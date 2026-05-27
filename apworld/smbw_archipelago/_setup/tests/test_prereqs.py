"""Tests for the prereq detectors.

Each detector shells out via `prereqs._run`. We monkeypatch that one
function to script the return value, so tests don't depend on the host
machine having (or lacking) the actual tools.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from apworld.smbw_archipelago._setup import prereqs as P


@pytest.fixture
def patch_run(monkeypatch: pytest.MonkeyPatch):
    """Patch prereqs._run / _safe_run with a scriptable fake.

    Yields the responses dict; mutate it before each detector call.
    Tuples of (returncode, stdout, stderr) keyed by argv[0]. A None
    entry means "raise FileNotFoundError" (simulates missing executable).
    """
    responses: dict[str, Any] = {}

    def fake_safe_run(cmd: list[str]) -> tuple[int, str, str] | None:
        key = cmd[0]
        if key not in responses:
            return None
        val = responses[key]
        if val is None:
            return None
        return val

    monkeypatch.setattr(P, "_safe_run", fake_safe_run)
    return responses


def test_check_git_success(patch_run: dict[str, Any]) -> None:
    patch_run["git"] = (0, "git version 2.43.0", "")
    r = P.check_git()
    assert r.ok is True
    assert "git version" in r.detail


def test_check_git_missing(patch_run: dict[str, Any]) -> None:
    patch_run["git"] = None
    r = P.check_git()
    assert r.ok is False
    assert "not found" in r.detail
    assert r.auto_installable is True


def test_check_cmake_rejects_too_old(patch_run: dict[str, Any]) -> None:
    patch_run["cmake"] = (0, "cmake version 3.10.0\n", "")
    # Disable default-path probe in test mode.
    P._CMAKE_DEFAULT_PATHS = ()  # type: ignore[assignment]
    r = P.check_cmake()
    assert r.ok is False
    assert "too old" in r.detail or "not found" in r.detail


def test_check_cmake_accepts_recent(patch_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    patch_run["cmake"] = (0, "cmake version 3.30.5\n", "")
    P._CMAKE_DEFAULT_PATHS = ()  # type: ignore[assignment]
    # Pretend `which cmake` returns a non-msys2 path so the bare-name
    # fallback isn't rejected.
    monkeypatch.setattr(P.shutil, "which", lambda _x: "C:/Program Files/CMake/bin/cmake.exe")
    r = P.check_cmake()
    assert r.ok is True
    assert "3.30.5" in r.detail


def test_check_cmake_rejects_msys2_bare_name(patch_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """devkitPro's MSYS2 cmake mangles drive-letter paths. Even when
    version-good, the bare-name PATH fallback must reject it."""
    patch_run["cmake"] = (0, "cmake version 3.30.5\n", "")
    P._CMAKE_DEFAULT_PATHS = ()  # type: ignore[assignment]
    monkeypatch.setattr(P.shutil, "which", lambda _x: "C:/devkitPro/msys2/usr/bin/cmake.exe")
    r = P.check_cmake()
    assert r.ok is False
    assert "msys2" in r.detail.lower()


def test_check_ninja_success(patch_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    patch_run["ninja"] = (0, "1.11.1\n", "")
    monkeypatch.setattr(P, "_winget_ninja_paths", lambda: [])
    monkeypatch.setattr(P.shutil, "which", lambda _x: "C:/some/path/ninja.exe")
    r = P.check_ninja()
    assert r.ok is True
    assert "1.11.1" in r.detail


def test_check_python311_uses_sys_executable(monkeypatch: pytest.MonkeyPatch, patch_run: dict[str, Any]) -> None:
    """When the wizard runs under 3.11+, sys.executable is the natural
    first probe and should win."""
    # Pretend sys.executable returns a 3.12.0 from its --version call.
    patch_run[sys.executable] = (0, "Python 3.12.0\n", "")
    # And `-c "import sys; print(sys.executable)"` returns the same path.
    monkeypatch.setattr(P.Path, "is_file", lambda self: True)
    r = P.check_python311()
    assert r.ok is True
    assert "3.12.0" in r.detail


def test_check_devkitpro_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEVKITPRO", raising=False)
    monkeypatch.setattr(P, "_devkitpro_default_root", lambda: None)
    r = P.check_devkitpro()
    assert r.ok is False
    assert "not found" in r.detail


def test_check_devkitpro_finds_via_env(monkeypatch: pytest.MonkeyPatch,
                                        tmp_path: Path,
                                        patch_run: dict[str, Any]) -> None:
    """Synthesize a devkitPro tree and verify the detector picks it up."""
    root = tmp_path / "devkitPro"
    gcc_path = root / "devkitA64" / "bin" / "aarch64-none-elf-gcc.exe"
    gcc_path.parent.mkdir(parents=True)
    gcc_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("DEVKITPRO", str(root))
    patch_run[str(gcc_path)] = (0, "gcc (devkitA64) 14.2.0\n", "")
    monkeypatch.setattr(P, "_devkitpro_default_root", lambda: None)
    r = P.check_devkitpro()
    assert r.ok is True
    assert "devkitA64" in r.detail or "gcc" in r.detail.lower()


def test_check_archipelago_submodule_present(monkeypatch: pytest.MonkeyPatch,
                                              tmp_path: Path) -> None:
    """Verify the file-probe-based check fires correctly."""
    repo = tmp_path / "fake-repo"
    (repo / "vendor" / "Archipelago").mkdir(parents=True)
    (repo / "vendor" / "Archipelago" / "CommonClient.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(P, "repo_root", lambda: repo)
    r = P.check_archipelago_submodule()
    assert r.ok is True


def test_check_archipelago_submodule_missing(monkeypatch: pytest.MonkeyPatch,
                                              tmp_path: Path) -> None:
    monkeypatch.setattr(P, "repo_root", lambda: tmp_path)
    # No .git, but ALSO force AP to look unimportable so we don't trip
    # the "satisfied by surrounding install" short-circuit.
    monkeypatch.setattr(P, "archipelago_importable", lambda: False)
    r = P.check_archipelago_submodule()
    assert r.ok is False
    assert "missing" in r.detail


def test_check_archipelago_submodule_short_circuits_on_ap_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When AP is importable AND we're not in a dev clone (no ``.git``),
    treat the submodule as satisfied -- a user running the wizard from
    a packaged apworld install doesn't need to clone anything."""
    monkeypatch.setattr(P, "repo_root", lambda: tmp_path)   # no .git
    monkeypatch.setattr(P, "archipelago_importable", lambda: True)
    r = P.check_archipelago_submodule()
    assert r.ok is True
    assert "surrounding" in r.detail.lower() or "satisfied" in r.detail.lower()


