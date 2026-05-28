"""Tests for the deploy phase.

Uses temp files to exercise the real copy + size-assertion logic
without depending on a real Ryujinx install.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apworld.smbw_archipelago._setup import deploy as D


def _make_build_outputs(tmp_path: Path) -> dict[str, Path]:
    """Synthesize the two artifacts LibHakkun's build places in build/exefs/."""
    exefs = tmp_path / "build" / "exefs"
    exefs.mkdir(parents=True)
    subsdk = exefs / "subsdk9"
    npdm = exefs / "main.npdm"
    subsdk.write_bytes(b"FAKE_SUBSDK9_CONTENT" * 100)
    npdm.write_bytes(b"FAKE_NPDM" * 100)
    return {"subsdk9": subsdk, "main.npdm": npdm}


def test_ryujinx_layout_includes_title_id_and_mod_name(tmp_path: Path) -> None:
    layout = D._ryujinx_layout(tmp_path)
    assert D.SMBW_TITLE_ID in str(layout["subsdk9"])
    assert D.RYU_MOD_NAME in str(layout["subsdk9"])
    # Both artifacts go to the same exefs dir.
    assert layout["subsdk9"].parent == layout["main.npdm"].parent
    assert layout["main.npdm"].name == "main.npdm"


def test_deploy_to_ryujinx_copies_artifacts(tmp_path: Path) -> None:
    outputs = _make_build_outputs(tmp_path)
    ryu_root = tmp_path / "ryujinx"
    ryu_root.mkdir()
    result = D.deploy_to_ryujinx(ryu_root, outputs)
    assert result.ok is True
    assert len(result.files) == 2
    for src, dst in result.files:
        assert dst.is_file()
        assert dst.stat().st_size == src.stat().st_size
    exefs = ryu_root / "mods" / "contents" / D.SMBW_TITLE_ID / D.RYU_MOD_NAME / "exefs"
    assert (exefs / "subsdk9").is_file()
    assert (exefs / "main.npdm").is_file()


def test_deploy_to_ryujinx_creates_directory_tree(tmp_path: Path) -> None:
    outputs = _make_build_outputs(tmp_path)
    ryu_root = tmp_path / "ryujinx"
    # Don't create the mods/contents/... tree; deploy must do it.
    ryu_root.mkdir()
    result = D.deploy_to_ryujinx(ryu_root, outputs)
    assert result.ok is True


def test_deploy_to_ryujinx_surfaces_missing_source(tmp_path: Path) -> None:
    """If the build output went missing between build and deploy (user
    cleaned mid-run), deploy must report it clearly rather than crash."""
    outputs = {
        "subsdk9": tmp_path / "does-not-exist" / "subsdk9",
        "main.npdm": tmp_path / "does-not-exist" / "main.npdm",
    }
    ryu_root = tmp_path / "ryujinx"
    ryu_root.mkdir()
    result = D.deploy_to_ryujinx(ryu_root, outputs)
    assert result.ok is False
    assert "unreadable" in result.error or "FileNotFoundError" in result.error


