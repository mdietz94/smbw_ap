"""Tests for the installer registry and the install_many orchestrator.

Per-installer body is heavy on subprocess; we test the orchestration
contract (ordering, short-circuit, registry coverage) directly and use
monkeypatching for the subprocess primitives.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from apworld.smbw_archipelago._setup import installers as I


def test_install_order_includes_every_registered() -> None:
    """INSTALL_ORDER and INSTALLERS must stay in lockstep — every key in
    one must appear in the other, otherwise install_many silently
    skips entries."""
    assert set(I.INSTALL_ORDER) == set(I.INSTALLERS.keys())


def test_install_many_runs_in_install_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """install_many should iterate in INSTALL_ORDER, not in the caller's
    given order — dependencies feed downstream."""
    calls: list[str] = []

    def make(key: str) -> Any:
        def installer(_on_line: Any = None) -> I.InstallResult:
            calls.append(key)
            return I.InstallResult(True, 0, "", "")
        return installer

    fake = {k: make(k) for k in I.INSTALL_ORDER}
    monkeypatch.setattr(I, "INSTALLERS", fake)

    # Pass keys out of order; install_many must reorder.
    installed, failed, results = I.install_many(
        ["archipelago_deps", "git", "cmake"],
    )
    assert installed == ["git", "cmake", "archipelago_deps"]
    assert failed == []
    assert calls == ["git", "cmake", "archipelago_deps"]


def test_install_many_stops_on_first_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def make(key: str, ok: bool) -> Any:
        def installer(_on_line: Any = None) -> I.InstallResult:
            calls.append(key)
            return I.InstallResult(ok, 0 if ok else 1, "", "")
        return installer

    fake = {
        "git": make("git", True),
        "cmake": make("cmake", False),
        "ninja": make("ninja", True),  # must not run
        "python311": make("python311", True),  # must not run
        "llvm19": make("llvm19", True),
        "archipelago_submodule": make("archipelago_submodule", True),
        "switch_mod_submodule": make("switch_mod_submodule", True),
        "archipelago_deps": make("archipelago_deps", True),
    }
    monkeypatch.setattr(I, "INSTALLERS", fake)

    installed, failed, results = I.install_many(list(I.INSTALL_ORDER))
    assert installed == ["git"]
    assert failed == ["cmake"]
    # Stopped after cmake; later keys not attempted.
    assert "ninja" not in calls


def test_install_many_skips_unknown_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting a key not in INSTALL_ORDER should be a no-op, not a crash."""
    installed, failed, results = I.install_many(["this-is-not-a-real-installer"])
    assert installed == []
    assert failed == []


def test_disk_space_precheck_refuses_when_low(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`_check_disk_space` walks up to the nearest existing parent and
    refuses with a precise message when free space is below need_bytes."""
    class FakeUsage:
        free = 100 * 1024 * 1024   # 100 MB

    monkeypatch.setattr(I.shutil, "disk_usage", lambda _p: FakeUsage())
    # Need 1 GB but only 100 MB free.
    with pytest.raises(I.InsufficientDiskError, match="not enough free space"):
        I._check_disk_space(tmp_path / "subdir", need_bytes=1 * 1024 ** 3)


def test_disk_space_precheck_allows_when_enough(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeUsage:
        free = 10 * 1024 ** 3   # 10 GB

    monkeypatch.setattr(I.shutil, "disk_usage", lambda _p: FakeUsage())
    # Need 1 GB and we have 10 GB.
    I._check_disk_space(tmp_path, need_bytes=1 * 1024 ** 3)
    # No exception → pass.


def test_check_winget_missing_returns_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The winget preflight on Windows surfaces a clear "install App
    Installer" error when winget isn't on PATH. Linux takes a different
    branch (covered by `test_check_winget_on_linux_is_not_applicable`)."""
    monkeypatch.setattr(I.sys, "platform", "win32")
    monkeypatch.setattr(I.shutil, "which", lambda name: None if name == "winget" else "/usr/bin/" + name)
    lines: list[str] = []
    r = I.check_winget(lines.append)
    assert r.ok is False
    assert "winget not found" in r.detail
    assert any("winget not found" in ln for ln in lines)


def test_check_winget_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(I.sys, "platform", "win32")
    monkeypatch.setattr(I.shutil, "which", lambda _n: "/usr/local/bin/winget")
    r = I.check_winget(lambda _line: None)
    assert r.ok is True
    assert "winget" in r.detail


def test_check_internet_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_open(_req, timeout=10, context=None):  # noqa: ARG001
        return FakeResp()

    monkeypatch.setattr(I.urllib.request, "urlopen", fake_open)
    r = I.check_internet(lambda _line: None)
    assert r.ok is True


def test_check_internet_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_open(_req, timeout=10, context=None):  # noqa: ARG001
        raise OSError("no network")

    monkeypatch.setattr(I.urllib.request, "urlopen", fake_open)
    r = I.check_internet(lambda _line: None)
    assert r.ok is False
    assert "no internet" in r.detail.lower() or "no network" in r.log.lower()


def test_check_internet_ssl_cert_failure_is_classified_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SSL cert-verification failure must NOT be reported as
    "no internet". The SteamOS failure mode is exactly this: HTTPS
    is reachable, but Python can't validate the cert. Misreporting
    sends users chasing a router bug they don't have."""
    import ssl
    import urllib.error

    ssl_err = ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
    )

    def fake_open(_req, timeout=10, context=None):  # noqa: ARG001
        raise urllib.error.URLError(ssl_err)

    monkeypatch.setattr(I.urllib.request, "urlopen", fake_open)
    lines: list[str] = []
    r = I.check_internet(lines.append)
    assert r.ok is False
    assert "ssl" in r.detail.lower()
    assert "certifi" in r.detail.lower() or "ssl_cert_file" in r.detail.lower()
    assert "no internet" not in r.detail.lower()
    assert any("ssl" in ln.lower() for ln in lines)


