"""Tests for the bridge event processor.

Replays event sequences (PlayReport bytes + nerve fires) through the
processor and asserts on the resulting CheckEmitted list / state.

All PlayReport fixtures live in test_play_report.py; we import them
here so the corpus is shared between the format-level tests and the
behavioral tests.
"""

from __future__ import annotations

import unittest

# Support both `python -m unittest bridge.test_processor` and direct
# `python bridge/test_processor.py`.
if __package__ is None or __package__ == "":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from processor import process_event
    from protocol import CheckKind, NerveFireMsg, NerveKind, PlayReportMsg
    from state import BridgeState, CurrentCourse
    from test_play_report import (
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
else:
    from .processor import process_event
    from .protocol import CheckKind, NerveFireMsg, NerveKind, PlayReportMsg
    from .state import BridgeState, CurrentCourse
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
        with self.assertLogs("bridge.processor", level="WARNING"):
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
# DeathLink counter (M3.8 groundwork — counting only, no triggering yet).

class TestDeathTracking(unittest.TestCase):
    def test_death_detected_bumps_counter(self):
        state = BridgeState()
        self.assertEqual(state.death_count, 0)
        process_event(state, NerveFireMsg(kind=NerveKind.DEATH_DETECTED, seq=1))
        process_event(state, NerveFireMsg(kind=NerveKind.DEATH_DETECTED, seq=2))
        self.assertEqual(state.death_count, 2)
        # Deaths don't emit AP checks directly.
        self.assertEqual(state.count_emitted(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
