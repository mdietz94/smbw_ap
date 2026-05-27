"""Auto-installers for the wizard's "Install all missing" mode.

The wizard's prereq page offers two paths (see `wizard.py`):
  - **Manual**: surface install links + paste-able commands.
  - **Auto**: run these installers silently.

Coverage:
  - winget tools (Git, CMake, Ninja, Python 3.11) — silent install +
    PATH prepend so the next Re-check resolves the tool without a
    shell restart.
  - devkitPro: download the official Windows installer from GitHub
    releases (with SHA-256 verification) and run it. Interactive — the
    installer's silent-mode flags vary by NSIS/InnoSetup version and a
    broken silent run is worse than a clear "click through this window".
  - switch-dev pacman group: invoke `pacman -S --noconfirm switch-dev`
    via devkitPro's bundled MSYS2 once devkitPro is present.
  - Git submodule init: `git submodule update --init --recursive` for
    `vendor/Archipelago` and `switch-mod/lib/{imgui,NintendoSDK,sead}`.
  - pip install of Archipelago's `requirements.txt` into the resolved
    Python 3.11+.

Each installer:
  - Streams subprocess output line-by-line to an `on_line` callback so
    the wizard's log popup shows live progress.
  - Pre-checks disk space (when applicable) before any download begins.
  - Returns an `InstallResult` with `ok` / `returncode` / `log` /
    `detail` so the wizard can distinguish "succeeded" from "failed"
    without parsing the stream.

Per-tool functions are keyed in `INSTALLERS` so the wizard's "Install
all missing" walker can iterate in `INSTALL_ORDER`.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import local_appdata_root
from .prereqs import (
    _prepend_path,
    _winget_ninja_paths,
    _DEVKITPRO_PACMAN_REL,
    _devkitpro_default_root,
    is_dev_clone,
    repo_root,
    resolved_devkitpro_root,
    resolved_python_bin,
)

# Suppress per-child console window under windowed Launcher
# (PyInstaller). No-op on non-Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

ProgressFn = Callable[[str], None]


@dataclass
class InstallResult:
    """Outcome of one install attempt.

    `ok` is the green-light flag. `returncode` is the underlying tool's
    exit code. `log` is the full captured stream for the wizard's
    "Copy log" button. `detail` is a short human-readable summary for
    the row's status flip.
    """
    ok: bool
    returncode: int
    log: str
    detail: str = ""


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def check_winget(on_line: ProgressFn | None = None) -> InstallResult:
    """Verify winget is on PATH before any wave of winget installs.

    winget ships with Windows 10 1809+ via the App Installer package,
    but LTSC images and stripped Win11 setups can lack it. Surface a
    single "install App Installer" error instead of N confusing
    winget-not-found errors in a row.
    """
    exe = shutil.which("winget")
    if exe is None:
        msg = (
            "winget not found on PATH — install \"App Installer\" from the "
            "Microsoft Store, or switch to Manual mode."
        )
        if on_line:
            on_line(msg)
        return InstallResult(False, 127, msg, msg)
    if on_line:
        on_line(f"[winget] resolved to {exe}")
    return InstallResult(True, 0, exe, exe)


def check_internet(on_line: ProgressFn | None = None) -> InstallResult:
    """Single connectivity probe before bulk install.

    HEAD https://github.com — we don't care that GitHub itself is up,
    we care that *some* HTTPS host responds, because every auto-
    installer pulls from a https URL. One clear "no internet" error
    beats N timeouts deep inside per-tool installers.
    """
    msg_ok = "internet reachable"
    msg_fail = (
        "no internet connectivity (HEAD https://github.com failed). "
        "Connect to the internet and click Install all missing again, "
        "or switch to Manual mode."
    )
    try:
        req = urllib.request.Request("https://github.com", method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 400:
                if on_line:
                    on_line(f"[net] {msg_ok} ({resp.status})")
                return InstallResult(True, 0, str(resp.status), msg_ok)
    except (urllib.error.URLError, OSError) as e:
        if on_line:
            on_line(f"[net] {msg_fail} ({e})")
        return InstallResult(False, 1, f"{type(e).__name__}: {e}", msg_fail)
    if on_line:
        on_line(f"[net] {msg_fail}")
    return InstallResult(False, 1, "non-2xx", msg_fail)


class InsufficientDiskError(RuntimeError):
    """Raised by `_check_disk_space` when the target drive has less free
    space than the install needs."""


def _check_disk_space(target: Path, need_bytes: int) -> None:
    probe = target
    for _ in range(20):
        if probe.exists():
            break
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    try:
        usage = shutil.disk_usage(str(probe))
    except OSError as e:
        raise InsufficientDiskError(
            f"could not determine free space on {probe}: {e}"
        ) from e
    if usage.free < need_bytes:
        raise InsufficientDiskError(
            f"not enough free space on {probe}: need "
            f"{need_bytes / (1024 ** 3):.2f} GiB, have "
            f"{usage.free / (1024 ** 3):.2f} GiB. Free up space and re-run."
        )


# ---------------------------------------------------------------------------
# Subprocess + winget helpers
# ---------------------------------------------------------------------------

def _stream_subprocess(
    cmd: list[str],
    *,
    on_line: ProgressFn | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> InstallResult:
    """Run a subprocess, streaming stdout+stderr line-by-line.

    Mirrors `build._stream_subprocess` (lighter weight here — no separate
    reader thread; install commands are fast enough to block-read).
    """
    log_lines: list[str] = []

    def _emit(line: str) -> None:
        log_lines.append(line)
        if on_line is not None:
            on_line(line)

    _emit(f"[install] spawning: {cmd}")
    child_env = dict(env) if env is not None else os.environ.copy()
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_NO_WINDOW,
        )
    except (FileNotFoundError, OSError) as e:
        msg = f"failed to spawn {cmd[0]}: {e}"
        _emit(msg)
        return InstallResult(False, 127, msg, msg)

    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            _emit(raw.rstrip("\r\n"))
        rc = proc.wait(timeout=timeout) if timeout else proc.wait()
    except subprocess.TimeoutExpired:
        _emit(f"[install] timeout after {timeout:.0f}s; killing pid={proc.pid}")
        proc.kill()
        proc.wait()
        rc = 124
    _emit(f"[install] subprocess exited with code {rc}")
    return InstallResult(rc == 0, rc, "\n".join(log_lines))


def winget_install(
    package_id: str,
    *,
    on_line: ProgressFn | None = None,
) -> InstallResult:
    """Silent winget install of a single package.

    `-e --id <id>` is exact-match by package identifier (so we don't
    accidentally install a near-name match). `--silent` suppresses the
    package's own GUI; `--accept-*-agreements` declines EULA prompts;
    `--disable-interactivity` is winget's master "never prompt" switch.
    """
    wg = shutil.which("winget")
    if wg is None:
        msg = "winget not on PATH (install App Installer from the Microsoft Store)"
        return InstallResult(False, 127, msg, msg)
    return _stream_subprocess(
        [
            wg, "install",
            "-e", "--id", package_id,
            "--silent",
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--disable-interactivity",
        ],
        on_line=on_line,
    )


# ---------------------------------------------------------------------------
# Per-tool installers
# ---------------------------------------------------------------------------

def install_git(on_line: ProgressFn | None = None) -> InstallResult:
    """winget-install Git for Windows."""
    r = winget_install("Git.Git", on_line=on_line)
    if not r.ok:
        return r
    # Standard install paths. Prepend the first that exists so a Re-check
    # without a shell restart resolves git.
    for candidate in (
        Path("C:/Program Files/Git/cmd/git.exe"),
        Path("C:/Program Files (x86)/Git/cmd/git.exe"),
    ):
        if candidate.is_file():
            _prepend_path(candidate.parent)
            if on_line:
                on_line(f"[install] prepended {candidate.parent} to PATH")
            return InstallResult(True, 0, r.log, str(candidate))
    return InstallResult(
        False, 1, r.log,
        "winget reported success but git.exe wasn't at the standard path",
    )


def install_cmake(on_line: ProgressFn | None = None) -> InstallResult:
    """winget-install Kitware CMake and prepend its install dir to PATH."""
    r = winget_install("Kitware.CMake", on_line=on_line)
    if not r.ok:
        return r
    for candidate in (
        Path("C:/Program Files/CMake/bin/cmake.exe"),
        Path("C:/Program Files (x86)/CMake/bin/cmake.exe"),
    ):
        if candidate.is_file():
            _prepend_path(candidate.parent)
            if on_line:
                on_line(f"[install] prepended {candidate.parent} to PATH")
            return InstallResult(True, 0, r.log, str(candidate))
    return InstallResult(
        False, 1, r.log,
        "winget reported success but cmake.exe wasn't at the canonical path",
    )


def install_ninja(on_line: ProgressFn | None = None) -> InstallResult:
    """winget-install Ninja and prepend its install dir to PATH."""
    r = winget_install("Ninja-build.Ninja", on_line=on_line)
    if not r.ok:
        return r
    paths = _winget_ninja_paths()
    if paths:
        _prepend_path(paths[0].parent)
        if on_line:
            on_line(f"[install] prepended {paths[0].parent} to PATH")
        return InstallResult(True, 0, r.log, str(paths[0]))
    return InstallResult(
        False, 1, r.log,
        "winget reported success but ninja.exe wasn't under the standard winget path",
    )


def install_python311(on_line: ProgressFn | None = None) -> InstallResult:
    """winget-install Python 3.11."""
    r = winget_install("Python.Python.3.11", on_line=on_line)
    if not r.ok:
        return r
    # Best-effort PATH prepend: winget lands py.exe under
    # %LOCALAPPDATA%\Programs\Python\Launcher\.
    localapp = os.environ.get("LOCALAPPDATA")
    if localapp:
        launcher = Path(localapp) / "Programs" / "Python" / "Launcher"
        if launcher.is_dir():
            _prepend_path(launcher)
            if on_line:
                on_line(f"[install] prepended {launcher} to PATH")
    return InstallResult(True, 0, r.log, "Python 3.11 installed")


# ---------------------------------------------------------------------------
# devkitPro — direct download + interactive run
# ---------------------------------------------------------------------------

# Pinned latest devkitPro Windows installer (as of 2026-05-25). Bumping
# is a three-field change: URL + (optional) SHA256 + the expected
# DEVKITPRO env var the installer sets system-wide. The installer is
# ~5 MB; the real toolchain comes down via pacman afterward (see
# `install_switch_dev`).
#
# We pin a specific release rather than tracking "latest" because the
# devkitPro installer occasionally changes its silent-mode flags and a
# silent install with the wrong flag set runs interactively anyway.
# A pinned version lets us pre-verify the flag set works.
DEVKITPRO_INSTALLER_URL = (
    "https://github.com/devkitPro/installer/releases/download/"
    "v3.0.3/devkitpro-updater-3.0.3.exe"
)
# SHA-256 left empty in v1: the upstream release doesn't publish a
# matching `.sha256` file, and pinning the binary's hash without an
# upstream source-of-truth invites stale-pin breakage. Future hardening:
# add the hash once we've verified it from a known-good machine.
DEVKITPRO_INSTALLER_SHA256: str = ""
DEVKITPRO_INSTALLER_BYTES = 6 * 1024 * 1024     # ~6 MB headroom
DEVKITPRO_INSTALL_MIN_FREE_BYTES = 1 * 1024 ** 3   # ~1 GiB for switch-dev too


def _devkitpro_local_cache() -> Path:
    d = local_appdata_root() / "devkitpro"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _download_with_progress(
    url: str,
    dest: Path,
    *,
    on_line: ProgressFn | None = None,
    expected_sha256: str = "",
) -> InstallResult:
    """Stream a URL to disk with per-chunk progress + optional SHA-256
    verification.

    Progress lines emit at roughly 5%-of-total intervals to keep the
    wizard log readable for ~MB downloads while still showing forward
    motion for ~GB ones.
    """
    log_lines: list[str] = []

    def _emit(s: str) -> None:
        log_lines.append(s)
        if on_line:
            on_line(s)

    _emit(f"[download] {url}")
    _emit(f"[download] dest: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "smbwap-setup/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length", "0") or 0)
            written = 0
            next_threshold = 0
            sha = hashlib.sha256() if expected_sha256 else None
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as fp:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    fp.write(chunk)
                    if sha is not None:
                        sha.update(chunk)
                    written += len(chunk)
                    if total and written >= next_threshold:
                        pct = (written * 100.0) / total
                        _emit(f"[download] {written/1024/1024:.1f}/{total/1024/1024:.1f} MB ({pct:.0f}%)")
                        next_threshold = written + max(total // 20, 1)
            os.replace(tmp, dest)
    except (urllib.error.URLError, OSError) as e:
        msg = f"[download] FAILED: {type(e).__name__}: {e}"
        _emit(msg)
        return InstallResult(False, 1, "\n".join(log_lines), msg)

    if expected_sha256 and sha is not None:
        got = sha.hexdigest()
        if got.lower() != expected_sha256.lower():
            msg = f"[download] SHA-256 mismatch: expected {expected_sha256}, got {got}"
            _emit(msg)
            try:
                dest.unlink()
            except OSError:
                pass
            return InstallResult(False, 1, "\n".join(log_lines), msg)
        _emit(f"[download] SHA-256 ok: {got}")

    _emit(f"[download] done: {dest}")
    return InstallResult(True, 0, "\n".join(log_lines), str(dest))


def install_devkitpro(on_line: ProgressFn | None = None) -> InstallResult:
    """Download the official devkitPro installer and launch it.

    Strategy:
      1. Pre-check disk (need ~1 GiB free for installer + switch-dev pkgs).
      2. Download the installer to %LOCALAPPDATA%\\SMBWArchipelago\\devkitpro\\.
      3. Run it interactively (not silent — installer's silent flags vary
         by version and a half-broken silent install is worse than a
         clearly-visible interactive one).
      4. Tell the user via on_line / detail to click through and then
         Re-check. The post-install probe (`prereqs.check_devkitpro`)
         picks up the new $DEVKITPRO env var when the user clicks
         Re-check.

    On non-Windows: returns ok=False with a "not supported" message —
    devkitPro on POSIX uses a different installer entirely (pacman from
    the user's distro).
    """
    if sys.platform != "win32":
        msg = (
            "automatic devkitPro install is Windows-only; on POSIX, "
            "follow https://devkitpro.org/wiki/Getting_Started"
        )
        if on_line:
            on_line(msg)
        return InstallResult(False, 1, msg, msg)

    cache = _devkitpro_local_cache()
    try:
        _check_disk_space(cache, DEVKITPRO_INSTALL_MIN_FREE_BYTES)
    except InsufficientDiskError as e:
        if on_line:
            on_line(f"[install] {e}")
        return InstallResult(False, 1, str(e), str(e))

    installer = cache / "devkitpro-updater.exe"
    if not installer.is_file():
        dl = _download_with_progress(
            DEVKITPRO_INSTALLER_URL,
            installer,
            on_line=on_line,
            expected_sha256=DEVKITPRO_INSTALLER_SHA256,
        )
        if not dl.ok:
            return dl

    if on_line:
        on_line(
            "[install] launching devkitPro installer — click through the "
            "installer that opens, select the Switch toolchain, then "
            "click Re-check in the wizard."
        )
    # Run without --silent: the installer is a stub that downloads + runs
    # the real toolchain installer; silent flags are inconsistent across
    # versions and a stuck-but-invisible installer is worse than an
    # interactive one.
    try:
        proc = subprocess.Popen(
            [str(installer)],
            creationflags=_NO_WINDOW,
        )
    except OSError as e:
        msg = f"failed to launch installer: {e}"
        if on_line:
            on_line(f"[install] {msg}")
        return InstallResult(False, 1, msg, msg)

    msg = (
        "devkitPro installer launched (PID "
        f"{proc.pid}). Click through it, then click Re-check."
    )
    if on_line:
        on_line(f"[install] {msg}")
    # Don't wait — the installer is interactive and the user owns the
    # subsequent click-through. The wizard's Re-check button re-probes
    # the prereq row.
    return InstallResult(True, 0, msg, msg)


def install_switch_dev(on_line: ProgressFn | None = None) -> InstallResult:
    """Invoke `pacman -S --noconfirm switch-dev` via devkitPro's MSYS2.

    Requires `install_devkitpro` to have completed successfully (and the
    user to have clicked Re-check on the devkitPro row so the wizard
    resolved `%DEVKITPRO%`). Without a resolved devkitPro root, this
    can't even find pacman.
    """
    root = resolved_devkitpro_root() or os.environ.get("DEVKITPRO")
    if not root:
        # Fall back to default install location — the user may have
        # installed but not Re-checked yet.
        default = _devkitpro_default_root()
        if default:
            root = str(default)
    if not root:
        msg = "DEVKITPRO not set and no install at default location; run devkitPro install first"
        if on_line:
            on_line(f"[install] {msg}")
        return InstallResult(False, 1, msg, msg)

    pac = Path(root) / _DEVKITPRO_PACMAN_REL
    if not pac.is_file():
        msg = f"pacman not found at {pac} (devkitPro install may be incomplete)"
        if on_line:
            on_line(f"[install] {msg}")
        return InstallResult(False, 1, msg, msg)

    # Pass devkitPro's own MSYS2 env vars so pacman finds its mirrors.
    env = os.environ.copy()
    env["DEVKITPRO"] = root
    return _stream_subprocess(
        [str(pac), "-S", "--noconfirm", "--needed", "switch-dev"],
        on_line=on_line,
        env=env,
        timeout=600.0,   # 10 min — slow mirrors can take a while
    )


# ---------------------------------------------------------------------------
# Git submodules
# ---------------------------------------------------------------------------

def _git_submodule_update(
    *submodule_paths: str,
    on_line: ProgressFn | None = None,
) -> InstallResult:
    """`git submodule update --init --recursive -- <path>...` from repo root.

    Pre-flights that we're actually in a git checkout.  Without this, a
    user running from a packaged apworld install (no ``.git`` anywhere
    in the tree) sees ``fatal: not a git repository`` from git itself,
    which is unactionable -- they'd need to know the wizard is trying to
    update a submodule of a repo that doesn't exist on their disk.
    """
    repo = repo_root()
    if not is_dev_clone(repo):
        msg = (
            f"not a git clone at {repo}: cannot run git submodule update. "
            f"To rebuild the Switch subsdk, clone the source repo "
            f"(https://github.com/mdietz94/smwonder_archipelago) and run "
            f"the wizard from there.  If the apworld is already installed "
            f"into your Archipelago, you do not need this step."
        )
        if on_line:
            on_line(f"[install] {msg}")
        return InstallResult(False, 1, msg, msg)
    git = shutil.which("git")
    if git is None:
        msg = "git not found on PATH; install git first"
        if on_line:
            on_line(f"[install] {msg}")
        return InstallResult(False, 127, msg, msg)
    return _stream_subprocess(
        [git, "submodule", "update", "--init", "--recursive", "--", *submodule_paths],
        on_line=on_line,
        cwd=repo,
        timeout=600.0,
    )


def install_archipelago_submodule(on_line: ProgressFn | None = None) -> InstallResult:
    return _git_submodule_update("vendor/Archipelago", on_line=on_line)


def install_switch_mod_submodule(on_line: ProgressFn | None = None) -> InstallResult:
    # switch-mod itself was promoted from submodule to plain subdirectory of
    # this repo. Only its nested third-party libs (imgui / NintendoSDK / sead)
    # still need `git submodule update --init` to be checked out.
    return _git_submodule_update(
        "switch-mod/lib/imgui",
        "switch-mod/lib/NintendoSDK",
        "switch-mod/lib/sead",
        on_line=on_line,
    )


# ---------------------------------------------------------------------------
# Archipelago pip deps
# ---------------------------------------------------------------------------

def install_archipelago_deps(on_line: ProgressFn | None = None) -> InstallResult:
    """`pip install -r vendor/Archipelago/requirements.txt` into the
    resolved Python 3.11+.

    Uses `prereqs.resolved_python_bin()` so the install lands in the same
    interpreter the wizard's import-probe will check on Re-check —
    otherwise a user with two Pythons could install into one and probe
    the other.
    """
    py = resolved_python_bin() or sys.executable
    req_path = repo_root() / "vendor" / "Archipelago" / "requirements.txt"
    if not req_path.is_file():
        msg = (
            f"requirements.txt not found at {req_path} — initialize the "
            "Archipelago submodule first"
        )
        if on_line:
            on_line(f"[install] {msg}")
        return InstallResult(False, 1, msg, msg)
    return _stream_subprocess(
        [py, "-m", "pip", "install", "-r", str(req_path)],
        on_line=on_line,
        timeout=600.0,
    )


# ---------------------------------------------------------------------------
# Registry + ordering
# ---------------------------------------------------------------------------

INSTALLERS: dict[str, Callable[[ProgressFn | None], InstallResult]] = {
    "git": install_git,
    "cmake": install_cmake,
    "ninja": install_ninja,
    "python311": install_python311,
    "devkitpro": install_devkitpro,
    "switch_dev": install_switch_dev,
    "archipelago_submodule": install_archipelago_submodule,
    "switch_mod_submodule": install_switch_mod_submodule,
    "archipelago_deps": install_archipelago_deps,
}

# Order the wizard's "Install all missing" walker uses. Dependencies feed
# downstream:
#   git → submodules (need git to clone)
#   submodules → archipelago_deps (need vendor/Archipelago/requirements.txt)
#   devkitpro → switch_dev (need pacman from devkitPro's MSYS2)
INSTALL_ORDER: tuple[str, ...] = (
    "git",
    "cmake",
    "ninja",
    "python311",
    "devkitpro",
    "switch_dev",
    "archipelago_submodule",
    "switch_mod_submodule",
    "archipelago_deps",
)


def install_many(
    keys: list[str],
    *,
    on_line: ProgressFn | None = None,
) -> tuple[list[str], list[str], dict[str, InstallResult]]:
    """Run the auto-installer for each key in `keys`, in INSTALL_ORDER.

    Returns `(installed, failed, results)` where `installed`/`failed`
    partition the keys actually attempted, and `results` is the per-key
    InstallResult for the wizard to render.

    Stops on first failure — later keys in INSTALL_ORDER often depend on
    earlier ones (switch_dev needs devkitpro; archipelago_deps needs the
    submodule).
    """
    installed: list[str] = []
    failed: list[str] = []
    results: dict[str, InstallResult] = {}
    selected = set(keys)
    for key in INSTALL_ORDER:
        if key not in selected:
            continue
        installer = INSTALLERS.get(key)
        if installer is None:
            failed.append(key)
            results[key] = InstallResult(False, 1, f"no installer for key {key!r}",
                                         f"no installer for {key}")
            break
        if on_line:
            on_line(f"[install] starting {key}")
        r = installer(on_line)
        results[key] = r
        if r.ok:
            installed.append(key)
        else:
            failed.append(key)
            break
    return installed, failed, results