def test_copy_files_size_assertion_catches_truncated_write(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """If the post-copy stat shows a smaller dest than source, the deploy
    must fail with DeployCopyError."""
    src = tmp_path / "src"
    src.write_bytes(b"A" * 1000)
    dst = tmp_path / "dst"

    # Monkey-patch shutil.copy2 to write a truncated dest (simulates a
    # disconnected SD card or AV-induced partial write).
    def fake_copy2(s: Path, d: Path) -> None:
        d.write_bytes(b"A" * 500)   # half the bytes

    monkeypatch.setattr(D.shutil, "copy2", fake_copy2)
    with pytest.raises(D.DeployCopyError, match="Partial write"):
        D._copy_files({"f": src}, {"f": dst})


def test_deploy_to_custom_folder(tmp_path: Path) -> None:
    outputs = _make_build_outputs(tmp_path)
    custom = tmp_path / "stage"
    custom.mkdir()
    result = D.deploy_to_custom_folder(custom, outputs)
    assert result.ok is True
    # Custom folder uses the SD-card layout (atmosphere/contents/...).
    assert (custom / "atmosphere" / "contents" / D.SMBW_TITLE_ID / "exefs" / "subsdk9").is_file()


def test_detect_ryujinx_returns_none_when_appdata_missing(monkeypatch: pytest.MonkeyPatch,
                                                            tmp_path: Path) -> None:
    """On Windows, no %APPDATA% → None. On Linux, this is achieved by
    pointing HOME / XDG at an empty tmpdir with no Ryujinx subdir."""
    monkeypatch.setattr(D.sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    assert D.detect_ryujinx_path() is None


def test_detect_ryujinx_returns_path_when_dir_exists(monkeypatch: pytest.MonkeyPatch,
                                                      tmp_path: Path) -> None:
    monkeypatch.setattr(D.sys, "platform", "win32")
    fake_appdata = tmp_path / "appdata"
    (fake_appdata / "Ryujinx").mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(fake_appdata))
    assert D.detect_ryujinx_path() == fake_appdata / "Ryujinx"


# ---------------------------------------------------------------------------
# Linux Ryujinx detection — XDG_CONFIG_HOME / ~/.config / ~/.local/share.
# ---------------------------------------------------------------------------


def test_detect_ryujinx_on_linux_finds_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An explicit XDG_CONFIG_HOME override wins over ~/.config when both
    contain a Ryujinx dir."""
    monkeypatch.setattr(D.sys, "platform", "linux")
    xdg = tmp_path / "xdg-config"
    (xdg / "Ryujinx").mkdir(parents=True)
    # Also create ~/.config/Ryujinx so we can prove XDG wins.
    home = tmp_path / "home"
    (home / ".config" / "Ryujinx").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(D.Path, "home", staticmethod(lambda: home))
    assert D.detect_ryujinx_path() == xdg / "Ryujinx"


def test_detect_ryujinx_on_linux_falls_back_to_dot_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Without XDG_CONFIG_HOME, the detector probes ~/.config/Ryujinx."""
    monkeypatch.setattr(D.sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    home = tmp_path / "home"
    (home / ".config" / "Ryujinx").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(D.Path, "home", staticmethod(lambda: home))
    assert D.detect_ryujinx_path() == home / ".config" / "Ryujinx"


def test_detect_ryujinx_on_linux_falls_back_to_local_share(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """If neither XDG nor ~/.config has Ryujinx but ~/.local/share does
    (older portable / AppImage layout), the detector still finds it."""
    monkeypatch.setattr(D.sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    home = tmp_path / "home"
    (home / ".local" / "share" / "Ryujinx").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(D.Path, "home", staticmethod(lambda: home))
    assert D.detect_ryujinx_path() == home / ".local" / "share" / "Ryujinx"


def test_detect_ryujinx_on_linux_returns_none_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Empty home + no XDG override → None (deploy will report nothing
    detected and the user picks Custom or installs Ryujinx)."""
    monkeypatch.setattr(D.sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(D.Path, "home", staticmethod(lambda: home))
    assert D.detect_ryujinx_path() is None


def test_ryujinx_primary_default_on_linux_returns_dot_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When no Ryujinx dir exists yet, primary_default still returns the
    canonical path so the wizard's hint text can show where deploy
    *will* write."""
    monkeypatch.setattr(D.sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(D.Path, "home", staticmethod(lambda: home))
    assert D.ryujinx_primary_default() == home / ".config" / "Ryujinx"


def test_ryujinx_primary_default_on_linux_honors_xdg_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(D.sys, "platform", "linux")
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert D.ryujinx_primary_default() == xdg / "Ryujinx"


def test_deploy_to_ryujinx_layout_works_with_linux_root(tmp_path: Path) -> None:
    """The deploy layout helper is platform-agnostic — verify it produces
    a working artifact tree under a Linux-shaped Ryujinx root."""
    linux_root = tmp_path / ".config" / "Ryujinx"
    linux_root.mkdir(parents=True)
    outputs = _make_build_outputs(tmp_path)
    result = D.deploy_to_ryujinx(linux_root, outputs)
    assert result.ok is True
    written = linux_root / "mods" / "contents" / D.SMBW_TITLE_ID / D.RYU_MOD_NAME / "exefs" / "subsdk9"
    assert written.is_file()
