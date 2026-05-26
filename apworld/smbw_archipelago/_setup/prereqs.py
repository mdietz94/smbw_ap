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

# Suppress the per-child console window when running under the Launcher's
# windowed PyInstaller (no parent console → Windows opens a fresh console
# for each CONSOLE-subsystem child, which steals focus from the wizard).
# No-op on non-Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Hard min for cmake — switch-mod uses target features that landed in 3.21
# (FetchContent updates, project versioning). The CLAUDE.md commands assume
# a Kitware Windows CMake; msys2's cmake mangles drive letters and is
# rejected below.
MIN_CMAKE = (3, 21)

# AP's CommonClient + kvui require Python 3.10+. We aim higher for headroom
# and to match the AP launcher's bundled interpreter.
MIN_PYTHON = (3, 11)

# Sample imports that prove the Archipelago Python deps are usable. If any
# of these fail, vendor/Archipelago/requirements.txt is not satisfied and
# the wizard's pip-install step is required.
_AP_SAMPLE_IMPORTS = ("websockets", "bsdiff4", "certifi", "kvui")

# devkitPro environment variable + binary path. The installer sets DEVKITPRO
# system-wide and (typically) drops devkitA64 at $DEVKITPRO\devkitA64\bin.
_DEVKITPRO_GCC_REL = Path("devkitA64") / "bin" / "aarch64-none-elf-gcc.exe"
_DEVKITPRO_PACMAN_REL = Path("msys2") / "usr" / "bin" / "pacman.exe"

