"""(stage_key, charaType) -> AP location name for character-block sanity.

The character blocks are the player-specific hidden
``ObjectBlockClarityCharacter`` actors (e.g. the Search-Party blocks):
each placed instance carries a ``PlayerCharaType`` (0-11) and only the
matching character can materialize and bump it.  So a check resolves as
``(current course, hitting character)``.

The table is GENERATED offline from the RomFS banc data by
``scripts/romfs/build_charblock_table.py`` and checked in as
``char_block_table.json`` next to this module -- the same
generated-and-committed pattern as ``location_table``.  We load it
zip-safe via ``pkgutil.get_data`` (NOT ``os.path``/``open``): the
standalone Archipelago install loads the world straight from the zipped
``.apworld`` without extracting, so a plain ``open()`` resolves to a path
*inside* the zip and fails.  Mirrors ``badge_table.all_badge_item_names``.

v1 collapse: (course, charaType) is unique everywhere except Course450 /
451 / 453 / 850 (a handful of Search-Party-style specials with multiple
blocks of the same character).  The generator collapses those to one
check per (course, charaType), so this table has ~154 rows.  Refining the
collapsed courses to per-block checks needs the Switch hook to ship the
block position (reserved field in the wire event, zeros in v1).

PlayerCharaType 0-11 -> character name is the roster-order mapping in the
generator (CHARA_NAMES) -- PROVISIONAL on the enum->name assignment (only
the array shape is confirmed from live PlayReport ``chara_type_array``
captures); the location NAMES bake the mapping in, but the lookup key here
is the raw (stage_key, chara) int pair, so a future name correction
doesn't renumber any AP location.
"""

from __future__ import annotations

import json
import logging
import pkgutil
from typing import Final


log = logging.getLogger("SMBW")


# __package__ here is "<root>.client"; char_block_table.json sits in this
# same `client` subpackage.
_PKG: Final[str] = __package__ or "apworld.smbw_archipelago.client"


def _load() -> dict[tuple[int, int], str]:
    try:
        raw = pkgutil.get_data(_PKG, "char_block_table.json")
        if raw is None:
            raise FileNotFoundError("char_block_table.json not in package")
        rows = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as e:
        log.warning(
            "char_block_table: can't read char_block_table.json from "
            "package %r: %s -- character-block sanity disabled", _PKG, e)
        return {}
    out: dict[tuple[int, int], str] = {}
    for r in rows:
        try:
            key = (int(r["stage_key"]) & 0xFFFFFFFF, int(r["chara"]))
            out[key] = str(r["name"])
        except (KeyError, TypeError, ValueError):
            log.warning("char_block_table: skipping malformed row %r", r)
    return out


# (stage_key, chara) -> AP location name.  Built once at import.
_TABLE: Final[dict[tuple[int, int], str]] = _load()


def lookup(stage_key: int, chara: int) -> str | None:
    """Resolve a (course stage_key, hitting charaType) to its AP location
    name, or ``None`` if no character block exists for that pair (the
    common case -- the GetDamageReactionPlayerNo hook over-fires on every
    block-damage resolve, and only real character-block courses map)."""
    return _TABLE.get((int(stage_key) & 0xFFFFFFFF, int(chara)))


def charas_for_stage(stage_key: int) -> set[int]:
    """The set of charaTypes that have a character block in ``stage_key``.
    Used by the processor's fallback path: when the Switch couldn't
    resolve the hitting character, but the current course has exactly one
    character block, the single chara is unambiguous."""
    sk = int(stage_key) & 0xFFFFFFFF
    return {chara for (s, chara) in _TABLE if s == sk}


def row_count() -> int:
    """Number of (course, chara) checks in the table (diagnostics/tests)."""
    return len(_TABLE)
