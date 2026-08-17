"""Compose the environment the wizard's child processes should inherit.

Why this module exists: on Linux, Archipelago commonly ships as an
**AppImage**. The AppImage runtime mounts itself at
``/tmp/.mount_<name><rand>/`` and its ``AppRun`` exports loader variables
so the bundled interpreter finds the bundled libraries::

    LD_LIBRARY_PATH=/tmp/.mount_archipMfEgoA/opt/Archipelago/lib
    PYTHONHOME=/tmp/.mount_archipMfEgoA/opt/Archipelago

Those variables are inherited by everything the wizard spawns. A *host*
binary (``/usr/bin/cmake``, ``/usr/bin/python3``, ``git``) then resolves
its shared libraries out of the AppImage bundle instead of ``/usr/lib``,
and dies the moment the bundled copy is older than what the host binary's
own dependencies require::

    cmake: /tmp/.mount_archipMfEgoA/opt/Archipelago/lib/libssl.so.3:
        version `OPENSSL_3.2.0' not found (required by /usr/lib/libcurl.so.4)

That is not an "OpenSSL is too old" problem — the host's OpenSSL is fine;
it is being *shadowed* by the bundle's. It broke the wizard two ways:
the `cmake` prereq probe exited non-zero and was reported as "not found
on PATH", and the build phase's `cmake` configure died on the same
linker error.

:func:`child_environ` strips the AppImage-scoped values so host children
run against the host's own libraries. Children that live *inside* the
AppImage (the bundled Python, for one) still need the bundle's loader
paths, so pass the command being spawned and they keep the unmodified
environment.

Everything here is a no-op unless we are actually running inside an
AppImage, and nothing mutates ``os.environ`` — the wizard's own process
keeps whatever the launcher gave it.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Path-list variables: split on os.pathsep (and whitespace, for
# LD_PRELOAD's alternate separator), drop the components that live inside
# the AppImage, keep the rest.
_SCOPED_LIST_VARS: tuple[str, ...] = (
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "LD_AUDIT",
    "PYTHONPATH",
    "GI_TYPELIB_PATH",
    "GTK_PATH",
    "QT_PLUGIN_PATH",
    "GSETTINGS_SCHEMA_DIR",
)

# Single-value variables: unset entirely when the value points into the
# AppImage. PYTHONHOME is the dangerous one — a host interpreter that
# inherits it tries to load the *bundle's* stdlib and fails to start.
_SCOPED_SINGLE_VARS: tuple[str, ...] = (
    "PYTHONHOME",
    "PYTHONEXECUTABLE",
    "GDK_PIXBUF_MODULE_FILE",
    "TCL_LIBRARY",
    "TK_LIBRARY",
)

# The AppImage runtime's mount-dir naming: `<tmpdir>/.mount_<name><6 rand>`.
# Matching on the leading `.mount_` segment rather than a hardcoded /tmp
# keeps this working when $TMPDIR points elsewhere.
_MOUNT_SEGMENT = re.compile(r"^\.mount_.+")


def _appimage_roots() -> list[str]:
    """Directories that belong to the running AppImage bundle.

    ``APPDIR`` is the documented one. We also accept a mount dir derived
    from ``sys.prefix`` / ``sys.executable`` because some AppRun scripts
    re-export or clear APPDIR before handing control to the payload.
    """
    roots: list[str] = []

    def add(value: str | None) -> None:
        if not value:
            return
        v = value.rstrip("/")
        if v and v not in roots:
            roots.append(v)

    add(os.environ.get("APPDIR"))
    for probe in (sys.prefix, sys.executable, os.environ.get("APPIMAGE")):
        mount = _mount_root(probe)
        add(mount)
    return roots


def _mount_root(path: str | None) -> str | None:
    """Return the ``…/.mount_xxxx`` prefix of ``path``, or None."""
    if not path:
        return None
    parts = Path(path).parts
    for i, part in enumerate(parts):
        if _MOUNT_SEGMENT.match(part):
            return str(Path(*parts[: i + 1])).rstrip("/")
    return None


def running_under_appimage() -> bool:
    """True when this process was launched from an AppImage bundle."""
    if sys.platform == "win32":
        return False
    if os.environ.get("APPIMAGE") or os.environ.get("APPDIR"):
        return True
    return _mount_root(sys.prefix) is not None or \
        _mount_root(sys.executable) is not None


def _under_appimage(path: str, roots: list[str]) -> bool:
    """True when ``path`` lives inside one of the bundle ``roots``."""
    if not path:
        return False
    p = path.rstrip("/")
    for root in roots:
        if p == root or p.startswith(root + "/"):
            return True
    # Belt-and-braces: a mount dir we failed to enumerate above still
    # carries the runtime's `.mount_` segment.
    return _mount_root(p) is not None


def _split_list(value: str) -> list[str]:
    """Split a loader path-list. LD_PRELOAD allows space separators, the
    rest use os.pathsep; splitting on both is safe for all of them."""
    return [c for c in re.split(rf"[{re.escape(os.pathsep)}\s]+", value) if c]


def child_environ(
    command: str | os.PathLike[str] | None = None,
    *,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return the env dict to hand a child process.

    ``command`` is the executable about to be spawned. When it resolves
    to something inside the AppImage bundle the environment is returned
    untouched — the bundled binary needs the bundle's loader paths. For
    host binaries (and when ``command`` is None) the AppImage-scoped
    loader variables are stripped.

    Outside an AppImage this is just ``dict(os.environ)``.
    """
    env = dict(base) if base is not None else os.environ.copy()
    if not running_under_appimage():
        return env

    roots = _appimage_roots()
    if not roots:
        return env
    if command is not None:
        resolved = _resolve_command(str(command), env)
        if resolved and _under_appimage(resolved, roots):
            return env

    for var in _SCOPED_LIST_VARS:
        value = env.get(var)
        if not value:
            continue
        kept = [c for c in _split_list(value) if not _under_appimage(c, roots)]
        if kept:
            env[var] = os.pathsep.join(kept)
        else:
            # An *empty* LD_LIBRARY_PATH is not the same as an unset one
            # (the loader has historically read it as "current dir"), so
            # drop the variable rather than blanking it.
            env.pop(var, None)

    for var in _SCOPED_SINGLE_VARS:
        value = env.get(var)
        if value and _under_appimage(value, roots):
            env.pop(var, None)

    return env


def _resolve_command(command: str, env: dict[str, str]) -> str | None:
    """Best-effort absolute path for ``command`` (bare names via PATH)."""
    if os.path.sep in command or (os.altsep and os.altsep in command):
        try:
            return str(Path(command).resolve())
        except OSError:
            return command
    import shutil
    return shutil.which(command, path=env.get("PATH"))


def appimage_shadowing_hint(text: str) -> str:
    """Return a one-line diagnosis when ``text`` looks like an AppImage
    library-shadowing failure, else "".

    Used by the prereq detectors so a tool that *is* installed but can't
    execute under the inherited environment reports the real cause
    instead of "not found on PATH".
    """
    if not text:
        return ""
    lowered = text.lower()
    looks_like_loader_error = (
        "version `" in text or "version '" in text
        or "cannot open shared object file" in lowered
        or "symbol lookup error" in lowered
        or "glibc_" in lowered
    )
    if not (looks_like_loader_error and ".mount_" in text):
        return ""
    return (
        "the tool is installed but failed to start because Archipelago's "
        "AppImage bundle was shadowing the system libraries it needs "
        "(LD_LIBRARY_PATH pointed into the AppImage mount)"
    )
