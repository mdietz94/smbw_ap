"""AP-authoritative Poplin shop **Wonder Seed** rows -> Switch slot masks.

The Poplin shops that sell a Wonder Seed had the same two-way coupling the
badge rows had (see [badge_table.py](badge_table.py) and
``probe/BadgeShop.hpp``): SMBW decides whether a seed row reads SOLD OUT
from the shop's own saved seed flag, and AP writes over that state from two
directions -- the Switch's ContainerAReader hook substitutes AP's count for
every read of the per-world Wonder-Seed counters, and
``probe::pushWonderSeedContainerDCounts`` blind-fills the world's 81-slot
per-course seed array (the shop's own slot is its ``WonderFlowerSaveCourseNo``,
70..80).  A seed row could therefore come out SOLD OUT with its AP check never
sent, making the location unreachable.

Fix, mirroring the badge shop: AP owns the seed row's *display state* and the
Switch never consults the game's flag.  This module turns AP's checked-location
set into the ``(managed, sold)`` slot masks of
:class:`wire.SetSeedShopStateMsg`.

Slot identity -- no new RE needed
--------------------------------
The shop screen exposes its lineup, so ``(current world index, number of
Wonder-Seed rows, row order)`` identifies the row.  Ground truth is the RomFS
``Stage/WorldMapInfo/World00N`` ``NpcTable`` (each ``BadgeShop`` NPC's
``SaleItemList`` entries with no ``Kind`` are its Wonder Seeds), which
cross-validates one-for-one against
:data:`location_table._SHOP_SEED_TABLE` -- a row's ``SaveId`` (absent == 0) is
exactly the PlayReport ``item_value``:

======== === ========= ===== =========================================
world_no npc row/SaveId price AP location
======== === ========= ===== =========================================
1        2   0          100   W1: Poplin Shop
2        2   0          100   PI: Poplin Shop (West)
2        4   0          100   PI: Poplin Shop (East)
3        4   0          100   W2: Poplin Shop (Bottom)
3        3   0          100   W2: Poplin Shop (Top)
4        7   0          100   W3: Poplin Shop
5        9   0          100   W4: Poplin Shop (Bottom)
5        5   0/1/2      30/100/200  W4: Poplin Shop (Secret)
6        3   0          100   W5: Poplin Shop
7        3   0          100   W6: Poplin Shop
======== === ========= ===== =========================================

Petal Isles and W2 each hold two single-seed shops whose lineups are
indistinguishable from the screen (same seed-row count, same price), and
``npc_id`` is not reachable at ``computeItemStates`` time.  Those two pairs
therefore **share one slot**, and this module only marks a shared slot sold
once *every* location behind it is obtained.  The bias is deliberate: showing
PURCHASABLE too long merely lets the player re-buy a seed whose check already
landed (AP ignores the duplicate), whereas showing SOLD OUT too early is the
bug we are fixing.

⚠ ``SEED_SHOP_SLOTS`` order + count MUST match ``kSeedShopSlots`` /
``kSeedShopSlotCount`` in ``switch-mod/src/probe/BadgeShop.cpp``.
"""

from __future__ import annotations

from typing import Final, Iterable


#: Slot index -> the AP locations behind it.  The Switch resolves a seed row
#: to its slot from ``(world index, seed-row count, row order)``; a slot with
#: more than one location is a lineup the Switch cannot disambiguate, and is
#: only ever marked sold when all of its locations are obtained.
#:
#: Each entry is ``(world_no, seed_rows, row_index, (location, ...))`` --
#: the first three fields document the Switch-side key and are asserted
#: against ``kSeedShopSlots`` by the tests.
SEED_SHOP_SLOTS: Final[list[tuple[int, int, int, tuple[str, ...]]]] = [
    # slot 0
    (1, 1, 0, ("W1: Poplin Shop - Wonder Seed",)),
    # slot 1 -- Petal Isles East + West are indistinguishable from the screen
    (2, 1, 0, ("PI: Poplin Shop (East) - Wonder Seed",
               "PI: Poplin Shop (West) - Wonder Seed")),
    # slot 2 -- W2 Top + Bottom, likewise
    (3, 1, 0, ("W2: Poplin Shop (Top) - Wonder Seed",
               "W2: Poplin Shop (Bottom) - Wonder Seed")),
    # slot 3
    (4, 1, 0, ("W3: Poplin Shop - Wonder Seed",)),
    # slot 4 -- W4's regular shop sells 1 seed, the Secret one sells 3, so
    # the seed-row count alone separates them
    (5, 1, 0, ("W4: Poplin Shop (Bottom) - Wonder Seed",)),
    # slots 5..7 -- W4 Secret, in shelf order (cheapest first)
    (5, 3, 0, ("W4: Poplin Shop (Secret) - Wonder Seed (30 Coins)",)),
    (5, 3, 1, ("W4: Poplin Shop (Secret) - Wonder Seed (100 Coins)",)),
    (5, 3, 2, ("W4: Poplin Shop (Secret) - Wonder Seed (200 Coins)",)),
    # slot 8
    (6, 1, 0, ("W5: Poplin Shop - Wonder Seed",)),
    # slot 9
    (7, 1, 0, ("W6: Poplin Shop - Wonder Seed",)),
]

#: Number of slots -- must equal ``kSeedShopSlotCount`` on the Switch.
SLOT_COUNT: Final[int] = len(SEED_SHOP_SLOTS)


def slot_location_names() -> list[tuple[str, ...]]:
    """``[locations_for_slot_0, locations_for_slot_1, ...]``.  Returns copies
    so callers can't mutate the table."""
    return [locs for _, _, _, locs in SEED_SHOP_SLOTS]


def all_location_names() -> list[str]:
    """Every shop-seed AP location name this table covers (12 names)."""
    return [name for _, _, _, locs in SEED_SHOP_SLOTS for name in locs]


def recompute_masks(
    location_name_to_id: dict[str, int],
    obtained_ids: Iterable[int],
    slot_locations: Iterable[int] | None = None,
) -> tuple[int, int]:
    """Compute the ``(managed, sold)`` slot masks for
    :class:`wire.SetSeedShopStateMsg`.

    ``location_name_to_id`` is the DataPackage name->id table,
    ``obtained_ids`` the locations already checked (or checked-in-flight),
    and ``slot_locations`` -- when given -- the location ids the server
    actually has for this slot, so a seed whose location this seed omits
    (category excluded, ...) stays on vanilla behavior instead of being
    force-shown as purchasable.

    A slot is **managed** iff every one of its locations resolves to a
    server-present id, and **sold** iff every one of them is obtained.
    Unresolved ids leave the slot unmanaged, so the feature degrades to
    vanilla rather than guessing: ``managed == 0`` makes the Switch side
    fully inert.
    """
    obtained = set(obtained_ids)
    known = None if slot_locations is None else set(slot_locations)
    managed = 0
    sold = 0
    for slot, (_world, _rows, _row, names) in enumerate(SEED_SHOP_SLOTS):
        ids = [location_name_to_id.get(n) for n in names]
        if any(i is None for i in ids):
            continue  # DataPackage maps not built yet -> leave vanilla
        if known is not None and any(i not in known for i in ids):
            continue  # not a check in this seed -> leave vanilla
        managed |= 1 << slot
        if all(i in obtained for i in ids):
            sold |= 1 << slot
    return managed, sold
