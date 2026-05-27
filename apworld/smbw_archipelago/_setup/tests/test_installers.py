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
        "devkitpro": make("devkitpro", True),
        "switch_dev": make("switch_dev", True),
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
    monkeypatch.setattr(I.shutil, "which", lambda name: None if name == "winget" else "/usr/bin/" + name)
    lines: list[str] = []
    r = I.check_winget(lines.append)
    assert r.ok is False
    assert "winget not found" in r.detail
    assert any("winget not found" in ln for ln in lines)


def test_check_winget_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(I.shutil, "which", lambda _n: "/usr/local/bin/winget")
    r = I.check_winget(lambda _line: None)
    assert r.ok is True
    assert "winget" in r.detail


def test_check_internet_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_open(_req, timeout=10):  # noqa: ARG001
        return FakeResp()

    monkeypatch.setattr(I.urllib.request, "urlopen", fake_open)
    r = I.check_internet(lambda _line: None)
    assert r.ok is True


def test_check_internet_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_open(_req, timeout=10):  # noqa: ARG001
        raise OSError("no network")

    monkeypatch.setattr(I.urllib.request, "urlopen", fake_open)
    r = I.check_internet(lambda _line: None)
    assert r.ok is False
    assert "no internet" in r.detail.lower() or "no network" in r.log.lower()


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
