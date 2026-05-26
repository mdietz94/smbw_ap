r"""Create the apworld dev-junction so Archipelago Launcher autodiscovers us.

Library used by both the `/setup` wizard's Junction phase and the
standalone `scripts/install_smbw_apworld.py` entry point. The junction
links `<repo>/vendor/Archipelago/custom_worlds/smbw_archipelago` →
`<repo>/apworld/smbw_archipelago`, so AP's apworld autodiscovery picks
up the dev source tree without needing to package an `.apworld` zip.

Supersedes the older `scripts/install_smbw_apworld.ps1` — the PS1
called `cmd /c mklink` which resolved to msys2's shell-shim `cmd` (not
Windows' `cmd.exe`) when `$DEVKITPRO\msys2\usr\bin` was on PATH and
failed with "Cannot run a document in the middle of a pipeline".
`subprocess.run(['cmd', '/c', ...])` from Python goes through the
Windows app-paths resolver and always lands on the real `cmd.exe`.

Idempotent: re-running over an existing junction is a no-op. Refuses to
overwrite a non-junction directory (might be a real install the user
doesn't want clobbered).

Windows-only in practice (uses `mklink /J`). On POSIX we fall back to a
symlink so tests can roundtrip without a Windows VM, even though the
wizard itself is Windows-only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .prereqs import repo_root

# Suppress per-child console window on Windows; no-op elsewhere.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class JunctionOutcome:
    ok: bool
    target: Path
    source: Path
    action: str    # "created" | "already_exists" | "error"
    message: str = ""


def apworld_source(repo: Path | None = None) -> Path:
    repo = repo if repo is not None else repo_root()
    return repo / "apworld" / "smbw_archipelago"


def junction_target(repo: Path | None = None) -> Path:
    repo = repo if repo is not None else repo_root()
    return repo / "vendor" / "Archipelago" / "custom_worlds" / "smbw_archipelago"


def is_reparse_point(path: Path) -> bool:
    """True if `path` is a Windows reparse point (junction or symlink) or
    a POSIX symlink. False if missing or a real directory."""
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink():
        return True
    if sys.platform == "win32":
        # `FILE_ATTRIBUTE_REPARSE_POINT` = 0x400
        try:
            import ctypes
            FILE_ATTRIBUTE_REPARSE_POINT = 0x400
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs == -1:
                return False
            return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
        except Exception:
            return False
    return False


def _create_junction_windows(target: Path, source: Path,
                             runner: Callable[[list[str]], tuple[int, str, str]] | None = None) -> tuple[int, str, str]:
    """Invoke `cmd /c mklink /J target source`. `runner` is injectable for
    tests so we don't actually shell out."""
    cmd = ["cmd", "/c", "mklink", "/J", str(target), str(source)]
    if runner is not None:
        return runner(cmd)
    res = subprocess.run(
        cmd, capture_output=True, text=True, timeout=10.0,
        creationflags=_NO_WINDOW,
    )
    return res.returncode, res.stdout or "", res.stderr or ""


def install_junction(
    repo: Path | None = None,
    *,
    runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
) -> JunctionOutcome:
    """Create the custom_worlds junction (or report what's already there).

    Returns a `JunctionOutcome` whose `action` field discriminates the
    three cases the caller cares about: created, already_exists, error.
    """
    repo = repo if repo is not None else repo_root()
    source = apworld_source(repo)
    target = junction_target(repo)

    if not source.is_dir():
        return JunctionOutcome(
            False, target, source, "error",
            f"apworld source not found: {source}",
        )

    custom_worlds = target.parent
    if not custom_worlds.is_dir():
        try:
            custom_worlds.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return JunctionOutcome(
                False, target, source, "error",
                f"could not create {custom_worlds}: {e}",
            )

    if target.exists() or target.is_symlink():
        if is_reparse_point(target):
            return JunctionOutcome(
                True, target, source, "already_exists",
                f"junction already exists at {target} -- leaving as-is",
            )
        return JunctionOutcome(
            False, target, source, "error",
            f"non-junction directory exists at {target} -- move it aside first",
        )

    if sys.platform == "win32":
        rc, out, err = _create_junction_windows(target, source, runner=runner)
        if rc != 0 or not target.exists():
            note = ""
            if "Access is denied" in (out + err) or "elevation" in (out + err).lower():
                note = " (is Developer Mode enabled or are you running as Administrator?)"
            return JunctionOutcome(
                False, target, source, "error",
                f"mklink failed: {(err or out).strip() or 'unknown error'}{note}",
            )
        return JunctionOutcome(
            True, target, source, "created",
            f"created junction: {target} -> {source}",
        )

    # POSIX fallback — symlink. Production users are on Windows; this
    # branch exists so the test suite can roundtrip junction creation
    # in CI without a Windows runner.
    try:
        os.symlink(source, target, target_is_directory=True)
    except OSError as e:
        return JunctionOutcome(
            False, target, source, "error",
            f"symlink failed: {e}",
        )
    return JunctionOutcome(
        True, target, source, "created",
        f"created symlink: {target} -> {source}",
    )
