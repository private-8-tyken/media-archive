#!/usr/bin/env python3
"""
cloudflareUploader.py
Multi-account Cloudflare R2 uploader:
- Checks ALL configured accounts for existing keys (no cross-db dupes)
- Uploads ONLY to the selected account (account_idx)
- Per-row 'status' on every record (planned, exists, missing, uploaded, skipped, failed, would-*)

Env (comma-separated lists; aligned by index):
  R2_ENDPOINTS              = "https://acc1.r2.cloudflarestorage.com,https://acc2..."
  R2_ACCESS_KEY_IDS         = "key1,key2"
  R2_SECRET_ACCESS_KEYS     = "sec1,sec2"
  R2_BUCKETS                = "bucket1,bucket2"   # optional; if single value, reused

Also supported (single value):
  PUBLIC_MEDIA_BASE         = "https://<worker>/"

Usage (CLI):
  python cloudflareUploader.py --input MediaToUpload --prefix Images --account 0 --dry-run
  python cloudflareUploader.py --input MediaToUpload --prefix Images --account 1 --overwrite
  python cloudflareUploader.py --input MediaToUpload --prefix Images --account 0 --check-only
"""

from __future__ import annotations
import argparse, json, mimetypes, os
from pathlib import Path
from typing import Dict, List, Tuple, Optional

try:
    from dotenv import load_dotenv  # optional
    load_dotenv()
except Exception:
    pass

import boto3
from botocore.exceptions import ClientError, BotoCoreError

VALID_PREFIXES = {"Gifs", "Images", "RedGiphys", "Videos"}

# ---------- Env parsing ----------
def _split_env(name: str) -> List[str]:
    raw = os.getenv(name, "") or ""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts

def _get_env_list(name: str, required: bool = True) -> List[str]:
    parts = _split_env(name)
    if required and not parts:
        raise RuntimeError(f"Missing required env: {name}")
    return parts

def _get_bucket_for_index(buckets: List[str], idx: int) -> str:
    if not buckets:
        raise RuntimeError("No buckets configured. Set R2_BUCKETS or R2_BUCKET.")
    if len(buckets) == 1:
        return buckets[0]
    if idx < 0 or idx >= len(buckets):
        raise RuntimeError(f"bucket index out of range for R2_BUCKETS (idx={idx})")
    return buckets[idx]

def _accounts_count() -> int:
    endpoints = _get_env_list("R2_ENDPOINTS")
    access_ids = _get_env_list("R2_ACCESS_KEY_IDS")
    secrets   = _get_env_list("R2_SECRET_ACCESS_KEYS")
    return min(len(endpoints), len(access_ids), len(secrets))

def _materialize_account(idx: int):
    endpoints = _get_env_list("R2_ENDPOINTS")
    access_ids = _get_env_list("R2_ACCESS_KEY_IDS")
    secrets   = _get_env_list("R2_SECRET_ACCESS_KEYS")
    buckets   = _split_env("R2_BUCKETS")  # optional multi or single

    n = min(len(endpoints), len(access_ids), len(secrets))
    if n == 0:
        raise RuntimeError("R2 accounts not configured.")
    if idx < 0 or idx >= n:
        raise RuntimeError(f"account_idx out of range: {idx} (have {n})")

    endpoint = endpoints[idx]
    key_id   = access_ids[idx]
    secret   = secrets[idx]

    # allow legacy single R2_BUCKET
    bucket = _get_bucket_for_index(buckets, idx) if buckets else (os.getenv("R2_BUCKET", "") or None)
    if not bucket:
        raise RuntimeError("Bucket not specified. Provide R2_BUCKETS (comma list or single) or R2_BUCKET.")

    return {
        "endpoint": endpoint,
        "access_key": key_id,
        "secret_key": secret,
        "bucket": bucket,
        "index": idx,
        "count": n,
    }

def _all_accounts() -> List[dict]:
    n = _accounts_count()
    return [_materialize_account(i) for i in range(n)]

