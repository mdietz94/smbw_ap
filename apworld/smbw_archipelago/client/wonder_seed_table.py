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
holds the current world index.  Live-observed ordering (W1..W6 and
Petal Isles validated 2026-05-26 after seeing W2 grants land in
Petal Isles and W3 grants land in W2; Special World validated
2026-05-28 from a live PlayReport ``course_in`` payload showing
``world_no=9``, matching the .rodata internal-name table
"Himitu"=9):

  world_val 1 = W1 Pipe-Rock Plateau          -> bucket 0
  world_val 2 = Petal Isles (the 2nd region)  -> bucket 6
  world_val 3 = W2 Fluff-Puff Peaks           -> bucket 1
  world_val 4 = W3 Shining Falls              -> bucket 2
  world_val 5 = W4 Sunbaked Desert            -> bucket 3
  world_val 6 = W5 Fungi Mines                -> bucket 4
  world_val 7 = W6 Deep Magma Bog             -> bucket 5
  world_val 8 = "Castle" (Bowser; no player overworld)
  world_val 9 = Special World                 -> bucket 7

The remap lives in ``main.cpp`` (``kWorldValToBucket``).  Earlier
notes that said ``W2 = 2`` / ``W3 = 3`` were misreadings -- the
player was actually in Petal Isles and W2 respectively at the time
of those observations.  Earlier notes that said ``Special = 8`` were
extrapolations from the W1..W6 + Petal Isles span; the Special World
slot wasn't directly observed until the 2026-05-28 capture.
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
    fallback semantics).

    No log on miss: ``_recompute_wonder_seed_counts`` calls this for
    every item in ``items_received`` (which includes every non-Wonder-
    Seed AP item -- character chooser, power-ups, 10 Coin, Royal Seeds,
    etc.), so a per-call debug line spams the log on every periodic
    2 s recompute.  Misses are normal control flow and not worth
    logging.
    """
    return _ITEM_TO_WORLD_INDEX.get(item_name)