def test_is_ssl_cert_error_detects_direct_and_wrapped() -> None:
    import ssl
    import urllib.error

    direct = ssl.SSLCertVerificationError(1, "x")
    wrapped = urllib.error.URLError(direct)
    other = OSError("unrelated")

    assert I._is_ssl_cert_error(direct) is True
    assert I._is_ssl_cert_error(wrapped) is True
    assert I._is_ssl_cert_error(other) is False


def test_resolve_ca_bundle_prefers_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SSL_CERT_FILE env var, when it points to an existing file, must
    take precedence over certifi / system bundle probing — users
    explicitly setting it are debugging or pinning a custom bundle."""
    custom = tmp_path / "custom-ca.pem"
    custom.write_text("# pretend cert\n")
    monkeypatch.setenv("SSL_CERT_FILE", str(custom))
    assert I._resolve_ca_bundle() == str(custom)


def test_resolve_ca_bundle_falls_back_to_system_candidate_on_linux(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When certifi isn't importable and SSL_CERT_FILE isn't set, the
    first existing path from _SYSTEM_CA_BUNDLE_CANDIDATES wins. This
    is the SteamOS auto-heal path."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    fake_bundle = tmp_path / "ca-certificates.crt"
    fake_bundle.write_text("# pretend cert\n")

    import builtins
    real_import = builtins.__import__

    def no_certifi(name, *a, **kw):
        if name == "certifi":
            raise ImportError("no certifi")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_certifi)
    monkeypatch.setattr(I, "_SYSTEM_CA_BUNDLE_CANDIDATES", (str(fake_bundle),))
    assert I._resolve_ca_bundle() == str(fake_bundle)


def test_resolve_ca_bundle_returns_none_when_nothing_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No env, no certifi, no system bundle → return None so the caller
    falls through to ssl.create_default_context() (Python's native
    default — what we had before this fix)."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    import builtins
    real_import = builtins.__import__

    def no_certifi(name, *a, **kw):
        if name == "certifi":
            raise ImportError("no certifi")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_certifi)
    monkeypatch.setattr(
        I, "_SYSTEM_CA_BUNDLE_CANDIDATES", (str(tmp_path / "nope.crt"),)
    )
    assert I._resolve_ca_bundle() is None


def test_winget_install_without_winget_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(I.shutil, "which", lambda _n: None)
    r = I.winget_install("Whatever.Package")
    assert r.ok is False
    assert "winget not on PATH" in r.log


def test_stream_subprocess_propagates_returncode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lightweight sanity check that _stream_subprocess collects stdout
    and surfaces the underlying returncode through InstallResult."""
    class FakeProc:
        pid = 123
        stdout = io.StringIO("hello\nworld\n")
        def wait(self, timeout=None):  # noqa: ARG002
            return 0

    monkeypatch.setattr(I.subprocess, "Popen", lambda *_a, **_k: FakeProc())
    lines: list[str] = []
    r = I._stream_subprocess(["fake-tool", "--version"], on_line=lines.append)
    assert r.ok is True
    assert r.returncode == 0
    assert "hello" in r.log
    assert "world" in r.log