def _s3_client(account: dict):
    session = boto3.session.Session()
    return session.client(
        service_name="s3",
        endpoint_url=account["endpoint"],
        aws_access_key_id=account["access_key"],
        aws_secret_access_key=account["secret_key"],
    )

# ---------- Planning & helpers ----------
def _plan_objects(input_path: Path, prefix: str) -> List[Tuple[Path, str]]:
    plans: List[Tuple[Path, str]] = []
    # top-level files → <prefix>/<stem><ext>
    for p in input_path.iterdir():
        if p.is_file():
            plans.append((p, f"{prefix}/{p.stem}{p.suffix}"))
    # galleries (immediate subdirs) → preserve sub-structure under <prefix>/<gallery>/
    for d in [x for x in input_path.iterdir() if x.is_dir()]:
        root_name = d.name
        for f in d.rglob("*"):
            if f.is_file():
                rel = f.relative_to(d).as_posix()
                plans.append((f, f"{prefix}/{root_name}/{rel}"))
    return plans

def _guess_content_type(path: Path) -> Optional[str]:
    ctype, _ = mimetypes.guess_type(str(path))
    return ctype

def _object_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        code = str(e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
        if code == "404" or e.response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}:
            return False
        raise

def _object_exists_anywhere(key: str) -> Tuple[bool, Optional[int], Optional[str]]:
    """
    Check all configured R2 accounts for 'key'.
    Returns (exists_anywhere, found_account_idx, found_bucket).
    """
    for acc in _all_accounts():
        s3c = _s3_client(acc)
        try:
            if _object_exists(s3c, acc["bucket"], key):
                return True, acc["index"], acc["bucket"]
        except (ClientError, BotoCoreError):
            # if a particular account is temporarily unreachable, treat as not-found there
            continue
    return False, None, None

