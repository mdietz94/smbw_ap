"""Auto-installers for the wizard's "Install all missing" mode.

The wizard's prereq page offers two paths (see `wizard.py`):
  - **Manual**: surface install links + paste-able commands.
  - **Auto**: run these installers silently.

Coverage:
  - winget tools (Git, CMake, Ninja, Python 3.11) — silent install +
    PATH prepend so the next Re-check resolves the tool without a
    shell restart.
  - LLVM 19.1.7: download the upstream Windows-MSVC tarball from GitHub
    releases (with SHA-256 verification) and extract to
    `%LOCALAPPDATA%/SMBWArchipelago/llvm/`. Coexists with any other LLVM
    the user has installed — never touches `C:\\Program Files\\LLVM\\`
    or modifies global PATH. The build step's PATH prepend is scoped to
    the build subprocess only.
  - Git submodule init: `git submodule update --init` for
    `vendor/Archipelago` and `switch-mod/{sys, lib/imgui}`.
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
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import local_appdata_root
from .child_env import child_environ
from .prereqs import (
    _prepend_path,
    _winget_ninja_paths,
    ensure_python3_shim,
    is_dev_clone,
    llvm_portable_root,
    repo_root,
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

    Non-Windows: winget doesn't exist and won't be invoked by any of
    the cross-platform installers. The wizard's prereq detectors set
    ``auto_installable=False`` for the winget-driven system tools on
    Linux, so the install walker shouldn't even ask — but if it does,
    return a clear "not applicable" result instead of a confusing
    "winget not found".
    """
    if sys.platform != "win32":
        msg = "winget preflight skipped (not applicable on non-Windows)"
        if on_line:
            on_line(msg)
        return InstallResult(True, 0, msg, msg)
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


# Common Linux/macOS system CA bundle locations. Probed in order; first
# existing file wins. Steam Deck / SteamOS hits the Arch path; most
# desktop distros hit one of these too. Used only as a fallback when
# certifi isn't installed and Python's default context can't find any
# trust store on its own (the SteamOS failure mode).
_SYSTEM_CA_BUNDLE_CANDIDATES: tuple[str, ...] = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Arch, SteamOS
    "/etc/pki/tls/certs/ca-bundle.crt",    # RHEL, Fedora, CentOS
    "/etc/ssl/ca-bundle.pem",              # openSUSE
    "/etc/ssl/cert.pem",                   # Alpine, macOS (post-12)
)


def _resolve_ca_bundle() -> str | None:
    """Return a path to a CA bundle, or None to let ssl use defaults.

    Resolution order:
      1. ``SSL_CERT_FILE`` env var (user override, always wins).
      2. ``certifi.where()`` if importable.
      3. First existing path from ``_SYSTEM_CA_BUNDLE_CANDIDATES``.
      4. ``None`` — caller should fall through to ``ssl.create_default_context()``.

    This exists because some Python installs (notably SteamOS's, where
    /usr is read-only and Python's default CA path doesn't map to the
    system bundle) ship with no working trust store, so every HTTPS
    call fails with ``CERTIFICATE_VERIFY_FAILED``. The user sees
    "no internet" even though they have it.
    """
    override = os.environ.get("SSL_CERT_FILE")
    if override and os.path.isfile(override):
        return override
    try:
        import certifi
        path = certifi.where()
        if os.path.isfile(path):
            return path
    except ImportError:
        pass
    for candidate in _SYSTEM_CA_BUNDLE_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


