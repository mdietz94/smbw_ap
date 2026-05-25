"""Tests for the bridge event processor.

Replays event sequences (PlayReport bytes + nerve fires) through the
processor and asserts on the resulting CheckEmitted list / state.

All PlayReport fixtures live in test_play_report.py; we import them
here so the corpus is shared between the format-level tests and the
behavioral tests.
"""

from __future__ import annotations

import unittest

from ..processor import _emit_ten_coin_checks, process_event
from ..protocol import (
    CheckEmitted,
    CheckKind,
    DeathReported,
    GoalCompleted,
    NerveFireMsg,
    NerveKind,
    PlayReportMsg,
)
from ..state import BridgeState, CurrentCourse
from .test_play_report import (
    COURSE_RESULT,
    KOOPAJR_RESULT_LOSS,
    KOOPAJR_RESULT_WIN,
    PALACE_COURSE_RESULT,
    W1_2_COURSE_IN,
    W1_2_COURSE_RESULT_SECRET,
    WORLD_ACTIVITY,
    WORLD_RESULT,
    WORLD_RESULT_W1_TO_W2,
)


# Real stage_keys from the corpus, used in assertions.
W1_1_STAGE_KEY = 2937190396      # Welcome to the Flower Kingdom
W1_2_STAGE_KEY = 232160011       # Piranha Plants on Parade
PIPEROCK_PALACE_STAGE_KEY = 2308078743


# ---------------------------------------------------------------------------
# Course correlation (M2.6 core).

class TestCourseInUpdatesCurrentCourse(unittest.TestCase):
    """course_in sets current_course; subsequent events attribute to it."""

    def test_course_in_sets_current_course(self):
        state = BridgeState()
        self.assertIsNone(state.current_course)

        emitted = process_event(
            state, PlayReportMsg(room="course_in", payload=W1_2_COURSE_IN))

        self.assertEqual(emitted, [])  # course_in itself doesn't fire an AP check
        self.assertIsNotNone(state.current_course)
        self.assertEqual(state.current_course.stage_key, W1_2_STAGE_KEY)
        self.assertEqual(state.current_course.world_no, 1)
        self.assertEqual(state.current_course.course_no, 3)

    def test_course_in_overrides_previous_course(self):
        state = BridgeState()
        state.set_current_course(CurrentCourse(stage_key=99999, world_no=9, course_no=9))

        process_event(state, PlayReportMsg(room="course_in", payload=W1_2_COURSE_IN))
        self.assertEqual(state.current_course.stage_key, W1_2_STAGE_KEY)


class TestWonderSeedAttribution(unittest.TestCase):
    """M2.6 attribution: WONDER_SEED_AWARDED is attributed to current_course."""

    def test_wonder_seed_after_course_in_attributes_correctly(self):
        state = BridgeState()
        process_event(state, PlayReportMsg(room="course_in", payload=W1_2_COURSE_IN))

        emitted = process_event(state, NerveFireMsg(
            kind=NerveKind.WONDER_SEED_AWARDED, seq=1))

        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].kind, CheckKind.WONDER_SEED)
        self.assertEqual(emitted[0].stage_key, W1_2_STAGE_KEY)
        self.assertEqual(emitted[0].metadata["world_no"], 1)
        self.assertEqual(emitted[0].metadata["course_no"], 3)

    def test_wonder_seed_with_no_current_course_is_dropped(self):
        """A WONDER_SEED_AWARDED fire outside any course should be dropped
        rather than misattributed.  (Probably can't happen in normal
        gameplay, but the bridge mustn't crash or emit bogus checks.)"""
        import logging
        state = BridgeState()
        # The processor logs a warning on this path; silence it for the
        # test so the suite output stays clean.
        with self.assertLogs("SMBW", level="WARNING"):
            emitted = process_event(state, NerveFireMsg(
                kind=NerveKind.WONDER_SEED_AWARDED, seq=1))
        self.assertEqual(emitted, [])
        self.assertEqual(state.count_emitted(), 0)

    def test_duplicate_wonder_seed_in_same_course_dedups(self):
        """Replaying the same wonder seed (e.g. across a reconnect) only
        fires the AP check once."""
        state = BridgeState()
        process_event(state, PlayReportMsg(room="course_in", payload=W1_2_COURSE_IN))

        first = process_event(state, NerveFireMsg(
            kind=NerveKind.WONDER_SEED_AWARDED, seq=1))
        second = process_event(state, NerveFireMsg(
            kind=NerveKind.WONDER_SEED_AWARDED, seq=2))

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])  # deduped
        self.assertEqual(state.count_emitted(CheckKind.WONDER_SEED), 1)