# ---------- Core API ----------
def upload_media(
    input_path: str | Path,
    r2_prefix: str,
    account_idx: int = 0,
    bucket: Optional[str] = None,
    dry_run: bool = False,
    overwrite: bool = False,
    check_only: bool = False,
) -> Dict:
    """
    Upload media under `input_path` to a selected R2 account (by index).

    Behavior:
      1) For each planned key, first check ALL accounts to avoid cross-db duplicates.
      2) If not present anywhere:
            - If not dry_run and not check_only: upload to selected account only.
      3) If present in any account: skip upload and mark where it exists.

    Modes:
      - check_only=True: only report existence anywhere; no uploads.
      - dry_run=True: report would-* actions; no uploads.
    """
    if r2_prefix not in VALID_PREFIXES:
        raise ValueError(f"prefix must be one of {sorted(VALID_PREFIXES)}")

    input_path = Path(input_path).resolve()
    if not input_path.exists() or not input_path.is_dir():
        raise FileNotFoundError(f"Input path not found or not a directory: {input_path}")

    account = _materialize_account(account_idx)
    if bucket:
        account["bucket"] = bucket

    # Destination client (selected account)
    s3_dest = None if (dry_run or check_only) else _s3_client(account)

    plans = _plan_objects(input_path, r2_prefix)

    report = {
        "account_index": account_idx,
        "accounts_total": account["count"],
        "endpoint": account["endpoint"],
        "bucket": account["bucket"],
        "prefix": r2_prefix,
        "input_path": str(input_path),
        "dry_run": bool(dry_run),
        "overwrite": bool(overwrite),      # only applies to selected account; cross-db duplicates are skipped regardless
        "check_only": bool(check_only),
        "planned": [],
        "uploaded": [],
        "skipped": [],
        "failed": [],
        "exists": [],     # for check_only: objects that already exist (any account)
        "missing": [],    # for check_only: objects that do not exist (any account)
        "summary": {
            "planned": 0, "uploaded": 0, "skipped": 0, "failed": 0,
            "bytes_uploaded": 0, "bytes_planned": 0,
            "exists": 0, "missing": 0
        },
    }

    # Plan
    for lp, key in plans:
        size = lp.stat().st_size
        entry = {
            "local": str(lp),
            "r2_key": key,
            "bytes": size,
            "content_type": _guess_content_type(lp),
            "status": "planned",
            "account_index": account_idx,   # selected destination
            "bucket": account["bucket"],
        }
        report["planned"].append(entry)
        report["summary"]["planned"] += 1
        report["summary"]["bytes_planned"] += size

    # Existence-only scan across ALL accounts (no uploads)
    if check_only:
        for lp, key in plans:
            size = lp.stat().st_size
            e = {
                "local": str(lp),
                "r2_key": key,
                "bytes": size,
                "status": None,
                "account_index": account_idx,
                "bucket": account["bucket"],
            }
            try:
                exists_any, found_idx, found_bucket = _object_exists_anywhere(key)
                if exists_any:
                    e["status"] = "exists"
                    e["found_in_account"] = found_idx
                    e["found_in_bucket"] = found_bucket
                    report["exists"].append(e)
                    report["summary"]["exists"] += 1
                else:
                    e["status"] = "missing"
                    report["missing"].append(e)
                    report["summary"]["missing"] += 1
            except Exception as err:
                e["status"] = "failed"
                e["error"] = repr(err)
                report["failed"].append(e)
                report["summary"]["failed"] += 1
        return report

    # Execute (or dry-run), with cross-account existence check first
    for lp, key in plans:
        size = lp.stat().st_size
        ctype = _guess_content_type(lp)
        entry = {
            "local": str(lp),
            "r2_key": key,
            "bytes": size,
            "content_type": ctype,
            "account_index": account_idx,
            "bucket": account["bucket"],
        }
        try:
            exists_any, found_idx, found_bucket = _object_exists_anywhere(key)

            if dry_run:
                if exists_any:
                    entry["status"] = "would-skip"
                    entry["reason"] = f"exists in account #{found_idx} ({found_bucket})"
                else:
                    entry["status"] = "would-upload"
                report["uploaded"].append(entry)
                report["summary"]["uploaded"] += 1
                continue

            # If present in ANY account, skip (prevents cross-db duplicates)
            if exists_any:
                entry["status"] = "skipped"
                entry["reason"] = f"exists in account #{found_idx} ({found_bucket})"
                report["skipped"].append(entry)
                report["summary"]["skipped"] += 1
                continue

            # Not present anywhere — upload to selected account
            extra = {}
            if ctype:
                extra["ContentType"] = ctype

            s3_dest.upload_file(
                Filename=str(lp),
                Bucket=account["bucket"],
                Key=key,
                ExtraArgs=extra or None
            )

            entry["status"] = "uploaded"
            report["uploaded"].append(entry)
            report["summary"]["uploaded"] += 1
            report["summary"]["bytes_uploaded"] += size

        except (ClientError, BotoCoreError, OSError) as e:
            entry["status"] = "failed"
            entry["error"] = repr(e)
            report["failed"].append(entry)
            report["summary"]["failed"] += 1

    return report

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(description="Upload media/galleries to Cloudflare R2 (multi-account; cross-account existence check).")
    ap.add_argument("--input", required=True, help="Folder with files and/or gallery subfolders")
    ap.add_argument("--prefix", required=True, choices=sorted(VALID_PREFIXES), help="Gifs | Images | RedGiphys | Videos")
    ap.add_argument("--account", type=int, default=0, help="Which R2 account index to use (0-based) for uploads")
    ap.add_argument("--bucket", help="Override bucket name for the selected account (optional)")
    ap.add_argument("--dry-run", action="store_true", help="Plan only; do not upload")
    ap.add_argument("--overwrite", action="store_true", help="(kept for compatibility) Overwrite in selected account (ignored when file exists in another account)")
    ap.add_argument("--check-only", action="store_true", help="Only check if objects exist across ALL accounts; no uploads")
    ap.add_argument("--print-json", action="store_true", help="Pretty-print result JSON")
    args = ap.parse_args()

    result = upload_media(
        input_path=args.input,
        r2_prefix=args.prefix,
        account_idx=args.account,
        bucket=args.bucket,
        dry_run=args.dry_run,
        overwrite=args.overwrite,
        check_only=args.check_only,
    )
    if args.print_json:
        print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
