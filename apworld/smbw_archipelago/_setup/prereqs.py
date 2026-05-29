"""Detect the tools the SMBW setup wizard needs.

The wizard runs these detectors on the Prereq-check page. Each detector
returns a `PrereqResult` with an `ok` flag, a human-readable status detail
(e.g. "cmake 3.30.5" on success, or "not found on PATH" on failure), and
an `install_url` the wizard surfaces as a clickable link when `ok=False`.

Detectors are intentionally pure-Python and stdlib-only so they import on
any Python 3.10+ — no Kivy, no third-party deps. The wizard module is the
only thing that pulls in Kivy.

For unit-testability every shell-out goes through `_run`, which is a thin
wrapper around `subprocess.run`. Tests monkeypatch `_run` to return scripted
results without touching the user's machine. Filesystem checks use
`pathlib.Path` directly because mocking `Path.exists` per-test is cleaner
than abstracting a filesystem facade.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import local_appdata_root

# Suppress the per-child console window when running under the Launcher's
# windowed PyInstaller (no parent console → Windows opens a fresh console
# for each CONSOLE-subsystem child, which steals focus from the wizard).
# No-op on non-Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Hard min for cmake — matches the floor declared by `cmake_minimum_required`
# across the build. The outer switch-mod/CMakeLists.txt and all LibHakkun
# addon CMakeLists.txt sit at 3.16; nothing in the tree uses CMP0135 or
# FetchContent that would push the bar higher. Kitware's Windows CMake is
# required; msys2's cmake mangles drive letters and is rejected below.
MIN_CMAKE = (3, 16)

# AP's CommonClient + kvui require Python 3.10+. We aim higher for headroom
# and to match the AP launcher's bundled interpreter.
MIN_PYTHON = (3, 11)

# Sample imports that prove the Archipelago Python deps are usable. If any
# of these fail, vendor/Archipelago/requirements.txt is not satisfied and
# the wizard's pip-install step is required.
_AP_SAMPLE_IMPORTS = ("websockets", "bsdiff4", "certifi", "kvui")

# LibHakkun is ABI-pinned to LLVM 19. The libc++ headers the toolchain
# downloads via setup_libcxx_prepackaged.py only link against 19.x; 20+
# fails to link and pre-19.1 lacks the C++23 features the cross-compile
# uses. The wizard rejects both directions.
LLVM_MAJOR = 19

# Install pages surfaced by the wizard's "Install..." fallback link when
# auto-install isn't available. The git / python311 entries swap to
# OS-neutral landing pages on non-Windows; the rest are OS-agnostic.
_INSTALL_URLS_WINDOWS = {
    "git": "https://git-scm.com/download/win",
    "python311": "https://www.python.org/downloads/release/python-3119/#files",
}
_INSTALL_URLS_LINUX = {
    "git": "https://git-scm.com/download/linux",
    "python311": "https://www.python.org/downloads/",
}
INSTALL_URLS = {
    "dev_mode": "ms-settings:developers",
    "git": "https://git-scm.com/download/win",
    "cmake": "https://cmake.org/download/",
    "ninja": "https://github.com/ninja-build/ninja/releases",
    "python311": "https://www.python.org/downloads/release/python-3119/#files",
    "llvm19": "https://github.com/llvm/llvm-project/releases/tag/llvmorg-19.1.7",
    "archipelago_submodule": "",
    "switch_mod_submodule": "",
    "archipelago_deps": "",
    "ryujinx": "https://github.com/Ryubing/Ryujinx/releases",
    "lz4": "",
    "pyelftools": "",
}
if sys.platform != "win32":
    INSTALL_URLS.update(_INSTALL_URLS_LINUX)


def _is_windows() -> bool:
    """Single source of truth for the platform branch.

    Windows is the primary target — winget auto-install, the LLVM-MSVC
    tarball download, `mklink /J` junctions, and the `python3.exe`
    shim all apply only here. Non-Windows (Linux/macOS) takes the
    detect-only path: prereq probes still work (everything lives on
    PATH), but auto-install for system tools is disabled because we
    won't try to dispatch apt/pacman/brew/etc.
    """
    return sys.platform == "win32"


@dataclass
class PrereqResult:
    """Outcome of a single detector.

    `key` is the stable identifier the wizard uses to map back into
    `INSTALL_URLS` and to render the right label. `detail` is the
    human-readable extra (version string, error message). `install_url`
    is non-empty when `ok=False` so the wizard can surface a clickable
    link.

    `auto_installable` opts the row into the wizard's auto-install path.
    The wizard's installer registry (`_setup.installers`) maps `key` →
    install function; setting True here tells the wizard to render an
    "Auto-install" button instead of just the "Install page..." link.

    `warn_only` rows (e.g. Ryujinx) don't block the pipeline. They surface
    in the GUI as yellow rather than red and don't propagate `ok=False`
    into `all_ok()`.
    """
    key: str
    name: str
    ok: bool
    detail: str
    install_url: str = ""
    note: str = ""
    auto_installable: bool = False
    warn_only: bool = False


def repo_root() -> Path:
    """Resolve the SMBW Archipelago repo root from this module's location.

    Real on-disk layout: ``<repo>/apworld/smbw_archipelago/_setup/prereqs.py``.
    Walking up three parents lands on the repo root regardless of whether
    Python loaded the module through the dev-mode junction at
    ``vendor/Archipelago/custom_worlds/smbw_archipelago/`` (we resolve()
    so the junction is followed back to the real path).

    In a ``git worktree`` checkout this returns the *worktree's* root.
    Use :func:`primary_worktree_root` when you need the clone that owns
    submodule checkouts (notably ``vendor/Archipelago/``, which is
    where the AP Launcher's custom_worlds junction must live).
    """
    return Path(__file__).resolve().parents[3]


def is_dev_clone(repo: Path | None = None) -> bool:
    """True if ``repo`` (default: :func:`repo_root`) is a git checkout
    (regular clone, submodule, or worktree).

    Used to gate phases that only make sense in a dev tree: ``git
    submodule update`` and the ``mklink /J`` junction.  A user who
    installed the .apworld into a stock Archipelago via pip / custom
    worlds extraction has no ``.git`` here and shouldn't get a confusing
    "fatal: not a git repository" when the wizard tries to fetch a
    submodule that's already satisfied by the surrounding AP install.

    ``.git`` is a directory in a normal clone, a file (with
    ``gitdir: ...``) in a worktree, and absent in a release install.
    Either form counts as "dev clone" for our purposes.
    """
    root = repo if repo is not None else repo_root()
    return (root / ".git").exists()


def archipelago_importable() -> bool:
    """True if ``CommonClient`` is importable from the current Python.

    Used as a "we don't need vendor/Archipelago to be initialized
    because AP is already on sys.path" short-circuit.  Uses
    :func:`importlib.util.find_spec` so we don't actually load the
    module (avoids the side effects of pulling in AP's heavy import
    graph for what should be a cheap probe).
    """
    import importlib.util
    try:
        return importlib.util.find_spec("CommonClient") is not None
    except (ImportError, ValueError):
        return False


def primary_worktree_root(start: Path | None = None) -> Path:
    """Resolve the *primary* worktree's repo root.

    Submodules (``vendor/Archipelago/`` and ``switch-mod/lib/*``) only get
    checked out into the primary clone, not into auxiliary ``git
    worktree`` checkouts.  The apworld junction therefore must be
    created in the primary's ``vendor/Archipelago/custom_worlds/``
    even when the installer is invoked from a worktree -- otherwise
    the junction lands in an empty worktree-side vendor dir that AP
    Launcher never reads.

    The junction's *target* (the apworld source) is a separate
    decision; we deliberately keep that pointing at the *current*
    worktree so dev iteration on a feature branch reflects immediately.

    Strategy:
      1. Ask git: ``rev-parse --path-format=absolute --git-common-dir``.
         Git's common-dir lives inside the primary worktree's ``.git``
         (literally the ``.git/`` directory), no matter which worktree
         the command runs from.  Its parent is the primary repo root.
      2. Fall back to ``start`` when git isn't available or the cwd
         isn't a git checkout (e.g. unit tests building a fake repo
         under ``tmp_path``).  Preserves the pre-worktree behavior.

    Returns the same path as :func:`repo_root` on a vanilla clone.
    """
    start = start if start is not None else repo_root()
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(start),
            capture_output=True,
            text=True,
            timeout=5.0,
            creationflags=_NO_WINDOW,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return start
    if res.returncode != 0:
        return start
    common = Path((res.stdout or "").strip())
    if not common.is_absolute() or not common.is_dir():
        return start
    # The common-dir is the primary worktree's `.git/` (named exactly
    # `.git`, even when the worktree we ran from has its own `.git`
    # file pointing here).  Parent of `.git` is the primary working
    # tree root.
    if common.name == ".git":
        return common.parent
    return start


def _run(cmd: list[str], *, timeout: float = 10.0) -> tuple[int, str, str]:
    """Subprocess wrapper that returns (returncode, stdout, stderr).

    Centralized so tests can monkeypatch one function instead of mocking
    `subprocess.run` per-detector. Non-zero exit codes are NOT exceptions
    — they're the normal "tool not found" signal.

    Raises `FileNotFoundError` only when the executable name itself can't
    be resolved (i.e. not on PATH); detectors catch this and treat it as
    "not installed".
    """
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=_NO_WINDOW,
    )
    return res.returncode, res.stdout or "", res.stderr or ""


def _safe_run(cmd: list[str]) -> tuple[int, str, str] | None:
    """`_run` that returns None instead of raising on FileNotFoundError /
    OSError. Use when a detector wants to treat 'executable missing' the
    same as 'executable exists but exited non-zero'."""
    try:
        return _run(cmd)
    except (FileNotFoundError, OSError):
        return None
    except subprocess.TimeoutExpired:
        return (1, "", "timeout")


def _prepend_path(dir_path: Path) -> None:
    """Prepend `dir_path` to `os.environ["PATH"]` for the current process.

    Used after a detector resolves a tool via a deterministic path that
    isn't on PATH yet — downstream subprocess invocations need to find
    the tool by bare name. Mutation is process-local; nothing persists to
    the user's environment.
    """
    cur = os.environ.get("PATH", "")
    s = str(dir_path)
    if s in cur.split(os.pathsep):
        return
    os.environ["PATH"] = s + os.pathsep + cur if cur else s


# ---------------------------------------------------------------------------
# Windows Developer Mode (or admin) — required for `mklink /J` in the
# junction phase, which is the only way to dev-install the apworld into
# vendor/Archipelago/custom_worlds without copying files.
# ---------------------------------------------------------------------------

def _is_admin() -> bool:
    """Best-effort admin probe. False on non-Windows."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _is_dev_mode() -> bool:
    """Read the AppModelUnlock registry key that gates `mklink /J` from a
    non-admin shell. False on non-Windows or if the key is missing."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock",
        )
        try:
            val, _ = winreg.QueryValueEx(key, "AllowDevelopmentWithoutDevLicense")
        finally:
            winreg.CloseKey(key)
        return int(val) == 1
    except (FileNotFoundError, OSError, ValueError):
        return False


def check_dev_mode() -> PrereqResult:
    """Windows Developer Mode (or running elevated).

    Non-Windows shortcut: returns ok=True since the junction step is a no-op
    on POSIX (we just symlink). The wizard is Windows-only in practice; this
    branch exists for cross-platform tests.
    """
    if sys.platform != "win32":
        return PrereqResult("dev_mode", "Windows Developer Mode", True,
                            "not applicable (non-Windows)")
    if _is_admin():
        return PrereqResult("dev_mode", "Windows Developer Mode", True,
                            "running elevated (admin)")
    if _is_dev_mode():
        return PrereqResult("dev_mode", "Windows Developer Mode", True,
                            "AllowDevelopmentWithoutDevLicense=1")
    return PrereqResult(
        "dev_mode", "Windows Developer Mode", False,
        "Dev Mode is OFF (required for mklink /J junctions)",
        INSTALL_URLS["dev_mode"],
        note=(
            "The junction step needs mklink /J, which on Windows 10+ "
            "requires either Developer Mode or admin privileges. "
            "Enable Settings → System → For developers → Developer Mode, "
            "then click Re-check. Or run the Archipelago Launcher as "
            "administrator (less recommended)."
        ),
    )


# ---------------------------------------------------------------------------
# Git — needed for `git submodule update`. Probed before submodule rows so
# the user sees the missing tool, not just a confusing "submodule missing".
# ---------------------------------------------------------------------------

def check_git() -> PrereqResult:
    r = _safe_run(["git", "--version"])
    if r is None or r[0] != 0:
        if _is_windows():
            note = (
                "Easiest install on Windows:\n"
                "    winget install Git.Git\n"
                "Or click Auto-install. PATH changes don't reach a running "
                "process, so the wizard re-probes Git's winget install dir "
                "on Re-check."
            )
        else:
            note = (
                "Install git via your distro's package manager:\n"
                "    apt install git    (Debian/Ubuntu)\n"
                "    dnf install git    (Fedora/RHEL)\n"
                "    pacman -S git      (Arch)\n"
                "Then click Re-check."
            )
        return PrereqResult(
            "git", "Git", False, "not found on PATH",
            INSTALL_URLS["git"],
            note=note,
            auto_installable=_is_windows(),
        )
    ver = (r[1] or r[2]).strip()
    return PrereqResult("git", "Git", True, ver, auto_installable=_is_windows())


# ---------------------------------------------------------------------------
# CMake — Kitware's Windows build. msys2's cmake (from devkitPro) is
# rejected because it mangles drive-letter paths and breaks the switch-mod
# configure. See CLAUDE.md "critical gotchas".
# ---------------------------------------------------------------------------

_CMAKE_DEFAULT_PATHS = (
    Path("C:/Program Files/CMake/bin/cmake.exe"),
    Path("C:/Program Files (x86)/CMake/bin/cmake.exe"),
)

# Path fragments that identify a cmake binary as the msys2 build (typically
# the one devkitPro's installer ships). Rejected during PATH fallback
# because they mangle Windows-absolute paths even when version-good.
_MSYS2_CMAKE_MARKERS = ("msys", "cygwin", "mingw")

_resolved_cmake: str | None = None


def resolved_cmake() -> str:
    """Return the cmake binary path resolved by the most recent
    `check_cmake` call, or the bare name "cmake" if detection hasn't run.
    """
    return _resolved_cmake if _resolved_cmake is not None else "cmake"


def _parse_cmake_version(text: str) -> tuple[int, int, int] | None:
    m = re.search(r"cmake version (\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or "0"))


def _is_msys2_cmake(resolved_path: str | None) -> bool:
    if not resolved_path:
        return False
    lowered = resolved_path.lower().replace("\\", "/")
    return any(marker in lowered for marker in _MSYS2_CMAKE_MARKERS)


def check_cmake() -> PrereqResult:
    """Probe Windows-native CMake first, then fall back to PATH.

    Side effect: writes the resolved binary path to module-level
    `_resolved_cmake` so the build phase invokes the same cmake the
    prereq check passed — without this, the build step could pick up
    msys2's cmake from PATH and blow up with drive-letter errors.
    """
    global _resolved_cmake

    candidates: list[str] = []
    # `C:/Program Files/...` probes only apply on Windows.
    if _is_windows():
        for default in _CMAKE_DEFAULT_PATHS:
            if default.exists():
                candidates.append(str(default))
    candidates.append("cmake")

    saw_msys2_path_fallback = False
    too_old_version: tuple[int, int, int] | None = None
    too_old_path: str | None = None
    saw_unparseable = False
    for cand in candidates:
        r = _safe_run([cand, "--version"])
        if r is None or r[0] != 0:
            continue
        ver = _parse_cmake_version(r[1] or r[2])
        if ver is None:
            saw_unparseable = True
            continue
        if (ver[0], ver[1]) < MIN_CMAKE:
            if too_old_version is None or ver > too_old_version:
                too_old_version = ver
                too_old_path = shutil.which(cand) or cand
            continue
        # msys2's cmake mangles drive-letter paths — Windows-only concern.
        if _is_windows() and cand == "cmake":
            resolved_path = shutil.which(cand)
            if _is_msys2_cmake(resolved_path):
                saw_msys2_path_fallback = True
                continue
        _resolved_cmake = cand
        return PrereqResult(
            "cmake", f"CMake {MIN_CMAKE[0]}.{MIN_CMAKE[1]}+", True,
            f"{ver[0]}.{ver[1]}.{ver[2]} ({cand})",
            auto_installable=_is_windows(),
        )

    if saw_msys2_path_fallback:
        return PrereqResult(
            "cmake", f"CMake {MIN_CMAKE[0]}.{MIN_CMAKE[1]}+", False,
            "only msys2's cmake is on PATH (from devkitPro) — install "
            "Kitware's Windows CMake instead",
            INSTALL_URLS["cmake"],
            note=(
                "msys2's cmake mangles Windows drive-letter paths "
                "(`C:\\…` → `/cwd/C:/…`) and breaks the switch-mod build. "
                "Easiest install on Windows:\n"
                "    winget install Kitware.CMake\n"
                "Or click Auto-install. The wizard probes the MSI's "
                "canonical install location directly on Re-check, so no "
                "shell restart is needed after winget finishes."
            ),
            auto_installable=True,
        )
    if _is_windows():
        note = (
            "Easiest install on Windows:\n"
            "    winget install Kitware.CMake\n"
            "Or click Auto-install above."
        )
    else:
        note = (
            f"Install CMake {MIN_CMAKE[0]}.{MIN_CMAKE[1]}+ via your "
            "distro's package manager:\n"
            "    apt install cmake     (Debian/Ubuntu)\n"
            "    dnf install cmake     (Fedora/RHEL)\n"
            "    pacman -S cmake       (Arch)\n"
            "Then click Re-check."
        )
    if too_old_version is not None:
        v = ".".join(str(p) for p in too_old_version)
        where = f" ({too_old_path})" if too_old_path else ""
        detail = (
            f"CMake {v} found{where} but too old — "
            f"{MIN_CMAKE[0]}.{MIN_CMAKE[1]}+ required"
        )
    elif saw_unparseable:
        detail = "cmake found but `--version` output couldn't be parsed"
    else:
        detail = "not found on PATH"
    return PrereqResult(
        "cmake", f"CMake {MIN_CMAKE[0]}.{MIN_CMAKE[1]}+", False,
        detail,
        INSTALL_URLS["cmake"],
        note=note,
        auto_installable=_is_windows(),
    )


# ---------------------------------------------------------------------------
# Ninja — generator for cmake. Cheap to install via winget.
# ---------------------------------------------------------------------------

_resolved_ninja_bin: str | None = None


def resolved_ninja_bin() -> str | None:
    return _resolved_ninja_bin


def _winget_ninja_paths() -> list[Path]:
    """Probe winget's deterministic install location."""
    localapp = os.environ.get("LOCALAPPDATA")
    if not localapp:
        return []
    base = Path(localapp) / "Microsoft" / "WinGet" / "Packages"
    if not base.is_dir():
        return []
    return sorted(base.glob("Ninja-build.Ninja_*/ninja.exe"))


def check_ninja() -> PrereqResult:
    global _resolved_ninja_bin

    # The winget deterministic-install probe only applies on Windows.
    if _is_windows():
        for candidate in _winget_ninja_paths():
            r = _safe_run([str(candidate), "--version"])
            if r is None or r[0] != 0:
                continue
            _prepend_path(candidate.parent)
            _resolved_ninja_bin = str(candidate.parent)
            ver = (r[1] or r[2]).strip()
            return PrereqResult("ninja", "Ninja", True,
                                f"{ver} ({candidate})", auto_installable=True)
    r = _safe_run(["ninja", "--version"])
    if r is None or r[0] != 0:
        if _is_windows():
            note = (
                "Easiest install on Windows:\n"
                "    winget install Ninja-build.Ninja\n"
                "Or click Auto-install above. The wizard probes winget's "
                "install dir directly on Re-check, so no shell restart "
                "needed."
            )
        else:
            note = (
                "Install Ninja via your distro's package manager:\n"
                "    apt install ninja-build   (Debian/Ubuntu)\n"
                "    dnf install ninja-build   (Fedora/RHEL)\n"
                "    pacman -S ninja           (Arch)\n"
                "Then click Re-check."
            )
        return PrereqResult(
            "ninja", "Ninja", False, "not found on PATH",
            INSTALL_URLS["ninja"],
            note=note,
            auto_installable=_is_windows(),
        )
    resolved = shutil.which("ninja")
    if resolved:
        _resolved_ninja_bin = str(Path(resolved).parent)
    ver = (r[1] or r[2]).strip()
    return PrereqResult("ninja", "Ninja", True, ver, auto_installable=_is_windows())


# ---------------------------------------------------------------------------
# Python 3.11+ — required by Archipelago. The wizard itself runs under
# whatever Python AP launched, but the build / pip steps need a known-good
# 3.11+.
# ---------------------------------------------------------------------------

_resolved_python_bin: str | None = None


def resolved_python_bin() -> str | None:
    return _resolved_python_bin


def _parse_python_version(text: str) -> tuple[int, int, int] | None:
    m = re.search(r"Python (\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or "0"))


def check_python311() -> PrereqResult:
    """Python 3.11+ availability.

    Probe order:
      1. The currently-running interpreter (`sys.executable`) — if the
         wizard was launched under 3.11+, we already have what we need.
      2. `py -3.11` / `py -3.12` / `py -3.13` (Windows Python launcher).
      3. Plain `python3.11` / `python3` / `python`.

    Side effect: caches the resolved interpreter path so installers.py
    can `pip install` into the SAME interpreter without re-probing.
    """
    global _resolved_python_bin

    candidates: list[list[str]] = []
    cur = sys.executable
    if cur:
        candidates.append([cur, "--version"])
    # `py -3.x` is the Windows Python launcher; doesn't exist on Linux.
    if _is_windows():
        candidates += [
            ["py", "-3.11", "--version"],
            ["py", "-3.12", "--version"],
            ["py", "-3.13", "--version"],
        ]
    candidates += [
        ["python3.11", "--version"],
        ["python3", "--version"],
        ["python", "--version"],
    ]
    for cmd in candidates:
        r = _safe_run(cmd)
        if r is None or r[0] != 0:
            continue
        ver = _parse_python_version(r[1] or r[2])
        if ver is None:
            continue
        if (ver[0], ver[1]) < MIN_PYTHON:
            continue
        # Resolve the actual interpreter path (py launcher returns its own
        # path; we want the real interpreter so pip targets the right env).
        r2 = _safe_run([*cmd[:-1], "-c", "import sys; print(sys.executable)"])
        if r2 and r2[0] == 0:
            interp = (r2[1] or "").strip().splitlines()[0] if (r2[1] or "").strip() else cmd[0]
        else:
            interp = cmd[0]
        if Path(interp).is_file():
            _resolved_python_bin = interp
            _prepend_path(Path(interp).parent)
            # `python3.exe` shim is a Windows-only workaround — Linux
            # already has `python3` on PATH from any standard install.
            if _is_windows():
                ensure_python3_shim(Path(interp))
        return PrereqResult(
            "python311", f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+", True,
            f"{ver[0]}.{ver[1]}.{ver[2]} ({cmd[0]})",
            auto_installable=_is_windows(),
        )
    if _is_windows():
        note = (
            "Easiest install on Windows:\n"
            "    winget install Python.Python.3.11\n"
            "Or click Auto-install. Archipelago's CommonClient + kvui "
            f"need Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+."
        )
    else:
        note = (
            f"Install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ via your "
            "distro's package manager:\n"
            "    apt install python3.11    (Debian/Ubuntu)\n"
            "    dnf install python3.11    (Fedora/RHEL)\n"
            "    pacman -S python          (Arch — ships current 3.x)\n"
            "Then click Re-check."
        )
    return PrereqResult(
        "python311", f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+", False,
        f"no Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ found",
        INSTALL_URLS["python311"],
        note=note,
        auto_installable=_is_windows(),
    )


# ---------------------------------------------------------------------------
# LLVM 19.1.x — clang/clang++ cross-compiler for the Switch target.
#
# LibHakkun's [switch-mod/sys/cmake/toolchain.cmake] hardcodes the C/C++
# compilers to bare `clang` / `clang++`. The libc++ + musl + compiler-rt
# bundle that toolchain.cmake fetches on first configure is ABI-pinned to
# LLVM 19.x, so we reject anything else even when found on PATH.
# ---------------------------------------------------------------------------


def llvm_portable_root() -> Path:
    """Where the wizard's auto-installer extracts LLVM to. Outside %APPDATA%
    because LOCALAPPDATA is roaming-profile-excluded and a ~3.3 GB toolchain
    has no business following a domain user across machines."""
    return local_appdata_root() / "llvm"


# Canonical Windows install paths laid down by the official LLVM .exe
# installer. Probed BEFORE `clang` on PATH so a dev with LLVM installed
# in the standard place (but not on PATH) doesn't see a perma-red prereq
# row. Mirrors `_CMAKE_DEFAULT_PATHS`. Order matters: 64-bit first.
_LLVM_DEFAULT_PATHS = (
    Path("C:/Program Files/LLVM/bin/clang.exe"),
    Path("C:/Program Files (x86)/LLVM/bin/clang.exe"),
)


_resolved_llvm_bin: str | None = None


def resolved_llvm_bin() -> str | None:
    """Return the LLVM `bin/` dir the most recent `check_llvm19` resolved,
    or None if detection hasn't run or didn't find one. build.py prepends
    this to the cmake subprocess's PATH so the build uses the SAME clang
    the prereq check passed — not whatever's first on the inherited PATH."""
    return _resolved_llvm_bin


def _parse_clang_version(text: str) -> tuple[int, int, int] | None:
    """`clang --version` prints `clang version 19.1.7 ...` on portable
    builds and `(distro) clang version 19.1.7 ...` on Linux distros.
    Capture the dotted triple regardless of leading prose."""
    m = re.search(r"clang version (\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or "0"))


def _llvm_gate_version(ver: tuple[int, int, int]) -> str | None:
    """Return None if `ver` is acceptable LLVM 19.1.x, else a short reason
    string the caller surfaces. LibHakkun's libc++ won't link against 20+;
    pre-19.1 lacks the C++23 features the cross-compile relies on."""
    if ver[0] != LLVM_MAJOR:
        return (f"need LLVM {LLVM_MAJOR}.x (got {ver[0]}.{ver[1]}.{ver[2]}); "
                f"LibHakkun's libc++ ABI is pinned at 19")
    if ver[1] < 1:
        return (f"need LLVM 19.1.x (got 19.{ver[1]}.{ver[2]}); 19.0.x "
                f"predates the libc++ features the cross-compile uses")
    return None


def check_llvm19() -> PrereqResult:
    """LLVM 19.1.x cross-compiler.

    Probe order:
      1. Portable install at ``%LOCALAPPDATA%/SMBWArchipelago/llvm/bin/clang.exe``
         — the path the wizard's auto-installer writes to. Wins
         unconditionally so a user with a different LLVM on PATH still
         gets the version the wizard built against.
      2. Canonical Windows install at ``C:/Program Files/LLVM/bin/clang.exe``
         — where the official LLVM .exe installer drops it. Probed before
         the PATH fallback because devs who installed LLVM themselves
         often don't add it to PATH; without this probe their prereq row
         shows red even though the right toolchain is sitting right
         there. Only accepted if version-gated to 19.1.x.
      3. ``clang --version`` on PATH — accept only if 19.1.x. Lets a
         developer with LLVM 19 already on PATH skip the ~806 MB download.

    Side effect: caches the resolved `bin/` dir so build.py can prepend
    it to the cmake subprocess PATH.
    """
    global _resolved_llvm_bin

    # Portable install + canonical Windows paths only apply on Windows
    # (the auto-installer can only populate `llvm_portable_root` on
    # Windows, and `C:/Program Files/LLVM/` is the Windows MSI path).
    if _is_windows():
        portable_clang = llvm_portable_root() / "bin" / "clang.exe"
        if portable_clang.is_file():
            r = _safe_run([str(portable_clang), "--version"])
            if r and r[0] == 0:
                ver = _parse_clang_version(r[1] or r[2])
                if ver is not None:
                    reason = _llvm_gate_version(ver)
                    if reason is None:
                        _resolved_llvm_bin = str(portable_clang.parent)
                        return PrereqResult(
                            "llvm19", "LLVM 19.1.x", True,
                            f"{ver[0]}.{ver[1]}.{ver[2]} ({portable_clang}) [portable]",
                            auto_installable=True,
                        )
                    return PrereqResult(
                        "llvm19", "LLVM 19.1.x", False,
                        f"portable install at {portable_clang.parent} has wrong "
                        f"version: {reason}",
                        INSTALL_URLS["llvm19"],
                        note=(
                            "Delete %LOCALAPPDATA%\\SMBWArchipelago\\llvm\\ "
                            "and click Auto-install to re-fetch LLVM 19.1.7."
                        ),
                        auto_installable=True,
                    )

        # Canonical official-installer paths. We only accept a 19.1.x match
        # here — a wrong-version system install is NOT a hard failure (the
        # PATH fallback below or a portable install can still succeed).
        for default_clang in _LLVM_DEFAULT_PATHS:
            if not default_clang.is_file():
                continue
            r = _safe_run([str(default_clang), "--version"])
            if r is None or r[0] != 0:
                continue
            ver = _parse_clang_version(r[1] or r[2])
            if ver is None:
                continue
            if _llvm_gate_version(ver) is not None:
                continue
            _resolved_llvm_bin = str(default_clang.parent)
            return PrereqResult(
                "llvm19", "LLVM 19.1.x", True,
                f"{ver[0]}.{ver[1]}.{ver[2]} ({default_clang})",
                auto_installable=True,
            )

    r = _safe_run(["clang", "--version"])
    if r is None or r[0] != 0:
        if _is_windows():
            note = (
                "Click Auto-install to download LLVM 19.1.7 (~806 MB, "
                "~3.3 GB on disk) into "
                "%LOCALAPPDATA%\\SMBWArchipelago\\llvm\\. Your system "
                "LLVM (if any) stays untouched — the wizard scopes its "
                "PATH prepend to the build subprocess."
            )
        else:
            note = (
                "Install LLVM 19.1.x via your distro's package manager:\n"
                "    apt install clang-19 lld-19   (Debian/Ubuntu — see https://apt.llvm.org/)\n"
                "    dnf install clang             (Fedora — verify version is 19.1.x)\n"
                "    pacman -S clang lld           (Arch — current is 19.x)\n"
                "Or download the upstream Linux build from:\n"
                "    https://github.com/llvm/llvm-project/releases/tag/llvmorg-19.1.7\n"
                "LibHakkun's toolchain.cmake hardcodes `clang`/`clang++`; "
                "ensure they're on PATH and report 19.1.x."
            )
        return PrereqResult(
            "llvm19", "LLVM 19.1.x", False,
            "not found (LibHakkun's toolchain.cmake hardcodes clang/clang++)",
            INSTALL_URLS["llvm19"],
            note=note,
            auto_installable=_is_windows(),
        )
    ver = _parse_clang_version(r[1] or r[2])
    if ver is None:
        return PrereqResult(
            "llvm19", "LLVM 19.1.x", False,
            "found on PATH but couldn't parse `clang --version` output",
            INSTALL_URLS["llvm19"],
            auto_installable=_is_windows(),
        )
    reason = _llvm_gate_version(ver)
    if reason is not None:
        if _is_windows():
            note = (
                "Your system-installed LLVM stays untouched. Click "
                "Auto-install to add a parallel LLVM 19.1.7 under "
                "%LOCALAPPDATA%\\SMBWArchipelago\\llvm\\."
            )
        else:
            note = (
                "Install a 19.1.x build via your distro's package "
                "manager (apt/dnf/pacman) — e.g. `clang-19` on "
                "Debian/Ubuntu (https://apt.llvm.org/) — or download "
                "the upstream Linux 19.1.7 tarball, and put its bin/ "
                "dir ahead of your current clang on PATH."
            )
        return PrereqResult(
            "llvm19", "LLVM 19.1.x", False,
            f"PATH has LLVM {ver[0]}.{ver[1]}.{ver[2]} — {reason}",
            INSTALL_URLS["llvm19"],
            note=note,
            auto_installable=_is_windows(),
        )
    path_clang = shutil.which("clang")
    if path_clang:
        _resolved_llvm_bin = str(Path(path_clang).parent)
    return PrereqResult(
        "llvm19", "LLVM 19.1.x", True,
        f"{ver[0]}.{ver[1]}.{ver[2]} (PATH)",
        auto_installable=_is_windows(),
    )


def ensure_python3_shim(python_exe: Path) -> None:
    """Create a ``python3.exe`` copy of ``python_exe`` in the same dir.

    Standard Windows CPython installers ship ``python.exe`` + ``pythonw.exe``
    but NOT ``python3.exe``. LibHakkun's [sys/cmake/toolchain.cmake] calls
    bare ``python3 sys/tools/setup_libcxx_prepackaged.py`` when the
    prepackaged libc++ + musl + compiler-rt bundle is missing. With nothing
    called ``python3.exe`` on PATH, that resolves to the Microsoft Store
    stub which exits silently — cmake captures the result var but never
    checks it, and the build dies much later with cryptic missing-stdlib
    errors.

    Copy (not symlink) because Windows symlinks need admin or Dev Mode.
    Idempotent: skips if ``python3.exe`` already exists.

    No-op on non-Windows: Linux/macOS Python installs already provide
    `python3` on PATH from the package itself; there is no Store-stub
    to shadow it.
    """
    if not _is_windows():
        return
    if not python_exe.is_file():
        return
    shim = python_exe.with_name("python3.exe")
    if shim.is_file():
        return
    try:
        shutil.copy2(python_exe, shim)
    except OSError:
        # Best-effort. If the copy fails (read-only target dir on a
        # locked-down install), the user can still configure manually by
        # passing ``-DPython3_EXECUTABLE=<path>`` to cmake.
        pass


# ---------------------------------------------------------------------------
# Submodules — vendor/Archipelago + switch-mod/lib/{imgui,NintendoSDK,sead}
# ---------------------------------------------------------------------------

def check_archipelago_submodule() -> PrereqResult:
    """vendor/Archipelago submodule initialized.

    Probes for a sentinel file (`CommonClient.py`) at the expected path.
    A missing submodule is the typical first-run failure on a fresh clone.

    Short-circuit: if ``CommonClient`` is importable from the current
    Python AND we're not in a dev clone (so we won't try to wire a
    junction into ``vendor/Archipelago/custom_worlds/`` anyway), we
    treat AP as satisfied by whatever install loaded us.  This is the
    Launcher / pip-install / packaged-apworld case where the user has
    no use for a fresh submodule clone.
    """
    sentinel = repo_root() / "vendor" / "Archipelago" / "CommonClient.py"
    if sentinel.is_file():
        return PrereqResult(
            "archipelago_submodule", "vendor/Archipelago submodule", True,
            f"present ({sentinel.parent})",
            auto_installable=True,
        )
    if archipelago_importable() and not is_dev_clone():
        return PrereqResult(
            "archipelago_submodule", "vendor/Archipelago submodule", True,
            "satisfied by surrounding Archipelago install (no dev clone)",
            auto_installable=True,
        )
    return PrereqResult(
        "archipelago_submodule", "vendor/Archipelago submodule", False,
        f"missing {sentinel} (submodule not initialized?)",
        INSTALL_URLS["archipelago_submodule"],
        note=(
            "Run from the repo root:\n"
            "    git submodule update --init --recursive vendor/Archipelago\n"
            "Or click Auto-install -- the wizard runs the git command for you.\n"
            "If you installed this apworld into an existing Archipelago "
            "(not a dev clone), clone the source repo to rebuild the "
            "Switch subsdk."
        ),
        auto_installable=True,
    )


def check_switch_mod_submodule() -> PrereqResult:
    """switch-mod's hakkun framework + vendored libs initialized.

    Required submodules:
      * ``switch-mod/sys``       — LibHakkun (the subsdk framework + cmake
                                   driver). The build cannot configure
                                   without it.
      * ``switch-mod/lib/imgui`` — Dear ImGui. Currently unused in the
                                   Phase 1 build (debug overlay gated off
                                   in config/config.cmake) but the next
                                   addon flip-on will pull it in.

    Two acceptable shapes:
      * **Dev clone**: ``switch-mod/sys/cmake/module.cmake`` present via
        ``git submodule update --init switch-mod/sys switch-mod/lib/imgui``.
        (LibHakkun has no top-level ``CMakeLists.txt`` — the repo's main
        ``switch-mod/CMakeLists.txt`` does ``include(sys/cmake/module.cmake)``
        instead of ``add_subdirectory(sys)``, so ``module.cmake`` is the
        load-bearing sentinel.)
      * **Packaged install**: the same sources ship inside the .apworld
        zip under ``smbwonder/_setup/switch-mod/`` (added by
        ``scripts/install_apworld.py --bundle-mod`` at release time).
        Extracted lazily on first build via
        :func:`build.bundled_switch_mod` -- here we just check the zip
        is on disk so the prereq row doesn't claim "missing" on a
        perfectly buildable packaged install.
    """
    sys_sentinel = repo_root() / "switch-mod" / "sys" / "cmake" / "module.cmake"
    if sys_sentinel.is_file():
        return PrereqResult(
            "switch_mod_submodule", "switch-mod hakkun + libs", True,
            f"present ({sys_sentinel.parent.parent})",
            auto_installable=True,
        )

    # Packaged-install short-circuit: avoid importing build.py at
    # apworld-import time (its zipfile / shutil imports are heavier
    # than we need for a probe), so duplicate the lightweight detection
    # logic here.  The actual extraction is deferred to build phase.
    if not is_dev_clone():
        setup_root = Path(__file__).resolve().parent
        cur = setup_root
        for _ in range(10):
            if cur.suffix == ".apworld" and cur.is_file():
                return PrereqResult(
                    "switch_mod_submodule", "switch-mod hakkun + libs", True,
                    f"bundled inside {cur.name} (extracted on first build)",
                    auto_installable=True,
                )
            parent = cur.parent
            if parent == cur:
                break
            cur = parent
        return PrereqResult(
            "switch_mod_submodule", "switch-mod hakkun + libs", False,
            "no bundled sources and not a dev clone",
            INSTALL_URLS["switch_mod_submodule"],
            note=(
                "The apworld appears to be installed as loose files "
                "(not a .apworld zip) outside a dev clone, so the wizard "
                "can neither find git-checked-out submodules nor extract "
                "bundled sources.  Re-install the apworld from a release "
                "build, or clone the source repo to rebuild from scratch."
            ),
        )

    return PrereqResult(
        "switch_mod_submodule", "switch-mod hakkun + libs", False,
        f"missing {sys_sentinel} (LibHakkun submodule under switch-mod/sys/ not initialized)",
        INSTALL_URLS["switch_mod_submodule"],
        note=(
            "Run from the repo root:\n"
            "    git submodule update --init switch-mod/sys switch-mod/lib/imgui\n"
            "Or click Auto-install."
        ),
        auto_installable=True,
    )


# ---------------------------------------------------------------------------
# Archipelago Python deps — pip-installed from vendor/Archipelago/requirements.txt.
# Marker file at %LOCALAPPDATA%\SMBWArchipelago\ap_deps.ok avoids re-running
# the slow import probe on every wizard open.
# ---------------------------------------------------------------------------

def ap_deps_marker_path() -> Path:
    return local_appdata_root() / "ap_deps.ok"


def lz4_marker_path() -> Path:
    return local_appdata_root() / "lz4.ok"


def pyelftools_marker_path() -> Path:
    return local_appdata_root() / "pyelftools.ok"


def check_archipelago_deps() -> PrereqResult:
    """Archipelago Python dependencies satisfied.

    Strategy:
      1. **Packaged-install short-circuit**: if we're not in a dev clone
         and ``CommonClient`` is importable from the current Python,
         the surrounding Archipelago install already provides AP deps.
         There is no in-tree ``requirements.txt`` to install from, and
         shelling out to pip would either pollute the user's Python or
         no-op against an already-satisfied env.  Skip both check and
         (later) install.
      2. Fast path: marker file at %LOCALAPPDATA%\\SMBWArchipelago\\ap_deps.ok
         records that a prior wizard run installed deps successfully. If
         the marker is newer than vendor/Archipelago/requirements.txt,
         skip the import probe.
      3. Slow path: probe by importing a representative sample
         (`_AP_SAMPLE_IMPORTS`). Done in a subprocess so missing modules
         don't pollute the wizard's import space.

    On success, writes/refreshes the marker so subsequent runs short-circuit.
    """
    if not is_dev_clone() and archipelago_importable():
        return PrereqResult(
            "archipelago_deps", "Archipelago Python deps", True,
            "satisfied by surrounding Archipelago install (no dev clone)",
            auto_installable=True,
        )

    marker = ap_deps_marker_path()
    req_path = repo_root() / "vendor" / "Archipelago" / "requirements.txt"

    if marker.is_file() and req_path.is_file():
        try:
            if marker.stat().st_mtime >= req_path.stat().st_mtime:
                return PrereqResult(
                    "archipelago_deps", "Archipelago Python deps", True,
                    f"installed (marker at {marker})",
                    auto_installable=True,
                )
        except OSError:
            pass

    probe = "import " + ", ".join(_AP_SAMPLE_IMPORTS)
    py = _resolved_python_bin or sys.executable
    r = _safe_run([py, "-c", probe])
    if r is not None and r[0] == 0:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("ok\n", encoding="utf-8")
        except OSError:
            pass
        return PrereqResult(
            "archipelago_deps", "Archipelago Python deps", True,
            f"importable via {Path(py).name}",
            auto_installable=True,
        )
    err = ((r[1] if r else "") + (r[2] if r else "")).strip()
    first_err = next(
        (ln for ln in err.splitlines() if "ModuleNotFoundError" in ln),
        err.splitlines()[-1] if err else "import probe failed",
    )
    return PrereqResult(
        "archipelago_deps", "Archipelago Python deps", False,
        f"missing: {first_err}",
        INSTALL_URLS["archipelago_deps"],
        note=(
            "Click Auto-install to run:\n"
            f"    {Path(py).name} -m pip install -r "
            "vendor/Archipelago/requirements.txt\n"
            f"into the resolved Python ({py})."
        ),
        auto_installable=True,
    )


# ---------------------------------------------------------------------------
# lz4 — Python package required by LibHakkun's deploy.py at cmake POST_BUILD.
# deploy.cmake calls `python deploy.py` which does `import lz4.block` at
# the top; if the PATH-resolved Python doesn't have lz4 the build fails
# immediately after linking with a ModuleNotFoundError.
# ---------------------------------------------------------------------------

def check_lz4() -> PrereqResult:
    """Verify lz4 is importable from the resolved Python.

    LibHakkun's deploy.cmake runs:
        COMMAND python .../sys/tools/deploy.py ...
    deploy.py does `import lz4.block` before anything else. The Python
    that cmake picks up from PATH is the same one `_compose_build_env`
    prepends via `resolved_python_bin()` — so lz4 must be installed into
    that interpreter.
    """
    marker = lz4_marker_path()
    if marker.is_file():
        return PrereqResult(
            "lz4", "Python lz4 (cmake build dep)", True,
            f"installed (marker at {marker})",
            auto_installable=True,
        )

    py = _resolved_python_bin or sys.executable
    r = _safe_run([py, "-c", "import lz4.block"])
    if r is not None and r[0] == 0:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("ok\n", encoding="utf-8")
        except OSError:
            pass
        return PrereqResult(
            "lz4", "Python lz4 (cmake build dep)", True,
            f"importable via {Path(py).name}",
            auto_installable=True,
        )

    return PrereqResult(
        "lz4", "Python lz4 (cmake build dep)", False,
        "not installed (LibHakkun's deploy.py does `import lz4.block`)",
        INSTALL_URLS["lz4"],
        note=(
            "Click Auto-install to `pip install lz4` into the resolved "
            "Python. ~1 MB."
        ),
        auto_installable=True,
    )


# ---------------------------------------------------------------------------
# pyelftools — Python package required by LibHakkun's elf2nso.py at cmake
# POST_BUILD. elf2nso.py does `from elftools.elf.elffile import ELFFile` at
# the top; if the PATH-resolved Python doesn't have pyelftools the build
# fails immediately after linking with an ImportError.
# ---------------------------------------------------------------------------

def check_pyelftools() -> PrereqResult:
    """Verify pyelftools is importable from the resolved Python.

    LibHakkun's SwitchTools.cmake runs elf2nso.py (Python) which imports
    elftools. The Python that cmake picks up from PATH is the same one
    `_compose_build_env` prepends via `resolved_python_bin()` — so
    pyelftools must be installed into that interpreter.
    """
    marker = pyelftools_marker_path()
    if marker.is_file():
        return PrereqResult(
            "pyelftools", "Python pyelftools (cmake build dep)", True,
            f"installed (marker at {marker})",
            auto_installable=True,
        )

    py = _resolved_python_bin or sys.executable
    r = _safe_run([py, "-c", "from elftools.elf.elffile import ELFFile"])
    if r is not None and r[0] == 0:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("ok\n", encoding="utf-8")
        except OSError:
            pass
        return PrereqResult(
            "pyelftools", "Python pyelftools (cmake build dep)", True,
            f"importable via {Path(py).name}",
            auto_installable=True,
        )

    return PrereqResult(
        "pyelftools", "Python pyelftools (cmake build dep)", False,
        "not installed (LibHakkun's elf2nso.py does `from elftools.elf.elffile import ELFFile`)",
        INSTALL_URLS["pyelftools"],
        note=(
            "Click Auto-install to `pip install pyelftools` into the resolved "
            "Python. ~1 MB."
        ),
        auto_installable=True,
    )


# ---------------------------------------------------------------------------
# Ryujinx — emulator install detection. Warn-only: the user may install
# Ryujinx after running /setup, and the deploy phase can recover by
# creating the mod dir tree under the platform's default Ryujinx root.
# ---------------------------------------------------------------------------

def ryujinx_default_root() -> Path | None:
    """Return the first existing Ryujinx config root for this platform,
    or the platform's primary default if none exist yet.

    Delegates to `deploy.detect_ryujinx_path()` for the actual probing
    so this and the deploy phase agree on which directory to target.
    """
    # Local import: deploy.py imports prereqs symbols, so avoid the
    # module-level cycle.
    from .deploy import detect_ryujinx_path, ryujinx_primary_default
    found = detect_ryujinx_path()
    if found is not None:
        return found
    return ryujinx_primary_default()


def check_ryujinx() -> PrereqResult:
    root = ryujinx_default_root()
    if root is None:
        return PrereqResult("ryujinx", "Ryujinx", True,
                            "skipped (no platform default known)",
                            warn_only=True)
    if root.is_dir():
        return PrereqResult(
            "ryujinx", "Ryujinx", True,
            f"Ryujinx config detected ({root})",
        )
    return PrereqResult(
        "ryujinx", "Ryujinx", False,
        f"no Ryujinx config at {root} (deploy will create mod dir)",
        INSTALL_URLS["ryujinx"],
        note=(
            f"Ryujinx isn't strictly required for the build, but Deploy "
            f"targets {root}/mods/contents/010015100b514000/smbwap/exefs "
            f"by default. If you install Ryujinx later, /setup's Deploy "
            f"step will create the directory tree."
        ),
        warn_only=True,
    )


# ---------------------------------------------------------------------------
# check_all() — wizard-display order, lightest checks first.
# ---------------------------------------------------------------------------

def check_all() -> list[PrereqResult]:
    """Run every detector. Order is wizard-display order — cheap probes
    first, then heavier ones. LLVM 19 appears after the small-download
    rows because it's the bulkiest install (~806 MB) and most likely to
    be missing on a fresh dev box."""
    return [
        check_dev_mode(),
        check_git(),
        check_cmake(),
        check_ninja(),
        check_python311(),
        check_llvm19(),
        check_archipelago_submodule(),
        check_switch_mod_submodule(),
        check_archipelago_deps(),
        check_lz4(),
        check_pyelftools(),
        check_ryujinx(),
    ]


def all_ok(results: list[PrereqResult]) -> bool:
    """True iff every non-warn-only result is ok."""
    return all(r.ok for r in results if not r.warn_only)


def missing_auto_installable(results: list[PrereqResult]) -> list[str]:
    """Keys of failed-but-auto-installable detectors, in INSTALL_ORDER
    sequence (installers.py drives the actual order). Used by the wizard
    to populate the auto-install checkbox list."""
    return [r.key for r in results if not r.ok and r.auto_installable]
