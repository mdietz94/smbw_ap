r"""Create the apworld dev-junction so Archipelago Launcher autodiscovers us.

Library used by both the `/setup` wizard's Junction phase and the
standalone `scripts/install_smbw_apworld.py` entry point. The junction
links ``<primary>/vendor/Archipelago/custom_worlds/smbw_archipelago``
→ ``<current-worktree>/apworld/smbw_archipelago``, so AP's apworld
autodiscovery picks up the dev source tree without needing to package
an ``.apworld`` zip.

Worktree asymmetry (intentional):

  * The *target* (location of the junction) lives in the **primary**
    clone's ``vendor/Archipelago/custom_worlds/``.  Submodules don't
    get checked out into ``git worktree`` checkouts, so the worktree's
    own ``vendor/Archipelago/`` is empty and AP Launcher reads from
    the primary clone regardless.  Computing the target from the
    primary worktree (see :func:`prereqs.primary_worktree_root`) puts
    the junction where it'll actually be discovered.

  * The *source* (where the junction resolves to) is the **current**
    worktree's apworld.  Re-running ``install_smbw_apworld.py`` from a
    feature-branch worktree repoints the junction at that worktree, so
    iterating on a branch reflects in the live AP client without
    having to merge first.

Supersedes the older ``scripts/install_smbw_apworld.ps1`` — the PS1
called ``cmd /c mklink`` which resolved to whatever ``cmd`` happened to
be first on PATH (msys2's shell-shim, if the user had an msys2 install
with its ``usr/bin`` on PATH), failing with "Cannot run a document in
the middle of a pipeline".  ``subprocess.run(['cmd', '/c', ...])`` from
Python goes through the Windows app-paths resolver and always lands on
the real ``cmd.exe``.

Idempotent: re-running over a junction that already points at the
desired source is a no-op.  A junction pointing somewhere else (e.g. a
stale link into a removed worktree) is replaced.  Refuses to overwrite
a *real* directory (not a reparse point) since that might be a packaged
install the user doesn't want clobbered.

Windows-only in practice (uses ``mklink /J``). On POSIX we fall back to
a symlink so tests can roundtrip without a Windows VM, even though the
wizard itself is Windows-only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .prereqs import primary_worktree_root, repo_root

# Suppress per-child console window on Windows; no-op elsewhere.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class JunctionOutcome:
    ok: bool
    target: Path
    source: Path
    action: str    # "created" | "already_exists" | "replaced" | "error"
    message: str = ""


def apworld_source(repo: Path | None = None) -> Path:
    """Path the junction resolves to: the *current* worktree's apworld."""
    repo = repo if repo is not None else repo_root()
    return repo / "apworld" / "smbw_archipelago"


def junction_target(vendor_repo: Path | None = None) -> Path:
    """Path the junction is created at: in the **primary** worktree's
    ``vendor/Archipelago/custom_worlds/``.  Defaults to
    :func:`prereqs.primary_worktree_root` so the junction outlives
    transient worktrees and is always discoverable by AP Launcher."""
    repo = vendor_repo if vendor_repo is not None else primary_worktree_root()
    return repo / "vendor" / "Archipelago" / "custom_worlds" / "smbw_archipelago"


def _paths_equivalent(a: Path, b: Path) -> bool:
    """Case-insensitive normalized path comparison that doesn't require
    either side to currently exist (so we can compare against a
    dangling junction's recorded destination)."""
    na = os.path.normcase(os.path.normpath(str(a)))
    nb = os.path.normcase(os.path.normpath(str(b)))
    return na == nb


def _existing_link_target(path: Path) -> Path | None:
    """Resolved destination of the reparse point at ``path``, or None if
    ``path`` isn't a link.  Uses :func:`os.path.realpath` so a dangling
    junction still reports its (now-deleted) target -- which is the
    exact case we need to detect to repoint stale links."""
    try:
        rp = os.path.realpath(str(path))
    except OSError:
        return None
    resolved = Path(rp)
    if _paths_equivalent(resolved, path):
        # realpath returned the input unchanged: not a link.
        return None
    return resolved