# Install pages surfaced by the wizard's "Install..." fallback link when
# auto-install isn't available.
INSTALL_URLS = {
    "dev_mode": "ms-settings:developers",
    "git": "https://git-scm.com/download/win",
    "cmake": "https://cmake.org/download/",
    "ninja": "https://github.com/ninja-build/ninja/releases",
    "python311": "https://www.python.org/downloads/release/python-3119/#files",
    "devkitpro": "https://github.com/devkitPro/installer/releases/latest",
    "switch_dev": "",
    "archipelago_submodule": "",
    "switch_mod_submodule": "",
    "archipelago_deps": "",
    "ryujinx": "https://github.com/Ryubing/Ryujinx/releases",
}


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
    """
    return Path(__file__).resolve().parents[3]


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
        return PrereqResult(
            "git", "Git", False, "not found on PATH",
            INSTALL_URLS["git"],
            note=(
                "Easiest install on Windows:\n"
                "    winget install Git.Git\n"
                "Or click Auto-install. PATH changes don't reach a running "
                "process, so the wizard re-probes Git's winget install dir "
                "on Re-check."
            ),
            auto_installable=True,
        )
    ver = (r[1] or r[2]).strip()
    return PrereqResult("git", "Git", True, ver, auto_installable=True)


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
    for default in _CMAKE_DEFAULT_PATHS:
        if default.exists():
            candidates.append(str(default))
    candidates.append("cmake")

    saw_msys2_path_fallback = False
    for cand in candidates:
        r = _safe_run([cand, "--version"])
        if r is None or r[0] != 0:
            continue
        ver = _parse_cmake_version(r[1] or r[2])
        if ver is None:
            continue
        if (ver[0], ver[1]) < MIN_CMAKE:
            continue
        if cand == "cmake":
            resolved_path = shutil.which(cand)
            if _is_msys2_cmake(resolved_path):
                saw_msys2_path_fallback = True
                continue
        _resolved_cmake = cand
        return PrereqResult(
            "cmake", f"CMake {MIN_CMAKE[0]}.{MIN_CMAKE[1]}+", True,
            f"{ver[0]}.{ver[1]}.{ver[2]} ({cand})",
            auto_installable=True,
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
    return PrereqResult(
        "cmake", f"CMake {MIN_CMAKE[0]}.{MIN_CMAKE[1]}+", False,
        "not found on PATH",
        INSTALL_URLS["cmake"],
        note=(
            "Easiest install on Windows:\n"
            "    winget install Kitware.CMake\n"
            "Or click Auto-install above."
        ),
        auto_installable=True,
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
        return PrereqResult(
            "ninja", "Ninja", False, "not found on PATH",
            INSTALL_URLS["ninja"],
            note=(
                "Easiest install on Windows:\n"
                "    winget install Ninja-build.Ninja\n"
                "Or click Auto-install above. The wizard probes winget's "
                "install dir directly on Re-check, so no shell restart "
                "needed."
            ),
            auto_installable=True,
        )
    resolved = shutil.which("ninja")
    if resolved:
        _resolved_ninja_bin = str(Path(resolved).parent)
    ver = (r[1] or r[2]).strip()
    return PrereqResult("ninja", "Ninja", True, ver, auto_installable=True)


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
    candidates += [
        ["py", "-3.11", "--version"],
        ["py", "-3.12", "--version"],
        ["py", "-3.13", "--version"],
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
        return PrereqResult(
            "python311", f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+", True,
            f"{ver[0]}.{ver[1]}.{ver[2]} ({cmd[0]})",
            auto_installable=True,
        )
    return PrereqResult(
        "python311", f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+", False,
        f"no Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ found",
        INSTALL_URLS["python311"],
        note=(
            "Easiest install on Windows:\n"
            "    winget install Python.Python.3.11\n"
            "Or click Auto-install. Archipelago's CommonClient + kvui "
            f"need Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+."
        ),
        auto_installable=True,
    )


# ---------------------------------------------------------------------------
# devkitPro / devkitA64 — cross-compiler for the Switch target.
# ---------------------------------------------------------------------------

_resolved_devkitpro_root: str | None = None


def resolved_devkitpro_root() -> str | None:
    return _resolved_devkitpro_root


def _devkitpro_default_root() -> Path | None:
    """Standard install location on Windows: C:\\devkitPro. Honored when
    %DEVKITPRO% isn't set yet (e.g. the user just installed but hasn't
    restarted the wizard)."""
    if sys.platform == "win32":
        for candidate in (Path("C:/devkitPro"), Path("C:/devkitpro")):
            if candidate.is_dir():
                return candidate
    return None


def check_devkitpro() -> PrereqResult:
    """devkitPro install + devkitA64 cross-compiler.

    Probe order:
      1. `%DEVKITPRO%` env var → check for `devkitA64/bin/aarch64-none-elf-gcc.exe`
      2. `C:\\devkitPro` (default Windows install location)

    Side effect: caches the resolved root so installers.py / build.py
    can re-use it without re-probing.
    """
    global _resolved_devkitpro_root

    candidates: list[Path] = []
    env_root = os.environ.get("DEVKITPRO")
    if env_root:
        candidates.append(Path(env_root))
    default = _devkitpro_default_root()
    if default and default not in candidates:
        candidates.append(default)

    for root in candidates:
        gcc = root / _DEVKITPRO_GCC_REL
        if not gcc.is_file():
            continue
        r = _safe_run([str(gcc), "--version"])
        if r is None or r[0] != 0:
            continue
        ver_line = (r[1] or r[2]).strip().splitlines()[0] if (r[1] or r[2]).strip() else ""
        _resolved_devkitpro_root = str(root)
        # Populate $DEVKITPRO for downstream subprocesses (cmake reads
        # this in the toolchain file).
        os.environ.setdefault("DEVKITPRO", str(root))
        return PrereqResult(
            "devkitpro", "devkitPro / devkitA64", True,
            f"{ver_line or 'devkitA64 gcc'} ({root})",
            auto_installable=True,
        )

    return PrereqResult(
        "devkitpro", "devkitPro / devkitA64", False,
        "not found (DEVKITPRO env unset and no install at C:\\devkitPro)",
        INSTALL_URLS["devkitpro"],
        note=(
            "Install devkitPro from the official installer "
            "(github.com/devkitPro/installer/releases) and select the "
            "Switch toolchain. The wizard's Auto-install downloads + runs "
            "this for you. After install, %DEVKITPRO% is set automatically; "
            "click Re-check to pick it up without restarting the wizard."
        ),
        auto_installable=True,
    )


# ---------------------------------------------------------------------------
# switch-dev pacman group — devkitPro ships a meta-installer that needs
# `pacman -S switch-dev` to actually fetch the Switch toolchain + libnx.
# A bare devkitPro install without switch-dev is missing libnx headers.
# ---------------------------------------------------------------------------

def _pacman_path() -> Path | None:
    root = _resolved_devkitpro_root or os.environ.get("DEVKITPRO")
    if not root:
        return None
    p = Path(root) / _DEVKITPRO_PACMAN_REL
    return p if p.is_file() else None


def check_switch_dev() -> PrereqResult:
    """devkitPro's `switch-dev` pacman package group — provides libnx,
    switch-tools, switch-libs, etc.

    Depends on `check_devkitpro` having run first (so the resolved root
    is available). If pacman itself can't be found, we surface a clear
    "devkitPro missing pacman" message rather than failing silently.
    """
    pac = _pacman_path()
    if pac is None:
        return PrereqResult(
            "switch_dev", "devkitPro switch-dev", False,
            "pacman not found (run devkitPro check first)",
            INSTALL_URLS["switch_dev"],
            note="Install devkitPro first; switch-dev installs via its bundled pacman.",
        )
    r = _safe_run([str(pac), "-Qq", "switch-dev"])
    if r is None or r[0] != 0:
        return PrereqResult(
            "switch_dev", "devkitPro switch-dev", False,
            "switch-dev package group not installed via pacman",
            INSTALL_URLS["switch_dev"],
            note=(
                "Open the devkitPro MSYS2 shell (`$DEVKITPRO\\msys2\\msys2.exe`) and run:\n"
                "    pacman -S --noconfirm switch-dev\n"
                "Or click Auto-install — the wizard invokes pacman silently."
            ),
            auto_installable=True,
        )
    # pacman -Qq prints the package name on success.
    pkgs = [ln for ln in (r[1] or "").strip().splitlines() if ln]
    return PrereqResult(
        "switch_dev", "devkitPro switch-dev", True,
        f"installed ({len(pkgs)} pkg(s))",
        auto_installable=True,
    )


# ---------------------------------------------------------------------------
# Submodules — vendor/Archipelago + switch-mod
# ---------------------------------------------------------------------------

def check_archipelago_submodule() -> PrereqResult:
    """vendor/Archipelago submodule initialized.

    Probes for a sentinel file (`CommonClient.py`) at the expected path.
    A missing submodule is the typical first-run failure on a fresh clone.
    """
    sentinel = repo_root() / "vendor" / "Archipelago" / "CommonClient.py"
    if sentinel.is_file():
        return PrereqResult(
            "archipelago_submodule", "vendor/Archipelago submodule", True,
            f"present ({sentinel.parent})",
            auto_installable=True,
        )
    return PrereqResult(
        "archipelago_submodule", "vendor/Archipelago submodule", False,
        f"missing {sentinel} (submodule not initialized?)",
        INSTALL_URLS["archipelago_submodule"],
        note=(
            "Run from the repo root:\n"
            "    git submodule update --init --recursive vendor/Archipelago\n"
            "Or click Auto-install — the wizard runs the git command for you."
        ),
        auto_installable=True,
    )


def check_switch_mod_submodule() -> PrereqResult:
    """switch-mod submodule initialized.

    Sentinel: `switch-mod/CMakeLists.txt`. Without it, the build phase
    has nothing to configure.
    """
    sentinel = repo_root() / "switch-mod" / "CMakeLists.txt"
    if sentinel.is_file():
        return PrereqResult(
            "switch_mod_submodule", "switch-mod submodule", True,
            f"present ({sentinel.parent})",
            auto_installable=True,
        )
    return PrereqResult(
        "switch_mod_submodule", "switch-mod submodule", False,
        f"missing {sentinel}",
        INSTALL_URLS["switch_mod_submodule"],
        note=(
            "Run from the repo root:\n"
            "    git submodule update --init --recursive switch-mod\n"
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
    from . import local_appdata_root
    return local_appdata_root() / "ap_deps.ok"


def check_archipelago_deps() -> PrereqResult:
    """Archipelago Python dependencies satisfied.

    Strategy:
      1. Fast path: marker file at %LOCALAPPDATA%\\SMBWArchipelago\\ap_deps.ok
         records that a prior wizard run installed deps successfully. If
         the marker is newer than vendor/Archipelago/requirements.txt,
         skip the import probe.
      2. Slow path: probe by importing a representative sample
         (`_AP_SAMPLE_IMPORTS`). Done in a subprocess so missing modules
         don't pollute the wizard's import space.

    On success, writes/refreshes the marker so subsequent runs short-circuit.
    """
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
# Ryujinx — emulator install detection. Warn-only: the user may install
# Ryujinx after running /setup, and the deploy phase can recover by
# creating the mod dir tree under %APPDATA%\Ryujinx\.
# ---------------------------------------------------------------------------

def ryujinx_default_root() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Ryujinx"


def check_ryujinx() -> PrereqResult:
    root = ryujinx_default_root()
    if root is None:
        return PrereqResult("ryujinx", "Ryujinx", True, "skipped (no APPDATA)",
                            warn_only=True)
    if root.is_dir():
        return PrereqResult(
            "ryujinx", "Ryujinx", True,
            f"%APPDATA%\\Ryujinx detected ({root})",
        )
    return PrereqResult(
        "ryujinx", "Ryujinx", False,
        f"no Ryujinx config at {root} (deploy will create mod dir)",
        INSTALL_URLS["ryujinx"],
        note=(
            "Ryujinx isn't strictly required for the build, but Deploy "
            "targets %APPDATA%\\Ryujinx\\mods\\contents\\010015100b514000\\"
            "smbwap\\exefs by default. If you install Ryujinx later, "
            "/setup's Deploy step will create the directory tree."
        ),
        warn_only=True,
    )


# ---------------------------------------------------------------------------
# check_all() — wizard-display order, lightest checks first.
# ---------------------------------------------------------------------------

def check_all() -> list[PrereqResult]:
    """Run every detector. Order is wizard-display order — cheap probes
    first, then heavier ones. devkitPro/switch-dev appear after CMake
    because they're the bulkiest install and the most likely to be missing
    on a fresh dev box."""
    return [
        check_dev_mode(),
        check_git(),
        check_cmake(),
        check_ninja(),
        check_python311(),
        check_devkitpro(),
        check_switch_dev(),
        check_archipelago_submodule(),
        check_switch_mod_submodule(),
        check_archipelago_deps(),
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
