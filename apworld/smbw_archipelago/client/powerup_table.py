"""Power-Up AP item -> Switch ItemGet deny-mask bit table.

The Switch's ``ItemGetMaskBuild`` trampoline (NSO +0x3c4050) strips deny-mask
bits from the player's per-item-type "can pick up" bitmask, making those item
types untouchable in-level (the vanilla DrillDig behavior: the item stays, no
pickup animation, no transform, no damage).  Bit = RomFS ItemGetActorType
enum + 1; mirrors ``probe/ItemGetGate.hpp`` and the bit table on
:class:`wire.SetItemGetDenyMaskMsg`.

The client computes the deny mask as "every gated bit whose AP item has NOT
been received" -- receiving the AP item *unlocks the ability to collect* that
power-up; no in-game grant write is needed.  Seeds generated before the
power-ups entered the pool precollect them, and precollected items arrive in
the connect-time ReceivedItems batch, so the mask naturally collapses to 0
for old seeds.
"""
from __future__ import annotations

# AP item name -> deny-mask bit position.
DENY_BIT_FOR_ITEM: dict[str, int] = {
    "Super Mushroom": 1,    # Kinoko
    "Fire Flower": 2,       # FireFlower
    "Elephant Fruit": 5,    # ElephantSuit
    "Drill Mushroom": 12,   # DrillSuit
    "Bubble Flower": 18,    # AwaFlower
}

# Every bit the AP integration gates.  Pickups for types outside this mask
# (stars, 1-UPs, keys, Wonder items, coins) are never denied.
GATED_MASK: int = 0
for _bit in DENY_BIT_FOR_ITEM.values():
    GATED_MASK |= 1 << _bit


def deny_bit_for_item(item_name: str) -> int | None:
    """Deny-mask bit for an AP item name, or None if the item is not a
    gated power-up."""
    return DENY_BIT_FOR_ITEM.get(item_name)