# ---------------------------------------------------------------------------
# M2.5 classification — course_result routing.

class TestCourseResultClassification(unittest.TestCase):

    def test_w1_1_top_of_flag(self):
        state = BridgeState()
        emitted = process_event(
            state, PlayReportMsg(room="course_result", payload=COURSE_RESULT))
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].kind, CheckKind.TOP_OF_FLAG)
        self.assertEqual(emitted[0].stage_key, W1_1_STAGE_KEY)
        self.assertEqual(emitted[0].metadata["goal_id"], 0)
        self.assertTrue(emitted[0].metadata["touch_goal_top"])
        self.assertTrue(emitted[0].metadata["got_finish_seed"])

    def test_w1_2_secret_exit(self):
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="course_result", payload=W1_2_COURSE_RESULT_SECRET))
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].kind, CheckKind.SECRET_EXIT)
        self.assertEqual(emitted[0].stage_key, W1_2_STAGE_KEY)
        self.assertEqual(emitted[0].metadata["goal_id"], 1)

    def test_duplicate_course_result_dedups(self):
        state = BridgeState()
        first = process_event(
            state, PlayReportMsg(room="course_result", payload=COURSE_RESULT))
        second = process_event(
            state, PlayReportMsg(room="course_result", payload=COURSE_RESULT))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_palace_companion_course_result_suppressed(self):
        """The course_result emitted alongside a palace WIN's
        koopajr_result has world_mother_seed=True and must NOT fire as
        a Normal Exit (its goal_id=0, touch_goal_top=False would naively
        route there)."""
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="course_result", payload=PALACE_COURSE_RESULT))
        self.assertEqual(emitted, [])
        self.assertEqual(state.count_emitted(), 0)


# ---------------------------------------------------------------------------
# M2.5 palace classification — koopajr_result routing.

class TestKoopajrResultClassification(unittest.TestCase):

    def test_palace_win_fires_royal_seed(self):
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="koopajr_result", payload=KOOPAJR_RESULT_WIN))
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].kind, CheckKind.PALACE_CLEAR)
        self.assertEqual(emitted[0].stage_key, PIPEROCK_PALACE_STAGE_KEY)
        self.assertEqual(emitted[0].metadata["challenge_count"], 2)

    def test_palace_loss_fires_nothing(self):
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="koopajr_result", payload=KOOPAJR_RESULT_LOSS))
        self.assertEqual(emitted, [])
        self.assertEqual(state.count_emitted(), 0)


# ---------------------------------------------------------------------------
# Full-flow integration: replay a realistic event stream end-to-end.

