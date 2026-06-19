"""Tests for the build phase.

`_stream_subprocess` itself is the same shape as installers — covered
via mock there. Here we focus on the build-specific glue: env
composition, artifact verification, configure-skip-if-ready.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from apworld.smbw_archipelago._setup import build as B


def test_expected_artifacts_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(B, "repo_root", lambda: tmp_path)
    arts = B.expected_artifacts()
    assert arts["subsdk9"] == tmp_path / "switch-mod" / "build" / "exefs" / "subsdk9"
    assert arts["main.npdm"] == tmp_path / "switch-mod" / "build" / "exefs" / "main.npdm"


def test_compose_build_env_prepends_llvm_bin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """LibHakkun's toolchain.cmake hardcodes bare clang/clang++. The
    build env must prepend the wizard-resolved LLVM bin so cmake picks
    up the verified 19.1.x rather than whatever's first on PATH."""
    llvm_bin = str(tmp_path / "llvm" / "bin")
    monkeypatch.setattr(B, "resolved_llvm_bin", lambda: llvm_bin)
    monkeypatch.setattr(B, "resolved_ninja_bin", lambda: None)
    monkeypatch.setattr(B, "resolved_python_bin", lambda: None)
    env = B._compose_build_env()
    assert llvm_bin in env["PATH"].split(os.pathsep)
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_compose_build_env_prepends_python_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """LibHakkun's toolchain.cmake shells out to bare `python3` for
    setup_libcxx_prepackaged.py. The build env must prepend the resolved
    Python's dir so that `python3.exe` (the shim ensure_python3_shim
    drops) is reachable."""
    py_exe = tmp_path / "py" / "python.exe"
    py_exe.parent.mkdir(parents=True)
    py_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "resolved_llvm_bin", lambda: None)
    monkeypatch.setattr(B, "resolved_ninja_bin", lambda: None)
    monkeypatch.setattr(B, "resolved_python_bin", lambda: str(py_exe))
    env = B._compose_build_env()
    assert str(py_exe.parent) in env["PATH"].split(os.pathsep)


