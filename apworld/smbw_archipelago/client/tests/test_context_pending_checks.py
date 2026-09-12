"""Tests for checks emitted before the AP server is connected.

Regression for the 2026-09-12 report: the Switch connected to the bridge
~18 min before the player ran /connect.  Shop badges already owned at save
load ("Coin Reward Badge Obtained", ...) and a course clear ("W1: Parachute
Cap I - Normal Exit") hit handle_check_emitted with no reverse maps, were
dropped, and -- because BridgeState had already dedup-recorded them --
never re-sent for the rest of the session.

Contract pinned here:
  1. A pre-connect check is queued, not dropped, and sent on Connected.
  2. The replay honours slot_data toggles delivered with Connected.
  3. Every sent location lands in CommonContext.locations_checked so the
     base client re-sends it after a mid-session reconnect.

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
    "ensure conftest.py is loaded); skipping pending-check tests.")
class TestContextPendingChecks(unittest.IsolatedAsyncioTestCase):

    COIN_REWARD_BIT = 9
    COIN_REWARD_LOC_NAME = "Coin Reward Badge Obtained"
    COIN_REWARD_LOC_ID = 501

    PARACHUTE_CAP_I_STAGE_KEY = 0xA8D377AB
    PARACHUTE_CAP_I_LOC_NAME = "W1: Parachute Cap I - Normal Exit"
    PARACHUTE_CAP_I_LOC_ID = 502

    async def asyncSetUp(self) -> None:  # type: ignore[override]
        from ..context import SMBWContext
        from ..state import BridgeState

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

        # Connected normally builds the maps from the DataPackage; stand in
        # with a fixed table so the test controls "connected or not".
        def _rebuild() -> None:
            self.ctx._location_name_to_id = {
                self.COIN_REWARD_LOC_NAME: self.COIN_REWARD_LOC_ID,
                self.PARACHUTE_CAP_I_LOC_NAME: self.PARACHUTE_CAP_I_LOC_ID,
            }
        self.ctx._rebuild_reverse_maps_from_context = _rebuild

    async def asyncTearDown(self) -> None:  # type: ignore[override]
        try:
            await self.ctx.shutdown()
        except Exception:
            pass

    def _location_checks(self) -> list[int]:
        loc_ids: list[int] = []
        for c in self.ctx.send_msgs.await_args_list:
            for m in c.args[0]:
                if m.get("cmd") == "LocationChecks":
                    loc_ids.extend(m["locations"])
        return loc_ids

    def _status_updates(self) -> list[int]:
        return [m["status"]
                for c in self.ctx.send_msgs.await_args_list
                for m in c.args[0] if m.get("cmd") == "StatusUpdate"]

    async def _emit_pre_connect_checks(self) -> None:
        from ..protocol import CheckEmitted, CheckKind
        await self.ctx.handle_check_emitted(CheckEmitted(
            kind=CheckKind.BADGE_ACQUIRED, stage_key=self.COIN_REWARD_BIT))
        await self.ctx.handle_check_emitted(CheckEmitted(
            kind=CheckKind.NORMAL_EXIT,
            stage_key=self.PARACHUTE_CAP_I_STAGE_KEY))

    async def test_pre_connect_checks_are_sent_on_connected(self):
        await self._emit_pre_connect_checks()
        self.assertEqual(self._location_checks(), [])
        self.assertEqual(len(self.ctx._pending_checks), 2)

        await self.ctx._handle_ap_package("Connected", {"slot_data": {}})

        self.assertCountEqual(
            self._location_checks(),
            [self.COIN_REWARD_LOC_ID, self.PARACHUTE_CAP_I_LOC_ID])
        self.assertEqual(self.ctx._pending_checks, [])

    async def test_second_connected_does_not_resend_queue(self):
        await self._emit_pre_connect_checks()
        await self.ctx._handle_ap_package("Connected", {"slot_data": {}})
        self.ctx.send_msgs.reset_mock()
        await self.ctx._handle_ap_package("Connected", {"slot_data": {}})
        self.assertEqual(self._location_checks(), [])

    async def test_sent_locations_recorded_for_reconnect_resend(self):
        await self._emit_pre_connect_checks()
        await self.ctx._handle_ap_package("Connected", {"slot_data": {}})
        self.assertTrue(
            {self.COIN_REWARD_LOC_ID, self.PARACHUTE_CAP_I_LOC_ID}
            <= self.ctx.locations_checked)

    async def test_pre_connect_goal_location_signals_goal_on_connected(self):
        from NetUtils import ClientStatus  # type: ignore
        await self._emit_pre_connect_checks()
        self.assertNotIn(ClientStatus.CLIENT_GOAL, self._status_updates())

        await self.ctx._handle_ap_package("Connected", {"slot_data": {
            "goal_location_name": self.PARACHUTE_CAP_I_LOC_NAME}})

        self.assertIn(ClientStatus.CLIENT_GOAL, self._status_updates())

    async def test_pre_connect_char_block_respects_slot_data_off(self):
        from ..protocol import CheckEmitted, CheckKind
        await self.ctx.handle_check_emitted(CheckEmitted(
            kind=CheckKind.CHARACTER_BLOCK,
            stage_key=self.PARACHUTE_CAP_I_STAGE_KEY,
            metadata={"chara": 1}))
        self.assertEqual(len(self.ctx._pending_checks), 1)

        await self.ctx._handle_ap_package("Connected", {"slot_data": {
            "character_block_sanity": False}})

        self.assertEqual(self._location_checks(), [])
        self.assertEqual(self.ctx._pending_checks, [])


if __name__ == "__main__":
    unittest.main()