class TestRealisticPlaythroughFlows(unittest.TestCase):
    """Replays the actual event sequences the user captured during play.

    Each test drives the processor with the same ordered events the
    Switch mod will (eventually) emit over the wire, and asserts on the
    final BridgeState shape."""

    def test_w1_1_top_of_flag_with_wonder_seed_grab(self):
        """Player enters W1-1, grabs the Wonder Phase seed mid-course,
        clears via Top of Flag.  Two AP checks expected."""
        state = BridgeState()
        # We don't have a captured course_in for W1-1, but we can
        # synthesize by setting state directly — the same code path
        # the real course_in would take.
        state.set_current_course(CurrentCourse(
            stage_key=W1_1_STAGE_KEY, world_no=1, course_no=2))

        # 1. Mid-course Wonder Phase seed grab.
        process_event(state, NerveFireMsg(
            kind=NerveKind.WONDER_SEED_AWARDED, seq=1))

        # 2. Flagpole touch precursor (no check yet).
        process_event(state, NerveFireMsg(
            kind=NerveKind.COURSE_CLEARED, seq=1))

        # 3. course_result PlayReport arrives ~8 ms later.
        process_event(state, PlayReportMsg(
            room="course_result", payload=COURSE_RESULT))

        # Expected outcome: WONDER_SEED + TOP_OF_FLAG, both attributed
        # to W1-1.
        self.assertTrue(state.has_emitted(CheckKind.WONDER_SEED, W1_1_STAGE_KEY))
        self.assertTrue(state.has_emitted(CheckKind.TOP_OF_FLAG, W1_1_STAGE_KEY))
        self.assertFalse(state.has_emitted(CheckKind.NORMAL_EXIT, W1_1_STAGE_KEY))
        self.assertEqual(state.count_emitted(), 2)

    def test_w1_2_secret_exit_with_wonder_seed(self):
        """Player enters W1-2, grabs Wonder Phase seed, takes the
        secret exit."""
        state = BridgeState()

        # 1. course_in
        process_event(state, PlayReportMsg(
            room="course_in", payload=W1_2_COURSE_IN))

        # 2. Wonder Phase seed
        process_event(state, NerveFireMsg(
            kind=NerveKind.WONDER_SEED_AWARDED, seq=1))

        # 3. Course clear via secret exit
        process_event(state, NerveFireMsg(
            kind=NerveKind.COURSE_CLEARED, seq=1))
        process_event(state, PlayReportMsg(
            room="course_result", payload=W1_2_COURSE_RESULT_SECRET))

        self.assertTrue(state.has_emitted(CheckKind.WONDER_SEED, W1_2_STAGE_KEY))
        self.assertTrue(state.has_emitted(CheckKind.SECRET_EXIT, W1_2_STAGE_KEY))
        self.assertFalse(state.has_emitted(CheckKind.TOP_OF_FLAG, W1_2_STAGE_KEY))
        self.assertEqual(state.count_emitted(), 2)

    def test_palace_win_dual_event_fires_only_palace_clear(self):
        """Palace WIN emits BOTH course_result AND koopajr_result.  The
        bridge must collapse this to a single PALACE_CLEAR AP check —
        the companion course_result is suppressed by world_mother_seed.
        Order: course_result first (it fires ~1 ms before koopajr_result
        per the live capture)."""
        state = BridgeState()
        state.set_current_course(CurrentCourse(
            stage_key=PIPEROCK_PALACE_STAGE_KEY,
            world_no=1, course_no=30))

        # In-palace wonder seed grab
        process_event(state, NerveFireMsg(
            kind=NerveKind.WONDER_SEED_AWARDED, seq=13))

        # COURSE_CLEARED nerve (boss defeated path)
        process_event(state, NerveFireMsg(
            kind=NerveKind.COURSE_CLEARED, seq=1))

        # Companion course_result (must be suppressed)
        process_event(state, PlayReportMsg(
            room="course_result", payload=PALACE_COURSE_RESULT))

        # Real palace WIN signal
        process_event(state, PlayReportMsg(
            room="koopajr_result", payload=KOOPAJR_RESULT_WIN))

        # WONDER_SEED + PALACE_CLEAR, no NORMAL_EXIT contamination.
        self.assertTrue(state.has_emitted(
            CheckKind.WONDER_SEED, PIPEROCK_PALACE_STAGE_KEY))
        self.assertTrue(state.has_emitted(
            CheckKind.PALACE_CLEAR, PIPEROCK_PALACE_STAGE_KEY))
        self.assertFalse(state.has_emitted(
            CheckKind.NORMAL_EXIT, PIPEROCK_PALACE_STAGE_KEY))
        self.assertEqual(state.count_emitted(), 2)

    def test_palace_loss_then_win_only_fires_on_win(self):
        """Player dies in palace, retries, wins.  Only the win counts."""
        state = BridgeState()
        state.set_current_course(CurrentCourse(
            stage_key=PIPEROCK_PALACE_STAGE_KEY,
            world_no=1, course_no=30))

        # First attempt — death.
        process_event(state, PlayReportMsg(
            room="koopajr_result", payload=KOOPAJR_RESULT_LOSS))
        self.assertEqual(state.count_emitted(), 0)

        # Second attempt — win.
        process_event(state, PlayReportMsg(
            room="course_result", payload=PALACE_COURSE_RESULT))
        process_event(state, PlayReportMsg(
            room="koopajr_result", payload=KOOPAJR_RESULT_WIN))

        self.assertTrue(state.has_emitted(
            CheckKind.PALACE_CLEAR, PIPEROCK_PALACE_STAGE_KEY))
        self.assertEqual(state.count_emitted(), 1)


