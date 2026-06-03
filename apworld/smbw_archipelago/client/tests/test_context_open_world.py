"""Tests for open-world mode on the client side.

Pins: Connected reads ``open_world_active`` / ``palaces_required`` from
slot_data and pushes the routable-world mask; receiving enough active
Royal Seeds forces all six seeds + flags the Castle routable exactly
once; the routable/royal-seed providers return the right values for the
LanServer replay path; and non-open-world seeds stay a no-op.

Same Archipelago-availability guard pattern as the other test_context_*
files.
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
    "ensure conftest.py is loaded); skipping open-world tests.")
class TestContextOpenWorld(unittest.IsolatedAsyncioTestCase):

    # Royal Seed item ids -> names (mask bits: W1=0, W3=2, W5=4).
    W1_SEED = 300
    W3_SEED = 302
    W5_SEED = 304

    async def asyncSetUp(self) -> None:  # type: ignore[override]
        from ..context import SMBWContext
        from ..state import BridgeState

        import CommonClient  # type: ignore
        CommonClient.network_data_package["games"].setdefault(
            "Super Mario Bros Wonder",
            {"checksum": "", "item_name_to_id": {}, "location_name_to_id": {}})

        self.state = BridgeState()
        self.ctx = SMBWContext(server_address=None, password=None, state=self.state)
        self.ctx.auth = "MarioSlot"
        self.ctx.slot = 1
        self.ctx.player_names = {1: "MarioSlot"}

        self.ctx.send_msgs = AsyncMock()
        self.ctx.update_death_link = AsyncMock()
        self.ctx.lan_server = MagicMock()

        self.ctx.item_names = MagicMock()
        self.ctx.item_names.lookup_in_game = lambda i: {
            self.W1_SEED: "W1 Royal Seed",
            self.W3_SEED: "W3 Royal Seed",
            self.W5_SEED: "W5 Royal Seed",
        }.get(i, f"?{i}")

    async def asyncTearDown(self) -> None:  # type: ignore[override]
        try:
            await self.ctx.shutdown()
        except Exception:
            pass

    async def _connect(self, slot_data: dict) -> None:
        await self.ctx._handle_ap_package("Connected", {"slot_data": slot_data})

    # ---- Connected ----------------------------------------------------

    async def test_connected_reads_open_world_slot_data(self):
        await self._connect({"open_world_active": [1, 3, 5], "palaces_required": 2})
        self.assertTrue(self.ctx.open_world)
        self.assertEqual(self.ctx.open_world_active, [1, 3, 5])
        self.assertEqual(self.ctx.palaces_required, 2)
        self.assertFalse(self.ctx._bowser_opened)

    async def test_connected_pushes_routable_worlds(self):
        await self._connect({"open_world_active": [1, 3, 5], "palaces_required": 2})
        # bits 0,2,4 set -> 0b10101 == 0x15; no Castle bit yet.
        self.ctx.lan_server.send_set_routable_worlds.assert_called_once_with(0x15)

    async def test_connected_without_open_world_is_inactive(self):
        await self._connect({})
        self.assertFalse(self.ctx.open_world)
        self.assertEqual(self.ctx.open_world_active, [])
        self.ctx.lan_server.send_set_routable_worlds.assert_not_called()

    # ---- Threshold -> unlock Bowser -----------------------------------

    async def test_threshold_unlocks_bowser_once(self):
        from .. import royal_seed_table
        await self._connect({"open_world_active": [1, 3, 5], "palaces_required": 2})
        self.ctx.lan_server.reset_mock()

        # Two active Royal Seeds received -> threshold met.
        self.ctx.items_received = [{"item": self.W1_SEED}, {"item": self.W3_SEED}]
        await self.ctx._handle_received_items(
            {"items": [{"item": self.W1_SEED}, {"item": self.W3_SEED}]})

        self.assertTrue(self.ctx._bowser_opened)
        self.ctx.lan_server.send_set_royal_seeds_absolute.assert_called_once_with(
            royal_seed_table.ALL_MASK)
        # Routable mask now includes the Castle bit (0x15 | 0x100).
        self.ctx.lan_server.send_set_routable_worlds.assert_called_with(0x115)

        # A third seed must NOT re-fire the unlock.
        self.ctx.lan_server.reset_mock()
        self.ctx.items_received = [
            {"item": self.W1_SEED}, {"item": self.W3_SEED}, {"item": self.W5_SEED}]
        await self.ctx._handle_received_items({"items": [{"item": self.W5_SEED}]})
        self.ctx.lan_server.send_set_royal_seeds_absolute.assert_not_called()

    async def test_below_threshold_does_not_unlock(self):
        await self._connect({"open_world_active": [1, 3, 5], "palaces_required": 3})
        self.ctx.lan_server.reset_mock()
        self.ctx.items_received = [{"item": self.W1_SEED}]
        await self.ctx._handle_received_items({"items": [{"item": self.W1_SEED}]})
        self.assertFalse(self.ctx._bowser_opened)
        self.ctx.lan_server.send_set_royal_seeds_absolute.assert_not_called()

    # ---- Providers (LanServer replay path) ----------------------------

    async def test_routable_mask_provider(self):
        await self._connect({"open_world_active": [2, 4], "palaces_required": 0})
        # W2=bit1, W4=bit3 -> 0b1010 == 0xA.
        self.assertEqual(self.ctx._recompute_routable_worlds_mask(), 0xA)
        self.ctx._bowser_opened = True
        self.assertEqual(self.ctx._recompute_routable_worlds_mask(), 0xA | 0x100)

    async def test_routable_mask_provider_inactive_returns_zero(self):
        await self._connect({})
        self.assertEqual(self.ctx._recompute_routable_worlds_mask(), 0)

    async def test_royal_seed_provider_only_after_unlock(self):
        from .. import royal_seed_table
        await self._connect({"open_world_active": [1, 2], "palaces_required": 1})
        self.assertIsNone(self.ctx._open_world_royal_seed_mask())
        self.ctx._bowser_opened = True
        self.assertEqual(
            self.ctx._open_world_royal_seed_mask(), royal_seed_table.ALL_MASK)

    async def test_royal_seed_provider_none_outside_open_world(self):
        await self._connect({})
        self.ctx._bowser_opened = True  # shouldn't happen, but be defensive
        self.assertIsNone(self.ctx._open_world_royal_seed_mask())


if __name__ == "__main__":
    unittest.main()
