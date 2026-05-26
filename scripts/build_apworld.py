"""Pack apworld/smbw_archipelago/ into a distributable .apworld zip.

Output: build/smbw_archipelago.apworld (gitignored).  Users drop it
into ~/.archipelago/custom_worlds/ to install.  Skipped on dev machines
that use the junction set up by scripts/install_smbw_apworld.py.
"""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "apworld" / "smbw_archipelago"
BUILD_DIR = REPO_ROOT / "build"
OUTPUT = BUILD_DIR / "smbw_archipelago.apworld"

# Skip __pycache__, .pyc, .pyo, and anything obvious from the test corpus.
EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache", "tests"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def main() -> int:
    if not SOURCE_DIR.is_dir():
        sys.stderr.write(f"missing source dir: {SOURCE_DIR}\n")
        return 1

    BUILD_DIR.mkdir(exist_ok=True)
    if OUTPUT.exists():
        OUTPUT.unlink()

    file_count = 0
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(SOURCE_DIR):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIR_NAMES]
            for name in files:
                if any(name.endswith(suf) for suf in EXCLUDE_SUFFIXES):
                    continue
                full = Path(root) / name
                # Archipelago's loader expects the inner directory name
                # to match the zip stem (smbw_archipelago/...).
                arc = Path("smbw_archipelago") / full.relative_to(SOURCE_DIR)
                zf.write(full, arcname=str(arc))
                file_count += 1

    size_kb = OUTPUT.stat().st_size / 1024
    print(f"wrote {OUTPUT} ({file_count} files, {size_kb:.1f} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