def test_stream_subprocess_spawn_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_popen(*_a, **_k):
        raise FileNotFoundError("no such tool")

    monkeypatch.setattr(I.subprocess, "Popen", fake_popen)
    r = I._stream_subprocess(["does-not-exist"])
    assert r.ok is False
    assert r.returncode == 127
    assert "failed to spawn" in r.detail


def test_git_submodule_update_refuses_when_not_a_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Running the wizard from a release / pip install (no ``.git``) must
    NOT shell out to git -- the user would see a cryptic
    ``fatal: not a git repository`` instead of an actionable error."""
    monkeypatch.setattr(I, "repo_root", lambda: tmp_path)   # no .git
    spawned: list[list[str]] = []

    def fake_stream(cmd: list[str], **_kw: Any) -> I.InstallResult:
        spawned.append(cmd)
        return I.InstallResult(True, 0, "")

    monkeypatch.setattr(I, "_stream_subprocess", fake_stream)
    lines: list[str] = []
    r = I._git_submodule_update("vendor/Archipelago", on_line=lines.append)
    assert r.ok is False
    assert "not a git clone" in r.detail
    assert spawned == []   # never reached git
    assert any("not a git clone" in ln for ln in lines)


def test_git_submodule_update_runs_when_in_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Counterpart: with a ``.git`` directory present, the installer
    proceeds to actually invoke git."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(I, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(I.shutil, "which", lambda _n: "/usr/bin/git")
    spawned: list[list[str]] = []

    def fake_stream(cmd: list[str], **_kw: Any) -> I.InstallResult:
        spawned.append(cmd)
        return I.InstallResult(True, 0, "")

    monkeypatch.setattr(I, "_stream_subprocess", fake_stream)
    r = I._git_submodule_update("vendor/Archipelago")
    assert r.ok is True
    assert len(spawned) == 1
    assert "submodule" in spawned[0]


def test_install_switch_mod_submodule_short_circuits_with_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Packaged install + bundle available: no git involvement, just
    report ok and let the build phase extract on demand."""
    monkeypatch.setattr(I, "is_dev_clone", lambda repo=None: False)
    from apworld.smbw_archipelago._setup import build as B
    monkeypatch.setattr(B, "bundled_switch_mod_available", lambda: True)

    spawned: list[list[str]] = []
    monkeypatch.setattr(
        I, "_stream_subprocess",
        lambda cmd, **_k: spawned.append(cmd) or I.InstallResult(True, 0, ""))

    lines: list[str] = []
    r = I.install_switch_mod_submodule(lines.append)
    assert r.ok is True
    assert spawned == []   # never reached git
    assert any("bundled" in ln for ln in lines)


def test_install_switch_mod_submodule_falls_through_to_git_without_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Packaged install with NO bundled sources is a real failure --
    the installer should fall through and surface the dev-clone error
    rather than silently passing."""
    # is_dev_clone accepts an optional repo arg, so the patched stub
    # has to accept it too.
    monkeypatch.setattr(I, "is_dev_clone", lambda repo=None: False)
    monkeypatch.setattr(I, "repo_root", lambda: tmp_path)   # no .git
    from apworld.smbw_archipelago._setup import build as B
    monkeypatch.setattr(B, "bundled_switch_mod_available", lambda: False)

    r = I.install_switch_mod_submodule()
    assert r.ok is False
    assert "not a git clone" in r.detail


def test_install_archipelago_deps_short_circuits_on_packaged_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Packaged install + AP importable: deps are satisfied by the
    surrounding Archipelago; the wizard must not try to pip install
    from a `requirements.txt` that doesn't exist in this tree."""
    monkeypatch.setattr(I, "is_dev_clone", lambda repo=None: False)
    from apworld.smbw_archipelago._setup import prereqs as P
    monkeypatch.setattr(P, "archipelago_importable", lambda: True)

    spawned: list[list[str]] = []
    monkeypatch.setattr(
        I, "_stream_subprocess",
        lambda cmd, **_k: spawned.append(cmd) or I.InstallResult(True, 0, ""))

    lines: list[str] = []
    r = I.install_archipelago_deps(lines.append)
    assert r.ok is True
    assert spawned == []   # never reached pip
    assert any("surrounding" in ln.lower() or "satisfied" in ln.lower() for ln in lines)


