"""Tests for the apworld junction installer."""

from __future__ import annotations

import os
import subprocess
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


def _materialize_target(cmd: list[str]) -> tuple[int, str, str]:
    """Windows mklink runner shim: create the target dir so subsequent
    ``is_reparse_point`` calls behave like the real junction would."""
    target = Path(cmd[-2])
    target.mkdir(parents=True, exist_ok=True)
    return 0, "Junction created", ""


def _make_real_link(target: Path, source: Path) -> None:
    """Create an actual junction (Windows) or symlink (POSIX) at ``target``
    pointing to ``source``.  Used to seed the "junction already exists"
    state for idempotency / replacement tests."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target), str(source)],
            check=True, capture_output=True,
        )
    else:
        os.symlink(source, target, target_is_directory=True)


def test_install_junction_creates_link(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path)
    runs: list[list[str]] = []

    def fake_runner(cmd: list[str]) -> tuple[int, str, str]:
        runs.append(cmd)
        return _materialize_target(cmd)

    if sys.platform == "win32":
        result = J.install_junction(repo, vendor_repo=repo, runner=fake_runner)
        assert result.ok is True
        assert result.action == "created"
        # Runner saw the mklink invocation.
        assert runs and runs[0][:4] == ["cmd", "/c", "mklink", "/J"]
        assert "smbw_archipelago" in runs[0][-2]
    else:
        # POSIX: uses os.symlink, no runner call.
        result = J.install_junction(repo, vendor_repo=repo)
        assert result.ok is True
        assert result.action == "created"
        assert J.junction_target(repo).is_symlink()


def test_install_junction_idempotent_on_existing_link(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path)
    target = J.junction_target(repo)
    source = J.apworld_source(repo)
    _make_real_link(target, source)

    # Now re-running should be a no-op.
    result = J.install_junction(repo, vendor_repo=repo)
    assert result.ok is True
    assert result.action == "already_exists"


def test_install_junction_refuses_non_link_directory(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path)
    target = J.junction_target(repo)
    target.mkdir(parents=True)
    (target / "real-file").write_text("", encoding="utf-8")

    result = J.install_junction(repo, vendor_repo=repo)
    assert result.ok is False
    assert result.action == "error"
    assert "non-junction" in result.message


def test_install_junction_fails_when_source_missing(tmp_path: Path) -> None:
    (tmp_path / "vendor" / "Archipelago").mkdir(parents=True)
    # apworld/smbw_archipelago/ is intentionally missing.
    result = J.install_junction(tmp_path, vendor_repo=tmp_path)
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
        result = J.install_junction(tmp_path, vendor_repo=tmp_path, runner=_materialize_target)
    else:
        result = J.install_junction(tmp_path, vendor_repo=tmp_path)
    assert result.ok is True
    assert (tmp_path / "vendor" / "Archipelago" / "custom_worlds").is_dir()


@pytest.mark.skipif(sys.platform != "win32", reason="mklink-specific")
def test_install_junction_surfaces_dev_mode_hint_on_access_denied(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path)
    def fake_runner(_cmd: list[str]) -> tuple[int, str, str]:
        return 1, "", "ERROR: Access is denied."
    result = J.install_junction(repo, vendor_repo=repo, runner=fake_runner)
    assert result.ok is False
    assert "Developer Mode" in result.message


# ---------------------------------------------------------------------------
# Worktree asymmetry: source = current worktree, target = primary clone.

def test_install_junction_targets_vendor_repo_separately_from_source(tmp_path: Path) -> None:
    """When invoked from a worktree, the junction's *source* is the
    worktree's apworld but its *location* is the primary clone's
    ``vendor/Archipelago/custom_worlds/``.  Submodules don't carry into
    git worktrees, so the worktree-side vendor dir is empty and AP
    Launcher reads from the primary."""
    worktree = tmp_path / "worktrees" / "feature"
    primary = tmp_path / "primary"
    _make_fake_repo(worktree)
    _make_fake_repo(primary)

    if sys.platform == "win32":
        result = J.install_junction(worktree, vendor_repo=primary,
                                    runner=_materialize_target)
    else:
        result = J.install_junction(worktree, vendor_repo=primary)
    assert result.ok is True
    assert result.action == "created"
    # Target landed in the primary's vendor dir, NOT the worktree's.
    assert result.target == J.junction_target(primary)
    assert not (worktree / "vendor" / "Archipelago"
                / "custom_worlds" / "smbw_archipelago").exists()
    # Source resolves to the worktree's apworld (not the primary's).
    assert result.source == J.apworld_source(worktree)


# ---------------------------------------------------------------------------
# Stale-link replacement (the M-26 redeploy gap that motivated this work).

def test_install_junction_replaces_dangling_link(tmp_path: Path) -> None:
    """A junction pointing into a removed worktree (or any vanished
    target) is dangling: ``Path.exists()`` returns False on the link
    itself but the directory entry still occupies the spot.
    Re-running install must detect and replace, not error out with
    ``non-junction directory``."""
    repo = _make_fake_repo(tmp_path)
    # Seed a link pointing at a dir we then delete -- a real dangling
    # junction.  Using a sibling dir keeps the link's *target name*
    # different from the install's intended source, so a successful
    # repoint definitely went through.
    dead_target = tmp_path / "removed-worktree-apworld"
    dead_target.mkdir()
    target = J.junction_target(repo)
    _make_real_link(target, dead_target)
    # Pull the rug out from under the link.
    dead_target.rmdir()

    if sys.platform == "win32":
        result = J.install_junction(repo, vendor_repo=repo,
                                    runner=_materialize_target)
    else:
        result = J.install_junction(repo, vendor_repo=repo)
    assert result.ok is True
    assert result.action == "replaced"
    assert "replaced" in result.message.lower()


def test_install_junction_repoints_link_to_different_source(tmp_path: Path) -> None:
    """A junction pointing at a *different* (still-existing) source --
    typical case: the user moved between two worktrees and wants the
    junction repointed -- gets rewritten, not left alone."""
    repo = _make_fake_repo(tmp_path)
    other_apworld = tmp_path / "other-worktree" / "apworld" / "smbw_archipelago"
    other_apworld.mkdir(parents=True)
    target = J.junction_target(repo)
    _make_real_link(target, other_apworld)
    assert J.is_reparse_point(target)

    if sys.platform == "win32":
        result = J.install_junction(repo, vendor_repo=repo,
                                    runner=_materialize_target)
    else:
        result = J.install_junction(repo, vendor_repo=repo)
    assert result.ok is True
    assert result.action == "replaced"


def test_install_junction_already_exists_when_link_matches_source(tmp_path: Path) -> None:
    """The matched-source case stays a no-op (not "replaced") so the
    common idempotent re-run doesn't churn the link."""
    repo = _make_fake_repo(tmp_path)
    _make_real_link(J.junction_target(repo), J.apworld_source(repo))
    result = J.install_junction(repo, vendor_repo=repo)
    assert result.ok is True
    assert result.action == "already_exists"