def test_compose_build_env_resolved_python_wins_over_path_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Regression: the wizard resolves a specific Python (e.g. 3.12 via
    `py -3.12`) and installs lz4 / pyelftools into it, but the build's
    cmake POST_BUILD runs bare `python elf2nso.py` / `deploy.py`. If a
    DIFFERENT Python (e.g. 3.14) sits first on the inherited PATH and the
    resolved one is also already on PATH (just later), the old skip-if-
    present prepend left 3.14 winning — so the build died with
    `ModuleNotFoundError: No module named 'lz4'` right after linking.

    The resolved Python's dir must be relocated to the FRONT, ahead of
    the other Python, regardless of whether it was already on PATH."""
    other_py_dir = tmp_path / "py314"
    resolved_dir = tmp_path / "py312"
    other_py_dir.mkdir()
    resolved_dir.mkdir()
    py_exe = resolved_dir / "python.exe"
    py_exe.write_text("", encoding="utf-8")
    # Inherited PATH: the wrong Python first, the resolved one already
    # present but later — the exact shape that defeated skip-if-present.
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(other_py_dir), str(resolved_dir)])
    )
    monkeypatch.setattr(B, "resolved_llvm_bin", lambda: None)
    monkeypatch.setattr(B, "resolved_ninja_bin", lambda: None)
    monkeypatch.setattr(B, "resolved_python_bin", lambda: str(py_exe))
    env = B._compose_build_env()
    parts = env["PATH"].split(os.pathsep)
    assert parts.index(str(resolved_dir)) < parts.index(str(other_py_dir))
    # And no duplicate entry left behind by the relocation.
    assert parts.count(str(resolved_dir)) == 1


def test_compose_build_env_prepends_ninja(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ninja_bin = str(tmp_path / "ninja-dir")
    monkeypatch.setattr(B, "resolved_llvm_bin", lambda: None)
    monkeypatch.setattr(B, "resolved_python_bin", lambda: None)
    monkeypatch.setattr(B, "resolved_ninja_bin", lambda: ninja_bin)
    env = B._compose_build_env()
    assert ninja_bin in env["PATH"].split(os.pathsep)


def test_compose_build_env_no_devkitpro_leakage(monkeypatch: pytest.MonkeyPatch) -> None:
    """The post-hakkun build env must not set DEVKITPRO or prepend
    msys2 paths from a *resolved* devkitPro install. We scrub the
    inherited PATH down to a known value first so we can assert what
    _compose_build_env itself adds — anything that was already in the
    user's PATH (e.g. a system-wide devkitPro install) is irrelevant
    here; we just care that the wizard doesn't re-add the exlaunch
    toolchain on top."""
    monkeypatch.setattr(B, "resolved_llvm_bin", lambda: None)
    monkeypatch.setattr(B, "resolved_ninja_bin", lambda: None)
    monkeypatch.setattr(B, "resolved_python_bin", lambda: None)
    monkeypatch.delenv("DEVKITPRO", raising=False)
    monkeypatch.setenv("PATH", "C:/known/path")
    env = B._compose_build_env()
    assert "DEVKITPRO" not in env
    # Composed-on PATH must be exactly the inherited stub (no new
    # devkitpro/msys2 entries prepended by the function).
    assert env.get("PATH", "") == "C:/known/path"


def test_run_build_phase_skips_configure_when_cache_exists(monkeypatch: pytest.MonkeyPatch,
                                                            tmp_path: Path) -> None:
    """If CMakeCache.txt is present, configure should be skipped by
    default — that's the dev re-build optimization."""
    monkeypatch.setattr(B, "repo_root", lambda: tmp_path)
    bd = tmp_path / "switch-mod" / "build"
    bd.mkdir(parents=True)
    (bd / "CMakeCache.txt").write_text("", encoding="utf-8")

    configure_calls: list[Any] = []
    build_calls: list[Any] = []

    def fake_configure(**kw: Any) -> B.BuildResult:
        configure_calls.append(kw)
        return B.BuildResult(True, 0, "")

    def fake_build(**kw: Any) -> B.BuildResult:
        build_calls.append(kw)
        # Materialize artifacts so post-build verification passes.
        (bd / "exefs").mkdir(exist_ok=True)
        (bd / "exefs" / "subsdk9").write_bytes(b"x")
        (bd / "exefs" / "main.npdm").write_bytes(b"y")
        return B.BuildResult(True, 0, "")

    monkeypatch.setattr(B, "cmake_configure", fake_configure)
    monkeypatch.setattr(B, "cmake_build", fake_build)

    outcome = B.run_build_phase(skip_configure_if_ready=True)
    assert outcome.ok is True
    assert configure_calls == []   # configure WAS skipped
    assert len(build_calls) == 1
    assert "subsdk9" in outcome.outputs
    assert "main.npdm" in outcome.outputs


def test_run_build_phase_runs_configure_when_cache_missing(monkeypatch: pytest.MonkeyPatch,
                                                            tmp_path: Path) -> None:
    monkeypatch.setattr(B, "repo_root", lambda: tmp_path)
    bd = tmp_path / "switch-mod" / "build"
    bd.mkdir(parents=True)
    # No CMakeCache.txt → configure must run.

    saw_configure = []
    def fake_configure(**kw: Any) -> B.BuildResult:
        saw_configure.append(True)
        return B.BuildResult(True, 0, "")

    def fake_build(**kw: Any) -> B.BuildResult:
        (bd / "exefs").mkdir(exist_ok=True)
        (bd / "exefs" / "subsdk9").write_bytes(b"x")
        (bd / "exefs" / "main.npdm").write_bytes(b"y")
        return B.BuildResult(True, 0, "")

    monkeypatch.setattr(B, "cmake_configure", fake_configure)
    monkeypatch.setattr(B, "cmake_build", fake_build)

    outcome = B.run_build_phase(skip_configure_if_ready=True)
    assert outcome.ok is True
    assert saw_configure == [True]