def test_install_archipelago_deps_pip_installs_in_dev_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Dev clone with a real requirements.txt: pip install runs."""
    monkeypatch.setattr(I, "is_dev_clone", lambda repo=None: True)
    monkeypatch.setattr(I, "repo_root", lambda: tmp_path)
    req = tmp_path / "vendor" / "Archipelago" / "requirements.txt"
    req.parent.mkdir(parents=True)
    req.write_text("websockets\n", encoding="utf-8")
    monkeypatch.setattr(I, "resolved_python_bin", lambda: "/usr/bin/python3")

    spawned: list[list[str]] = []
    monkeypatch.setattr(
        I, "_stream_subprocess",
        lambda cmd, **_k: spawned.append(cmd) or I.InstallResult(True, 0, ""))

    r = I.install_archipelago_deps()
    assert r.ok is True
    assert len(spawned) == 1
    assert "pip" in spawned[0]
    assert "install" in spawned[0]


def test_install_llvm19_short_circuits_when_already_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """If `bin/clang.exe` already exists under the portable root, the
    installer should skip the ~806 MB download entirely."""
    root = tmp_path / "llvm"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "clang.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(I, "llvm_portable_root", lambda: root)
    monkeypatch.setattr(I.sys, "platform", "win32")

    # Any download attempt would trip this — the test catches a regression
    # to non-idempotent behavior.
    def boom(*_a, **_kw):
        raise AssertionError("download must not run when already installed")
    monkeypatch.setattr(I, "_download_with_progress", boom)

    lines: list[str] = []
    r = I.install_llvm19(lines.append)
    assert r.ok is True
    assert any("already installed" in ln for ln in lines)


def test_install_llvm19_non_windows_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pinned tarball is the windows-msvc build; refuse on POSIX
    rather than downloading something that won't run."""
    monkeypatch.setattr(I.sys, "platform", "linux")
    r = I.install_llvm19()
    assert r.ok is False
    assert "windows" in r.detail.lower()


def test_install_switch_mod_submodule_targets_sys_and_imgui(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The post-migration submodule init must target switch-mod/sys
    (LibHakkun) + switch-mod/lib/imgui only — NOT the exlaunch-era
    NintendoSDK or sead, which were removed from .gitmodules."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(I, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(I.shutil, "which", lambda _n: "/usr/bin/git")
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        I, "_stream_subprocess",
        lambda cmd, **_k: spawned.append(cmd) or I.InstallResult(True, 0, ""))

    r = I.install_switch_mod_submodule()
    assert r.ok is True
    assert len(spawned) == 1
    cmd = spawned[0]
    assert "switch-mod/sys" in cmd
    assert "switch-mod/lib/imgui" in cmd
    assert "switch-mod/lib/NintendoSDK" not in cmd
    assert "switch-mod/lib/sead" not in cmd


def test_no_retired_exlaunch_installers_remain() -> None:
    """Hakkun migration retired these keys; guard against re-introduction."""
    for retired in ("devkitpro", "switch_dev"):
        assert retired not in I.INSTALLERS
        assert retired not in I.INSTALL_ORDER


# ---------------------------------------------------------------------------
# install_lz4
# ---------------------------------------------------------------------------

