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


def lookup_name(check: CheckEmitted) -> str | None:
    """Resolve a CheckEmitted to its canonical AP location name.

    Returns ``None`` when no mapping exists; the caller (ap_client)
    should log + drop.  Missing entries are normal early in M4 (most
    courses haven't been mapped yet) and should NOT cascade into an AP
    error -- the bridge stays connected and the player just doesn't
    get credit for the un-mapped course until the table extends.
    """
    name = _TABLE.get((check.kind, check.stage_key))
    if name is None:
        log.debug(
            "location_table: no AP location for kind=%s stage_key=%d",
            check.kind.value, check.stage_key)
    return name
