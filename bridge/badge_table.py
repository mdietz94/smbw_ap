"""AP item name -> badge internal_id mapping.

The M3.2 grant primitive (``probe::grantBadgeBit`` in
``switch-mod/src/program/main.cpp``) takes the bit position of the
badge in SMBW's container-C owned-bitfield at hash ``0x105df820``.
That bit position IS the internal_id; bit 4 means internal_id 4.

The manual apworld doesn't carry internal_ids, so this table is
hand-coded.  Add a row as each badge's bit position is confirmed.
Missing entries log + drop -- the bridge silently ignores grants it
doesn't recognize, which is the desired failure mode (AP server is
happy, in-game UI just doesn't show the badge until the table catches
up).

Confidence levels:

  - **live** -- end-to-end validated: AP send -> bridge -> Switch ->
    grantBadgeBit -> badge appears immediately in the equip menu live.
    Sourced from the M3.2 sprint (2026-05-24).

  - **save-diff** -- bit position identified by diffing pre/post save
    files against MemetendoYT's editor.  Highly likely correct (save
    bytes match) but live-grant has not been tested.  M5 work will
    promote these to ``live`` after dogfooding.

CLAUDE.md badge section + ``docs/save-diff-findings.md`` are the
sources of truth for new entries; cross-reference before adding.
"""

from __future__ import annotations

import logging
from typing import Final


log = logging.getLogger(__name__)


# (item_name, internal_id, confidence)
#
# item_name must match ``manual_smbwonder_zim/data/items.json``
# exactly so the AP server's display string resolves cleanly.
_BADGES: Final[list[tuple[str, int, str]]] = [
    # Live-validated end-to-end (M3.2, 2026-05-24).
    ("Spring Feet Badge", 4, "live"),

    # Save-diff identified; live-grant test pending.  Bit positions come
    # from ``docs/save-diff-findings.md``; the file-offset bitfield at
    # 0x0EA0 mirrors the live container-C bitfield, so these IDs should
    # work via ``grantBadgeBit`` -- but until smoke-tested treat them as
    # provisional.
    ("Coin Reward Badge", 9, "save-diff"),
    ("Auto Super Mushroom Badge", 46, "save-diff"),
    # Parachute & Wall-Climb were identified as a pair at {34, 35}
    # without a determined ordering.  Skipping both for now; the AP
    # server will get warnings instead of granting the wrong badge.
    # ("Parachute Cap Badge",  34_or_35, "save-diff:unordered"),
    # ("Wall-Climb Jump Badge", 34_or_35, "save-diff:unordered"),
]


_NAME_TO_ID: Final[dict[str, int]] = {name: bit for name, bit, _ in _BADGES}


def grant_internal_id_for_item(item_name: str) -> int | None:
    """Look up the badge bit for an AP item name.  Returns ``None`` if
    the name isn't a known badge (the caller should log + drop)."""
    bit = _NAME_TO_ID.get(item_name)
    if bit is None:
        log.debug("badge_table: no internal_id for item %r", item_name)
        return None
    return bit


def is_badge_item(item_name: str) -> bool:
    """Cheap check: does this AP item name correspond to a known badge?

    Used by the AP context to short-circuit non-badge items in M4 (which
    only handles badges).  M5 will replace this with category-based
    dispatch via the manual apworld's item category metadata.
    """
    return item_name in _NAME_TO_ID