# ---------------------------------------------------------------------------
# Boring rooms shouldn't crash the processor.

class TestIgnoredRooms(unittest.TestCase):
    def test_world_activity_is_a_noop(self):
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="world_activity", payload=WORLD_ACTIVITY))
        self.assertEqual(emitted, [])

    def test_world_result_intra_world_is_a_noop(self):
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="world_result", payload=WORLD_RESULT))
        self.assertEqual(emitted, [])

    def test_world_result_inter_world_is_a_noop(self):
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="world_result", payload=WORLD_RESULT_W1_TO_W2))
        self.assertEqual(emitted, [])


# ---------------------------------------------------------------------------
# DeathLink (M3.8 — outbound: emit DeathReported per DEATH_DETECTED so the
# AP layer can decide whether to bounce.  The local death_count counter
# is bumped for diagnostics on every detected death regardless of
# DeathLink enable state.)

class TestDeathTracking(unittest.TestCase):
    def test_death_detected_bumps_counter(self):
        state = BridgeState()
        self.assertEqual(state.death_count, 0)
        process_event(state, NerveFireMsg(kind=NerveKind.DEATH_DETECTED, seq=1))
        process_event(state, NerveFireMsg(kind=NerveKind.DEATH_DETECTED, seq=2))
        self.assertEqual(state.death_count, 2)
        # Deaths don't emit AP location checks.
        self.assertEqual(state.count_emitted(), 0)

    def test_death_detected_emits_death_reported(self):
        state = BridgeState()
        emitted = process_event(
            state, NerveFireMsg(kind=NerveKind.DEATH_DETECTED, seq=42))
        self.assertEqual(len(emitted), 1)
        self.assertIsInstance(emitted[0], DeathReported)
        self.assertEqual(emitted[0].seq, 42)

    def test_death_detected_does_not_emit_check(self):
        """DeathReported is its own emit type; no CheckEmitted produced."""
        state = BridgeState()
        emitted = process_event(
            state, NerveFireMsg(kind=NerveKind.DEATH_DETECTED, seq=1))
        self.assertFalse(any(isinstance(e, CheckEmitted) for e in emitted))


# ---------------------------------------------------------------------------
# M3.7 Game-completion goal hook — outbound: emit GoalCompleted once on
# the first GAME_GOAL_REACHED Nerve fire, deduped by BridgeState so a
# replay (e.g. player re-enters the cleared save) is silenced.