def _build_ssl_context() -> ssl.SSLContext:
    """SSL context for wizard HTTPS requests.

    Uses ``_resolve_ca_bundle()`` when the default trust store would
    otherwise be empty / unreachable, so users on systems with broken
    Python CA config (most notably SteamOS) don't get spurious
    "no internet" errors from the preflight or download steps.
    """
    cafile = _resolve_ca_bundle()
    if cafile is not None:
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def _is_ssl_cert_error(exc: BaseException) -> bool:
    """True if ``exc`` is (or wraps) an SSL certificate verification error.

    ``urlopen`` re-raises SSL errors wrapped inside ``URLError``
    (the SSL exception lands on ``URLError.reason``), so direct
    ``isinstance`` checks miss them.
    """
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, ssl.SSLCertVerificationError):
            return True
        reason = getattr(cur, "reason", None)
        if isinstance(reason, ssl.SSLCertVerificationError):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def check_internet(on_line: ProgressFn | None = None) -> InstallResult:
    """Single connectivity probe before bulk install.

    HEAD https://github.com — we don't care that GitHub itself is up,
    we care that *some* HTTPS host responds, because every auto-
    installer pulls from a https URL. One clear "no internet" error
    beats N timeouts deep inside per-tool installers.

    Distinguishes SSL certificate-verification failures from real
    connectivity failures: the user reaching us *can* be online but
    have a Python install that can't validate HTTPS (no certifi, no
    system CA bundle on the default search path — common on SteamOS).
    Misreporting that as "no internet" sends the user chasing a router
    bug they don't have.
    """
    msg_ok = "internet reachable"
    msg_fail = (
        "no internet connectivity (HEAD https://github.com failed). "
        "Connect to the internet and click Install all missing again, "
        "or switch to Manual mode."
    )
    msg_ssl = (
        "SSL certificate verification failed reaching https://github.com — "
        "you ARE online, but this Python can't validate HTTPS certs. "
        "Fix: `pip install --user certifi` and re-run, or set "
        "SSL_CERT_FILE to a CA bundle "
        "(SteamOS: /etc/ssl/certs/ca-certificates.crt)."
    )
    try:
        req = urllib.request.Request("https://github.com", method="HEAD")
        with urllib.request.urlopen(req, timeout=10, context=_build_ssl_context()) as resp:
            if 200 <= resp.status < 400:
                if on_line:
                    on_line(f"[net] {msg_ok} ({resp.status})")
                return InstallResult(True, 0, str(resp.status), msg_ok)
    except (urllib.error.URLError, OSError) as e:
        if _is_ssl_cert_error(e):
            if on_line:
                on_line(f"[net] {msg_ssl} ({e})")
            return InstallResult(False, 1, f"{type(e).__name__}: {e}", msg_ssl)
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
    # child_environ (not a raw os.environ copy) so a HOST tool we spawn
    # from inside Archipelago's Linux AppImage doesn't inherit the
    # bundle's LD_LIBRARY_PATH / PYTHONHOME. See child_env.py.
    child_env = dict(env) if env is not None else child_environ(cmd[0])
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


