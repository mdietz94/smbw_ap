"""Tests for the AP-authoritative power-up pickup deny mask.

Power-Up items are pool items (no longer precollected); the Switch denies
in-level pickups for every gated power-up whose AP item hasn't been
received (the ItemGetMaskBuild trampoline strips the bits).  These tests
pin the client-side mask derivation: the slot_data gate, the per-item bit
clears, the `/deny_powerups` override, and the ReceivedItems push.

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
    "ensure conftest.py is loaded); skipping powerup-gate tests.")
class TestContextPowerupGate(unittest.IsolatedAsyncioTestCase):

    FIRE_ITEM_ID = 300
    ELEPHANT_ITEM_ID = 301
    DRILL_ITEM_ID = 302
    BUBBLE_ITEM_ID = 303
    OTHER_ITEM_ID = 310

    async def asyncSetUp(self) -> None:  # type: ignore[override]
        from ..context import SMBWContext
        from ..state import BridgeState
        from .. import powerup_table
        self.powerup_table = powerup_table

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
            self.FIRE_ITEM_ID: "Fire Flower",
            self.ELEPHANT_ITEM_ID: "Elephant Fruit",
            self.DRILL_ITEM_ID: "Drill Mushroom",
            self.BUBBLE_ITEM_ID: "Bubble Flower",
            self.OTHER_ITEM_ID: "Standee",
        }.get(i, f"?{i}")

    def _recv(self, *item_ids: int) -> None:
        for iid in item_ids:
            it = MagicMock()
            it.item = iid
            self.ctx.items_received.append(it)

    # ---- mask derivation -------------------------------------------------

    def test_not_gating_means_vanilla(self) -> None:
        # Seed without the powerup_gating marker (old seed): mask 0 even
        # with no power-up items received.
        self.assertFalse(self.ctx.powerup_gating)
        self.assertEqual(self.ctx._recompute_itemget_deny_mask(), 0)

    def test_gating_with_no_items_denies_all(self) -> None:
        self.ctx.powerup_gating = True
        self.assertEqual(
            self.ctx._recompute_itemget_deny_mask(),
            self.powerup_table.GATED_MASK)

    def test_each_item_clears_its_bit(self) -> None:
        self.ctx.powerup_gating = True
        self._recv(self.FIRE_ITEM_ID)
        mask = self.ctx._recompute_itemget_deny_mask()
        self.assertEqual(
            mask, self.powerup_table.GATED_MASK & ~(1 << 2))
        self._recv(self.ELEPHANT_ITEM_ID, self.DRILL_ITEM_ID)
        mask = self.ctx._recompute_itemget_deny_mask()
        self.assertEqual(
            mask,
            self.powerup_table.GATED_MASK & ~((1 << 2) | (1 << 5) | (1 << 12)))

    def test_all_items_means_vanilla(self) -> None:
        self.ctx.powerup_gating = True
        self._recv(self.FIRE_ITEM_ID, self.ELEPHANT_ITEM_ID,
                   self.DRILL_ITEM_ID, self.BUBBLE_ITEM_ID)
        self.assertEqual(self.ctx._recompute_itemget_deny_mask(), 0)

    def test_super_mushroom_not_gated(self) -> None:
        # 2026-06-10 decision: regular Super Mushrooms stay vanilla
        # pickups -- bit 1 (Kinoko) is never in the AP-derived mask.
        self.ctx.powerup_gating = True
        self.assertFalse(self.powerup_table.GATED_MASK & (1 << 1))
        self.assertNotIn("Super Mushroom", self.powerup_table.DENY_BIT_FOR_ITEM)

    def test_non_powerup_items_ignored(self) -> None:
        self.ctx.powerup_gating = True
        self._recv(self.OTHER_ITEM_ID)
        self.assertEqual(
            self.ctx._recompute_itemget_deny_mask(),
            self.powerup_table.GATED_MASK)

    def test_override_wins_and_clears(self) -> None:
        self.ctx.powerup_gating = True
        self.ctx.set_itemget_deny_override(0x4)
        self.assertEqual(self.ctx._recompute_itemget_deny_mask(), 0x4)
        self.ctx.lan_server.send_set_itemget_deny.assert_called_with(0x4)
        self.ctx.set_itemget_deny_override(None)
        self.assertEqual(
            self.ctx._recompute_itemget_deny_mask(),
            self.powerup_table.GATED_MASK)

    # ---- push triggers ----------------------------------------------------

    async def test_received_items_pushes_mask(self) -> None:
        self.ctx.powerup_gating = True
        self._recv(self.BUBBLE_ITEM_ID)
        await self.ctx._handle_received_items({"items": []})
        self.ctx.lan_server.send_set_itemget_deny.assert_called_with(
            self.powerup_table.GATED_MASK & ~(1 << 18))

    async def test_connected_reads_slot_data_and_pushes(self) -> None:
        await self.ctx._handle_ap_package(
            "Connected", {"slot_data": {"powerup_gating": True}})
        self.assertTrue(self.ctx.powerup_gating)
        self.ctx.lan_server.send_set_itemget_deny.assert_called_with(
            self.powerup_table.GATED_MASK)

    async def test_connected_old_seed_stays_vanilla(self) -> None:
        await self.ctx._handle_ap_package("Connected", {"slot_data": {}})
        self.assertFalse(self.ctx.powerup_gating)
        self.ctx.lan_server.send_set_itemget_deny.assert_called_with(0)


if __name__ == "__main__":
    unittest.main()
