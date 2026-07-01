"""Tests for open-world mode on the client side.

Pins: Connected reads ``open_world_active`` / ``palaces_required`` from
slot_data and pushes the routable-world mask; receiving enough active
Royal Seeds flags the Castle routable exactly once but does NOT win the
seed (the goal is actually beating Bowser, same as vanilla); the
final-Bowser death-gate demands all six AP Royal Seeds AND
``palaces_required`` palaces cleared in the player's own game; the
routable/royal-seed providers return the
right values for the LanServer replay path; and non-open-world seeds stay
a no-op.

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

    def _client_goal_sent(self) -> bool:
        """True iff any send_msgs call carried a StatusUpdate(CLIENT_GOAL)."""
        from NetUtils import ClientStatus  # type: ignore
        for call in self.ctx.send_msgs.call_args_list:
            for msg in call.args[0]:
                if (msg.get("cmd") == "StatusUpdate"
                        and msg.get("status") == ClientStatus.CLIENT_GOAL):
                    return True
        return False

    # ---- Connected ----------------------------------------------------

    async def test_connected_reads_open_world_slot_data(self):
        await self._connect({"open_world_active": [1, 3, 5], "palaces_required": 2})
        self.assertTrue(self.ctx.open_world)
        self.assertEqual(self.ctx.open_world_active, [1, 3, 5])
        self.assertEqual(self.ctx.palaces_required, 2)
        self.assertFalse(self.ctx._bowser_opened)

    async def test_connected_pushes_routable_worlds(self):
        await self._connect({"open_world_active": [1, 3, 5], "palaces_required": 2})
        # Walk-in model: Petal Isles (bit 6 == 0x40) is routable; W2..W6 are
        # reached on foot from PI, so no bits for them.  W1 IS active here and
        # is NOT walk-connected to PI, so it gets its fast-travel bit 0 (0x01).
        # No Castle bit yet.  Expect 0x40 | 0x01 == 0x41.
        self.ctx.lan_server.send_set_routable_worlds.assert_called_once_with(0x41)

    async def test_connected_without_open_world_is_inactive(self):
        await self._connect({})
        self.assertFalse(self.ctx.open_world)
        self.assertEqual(self.ctx.open_world_active, [])
        self.ctx.lan_server.send_set_routable_worlds.assert_not_called()

    # ---- Threshold -> unlock Bowser -----------------------------------

    async def test_threshold_unlocks_bowser_once(self):
        await self._connect({"open_world_active": [1, 3, 5], "palaces_required": 2})
        self.ctx.lan_server.reset_mock()

        # Two active Royal Seeds received -> threshold met.
        self.ctx.items_received = [{"item": self.W1_SEED}, {"item": self.W3_SEED}]
        await self.ctx._handle_received_items(
            {"items": [{"item": self.W1_SEED}, {"item": self.W3_SEED}]})

        self.assertTrue(self.ctx._bowser_opened)
        # Royal Seeds are NEVER pushed to the Switch (vanilla-owned; the AP
        # count gates the final level only via the death-gate).
        self.ctx.lan_server.send_set_royal_seeds_absolute.assert_not_called()
        # Routable mask now includes the Castle bit.  W1 is active in this
        # seed, so its fast-travel bit 0 is also set:
        # PI 0x40 | W1 0x01 | Castle 0x100 == 0x141.
        self.ctx.lan_server.send_set_routable_worlds.assert_called_with(0x141)

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

    # ---- Goal is beating Bowser, not holding seeds --------------------

    async def test_threshold_does_not_signal_goal(self):
        """Meeting the Royal-Seed threshold opens the Castle route but must
        NOT win the seed -- open-world now mirrors vanilla, where the goal is
        actually defeating Bowser."""
        await self._connect({"open_world_active": [1, 3, 5], "palaces_required": 2})
        self.ctx.send_msgs.reset_mock()
        self.ctx.items_received = [{"item": self.W1_SEED}, {"item": self.W3_SEED}]
        await self.ctx._handle_received_items(
            {"items": [{"item": self.W1_SEED}, {"item": self.W3_SEED}]})
        self.assertTrue(self.ctx._bowser_opened)
        self.assertFalse(self.ctx._goal_status_sent)
        self.assertFalse(self._client_goal_sent())

    async def test_bowser_clear_signals_goal(self):
        """Defeating Bowser (GameGoalReached Nerve -> handle_goal_completed)
        is what wins an open-world seed."""
        from ..protocol import GoalCompleted
        await self._connect({
            "open_world_active": [1, 3, 5],
            "palaces_required": 2,
            "goal_location_name": "BC: Bowser's Rage Stage - Royal Seed",
        })
        self.ctx.send_msgs.reset_mock()
        await self.ctx.handle_goal_completed(GoalCompleted(seq=1))
        self.assertTrue(self.ctx._goal_status_sent)
        self.assertTrue(self._client_goal_sent())

    # ---- Final-Bowser death-gate: all six seeds + palaces cleared -----

    async def test_bowser_gate_requires_all_seeds_and_palaces(self):
        """Open-world Bowser gate: must hold ALL six AP Royal Seeds AND have
        cleared palaces_required palaces in the player's own game."""
        from ..protocol import GateEntered, GateKind
        await self._connect({"open_world_active": [1, 3, 5], "palaces_required": 2})
        ev = GateEntered(
            stage_key=0x6895BF00, gate_kind=GateKind.ROYAL_SEEDS,
            requirement=6, world_no=8, course_no=0)
        self.ctx._recompute_royal_seed_mask = MagicMock()
        self.ctx._palaces_cleared_in_game = MagicMock()

        # All six seeds + 2 palaces cleared -> satisfied.
        self.ctx._recompute_royal_seed_mask.return_value = 0b111111
        self.ctx._palaces_cleared_in_game.return_value = 2
        self.assertTrue(self.ctx._gate_requirement_met(ev))

        # All six seeds but only 1 palace cleared in-game -> bounce.
        self.ctx._palaces_cleared_in_game.return_value = 1
        self.assertFalse(self.ctx._gate_requirement_met(ev))

        # Five seeds (missing one) even with palaces cleared -> bounce.
        self.ctx._recompute_royal_seed_mask.return_value = 0b011111
        self.ctx._palaces_cleared_in_game.return_value = 2
        self.assertFalse(self.ctx._gate_requirement_met(ev))

        # The kill message names both conditions.
        cause = self.ctx._gate_kill_cause(ev)
        self.assertIn("6 AP Royal Seeds", cause)
        self.assertIn("2 palaces", cause)

    async def test_palaces_cleared_counts_checked_locations(self):
        """_palaces_cleared_in_game counts checked palace-clear AP locations
        (persistent server state), keyed off the Royal-Seed table's palace
        stage_keys -- the final Bowser stage is not among them."""
        from .. import royal_seed_table
        from ..location_table import lookup_name
        from ..protocol import CheckEmitted, CheckKind
        await self._connect({"open_world_active": [1, 3, 5], "palaces_required": 2})
        names = [
            lookup_name(CheckEmitted(kind=CheckKind.PALACE_CLEAR, stage_key=sk))
            for sk in royal_seed_table.PALACE_STAGE_KEYS
        ]
        self.assertTrue(all(names), "all six palace locations must resolve")
        self.ctx._location_name_to_id = {n: 1000 + i for i, n in enumerate(names)}

        self.ctx.checked_locations = set()
        self.assertEqual(self.ctx._palaces_cleared_in_game(), 0)
        # Two distinct palace locations checked -> 2 (an unrelated id ignored).
        self.ctx.checked_locations = {1000, 1002, 55555}
        self.assertEqual(self.ctx._palaces_cleared_in_game(), 2)

    # ---- Providers (LanServer replay path) ----------------------------

    async def test_routable_mask_provider(self):
        await self._connect({"open_world_active": [2, 4], "palaces_required": 0})
        # Walk-in model: only Petal Isles (bit 6 == 0x40) is routable for
        # W2..W6 (reached on foot from PI).  W1 is NOT active here, so its
        # fast-travel bit 0 must stay clear.
        self.assertEqual(self.ctx._recompute_routable_worlds_mask(), 0x40)
        self.ctx._bowser_opened = True
        self.assertEqual(self.ctx._recompute_routable_worlds_mask(), 0x40 | 0x100)

    async def test_routable_mask_w1_active_sets_fasttravel_bit(self):
        # W1 is not walk-connected to Petal Isles, so when it is active it
        # must be made fast-travelable via its routable bit 0 (0x01); the
        # Switch first-course fill + travel hooks then land the player on 1-1.
        await self._connect({"open_world_active": [1, 4], "palaces_required": 0})
        self.assertEqual(self.ctx._recompute_routable_worlds_mask(), 0x40 | 0x01)
        self.ctx._bowser_opened = True
        self.assertEqual(
            self.ctx._recompute_routable_worlds_mask(), 0x40 | 0x01 | 0x100)

    async def test_routable_mask_w1_inactive_no_fasttravel_bit(self):
        # W1 absent from the active set -> bit 0 must NOT be set (W1 is never
        # travelable when it is not an active world).
        await self._connect({"open_world_active": [3, 5, 6], "palaces_required": 0})
        mask = self.ctx._recompute_routable_worlds_mask()
        self.assertEqual(mask & 0x01, 0)
        self.assertEqual(mask, 0x40)

    async def test_routable_mask_provider_inactive_returns_zero(self):
        await self._connect({})
        self.assertEqual(self.ctx._recompute_routable_worlds_mask(), 0)

    # ---- force-cleared (secret-exit replay) courses ----------------------

    async def test_force_cleared_standard_mode_also_forces(self):
        # Applies in BOTH modes (2026-07-01): a non-open-world seed still
        # forces the no-NORMAL_EXIT courses (safe-if-unnecessary; guards
        # against AP-authoritative overwrites clobbering the native flag).
        from ..force_cleared_table import FORCE_CLEARED_COURSES
        await self._connect({})
        self.assertFalse(self.ctx.open_world)
        self.ctx.checked_locations = set()
        expected = (1 << len(FORCE_CLEARED_COURSES)) - 1
        self.assertEqual(self.ctx._recompute_force_cleared_mask(), expected)

    async def test_force_cleared_open_world_no_normal_exit_always_set(self):
        # Both current courses (Operation Poplin Rescue, Royal Seed Mansion)
        # have no NORMAL_EXIT location, so both are forced regardless of
        # checked_locations.
        from ..force_cleared_table import FORCE_CLEARED_COURSES
        await self._connect(
            {"open_world_active": [6], "palaces_required": 0})
        self.ctx.checked_locations = set()
        expected = (1 << len(FORCE_CLEARED_COURSES)) - 1
        self.assertEqual(self.ctx._recompute_force_cleared_mask(), expected)

    async def test_force_cleared_normal_exit_gated_on_checked(self):
        # A hypothetical course WITH a NORMAL_EXIT location is only forced
        # once that location is checked.  Patch the table to exercise the
        # gated branch without depending on real data.
        from .. import force_cleared_table
        await self._connect(
            {"open_world_active": [6], "palaces_required": 0})
        orig = force_cleared_table.FORCE_CLEARED_COURSES
        force_cleared_table.FORCE_CLEARED_COURSES = [
            ("Fake - Secret Exit", "Fake - Normal Exit"),
        ]
        try:
            self.ctx._location_name_to_id = {"Fake - Normal Exit": 4242}
            self.ctx.checked_locations = set()
            self.assertEqual(self.ctx._recompute_force_cleared_mask(), 0)
            self.ctx.checked_locations = {4242}
            self.assertEqual(self.ctx._recompute_force_cleared_mask(), 0b1)
        finally:
            force_cleared_table.FORCE_CLEARED_COURSES = orig

    async def test_royal_seed_provider_never_pushes(self):
        # Royal Seeds are NEVER pushed to the Switch: the in-game seed state
        # stays vanilla and the AP-granted count gates the final level only
        # via the death-gate.  The provider therefore always returns None --
        # even after the Bowser-unlock threshold latches.
        await self._connect({"open_world_active": [1, 2], "palaces_required": 1})
        self.assertIsNone(self.ctx._open_world_royal_seed_mask())
        self.ctx._bowser_opened = True
        self.assertIsNone(self.ctx._open_world_royal_seed_mask())

    async def test_royal_seed_provider_none_outside_open_world(self):
        await self._connect({})
        self.ctx._bowser_opened = True  # shouldn't happen, but be defensive
        self.assertIsNone(self.ctx._open_world_royal_seed_mask())

    # ---- World-unlock hash split (Int vs Bool writers) -----------------

    async def test_connected_pushes_world_unlock_split(self):
        from .. import world_unlock_table as wut
        await self._connect({"open_world_active": [1, 3, 5], "palaces_required": 2})
        self.ctx.lan_server.send_apply_world_unlock.assert_called_once_with(
            wut.WORLD_UNLOCK_INT_HASHES, wut.WORLD_UNLOCK_BOOL_HASHES)

    async def test_world_unlock_provider_returns_split_pair(self):
        from .. import world_unlock_table as wut
        await self._connect({"open_world_active": [1], "palaces_required": 0})
        self.assertEqual(
            self.ctx._world_unlock_hashes(),
            (wut.WORLD_UNLOCK_INT_HASHES, wut.WORLD_UNLOCK_BOOL_HASHES))

    async def test_world_unlock_provider_empty_outside_open_world(self):
        await self._connect({})
        self.assertEqual(self.ctx._world_unlock_hashes(), ((), ()))


if __name__ == "__main__":
    unittest.main()
