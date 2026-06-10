"""AP item name -> SMBW save-data hash mapping for the 6 Royal Seeds.

✅ **STATUS: AP-authoritative absolute mask (2026-05-26).**

Royal Seeds use the same idempotent-overwrite pattern as badges and
Wonder Seeds: the bridge holds a canonical 6-bit mask derived from
``items_received``, and clobbers the Switch's container-B bools on
every ``ReceivedItems``, every Switch ``HelloMsg``, and the ~2 s
periodic tick.  Each bit corresponds to one world (bit 0 = W1,
bit 5 = W6); the Switch dispatch loops the 6 hashes and calls
``probe::grantContainerBBool(hash, bit_value)`` for each one --
clearing a bool the player obtained in-game (palace clear before AP
released that seed item) just as reliably as setting one AP granted.

The earlier M4.5 implementation only replayed AP-granted seeds on
HelloMsg via an additive ``GrantHashKeyedMsg`` per seed; that closed
the save-survival gap for AP-granted seeds but couldn't revert an
in-game palace clear that ran ahead of AP.  The container-B bool
writer ``FUN_710049EA24`` (NSO +0x0049EA24) accepts ``value=0`` just
fine, so the absolute-overwrite primitive is a natural fit.

The Switch-side bool writer calls ``FUN_710049EA24`` -- the high-level
container-B bool writer -- which gates on the gmd+0x68 init/lock and
delegates to ``FUN_7101F263FC`` with ``substruct = gmd + 8`` (the
deferred-write bool setter for the gmd+8 substruct).  M3.3b smoke test
on 2026-05-25 wrote all 8 documented bool hashes (6 Royal Seeds +
COMPLETE_GAME + INTRO_CUTSCENE_COMPLETED) and save-diff confirmed the
6 pair-region byte flips at the expected offsets (W2 @ 0x0064,
W3 @ 0x0384, W4 @ 0x01F4, W5 @ 0x036C, W6 @ 0x00BC,
COMPLETE_GAME @ 0x0044).  W1 and INTRO no-op'd because they were
already 0x01 in the test save -- idempotent, as expected.

This table is intentionally narrow.  Container B also holds
COMPLETE_GAME / INTRO_CUTSCENE_COMPLETED bools which we deliberately
*don't* expose to the absolute-mask path -- they aren't AP items and
the player should keep them once earned (they gate cutscenes /
credits, not progression).  Container A counters (flower_coin,
regular_coin) route through their own paths and are untouched here.

Save-survival is implicit in the absolute-overwrite pattern: every
HelloMsg re-applies the mask, so reconnects (Switch reboot, Ryujinx
restart, save/reload that triggers a fresh handshake) restore the
correct state from AP's view.  The periodic tick keeps any
between-save in-game pickup reverted within ~2 s.
"""

from __future__ import annotations

import logging
from typing import Final


log = logging.getLogger("SMBW")


# u8 bool slots: value=1 grants the seed.  See CLAUDE.md "GameDataMgr
# save-data API" for the canonical hash table; see docs/save-diff-findings.md
# for the file-offset verification (pair-region 0x0350 for W1 Royal Seed).
ROYAL_SEED_VALUE: Final[int] = 1


# (item_name, hash, palace_stage_key, source) ordered W1..W6 -- list
# index IS the mask bit position (W1 = bit 0, W2 = bit 1, ..., W6 =
# bit 5).  This ordering is part of the wire contract with the Switch
# dispatch in ``ApFrameBridge.cpp``: both ends iterate the same 6
# hashes in the same order, so bit N here MUST be bit N in the
# Switch's ``kRoyalSeedHashes`` array.
#
# item_name MUST match ``manual_smbwonder_zim/data/items.json`` exactly
# so the AP server's display string resolves cleanly.  All 6 entries
# below were confirmed character-for-character against items.json.
#
# ``palace_stage_key`` is the 32-bit course-name hash of the palace
# stage whose ``PALACE_CLEAR`` AP location corresponds to this Royal
# Seed item.  Values mirror ``location_table.py``'s
# ``_STAGE_*_PALACE`` constants (W3 uses ``_STAGE_ROYAL_SEED_MANSION``;
# W5 uses ``_STAGE_OPERATION_POPLIN_RESCUE``).  Used by the AP-grant
# auto-emit path in ``SMBWContext._handle_received_items`` to resolve
# the matching location at item-receipt time -- see CLAUDE.md note on
# why the natural ``PALACE_CLEAR`` PlayReport often doesn't fire once
# the seed bool is already set (the in-game UI says "cleared" and the
# player has no reason to re-enter).  ``docs/royal-seed-check-loss-re-plan.md``
# tracks the RE work to find a path that fires the check naturally.
#
# ``source`` records where the hash came from -- ``live`` means
# MemetendoYT-verified as the correct save-data hash AND the in-game
# grant has been validated end-to-end (M3.3b smoke 2026-05-25:
# save-diff confirmed pair-region byte flips for each).
_ROYAL_SEEDS: Final[list[tuple[str, int, int, str]]] = [
    ("W1 Royal Seed", 0x55815859, 0x89927C97, "live"),
    ("W2 Royal Seed", 0x49ABBA86, 0xB2B07454, "live"),
    ("W3 Royal Seed", 0xB550D8D6, 0xA5E2BB3A, "live"),
    ("W4 Royal Seed", 0x1DCF7F6E, 0x1969941E, "live"),
    ("W5 Royal Seed", 0x0D5A3E00, 0x87E6D263, "live"),
    ("W6 Royal Seed", 0xD4660D2B, 0x7E523816, "live"),
]


