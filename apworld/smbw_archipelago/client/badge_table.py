"""AP item name -> badge internal_id mapping.

The M4 grant primitive (``probe::setBadgeBitfieldAbsolute`` in
``switch-mod/src/program/main.cpp``) writes the absolute owned-badge
bitfield in SMBW's container-C bitfield at hash ``0x105df820``.  The
bit position of each badge IS the internal_id; bit 4 means internal_id
4.  The bridge OR's together one bit per item in
``CommonContext.items_received`` to build the mask it pushes.

The manual apworld doesn't carry internal_ids, so this table is
hand-coded.  Add a row as each badge's bit position is confirmed.
Missing entries log + drop -- the bridge silently ignores items it
doesn't recognize, which is the desired failure mode (AP server is
happy, in-game UI just doesn't show the badge until the table catches
up).

Confidence levels:

  - **live** -- end-to-end validated: AP send -> bridge -> Switch ->
    container-C bitfield -> badge appears immediately in the equip
    menu live.  Sourced from the M3.2 sprint (2026-05-24).

  - **save-diff** -- bit position identified by diffing pre/post save
    files against MemetendoYT's editor.  Highly likely correct (save
    bytes match) but live-grant has not been tested.  M5 work will
    promote these to ``live`` after dogfooding.

CLAUDE.md badge section + ``docs/save-diff-findings.md`` are the
sources of truth for new entries; cross-reference before adding.
"""

from __future__ import annotations

import json
import logging
import pkgutil
from typing import Final


log = logging.getLogger("SMBW")


# (item_name, internal_id, confidence)
#
# item_name must match ``manual_smbwonder_zim/data/items.json``
# exactly so the AP server's display string resolves cleanly.
_BADGES: Final[list[tuple[str, int, str]]] = [
    # Live-validated end-to-end (M3.2, 2026-05-24).
    ("Spring Feet Badge", 4, "live"),

    # Save-diff identified.  Bit positions come from
    # ``docs/save-diff-findings.md``; the file-offset bitfield at
    # 0x0EA0 mirrors the live container-C bitfield.  Both indirectly
    # corroborated by the M2.3 probe completion (no other bit's probe
    # produced these badges).
    ("Coin Reward Badge", 9, "save-diff"),
    ("Auto Super Mushroom Badge", 46, "save-diff"),

    # M2.3 probe-loop finds (2026-05-26; user-verified via /badge_probe
    # in the live game — opened the badge inventory and read off the
    # single visible badge for each mask).  Parachute & Wall-Climb's
    # {34, 35} ambiguity (previously noted in docs/save-diff-findings.md)
    # is resolved here: probe bit 34 produced Wall-Climb, bit 35 produced
    # Parachute Cap.
    ("Floating High Jump Badge", 0, "probe"),
    ("Fast Dash Badge", 8, "probe"),
    ("Boosting Spin Jump Badge", 14, "probe"),
    ("Jet Run Badge", 19, "probe"),
    ("Add ! Blocks Badge", 28, "probe"),
    ("Dolphin Kick Badge", 29, "probe"),
    ("Sensor Badge", 32, "probe"),
    ("Sound Off? Badge", 33, "probe"),
    ("Wall-Climb Jump Badge", 34, "probe"),
    ("Parachute Cap Badge", 35, "probe"),
    ("Invisibility Badge", 38, "probe"),
    ("Crouching High Jump Badge", 39, "probe"),
    ("Coin Magnet Badge", 40, "probe"),
    ("Timed High Jump Badge", 42, "probe"),
    ("Rhythm Jump Badge", 47, "probe"),
    ("Safety Bounce Badge", 49, "probe"),
    ("Grappling Vine Badge", 53, "probe"),
    ("All Fire Power Badge", 54, "probe"),
    ("All Elephant Power Badge", 55, "probe"),
    ("All Drill Power Badge", 57, "probe"),
    # User probed as "All Bubble Power"; AP item name is "All Bubble
    # Flower Badge" (only one "Bubble" badge in items.json so the
    # mapping is unambiguous; the in-game display name differs).
    ("All Bubble Flower Badge", 58, "probe"),
]


_NAME_TO_ID: Final[dict[str, int]] = {name: bit for name, bit, _ in _BADGES}

