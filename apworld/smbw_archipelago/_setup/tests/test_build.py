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
    assert arts["subsdk9"] == tmp_path / "switch-mod" / "build" / "subsdk9"
    assert arts["main.npdm"] == tmp_path / "switch-mod" / "build" / "subsdk9.npdm"


def test_compose_build_env_sets_devkitpro(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(B, "resolved_devkitpro_root", lambda: str(tmp_path / "devkitPro"))
    monkeypatch.setattr(B, "resolved_ninja_bin", lambda: None)
    (tmp_path / "devkitPro" / "msys2" / "usr" / "bin").mkdir(parents=True)
    env = B._compose_build_env()
    assert env["DEVKITPRO"] == str(tmp_path / "devkitPro")
    expected_msys2 = str(tmp_path / "devkitPro" / "msys2" / "usr" / "bin")
    assert expected_msys2 in env["PATH"]
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_compose_build_env_prepends_ninja(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ninja_bin = str(tmp_path / "ninja-dir")
    monkeypatch.setattr(B, "resolved_devkitpro_root", lambda: None)
    monkeypatch.setattr(B, "resolved_ninja_bin", lambda: ninja_bin)
    env = B._compose_build_env()
    assert ninja_bin in env["PATH"].split(os.pathsep)


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
        (bd / "subsdk9").write_bytes(b"x")
        (bd / "subsdk9.npdm").write_bytes(b"y")
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
        (bd / "subsdk9").write_bytes(b"x")
        (bd / "subsdk9.npdm").write_bytes(b"y")
        return B.BuildResult(True, 0, "")

    monkeypatch.setattr(B, "cmake_configure", fake_configure)
    monkeypatch.setattr(B, "cmake_build", fake_build)

    outcome = B.run_build_phase(skip_configure_if_ready=True)
    assert outcome.ok is True
    assert saw_configure == [True]


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
        (bd / "subsdk9").write_bytes(b"")          # empty
        (bd / "subsdk9.npdm").write_bytes(b"x")
        return B.BuildResult(True, 0, "")

    monkeypatch.setattr(B, "cmake_configure", lambda **_k: B.BuildResult(True, 0, ""))
    monkeypatch.setattr(B, "cmake_build", fake_build)

    outcome = B.run_build_phase()
    assert outcome.ok is False


def test_cmake_configure_fails_without_toolchain(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    """When toolchain.cmake doesn't exist we must fail clean, not crash
    inside cmake."""
    monkeypatch.setattr(B, "repo_root", lambda: tmp_path)
    (tmp_path / "switch-mod").mkdir()
    # No cmake/toolchain.cmake.
    r = B.cmake_configure()
    assert r.ok is False
    assert "toolchain" in r.log.lower()


def test_cmake_build_fails_without_build_dir(monkeypatch: pytest.MonkeyPatch,
                                              tmp_path: Path) -> None:
    monkeypatch.setattr(B, "repo_root", lambda: tmp_path)
    # No switch-mod/build/.
    r = B.cmake_build()
    assert r.ok is False
    assert "build dir missing" in r.log
