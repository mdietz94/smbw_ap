"""Copy switch-mod build outputs to the user's chosen deploy target.

For v1, Ryujinx is the only supported deploy target. The artifact layout
matches what CLAUDE.md's "Daily dev loop" `Copy-Item` commands produce:

  %APPDATA%\\Ryujinx\\mods\\contents\\010015100b514000\\smbwap\\exefs\\
      subsdk9
      main.npdm     ← (renamed from `subsdk9.npdm` during copy)

The deploy module is intentionally separate from build.py so a user can
re-deploy a previously-built artifact (e.g. after switching Ryujinx
installs, or after Ryujinx wiped the mods folder) without re-running the
build phase.

Future: SD-card layout for M6 real-Switch target. The skeleton is here
(`_sd_layout`, `detect_sd_candidates`) and matches what CLAUDE.md's
build/deploy path looks like, but isn't wired into the wizard yet.
"""

from __future__ import annotations

import os
import shutil
import string
import sys
from dataclasses import dataclass
from pathlib import Path

# SMBW v1.0.0 Atmosphere title id — every offset in the mod is pinned to
# this; never apply the v1.0.1 update in Ryujinx.
SMBW_TITLE_ID = "010015100b514000"

# Ryujinx mod folder name. Matches the path CLAUDE.md's `Copy-Item`
# snippet targets; renaming this would orphan existing dev setups.
RYU_MOD_NAME = "smbwap"


@dataclass
class DeployResult:
    """Per-deploy summary returned to the wizard for the "Copied N files
    to ..." summary line. `files` is in source→dest tuple form so the
    wizard can render a small table if it wants."""
    ok: bool
    target: str
    files: list[tuple[Path, Path]]
    error: str = ""


def detect_ryujinx_path() -> Path | None:
    """Return `%APPDATA%/Ryujinx/` if it exists, else None.

    Matches the location Ryujinx itself defaults to on Windows. The
    wizard's Deploy page also lets the user browse to a non-default
    install; this function is just the auto-detect hint.
    """
    if sys.platform != "win32":
        # Non-Windows: best-effort. Tests can monkeypatch.
        appdata = os.environ.get("APPDATA")
        if appdata:
            p = Path(appdata) / "Ryujinx"
            return p if p.is_dir() else None
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    p = Path(appdata) / "Ryujinx"
    return p if p.is_dir() else None


def detect_sd_candidates() -> list[Path]:
    """Return all currently-mounted drive roots that look like a Switch
    SD card (i.e. have an `atmosphere/` directory at the root).

    Reserved for M6 real-Switch support. Not yet wired into the wizard.
    """
    if sys.platform != "win32":
        return []
    candidates: list[Path] = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:/")
        if not root.exists():
            continue
        atmo = root / "atmosphere"
        if atmo.is_dir():
            candidates.append(root)
    return candidates


def _ryujinx_layout(ryujinx_root: Path) -> dict[str, Path]:
    """Destination paths for the two artifacts inside a Ryujinx install.

    Note the rename: build emits `subsdk9.npdm`, but the Switch loader
    reads it as `main.npdm`. CLAUDE.md's `Copy-Item` does the same
    rename on copy.
    """
    base = ryujinx_root / "mods" / "contents" / SMBW_TITLE_ID / RYU_MOD_NAME / "exefs"
    return {
        "subsdk9": base / "subsdk9",
        "main.npdm": base / "main.npdm",
    }


def _sd_layout(sd_root: Path) -> dict[str, Path]:
    """Destination paths under a Switch SD card root.

    Layout: `<drive>:/atmosphere/contents/<TITLE_ID>/exefs/`. Reserved
    for M6; not yet wired into the wizard.
    """
    base = sd_root / "atmosphere" / "contents" / SMBW_TITLE_ID / "exefs"
    return {
        "subsdk9": base / "subsdk9",
        "main.npdm": base / "main.npdm",
    }


class DeployCopyError(OSError):
    """`_copy_files` raises this in place of a bare OSError so the wizard's
    error handler can display the source/destination context the user
    needs to diagnose the failure."""


