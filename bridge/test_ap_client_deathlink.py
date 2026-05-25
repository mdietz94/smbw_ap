"""Tests for the M3.8 DeathLink wiring in :mod:`bridge.ap_client`.

Exercises the four behaviors:
  1. ``on_deathlink`` forwards to ``lan_server.send_kill`` when enabled.
  2. ``on_deathlink`` drops self-sourced bounces (belt-and-braces self-ping
     guard over CommonClient's timestamp-based gate).
  3. ``handle_death_reported`` calls ``send_death("mario_died")`` only when
     deathlink is enabled.
  4. ``_handle_ap_package("Connected", ...)`` reads ``slot_data.death_link``
     and toggles the connection tag via ``update_death_link``.

CommonContext requires Archipelago on ``sys.path``; this module replays the
same lookup :mod:`bridge.__main__` does so the suite can run with no extra
PYTHONPATH setup.  If neither vendor checkout is present the whole module
is skipped (importing ap_client would crash on ``from CommonClient
import``).
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


def _ensure_archipelago_on_path() -> bool:
    """Locate a usable Archipelago checkout and add it to sys.path.
    Returns True on success, False if none could be found.

    Walks upward from this file looking for ``vendor/Archipelago`` at
    each level (so the test still finds the main-repo vendor when we run
    inside a ``.claude/worktrees/<…>`` worktree, which has no ``vendor/``
    of its own).  Also tries the sibling ``smo_archipelago`` checkout as
    a legacy fallback for devs who already had it."""
    here = Path(__file__).resolve()
    seen: list[Path] = []
    # Walk upward 6 levels max -- worktree depth is 4 (.claude/worktrees/
    # <slug>/bridge/file) so 6 gives 2 levels of headroom.
    p = here.parent
    for _ in range(6):
        seen.append(p / "vendor" / "Archipelago")
        seen.append(p.parent / "smo_archipelago" / "vendor" / "Archipelago")
        p = p.parent
    for c in seen:
        if (c / "CommonClient.py").is_file():
            sp = str(c)
            if sp not in sys.path:
                sys.path.insert(0, sp)
            return True
    return False


_ARCHIPELAGO_AVAILABLE = _ensure_archipelago_on_path()


@unittest.skipUnless(
    _ARCHIPELAGO_AVAILABLE,
    "Archipelago checkout not found (neither bridge/../vendor nor "
    "smo_archipelago/vendor); skipping ap_client DeathLink tests.")
class TestApClientDeathLink(unittest.IsolatedAsyncioTestCase):
    """Behavioral tests against a real SMBWContext with the network bits
    mocked.  CommonContext doesn't need a live server to construct."""

    async def asyncSetUp(self) -> None:  # type: ignore[override]
        # CommonContext.__init__ spawns a keep_alive Task during ctor;
        # that requires a running event loop, hence asyncSetUp rather
        # than setUp.  Local imports so the @skipUnless on the class
        # short-circuits before we try to construct anything when the
        # AP framework isn't reachable.
        if __package__ in (None, ""):
            from ap_client import SMBWContext  # type: ignore
            from protocol import DeathReported  # type: ignore
            from state import BridgeState  # type: ignore
        else:
            from .ap_client import SMBWContext
            from .protocol import DeathReported
            from .state import BridgeState
        self._SMBWContext = SMBWContext
        self._DeathReported = DeathReported

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
