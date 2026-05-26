"""AP item name -> ``IncrementHashKeyed`` (hash, delta) mapping for the
purple-coin / flower-coin filler items.

Sibling of :mod:`royal_seed_table`: where Royal Seeds are absolute
container-B bool sets (idempotent), coin items are container-A counter
**increments** -- the in-game purple coin total is shared between AP
grants and gameplay-earned coins, so an absolute set would clobber the
player's own progress.  The Switch-side primitive is a read-modify-
write on ``FUN_710012AE94`` (reader) + ``FUN_710049F648`` (writer); see
``probe::incrementContainerACounter`` in
[switch-mod/src/program/main.cpp](../../../switch-mod/src/program/main.cpp).

The single entry today is the AP item ``"10 Coin"`` (see
``data/items.json``) which adds 10 to ``flower_coin`` (hash
``0xF4EE6827``).  Counter slot is u16; the Switch writer truncates
internally, so even thousands of "10 Coin" items roll over cleanly.

Save-survival caveat: the container-A writer is deferred-write
(queues to ``gmd+0xf8`` dirty buffer, flushes on next save).  Unlike
Royal Seeds, we don't replay coin grants on HelloMsg -- replay would
double-count.  Accepted for filler; if a counter ever becomes
progression-critical, see the wire-message docstring for the dedup
story we'd need.
"""

from __future__ import annotations

import logging
from typing import Final


log = logging.getLogger("SMBW")


# (item_name, hash, delta)
#
# ``item_name`` MUST match ``data/items.json`` exactly so the AP
# server's display string resolves cleanly.  ``hash`` is the SMBW
# save-data field hash (MemetendoYT-verified, see CLAUDE.md
# "GameDataMgr save-data API" -- ``flower_coin`` is ``0xF4EE6827``,
# u16 wide).  ``delta`` is the amount to add to the running total per
# AP item received.
_COIN_ITEMS: Final[list[tuple[str, int, int]]] = [
    ("10 Coin", 0xF4EE6827, 10),
]


_NAME_TO_GRANT: Final[dict[str, tuple[int, int]]] = {
    name: (h, d) for name, h, d in _COIN_ITEMS
}


def is_coin_item(item_name: str) -> bool:
    """Cheap check: does this AP item name correspond to a coin grant?"""
    return item_name in _NAME_TO_GRANT


def grant_for_item(item_name: str) -> tuple[int, int] | None:
    """Look up ``(hash, delta)`` for a coin AP item name.  Returns
    ``None`` if the name isn't a known coin item (caller should log +
    drop, matching ``royal_seed_table.hash_for_item``'s contract)."""
    g = _NAME_TO_GRANT.get(item_name)
    if g is None:
        log.debug("coin_table: no grant for item %r", item_name)
        return None
    return g
