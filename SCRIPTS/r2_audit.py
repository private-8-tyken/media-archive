# r2_audit.py
"""
Audit local media files (including gallery subfolders) against Cloudflare R2 keys,
across one or multiple R2 accounts.

Supports recursive matching of both:
  - post_id.jpeg
  - post_id/01.jpeg (gallery folders)

Env (multi-account, comma-separated; aligned by index):
  R2_ENDPOINTS
  R2_ACCESS_KEY_IDS
  R2_SECRET_ACCESS_KEYS
  R2_BUCKETS                # optional; if single, reused for all
Fallback (single-account):
  R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET

Usage (Python):
    from pathlib import Path
    from r2_audit import audit_local_vs_r2

    results = audit_local_vs_r2(
        local_root=Path("Out/media_files"),
        r2_prefixes=["Images", "RedGiphys", "Gifs", "Videos"],
        account_indices=None,  # None = audit ALL configured accounts
        write_csv_to=Path("Media/__reports/r2_audit.csv"),
        show_progress=True
    )

CLI:
    python r2_audit.py --local-root Out/media_files --prefixes Images,RedGiphys,Gifs,Videos --accounts all --csv Media/__reports/r2_audit.csv --progress
"""

from __future__ import annotations

import os
import csv
from pathlib import Path
from collections import defaultdict, Counter
from typing import Iterable, Optional, Dict, Any, List, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, BotoCoreError

# ✅ Auto-load .env credentials
try:
    from dotenv import load_dotenv
    for candidate in [Path(".env"), Path(__file__).resolve().parent / ".env", Path(__file__).resolve().parent.parent / ".env"]:
        if candidate.exists():
            load_dotenv(candidate, override=False)
            break
except Exception:
    pass

try:
    from tqdm import tqdm as _tqdm
except Exception:
    _tqdm = None


# ---------------------------------------------------------------------------
# Env & accounts helpers
# ---------------------------------------------------------------------------

def _split_env(name: str) -> List[str]:
    raw = os.getenv(name, "") or ""
    return [p.strip() for p in raw.split(",") if p.strip()]

def _multi_accounts_available() -> bool:
    return all(len(_split_env(n)) > 0 for n in ["R2_ENDPOINTS", "R2_ACCESS_KEY_IDS", "R2_SECRET_ACCESS_KEYS"])

def _accounts_count() -> int:
    if _multi_accounts_available():
        return min(len(_split_env("R2_ENDPOINTS")), len(_split_env("R2_ACCESS_KEY_IDS")), len(_split_env("R2_SECRET_ACCESS_KEYS")))
    # single-account fallback
    return 1 if os.getenv("R2_ENDPOINT") and os.getenv("R2_ACCESS_KEY_ID") and os.getenv("R2_SECRET_ACCESS_KEY") else 0

def _materialize_account(idx: int) -> Dict[str, str | int]:
    if _multi_accounts_available():
        endpoints = _split_env("R2_ENDPOINTS")
        access_ids = _split_env("R2_ACCESS_KEY_IDS")
        secrets   = _split_env("R2_SECRET_ACCESS_KEYS")
        buckets   = _split_env("R2_BUCKETS")  # optional; may be 0/1/N length

        n = min(len(endpoints), len(access_ids), len(secrets))
        if idx < 0 or idx >= n:
            raise RuntimeError(f"account_idx out of range ({idx}); configured={n}")

        bucket = ""
        if buckets:
            bucket = buckets[0] if len(buckets) == 1 else (buckets[idx] if idx < len(buckets) else "")
        if not bucket:
            bucket = os.getenv("R2_BUCKET", "")

        if not bucket:
            raise RuntimeError("No bucket configured. Provide R2_BUCKETS or R2_BUCKET.")

        return {
            "index": idx,
            "count": n,
            "endpoint": endpoints[idx],
            "access_key": access_ids[idx],
            "secret_key": secrets[idx],
            "bucket": bucket,
        }

    # single-account fallback
    endpoint = os.getenv("R2_ENDPOINT")
    access   = os.getenv("R2_ACCESS_KEY_ID")
    secret   = os.getenv("R2_SECRET_ACCESS_KEY")
    bucket   = os.getenv("R2_BUCKET")
    if not all([endpoint, access, secret, bucket]):
        raise RuntimeError("Missing single-account envs (R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET).")
    return {
        "index": 0,
        "count": 1,
        "endpoint": endpoint,
        "access_key": access,
        "secret_key": secret,
        "bucket": bucket,
    }