# Reverse lookup for M2.3 outbound badge-check detection: the Switch
# reports the bit position it observed going set; the bridge needs the
# matching AP item / location name.  Built once at module load.
_ID_TO_NAME: Final[dict[int, str]] = {bit: name for name, bit, _ in _BADGES}


def grant_internal_id_for_item(item_name: str) -> int | None:
    """Look up the badge bit for an AP item name.  Returns ``None`` if
    the name isn't a known badge -- the caller is expected to silently
    drop and route the item elsewhere (Royal Seed table, Wonder Seed
    counter, etc.).  Called per-item on every 2 s absolute-sync tick,
    so this stays log-free by design -- any per-call log here would
    multiply by ``items_received`` size and tick rate."""
    return _NAME_TO_ID.get(item_name)


def name_for_internal_id(internal_id: int) -> str | None:
    """Look up the AP item name for a badge bit position.  Returns
    ``None`` if the bit isn't a known badge (the caller should log +
    drop, then update ``_BADGES`` with a save-diff capture)."""
    name = _ID_TO_NAME.get(internal_id)
    if name is None:
        log.debug("badge_table: no name for internal_id %d", internal_id)
        return None
    return name


def is_badge_item(item_name: str) -> bool:
    """Cheap check: does this AP item name correspond to a known badge?

    Used by the AP context to short-circuit non-badge items in M4 (which
    only handles badges).  M5 will replace this with category-based
    dispatch via the manual apworld's item category metadata.
    """
    return item_name in _NAME_TO_ID


# ---------------------------------------------------------------------------
# M2.3 probe-and-discover helpers.  Drive `/badge_probe_next` and
# `/badge_status` in commands.py to walk the unmapped bits and item names.


def mapped_bits() -> set[int]:
    """Set of badge bit positions (== internal_ids) already in _BADGES."""
    return set(_NAME_TO_ID.values())


# items.json (the source of truth for the 24 badge item names) lives at
# the apworld root: apworld/smbw_archipelago/data/items.json, one level up
# from this `client` subpackage.  Read it with pkgutil.get_data (NOT
# os.path + open): the standalone Archipelago install loads the world
# straight from the zipped `.apworld` without extracting it, so an
# os.path/open read resolves to a path *inside* the zip and fails with
# ENOENT (only the dev junction, an extracted folder, made open() work).
# Mirrors Data.load_data_file's zip-safe loader.  __package__ here is
# "<root>.client"; the data dir hangs off "<root>".
_ROOT_PKG = (__package__ or "").rsplit(".", 1)[0]


def all_badge_item_names() -> list[str]:
    """Read items.json and return every AP item whose name contains
    'Badge'.  This is the canonical 24-name list; the probe loop uses
    it to know which items still need an internal_id mapping."""
    try:
        raw = pkgutil.get_data(_ROOT_PKG, "data/items.json")
        if raw is None:
            raise FileNotFoundError("data/items.json not found in package")
        entries = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as e:
        log.warning(
            "badge_table: can't read data/items.json from package %r: %s",
            _ROOT_PKG, e)
        return []
    return [e["name"] for e in entries if "Badge" in e.get("name", "")]


def unmapped_item_names() -> list[str]:
    """Badge item names from items.json that don't yet have an
    internal_id in _BADGES.  21 of 24 at M2.3 ship time."""
    return [n for n in all_badge_item_names() if n not in _NAME_TO_ID]


def next_unmapped_bit(
    after: int = -1,
    ceiling: int = 64,
    extra_skip: set[int] | None = None,
) -> int | None:
    """Find the lowest bit position strictly greater than `after` that
    isn't already in _BADGES.  Returns ``None`` if every bit in
    [after+1, ceiling) is already mapped (or in ``extra_skip``).  Used
    by `/badge_probe_next` to walk the unknown space.

    ``extra_skip`` is the runtime "known-invalid" set the context
    accumulates from `/badge_probe_invalid`.  Pass it so the
    probe-next loop skips bits the user has already confirmed don't
    correspond to any badge (the SMBW badge bitfield is sparse:
    ~24 valid bits scattered across 0..63, so most bits are dead).
    """
    taken = mapped_bits()
    skip = extra_skip or set()
    for b in range(max(0, after + 1), ceiling):
        if b in taken or b in skip:
            continue
        return b
    return None
