"""Tests for the M3.8 DeathLink wiring in :mod:`...client.context`.

Exercises the four behaviors:
  1. ``on_deathlink`` forwards to ``lan_server.send_kill`` when enabled.
  2. ``on_deathlink`` drops self-sourced bounces (belt-and-braces self-ping
     guard over CommonClient's timestamp-based gate).
  3. ``handle_death_reported`` calls ``send_death("mario_died")`` only when
     deathlink is enabled.
  4. ``_handle_ap_package("Connected", ...)`` reads ``slot_data.death_link``
     and toggles the connection tag via ``update_death_link``.

Archipelago is loaded onto sys.path by the repo-root conftest.py; if it's
missing the entire module's imports would fail at collection.  We gate
the test class with @skipUnless so the suite still passes in environments
that don't ship the vendor/Archipelago submodule.
"""

from __future__ import annotations

import asyncio
import sys
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
    "ensure conftest.py is loaded); skipping context DeathLink tests.")
class TestContextDeathLink(unittest.IsolatedAsyncioTestCase):
    """Behavioral tests against a real SMBWContext with the network bits
    mocked.  CommonContext doesn't need a live server to construct."""

    async def asyncSetUp(self) -> None:  # type: ignore[override]
        # CommonContext.__init__ spawns a keep_alive Task during ctor;
        # that requires a running event loop, hence asyncSetUp rather
        # than setUp.  Local imports so the @skipUnless on the class
        # short-circuits before we try to construct anything when the
        # AP framework isn't reachable.
        from ..context import SMBWContext
        from ..protocol import DeathReported, GoalCompleted
        from ..state import BridgeState
        self._SMBWContext = SMBWContext
        self._DeathReported = DeathReported
        self._GoalCompleted = GoalCompleted

        # CommonContext.__init__ reads `network_data_package["games"][game]`
        # to seed its checksums dict.  In a pytest run we haven't been
        # discovered by Archipelago's worlds loader (the .apworld lives in
        # custom_worlds/ only for live runs), so stub the entry to avoid
        # KeyError.  Production runs (via Launcher / run_client.py) hit a
        # fully-populated package via custom_worlds discovery.
        import CommonClient  # type: ignore
        CommonClient.network_data_package["games"].setdefault(
            "SMBWonder", {"checksum": "", "item_name_to_id": {}, "location_name_to_id": {}})

        self.state = BridgeState()
        self.ctx = SMBWContext(
            server_address=None, password=None, state=self.state)
        self.ctx.auth = "MarioSlot"
        self.ctx.slot = 1
        self.ctx.player_names = {1: "MarioSlot"}

        # Patch out the network-talking AP methods that on_deathlink /
        # handle_death_reported / Connected would otherwise invoke.
        self.ctx.send_msgs = AsyncMock()             # type: ignore[assignment]
        self.ctx.send_death = AsyncMock()            # type: ignore[assignment]
        self.ctx.update_death_link = AsyncMock()     # type: ignore[assignment]

        # LAN server is just a stand-in -- we only assert that send_kill
        # is invoked with the right shape.
        self.ctx.lan_server = MagicMock()

    async def asyncTearDown(self) -> None:  # type: ignore[override]
        # CommonContext leaves a keep_alive task running; shut it down so
        # the test loop closes cleanly without "Task was destroyed but
        # is pending" warnings.
        try:
            await self.ctx.shutdown()
        except Exception:
            pass

    # ---- on_deathlink ----------------------------------------------

    def test_on_deathlink_forwards_to_lan_server(self):
        self.ctx.deathlink_enabled = True
        self.ctx.on_deathlink({
            "time": 1234.5,
            "source": "Friend",
            "cause": "fell in a pit",
        })
        self.ctx.lan_server.send_kill.assert_called_once_with(
            source="Friend", cause="fell in a pit")

    def test_on_deathlink_no_forward_when_disabled(self):
        self.ctx.deathlink_enabled = False
        self.ctx.on_deathlink({
            "time": 1.0,
            "source": "Friend",
            "cause": "x",
        })
        self.ctx.lan_server.send_kill.assert_not_called()

    def test_on_deathlink_drops_self_sourced_bounce(self):
        self.ctx.deathlink_enabled = True
        # Source equals our own player name -> drop.
        self.ctx.on_deathlink({
            "time": 1.0,
            "source": "MarioSlot",
            "cause": "mario_died",
        })
        self.ctx.lan_server.send_kill.assert_not_called()

    def test_on_deathlink_with_no_lan_server_just_logs(self):
        self.ctx.deathlink_enabled = True
        self.ctx.lan_server = None
        # Must not raise even though there's no Switch bound.
        self.ctx.on_deathlink({
            "time": 1.0,
            "source": "Friend",
            "cause": "x",
        })

    # ---- handle_death_reported -------------------------------------

    async def test_handle_death_reported_enabled_sends_bounce(self):
        self.ctx.deathlink_enabled = True
        await self.ctx.handle_death_reported(self._DeathReported(seq=5))
        self.ctx.send_death.assert_awaited_once_with("mario_died")

    async def test_handle_death_reported_disabled_no_send(self):
        self.ctx.deathlink_enabled = False
        await self.ctx.handle_death_reported(self._DeathReported(seq=5))
        self.ctx.send_death.assert_not_called()

    # ---- Connected -> slot_data death_link toggle -----------------

    async def test_connected_slot_data_enables_deathlink(self):
        self.assertFalse(self.ctx.deathlink_enabled)
        await self.ctx._handle_ap_package(
            "Connected",
            {"slot_data": {"death_link": True}})
        self.assertTrue(self.ctx.deathlink_enabled)
        self.ctx.update_death_link.assert_awaited_with(True)

    async def test_connected_slot_data_false_disables_deathlink(self):
        self.ctx.deathlink_enabled = True
        await self.ctx._handle_ap_package(
            "Connected",
            {"slot_data": {"death_link": False}})
        self.assertFalse(self.ctx.deathlink_enabled)
        self.ctx.update_death_link.assert_awaited_with(False)

    async def test_connected_missing_slot_data_defaults_off(self):
        # No slot_data key at all -> treat as death_link off.
        await self.ctx._handle_ap_package("Connected", {})
        self.assertFalse(self.ctx.deathlink_enabled)
        self.ctx.update_death_link.assert_awaited_with(False)

    # ---- handle_goal_completed (M3.7) ------------------------------

    async def test_handle_goal_completed_sends_status_update(self):
        """The bridge translates a GoalCompleted into one AP
        StatusUpdate with ClientStatus.CLIENT_GOAL (= 30).

        Legacy-fallback shape: ``goal_location_name`` is still None
        (no Connected fired), so the hook assumes Bowser and fires
        unconditionally."""
        from NetUtils import ClientStatus  # type: ignore
        await self.ctx.handle_goal_completed(self._GoalCompleted(seq=1))
        self.ctx.send_msgs.assert_awaited_once_with([{
            "cmd": "StatusUpdate",
            "status": ClientStatus.CLIENT_GOAL,
        }])

    async def test_handle_goal_completed_does_not_depend_on_deathlink(self):
        """Game-completion is independent of DeathLink state -- the AP
        StatusUpdate should fire whether DeathLink is on or off."""
        from NetUtils import ClientStatus  # type: ignore
        self.ctx.deathlink_enabled = False
        await self.ctx.handle_goal_completed(self._GoalCompleted(seq=7))
        self.ctx.send_msgs.assert_awaited_once_with([{
            "cmd": "StatusUpdate",
            "status": ClientStatus.CLIENT_GOAL,
        }])

    async def test_handle_goal_completed_bowser_goal_fires(self):
        """When slot_data declared Bowser as the goal, the Switch's
        Bowser-Nerve hook is the authoritative trigger for
        CLIENT_GOAL."""
        from NetUtils import ClientStatus  # type: ignore
        from ..context import GOAL_LOCATION_NAME_BOWSER
        self.ctx.goal_location_name = GOAL_LOCATION_NAME_BOWSER
        await self.ctx.handle_goal_completed(self._GoalCompleted(seq=2))
        self.ctx.send_msgs.assert_awaited_once_with([{
            "cmd": "StatusUpdate",
            "status": ClientStatus.CLIENT_GOAL,
        }])

    async def test_handle_goal_completed_other_goal_suppressed(self):
        """When the player picked a non-Bowser goal (Wonder Gauntlet /
        WONDER?), defeating Bowser is just a milestone -- the Nerve
        hook must NOT mark the slot done.  CLIENT_GOAL flows through
        ``handle_check_emitted`` on the actual goal-location check."""
        self.ctx.goal_location_name = (
            "Special: Wonder Gauntlet - Normal Exit")
        await self.ctx.handle_goal_completed(self._GoalCompleted(seq=3))
        self.ctx.send_msgs.assert_not_called()

    # ---- Connected -> slot_data goal_location_name ------------------

    async def test_connected_reads_goal_location_name(self):
        from ..context import GOAL_LOCATION_NAME_BOWSER
        self.assertIsNone(self.ctx.goal_location_name)
        await self.ctx._handle_ap_package(
            "Connected",
            {"slot_data": {"goal_location_name": GOAL_LOCATION_NAME_BOWSER}})
        self.assertEqual(self.ctx.goal_location_name,
                         GOAL_LOCATION_NAME_BOWSER)

    async def test_connected_missing_goal_location_name_leaves_none(self):
        """Pre-M3.7-respect apworlds don't populate goal_location_name;
        the bridge logs a warning and falls back to the legacy
        unconditional-Bowser-fire path in handle_goal_completed."""
        await self.ctx._handle_ap_package("Connected", {"slot_data": {}})
        self.assertIsNone(self.ctx.goal_location_name)

    async def test_connected_non_string_goal_location_name_ignored(self):
        """Defensive: if slot_data has a wrong-typed value, ignore it
        rather than crashing the Connected handler."""
        await self.ctx._handle_ap_package(
            "Connected", {"slot_data": {"goal_location_name": 42}})
        self.assertIsNone(self.ctx.goal_location_name)

    # ---- handle_check_emitted -> CLIENT_GOAL on goal-location -------

    async def test_handle_check_emitted_goal_location_sends_client_goal(
            self):
        """When the player's goal is Wonder Gauntlet and the player
        clears it (NORMAL_EXIT CheckEmitted that resolves to the
        Wonder Gauntlet location), the bridge sends CLIENT_GOAL
        even though that AP location has address=None and thus no
        LocationCheck reaches the server."""
        from NetUtils import ClientStatus  # type: ignore
        from ..protocol import CheckEmitted, CheckKind
        self.ctx.goal_location_name = (
            "Special: Wonder Gauntlet - Normal Exit")
        # Goal locations have address=None so they won't be in the
        # name-to-id map -- intentionally omitted.
        self.ctx._location_name_to_id = {}
        await self.ctx.handle_check_emitted(CheckEmitted(
            kind=CheckKind.NORMAL_EXIT, stage_key=0x4871EB85))
        self.ctx.send_msgs.assert_any_await([{
            "cmd": "StatusUpdate",
            "status": ClientStatus.CLIENT_GOAL,
        }])

    async def test_handle_check_emitted_non_goal_location_no_client_goal(
            self):
        """A LocationCheck for a non-goal location must not signal
        CLIENT_GOAL."""
        from ..protocol import CheckEmitted, CheckKind
        self.ctx.goal_location_name = (
            "Special: Wonder Gauntlet - Normal Exit")
        self.ctx._location_name_to_id = {
            "W1: Welcome to the Flower Kingdom! - Wonder Seed": 12345,
        }
        await self.ctx.handle_check_emitted(CheckEmitted(
            kind=CheckKind.WONDER_SEED, stage_key=2937190396))
        # send_msgs fired exactly once -- the LocationChecks, not a
        # StatusUpdate.
        self.assertEqual(self.ctx.send_msgs.await_count, 1)
        call = self.ctx.send_msgs.await_args_list[0]
        msgs = call.args[0]
        self.assertEqual(msgs[0]["cmd"], "LocationChecks")

    async def test_handle_check_emitted_no_goal_set_no_client_goal(self):
        """Before Connected fires (goal_location_name is None), no
        check should accidentally trigger CLIENT_GOAL."""
        from ..protocol import CheckEmitted, CheckKind
        self.assertIsNone(self.ctx.goal_location_name)
        self.ctx._location_name_to_id = {
            "Special: Wonder Gauntlet - Normal Exit": 99999,
        }
        await self.ctx.handle_check_emitted(CheckEmitted(
            kind=CheckKind.NORMAL_EXIT, stage_key=0x4871EB85))
        for call in self.ctx.send_msgs.await_args_list:
            for msg in call.args[0]:
                self.assertNotEqual(msg["cmd"], "StatusUpdate")

    async def test_goal_dedup_bowser_then_check(self):
        """Bowser kill fires the Nerve hook first; then the
        PALACE_CLEAR check for Bowser's Rage Stage fires from the
        SetCourseClearFlagToGameData path.  CLIENT_GOAL must be sent
        at most once."""
        from NetUtils import ClientStatus  # type: ignore
        from ..context import GOAL_LOCATION_NAME_BOWSER
        from ..protocol import CheckEmitted, CheckKind
        self.ctx.goal_location_name = GOAL_LOCATION_NAME_BOWSER
        self.ctx._location_name_to_id = {}

        await self.ctx.handle_goal_completed(self._GoalCompleted(seq=1))
        await self.ctx.handle_check_emitted(CheckEmitted(
            kind=CheckKind.PALACE_CLEAR, stage_key=0x6895BF00))

        client_goal_msgs = [
            msg
            for call in self.ctx.send_msgs.await_args_list
            for msg in call.args[0]
            if msg.get("cmd") == "StatusUpdate"
            and msg.get("status") == ClientStatus.CLIENT_GOAL
        ]
        self.assertEqual(len(client_goal_msgs), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
