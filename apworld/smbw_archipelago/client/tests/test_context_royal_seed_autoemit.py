"""Tests for Royal Seed item receipt now that seeds are vanilla-owned.

Royal Seeds used to be AP-authoritative: receiving the item pushed a
``SetRoyalSeedsAbsolute`` to the Switch AND auto-resolved the matching
palace ``- Royal Seed`` location (because clobbering the container-B
bool flipped the world-map UI to "cleared", suppressing the natural
``PALACE_CLEAR`` PlayReport).

Both of those were removed (2026-06-03): the vanilla game owns Royal
Seed state, the player re-enters the palace, and the natural
``koopajr_result`` -> ``PALACE_CLEAR`` path fires the AP check.  These
tests pin the NEW behavior: receiving a Royal Seed item is a no-op on
both the Switch-push and AP-LocationCheck sides.

Same Archipelago-availability guard pattern as the other
test_context_* files.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock


def _try_import_archipelago() -> bool:
    try:
        import CommonClient  # noqa: F401
        return True
    except Exception:
        return False


_ARCHIPELAGO_AVAILABLE = _try_import_archipelago()


@unittest.skipUnless(
    _ARCHIPELAGO_AVAILABLE,
    "Archipelago not importable (run `git submodule update --init` and "
    "ensure conftest.py is loaded); skipping royal-seed-receipt tests.")
class TestContextRoyalSeedReceipt(unittest.IsolatedAsyncioTestCase):

    # W1 Royal Seed: palace stage_key 0x89927C97 (Pipe-Rock Plateau Palace)
    W1_SEED_ITEM_ID = 300
    W1_SEED_LOC_ID = 700
    W1_PALACE_STAGE_KEY = 0x89927C97

    # W3 Royal Seed: palace stage_key 0xA5E2BB3A (Royal Seed Mansion).
    W3_SEED_ITEM_ID = 302
    W3_SEED_LOC_ID = 702
    W3_PALACE_STAGE_KEY = 0xA5E2BB3A

    # W6 Royal Seed: palace stage_key 0x7E523816 (Deep Magma Bog Palace)
    W6_SEED_ITEM_ID = 305
    W6_SEED_LOC_ID = 705

    # Non-seed: a known-good "10 Coin" routed through the increment path.
    TEN_COIN_ITEM_ID = 200

    async def asyncSetUp(self) -> None:  # type: ignore[override]
        from ..context import SMBWContext
        from ..state import BridgeState
        self._SMBWContext = SMBWContext

        import CommonClient  # type: ignore
        CommonClient.network_data_package["games"].setdefault(
            "Super Mario Bros Wonder",
            {"checksum": "", "item_name_to_id": {}, "location_name_to_id": {}})

        self.state = BridgeState()
        self.ctx = SMBWContext(
            server_address=None, password=None, state=self.state)
        self.ctx.auth = "MarioSlot"
        self.ctx.slot = 1
        self.ctx.player_names = {1: "MarioSlot"}

        self.ctx.send_msgs = AsyncMock()
        self.ctx.update_death_link = AsyncMock()
        self.ctx.lan_server = MagicMock()

        self.ctx.item_names = MagicMock()
        self.ctx.item_names.lookup_in_game = lambda i: {
            self.W1_SEED_ITEM_ID: "W1 Royal Seed",
            self.W3_SEED_ITEM_ID: "W3 Royal Seed",
            self.W6_SEED_ITEM_ID: "W6 Royal Seed",
            self.TEN_COIN_ITEM_ID: "10 Coin",
        }.get(i, f"?{i}")

        self.ctx._location_name_to_id = {
            "W1: Pipe-Rock Plateau Palace - Royal Seed": self.W1_SEED_LOC_ID,
            "W3: Royal Seed Mansion - Royal Seed": self.W3_SEED_LOC_ID,
            "W6: Deep Magma Bog Palace - Royal Seed": self.W6_SEED_LOC_ID,
        }

    async def asyncTearDown(self) -> None:  # type: ignore[override]
        try:
            await self.ctx.shutdown()
        except Exception:
            pass

    def _location_checks_sent(self) -> set[int]:
        loc_ids: set[int] = set()
        for call in self.ctx.send_msgs.await_args_list:
            for m in call.args[0]:
                if m.get("cmd") == "LocationChecks":
                    loc_ids.update(m["locations"])
        return loc_ids

    # ---- Vanilla-owned: receipt is a no-op ---------------------------

    async def test_royal_seed_receipt_does_not_push_to_switch(self):
        await self.ctx._handle_received_items(
            {"items": [{"item": self.W1_SEED_ITEM_ID}]})
        self.ctx.lan_server.send_set_royal_seeds_absolute.assert_not_called()

    async def test_royal_seed_receipt_does_not_auto_resolve_location(self):
        # No auto-resolve: the palace location must come from the natural
        # PALACE_CLEAR path, not from item receipt.
        await self.ctx._handle_received_items(
            {"items": [{"item": self.W1_SEED_ITEM_ID}]})
        self.assertNotIn(self.W1_SEED_LOC_ID, self._location_checks_sent())

    async def test_royal_seed_receipt_does_not_record_palace_clear(self):
        from ..protocol import CheckKind
        await self.ctx._handle_received_items(
            {"items": [{"item": self.W3_SEED_ITEM_ID}]})
        self.assertFalse(self.state.has_emitted(
            CheckKind.PALACE_CLEAR, self.W3_PALACE_STAGE_KEY))

    async def test_multiple_seeds_in_one_batch_emit_no_checks(self):
        await self.ctx._handle_received_items({"items": [
            {"item": self.W1_SEED_ITEM_ID},
            {"item": self.W3_SEED_ITEM_ID},
            {"item": self.W6_SEED_ITEM_ID},
        ]})
        self.assertEqual(self._location_checks_sent(), set())

    # ---- Negative paths ----------------------------------------------

    async def test_non_seed_item_does_not_emit_palace_clear(self):
        # "10 Coin" takes the increment path; PALACE_CLEAR must not fire.
        from ..protocol import CheckKind
        await self.ctx._handle_received_items(
            {"items": [{"item": self.TEN_COIN_ITEM_ID}]})
        self.assertEqual(
            self.state.count_emitted(CheckKind.PALACE_CLEAR), 0)


if __name__ == "__main__":
    unittest.main()