def _remove_link(path: Path) -> None:
    """Drop a junction / symlink at ``path`` without touching its target.

    On Windows, junctions look like directories to most syscalls --
    pathlib's ``Path.unlink`` routes to ``DeleteFile`` which fails on
    them, so we use ``os.rmdir`` (drops just the reparse point, not the
    target).  Python-created symlinks-to-dirs (rarely used here) come
    off via ``os.unlink``; try that as a fallback.
    """
    if sys.platform == "win32":
        try:
            os.rmdir(str(path))
            return
        except OSError:
            os.unlink(str(path))
            return
    os.unlink(str(path))


def is_reparse_point(path: Path) -> bool:
    """True if ``path`` is a Windows reparse point (junction or symlink)
    or a POSIX symlink.  False if missing or a real directory.

    Uses :func:`os.path.lexists` (not ``path.exists()``) so a *dangling*
    junction -- one whose target was deleted -- is still recognized.
    ``Path.exists()`` follows links and returns False when the target is
    gone, which silently broke the stale-link replacement path before
    2026-05-26.
    """
    if not os.path.lexists(str(path)):
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
    vendor_repo: Path | None = None,
    runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
) -> JunctionOutcome:
    """Create the custom_worlds junction (or report what's already there).

    Args:
        repo: where the junction *resolves* to (the apworld source).
            Defaults to the current-worktree :func:`repo_root`.  Pass
            an explicit path in tests that build a fake repo tree.
        vendor_repo: where the junction is *created* (inside this
            repo's ``vendor/Archipelago/custom_worlds/``).  Defaults to
            :func:`prereqs.primary_worktree_root` so the junction lands
            in the primary clone even when invoked from a worktree.
            Tests usually pass ``vendor_repo=repo`` to keep both ends
            inside ``tmp_path``.
        runner: shell-out shim for the Windows ``mklink`` invocation
            (tests inject this to avoid touching the real filesystem).

    Returns a `JunctionOutcome` whose ``action`` field discriminates
    the cases the caller cares about: ``created``, ``already_exists``,
    ``replaced`` (a stale or wrong-target link was rewritten), or
    ``error``.
    """
    repo = repo if repo is not None else repo_root()
    source = apworld_source(repo)
    target = junction_target(vendor_repo)

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

    # `target.exists()` returns False for a dangling reparse point (the
    # link still occupies the directory entry but its destination is
    # gone), so probe `is_reparse_point` independently.
    replaced_existing = False
    if target.exists() or target.is_symlink() or is_reparse_point(target):
        if is_reparse_point(target):
            existing = _existing_link_target(target)
            if existing is not None and _paths_equivalent(existing, source):
                return JunctionOutcome(
                    True, target, source, "already_exists",
                    f"junction already exists at {target} -- leaving as-is",
                )
            # Stale (dangling) or wrong-target link.  Two common causes:
            # (a) a previous install ran from a `git worktree` that
            # has since been removed, leaving the link pointing into
            # nothing; (b) the user switched worktrees and wants the
            # junction repointed at the current one.  Replace either
            # way -- the link itself comes off without touching the
            # (possibly already-deleted) target it points at.
            try:
                _remove_link(target)
            except OSError as e:
                stale_target = str(existing) if existing else "<unknown>"
                return JunctionOutcome(
                    False, target, source, "error",
                    f"could not remove stale junction at {target} "
                    f"(was -> {stale_target}): {e}",
                )
            replaced_existing = True
        else:
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
        if replaced_existing:
            return JunctionOutcome(
                True, target, source, "replaced",
                f"replaced stale junction at {target} -> {source}",
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
    if replaced_existing:
        return JunctionOutcome(
            True, target, source, "replaced",
            f"replaced stale symlink at {target} -> {source}",
        )
    return JunctionOutcome(
        True, target, source, "created",
        f"created symlink: {target} -> {source}",
    )
