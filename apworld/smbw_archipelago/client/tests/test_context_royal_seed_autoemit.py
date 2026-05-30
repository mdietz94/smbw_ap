"""Tests for the AP-grant -> PALACE_CLEAR auto-emit path for Royal Seeds.

When the bridge receives a Royal Seed item from AP, the Switch's
container-B bool gets set immediately -- which flips the world-map UI to
"palace cleared", giving the player no in-game reason to re-enter the
palace.  The natural ``PALACE_CLEAR`` PlayReport that normally fires the
matching ``- Royal Seed`` AP location therefore never arrives, leaving
the location unreachable unless the bridge resolves it directly at
grant time.  These tests pin that auto-emit behavior.

The companion RE plan in ``docs/royal-seed-check-loss-re-plan.md``
tracks the investigation of why the natural path fails and whether
there's a fix that doesn't require this short-circuit.

Same Archipelago-availability guard pattern as the other
test_context_* files (mirrors test_context_badge_autoemit.py).
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
    "ensure conftest.py is loaded); skipping royal-seed-autoemit tests.")
class TestContextRoyalSeedAutoEmit(unittest.IsolatedAsyncioTestCase):

    # W1 Royal Seed: palace stage_key 0x89927C97 (Pipe-Rock Plateau Palace)
    W1_SEED_ITEM_ID = 300
    W1_SEED_LOC_ID = 700
    W1_PALACE_STAGE_KEY = 0x89927C97

    # W3 Royal Seed: palace stage_key 0xA5E2BB3A (Royal Seed Mansion).
    # Verifies the W3 special-case where the "palace" is actually a
    # mansion -- mapping is in royal_seed_table, not derived.
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

    # ---- Core auto-emit behavior --------------------------------------

    async def test_royal_seed_receipt_sends_location_check(self):
        await self.ctx._handle_received_items(
            {"items": [{"item": self.W1_SEED_ITEM_ID}]})
        # SetRoyalSeedsAbsolute still flows -- the absolute push wasn't
        # disturbed.
        self.ctx.lan_server.send_set_royal_seeds_absolute.assert_called_once()
        # And a LocationChecks message for the W1 palace fires.
        sent = [c.args[0] for c in self.ctx.send_msgs.await_args_list]
        loc_checks = [
            msg[0] for msg in sent
            if msg and msg[0].get("cmd") == "LocationChecks"
        ]
        self.assertTrue(
            any(self.W1_SEED_LOC_ID in m["locations"] for m in loc_checks),
            f"expected LocationChecks containing {self.W1_SEED_LOC_ID}, "
            f"got {sent!r}")

    async def test_royal_seed_receipt_records_in_bridge_state(self):
        from ..protocol import CheckKind
        await self.ctx._handle_received_items(
            {"items": [{"item": self.W3_SEED_ITEM_ID}]})
        self.assertTrue(self.state.has_emitted(
            CheckKind.PALACE_CLEAR, self.W3_PALACE_STAGE_KEY))

    async def test_royal_seed_receipt_dedups_across_replays(self):
        # Two ReceivedItems batches with the same seed -- the second
        # must NOT trigger a second LocationCheck or a second state record.
        await self.ctx._handle_received_items(
            {"items": [{"item": self.W1_SEED_ITEM_ID}]})
        first_send_count = self.ctx.send_msgs.await_count
        await self.ctx._handle_received_items(
            {"items": [{"item": self.W1_SEED_ITEM_ID}]})
        self.assertEqual(self.ctx.send_msgs.await_count, first_send_count,
                         "second receipt of the same seed re-sent")

    async def test_multiple_seeds_in_one_batch_each_get_a_check(self):
        await self.ctx._handle_received_items({"items": [
            {"item": self.W1_SEED_ITEM_ID},
            {"item": self.W3_SEED_ITEM_ID},
            {"item": self.W6_SEED_ITEM_ID},
        ]})
        sent = [c.args[0] for c in self.ctx.send_msgs.await_args_list]
        loc_ids: set[int] = set()
        for msg in sent:
            for m in msg:
                if m.get("cmd") == "LocationChecks":
                    loc_ids.update(m["locations"])
        self.assertEqual(
            loc_ids,
            {self.W1_SEED_LOC_ID, self.W3_SEED_LOC_ID, self.W6_SEED_LOC_ID})

    # ---- Negative paths -----------------------------------------------

    async def test_non_seed_item_does_not_emit_palace_clear(self):
        # "10 Coin" takes the increment path; PALACE_CLEAR auto-emit
        # must not fire.
        from ..protocol import CheckKind
        await self.ctx._handle_received_items(
            {"items": [{"item": self.TEN_COIN_ITEM_ID}]})
        self.assertEqual(
            self.state.count_emitted(CheckKind.PALACE_CLEAR), 0)


if __name__ == "__main__":
    unittest.main()
