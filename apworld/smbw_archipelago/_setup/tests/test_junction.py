"""Tests for the apworld junction installer."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from apworld.smbw_archipelago._setup import junction as J


def _make_fake_repo(tmp_path: Path) -> Path:
    """Build a minimal repo skeleton: <repo>/apworld/smbw_archipelago/ +
    <repo>/vendor/Archipelago/. install_junction probes these paths."""
    (tmp_path / "apworld" / "smbw_archipelago").mkdir(parents=True)
    (tmp_path / "apworld" / "smbw_archipelago" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "vendor" / "Archipelago").mkdir(parents=True)
    return tmp_path


def test_install_junction_creates_link(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path)
    runs: list[list[str]] = []

    def fake_runner(cmd: list[str]) -> tuple[int, str, str]:
        runs.append(cmd)
        # Materialize the target so `is_reparse_point` returns True next time
        # (the real cmd /c mklink would do this).
        target = Path(cmd[-2])
        target.mkdir(parents=True, exist_ok=True)
        return 0, "Junction created", ""

    if sys.platform == "win32":
        result = J.install_junction(repo, runner=fake_runner)
        assert result.ok is True
        assert result.action == "created"
        # Runner saw the mklink invocation.
        assert runs and runs[0][:4] == ["cmd", "/c", "mklink", "/J"]
        assert "smbw_archipelago" in runs[0][-2]
    else:
        # POSIX: uses os.symlink, no runner call.
        result = J.install_junction(repo)
        assert result.ok is True
        assert result.action == "created"
        assert J.junction_target(repo).is_symlink()


def test_install_junction_idempotent_on_existing_link(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path)
    target = J.junction_target(repo)
    source = J.apworld_source(repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Create a real link first.
    if sys.platform == "win32":
        # Use Python's own subprocess to create the real junction.
        import subprocess
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target), str(source)],
            check=True, capture_output=True,
        )
    else:
        os.symlink(source, target, target_is_directory=True)

    # Now re-running should be a no-op.
    result = J.install_junction(repo)
    assert result.ok is True
    assert result.action == "already_exists"


def test_install_junction_refuses_non_link_directory(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path)
    target = J.junction_target(repo)
    target.mkdir(parents=True)
    (target / "real-file").write_text("", encoding="utf-8")

    result = J.install_junction(repo)
    assert result.ok is False
    assert result.action == "error"
    assert "non-junction" in result.message


def test_install_junction_fails_when_source_missing(tmp_path: Path) -> None:
    (tmp_path / "vendor" / "Archipelago").mkdir(parents=True)
    # apworld/smbw_archipelago/ is intentionally missing.
    result = J.install_junction(tmp_path)
    assert result.ok is False
    assert result.action == "error"
    assert "apworld source not found" in result.message


def test_install_junction_creates_custom_worlds_parent(tmp_path: Path) -> None:
    """vendor/Archipelago exists but custom_worlds doesn't yet."""
    (tmp_path / "apworld" / "smbw_archipelago").mkdir(parents=True)
    (tmp_path / "vendor" / "Archipelago").mkdir(parents=True)
    # No custom_worlds yet.
    assert not (tmp_path / "vendor" / "Archipelago" / "custom_worlds").exists()

    if sys.platform == "win32":
        result = J.install_junction(tmp_path, runner=lambda cmd: (
            (Path(cmd[-2]).mkdir(parents=True, exist_ok=True), (0, "", ""))[1]
        ))
    else:
        result = J.install_junction(tmp_path)
    assert result.ok is True
    assert (tmp_path / "vendor" / "Archipelago" / "custom_worlds").is_dir()


@pytest.mark.skipif(sys.platform != "win32", reason="mklink-specific")
def test_install_junction_surfaces_dev_mode_hint_on_access_denied(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path)
    def fake_runner(_cmd: list[str]) -> tuple[int, str, str]:
        return 1, "", "ERROR: Access is denied."
    result = J.install_junction(repo, runner=fake_runner)
    assert result.ok is False
    assert "Developer Mode" in result.message