def _s3_client(account: Dict[str, str | int]):
    return boto3.client(
        "s3",
        endpoint_url=str(account["endpoint"]),
        aws_access_key_id=str(account["access_key"]),
        aws_secret_access_key=str(account["secret_key"]),
        region_name=os.getenv("R2_REGION", "auto"),
        config=Config(signature_version="s3v4"),
    )


# ---------------------------------------------------------------------------
# Listing & key utils
# ---------------------------------------------------------------------------

def _posix_rel(local_path: Path, root: Path) -> str:
    """Relative path with forward slashes (S3-style)."""
    return local_path.relative_to(root).as_posix()

def _make_key(prefix: str, rel: str) -> str:
    prefix = (prefix or "").strip().strip("/")
    return f"{prefix}/{rel}" if prefix else rel

def _list_remote_objects(s3, bucket: str, prefix: Optional[str] = None) -> Dict[str, Any]:
    """Return {key: s3_object_dict} for the given (optional) prefix."""
    paginator = s3.get_paginator("list_objects_v2")
    kwargs = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix
    by_key: Dict[str, Any] = {}
    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []):
            by_key[obj["Key"]] = obj
    return by_key

def _split_dir_stem_ext(key: str) -> Tuple[str, str, str, str]:
    """
    Return (dir_with_trailing_slash_or_empty, stem, ext_with_dot_or_empty, filename).
    For example:
        "Images/abc/01.jpeg" -> ("Images/abc/", "01", ".jpeg", "01.jpeg")
        "Images/abc.jpeg"    -> ("Images/", "abc", ".jpeg", "abc.jpeg")
    """
    if "/" in key:
        d, fname = key.rsplit("/", 1)
        d += "/"
    else:
        d, fname = "", key
    if "." in fname:
        st, ex = fname.rsplit(".", 1)
        return d, st, "." + ex.lower(), fname
    else:
        return d, fname, "", fname

def _resolve_prefixes(r2_prefixes: Optional[Iterable[str]]) -> List[str]:
    """Resolve prefixes from args → env → default."""
    if r2_prefixes:
        return [p.strip().strip("/") for p in r2_prefixes if str(p).strip()]
    env_multi = os.getenv("R2_PREFIXES")
    if env_multi:
        return [p.strip().strip("/") for p in env_multi.split(",") if p.strip()]
    env_single = (os.getenv("R2_PREFIX") or "").strip().strip("/")
    if env_single:
        return [env_single]
    return ["Images", "RedGiphys", "Gifs", "Videos"]


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------