class TestGameGoalReached(unittest.TestCase):
    def test_first_fire_emits_goal_completed(self):
        state = BridgeState()
        self.assertFalse(state.goal_complete)
        emitted = process_event(
            state, NerveFireMsg(kind=NerveKind.GAME_GOAL_REACHED, seq=1))
        self.assertEqual(len(emitted), 1)
        self.assertIsInstance(emitted[0], GoalCompleted)
        self.assertEqual(emitted[0].seq, 1)
        self.assertTrue(state.goal_complete)

    def test_second_fire_is_deduped(self):
        """The Nerve should only fire once per save, but a replay (save
        reload + cleared post-Bowser cutscene) MUST not generate a second
        AP StatusUpdate."""
        state = BridgeState()
        first = process_event(
            state, NerveFireMsg(kind=NerveKind.GAME_GOAL_REACHED, seq=1))
        second = process_event(
            state, NerveFireMsg(kind=NerveKind.GAME_GOAL_REACHED, seq=2))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_game_goal_does_not_emit_check(self):
        """GoalCompleted is its own emit type; no CheckEmitted produced."""
        state = BridgeState()
        emitted = process_event(
            state, NerveFireMsg(kind=NerveKind.GAME_GOAL_REACHED, seq=1))
        self.assertFalse(any(isinstance(e, CheckEmitted) for e in emitted))


# ---------------------------------------------------------------------------
# M2.2 TEN_COIN emission — diff of big_flower_coin_course_{in,out}.
#
# All live `course_result` fixtures (COURSE_RESULT, W1_2_COURSE_RESULT_SECRET,
# PALACE_COURSE_RESULT) happen to have `_in == _out` so they exercise only
# the no-op path.  The diff path itself is exercised via _emit_ten_coin_checks
# called with synthetic field dicts — building a hand-encoded PlayReport
# payload just to drive the same code through process_event would add a lot
# of bytes for no extra coverage.

class TestTenCoinDiffEmission(unittest.TestCase):

    def _stage_info(self) -> dict:
        return {"stage_key": W1_2_STAGE_KEY, "world_no": 1, "course_no": 3}

    def test_single_coin_newly_collected_at_index_0(self):
        state = BridgeState()
        fields = {
            "big_flower_coin_course_in":  [False, False, False],
            "big_flower_coin_course_out": [True,  False, False],
        }
        emitted = _emit_ten_coin_checks(state, self._stage_info(), fields)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].kind, CheckKind.TEN_COIN)
        self.assertEqual(emitted[0].stage_key, W1_2_STAGE_KEY)
        self.assertEqual(emitted[0].metadata["coin_index"], 0)

    def test_two_coins_newly_collected_with_one_carryover(self):
        state = BridgeState()
        fields = {
            "big_flower_coin_course_in":  [True, False, False],
            "big_flower_coin_course_out": [True, True,  True],
        }
        emitted = _emit_ten_coin_checks(state, self._stage_info(), fields)
        indices = sorted(c.metadata["coin_index"] for c in emitted)
        self.assertEqual(indices, [1, 2])
        # Existing carryover (#0) doesn't fire because we only emit on
        # the False→True transition.
        self.assertFalse(any(c.metadata["coin_index"] == 0 for c in emitted))

    def test_all_three_collected_in_one_run_fires_three(self):
        state = BridgeState()
        fields = {
            "big_flower_coin_course_in":  [False, False, False],
            "big_flower_coin_course_out": [True,  True,  True],
        }
        emitted = _emit_ten_coin_checks(state, self._stage_info(), fields)
        self.assertEqual(len(emitted), 3)
        indices = sorted(c.metadata["coin_index"] for c in emitted)
        self.assertEqual(indices, [0, 1, 2])

    def test_no_change_emits_nothing(self):
        state = BridgeState()
        for arr in ([False, False, False], [True, True, True], [False, True, True]):
            fields = {
                "big_flower_coin_course_in":  arr,
                "big_flower_coin_course_out": arr,
            }
            emitted = _emit_ten_coin_checks(state, self._stage_info(), fields)
            self.assertEqual(emitted, [], f"arr={arr}")

    def test_true_to_false_transition_emits_nothing(self):
        # Shouldn't happen in practice (you can't un-collect a coin) but
        # the bridge must not crash or emit spurious checks if it does.
        state = BridgeState()
        fields = {
            "big_flower_coin_course_in":  [True, True, True],
            "big_flower_coin_course_out": [False, False, False],
        }
        emitted = _emit_ten_coin_checks(state, self._stage_info(), fields)
        self.assertEqual(emitted, [])

    def test_missing_arrays_emits_nothing(self):
        state = BridgeState()
        emitted = _emit_ten_coin_checks(state, self._stage_info(), {})
        self.assertEqual(emitted, [])

    def test_non_list_arrays_emits_nothing(self):
        state = BridgeState()
        fields = {
            "big_flower_coin_course_in":  None,
            "big_flower_coin_course_out": True,
        }
        emitted = _emit_ten_coin_checks(state, self._stage_info(), fields)
        self.assertEqual(emitted, [])

    def test_mismatched_array_lengths_uses_shorter(self):
        # Defensive: real payloads always have 3-element arrays, but
        # don't crash if the shape ever drifts.
        state = BridgeState()
        fields = {
            "big_flower_coin_course_in":  [False, False],
            "big_flower_coin_course_out": [True,  True, True],
        }
        emitted = _emit_ten_coin_checks(state, self._stage_info(), fields)
        indices = sorted(c.metadata["coin_index"] for c in emitted)
        self.assertEqual(indices, [0, 1])

    def test_duplicate_coin_grab_dedups(self):
        state = BridgeState()
        fields = {
            "big_flower_coin_course_in":  [False, False, False],
            "big_flower_coin_course_out": [True,  False, False],
        }
        first = _emit_ten_coin_checks(state, self._stage_info(), fields)
        second = _emit_ten_coin_checks(state, self._stage_info(), fields)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [],
            "same (stage_key, coin_index) must dedup on second emit")
        self.assertEqual(state.count_emitted(CheckKind.TEN_COIN), 1)

    def test_different_coin_indices_dedup_independently(self):
        # Critical: stage_key W1_2 dedup must NOT collapse coin_index 0
        # and coin_index 1 — they're separate AP locations.
        state = BridgeState()
        _emit_ten_coin_checks(state, self._stage_info(), {
            "big_flower_coin_course_in":  [False, True, True],
            "big_flower_coin_course_out": [True,  True, True],
        })
        # Re-enter the course, collect coin #1 (the second one).
        _emit_ten_coin_checks(state, self._stage_info(), {
            "big_flower_coin_course_in":  [True, False, True],
            "big_flower_coin_course_out": [True, True,  True],
        })
        self.assertEqual(state.count_emitted(CheckKind.TEN_COIN), 2)
        self.assertTrue(state.has_emitted(
            CheckKind.TEN_COIN, W1_2_STAGE_KEY, coin_index=0))
        self.assertTrue(state.has_emitted(
            CheckKind.TEN_COIN, W1_2_STAGE_KEY, coin_index=1))
        self.assertFalse(state.has_emitted(
            CheckKind.TEN_COIN, W1_2_STAGE_KEY, coin_index=2))


