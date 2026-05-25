"""AP item name -> SMBW save-data hash mapping for the 6 Royal Seeds.

⚠️ **STATUS (2026-05-25): wired but NOT yet granting in-game.**

The bridge plumbing (this table + ``send_grant_hash_keyed`` +
``GrantHashKeyedMsg`` + Switch-side ``probe::grantContainerACounter``)
is complete and reusable.  The leaf primitive on the Switch side is
``FUN_710049F648`` (container-A counter writer) -- live-validated
2026-05-25 for the u16 ``flower_coin`` counter but **falsified for the
Royal Seed u8 bool slots**.  The M3.3 smoke test called the writer
with hash ``0x55815859`` value ``1``; the trampoline confirmed the
call landed; post-save value at file offset ``0x0354`` stayed at ``0``.

Conclusion: the Royal Seed bools live in container A's pair-region as
storage but the container-A *writer* is typed and silently no-ops on
non-counter slots.  M3.3b needs the container-B writer (candidate
``FUN_71005E93FC``, per docs/static-analysis-findings.md) or a separate
typed-bool writer entry point.

This table is kept wired so that when the Switch-side fallback writer
lands, only ``probe::grantContainerACounter`` needs to branch by hash
to dispatch the 6 Royal Seed hashes through the bool writer instead
(or, equivalently, a new ``probe::grantContainerBBool`` companion).
The bridge side stays untouched.  ``ap_client._handle_received_items``
currently logs a WARNING when forwarding a Royal Seed so the operator
isn't surprised when AP records the item received but the seed
doesn't unlock in-game.

This table is intentionally narrow.  Container A also holds counters
(flower_coin, regular_coin) and bools (COMPLETE_GAME, INTRO) that we
deliberately *don't* expose as AP items yet -- coin denomination
requires read-modify-write tracking and the two completion bools
aren't in the manual apworld's items.json.

Missing item names log + drop (silent no-op) -- the bridge ignores
grants for items not in this table, which is the desired failure
mode (AP server is happy, in-game state just stays put).
"""

from __future__ import annotations

import logging
from typing import Final


log = logging.getLogger(__name__)


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
# ``source`` records where the hash came from -- ``memetendoYT-pending``
# means it's MemetendoYT-verified as the correct save-data hash AND
# present in the pair region at the expected file offset, but the
# in-game grant doesn't land yet (see module docstring).  Promote to
# ``live`` once the container-B writer ships and a Phase C end-to-end
# smoke succeeds.
_ROYAL_SEEDS: Final[list[tuple[str, int, str]]] = [
    ("W1 Royal Seed", 0x55815859, "memetendoYT-pending"),
    ("W2 Royal Seed", 0x49ABBA86, "memetendoYT-pending"),
    ("W3 Royal Seed", 0xB550D8D6, "memetendoYT-pending"),
    ("W4 Royal Seed", 0x1DCF7F6E, "memetendoYT-pending"),
    ("W5 Royal Seed", 0x0D5A3E00, "memetendoYT-pending"),
    ("W6 Royal Seed", 0xD4660D2B, "memetendoYT-pending"),
]


_NAME_TO_HASH: Final[dict[str, int]] = {name: h for name, h, _ in _ROYAL_SEEDS}


def is_royal_seed_item(item_name: str) -> bool:
    """Cheap check: does this AP item name correspond to a known Royal Seed?

    Used by the AP context to dispatch container-A grant items via
    ``send_grant_hash_keyed`` instead of the badge path.
    """
    return item_name in _NAME_TO_HASH


def hash_for_item(item_name: str) -> int | None:
    """Look up the container-A hash for a Royal Seed AP item name.  Returns
    ``None`` if the name isn't a known Royal Seed (the caller should log +
    drop, matching ``badge_table.grant_internal_id_for_item``'s contract)."""
    h = _NAME_TO_HASH.get(item_name)
    if h is None:
        log.debug("royal_seed_table: no hash for item %r", item_name)
        return None
    return h
