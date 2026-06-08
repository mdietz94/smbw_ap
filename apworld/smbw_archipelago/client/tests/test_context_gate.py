"""Tests for the level-entry gate in :mod:`...client.context`.

The Switch can't physically block entry to a course AP logic gates (the
pre-commit gate is a documented dead end -- see
``docs/gate-entry-session3-handoff.md``), so the bridge detects entry
via ``course_in`` and, if the player lacks the required AP item, bounces
them out via the DeathLink ``synthKill`` route after a grace window,
re-arming while they stay inside.

These tests exercise ``SMBWContext.handle_gate_entered`` /
``_gate_requirement_met`` / ``_gate_kill_loop`` with the item-mask
recompute methods stubbed (the item-table plumbing is covered by the
badge/royal-seed table tests) and ``GATE_KILL_DELAY_S`` patched tiny so
the loop runs fast.

Archipelago is loaded onto sys.path by the repo-root conftest.py; if
it's missing the imports fail at collection, so the class is gated with
@skipUnless like the other context tests.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def _try_import_archipelago() -> bool:
    try:
        import CommonClient  # noqa: F401
        return True
    except Exception:
        return False


_ARCHIPELAGO_AVAILABLE = _try_import_archipelago()


@unittest.skipUnless(
    _ARCHIPELAGO_AVAILABLE,
    "Archipelago not importable; skipping context level-entry-gate tests.")
class TestContextLevelEntryGate(unittest.IsolatedAsyncioTestCase):

    BADGE_STAGE = 0xDADED63E   # W1 Wall-Climb Jump I -> badge 34
    BADGE_ID = 34
    BOWSER_STAGE = 0x6895BF00  # BC: Bowser's Rage Stage

    async def asyncSetUp(self) -> None:  # type: ignore[override]
        from .. import context as context_mod
        from ..context import SMBWContext
        from ..protocol import GateEntered, GateKind
        from ..state import BridgeState, CurrentCourse
        self._context_mod = context_mod
        self._GateEntered = GateEntered
        self._GateKind = GateKind
        self._CurrentCourse = CurrentCourse

        import CommonClient  # type: ignore
        CommonClient.network_data_package["games"].setdefault(
            "Super Mario Bros Wonder",
            {"checksum": "", "item_name_to_id": {}, "location_name_to_id": {}})

        self.state = BridgeState()
        self.ctx = SMBWContext(
            server_address=None, password=None, state=self.state)
        self.ctx.auth = "MarioSlot"
        self.ctx.slot = 1  # "connected"
        self.ctx.player_names = {1: "MarioSlot"}

        # Network-talking AP methods the Connected handler would invoke.
        self.ctx.send_msgs = AsyncMock()             # type: ignore[assignment]
        self.ctx.update_death_link = AsyncMock()     # type: ignore[assignment]

        # Stub the item-mask recompute methods so the gate logic is
        # tested in isolation from the item-table lookup plumbing.
        self.ctx._recompute_badge_mask = MagicMock(return_value=0)
        self.ctx._recompute_royal_seed_mask = MagicMock(return_value=0)

        self.ctx.lan_server = MagicMock()

    async def asyncTearDown(self) -> None:  # type: ignore[override]
        try:
            await self.ctx.shutdown()
        except Exception:
            pass

    # ---- helpers ----------------------------------------------------

    def _enter(self, stage_key: int) -> None:
        """Mirror what the processor does on course_in: mark in-course
        with the given stage as current_course."""
        self.state.mark_course_entered(
            self._CurrentCourse(stage_key=stage_key, world_no=1, course_no=5))

    def _badge_gate(self, stage_key: int | None = None):
        return self._GateEntered(
            stage_key=stage_key if stage_key is not None else self.BADGE_STAGE,
            gate_kind=self._GateKind.BADGE,
            requirement=self.BADGE_ID)

    def _bowser_gate(self):
        return self._GateEntered(
            stage_key=self.BOWSER_STAGE,
            gate_kind=self._GateKind.ROYAL_SEEDS,
            requirement=6)

    # ---- _gate_requirement_met (pure) -------------------------------

    def test_requirement_met_badge_owned(self):
        self.ctx._recompute_badge_mask.return_value = (1 << self.BADGE_ID)
        self.assertTrue(self.ctx._gate_requirement_met(self._badge_gate()))

    def test_requirement_unmet_badge_missing(self):
        self.ctx._recompute_badge_mask.return_value = (1 << 7)  # some other bit
        self.assertFalse(self.ctx._gate_requirement_met(self._badge_gate()))

    def test_requirement_met_six_royal_seeds(self):
        self.ctx._recompute_royal_seed_mask.return_value = 0b111111
        self.assertTrue(self.ctx._gate_requirement_met(self._bowser_gate()))

    def test_requirement_unmet_five_royal_seeds(self):
        self.ctx._recompute_royal_seed_mask.return_value = 0b011111
        self.assertFalse(self.ctx._gate_requirement_met(self._bowser_gate()))

    def test_unknown_gate_kind_fails_open(self):
        bad = MagicMock()
        bad.gate_kind = "future_kind"
        bad.stage_key = 0x1234
        bad.requirement = 1
        self.assertTrue(self.ctx._gate_requirement_met(bad))

    # ---- handle_gate_entered: arming decisions ----------------------

    async def test_satisfied_requirement_does_not_arm(self):
        self.ctx._recompute_badge_mask.return_value = (1 << self.BADGE_ID)
        self._enter(self.BADGE_STAGE)
        await self.ctx.handle_gate_entered(self._badge_gate())
        self.assertIsNone(self.ctx._gate_kill_task)
        self.ctx.lan_server.send_kill.assert_not_called()

    async def test_not_connected_still_arms_anticheat(self):
        # Anti-cheat: a player who isn't connected to AP must still be
        # gated -- otherwise they could sequence-break offline.  With no
        # recorded item, the requirement is unmet, so the loop arms.
        self.ctx.slot = None  # not connected
        self._enter(self.BADGE_STAGE)
        with patch.object(self._context_mod, "GATE_KILL_DELAY_S", 0.01):
            await self.ctx.handle_gate_entered(self._badge_gate())
            self.assertIsNotNone(self.ctx._gate_kill_task)
            await asyncio.sleep(0.05)
            self.assertGreaterEqual(
                self.ctx.lan_server.send_kill.call_count, 1)
            self.state.mark_course_exited()

    async def test_gating_disabled_does_not_arm(self):
        self.ctx.entry_gating_enabled = False
        self._enter(self.BADGE_STAGE)
        await self.ctx.handle_gate_entered(self._badge_gate())
        self.assertIsNone(self.ctx._gate_kill_task)
        self.ctx.lan_server.send_kill.assert_not_called()

    async def test_unmet_requirement_arms_task(self):
        self._enter(self.BADGE_STAGE)
        with patch.object(self._context_mod, "GATE_KILL_DELAY_S", 100.0):
            # Huge delay -> task is created but no kill fires during the test.
            await self.ctx.handle_gate_entered(self._badge_gate())
            self.assertIsNotNone(self.ctx._gate_kill_task)
            self.ctx.lan_server.send_kill.assert_not_called()
        self.ctx._cancel_gate_kill()

    # ---- _gate_kill_loop: kill + re-arm + stop conditions -----------

    async def test_kill_fires_and_rearms_then_stops_on_exit(self):
        self._enter(self.BADGE_STAGE)
        with patch.object(self._context_mod, "GATE_KILL_DELAY_S", 0.01):
            await self.ctx.handle_gate_entered(self._badge_gate())
            await asyncio.sleep(0.08)  # several delay windows
            # Re-armed: more than one kill while still inside w/o badge.
            self.assertGreaterEqual(
                self.ctx.lan_server.send_kill.call_count, 2)
            # The kill uses the DeathLink route shape.
            _, kwargs = self.ctx.lan_server.send_kill.call_args
            self.assertEqual(kwargs["source"], "SMBW Gate")
            self.assertIn("badge", kwargs["cause"].lower())

            # Player leaves -> loop stops re-arming.
            self.state.mark_course_exited()
            await asyncio.sleep(0.05)
            settled = self.ctx.lan_server.send_kill.call_count
            await asyncio.sleep(0.05)
            self.assertEqual(
                self.ctx.lan_server.send_kill.call_count, settled)
        self.assertIsNone(self.ctx._gate_kill_task)

    async def test_loop_stops_when_item_acquired_midway(self):
        self._enter(self.BADGE_STAGE)
        with patch.object(self._context_mod, "GATE_KILL_DELAY_S", 0.01):
            await self.ctx.handle_gate_entered(self._badge_gate())
            await asyncio.sleep(0.05)
            self.assertGreaterEqual(
                self.ctx.lan_server.send_kill.call_count, 1)
            # AP grants the badge -> requirement now met -> loop stops.
            self.ctx._recompute_badge_mask.return_value = (1 << self.BADGE_ID)
            await asyncio.sleep(0.05)
            settled = self.ctx.lan_server.send_kill.call_count
            await asyncio.sleep(0.05)
            self.assertEqual(
                self.ctx.lan_server.send_kill.call_count, settled)

    async def test_loop_stops_when_moved_to_other_course(self):
        self._enter(self.BADGE_STAGE)
        with patch.object(self._context_mod, "GATE_KILL_DELAY_S", 0.01):
            await self.ctx.handle_gate_entered(self._badge_gate())
            await asyncio.sleep(0.05)
            self.assertGreaterEqual(
                self.ctx.lan_server.send_kill.call_count, 1)
            # Player is now in a different course (still in_course, but a
            # different stage_key) -> the stale loop stops.
            self.state.mark_course_entered(
                self._CurrentCourse(stage_key=0x11112222))
            await asyncio.sleep(0.05)
            settled = self.ctx.lan_server.send_kill.call_count
            await asyncio.sleep(0.05)
            self.assertEqual(
                self.ctx.lan_server.send_kill.call_count, settled)

    async def test_new_gate_entry_supersedes_old_task(self):
        self._enter(self.BADGE_STAGE)
        with patch.object(self._context_mod, "GATE_KILL_DELAY_S", 100.0):
            await self.ctx.handle_gate_entered(self._badge_gate())
            first = self.ctx._gate_kill_task
            self.assertIsNotNone(first)
            # A second gated entry replaces the first task.
            await self.ctx.handle_gate_entered(
                self._badge_gate(stage_key=0x33334444))
            second = self.ctx._gate_kill_task
            self.assertIsNotNone(second)
            self.assertIsNot(first, second)
            # cancel() is async -- let the loop process it.
            await asyncio.sleep(0.01)
            self.assertTrue(first.cancelled() or first.done())
        self.ctx._cancel_gate_kill()

    async def test_bowser_gate_kills_without_six_seeds(self):
        self.ctx._recompute_royal_seed_mask.return_value = 0b011111  # only 5
        self._enter(self.BOWSER_STAGE)
        with patch.object(self._context_mod, "GATE_KILL_DELAY_S", 0.01):
            await self.ctx.handle_gate_entered(self._bowser_gate())
            await asyncio.sleep(0.05)
            self.assertGreaterEqual(
                self.ctx.lan_server.send_kill.call_count, 1)
            _, kwargs = self.ctx.lan_server.send_kill.call_args
            self.assertIn("royal seed", kwargs["cause"].lower())
            self.state.mark_course_exited()

    # ---- Open-world: only gate badge courses that are in logic -------

    def _badge_gate_world(self, world_no: int, stage_key: int | None = None):
        return self._GateEntered(
            stage_key=stage_key if stage_key is not None else self.BADGE_STAGE,
            gate_kind=self._GateKind.BADGE,
            requirement=self.BADGE_ID,
            world_no=world_no,
        )

    def test_in_logic_true_outside_open_world(self):
        # Standard mode: every world is in logic regardless of world_no.
        self.assertFalse(self.ctx.open_world)
        self.assertTrue(
            self.ctx._gate_course_in_logic(self._badge_gate_world(4)))

    def test_in_logic_active_numbered_world(self):
        self.ctx.open_world = True
        self.ctx.open_world_active = [3]  # AP world 3 == PlayReport world_no 4
        self.assertTrue(
            self.ctx._gate_course_in_logic(self._badge_gate_world(4)))

    def test_in_logic_inactive_numbered_world(self):
        self.ctx.open_world = True
        self.ctx.open_world_active = [1, 2]
        self.assertFalse(
            self.ctx._gate_course_in_logic(self._badge_gate_world(4)))  # AP 3

    def test_in_logic_petal_isles_out_of_logic(self):
        # PlayReport world_no=2 is the Petal Isles hub -- never a numbered
        # active world, so its badge courses are out of logic.
        self.ctx.open_world = True
        self.ctx.open_world_active = [1, 2, 3]
        self.assertFalse(
            self.ctx._gate_course_in_logic(self._badge_gate_world(2)))

    def test_in_logic_royal_seeds_gate_always_in_logic(self):
        # The final-Bowser ROYAL_SEEDS gate is never world-filtered.
        self.ctx.open_world = True
        self.ctx.open_world_active = [1]
        self.assertTrue(self.ctx._gate_course_in_logic(self._bowser_gate()))

    async def test_open_world_inactive_world_badge_not_gated(self):
        self.ctx.open_world = True
        self.ctx.open_world_active = [1, 2]  # AP world 3 (world_no 4) inactive
        self._enter(self.BADGE_STAGE)
        with patch.object(self._context_mod, "GATE_KILL_DELAY_S", 0.01):
            await self.ctx.handle_gate_entered(self._badge_gate_world(4))
            self.assertIsNone(self.ctx._gate_kill_task)
            await asyncio.sleep(0.05)
            self.ctx.lan_server.send_kill.assert_not_called()

    async def test_open_world_active_world_badge_still_gated(self):
        self.ctx.open_world = True
        self.ctx.open_world_active = [3]  # AP world 3 (world_no 4) active
        self._enter(self.BADGE_STAGE)
        with patch.object(self._context_mod, "GATE_KILL_DELAY_S", 0.01):
            await self.ctx.handle_gate_entered(self._badge_gate_world(4))
            self.assertIsNotNone(self.ctx._gate_kill_task)
            await asyncio.sleep(0.05)
            self.assertGreaterEqual(
                self.ctx.lan_server.send_kill.call_count, 1)
            self.state.mark_course_exited()

    async def test_open_world_bowser_gate_unaffected_by_world_filter(self):
        # Even with a tiny active-world set, the Bowser gate still fires
        # when the player lacks the Royal Seeds.
        self.ctx.open_world = True
        self.ctx.open_world_active = [1]
        self.ctx._recompute_royal_seed_mask.return_value = 0b011111  # only 5
        self._enter(self.BOWSER_STAGE)
        with patch.object(self._context_mod, "GATE_KILL_DELAY_S", 0.01):
            await self.ctx.handle_gate_entered(self._bowser_gate())
            await asyncio.sleep(0.05)
            self.assertGreaterEqual(
                self.ctx.lan_server.send_kill.call_count, 1)
            self.state.mark_course_exited()

    # ---- Connected slot_data toggle ---------------------------------

    async def test_connected_slot_data_disables_gating(self):
        self.assertTrue(self.ctx.entry_gating_enabled)
        await self.ctx._handle_ap_package(
            "Connected", {"slot_data": {"level_entry_gating": False}})
        self.assertFalse(self.ctx.entry_gating_enabled)

    async def test_connected_missing_key_defaults_on(self):
        self.ctx.entry_gating_enabled = False
        await self.ctx._handle_ap_package("Connected", {"slot_data": {}})
        self.assertTrue(self.ctx.entry_gating_enabled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