class TestTenCoinIntegrationViaFixtures(unittest.TestCase):
    """End-to-end check that the live fixtures don't accidentally emit
    TEN_COIN — all three have `_in == _out`, so the existing exit-type
    behaviour must be unchanged."""

    def test_w1_1_top_of_flag_emits_no_ten_coin(self):
        state = BridgeState()
        emitted = process_event(
            state, PlayReportMsg(room="course_result", payload=COURSE_RESULT))
        self.assertEqual(
            [c.kind for c in emitted], [CheckKind.TOP_OF_FLAG])
        self.assertEqual(state.count_emitted(CheckKind.TEN_COIN), 0)

    def test_w1_2_secret_exit_emits_no_ten_coin(self):
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="course_result", payload=W1_2_COURSE_RESULT_SECRET))
        self.assertEqual(
            [c.kind for c in emitted], [CheckKind.SECRET_EXIT])
        self.assertEqual(state.count_emitted(CheckKind.TEN_COIN), 0)

    def test_palace_companion_emits_no_ten_coin(self):
        # world_mother_seed=True early-return must keep us out of the
        # TEN_COIN code path entirely.
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="course_result", payload=PALACE_COURSE_RESULT))
        self.assertEqual(emitted, [])
        self.assertEqual(state.count_emitted(CheckKind.TEN_COIN), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
