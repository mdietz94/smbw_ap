"""Tests for the `.smbwap` file (read/write/parse + launch-arg expansion)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from apworld.smbw_archipelago._setup.smbwap_file import (
    GAME_NAME, SMBWAP_METADATA_ENTRY, SMBWAP_SCHEMA_VERSION,
    SmbwapFile, parse_smbwap, smbwap_to_launch_args,
)


def test_write_then_parse_roundtrips(tmp_path: Path) -> None:
    f = SmbwapFile(slot_name="Mario", seed_name="abc123",
                   server_address="archipelago.gg:38281", password="hunter2")
    out = tmp_path / "test.smbwap"
    f.write(out)
    assert out.is_file()
    # Should be a zip with metadata.json inside.
    with zipfile.ZipFile(out) as zf:
        assert SMBWAP_METADATA_ENTRY in zf.namelist()
    parsed = parse_smbwap(out)
    assert parsed.slot_name == "Mario"
    assert parsed.seed_name == "abc123"
    assert parsed.server_address == "archipelago.gg:38281"
    assert parsed.password == "hunter2"
    assert parsed.game == GAME_NAME
    assert parsed.version == SMBWAP_SCHEMA_VERSION


def test_parse_accepts_bare_json_for_back_compat(tmp_path: Path) -> None:
    """Alpha builds wrote raw-JSON .smbwap files. The reader sniffs the
    magic bytes so legacy files keep working."""
    raw = json.dumps({
        "game": GAME_NAME,
        "version": 1,
        "slot_name": "Mario",
        "seed_name": "",
        "server_address": "",
        "password": "",
    })
    out = tmp_path / "legacy.smbwap"
    out.write_text(raw, encoding="utf-8")
    parsed = parse_smbwap(out)
    assert parsed.slot_name == "Mario"


def test_parse_rejects_wrong_game(tmp_path: Path) -> None:
    out = tmp_path / "wrong-game.smbwap"
    out.write_text(json.dumps({
        "game": "Some Other Game",
        "version": 1,
        "slot_name": "Mario",
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="not for"):
        parse_smbwap(out)


def test_parse_rejects_future_version(tmp_path: Path) -> None:
    out = tmp_path / "future.smbwap"
    out.write_text(json.dumps({
        "game": GAME_NAME,
        "version": 999,
        "slot_name": "Mario",
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="newer than client supports"):
        parse_smbwap(out)


def test_parse_rejects_empty_slot_name(tmp_path: Path) -> None:
    out = tmp_path / "no-slot.smbwap"
    out.write_text(json.dumps({
        "game": GAME_NAME, "version": 1, "slot_name": "",
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="slot_name"):
        parse_smbwap(out)


def test_parse_rejects_malformed_json(tmp_path: Path) -> None:
    out = tmp_path / "junk.smbwap"
    out.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        parse_smbwap(out)


def test_smbwap_to_launch_args_minimal() -> None:
    s = SmbwapFile(slot_name="Mario")
    args = smbwap_to_launch_args(s)
    assert args == ["--name", "Mario"]


def test_smbwap_to_launch_args_full() -> None:
    s = SmbwapFile(slot_name="Mario",
                   server_address="archipelago.gg:38281",
                   password="hunter2")
    args = smbwap_to_launch_args(s)
    assert args == [
        "--name", "Mario",
        "--connect", "archipelago.gg:38281",
        "--password", "hunter2",
    ]


def test_smbwap_to_launch_args_omits_empty_fields() -> None:
    """An empty server_address means "ask via Connect bar"; we omit the
    flag rather than passing `--connect ''` (SMBW Client treats empty
    string as "explicitly nothing", not "prompt")."""
    s = SmbwapFile(slot_name="Mario", password="hunter2")
    args = smbwap_to_launch_args(s)
    assert "--connect" not in args
    assert "--password" in args
