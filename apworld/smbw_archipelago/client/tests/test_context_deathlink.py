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
        StatusUpdate with ClientStatus.CLIENT_GOAL (= 30)."""
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
