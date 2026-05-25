"""AP item name -> SMBW save-data hash mapping for the 6 Royal Seeds.

✅ **STATUS (2026-05-25): live end-to-end.**

The full chain (this table + ``send_grant_hash_keyed`` +
``GrantHashKeyedMsg`` + Switch-side ``probe::grantContainerBBool``)
is validated.  The Switch primitive calls ``FUN_710049EA24`` (NSO
+0x0049EA24, the high-level container-B bool writer) which gates on
the gmd+0x68 init/lock and delegates to ``FUN_7101F263FC`` with
``substruct = gmd + 8`` (the deferred-write bool setter for the gmd+8
substruct).  M3.3b smoke test on 2026-05-25 wrote all 8 documented
bool hashes (6 Royal Seeds + COMPLETE_GAME + INTRO_CUTSCENE_COMPLETED)
and save-diff confirmed the 6 pair-region byte flips at the expected
offsets (W2 @ 0x0064, W3 @ 0x0384, W4 @ 0x01F4, W5 @ 0x036C,
W6 @ 0x00BC, COMPLETE_GAME @ 0x0044).  W1 and INTRO no-op'd because
they were already 0x01 in the test save -- idempotent, as expected.

Dispatch routing lives in ``switch-mod/src/program/ap/ApFrameBridge.cpp``
``drainInbound()`` -- the ``GrantHashKeyed`` case branches on
``isBoolHash(hash)`` to route bool hashes to ``grantContainerBBool``
and counter hashes (flower_coin, regular_coin) to
``grantContainerACounter``.  Bool whitelist is ``kBoolHashes`` in
``ApFrameBridge.hpp``.

This table is intentionally narrow.  Container A also holds counters
(flower_coin, regular_coin) and Container B holds bools (COMPLETE_GAME,
INTRO) that we deliberately *don't* expose as AP items yet -- coin
denomination requires read-modify-write tracking and the two
completion bools aren't in the manual apworld's items.json.  The
Switch dispatch still routes them correctly if the bridge ever
forwards them (e.g. via a future test harness).

Missing item names log + drop (silent no-op) -- the bridge ignores
grants for items not in this table, which is the desired failure
mode (AP server is happy, in-game state just stays put).

⚠️ **Save-survival caveat** -- the bool writer is deferred-write (queues
to the dirty buffer at gmd+0xf8, flushed at next save).  Grants survive
the current play session but if the player loads a fresh save before
the buffer flushes, the grant is lost.  M4.5 bridge replay-on-HelloMsg
re-emits every received item on Switch reconnect, which is the durable
fix; M3.3b alone covers in-session grants.
"""

from __future__ import annotations

import logging
from typing import Final


log = logging.getLogger("SMBW")


# u8 bool slots: value=1 grants the seed.  See CLAUDE.md "GameDataMgr
# save-data API" for the canonical hash table; see docs/save-diff-findings.md
# for the file-offset verification (pair-region 0x0350 for W1 Royal Seed).
ROYAL_SEED_VALUE: Final[int] = 1


# (item_name, hash, source)
#
# item_name MUST match ``manual_smbwonder_zim/data/items.json`` exactly
# so the AP server's display string resolves cleanly.  All 6 entries
# below were confirmed character-for-character against items.json.
#
# ``source`` records where the hash came from -- ``live`` means
# MemetendoYT-verified as the correct save-data hash AND the in-game
# grant has been validated end-to-end (M3.3b smoke 2026-05-25:
# save-diff confirmed pair-region byte flips for each).
_ROYAL_SEEDS: Final[list[tuple[str, int, str]]] = [
    ("W1 Royal Seed", 0x55815859, "live"),
    ("W2 Royal Seed", 0x49ABBA86, "live"),
    ("W3 Royal Seed", 0xB550D8D6, "live"),
    ("W4 Royal Seed", 0x1DCF7F6E, "live"),
    ("W5 Royal Seed", 0x0D5A3E00, "live"),
    ("W6 Royal Seed", 0xD4660D2B, "live"),
]


_NAME_TO_HASH: Final[dict[str, int]] = {name: h for name, h, _ in _ROYAL_SEEDS}


def is_royal_seed_item(item_name: str) -> bool:
    """Cheap check: does this AP item name correspond to a known Royal Seed?

    Used by the AP context to dispatch Royal Seed grants via
    ``send_grant_hash_keyed`` instead of the badge path.  Switch-side
    dispatch routes the hash to the container-B bool writer (see
    ``ApFrameBridge.cpp`` ``drainInbound``).
    """
    return item_name in _NAME_TO_HASH


def hash_for_item(item_name: str) -> int | None:
    """Look up the container-B bool hash for a Royal Seed AP item name.
    Returns ``None`` if the name isn't a known Royal Seed (the caller
    should log + drop, matching ``badge_table.grant_internal_id_for_item``'s
    contract)."""
    h = _NAME_TO_HASH.get(item_name)
    if h is None:
        log.debug("royal_seed_table: no hash for item %r", item_name)
        return None
    return h