def test_install_lz4_runs_pip_and_writes_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Successful pip install: command contains 'lz4' and marker is written."""
    marker = tmp_path / "lz4.ok"
    from apworld.smbw_archipelago._setup import prereqs as P
    monkeypatch.setattr(P, "lz4_marker_path", lambda: marker)
    monkeypatch.setattr(I, "resolved_python_bin", lambda: "/usr/bin/python3")

    spawned: list[list[str]] = []
    monkeypatch.setattr(
        I, "_stream_subprocess",
        lambda cmd, **_k: spawned.append(cmd) or I.InstallResult(True, 0, ""))

    r = I.install_lz4()
    assert r.ok is True
    assert len(spawned) == 1
    assert "lz4" in spawned[0]
    assert "pip" in spawned[0]
    assert "--user" in spawned[0]
    assert marker.is_file()


def test_install_lz4_pip_failure_no_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """pip install fails: marker must NOT be written and ok=False returned."""
    marker = tmp_path / "lz4.ok"
    from apworld.smbw_archipelago._setup import prereqs as P
    monkeypatch.setattr(P, "lz4_marker_path", lambda: marker)
    monkeypatch.setattr(I, "resolved_python_bin", lambda: "/usr/bin/python3")

    monkeypatch.setattr(
        I, "_stream_subprocess",
        lambda cmd, **_k: I.InstallResult(False, 1, "pip failed"))

    r = I.install_lz4()
    assert r.ok is False
    assert not marker.exists()


# ---------------------------------------------------------------------------
# install_pyelftools
# ---------------------------------------------------------------------------

def test_install_pyelftools_runs_pip_and_writes_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Successful pip install: command contains 'pyelftools' and marker is written."""
    marker = tmp_path / "pyelftools.ok"
    from apworld.smbw_archipelago._setup import prereqs as P
    monkeypatch.setattr(P, "pyelftools_marker_path", lambda: marker)
    monkeypatch.setattr(I, "resolved_python_bin", lambda: "/usr/bin/python3")

    spawned: list[list[str]] = []
    monkeypatch.setattr(
        I, "_stream_subprocess",
        lambda cmd, **_k: spawned.append(cmd) or I.InstallResult(True, 0, ""))

    r = I.install_pyelftools()
    assert r.ok is True
    assert len(spawned) == 1
    assert "pyelftools" in spawned[0]
    assert "pip" in spawned[0]
    assert "--user" in spawned[0]
    assert marker.is_file()


def test_install_pyelftools_pip_failure_no_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """pip install fails: marker must NOT be written and ok=False returned."""
    marker = tmp_path / "pyelftools.ok"
    from apworld.smbw_archipelago._setup import prereqs as P
    monkeypatch.setattr(P, "pyelftools_marker_path", lambda: marker)
    monkeypatch.setattr(I, "resolved_python_bin", lambda: "/usr/bin/python3")

    monkeypatch.setattr(
        I, "_stream_subprocess",
        lambda cmd, **_k: I.InstallResult(False, 1, "pip failed"))

    r = I.install_pyelftools()
    assert r.ok is False
    assert not marker.exists()


# ---------------------------------------------------------------------------
# Linux branch — system-tool installers must NOT shell out to winget,
# and check_winget must report "not applicable" rather than failing.
# ---------------------------------------------------------------------------


@pytest.fixture
def force_linux(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(I.sys, "platform", "linux")
    return monkeypatch


def test_check_winget_on_linux_is_not_applicable(force_linux) -> None:
    """winget preflight should return ok=True with a clear message on
    Linux — the install walker shouldn't even ask, but a defensive call
    must not surface a confusing "winget not found" error."""
    r = I.check_winget(lambda _line: None)
    assert r.ok is True
    assert "not applicable" in r.detail.lower() or "skipped" in r.detail.lower()


def test_install_git_on_linux_returns_manual_install_result(
    force_linux, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """install_git must never invoke winget on Linux."""
    def boom(*_a, **_kw):
        raise AssertionError("winget_install must not be called on Linux")
    monkeypatch.setattr(I, "winget_install", boom)
    r = I.install_git()
    assert r.ok is False
    assert "package manager" in r.detail.lower()


def test_install_cmake_on_linux_returns_manual_install_result(
    force_linux, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a, **_kw):
        raise AssertionError("winget_install must not be called on Linux")
    monkeypatch.setattr(I, "winget_install", boom)
    r = I.install_cmake()
    assert r.ok is False
    assert "package manager" in r.detail.lower()


def test_install_ninja_on_linux_returns_manual_install_result(
    force_linux, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a, **_kw):
        raise AssertionError("winget_install must not be called on Linux")
    monkeypatch.setattr(I, "winget_install", boom)
    r = I.install_ninja()
    assert r.ok is False
    assert "package manager" in r.detail.lower()


def test_install_python311_on_linux_returns_manual_install_result(
    force_linux, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a, **_kw):
        raise AssertionError("winget_install must not be called on Linux")
    monkeypatch.setattr(I, "winget_install", boom)
    r = I.install_python311()
    assert r.ok is False
    assert "package manager" in r.detail.lower()


def test_install_llvm19_on_linux_points_at_distro_install(
    force_linux,
) -> None:
    """install_llvm19 was already gated on Windows-only; the message
    should explicitly point at the distro package / upstream Linux
    tarball, not the bare "Windows-only" string."""
    r = I.install_llvm19()
    assert r.ok is False
    low = r.detail.lower()
    assert "package manager" in low or "apt" in low or "linux" in low
