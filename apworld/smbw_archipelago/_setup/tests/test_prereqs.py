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


@pytest.fixture
def force_windows(monkeypatch: pytest.MonkeyPatch):
    """Pretend we're on Windows for tests that exercise Windows-only
    behavior (winget hints, msys2 cmake rejection, portable LLVM probe,
    `python3.exe` shim). Without this, the same tests fail on a Linux
    CI runner where the platform-branching detectors take the
    "package-manager" path instead."""
    monkeypatch.setattr(P, "_is_windows", lambda: True)
    monkeypatch.setattr(P.sys, "platform", "win32")
    return monkeypatch


def test_check_git_success(patch_run: dict[str, Any]) -> None:
    patch_run["git"] = (0, "git version 2.43.0", "")
    r = P.check_git()
    assert r.ok is True
    assert "git version" in r.detail


def test_check_git_missing(force_windows, patch_run: dict[str, Any]) -> None:
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
    # Distinguish too-old from not-found; surface the actual version.
    assert "too old" in r.detail
    assert "3.10.0" in r.detail


def test_check_cmake_accepts_recent(patch_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    patch_run["cmake"] = (0, "cmake version 3.30.5\n", "")
    P._CMAKE_DEFAULT_PATHS = ()  # type: ignore[assignment]
    # Pretend `which cmake` returns a non-msys2 path so the bare-name
    # fallback isn't rejected.
    monkeypatch.setattr(P.shutil, "which", lambda _x: "C:/Program Files/CMake/bin/cmake.exe")
    r = P.check_cmake()
    assert r.ok is True
    assert "3.30.5" in r.detail


def test_check_cmake_rejects_msys2_bare_name(force_windows, patch_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """msys2's cmake mangles drive-letter paths. Even when version-good,
    the bare-name PATH fallback must reject it. Windows-only behavior."""
    patch_run["cmake"] = (0, "cmake version 3.30.5\n", "")
    P._CMAKE_DEFAULT_PATHS = ()  # type: ignore[assignment]
    monkeypatch.setattr(P.shutil, "which", lambda _x: "C:/msys64/usr/bin/cmake.exe")
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


def test_check_llvm19_missing(force_windows,
                              monkeypatch: pytest.MonkeyPatch,
                              tmp_path: Path,
                              patch_run: dict[str, Any]) -> None:
    """No portable install, no canonical install, no `clang` on PATH →
    fail with auto-install available. Windows-only (the auto-install
    tarball is the MSVC build); the Linux equivalent is covered by
    `test_check_llvm19_missing_on_linux_skips_portable_probe`."""
    monkeypatch.setattr(P, "llvm_portable_root", lambda: tmp_path / "noexist")
    monkeypatch.setattr(P, "_LLVM_DEFAULT_PATHS", ())
    patch_run["clang"] = None
    r = P.check_llvm19()
    assert r.ok is False
    assert "not found" in r.detail
    assert r.auto_installable is True


def test_check_llvm19_accepts_portable_19(force_windows,
                                          monkeypatch: pytest.MonkeyPatch,
                                          tmp_path: Path,
                                          patch_run: dict[str, Any]) -> None:
    """Synthesize a portable LLVM 19.1.7 install and verify the detector
    picks it up + caches the bin dir. Portable install is a Windows-only
    concept (the wizard's auto-installer only populates it on Windows)."""
    P._resolved_llvm_bin = None  # type: ignore[attr-defined]
    root = tmp_path / "llvm"
    bin_dir = root / "bin"
    clang_exe = bin_dir / "clang.exe"
    bin_dir.mkdir(parents=True)
    clang_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(P, "llvm_portable_root", lambda: root)
    monkeypatch.setattr(P, "_LLVM_DEFAULT_PATHS", ())
    patch_run[str(clang_exe)] = (0, "clang version 19.1.7\n", "")
    r = P.check_llvm19()
    assert r.ok is True
    assert "19.1.7" in r.detail
    assert P.resolved_llvm_bin() == str(bin_dir)


def test_check_llvm19_accepts_canonical_install(force_windows,
                                                monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path,
                                                patch_run: dict[str, Any]) -> None:
    """Probe finds LLVM 19 at the canonical `C:\\Program Files\\LLVM\\bin\\`
    install path even when there's no portable install and `clang` isn't
    on PATH (typical for devs who installed via the official .exe but
    didn't tick "add to PATH")."""
    P._resolved_llvm_bin = None  # type: ignore[attr-defined]
    monkeypatch.setattr(P, "llvm_portable_root", lambda: tmp_path / "noportable")
    canonical = tmp_path / "Program Files" / "LLVM" / "bin" / "clang.exe"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("", encoding="utf-8")
    monkeypatch.setattr(P, "_LLVM_DEFAULT_PATHS", (canonical,))
    patch_run[str(canonical)] = (0, "clang version 19.1.7\n", "")
    # `clang` on PATH must NOT win — we want to prove the canonical
    # probe fires before PATH fallback.
    patch_run["clang"] = None
    r = P.check_llvm19()
    assert r.ok is True
    assert "19.1.7" in r.detail
    assert P.resolved_llvm_bin() == str(canonical.parent)


def test_check_llvm19_skips_canonical_with_wrong_version(monkeypatch: pytest.MonkeyPatch,
                                                         tmp_path: Path,
                                                         patch_run: dict[str, Any]) -> None:
    """A canonical install at the wrong major version (e.g. LLVM 18 or 20
    that the user installed for another project) must not poison the
    probe — fall through to the PATH fallback or report not-found
    cleanly rather than blocking on the wrong-version system install."""
    P._resolved_llvm_bin = None  # type: ignore[attr-defined]
    monkeypatch.setattr(P, "llvm_portable_root", lambda: tmp_path / "noportable")
    canonical = tmp_path / "Program Files" / "LLVM" / "bin" / "clang.exe"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("", encoding="utf-8")
    monkeypatch.setattr(P, "_LLVM_DEFAULT_PATHS", (canonical,))
    patch_run[str(canonical)] = (0, "clang version 20.1.0\n", "")
    patch_run["clang"] = None
    r = P.check_llvm19()
    # Wrong-version canonical install was skipped; PATH probe also
    # failed → reported as not-found rather than blocking on the bad
    # canonical install.
    assert r.ok is False
    assert "not found" in r.detail


def test_check_llvm19_rejects_wrong_major(monkeypatch: pytest.MonkeyPatch,
                                          tmp_path: Path,
                                          patch_run: dict[str, Any]) -> None:
    """LLVM 20+ on PATH must be rejected with a clear version-mismatch
    message — LibHakkun's libc++ ABI is pinned at 19."""
    P._resolved_llvm_bin = None  # type: ignore[attr-defined]
    monkeypatch.setattr(P, "llvm_portable_root", lambda: tmp_path / "noexist")
    monkeypatch.setattr(P, "_LLVM_DEFAULT_PATHS", ())
    patch_run["clang"] = (0, "clang version 20.1.0\n", "")
    r = P.check_llvm19()
    assert r.ok is False
    assert "19" in r.detail


def test_ensure_python3_shim_creates_copy(force_windows, tmp_path: Path) -> None:
    src = tmp_path / "python.exe"
    src.write_text("fake interpreter", encoding="utf-8")
    P.ensure_python3_shim(src)
    shim = tmp_path / "python3.exe"
    assert shim.is_file()
    assert shim.read_text(encoding="utf-8") == "fake interpreter"


def test_ensure_python3_shim_idempotent(force_windows, tmp_path: Path) -> None:
    src = tmp_path / "python.exe"
    src.write_text("v1", encoding="utf-8")
    shim = tmp_path / "python3.exe"
    shim.write_text("existing", encoding="utf-8")
    P.ensure_python3_shim(src)
    # Existing shim left alone.
    assert shim.read_text(encoding="utf-8") == "existing"


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


def test_check_archipelago_deps_short_circuits_on_packaged_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Packaged install + AP importable: skip the marker/import probe
    and trust the surrounding install.  Otherwise the wizard queues a
    pointless pip install against a `requirements.txt` that doesn't
    exist outside a dev clone."""
    monkeypatch.setattr(P, "is_dev_clone", lambda repo=None: False)
    monkeypatch.setattr(P, "archipelago_importable", lambda: True)
    # Should not even try to read the marker -- if we patch _safe_run
    # to raise, the test would catch a leak into the probe path.
    def boom(*_a, **_kw):
        raise AssertionError("import probe must not run in short-circuit")
    monkeypatch.setattr(P, "_safe_run", boom)
    r = P.check_archipelago_deps()
    assert r.ok is True
    assert "surrounding" in r.detail.lower() or "satisfied" in r.detail.lower()


def test_check_archipelago_deps_runs_probe_in_dev_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Dev clone: the short-circuit MUST NOT fire even if AP happens to
    be importable -- a dev with a stale submodule wants the import
    probe to surface the actual breakage."""
    monkeypatch.setattr(P, "is_dev_clone", lambda repo=None: True)
    monkeypatch.setattr(P, "archipelago_importable", lambda: True)
    monkeypatch.setattr(P, "repo_root", lambda: tmp_path)   # no req.txt

    probe_calls: list[list[str]] = []
    def fake_run(cmd):
        probe_calls.append(cmd)
        return (0, "", "")
    monkeypatch.setattr(P, "_safe_run", fake_run)

    P.check_archipelago_deps()
    assert probe_calls, "import probe should run in a dev clone"


def test_check_switch_mod_submodule_present(monkeypatch: pytest.MonkeyPatch,
                                             tmp_path: Path) -> None:
    # switch-mod itself is inlined in the repo; the check keys on the
    # LibHakkun framework (switch-mod/sys/cmake/module.cmake) being
    # checked out, since that's the file the repo's main CMakeLists.txt
    # actually includes.
    (tmp_path / "switch-mod" / "sys" / "cmake").mkdir(parents=True)
    (tmp_path / "switch-mod" / "sys" / "cmake" / "module.cmake").write_text(
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
    assert "llvm19" in keys
    assert "archipelago_submodule" in keys
    assert "switch_mod_submodule" in keys
    assert "archipelago_deps" in keys
    assert "ryujinx" in keys
    assert "lz4" in keys
    assert "pyelftools" in keys
    # exlaunch-era keys should be retired entirely:
    assert "devkitpro" not in keys
    assert "switch_dev" not in keys


# ---------------------------------------------------------------------------
# check_lz4
# ---------------------------------------------------------------------------

def test_check_lz4_marker_short_circuits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """If the marker file exists, skip the import probe entirely."""
    marker = tmp_path / "lz4.ok"
    marker.write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr(P, "lz4_marker_path", lambda: marker)

    def boom(*_a, **_kw):
        raise AssertionError("import probe must not run when marker exists")
    monkeypatch.setattr(P, "_safe_run", boom)

    r = P.check_lz4()
    assert r.ok is True
    assert r.auto_installable is True


def test_check_lz4_ok_via_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No marker but lz4 is importable: check returns ok and writes marker."""
    marker = tmp_path / "lz4.ok"
    monkeypatch.setattr(P, "lz4_marker_path", lambda: marker)
    monkeypatch.setattr(P, "_resolved_python_bin", "/usr/bin/python3")

    def fake_run(cmd):
        assert "import lz4.block" in " ".join(cmd)
        return (0, "", "")
    monkeypatch.setattr(P, "_safe_run", fake_run)

    r = P.check_lz4()
    assert r.ok is True
    assert marker.is_file()


def test_check_lz4_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """lz4 not installed: check returns ok=False with auto_installable."""
    marker = tmp_path / "lz4.ok"
    monkeypatch.setattr(P, "lz4_marker_path", lambda: marker)
    monkeypatch.setattr(P, "_resolved_python_bin", "/usr/bin/python3")
    monkeypatch.setattr(P, "_safe_run", lambda _cmd: (1, "", "ModuleNotFoundError: No module named 'lz4'"))

    r = P.check_lz4()
    assert r.ok is False
    assert r.auto_installable is True
    assert not marker.exists()


# ---------------------------------------------------------------------------
# check_pyelftools
# ---------------------------------------------------------------------------

def test_check_pyelftools_marker_short_circuits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """If the marker file exists, skip the import probe entirely."""
    marker = tmp_path / "pyelftools.ok"
    marker.write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr(P, "pyelftools_marker_path", lambda: marker)

    def boom(*_a, **_kw):
        raise AssertionError("import probe must not run when marker exists")
    monkeypatch.setattr(P, "_safe_run", boom)

    r = P.check_pyelftools()
    assert r.ok is True
    assert r.auto_installable is True


def test_check_pyelftools_ok_via_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No marker but pyelftools is importable: check returns ok and writes marker."""
    marker = tmp_path / "pyelftools.ok"
    monkeypatch.setattr(P, "pyelftools_marker_path", lambda: marker)
    monkeypatch.setattr(P, "_resolved_python_bin", "/usr/bin/python3")

    def fake_run(cmd):
        assert "elftools" in " ".join(cmd)
        return (0, "", "")
    monkeypatch.setattr(P, "_safe_run", fake_run)

    r = P.check_pyelftools()
    assert r.ok is True
    assert marker.is_file()


def test_check_pyelftools_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """pyelftools not installed: check returns ok=False with auto_installable."""
    marker = tmp_path / "pyelftools.ok"
    monkeypatch.setattr(P, "pyelftools_marker_path", lambda: marker)
    monkeypatch.setattr(P, "_resolved_python_bin", "/usr/bin/python3")
    monkeypatch.setattr(P, "_safe_run", lambda _cmd: (1, "", "ModuleNotFoundError: No module named 'elftools'"))

    r = P.check_pyelftools()
    assert r.ok is False
    assert r.auto_installable is True
    assert not marker.exists()


# ---------------------------------------------------------------------------
# Linux branch — system tools become detect-only (no winget, no
# auto-install). Tests force `_is_windows` False so they pass on any
# host. The reverse (forcing True) covers the Windows branch on a
# Linux CI runner; the host-native tests above already cover Windows
# on a Windows runner.
# ---------------------------------------------------------------------------


@pytest.fixture
def force_linux(monkeypatch: pytest.MonkeyPatch):
    """Pretend we're on Linux for the duration of the test.

    Patches both `_is_windows` (the helper every detector branches on)
    and `sys.platform` (so any deeper code that checks platform agrees).
    """
    monkeypatch.setattr(P, "_is_windows", lambda: False)
    monkeypatch.setattr(P.sys, "platform", "linux")
    return monkeypatch


def test_check_git_missing_on_linux_is_not_auto_installable(
    force_linux, patch_run: dict[str, Any],
) -> None:
    """On Linux, missing git surfaces as detect-only with package-manager
    hints — `winget` must not appear in the note."""
    patch_run["git"] = None
    r = P.check_git()
    assert r.ok is False
    assert r.auto_installable is False
    assert "winget" not in r.note.lower()
    assert "apt" in r.note or "package manager" in r.note.lower()


def test_check_git_present_on_linux_is_not_auto_installable(
    force_linux, patch_run: dict[str, Any],
) -> None:
    """Even when git IS present, the row's auto_installable flag stays
    False on Linux so the wizard doesn't surface an Auto-install button
    that would do nothing useful."""
    patch_run["git"] = (0, "git version 2.43.0", "")
    r = P.check_git()
    assert r.ok is True
    assert r.auto_installable is False


def test_check_cmake_missing_on_linux_skips_winget_note(
    force_linux, patch_run: dict[str, Any],
) -> None:
    patch_run["cmake"] = None
    P._CMAKE_DEFAULT_PATHS = ()  # type: ignore[assignment]
    r = P.check_cmake()
    assert r.ok is False
    assert r.auto_installable is False
    assert "winget" not in r.note.lower()


def test_check_ninja_missing_on_linux_skips_winget_probe(
    force_linux, monkeypatch: pytest.MonkeyPatch, patch_run: dict[str, Any],
) -> None:
    """The _winget_ninja_paths probe must not even run on Linux; the
    detector falls straight to `shutil.which`."""
    def boom() -> list[Path]:
        raise AssertionError("_winget_ninja_paths must not be called on Linux")
    monkeypatch.setattr(P, "_winget_ninja_paths", boom)
    patch_run["ninja"] = None
    r = P.check_ninja()
    assert r.ok is False
    assert r.auto_installable is False
    assert "winget" not in r.note.lower()


def test_check_python311_missing_on_linux_skips_py_launcher(
    force_linux, monkeypatch: pytest.MonkeyPatch, patch_run: dict[str, Any],
) -> None:
    """The Windows-only `py -3.x` launcher candidates must not be probed
    on Linux."""
    seen: list[list[str]] = []

    def recording_safe_run(cmd: list[str]) -> tuple[int, str, str] | None:
        seen.append(cmd)
        return None  # everything missing
    monkeypatch.setattr(P, "_safe_run", recording_safe_run)
    # Force sys.executable to a path that's_safe_run will return None for.
    monkeypatch.setattr(P, "sys", P.sys)  # no-op, but keeps lints quiet
    r = P.check_python311()
    assert r.ok is False
    assert r.auto_installable is False
    assert "winget" not in r.note.lower()
    # The `py` launcher must not appear in the probed-commands list.
    assert not any(cmd and cmd[0] == "py" for cmd in seen), \
        f"py launcher was probed on Linux: {seen}"


def test_check_llvm19_missing_on_linux_skips_portable_probe(
    force_linux, monkeypatch: pytest.MonkeyPatch, patch_run: dict[str, Any],
) -> None:
    """On Linux, the portable + canonical Windows install probes must
    be skipped — only the PATH probe runs."""
    def boom() -> Path:
        raise AssertionError("llvm_portable_root must not be called on Linux")
    monkeypatch.setattr(P, "llvm_portable_root", boom)
    # _LLVM_DEFAULT_PATHS still set; the gate is the platform branch,
    # not the path set being empty.
    patch_run["clang"] = None
    r = P.check_llvm19()
    assert r.ok is False
    assert r.auto_installable is False
    assert "winget" not in r.note.lower()
    assert "package manager" in r.note.lower() or "apt" in r.note


def test_check_llvm19_accepts_path_clang_on_linux(
    force_linux, patch_run: dict[str, Any],
) -> None:
    """A distro `clang version 19.1.7` (or even `(Ubuntu …) clang version
    19.1.7`) on PATH should be accepted on Linux."""
    P._resolved_llvm_bin = None  # type: ignore[attr-defined]
    patch_run["clang"] = (0, "Ubuntu clang version 19.1.7", "")
    r = P.check_llvm19()
    assert r.ok is True
    assert "19.1.7" in r.detail


def test_ensure_python3_shim_is_noop_on_linux(
    force_linux, tmp_path: Path,
) -> None:
    """The python3.exe shim trick is Windows-only — never create a shim
    on Linux even if asked."""
    src = tmp_path / "python"
    src.write_text("fake interpreter", encoding="utf-8")
    P.ensure_python3_shim(src)
    # No python3.exe (or python3) shim should appear next to it.
    assert not (tmp_path / "python3.exe").exists()
    assert not (tmp_path / "python3").exists() or (tmp_path / "python3") == src


def test_install_urls_swap_for_linux_at_import_time() -> None:
    """The git / python311 entries in INSTALL_URLS swap to OS-neutral
    landing pages on non-Windows; the test platform decides which
    entries are live."""
    git_url = P.INSTALL_URLS["git"]
    python_url = P.INSTALL_URLS["python311"]
    if sys.platform == "win32":
        assert "/download/win" in git_url
        assert "python-3119" in python_url
    else:
        assert "/download/linux" in git_url
        # The Linux URL is the generic landing page, not the 3.11.9
        # release-specific anchor.
        assert "release/python-3119" not in python_url