def _copy_files(
    sources: dict[str, Path],
    dests: dict[str, Path],
) -> list[tuple[Path, Path]]:
    """Copy each (source, dest) pair, creating parent dirs.

    Returns the list of (source, dest) actually copied. Raises
    `DeployCopyError` on any IO error, with the failing pair embedded in
    the message. After a successful copy each destination's size is
    asserted equal to the source's size — some Windows file-system
    filters can return early before the bytes have landed, and a partial
    write looks identical to a successful deploy until the game tries
    to load the malformed file. A size mismatch is treated as a copy
    failure.
    """
    copied: list[tuple[Path, Path]] = []
    for key, src in sources.items():
        dst = dests[key]
        try:
            src_size = src.stat().st_size
        except OSError as e:
            raise DeployCopyError(
                f"Source file unreadable before copy: {src} ({e}). "
                f"Re-run the Build step to regenerate it."
            ) from e
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise DeployCopyError(
                f"Could not create destination directory {dst.parent} "
                f"for {key}: {e}. Check that the target drive is "
                f"writable and has free space."
            ) from e
        try:
            shutil.copy2(src, dst)
        except OSError as e:
            raise DeployCopyError(
                f"Failed to copy {src.name} to {dst}: {e}. "
                f"If this is Ryujinx, check it's not running with the "
                f"mod file locked; if SD card, check the drive is "
                f"writable and inserted."
            ) from e
        try:
            actual = dst.stat().st_size
        except OSError as e:
            raise DeployCopyError(
                f"Copied {src.name} to {dst} but couldn't stat the "
                f"result: {e}. The destination may have been deleted "
                f"or the drive disconnected during the copy."
            ) from e
        if actual != src_size:
            raise DeployCopyError(
                f"Partial write: {src.name} is {src_size} bytes but "
                f"{dst} is {actual} bytes. Free space (or reconnect "
                f"the SD card) and re-run Deploy."
            )
        copied.append((src, dst))
    return copied


def deploy_to_ryujinx(
    ryujinx_root: Path,
    build_outputs: dict[str, Path],
) -> DeployResult:
    """Copy build outputs to a Ryujinx install root.

    `build_outputs` must include keys `"subsdk9"` and `"main.npdm"`
    pointing at the artifacts produced by `_setup.build.cmake_build()`.
    """
    try:
        dests = _ryujinx_layout(ryujinx_root)
        copied = _copy_files(build_outputs, dests)
        return DeployResult(
            ok=True,
            target=f"Ryujinx at {ryujinx_root}",
            files=copied,
        )
    except (OSError, PermissionError) as e:
        cause = e.__cause__ if isinstance(e, DeployCopyError) and e.__cause__ else e
        return DeployResult(
            ok=False,
            target=f"Ryujinx at {ryujinx_root}",
            files=[],
            error=f"{type(cause).__name__}: {e}",
        )


def deploy_to_sd(sd_root: Path, build_outputs: dict[str, Path]) -> DeployResult:
    """Copy build outputs to a Switch SD card root. Reserved for M6."""
    try:
        dests = _sd_layout(sd_root)
        copied = _copy_files(build_outputs, dests)
        return DeployResult(
            ok=True,
            target=f"SD card at {sd_root}",
            files=copied,
        )
    except (OSError, PermissionError) as e:
        cause = e.__cause__ if isinstance(e, DeployCopyError) and e.__cause__ else e
        return DeployResult(
            ok=False,
            target=f"SD card at {sd_root}",
            files=[],
            error=f"{type(cause).__name__}: {e}",
        )


def deploy_to_custom_folder(
    custom_root: Path,
    build_outputs: dict[str, Path],
) -> DeployResult:
    """Copy build outputs to an arbitrary folder using the SD-card layout.

    Useful when the user wants to manage SD-card sync themselves. Writes
    the same `atmosphere/contents/<TITLE_ID>/exefs/` subtree the SD-card
    deploy produces, just under the user's chosen folder root.
    """
    try:
        dests = _sd_layout(custom_root)
        copied = _copy_files(build_outputs, dests)
        return DeployResult(
            ok=True,
            target=f"Custom folder at {custom_root}",
            files=copied,
        )
    except (OSError, PermissionError) as e:
        cause = e.__cause__ if isinstance(e, DeployCopyError) and e.__cause__ else e
        return DeployResult(
            ok=False,
            target=f"Custom folder at {custom_root}",
            files=[],
            error=f"{type(cause).__name__}: {e}",
        )