# winget exit code APPINSTALLER_CLI_ERROR_PACKAGE_ALREADY_INSTALLED.
# Surfaced as "Found an existing package already installed. Trying to
# upgrade the installed package... No available upgrade found." We treat
# this as install success: the package is on the system, which is what
# the install was meant to achieve. The caller's PATH-prepend / re-probe
# step still runs and will surface a clear error if the binary isn't at
# the expected canonical path.
_WINGET_PACKAGE_ALREADY_INSTALLED = 0x8A15002B  # 2316632107


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
    r = _stream_subprocess(
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
    if not r.ok and r.returncode == _WINGET_PACKAGE_ALREADY_INSTALLED:
        msg = (
            f"winget rc=0x{r.returncode:08X}: {package_id} already "
            f"installed (no upgrade available) — treating as success"
        )
        if on_line:
            on_line(f"[winget] {msg}")
        return InstallResult(True, 0, r.log, msg)
    return r


# ---------------------------------------------------------------------------
# Per-tool installers
# ---------------------------------------------------------------------------

def _manual_install_result(
    tool: str,
    package_hint: str,
    *,
    on_line: ProgressFn | None = None,
) -> InstallResult:
    """Surfaced when an installer is called on a platform it can't service.

    Non-Windows: the wizard's prereq detectors flip
    ``auto_installable=False`` for winget-driven tools so the install
    walker normally skips them. This is the safety net: if a caller
    invokes the installer directly, return a clear "install with your
    package manager" message instead of falling through to a winget
    invocation that would explode with ``FileNotFoundError``.
    """
    msg = (
        f"{tool} can't be auto-installed on this platform. "
        f"Install via your distro's package manager (e.g. {package_hint}) "
        f"and re-run the wizard."
    )
    if on_line:
        on_line(f"[install] {msg}")
    return InstallResult(False, 1, msg, msg)


def install_git(on_line: ProgressFn | None = None) -> InstallResult:
    """winget-install Git for Windows."""
    if sys.platform != "win32":
        return _manual_install_result(
            "git", "apt install git / dnf install git / pacman -S git",
            on_line=on_line)
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
    if sys.platform != "win32":
        return _manual_install_result(
            "CMake 3.16+",
            "apt install cmake / dnf install cmake / pacman -S cmake",
            on_line=on_line)
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
    if sys.platform != "win32":
        return _manual_install_result(
            "Ninja",
            "apt install ninja-build / dnf install ninja-build / pacman -S ninja",
            on_line=on_line)
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
    if sys.platform != "win32":
        return _manual_install_result(
            "Python 3.11+",
            "apt install python3.11 / dnf install python3.11 / pacman -S python",
            on_line=on_line)
    r = winget_install("Python.Python.3.11", on_line=on_line)
    if not r.ok:
        return r
    # Best-effort PATH prepend: winget lands py.exe under
    # %LOCALAPPDATA%\Programs\Python\Launcher\ and the interpreter at
    # %LOCALAPPDATA%\Programs\Python\Python311\python.exe. Drop the
    # `python3.exe` shim so LibHakkun's bare-`python3` shellouts resolve
    # to the real interpreter, not the Microsoft Store stub.
    localapp = os.environ.get("LOCALAPPDATA")
    if localapp:
        launcher = Path(localapp) / "Programs" / "Python" / "Launcher"
        if launcher.is_dir():
            _prepend_path(launcher)
            if on_line:
                on_line(f"[install] prepended {launcher} to PATH")
        python_exe = (Path(localapp) / "Programs" / "Python"
                      / "Python311" / "python.exe")
        if python_exe.is_file():
            ensure_python3_shim(python_exe)
            if on_line:
                on_line(f"[install] ensured python3.exe shim next to {python_exe}")
    return InstallResult(True, 0, r.log, "Python 3.11 installed")


# ---------------------------------------------------------------------------
# LLVM 19.1.7 — direct tar.xz extract to %LOCALAPPDATA%\SMBWArchipelago\llvm\
#
# LibHakkun's [switch-mod/sys/cmake/toolchain.cmake] hardcodes clang/clang++
# and the libc++ headers it fetches via setup_libcxx_prepackaged.py are
# ABI-pinned to LLVM 19. We pin a specific 19.1.x release for reproducibility.
# ---------------------------------------------------------------------------

LLVM_VERSION = "19.1.7"
LLVM_URL = (
    "https://github.com/llvm/llvm-project/releases/download/"
    f"llvmorg-{LLVM_VERSION}/clang+llvm-{LLVM_VERSION}-x86_64-pc-windows-msvc.tar.xz"
)
# Upstream-published SHA-256 of the tarball — protects against
# mid-download corruption and supply-chain tamper.
LLVM_SHA256 = "b4557b4f012161f56a2f5d9e877ab9635cafd7a08f7affe14829bd60c9d357f0"
LLVM_DOWNLOAD_BYTES = 845_236_708       # ~806 MB compressed
LLVM_UNPACKED_BYTES = 3_563_705_149     # ~3.32 GB on disk


def _download_with_progress(
    url: str,
    dest: Path,
    *,
    on_line: ProgressFn | None = None,
    expected_sha256: str = "",
    timeout: float = 600.0,
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
        with urllib.request.urlopen(req, timeout=timeout, context=_build_ssl_context()) as resp:
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


def _extract_tar_xz_strip_top(
    src: Path,
    dst: Path,
    *,
    on_line: ProgressFn | None = None,
) -> InstallResult:
    """Extract ``src`` (.tar.xz) into ``dst``, stripping the single
    top-level dir. LLVM's tarball lays out as
    ``clang+llvm-19.1.7-x86_64-pc-windows-msvc/...``; we land its
    contents directly under ``<dst>/`` so the result is
    ``<dst>/bin/clang.exe`` rather than ``<dst>/clang+llvm-.../bin/clang.exe``.
    """
    log_lines: list[str] = []

    def _emit(s: str) -> None:
        log_lines.append(s)
        if on_line:
            on_line(s)

    _emit(f"[extract] opening {src} (tar.xz)")
    dst.mkdir(parents=True, exist_ok=True)
    last_emit = time.monotonic()
    n = 0
    try:
        with tarfile.open(src, "r:xz") as tf:
            for member in tf:
                parts = member.name.split("/", 1)
                if len(parts) < 2 or not parts[1]:
                    continue  # skip the top-level dir entry itself
                stripped = parts[1]
                if any(seg in (".", "..") for seg in stripped.split("/")):
                    raise RuntimeError(
                        f"refusing to extract suspicious entry: {member.name!r}"
                    )
                target = dst / stripped
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if member.issym() or member.islnk():
                    # Windows MSVC tarball shouldn't contain these; if it
                    # does, skip quietly — extractfile() returns None for
                    # symlinks anyway.
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                f = tf.extractfile(member)
                if f is None:
                    continue
                with f, open(target, "wb") as out:
                    shutil.copyfileobj(f, out)
                try:
                    os.chmod(target, member.mode & 0o777)
                except OSError:
                    pass
                n += 1
                now = time.monotonic()
                if now - last_emit >= 1.0:
                    last_emit = now
                    _emit(f"[extract] {n} files written...")
    except (tarfile.TarError, OSError, RuntimeError) as e:
        msg = f"extract failed: {e}"
        _emit(msg)
        return InstallResult(False, 1, "\n".join(log_lines), msg)
    _emit(f"[extract] done ({n} files)")
    return InstallResult(True, 0, "\n".join(log_lines), str(dst))


def install_llvm19(on_line: ProgressFn | None = None) -> InstallResult:
    """Download LLVM 19.1.7 + extract to %LOCALAPPDATA%\\SMBWArchipelago\\llvm\\.

    Coexists with any other LLVM the user has installed — never touches
    `C:\\Program Files\\LLVM\\` or modifies global PATH. The build step's
    PATH prepend (build._compose_build_env) is scoped to the build
    subprocess only.

    Flow:
      1. Disk-space precheck against download + unpacked total.
      2. Skip download if a portable install already exists with a
         working `clang.exe`.
      3. Download to a temp file with SHA-256 verification.
      4. Extract to `<root>/llvm/`, stripping the archive's top-level
         `clang+llvm-19.1.7-.../` dir.
      5. The next `check_llvm19()` finds `bin/clang.exe` and flips the
         prereq row green.
    """
    def emit(msg: str) -> None:
        if on_line:
            on_line(msg)

    if sys.platform != "win32":
        msg = (
            "LLVM 19.1.x can't be auto-installed on this platform "
            "(the bundled tarball is the Windows-MSVC build). Install "
            "via your distro's package manager — e.g. `clang-19 lld-19` "
            "on Debian/Ubuntu via https://apt.llvm.org/, `clang` on "
            "Arch/Fedora — or download the upstream Linux 19.1.7 build "
            f"from {LLVM_URL.rsplit('/', 1)[0]}/, then re-run the wizard."
        )
        emit(f"[llvm] {msg}")
        return InstallResult(False, 1, msg, msg)

    dst = llvm_portable_root()
    clang = dst / "bin" / "clang.exe"
    if clang.is_file():
        emit(f"[llvm] already installed at {dst}; skipping download")
        return InstallResult(True, 0, str(dst), str(dst))

    try:
        # Need download + unpacked + ~10% headroom for tarfile streaming.
        need = int((LLVM_DOWNLOAD_BYTES + LLVM_UNPACKED_BYTES) * 1.10)
        _check_disk_space(dst, need)
    except InsufficientDiskError as e:
        emit(f"[llvm] {e}")
        return InstallResult(False, 1, str(e), str(e))

    with tempfile.TemporaryDirectory(prefix="smbwap-llvm-") as td:
        td_path = Path(td)
        tarball = td_path / f"clang+llvm-{LLVM_VERSION}-windows-msvc.tar.xz"
        emit(f"[llvm] downloading LLVM {LLVM_VERSION} (~{LLVM_DOWNLOAD_BYTES / (1024**2):.0f} MB)...")
        r = _download_with_progress(
            LLVM_URL, tarball,
            on_line=on_line,
            expected_sha256=LLVM_SHA256,
            timeout=600.0,
        )
        if not r.ok:
            return r
        emit(f"[llvm] extracting to {dst} (~{LLVM_UNPACKED_BYTES / (1024**3):.1f} GB unpacked)...")
        r = _extract_tar_xz_strip_top(tarball, dst, on_line=on_line)
        if not r.ok:
            return r

    if not clang.is_file():
        msg = (
            f"extraction reported success but {clang} is missing. The "
            f"tarball layout may have changed in a newer release."
        )
        emit(f"[llvm] {msg}")
        return InstallResult(False, 1, msg, msg)
    _prepend_path(clang.parent)
    emit(f"[llvm] ready: {clang} (PATH-prepended for this process)")
    return InstallResult(True, 0, str(dst), str(dst))


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
    """Initialize switch-mod's hakkun framework + vendored libs.

    Two paths:
      * **Dev clone**: ``git submodule update --init`` for
        ``switch-mod/sys`` (LibHakkun — load-bearing for the build) and
        ``switch-mod/lib/imgui`` (currently gated off, but cheap to
        keep ready for the debug-overlay flip-on).
      * **Packaged install**: the same sources ship inside the .apworld
        zip at ``smbwonder/_setup/switch-mod/`` thanks to
        ``scripts/install_apworld.py --bundle-mod``.  No git needed --
        the build phase calls ``bundled_switch_mod()`` to extract them
        on first use.  We just verify the bundle is reachable.
    """
    if not is_dev_clone():
        # build.py owns the extraction logic; importing it here is fine
        # (single direction; build never imports installers).
        from .build import bundled_switch_mod_available
        if bundled_switch_mod_available():
            msg = "satisfied by bundled sources inside the .apworld (extracted on first build)"
            if on_line:
                on_line(f"[install] {msg}")
            return InstallResult(True, 0, msg, msg)
        # Fall through to the git path so the user gets a clear "not a
        # git clone" error rather than a silent "ok" that fails later.

    # switch-mod is plain tracked subdirectory of this repo; only the
    # framework (sys = LibHakkun) and one nested third-party lib (imgui)
    # still need `git submodule update --init`. The exlaunch-era
    # submodules (NintendoSDK, sead) were retired during the hakkun
    # migration.
    return _git_submodule_update(
        "switch-mod/sys",
        "switch-mod/lib/imgui",
        on_line=on_line,
    )


# ---------------------------------------------------------------------------
# Archipelago pip deps
# ---------------------------------------------------------------------------

def _pip_install(
    py: str,
    args: list[str],
    *,
    on_line: ProgressFn | None = None,
    timeout: float = 120.0,
) -> InstallResult:
    """`<py> -m pip install --user <args>` with two documented retries.

    Distro-packaged interpreters (Arch, Debian 12+, Fedora 38+, and the
    `/usr/bin/python3` the wizard resolves when Archipelago's AppImage
    doesn't expose a usable `sys.executable`) mark themselves
    **externally managed** per PEP 668, and plain `pip install --user`
    refuses to run at all. That is what forced a user to install `lz4`
    system-wide by hand before the build could get past linking.

    Retry ladder, most-conservative first:
      1. `--user`                              — the normal case.
      2. `--user --break-system-packages`      — PEP 668 refusal. Still
         writes only to `~/.local/lib/...`; the distro's own
         site-packages is untouched, so the flag's scary name overstates
         what happens here.
      3. no `--user`                           — some venvs disable user
         installs outright ("Can not perform a '--user' install"); inside
         a venv the plain form is already isolated.
    """
    base = [py, "-m", "pip", "install", "--disable-pip-version-check"]
    result = _stream_subprocess(
        [*base, "--user", *args], on_line=on_line, timeout=timeout)
    if result.ok:
        return result

    log = result.log.lower()
    if "externally-managed-environment" in log or "externally managed" in log:
        if on_line:
            on_line(
                "[pip] this Python is externally managed (PEP 668); "
                "retrying with --break-system-packages, which still "
                "installs into your user site (~/.local) only")
        result = _stream_subprocess(
            [*base, "--user", "--break-system-packages", *args],
            on_line=on_line, timeout=timeout)
        if result.ok:
            return result
        log = result.log.lower()

    if "can not perform a '--user' install" in log or \
            "user site-packages are not visible" in log:
        if on_line:
            on_line("[pip] --user rejected (virtualenv); retrying without it")
        result = _stream_subprocess([*base, *args], on_line=on_line,
                                    timeout=timeout)
    return result


def install_lz4(on_line: ProgressFn | None = None) -> InstallResult:
    """`pip install --user lz4` into the resolved Python.

    LibHakkun's deploy.cmake calls `python deploy.py` which imports
    lz4.block. The resolved Python (prepended to PATH by _compose_build_env)
    must have lz4 installed for the cmake build to succeed.
    """
    from .prereqs import lz4_marker_path

    py = resolved_python_bin() or sys.executable
    result = _pip_install(py, ["lz4"], on_line=on_line)
    if result.ok:
        marker = lz4_marker_path()
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("ok\n", encoding="utf-8")
        except OSError as e:
            if on_line:
                on_line(f"[lz4] could not write marker {marker}: {e}")
    return result


def install_pyelftools(on_line: ProgressFn | None = None) -> InstallResult:
    """`pip install --user pyelftools` into the resolved Python.

    LibHakkun's SwitchTools.cmake runs elf2nso.py which imports elftools.
    The resolved Python (prepended to PATH by _compose_build_env) must have
    pyelftools installed for the cmake build to succeed.
    """
    from .prereqs import pyelftools_marker_path

    py = resolved_python_bin() or sys.executable
    result = _pip_install(py, ["pyelftools"], on_line=on_line)
    if result.ok:
        marker = pyelftools_marker_path()
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("ok\n", encoding="utf-8")
        except OSError as e:
            if on_line:
                on_line(f"[pyelftools] could not write marker {marker}: {e}")
    return result


def install_archipelago_deps(on_line: ProgressFn | None = None) -> InstallResult:
    """`pip install -r vendor/Archipelago/requirements.txt` into the
    resolved Python 3.11+.

    Goes through :func:`_pip_install`, so it prefers a user-site install
    and handles PEP 668 / venv refusals the same way the small build-dep
    installs do.

    Uses `prereqs.resolved_python_bin()` so the install lands in the same
    interpreter the wizard's import-probe will check on Re-check --
    otherwise a user with two Pythons could install into one and probe
    the other.

    Short-circuits when running from a packaged install where the
    surrounding Archipelago already supplies these deps -- there is no
    in-tree ``requirements.txt`` to install from and shelling out to
    pip would either pollute the user's Python or no-op against an
    already-satisfied env.
    """
    from .prereqs import archipelago_importable
    if not is_dev_clone() and archipelago_importable():
        msg = "satisfied by surrounding Archipelago install (no requirements.txt to install from)"
        if on_line:
            on_line(f"[install] {msg}")
        return InstallResult(True, 0, msg, msg)

    py = resolved_python_bin() or sys.executable
    req_path = repo_root() / "vendor" / "Archipelago" / "requirements.txt"
    if not req_path.is_file():
        msg = (
            f"requirements.txt not found at {req_path} -- initialize the "
            "Archipelago submodule first, or run the wizard from inside "
            "an existing Archipelago install"
        )
        if on_line:
            on_line(f"[install] {msg}")
        return InstallResult(False, 1, msg, msg)
    return _pip_install(py, ["-r", str(req_path)], on_line=on_line,
                        timeout=600.0)


# ---------------------------------------------------------------------------
# Registry + ordering
# ---------------------------------------------------------------------------

INSTALLERS: dict[str, Callable[[ProgressFn | None], InstallResult]] = {
    "git": install_git,
    "cmake": install_cmake,
    "ninja": install_ninja,
    "python311": install_python311,
    "llvm19": install_llvm19,
    "archipelago_submodule": install_archipelago_submodule,
    "switch_mod_submodule": install_switch_mod_submodule,
    "archipelago_deps": install_archipelago_deps,
    "lz4": install_lz4,
    "pyelftools": install_pyelftools,
}

# Order the wizard's "Install all missing" walker uses. Dependencies feed
# downstream:
#   git → submodules (need git to clone)
#   submodules → archipelago_deps (need vendor/Archipelago/requirements.txt)
# LLVM is the biggest download (~806 MB), so it goes early — fail-fast on
# disk / network problems while user attention is still fresh.
INSTALL_ORDER: tuple[str, ...] = (
    "git",
    "cmake",
    "ninja",
    "python311",
    "llvm19",
    "archipelago_submodule",
    "switch_mod_submodule",
    "archipelago_deps",
    "lz4",
    "pyelftools",
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
