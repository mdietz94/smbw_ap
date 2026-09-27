"""Tests for the subsdk build-identity helpers (`_setup/build_id.py`).

Pure-Python half of the build-id plumbing: the grammar shared with
switch-mod/cmake/BuildId.cmake, the bundle VERSION round-trip, and the
stale-subsdk9 warning the client logs on Hello.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

from apworld.smbw_archipelago._setup import build_id as B


# ---------------------------------------------------------------------------
# parse / compare
# ---------------------------------------------------------------------------


def test_parse_dev_build_id() -> None:
    p = B.parse_mod_ver("g3f9a1c2-dirty@20260921")
    assert p.stamped is True
    assert p.source == "g3f9a1c2"
    assert p.dirty is True
    assert p.build_date == "20260921"
    assert p.known is True


def test_parse_release_bundle_build_id() -> None:
    p = B.parse_mod_ver("0.2.0+g3f9a1c2@20260921")
    assert p.source == "0.2.0+g3f9a1c2"
    assert p.dirty is False
    assert p.build_date == "20260921"


def test_parse_legacy_mod_ver_is_unstamped() -> None:
    """Pre-2026-09 builds send the hand-maintained tag; no '@' = legacy."""
    p = B.parse_mod_ver("smbwap-m4")
    assert p.stamped is False
    assert p.known is False
    assert p.source == "smbwap-m4"


def test_parse_unknown_source_is_stamped_but_not_known() -> None:
    p = B.parse_mod_ver("unknown@20260921")
    assert p.stamped is True
    assert p.known is False


def test_source_key_strips_dirty_only() -> None:
    assert B.source_key("g1234567-dirty") == "g1234567"
    assert B.source_key("0.2.0+g1234567") == "0.2.0+g1234567"
    assert B.source_key("") == ""


def test_mismatch_warning_none_when_sources_agree() -> None:
    assert B.mod_ver_mismatch_warning("g1234567@20260921", "g1234567") is None
    # Dirty flag can flip between build and run -- not a mismatch.
    assert B.mod_ver_mismatch_warning("g1234567-dirty@20260921", "g1234567") is None
    assert B.mod_ver_mismatch_warning("g1234567@20260921", "g1234567-dirty") is None


def test_mismatch_warning_on_different_hash() -> None:
    w = B.mod_ver_mismatch_warning("g1234567@20260901", "gabcdef0")
    assert w is not None
    assert "does not match" in w
    assert "g1234567@20260901" in w and "gabcdef0" in w


def test_mismatch_warning_for_legacy_build_regardless_of_expected() -> None:
    w = B.mod_ver_mismatch_warning("smbwap-m4", None)
    assert w is not None
    assert "before build-id stamping" in w
    assert "smbwap-m4" in w


def test_mismatch_warning_quiet_when_nothing_to_compare() -> None:
    # Plain-folder install: the client has no expected id.
    assert B.mod_ver_mismatch_warning("g1234567@20260921", None) is None
    # Mod built with neither git nor VERSION: nothing to compare against.
    assert B.mod_ver_mismatch_warning("unknown@20260921", "g1234567") is None


# ---------------------------------------------------------------------------
# sanitize / world_version / bundle id
# ---------------------------------------------------------------------------


def test_sanitize_source_id_matches_cmake_policy() -> None:
    assert B.sanitize_source_id(" 0.2.0+g12ab-dirty\n") == "0.2.0+g12ab-dirty"
    assert B.sanitize_source_id('bad"chars @here') == "badcharshere"
    assert B.sanitize_source_id("") == B.UNKNOWN
    assert len(B.sanitize_source_id("x" * 100)) == B.MAX_SOURCE_ID_LEN


def test_read_world_version(tmp_path: Path) -> None:
    (tmp_path / "archipelago.json").write_text('{"world_version": "0.3.1"}\n', encoding="utf-8")
    assert B.read_world_version(tmp_path) == "0.3.1"


def test_read_world_version_missing_or_bad(tmp_path: Path) -> None:
    assert B.read_world_version(tmp_path) is None
    (tmp_path / "archipelago.json").write_text("not json", encoding="utf-8")
    assert B.read_world_version(tmp_path) is None
    (tmp_path / "archipelago.json").write_text('{"world_version": ""}', encoding="utf-8")
    assert B.read_world_version(tmp_path) is None


def _fake_repo(tmp_path: Path, world_version: str | None) -> Path:
    apworld = tmp_path / "apworld" / "smbw_archipelago"
    apworld.mkdir(parents=True)
    if world_version is not None:
        (apworld / "archipelago.json").write_text(
            '{"world_version": "%s"}' % world_version, encoding="utf-8")
    return tmp_path


def test_bundle_source_id_combines_version_and_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _fake_repo(tmp_path, "0.2.0")
    monkeypatch.setattr(B, "git_source_id", lambda *_a, **_kw: "g1234567-dirty")
    assert B.bundle_source_id(repo) == "0.2.0+g1234567-dirty"


def test_bundle_source_id_falls_back_to_whichever_is_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _fake_repo(tmp_path, "0.2.0")
    monkeypatch.setattr(B, "git_source_id", lambda *_a, **_kw: None)
    assert B.bundle_source_id(repo) == "0.2.0"

    repo2 = _fake_repo(tmp_path / "two", None)
    monkeypatch.setattr(B, "git_source_id", lambda *_a, **_kw: "gabcdef0")
    assert B.bundle_source_id(repo2) == "gabcdef0"

    repo3 = _fake_repo(tmp_path / "three", None)
    monkeypatch.setattr(B, "git_source_id", lambda *_a, **_kw: None)
    assert B.bundle_source_id(repo3) == B.UNKNOWN


def test_git_source_id_on_non_repo_is_none(tmp_path: Path) -> None:
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    assert B.git_source_id(empty) is None


def test_git_source_id_on_this_checkout_has_expected_shape() -> None:
    """Live check against the repo the tests run from.  Skipped when the
    tests run from an extracted tarball / no git."""
    repo = Path(__file__).resolve().parents[4]
    if not (repo / ".git").exists():
        pytest.skip("not a git checkout")
    sid = B.git_source_id(repo)
    if sid is None:
        pytest.skip("git not available")
    assert re.fullmatch(r"g[0-9a-f]{7,}(-dirty)?", sid), sid


# ---------------------------------------------------------------------------
# bundle VERSION round-trip + expected_mod_source_id dispatch
# ---------------------------------------------------------------------------


def _make_apworld_zip(zip_path: Path, version: str | None) -> None:
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("smbwonder/__init__.py", "")
        zf.writestr("smbwonder/_setup/__init__.py", "")
        zf.writestr("smbwonder/_setup/switch-mod/CMakeLists.txt", "project(x)\n")
        if version is not None:
            zf.writestr(B.BUNDLE_VERSION_ARCNAME, version + "\n")


def test_read_bundle_version_round_trip(tmp_path: Path) -> None:
    zp = tmp_path / "smbwonder.apworld"
    _make_apworld_zip(zp, "0.2.0+g1234567")
    assert B.read_bundle_version(zp) == "0.2.0+g1234567"


def test_read_bundle_version_absent_or_bad_zip(tmp_path: Path) -> None:
    zp = tmp_path / "smbwonder.apworld"
    _make_apworld_zip(zp, None)
    assert B.read_bundle_version(zp) is None
    bad = tmp_path / "bad.apworld"
    bad.write_bytes(b"PK\x03\x04 truncated")
    assert B.read_bundle_version(bad) is None
    assert B.read_bundle_version(tmp_path / "missing.apworld") is None


def test_bundle_version_arcname_matches_extraction_layout() -> None:
    """build.py extracts everything under ``smbwonder/_setup/`` so the
    VERSION file must land at ``<bundle>/switch-mod/VERSION`` -- the path
    cmake/BuildId.cmake reads (``${CMAKE_SOURCE_DIR}/VERSION``)."""
    assert B.BUNDLE_VERSION_ARCNAME == "smbwonder/_setup/switch-mod/VERSION"


def test_expected_mod_source_id_dev_checkout_uses_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    setup_root = repo / "apworld" / "smbw_archipelago" / "_setup"
    setup_root.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")  # worktree shape
    seen: list[Path] = []

    def fake_git(path: Path, **_kw: object) -> str:
        seen.append(path)
        return "g7654321"

    monkeypatch.setattr(B, "git_source_id", fake_git)
    assert B.expected_mod_source_id(setup_root) == "g7654321"
    assert seen == [repo]


def test_expected_mod_source_id_packaged_install_reads_zip(tmp_path: Path) -> None:
    zp = tmp_path / "smbwonder.apworld"
    _make_apworld_zip(zp, "0.2.0+gabcdef0")
    setup_root = zp / "smbwonder" / "_setup"   # zipimport-style path
    assert B.expected_mod_source_id(setup_root) == "0.2.0+gabcdef0"


def test_expected_mod_source_id_plain_folder_is_none(tmp_path: Path) -> None:
    setup_root = tmp_path / "custom_worlds" / "smbwonder" / "_setup"
    setup_root.mkdir(parents=True)
    assert B.expected_mod_source_id(setup_root) is None
