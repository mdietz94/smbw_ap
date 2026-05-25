"""(CheckKind, stage_key) -> AP location name table.

The processor emits ``CheckEmitted(kind, stage_key, ...)``; this table
turns those into the canonical AP location strings the multiworld
server expects.  Name resolution to numeric AP IDs happens via the
connected ``CommonContext.location_names_to_id`` at lookup time.

M4 ships W1 only and only the courses whose ``stage_key`` has been
captured live in a PlayReport fixture (``bridge/test_play_report.py``).
Other courses + worlds get added as their stage_keys come in.

Location names must match ``manual_smbwonder_zim/data/locations.json``
character-for-character.  Add new entries from grepping that file.

The table is hand-coded rather than generated from the apworld because:

- The apworld doesn't carry ``stage_key`` -- it's an SMBW runtime value
  and there's no other source of truth.
- We don't want a runtime dependency on the manual apworld's Python
  module; the bridge should run from a plain venv without Archipelago's
  full custom_worlds loader.
"""

from __future__ import annotations

import logging
from typing import Final

from .badge_table import _BADGES
from .protocol import CheckEmitted, CheckKind


log = logging.getLogger("SMBW")


# Stage keys captured from PlayReport fixtures.  These are the SMBW
# engine's per-course identifiers; they're stable across saves and
# (presumably) across players.  Source of truth: the ``stage_key``
# fields decoded from ``bridge/test_play_report.py``'s W1 captures.
_STAGE_W1_1: Final[int] = 2937190396   # Welcome to the Flower Kingdom!
_STAGE_W1_2: Final[int] = 232160011    # Piranha Plants on Parade
_STAGE_PIPEROCK_PALACE: Final[int] = 2308078743


# (CheckKind, stage_key) -> AP location name.
#
# Note: for CheckKind.BADGE_ACQUIRED, ``stage_key`` holds the badge bit
# position (== SMBW internal_id), not a course key.  See
# ``protocol.CheckEmitted.stage_key`` docstring for the per-kind
# semantics.  Badge entries are appended below from ``badge_table._BADGES``
# so adding a new internal_id mapping in one place flows through to
# both the inbound grant path and the outbound check path.
_TABLE: Final[dict[tuple[CheckKind, int], str]] = {
    # W1-1: Welcome to the Flower Kingdom!
    (CheckKind.NORMAL_EXIT, _STAGE_W1_1): "W1: Welcome to the Flower Kingdom! - Normal Exit",
    (CheckKind.TOP_OF_FLAG, _STAGE_W1_1): "W1: Welcome to the Flower Kingdom! - Top of Flag",
    (CheckKind.WONDER_SEED, _STAGE_W1_1): "W1: Welcome to the Flower Kingdom! - Wonder Seed",

    # W1-2: Piranha Plants on Parade
    (CheckKind.NORMAL_EXIT, _STAGE_W1_2): "W1: Piranha Plants on Parade - Normal Exit",
    (CheckKind.SECRET_EXIT, _STAGE_W1_2): "W1: Piranha Plants on Parade - Secret Exit",
    (CheckKind.TOP_OF_FLAG, _STAGE_W1_2): "W1: Piranha Plants on Parade - Top of Flag",
    (CheckKind.WONDER_SEED, _STAGE_W1_2): "W1: Piranha Plants on Parade - Wonder Seed",

    # Pipe-Rock Plateau Palace (W1 Royal Seed boss)
    (CheckKind.PALACE_CLEAR, _STAGE_PIPEROCK_PALACE): "W1: Pipe-Rock Plateau Palace - Royal Seed",
}


# M2.3 badge entries.  AP location names usually follow the pattern
# ``"<Badge Item Name> Obtained"``, but the apworld has one naming
# asymmetry: items.json calls the bubble badge "All Bubble Flower Badge"
# while locations.json calls the corresponding check
# "All Bubble Power Badge Obtained".  The override map below patches
# that single case; all 23 others follow the canonical pattern.
_BADGE_LOCATION_NAME_OVERRIDES: Final[dict[str, str]] = {
    "All Bubble Flower Badge": "All Bubble Power Badge Obtained",
}


def _populate_badge_entries() -> None:
    for name, bit, _confidence in _BADGES:
        loc = _BADGE_LOCATION_NAME_OVERRIDES.get(name, f"{name} Obtained")
        _TABLE[(CheckKind.BADGE_ACQUIRED, bit)] = loc


_populate_badge_entries()


# (stage_key, coin_index) -> AP location name for TEN_COIN checks.
# Each non-palace course has three "10 Coin" AP locations named
# #1/#2/#3.  The PlayReport ``big_flower_coin_course_in/out`` arrays
# carry per-course coin state at indices 0/1/2; whether those align
# with the apworld's #1/#2/#3 numbering is unverified — see
# docs/m2.2-runbook.md "Open question: array index vs apworld # numbering"
# for the blind-mapping MVP choice (index N → #N+1).
_TEN_COIN_TABLE: Final[dict[tuple[int, int], str]] = {
    (_STAGE_W1_1, 0): "W1: Welcome to the Flower Kingdom! - 10 Coin #1",
    (_STAGE_W1_1, 1): "W1: Welcome to the Flower Kingdom! - 10 Coin #2",
    (_STAGE_W1_1, 2): "W1: Welcome to the Flower Kingdom! - 10 Coin #3",

    (_STAGE_W1_2, 0): "W1: Piranha Plants on Parade - 10 Coin #1",
    (_STAGE_W1_2, 1): "W1: Piranha Plants on Parade - 10 Coin #2",
    (_STAGE_W1_2, 2): "W1: Piranha Plants on Parade - 10 Coin #3",

    # Pipe-Rock Plateau Palace is intentionally absent — palaces have
    # zero "Big Flower Coin" placements (the W1 palace fixture shows
    # big_flower_coin_course_in/out = [F,F,F]/[F,F,F]) and the apworld
    # has no "Palace - 10 Coin" locations.
}


def lookup_name(check: CheckEmitted) -> str | None:
    """Resolve a CheckEmitted to its canonical AP location name.

    Returns ``None`` when no mapping exists; the caller (ap_client)
    should log + drop.  Missing entries are normal early in M4 (most
    courses haven't been mapped yet) and should NOT cascade into an AP
    error -- the bridge stays connected and the player just doesn't
    get credit for the un-mapped course until the table extends.
    """
    if check.kind == CheckKind.TEN_COIN:
        idx = int(check.metadata.get("coin_index", 0))
        name = _TEN_COIN_TABLE.get((check.stage_key, idx))
        if name is None:
            log.debug(
                "location_table: no AP location for ten_coin stage_key=%d index=%d",
                check.stage_key, idx)
        return name

    name = _TABLE.get((check.kind, check.stage_key))
    if name is None:
        log.debug(
            "location_table: no AP location for kind=%s stage_key=%d",
            check.kind.value, check.stage_key)
    return name
