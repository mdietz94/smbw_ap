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

# AP item name -> deny-mask bit position.  The "All X Power" badges alias
# their power-up's bit: since PR #167 the logic accepts the badge as an
# alternative to the power-up item (equipping it turns every spawned
# power-up into X), so receiving the badge must also unlock picking X up --
# otherwise a badge-satisfied seed would be logically beatable while the
# mask physically denies the pickup.
DENY_BIT_FOR_ITEM: dict[str, int] = {
    "Fire Flower": 2,       # FireFlower
    "All Fire Power Badge": 2,
    "Elephant Fruit": 5,    # ElephantSuit
    "All Elephant Power Badge": 5,
    "Drill Mushroom": 12,   # DrillSuit
    "All Drill Power Badge": 12,
    "Bubble Flower": 18,    # AwaFlower
    "All Bubble Flower Badge": 18,
}

# Every bit the AP integration gates.  Pickups for types outside this mask
# (regular Super Mushrooms, stars, 1-UPs, keys, Wonder items, coins) are
# never denied -- the Super Mushroom stays a vanilla pickup by design
# (2026-06-10 decision; bit 1 / Kinoko remains available to the
# /deny_powerups override for experiments).
GATED_MASK: int = 0
for _bit in DENY_BIT_FOR_ITEM.values():
    GATED_MASK |= 1 << _bit


def deny_bit_for_item(item_name: str) -> int | None:
    """Deny-mask bit for an AP item name, or None if the item is not a
    gated power-up."""
    return DENY_BIT_FOR_ITEM.get(item_name)
