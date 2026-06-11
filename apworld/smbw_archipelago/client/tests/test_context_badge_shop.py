"""Tests for AP-authoritative Poplin badge-shop ownership (2026-06-10).

The Switch badge-shop override (wire.SetBadgeShopStateMsg) is driven by
``SMBWContext._recompute_badge_shop_state``: ``managed`` = every shop badge
(an AP check), ``sold`` = the managed badges whose "<Badge> Obtained"
location is obtained (server-confirmed via ``checked_locations`` or sent
this session via ``_sent_loc_ids``).  These tests pin that recompute and
the push points (Connected, RoomUpdate, on-check-emitted).

Same Archipelago-availability guard as the other test_context_* files.
"""

from __future__ import annotations

import asyncio
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
    "ensure conftest.py is loaded); skipping badge-shop tests.")
class TestContextBadgeShop(unittest.IsolatedAsyncioTestCase):

    COIN_REWARD_BIT = 9
    COIN_REWARD_LOC_ID = 501
    COIN_REWARD_LOC_NAME = "Coin Reward Badge Obtained"

    async def asyncSetUp(self) -> None:  # type: ignore[override]
        from ..context import SMBWContext
        from ..state import BridgeState
        from .. import location_table

        self._SMBWContext = SMBWContext
        self._shop_locations = location_table.shop_badge_location_names()

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
        self.ctx.lan_server = MagicMock()

        # Map every shop-badge location to a synthetic AP id, and mark them
        # all present in this slot (the server's location set).
        self.ctx._location_name_to_id = {
            name: 1000 + bit
            for bit, name in self._shop_locations.items()
        }
        self.ctx.server_locations = set(self.ctx._location_name_to_id.values())

    async def asyncTearDown(self) -> None:  # type: ignore[override]
        try:
            await self.ctx.shutdown()
        except Exception:
            pass

    def _expected_managed(self) -> int:
        m = 0
        for bit in self._shop_locations:
            m |= (1 << bit)
        return m

    async def test_managed_is_all_shop_badges(self):
        managed, sold = self.ctx._recompute_badge_shop_state()
        self.assertEqual(managed, self._expected_managed())
        self.assertNotEqual(managed, 0)
        self.assertEqual(sold, 0)  # nothing checked yet

    async def test_managed_independent_of_reverse_maps(self):
        # managed must be correct even before the DataPackage maps exist.
        self.ctx._location_name_to_id = {}
        managed, sold = self.ctx._recompute_badge_shop_state()
        self.assertEqual(managed, self._expected_managed())
        self.assertEqual(sold, 0)

    async def test_sold_from_checked_locations(self):
        loc_id = self.ctx._location_name_to_id[self.COIN_REWARD_LOC_NAME]
        self.ctx.checked_locations.add(loc_id)
        managed, sold = self.ctx._recompute_badge_shop_state()
        self.assertTrue((sold >> self.COIN_REWARD_BIT) & 1)
        # Only the checked badge is sold; managed is unchanged.
        self.assertEqual(sold, 1 << self.COIN_REWARD_BIT)
        self.assertEqual(managed, self._expected_managed())

    async def test_sold_from_sent_loc_ids(self):
        # Optimistic: a locally-sent (not yet server-confirmed) check marks
        # the badge sold-out so it doesn't flicker back to purchasable.
        loc_id = self.ctx._location_name_to_id[self.COIN_REWARD_LOC_NAME]
        self.ctx._sent_loc_ids.add(loc_id)
        _managed, sold = self.ctx._recompute_badge_shop_state()
        self.assertTrue((sold >> self.COIN_REWARD_BIT) & 1)

    async def test_push_sends_wire_message(self):
        self.ctx._push_badge_shop_state()
        self.ctx.lan_server.send_set_badge_shop_state.assert_called_once()
        managed, sold = (
            self.ctx.lan_server.send_set_badge_shop_state.call_args.args)
        self.assertEqual(managed, self._expected_managed())
        self.assertEqual(sold, 0)

    async def test_push_noop_without_lan_server(self):
        self.ctx.lan_server = None
        # Must not raise when no Switch client is bound.
        self.ctx._push_badge_shop_state()

    # ---- AP shop-text (scouted check) ---------------------------------

    def _scout(self, loc_name: str, item_id: int, player: int) -> None:
        """Inject a scouted NetworkItem into locations_info for a shop
        badge location (mimics the LocationScouts reply)."""
        from NetUtils import NetworkItem
        loc_id = self.ctx._location_name_to_id[loc_name]
        self.ctx.locations_info[loc_id] = NetworkItem(
            item_id, loc_id, player, 0)

    async def test_shop_text_empty_before_scout(self):
        self.assertEqual(self.ctx._recompute_badge_shop_text(), {})

    async def test_shop_text_from_scouted_item(self):
        self.ctx.player_names = {1: "MarioSlot", 2: "Zelda"}
        self.ctx.slot = 1
        # Resolve any item id to a fixed name regardless of slot.
        self.ctx.item_names = MagicMock()
        self.ctx.item_names.lookup_in_slot = lambda code, slot: "Master Sword"
        self._scout(self.COIN_REWARD_LOC_NAME, item_id=555, player=2)

        text = self.ctx._recompute_badge_shop_text()
        self.assertIn(self.COIN_REWARD_BIT, text)
        self.assertEqual(text[self.COIN_REWARD_BIT], "Zelda's Master Sword")

    async def test_shop_text_own_item_phrasing(self):
        self.ctx.player_names = {1: "MarioSlot"}
        self.ctx.slot = 1
        self.ctx.item_names = MagicMock()
        self.ctx.item_names.lookup_in_slot = lambda code, slot: "Spring Feet Badge"
        self._scout(self.COIN_REWARD_LOC_NAME, item_id=100, player=1)

        text = self.ctx._recompute_badge_shop_text()
        self.assertEqual(text[self.COIN_REWARD_BIT], "Your Spring Feet Badge")

    async def test_shop_text_item_name_fallback(self):
        # When the item name can't be resolved, fall back to "item <id>".
        self.ctx.player_names = {2: "Zelda"}
        self.ctx.item_names = MagicMock()
        self.ctx.item_names.lookup_in_slot = MagicMock(side_effect=KeyError)
        self.ctx.item_names.lookup_in_game = MagicMock(side_effect=KeyError)
        self._scout(self.COIN_REWARD_LOC_NAME, item_id=777, player=2)

        text = self.ctx._recompute_badge_shop_text()
        self.assertEqual(text[self.COIN_REWARD_BIT], "Zelda's item 777")

    async def test_scout_sends_location_scouts(self):
        self.ctx.send_msgs.reset_mock()
        await self.ctx._scout_shop_badge_locations()
        scouted = [
            m for c in self.ctx.send_msgs.await_args_list for m in c.args[0]
            if m.get("cmd") == "LocationScouts"
        ]
        self.assertTrue(scouted, "expected a LocationScouts message")
        # All shop-badge loc ids requested.
        wanted = set(self.ctx._shop_badge_location_ids())
        got = set(scouted[0]["locations"])
        self.assertEqual(got, wanted)

    async def test_scout_excludes_locations_not_in_slot(self):
        # A shop-badge location absent from this seed (not in
        # server_locations) must NOT be scouted -- scouting an unknown id
        # drops the server connection.
        all_ids = set(self.ctx._location_name_to_id.values())
        missing = self.ctx._location_name_to_id[self.COIN_REWARD_LOC_NAME]
        self.ctx.server_locations = all_ids - {missing}
        ids = self.ctx._shop_badge_location_ids()
        self.assertNotIn(missing, ids)
        self.assertTrue(set(ids).issubset(self.ctx.server_locations))

    async def test_scout_noop_when_no_known_locations(self):
        self.ctx.server_locations = set()
        self.ctx.send_msgs.reset_mock()
        await self.ctx._scout_shop_badge_locations()
        scouted = [
            m for c in self.ctx.send_msgs.await_args_list for m in c.args[0]
            if m.get("cmd") == "LocationScouts"
        ]
        self.assertEqual(scouted, [])

    async def test_check_emitted_pushes_sold_state(self):
        from ..protocol import CheckEmitted, CheckKind
        check = CheckEmitted(
            kind=CheckKind.BADGE_ACQUIRED, stage_key=self.COIN_REWARD_BIT)
        await self.ctx.handle_check_emitted(check)
        # The shop-state push fired and the just-bought badge is sold-out.
        self.ctx.lan_server.send_set_badge_shop_state.assert_called()
        managed, sold = (
            self.ctx.lan_server.send_set_badge_shop_state.call_args.args)
        self.assertTrue((sold >> self.COIN_REWARD_BIT) & 1)
        # And the LocationCheck went out.
        loc_id = self.ctx._location_name_to_id[self.COIN_REWARD_LOC_NAME]
        sent = [
            m for c in self.ctx.send_msgs.await_args_list for m in c.args[0]
            if m.get("cmd") == "LocationChecks"
        ]
        self.assertTrue(any(loc_id in m["locations"] for m in sent))


if __name__ == "__main__":
    unittest.main()