def audit_local_vs_r2(
    local_root: Path | str,
    *,
    r2_prefixes: Optional[Iterable[str]] = None,
    account_indices: Optional[Iterable[int]] = None,  # None = audit ALL configured accounts
    write_csv_to: Optional[Path | str] = None,
    show_progress: bool = False,
) -> Dict[str, Any]:
    """
    Compare local files (recursively) under `local_root` against Cloudflare R2 keys across prefixes
    and across one/many accounts.

    Returns an object with per-file rows, summary counts, and environment config.
    """

    root = Path(local_root).resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Local images root not found or not a directory: {root}")

    prefixes = _resolve_prefixes(r2_prefixes)

    # Figure which accounts to audit
    total_accounts = _accounts_count()
    if total_accounts == 0:
        raise RuntimeError("No R2 account configuration found.")

    if account_indices is None:
        use_indices = list(range(total_accounts))
    else:
        use_indices = sorted(set(int(i) for i in account_indices))
        for i in use_indices:
            if i < 0 or i >= total_accounts:
                raise ValueError(f"Account index {i} is out of range (0..{total_accounts-1}).")

    # Build remote maps per account+prefix
    remote_by_acc_prefix: Dict[int, Dict[str, Any]] = {}
    by_dir_stem_by_acc: Dict[int, Dict[Tuple[str, str], List[Tuple[str, str]]]] = {}
    prefix_key_counts_by_acc: Dict[int, Counter[str]] = {}

    for i in use_indices:
        acc = _materialize_account(i)
        s3 = _s3_client(acc)

        prefix_key_counts: Counter[str] = Counter()
        remote_by_key: Dict[str, Any] = {}

        for pref in prefixes:
            pref_norm = pref.strip().strip("/")
            submap = _list_remote_objects(s3, str(acc["bucket"]), pref_norm if pref_norm else None)
            remote_by_key.update(submap)
            prefix_key_counts[pref_norm or "(root)"] += len(submap)

        # Build same-stem index for this account
        by_dir_stem: Dict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(list)
        for key in remote_by_key:
            d, st, ex, _ = _split_dir_stem_ext(key)
            by_dir_stem[(d, st)].append((key, ex))

        remote_by_acc_prefix[i] = remote_by_key
        by_dir_stem_by_acc[i] = by_dir_stem
        prefix_key_counts_by_acc[i] = prefix_key_counts

    # Scan local files
    local_files = [p for p in root.rglob("*") if p.is_file()]
    iterator = local_files
    if show_progress and _tqdm is not None:
        iterator = _tqdm(local_files, total=len(local_files), unit="file", desc="Auditing")

    rows: List[Dict[str, Any]] = []
    totals: Counter[str] = Counter()
    per_prefix_exact: Counter[str] = Counter()
    per_prefix_same_stem: Counter[str] = Counter()

    for local in iterator:
        local_rel = _posix_rel(local, root)
        local_ext = (("." + local.suffix.lower().lstrip(".")) if local.suffix else "").lower()
        expected_keys_per_prefix = [_make_key(pref, local_rel) for pref in prefixes]

        matched = False
        match_type = "missing"
        matched_prefix = ""
        matched_key = ""
        remote_ext = ""
        same_ext = False
        note = ""
        matched_account_index: Optional[int] = None
        matched_bucket: str = ""

        # Check each account for exact match first
        for acc_idx in use_indices:
            remote_by_key = remote_by_acc_prefix[acc_idx]
            for pref, key in zip(prefixes, expected_keys_per_prefix):
                if key in remote_by_key:
                    matched = True
                    match_type = "exact"
                    matched_prefix = pref
                    matched_key = key
                    _, _, remote_ext, _ = _split_dir_stem_ext(key)
                    same_ext = (remote_ext == local_ext)
                    matched_account_index = acc_idx
                    matched_bucket = str(_materialize_account(acc_idx)["bucket"])
                    note = "exact_match"
                    totals["exact"] += 1
                    per_prefix_exact[pref or "(root)"] += 1
                    break
            if matched:
                break

        # If still not matched, look for same-stem (e.g., .jpg vs .jpeg) in any account
        if not matched:
            for acc_idx in use_indices:
                by_dir_stem = by_dir_stem_by_acc[acc_idx]
                for pref, key in zip(prefixes, expected_keys_per_prefix):
                    parent_dir, stem, _, _ = _split_dir_stem_ext(key)
                    candidates = by_dir_stem.get((parent_dir, stem), [])
                    if candidates:
                        alt_key, remote_ext = candidates[0]
                        matched = True
                        match_type = "same_stem"
                        matched_prefix = pref
                        matched_key = alt_key
                        same_ext = (remote_ext == local_ext)
                        matched_account_index = acc_idx
                        matched_bucket = str(_materialize_account(acc_idx)["bucket"])
                        note = "found_same_stem_same_ext" if same_ext else "found_same_stem_diff_ext"
                        totals["same_stem"] += 1
                        per_prefix_same_stem[pref or "(root)"] += 1
                        break
                if matched:
                    break

        if not matched:
            totals["missing"] += 1
            note = "missing"

        rows.append({
            "local_rel": local_rel,
            "local_ext": local_ext or "",
            "all_expected_keys": " | ".join(expected_keys_per_prefix),
            "matched": matched,
            "match_type": match_type,
            "matched_prefix": matched_prefix or "",
            "matched_key": matched_key,
            "remote_ext": remote_ext or "",
            "same_ext": same_ext,
            "matched_account_index": (matched_account_index if matched_account_index is not None else ""),
            "matched_bucket": matched_bucket,
            "note": note,
        })

    # Aggregate remote counts across accounts (for quick overview)
    combined_prefix_counts: Counter[str] = Counter()
    for acc_idx in use_indices:
        for k, v in prefix_key_counts_by_acc[acc_idx].items():
            combined_prefix_counts[k] += v

    # Write CSV if requested
    csv_path: Optional[Path] = None
    if write_csv_to is not None:
        write_csv_to = Path(write_csv_to)
        csv_path = write_csv_to / "r2_audit.csv" if write_csv_to.is_dir() else write_csv_to
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        header = [
            "local_rel", "local_ext", "all_expected_keys",
            "matched", "match_type", "matched_prefix", "matched_key",
            "remote_ext", "same_ext",
            "matched_account_index", "matched_bucket",
            "note",
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            w.writerows(rows)

    return {
        "total_files": len(rows),
        "totals": totals,
        "per_prefix_exact": per_prefix_exact,
        "per_prefix_same_stem": per_prefix_same_stem,
        "prefix_key_counts_combined": combined_prefix_counts,
        "prefix_key_counts_by_account": {i: prefix_key_counts_by_acc[i] for i in use_indices},
        "rows": rows,
        "csv_path": csv_path,
        "config": {
            "local_root": str(root),
            "prefixes": prefixes,
            "audited_accounts": use_indices,
            "accounts_total": total_accounts,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="Audit local media against Cloudflare R2 (multi-account).")
    ap.add_argument("--local-root", required=True, help="Path to local media root")
    ap.add_argument("--prefixes", default=None, help="Comma-separated list of prefixes (default: Images,RedGiphys,Gifs,Videos)")
    ap.add_argument("--accounts", default="all", help="'all' or comma-separated indices, e.g. 0,1")
    ap.add_argument("--csv", default=None, help="Write CSV to this file or directory")
    ap.add_argument("--progress", action="store_true", help="Show progress bar")

    args = ap.parse_args()
    prefixes = [p.strip() for p in args.prefixes.split(",")] if args.prefixes else None

    if args.accounts.strip().lower() == "all":
        accs = None
    else:
        accs = [int(x.strip()) for x in args.accounts.split(",") if x.strip()]

    res = audit_local_vs_r2(
        local_root=Path(args.local_root),
        r2_prefixes=prefixes,
        account_indices=accs,
        write_csv_to=(Path(args.csv) if args.csv else None),
        show_progress=args.progress,
    )

    cfg = res["config"]
    totals = res["totals"]

    print(f"Local root: {cfg['local_root']}")
    print("Prefixes:", [p or '(root)' for p in cfg['prefixes']])
    print(f"Audited accounts: {cfg['audited_accounts']} (of {cfg['accounts_total']})")

    print("\nRemote objects per prefix (combined across audited accounts):")
    for p in cfg['prefixes']:
        key = p or "(root)"
        print(f"  {key}: {res['prefix_key_counts_combined'][key]}")

    print(f"\nLocal files scanned: {res['total_files']}")
    print(f"  Exact matches:      {totals.get('exact', 0)}")
    print(f"  Same-stem matches:  {totals.get('same_stem', 0)}")
    print(f"  Missing:            {totals.get('missing', 0)}")

    if res["csv_path"]:
        print(f"\nCSV written: {res['csv_path']}")

if __name__ == "__main__":
    main()