# Mask width matches the table length; surfaced as a constant so the
# bridge / context / tests don't all hard-code 6.
WORLD_COUNT: Final[int] = len(_ROYAL_SEEDS)

# Full mask covering all 6 Royal Seeds; handy in tests and `/grant`
# CLI verbs.
ALL_MASK: Final[int] = (1 << WORLD_COUNT) - 1


# Hashes in mask-bit order -- exported for the Switch-side iteration
# contract.  Length == WORLD_COUNT.
ROYAL_SEED_HASHES: Final[tuple[int, ...]] = tuple(h for _, h, _, _ in _ROYAL_SEEDS)

# Palace stage_keys in mask-bit order (W1..W6) -- the six numbered-world
# palace clears (NOT the final Bowser stage).  The open-world final-Bowser
# death-gate uses these to count how many palaces the player has actually
# beaten in-game (a checked PALACE_CLEAR location per stage_key).
PALACE_STAGE_KEYS: Final[tuple[int, ...]] = tuple(sk for _, _, sk, _ in _ROYAL_SEEDS)


_NAME_TO_BIT: Final[dict[str, int]] = {
    name: bit for bit, (name, _, _, _) in enumerate(_ROYAL_SEEDS)
}

_NAME_TO_HASH: Final[dict[str, int]] = {
    name: h for name, h, _, _ in _ROYAL_SEEDS
}

_NAME_TO_PALACE_STAGE_KEY: Final[dict[str, int]] = {
    name: sk for name, _, sk, _ in _ROYAL_SEEDS
}


def is_royal_seed_item(item_name: str) -> bool:
    """Cheap check: does this AP item name correspond to a known Royal Seed?

    Used by the AP context to OR the matching bit into the canonical
    absolute mask; the Switch dispatch (see ``ApFrameBridge.cpp``
    ``drainInbound``'s ``SetRoyalSeedsAbsolute`` case) loops the 6
    Royal Seed hashes and calls ``probe::grantContainerBBool(hash, (mask
    >> bit) & 1)`` per bit -- setting AP-granted seeds and clearing any
    seed the player obtained in-game without AP releasing the item.
    """
    return item_name in _NAME_TO_HASH


def bit_for_item(item_name: str) -> int | None:
    """Return the mask-bit position for ``item_name`` (0 = W1, ..., 5 = W6),
    or ``None`` if the name isn't a known Royal Seed.  The absolute-mask
    path uses this to OR the bit into the AP-derived mask.  Called
    per-item on every 2 s absolute-sync tick, so this stays log-free
    by design (mirror of badge_table.grant_internal_id_for_item)."""
    return _NAME_TO_BIT.get(item_name)


def hash_for_item(item_name: str) -> int | None:
    """Look up the container-B bool hash for a Royal Seed AP item name.
    Returns ``None`` if the name isn't a known Royal Seed.  Retained for
    the ``/grant_hash`` debug command and tests that want to round-trip
    a single hash; the production sync path uses the absolute mask
    instead."""
    h = _NAME_TO_HASH.get(item_name)
    if h is None:
        log.debug("royal_seed_table: no hash for item %r", item_name)
        return None
    return h


def palace_stage_key_for_item(item_name: str) -> int | None:
    """Look up the palace ``stage_key`` for a Royal Seed AP item name.

    Returns the 32-bit course-name hash of the palace whose
    ``PALACE_CLEAR`` AP location pairs with this seed item -- e.g.
    ``"W1 Royal Seed"`` maps to Pipe-Rock Plateau Palace's stage_key
    (0x89927C97).  ``None`` for unrecognized names.

    Used by ``SMBWContext._handle_received_items`` to auto-emit the
    matching location check at item-receipt time -- the natural
    ``PALACE_CLEAR`` PlayReport often fails to fire once AP has already
    set the seed bool (the world map shows the palace as cleared, so the
    player has no in-game reason to re-enter).  Mirrors the badge
    auto-resolve pattern where the same bit serves dual duty (Mario
    equips from it AND the shop refuses to sell any bit it sees set).
    """
    return _NAME_TO_PALACE_STAGE_KEY.get(item_name)