def test_check_archipelago_submodule_does_not_short_circuit_in_dev_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A dev clone with the submodule un-initialized must still report
    failure even if AP happens to be importable (e.g. from a separate
    pip install) -- the wizard needs the in-tree vendor/Archipelago for
    the junction phase."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(P, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(P, "archipelago_importable", lambda: True)
    r = P.check_archipelago_submodule()
    assert r.ok is False
    assert "missing" in r.detail


def test_is_dev_clone_detects_git_directory(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert P.is_dev_clone(tmp_path) is True


def test_is_dev_clone_detects_git_file_for_worktree(tmp_path: Path) -> None:
    """``git worktree`` checkouts have ``.git`` as a *file* containing
    ``gitdir: ...``, not a directory.  is_dev_clone must accept both."""
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    assert P.is_dev_clone(tmp_path) is True


def test_is_dev_clone_false_outside_repo(tmp_path: Path) -> None:
    assert P.is_dev_clone(tmp_path) is False


def test_check_switch_mod_submodule_present(monkeypatch: pytest.MonkeyPatch,
                                             tmp_path: Path) -> None:
    # switch-mod itself is inlined in the repo; the check now keys on
    # one of the nested vendored libs (imgui) being checked out.
    (tmp_path / "switch-mod" / "lib" / "imgui").mkdir(parents=True)
    (tmp_path / "switch-mod" / "lib" / "imgui" / "imgui.h").write_text(
        "", encoding="utf-8"
    )
    monkeypatch.setattr(P, "repo_root", lambda: tmp_path)
    r = P.check_switch_mod_submodule()
    assert r.ok is True


def test_check_switch_mod_submodule_accepts_bundled_apworld(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Packaged-install path: imgui.h doesn't exist on disk, we're not
    in a dev clone, but a .apworld zip is reachable by walking up from
    this file -- the check must return ok so the wizard doesn't queue a
    pointless `git submodule update` install step."""
    # Synthesize a .apworld file + a fake __file__ under it.
    apworld = tmp_path / "smbwonder.apworld"
    apworld.write_bytes(b"PK\x03\x04")
    fake_file = apworld / "smbwonder" / "_setup" / "prereqs.py"
    # No actual on-disk dir here -- Path() math is enough; the helper
    # just walks parents and checks suffix.

    monkeypatch.setattr(P, "repo_root", lambda: tmp_path / "no-repo")
    monkeypatch.setattr(P, "is_dev_clone", lambda: False)
    # Patch the resolved __file__ that check_switch_mod_submodule walks
    # up from.
    import apworld.smbw_archipelago._setup.prereqs as P_mod

    orig_resolve = Path.resolve
    def fake_resolve(self):
        # Only intercept the prereqs.py resolve() inside the check;
        # leave everything else alone so test infrastructure works.
        if self == Path(P_mod.__file__):
            return fake_file
        return orig_resolve(self)
    monkeypatch.setattr(Path, "resolve", fake_resolve)

    r = P.check_switch_mod_submodule()
    assert r.ok is True
    assert "bundled" in r.detail.lower()


def test_check_switch_mod_submodule_fails_for_loose_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Loose-files install (extracted .apworld, not a zip ancestor,
    not a dev clone): no way to build, surface a clear failure."""
    monkeypatch.setattr(P, "repo_root", lambda: tmp_path / "no-repo")
    monkeypatch.setattr(P, "is_dev_clone", lambda: False)
    # Make sure Path.resolve doesn't accidentally land on a real
    # .apworld -- redirect to a clean tmp tree with no .apworld
    # ancestors.
    import apworld.smbw_archipelago._setup.prereqs as P_mod
    loose_setup = tmp_path / "loose" / "smbw_archipelago" / "_setup"
    loose_setup.mkdir(parents=True)
    orig_resolve = Path.resolve
    def fake_resolve(self):
        if self == Path(P_mod.__file__):
            return loose_setup / "prereqs.py"
        return orig_resolve(self)
    monkeypatch.setattr(Path, "resolve", fake_resolve)

    r = P.check_switch_mod_submodule()
    assert r.ok is False
    assert "not a dev clone" in r.detail.lower() or "no bundled" in r.detail.lower()


def test_check_ryujinx_warn_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ryujinx missing is warn-only — won't fail the pipeline."""
    monkeypatch.setattr(P, "ryujinx_default_root", lambda: tmp_path / "nonexistent")
    r = P.check_ryujinx()
    assert r.warn_only is True


def test_all_ok_ignores_warn_only() -> None:
    """check_ryujinx is the only warn-only row; an unset Ryujinx must not
    propagate ok=False through all_ok."""
    results = [
        P.PrereqResult("git", "Git", True, "ok"),
        P.PrereqResult("ryujinx", "Ryujinx", False, "missing", warn_only=True),
    ]
    assert P.all_ok(results) is True


def test_missing_auto_installable_filters_correctly() -> None:
    results = [
        P.PrereqResult("git", "Git", False, "", auto_installable=True),
        P.PrereqResult("dev_mode", "Dev Mode", False, "", auto_installable=False),
        P.PrereqResult("cmake", "CMake", True, "", auto_installable=True),
        P.PrereqResult("ryujinx", "Ryujinx", False, "", warn_only=True),
    ]
    keys = P.missing_auto_installable(results)
    assert keys == ["git"]


def test_check_all_returns_ordered_results() -> None:
    """check_all wraps the per-detector calls in a fixed display order;
    a smoke test that the function returns a non-empty list with all
    expected keys is enough."""
    results = P.check_all()
    keys = {r.key for r in results}
    assert "dev_mode" in keys
    assert "git" in keys
    assert "cmake" in keys
    assert "ninja" in keys
    assert "python311" in keys
    assert "devkitpro" in keys
    assert "switch_dev" in keys
    assert "archipelago_submodule" in keys
    assert "switch_mod_submodule" in keys
    assert "archipelago_deps" in keys
    assert "ryujinx" in keys
