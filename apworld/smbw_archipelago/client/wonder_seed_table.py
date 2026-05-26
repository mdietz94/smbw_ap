"""AP Wonder Seed item -> per-world bucket mapping.

✅ **STATUS (2026-05-26): live-validated end-to-end.**

The gate override hypothesis was confirmed live: with all 5 mirror
hashes of the per-current-world Wonder Seed count written to 99, a
W3 gate that previously denied entry (player had only 1 actual W3
seed) opened on the next attempt and the in-game UI showed 99.  See
the ``smbwap-wonder-seed-gate-solved`` memory and
[docs/static-analysis-findings.md](../../docs/static-analysis-findings.md)
"2026-05-26 — Wonder Seed gate hypothesis confirmed" for the full
discovery trail.

**The grant mechanism**: the gate predicate ``FUN_71001787b40`` (NSO
``+0x1787b40``) reads container-A hash ``0x390eb960`` for the
*current world's* Wonder Seed count and compares it against a
per-gate threshold.  Five hashes mirror that count and update in
lockstep on every seed-count change:

  - ``0x21f89ab1``
  - ``0x8c20ccb7``  (the value formerly assumed to be "lifetime" --
                    actually per-current-world; resets on world
                    transitions)
  - ``0xeeff353b``
  - ``0x390eb960``  (the one the gate predicate reads)
  - ``0xa0e5f253``

The Switch primitive ``probe::pushWonderSeedOverride(value)`` writes
``value`` to all 5 via the container-A counter writer
(``FUN_710049F648``).  The natural game code recomputes them from
per-course bitfields on every world transition, which is why the
bridge has to re-push on a ~2 s cadence -- same idempotent
absolute-overwrite pattern as badges.

**This module's job** is to translate AP item names into per-world
bucket counts.  The 8 buckets are:

  index 0  W1 Wonder Seed
  index 1  W2 Wonder Seed
  index 2  W3 Wonder Seed
  index 3  W4 Wonder Seed
  index 4  W5 Wonder Seed
  index 5  W6 Wonder Seed
  index 6  Petal Isles Wonder Seed
  index 7  Special World Wonder Seed

The Switch maps the value at container-A hash ``0x9f5ead3c`` (the
live "current world index") to a bucket index; the bridge ships the
8-tuple via ``SetWonderSeedCountsMsg``.

**Per-world routing in the Switch**: container-A hash ``0x9f5ead3c``
holds the current world index.  Observed values: ``W2 = 2``, ``W3 =
3`` (from the 09-34-58 run log).  The natural assumption -- W1=1,
W4=4, W5=5, W6=6, Petal Isles=7, Special=8 -- is encoded in the
Switch dispatcher (``main.cpp`` NerveActivateOnce tick).  Petal
Isles / Special indices are tentative; if they turn out to be
different values, fix the Switch-side map without touching this
table.
"""

from __future__ import annotations

import logging
from typing import Final


log = logging.getLogger("SMBW")


# Number of world buckets -- mirrors ``SetWonderSeedCountsMsg.WORLD_COUNT``.
# Kept here so the context layer can size its accumulator without
# importing wire.
WORLD_COUNT: Final[int] = 8


# AP item name -> bucket index (0..WORLD_COUNT-1).  Item names must
# match ``data/items.json`` exactly.  ``data/items.json`` declares
# each Wonder Seed kind with a ``count`` (e.g. W1 = 35 means 35 seeds
# in the multiworld pool); each *received* item bumps the
# corresponding bucket by 1.
_ITEM_TO_WORLD_INDEX: Final[dict[str, int]] = {
    "W1 Wonder Seed":             0,
    "W2 Wonder Seed":             1,
    "W3 Wonder Seed":             2,
    "W4 Wonder Seed":             3,
    "W5 Wonder Seed":             4,
    "W6 Wonder Seed":             5,
    "Petal Isles Wonder Seed":    6,
    "Special World Wonder Seed":  7,
}


def is_wonder_seed_item(item_name: str) -> bool:
    """Cheap check: does this AP item name correspond to a Wonder Seed?

    Used by :meth:`SMBWContext._handle_received_items` to short-circuit
    non-Wonder-Seed items in the dispatch ladder.  Counted into a bucket
    by :func:`world_index_for_item`."""
    return item_name in _ITEM_TO_WORLD_INDEX


def world_index_for_item(item_name: str) -> int | None:
    """Look up the world bucket index for a Wonder Seed AP item name.
    Returns ``None`` if the name isn't a known Wonder Seed (the caller
    should silently drop, matching the rest of the ItemTable layer's
    fallback semantics)."""
    idx = _ITEM_TO_WORLD_INDEX.get(item_name)
    if idx is None:
        log.debug("wonder_seed_table: no bucket for item %r", item_name)
    return idx
