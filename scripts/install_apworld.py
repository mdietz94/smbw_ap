"""Zip apworld/smbw_archipelago/ into vendor/Archipelago/custom_worlds/smbwonder.apworld.

An `.apworld` file is just a zip with the world package at its root.
Archipelago 0.5+ auto-discovers `.apworld` files in
`<checkout>/custom_worlds/` at startup, so this is the supported way to
ship a custom world without polluting `vendor/Archipelago/worlds/`
(which would also get clobbered by `git submodule update`).

The zip is named `smbwonder.apworld` (so Archipelago imports the world
as `worlds.smbwonder`) while the source folder on disk stays
`apworld/smbw_archipelago/` — the in-repo name was kept to avoid
churning every dev-workflow path reference, but the deployed/distributed
identifier is `smbwonder`.

Idempotent: re-running overwrites the existing zip.

Run from anywhere; paths resolve relative to this file.

    # Dev / unit-test default: apworld files only
    python scripts/install_apworld.py

    # Release build: also bundle switch-mod sources (incl. lib submodules)
    # under _setup/switch_mod/ inside the zip so the first-run wizard has
    # everything it needs to compile the Switch mod on the user's machine.
    python scripts/install_apworld.py --bundle-mod
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "apworld" / "smbw_archipelago"
DST_DIR = REPO / "vendor" / "Archipelago" / "custom_worlds"
DST = DST_DIR / "smbwonder.apworld"

# Skipped when collecting apworld files. The `tests` directory ships ~200
# unit tests + fixtures that bloat the zip and have no runtime purpose
# on the user's machine.
SKIP_NAMES = {"__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", "tests"}

# IP discipline note: SMO ships a CLIENT_DATA_IP_BLOCKLIST gate here for
# Nintendo-IP files (shine_map.json, capture_map.json) that its
# extract_shine_map.py produces from a user's NSP. SMBW currently has no
# analogous NSP-extraction pipeline — all in-tree tables (badge_table.py,
# coin_table.py, royal_seed_table.py, location_table.py) are hand-built
# from cross-referenced sources. If a future SMBW maintainer adds an
# NSP-extractor that drops localized strings under client/data/, wire a
# blocklist here following the pattern in
# smo_archipelago/scripts/install_apworld.py.

# Extra dirs skipped from --bundle-mod (the switch-mod tree contains
# build artifacts and dev-only outputs that bloat the zip).
MOD_SKIP_NAMES = SKIP_NAMES | {
    "build",          # cmake build dir
    ".git",
    ".github",
    ".vscode",
}

# Extension blacklist for --bundle-mod (binaries that have no business
# in the source bundle).
MOD_SKIP_SUFFIXES = {".exe", ".dll", ".pdb", ".obj", ".o", ".a"}

# Submodule paths under switch-mod/lib/ that must be populated for a
# --bundle-mod release to produce a buildable bundle. The user-facing
# install_apworld --bundle-mod requires `git submodule update --init
# --recursive`; this check surfaces the failure at bundle time with a
# clear instruction rather than letting the user discover it as a cmake
# error on their machine.
SWITCH_MOD_SUBMODULES = (
    Path("lib") / "imgui",
    Path("lib") / "NintendoSDK",
    Path("lib") / "sead",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--bundle-mod",
        action="store_true",
        help="Also copy switch-mod/ sources (incl. lib submodule contents) "
             "under _setup/switch_mod/ in the zip. Required for the "
             "first-run wizard to build subsdk9 on the user's machine.",
    )
    return p.parse_args(argv)


def _collect_files(root: Path, *, skip_names: set[str],
                   skip_suffixes: set[str] | None = None) -> list[Path]:
    """Recursively gather files under `root`, skipping any path whose
    parts intersect `skip_names` or whose suffix is in `skip_suffixes`."""
    files: list[Path] = []
    skip_suffixes = skip_suffixes or set()
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in skip_names for part in p.parts):
            continue
        if p.suffix.lower() in skip_suffixes:
            continue
        files.append(p)
    return files


def _check_submodule_populated(path: Path) -> bool:
    """True iff `path` exists and contains at least one entry.

    Uninitialized git submodules leave their target directory empty (or
    missing entirely). A populated submodule has content. We avoid
    named-file sentinels because upstream repos rearrange their top-level
    files; "directory is non-empty" is the actual semantic we want.
    """
    if not path.is_dir():
        return False
    try:
        next(path.iterdir())
        return True
    except StopIteration:
        return False


def _add_to_zip(zf: zipfile.ZipFile, src: Path, arcname: Path) -> None:
    """Write src to zf at arcname, using forward-slash POSIX arcnames so
    the zip works the same on Windows/Linux/Mac."""
    zf.write(src, arcname.as_posix())


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not SRC.is_dir():
        print(f"FAIL: apworld source not found at {SRC}", file=sys.stderr)
        return 2
    if not DST_DIR.parent.is_dir():
        print(f"FAIL: Archipelago checkout not found at {DST_DIR.parent}",
              file=sys.stderr)
        print("      (run `git submodule update --init --recursive` first)",
              file=sys.stderr)
        return 2
    DST_DIR.mkdir(parents=True, exist_ok=True)

    apworld_files = _collect_files(SRC, skip_names=SKIP_NAMES)
    if not apworld_files:
        print(f"FAIL: no files under {SRC}", file=sys.stderr)
        return 2

    bundled_mod_files: list[Path] = []
    if args.bundle_mod:
        mod_root = REPO / "switch-mod"
        if not mod_root.is_dir():
            print(f"FAIL: --bundle-mod requested but {mod_root} missing",
                  file=sys.stderr)
            return 2
        # Submodule presence check: each lib submodule must be populated
        # (non-empty directory). Without these the bundled tree would
        # produce a cmake error on the user's machine. `--recursive` is
        # required here because the lib/ submodules are siblings, not
        # nested — but a future maintainer adding a nested submodule
        # should keep the recursive form in the user instruction.
        for rel in SWITCH_MOD_SUBMODULES:
            sub = mod_root / rel
            if not _check_submodule_populated(sub):
                print(
                    f"FAIL: --bundle-mod requested but submodule "
                    f"{rel.as_posix()} is not initialized (empty or "
                    f"missing at {sub}). Run "
                    f"`git submodule update --init --recursive` first.",
                    file=sys.stderr,
                )
                return 2
        bundled_mod_files = _collect_files(
            mod_root,
            skip_names=MOD_SKIP_NAMES,
            skip_suffixes=MOD_SKIP_SUFFIXES,
        )

    with zipfile.ZipFile(DST, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in apworld_files:
            # Inside the zip, paths must be `smbwonder/...` so Archipelago
            # imports it as `worlds.smbwonder` — the upstream loader does
            # `importer.find_spec(f"worlds.{Path(apworld.path).stem}")`,
            # so the inner folder name MUST equal the zip stem.
            _add_to_zip(zf, p, Path("smbwonder") / p.relative_to(SRC))

        # Bundled mod sources land under smbwonder/_setup/switch-mod/ so
        # extraction at runtime yields the same on-disk layout the dev
        # checkout has at <repo>/switch-mod/, letting build.py's
        # switch_mod_root(repo) -> repo / "switch-mod" math work for
        # both shapes without per-call dispatch.
        for p in bundled_mod_files:
            _add_to_zip(
                zf, p,
                Path("smbwonder") / "_setup" / "switch-mod" /
                p.relative_to(REPO / "switch-mod"),
            )

    total_files = len(apworld_files) + len(bundled_mod_files)
    size_kb = DST.stat().st_size / 1024
    extras = f" (+{len(bundled_mod_files)} mod)" if args.bundle_mod else ""
    print(f"OK: wrote {DST} ({total_files} files{extras}, {size_kb:.1f} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
