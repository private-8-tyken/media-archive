from __future__ import annotations
import os
import shutil
import mimetypes
from pathlib import Path
from typing import Iterable, Literal, Dict, List, Optional

try:
    from tqdm.auto import tqdm as _tqdm
except Exception:
    _tqdm = None

# ---- classification rules (ext-driven with MIME fallback) ----
VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".tiff"}
GIF_EXT   = {".gif"}  # .gif always -> Gifs

CATEGORY_NAMES = {"Images", "Videos", "Gifs"}

def _category_for(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    if ext in GIF_EXT:
        return "Gifs"
    if ext in VIDEO_EXT:
        return "Videos"
    if ext in IMAGE_EXT:
        return "Images"

    # Fallback: MIME sniff
    mtype, _ = mimetypes.guess_type(str(path))
    if not mtype:
        return None
    if mtype == "image/gif":
        return "Gifs"
    if mtype.startswith("image/"):
        return "Images"
    if mtype.startswith("video/"):
        return "Videos"
    return None

def _iter_files(root: Path, patterns: Iterable[str] = ("**/*",)) -> Iterable[Path]:
    for patt in patterns:
        for p in root.glob(patt):
            if p.is_file():
                yield p

def _next_nonconflicting(dest: Path) -> Path:
    """If dest exists, append _1, _2, ... before suffix."""
    if not dest.exists():
        return dest
    stem, suf = dest.stem, dest.suffix
    i = 1
    while True:
        candidate = dest.with_name(f"{stem}_{i}{suf}")
        if not candidate.exists():
            return candidate
        i += 1

def _top_level_dir_under(base: Path, child: Path) -> Optional[str]:
    """
    If child is inside base, return the first path segment under base.
    e.g., base=/root, child=/root/ABC/01.jpg -> "ABC"
    If child == base/filename.jpg (no subdir), return None.
    """
    try:
        rel = child.resolve().relative_to(base.resolve())
    except Exception:
        return None
    parts = rel.parts
    if len(parts) >= 2:  # at least <dir>/<file>
        return parts[0]
    return None

def _prune_empty_dirs(
    root: Path,
    *,
    exclude: Optional[Path] = None,
    prune_junk: bool = True,
) -> List[str]:
    """
    Recursively delete empty directories under `root` (bottom-up).
    Never deletes `root` itself.

    `exclude`: if provided AND it lies *inside* `root`, skip pruning inside that subtree.
    If exclude is outside or a parent of `root`, it is ignored.
    """
    removed: List[str] = []

    root_res = root.resolve()
    exclude_res = exclude.resolve() if exclude else None

    # Only honor exclude if it's *inside* root
    exclude_effective = None
    if exclude_res:
        try:
            exclude_res.relative_to(root_res)  # succeeds only if exclude is inside root
            exclude_effective = exclude_res
        except Exception:
            exclude_effective = None  # exclude outside root -> ignore

    # Known junk files that can block "emptiness"
    junk_names = {".DS_Store", "Thumbs.db", "desktop.ini"} if prune_junk else set()

    for dirpath, dirnames, filenames in os.walk(root_res, topdown=False):
        dp = Path(dirpath)

        # Never delete the root itself
        if dp == root_res:
            continue

        # Skip exclude subtree *only if exclude is under root*
        if exclude_effective and (dp == exclude_effective or exclude_effective in dp.parents):
            continue

        # Optionally remove junk files to allow dir to become empty
        if prune_junk and filenames:
            for name in list(filenames):
                if name in junk_names:
                    try:
                        (dp / name).unlink(missing_ok=True)
                    except Exception:
                        pass  # ignore failures

        # If directory is now empty, remove it
        try:
            if not any(dp.iterdir()):
                dp.rmdir()
                removed.append(str(dp))
        except Exception:
            # Ignore directories we can't remove (permissions, etc.)
            pass

    return removed

def organize_downloads(
    *,
    input_dir: str | Path,
    output_dir: str | Path | None = None,
    include_patterns: Iterable[str] = ("**/*",),
    # What to do with files: move (default), copy, or link (symlink)
    strategy: Literal["move", "copy", "link"] = "move",
    # On conflict at destination: skip or rename (append _1, _2, ...)
    conflict: Literal["skip", "rename"] = "rename",
    show_progress: bool = True,
    dry_run: bool = False,
    prune_empty_galleries: bool = True,
) -> Dict[str, object]:
    """
    Organize media files into:
      - Images/
      - Videos/
      - Gifs/

    Gallery-aware behavior:
      - If a file came from a subfolder under input_dir (e.g., input_dir/<galleryID>/file.ext),
        it is treated as part of a "gallery".
      - Files are routed by type into category subfolders WHILE preserving the gallery name:
          Images/<galleryID>/..., Videos/<galleryID>/..., Gifs/<galleryID>/...
      - Mixed galleries are therefore split across category roots, each keeping the original gallery name.
      - Loose files (no subdir under input_dir) go directly under Images/, Videos/, or Gifs/ with no extra folder.

    After moving, optionally deletes now-empty folders inside `input_dir`
    (only meaningful when strategy="move").

    Already-organized files (i.e., those already under output_dir/Images|Videos|Gifs) are skipped.

    Returns a stats dict.
    """
    in_dir = Path(input_dir).resolve()
    out_dir = Path(output_dir).resolve() if output_dir else in_dir

    stats: Dict[str, object] = {
        "moved": 0,
        "copied": 0,
        "linked": 0,
        "skipped": 0,
        "unknown": 0,
        "dry_run": dry_run,
        "strategy": strategy,
        "conflict": conflict,
        "input_dir": str(in_dir),
        "output_dir": str(out_dir),
        "errors": [],           # list[dict]: {"file":..., "error":...}
        "created_dirs": set(),  # set[str]
        "pruned_dirs": [],      # list[str]
    }

    files = list(_iter_files(in_dir, include_patterns))
    iterator = files
    if show_progress and _tqdm is not None:
        iterator = _tqdm(iterator, total=len(files), unit="file", desc="Organizing media")

    created_dirs: set[str] = set()

    for src in iterator:
        try:
            # Skip if already inside an output category folder (avoid thrash)
            try:
                rel_out = src.resolve().relative_to(out_dir)
                first = rel_out.parts[0] if rel_out.parts else ""
                if first in CATEGORY_NAMES:
                    stats["skipped"] = int(stats["skipped"]) + 1
                    continue
            except Exception:
                pass  # src not under out_dir

            cat = _category_for(src)
            if not cat:
                stats["unknown"] = int(stats["unknown"]) + 1
                continue

            # Determine gallery name: top-level folder directly under input_dir
            gallery_name = _top_level_dir_under(in_dir, src)

            # Build destination folder:
            #   - If gallery_name exists: <out>/<Category>/<gallery_name>/
            #   - Else:                   <out>/<Category>/
            target_dir = out_dir / cat
            if gallery_name and gallery_name not in CATEGORY_NAMES:
                target_dir = target_dir / gallery_name

            # Create directory only when needed
            if not dry_run and not target_dir.exists():
                target_dir.mkdir(parents=True, exist_ok=True)
                created_dirs.add(str(target_dir))

            dest = target_dir / src.name

            if dest.exists():
                if conflict == "skip":
                    stats["skipped"] = int(stats["skipped"]) + 1
                    continue
                elif conflict == "rename":
                    dest = _next_nonconflicting(dest)
                elif conflict == "move_existing":
                    # Move the source file to AlreadyMoved/
                    rel = src.relative_to(out_dir)
                    already_dir = out_dir / "AlreadyMoved" / rel.parent
                    already_dir.mkdir(parents=True, exist_ok=True)
                    alt_path = already_dir / src.name
                    shutil.move(str(src), str(alt_path))
                    stats["skipped"] = int(stats["skipped"]) + 1
                    print(f"[SKIPPED→AlreadyMoved] {src.name} → {alt_path}")
                    continue

            if dry_run:
                continue

            if strategy == "move":
                shutil.move(str(src), str(dest))
                stats["moved"] = int(stats["moved"]) + 1
            elif strategy == "copy":
                shutil.copy2(str(src), str(dest))
                stats["copied"] = int(stats["copied"]) + 1
            elif strategy == "link":
                # Create relative symlink (POSIX). On Windows, may require admin/dev mode.
                rel = os.path.relpath(src, start=target_dir)
                (target_dir / dest.name).symlink_to(rel)
                stats["linked"] = int(stats["linked"]) + 1
            else:
                raise ValueError(f"Unknown strategy: {strategy}")

        except Exception as e:
            errors: List[dict] = stats["errors"]  # type: ignore[assignment]
            errors.append({"file": str(src), "error": str(e)})

    stats["created_dirs"] = created_dirs

    # Post-move pruning of empty original gallery folders (only makes sense for 'move')
        # Post-move pruning of empty original gallery folders
    if not dry_run and strategy == "move" and prune_empty_galleries:
        pruned = _prune_empty_dirs(
            in_dir,
            # Only exclude if output_dir lies inside input_dir; helper handles this.
            exclude=out_dir if out_dir != in_dir else None,
            prune_junk=True,
        )
        stats["pruned_dirs"] = pruned

    return stats

# ---- Optional tiny CLI ----
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(
        description="Organize media into Images/, Videos/, and Gifs/ with gallery preservation; prune empty source folders."
    )
    p.add_argument("input_dir", help="Directory containing downloaded files (galleries as subfolders).")
    p.add_argument("--output-dir", help="Destination root (default: organize in-place under input_dir).")
    p.add_argument("--strategy", choices=["move", "copy", "link"], default="move", help="How to materialize organized files.")
    p.add_argument( "--conflict", choices=["skip", "rename", "move_existing"], default="rename", help="On name conflicts at destination." )
    p.add_argument("--no-progress", action="store_true", help="Disable tqdm progress bar.")
    p.add_argument("--dry-run", action="store_true", help="Plan only; do not modify files.")
    p.add_argument("--no-prune", action="store_true", help="Do not delete empty folders from input_dir after moving.")
    args = p.parse_args()

    res = organize_downloads(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        strategy=args.strategy,
        conflict=args.conflict,
        show_progress=not args.no_progress,
        dry_run=args.dry_run,
        prune_empty_galleries=not args.no_prune,
    )
    print(res)