def test_run_build_phase_forces_configure_when_bridge_host_set(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A bridge_host override must reach cmake even when CMakeCache.txt
    exists — otherwise the -DBRIDGE_HOST_STRING never lands. So configure
    is forced, and bridge_host is threaded into it."""
    monkeypatch.setattr(B, "repo_root", lambda: tmp_path)
    bd = tmp_path / "switch-mod" / "build"
    bd.mkdir(parents=True)
    (bd / "CMakeCache.txt").write_text("", encoding="utf-8")  # cache present

    configure_calls: list[Any] = []

    def fake_configure(**kw: Any) -> B.BuildResult:
        configure_calls.append(kw)
        return B.BuildResult(True, 0, "")

    def fake_build(**kw: Any) -> B.BuildResult:
        (bd / "exefs").mkdir(exist_ok=True)
        (bd / "exefs" / "subsdk9").write_bytes(b"x")
        (bd / "exefs" / "main.npdm").write_bytes(b"y")
        return B.BuildResult(True, 0, "")

    monkeypatch.setattr(B, "cmake_configure", fake_configure)
    monkeypatch.setattr(B, "cmake_build", fake_build)

    outcome = B.run_build_phase(skip_configure_if_ready=True, bridge_host="10.0.0.5")
    assert outcome.ok is True
    # Configure ran despite the cache, and carried the override.
    assert len(configure_calls) == 1
    assert configure_calls[0].get("bridge_host") == "10.0.0.5"


def test_run_build_phase_skips_configure_when_cached_seed_matches(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A normal /setup always passes a seed; when the cache already holds
    that exact seed, configure should still be skipped (fast rebuild)."""
    monkeypatch.setattr(B, "repo_root", lambda: tmp_path)
    bd = tmp_path / "switch-mod" / "build"
    bd.mkdir(parents=True)
    (bd / "CMakeCache.txt").write_text(
        "BRIDGE_HOST_STRING:STRING=192.168.7.42\n", encoding="utf-8")

    configure_calls: list[Any] = []
    monkeypatch.setattr(B, "cmake_configure",
                        lambda **kw: configure_calls.append(kw) or B.BuildResult(True, 0, ""))

    def fake_build(**kw: Any) -> B.BuildResult:
        (bd / "exefs").mkdir(exist_ok=True)
        (bd / "exefs" / "subsdk9").write_bytes(b"x")
        (bd / "exefs" / "main.npdm").write_bytes(b"y")
        return B.BuildResult(True, 0, "")

    monkeypatch.setattr(B, "cmake_build", fake_build)

    outcome = B.run_build_phase(skip_configure_if_ready=True, bridge_host="192.168.7.42")
    assert outcome.ok is True
    assert configure_calls == []   # same seed → no reconfigure


def test_run_build_phase_reconfigures_when_cached_seed_differs(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When the requested seed differs from the cached one, configure
    must run so the new -DBRIDGE_HOST_STRING lands."""
    monkeypatch.setattr(B, "repo_root", lambda: tmp_path)
    bd = tmp_path / "switch-mod" / "build"
    bd.mkdir(parents=True)
    (bd / "CMakeCache.txt").write_text(
        "BRIDGE_HOST_STRING:STRING=192.168.1.1\n", encoding="utf-8")

    configure_calls: list[Any] = []
    monkeypatch.setattr(B, "cmake_configure",
                        lambda **kw: configure_calls.append(kw) or B.BuildResult(True, 0, ""))

    def fake_build(**kw: Any) -> B.BuildResult:
        (bd / "exefs").mkdir(exist_ok=True)
        (bd / "exefs" / "subsdk9").write_bytes(b"x")
        (bd / "exefs" / "main.npdm").write_bytes(b"y")
        return B.BuildResult(True, 0, "")

    monkeypatch.setattr(B, "cmake_build", fake_build)

    outcome = B.run_build_phase(skip_configure_if_ready=True, bridge_host="10.0.0.5")
    assert outcome.ok is True
    assert len(configure_calls) == 1
    assert configure_calls[0].get("bridge_host") == "10.0.0.5"


def test_cached_bridge_host_reads_cmakecache(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(B, "repo_root", lambda: tmp_path)
    bd = tmp_path / "switch-mod" / "build"
    bd.mkdir(parents=True)
    assert B.cached_bridge_host() is None   # no cache yet
    (bd / "CMakeCache.txt").write_text(
        "SOME_OTHER:STRING=x\nBRIDGE_HOST_STRING:STRING=10.1.2.3\n", encoding="utf-8")
    assert B.cached_bridge_host() == "10.1.2.3"


def test_cmake_configure_forwards_bridge_host_define(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """cmake_configure must add -DBRIDGE_HOST_STRING=<addr> to the cmake
    command line when bridge_host is given (and omit it otherwise)."""
    src = tmp_path / "switch-mod"
    (src / "sys" / "cmake").mkdir(parents=True)
    (src / "sys" / "cmake" / "module.cmake").write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "is_dev_clone", lambda: True)
    monkeypatch.setattr(B, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(B, "resolved_cmake", lambda: "cmake")
    monkeypatch.setattr(B, "_apply_hakkun_patches", lambda *a, **k: None)
    monkeypatch.setattr(B, "_compose_build_env", lambda: {})

    captured: list[list[str]] = []

    def fake_stream(cmd: list[str], **_kw: Any) -> B.BuildResult:
        captured.append(cmd)
        return B.BuildResult(True, 0, "")

    monkeypatch.setattr(B, "_stream_subprocess", fake_stream)

    B.cmake_configure(bridge_host="192.168.7.42")
    assert any(a == "-DBRIDGE_HOST_STRING=192.168.7.42" for a in captured[0])

    captured.clear()
    B.cmake_configure()  # no override
    assert not any(a.startswith("-DBRIDGE_HOST_STRING") for a in captured[0])


def test_run_build_phase_fails_on_missing_artifacts(monkeypatch: pytest.MonkeyPatch,
                                                     tmp_path: Path) -> None:
    """Build subprocess exiting 0 but no artifacts on disk should be
    reported as failure — otherwise deploy fails confusingly later."""
    monkeypatch.setattr(B, "repo_root", lambda: tmp_path)
    bd = tmp_path / "switch-mod" / "build"
    bd.mkdir(parents=True)
    (bd / "CMakeCache.txt").write_text("", encoding="utf-8")

    monkeypatch.setattr(B, "cmake_configure", lambda **_k: B.BuildResult(True, 0, ""))
    # Build "succeeds" but doesn't write any artifact.
    monkeypatch.setattr(B, "cmake_build", lambda **_k: B.BuildResult(True, 0, ""))

    outcome = B.run_build_phase()
    assert outcome.ok is False
    assert outcome.outputs == {}


def test_run_build_phase_fails_on_empty_artifacts(monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Path) -> None:
    """A 0-byte subsdk9 should be treated as a build failure (linker
    sometimes produces empty files on error)."""
    monkeypatch.setattr(B, "repo_root", lambda: tmp_path)
    bd = tmp_path / "switch-mod" / "build"
    bd.mkdir(parents=True)
    (bd / "CMakeCache.txt").write_text("", encoding="utf-8")

    def fake_build(**_kw: Any) -> B.BuildResult:
        (bd / "exefs").mkdir(exist_ok=True)
        (bd / "exefs" / "subsdk9").write_bytes(b"")          # empty
        (bd / "exefs" / "main.npdm").write_bytes(b"x")
        return B.BuildResult(True, 0, "")

    monkeypatch.setattr(B, "cmake_configure", lambda **_k: B.BuildResult(True, 0, ""))
    monkeypatch.setattr(B, "cmake_build", fake_build)

    outcome = B.run_build_phase()
    assert outcome.ok is False


def test_cmake_configure_fails_without_libhakkun(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    """When switch-mod/sys/cmake/module.cmake (LibHakkun submodule sentinel)
    is missing, the configure must fail clean with an actionable
    `git submodule update` hint rather than blow up inside cmake's
    `include(sys/cmake/module.cmake)`."""
    monkeypatch.setattr(B, "is_dev_clone", lambda: True)
    monkeypatch.setattr(B, "repo_root", lambda: tmp_path)
    (tmp_path / "switch-mod").mkdir()
    # No sys/cmake/module.cmake → LibHakkun submodule not initialized.
    r = B.cmake_configure()
    assert r.ok is False
    assert "submodule" in r.log.lower() or "libhakkun" in r.log.lower()


def test_cmake_build_fails_without_build_dir(monkeypatch: pytest.MonkeyPatch,
                                              tmp_path: Path) -> None:
    monkeypatch.setattr(B, "repo_root", lambda: tmp_path)
    # No switch-mod/build/.
    r = B.cmake_build()
    assert r.ok is False
    assert "build dir missing" in r.log


# ---------------------------------------------------------------------------
# Bundled-tree extraction tests -- packaged-install ("running from a
# stock AP custom_worlds/<name>.apworld") path.  These exercise the seam
# between `Path(__file__)` walking up to find a .apworld zip ancestor
# and the extraction-to-%APPDATA% logic the wizard's build phase
# depends on.
# ---------------------------------------------------------------------------

def _make_fake_apworld(zip_path: Path, *, with_switch_mod: bool = True) -> None:
    """Build a minimal .apworld zip mimicking what
    scripts/install_apworld.py --bundle-mod emits.  Tests use this in
    place of the real ~MB release artifact."""
    import zipfile
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("smbwonder/__init__.py", "")
        zf.writestr("smbwonder/_setup/__init__.py", "")
        if with_switch_mod:
            # Minimum cmake source tree the wizard expects.  CMakeLists
            # is the sentinel `bundled_switch_mod()` checks for.
            zf.writestr("smbwonder/_setup/switch-mod/CMakeLists.txt",
                        "project(smbwap)\n")
            zf.writestr("smbwonder/_setup/switch-mod/lib/imgui/imgui.h",
                        "// fake\n")
            zf.writestr("smbwonder/_setup/switch-mod/src/program/main.cpp",
                        "int main(){}\n")


def test_find_apworld_zip_walks_up_to_zip_ancestor(tmp_path: Path) -> None:
    """Production: `Path(__file__)` is somewhere under
    `<.../some.apworld>/<innerpkg>/...`.  _find_apworld_zip walks up
    looking for the `.apworld` file ancestor."""
    apworld = tmp_path / "smbwonder.apworld"
    apworld.write_bytes(b"PK\x03\x04")   # not a real zip; just needs to be a file
    deep = apworld / "smbwonder" / "_setup"
    found = B._find_apworld_zip(deep)
    assert found == apworld


def test_find_apworld_zip_returns_none_for_dev_tree(tmp_path: Path) -> None:
    """Dev checkouts have no `.apworld` ancestor; the helper returns
    None so callers fall through to the in-place dev path."""
    deep = tmp_path / "smwonder_archipelago" / "apworld" / "smbw_archipelago" / "_setup"
    deep.mkdir(parents=True)
    assert B._find_apworld_zip(deep) is None


def test_extract_bundled_tree_extracts_from_real_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: feed a fake .apworld, point _SETUP_ROOT inside it,
    redirect %APPDATA% to a tmpdir, and verify the switch-mod tree
    lands on disk with the expected CMakeLists sentinel."""
    apworld = tmp_path / "smbwonder.apworld"
    _make_fake_apworld(apworld)
    setup_root = apworld / "smbwonder" / "_setup"
    dst = tmp_path / "appdata-bundled"

    # Reset memoization so the test's redirect actually takes effect.
    monkeypatch.setattr(B, "_extracted_bundled_root", None)
    result = B._extract_bundled_tree(setup_root, dst_override=dst)

    assert result == dst
    assert (dst / "switch-mod" / "CMakeLists.txt").is_file()
    assert (dst / "switch-mod" / "lib" / "imgui" / "imgui.h").is_file()
    assert (dst / ".source-zip-mtime").is_file()


def test_extract_bundled_tree_returns_setup_root_on_dev_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No .apworld ancestor: extraction is a no-op and the helper
    returns `setup_root` unchanged."""
    setup_root = tmp_path / "apworld" / "smbw_archipelago" / "_setup"
    setup_root.mkdir(parents=True)
    monkeypatch.setattr(B, "_extracted_bundled_root", None)
    assert B._extract_bundled_tree(setup_root, dst_override=tmp_path / "n/a") == setup_root


def test_extract_bundled_tree_caches_via_mtime_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second call with the same source zip mtime must NOT re-extract.
    We assert by tracking how many times zipfile.ZipFile is opened."""
    import zipfile as _zf
    apworld = tmp_path / "smbwonder.apworld"
    _make_fake_apworld(apworld)
    setup_root = apworld / "smbwonder" / "_setup"
    dst = tmp_path / "appdata-bundled"

    opens: list[Path] = []
    real_zipfile = _zf.ZipFile

    def tracking_zipfile(path, *a, **kw):
        opens.append(Path(path))
        return real_zipfile(path, *a, **kw)

    monkeypatch.setattr(B.zipfile, "ZipFile", tracking_zipfile)
    monkeypatch.setattr(B, "_extracted_bundled_root", None)

    B._extract_bundled_tree(setup_root, dst_override=dst)
    first_count = len(opens)
    # Second invocation: should skip the extraction entirely.
    monkeypatch.setattr(B, "_extracted_bundled_root", None)   # bypass in-process memo
    B._extract_bundled_tree(setup_root, dst_override=dst)
    assert len(opens) == first_count   # no fresh ZipFile open


def test_extract_bundled_tree_reextracts_when_source_mtime_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the user drops a newer .apworld in, mtime mismatch must
    trigger re-extraction (otherwise the wizard serves stale sources)."""
    import os
    apworld = tmp_path / "smbwonder.apworld"
    _make_fake_apworld(apworld)
    setup_root = apworld / "smbwonder" / "_setup"
    dst = tmp_path / "appdata-bundled"

    monkeypatch.setattr(B, "_extracted_bundled_root", None)
    B._extract_bundled_tree(setup_root, dst_override=dst)
    first_mtime = (dst / ".source-zip-mtime").read_text(encoding="utf-8")

    # Touch the apworld so its mtime advances.
    new_mtime = apworld.stat().st_mtime + 100
    os.utime(apworld, (new_mtime, new_mtime))

    monkeypatch.setattr(B, "_extracted_bundled_root", None)
    B._extract_bundled_tree(setup_root, dst_override=dst)
    second_mtime = (dst / ".source-zip-mtime").read_text(encoding="utf-8")
    assert first_mtime != second_mtime


def test_extract_bundled_tree_rejects_zip_with_no_switch_mod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A .apworld built without --bundle-mod has no switch-mod files
    under the bundled prefix.  Must raise FileNotFoundError, not
    silently extract an empty tree."""
    apworld = tmp_path / "smbwonder.apworld"
    _make_fake_apworld(apworld, with_switch_mod=False)
    setup_root = apworld / "smbwonder" / "_setup"
    monkeypatch.setattr(B, "_extracted_bundled_root", None)
    with pytest.raises(FileNotFoundError, match="no files under switch_mod/"):
        B._extract_bundled_tree(setup_root, dst_override=tmp_path / "n/a")


def test_bundled_switch_mod_available_true_in_dev_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dev clone with a real switch-mod/CMakeLists.txt: probe returns
    True without trying to find an apworld zip."""
    (tmp_path / "switch-mod").mkdir()
    (tmp_path / "switch-mod" / "CMakeLists.txt").write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "is_dev_clone", lambda: True)
    monkeypatch.setattr(B, "repo_root", lambda: tmp_path)
    assert B.bundled_switch_mod_available() is True


def test_bundled_switch_mod_available_false_for_loose_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not a dev clone AND no .apworld ancestor: nothing to build from."""
    monkeypatch.setattr(B, "is_dev_clone", lambda: False)
    monkeypatch.setattr(B, "_SETUP_ROOT", tmp_path / "loose")
    (tmp_path / "loose").mkdir()
    assert B.bundled_switch_mod_available() is False


def test_resolve_repo_uses_dev_root_when_in_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """In a dev clone, _resolve_repo(None) returns the git root --
    matches the original switch_mod_root(None) behaviour."""
    monkeypatch.setattr(B, "is_dev_clone", lambda: True)
    monkeypatch.setattr(B, "repo_root", lambda: tmp_path / "dev")
    assert B._resolve_repo(None) == tmp_path / "dev"


def test_resolve_repo_extracts_bundle_when_not_in_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Packaged install: _resolve_repo(None) triggers extraction."""
    monkeypatch.setattr(B, "is_dev_clone", lambda: False)

    fake_bundle = tmp_path / "bundle-root"
    fake_bundle.mkdir()
    monkeypatch.setattr(B, "_extract_bundled_tree", lambda: fake_bundle)
    assert B._resolve_repo(None) == fake_bundle


def test_resolve_repo_respects_explicit_arg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tests pass an explicit `repo=tmp_path` to point at synthetic
    layouts; the dispatcher must NOT override that."""
    monkeypatch.setattr(B, "is_dev_clone", lambda: False)
    assert B._resolve_repo(tmp_path) == tmp_path
