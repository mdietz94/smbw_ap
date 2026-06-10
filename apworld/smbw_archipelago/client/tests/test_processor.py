"""Tests for the bridge event processor.

Replays event sequences (PlayReport bytes + nerve fires) through the
processor and asserts on the resulting CheckEmitted list / state.

All PlayReport fixtures live in test_play_report.py; we import them
here so the corpus is shared between the format-level tests and the
behavioral tests.
"""

from __future__ import annotations

import unittest

from ..processor import (
    _BOWSER_CASTLE_STAGE_KEYS,
    _BREAK_TIME_STAGE_KEYS,
    _FAKE_EXIT_STAGE_KEYS,
    _FINAL_BOWSER_STAGE_KEY,
    _HUB_HOUSE_STAGE_KEYS,
    _STAGE_TO_BADGE_INTERNAL_ID,
    _emit_ten_coin_checks,
    _handle_course_in,
    _handle_course_result,
    process_event,
)
from ..protocol import (
    BadgeAcquiredMsg,
    CheckEmitted,
    CheckKind,
    DeathReported,
    GateEntered,
    GateKind,
    GoalCompleted,
    NerveFireMsg,
    NerveKind,
    PlayReportMsg,
)
from ..state import BridgeState, CurrentCourse
from .test_play_report import (
    BREAK_TIME_COURSE_RESULT,
    COURSE_RESULT,
    COURSE_RESULT_QUIT,
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
ROBBIRD_COVE_STAGE_KEY = 979253884  # W2 Robbird Cove (course_no 2)
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
        """Top of Flag is a strict superset of Normal Exit, so both
        check kinds fire on the same course_result."""
        state = BridgeState()
        emitted = process_event(
            state, PlayReportMsg(room="course_result", payload=COURSE_RESULT))
        self.assertEqual(len(emitted), 2)
        self.assertEqual(
            [c.kind for c in emitted],
            [CheckKind.NORMAL_EXIT, CheckKind.TOP_OF_FLAG])
        for c in emitted:
            self.assertEqual(c.stage_key, W1_1_STAGE_KEY)
            self.assertEqual(c.metadata["goal_id"], 0)
            self.assertTrue(c.metadata["touch_goal_top"])
            self.assertTrue(c.metadata["got_finish_seed"])

    def test_w1_2_secret_exit(self):
        """The fixture has goal_id=1 (secret) AND
        touch_goal_top_result=True (player happened to top the secret
        flagpole), so both SECRET_EXIT and TOP_OF_FLAG fire — there is
        one Top of Flag location per course, shared between the normal
        and secret flagpoles."""
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="course_result", payload=W1_2_COURSE_RESULT_SECRET))
        self.assertEqual(len(emitted), 2)
        self.assertEqual(
            [e.kind for e in emitted],
            [CheckKind.SECRET_EXIT, CheckKind.TOP_OF_FLAG])
        for c in emitted:
            self.assertEqual(c.stage_key, W1_2_STAGE_KEY)
            self.assertEqual(c.metadata["goal_id"], 1)
            self.assertTrue(c.metadata["touch_goal_top"])

    def test_duplicate_course_result_dedups(self):
        state = BridgeState()
        first = process_event(
            state, PlayReportMsg(room="course_result", payload=COURSE_RESULT))
        second = process_event(
            state, PlayReportMsg(room="course_result", payload=COURSE_RESULT))
        # Top-of-flag clear emits both NORMAL_EXIT and TOP_OF_FLAG; a
        # replay of the same payload must dedup both.
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [])

    def test_palace_course_result_emits_palace_clear_not_normal_exit(self):
        """A palace course_result (goal_id=0, touch_goal_top=False, the
        AAPCS-default zeroed fields) must route to PALACE_CLEAR, NOT the
        Normal Exit those fields would naively pick.

        This is the Royal-Seed check-loss fix (Issue 4, 2026-06-02): the
        koopajr_result PlayReport that previously carried PALACE_CLEAR is
        SKIPPED by the game when the world's seed bool is already set, so
        course_result is sometimes the only palace event we receive.
        Emitting PALACE_CLEAR here (rather than the old ``kinds = []``)
        makes the clear authoritative off course_result; the concurrent
        koopajr_result, when it does fire, dedups against this emit."""
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="course_result", payload=PALACE_COURSE_RESULT))
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].kind, CheckKind.PALACE_CLEAR)
        self.assertEqual(emitted[0].stage_key, PIPEROCK_PALACE_STAGE_KEY)
        self.assertFalse(state.has_emitted(
            CheckKind.NORMAL_EXIT, PIPEROCK_PALACE_STAGE_KEY))

    def test_fake_exit_at_jump_jump_jump_emits_fake_exit_and_top_of_flag(self):
        """W2: Jump! Jump Jump! Fake Exit live capture, 2026-05-29.
        The PlayReport had goal_id=1 (NOT 2 as previously guessed),
        touch_goal_top_result=True.  Must route to FAKE_EXIT (via the
        stage-key allowlist) AND TOP_OF_FLAG (player topped the fake
        flag, which the user wants to count toward the shared
        TOP_OF_FLAG check)."""
        state = BridgeState()
        fields = {
            "stage_info": {
                "stage_key": 0x9C6F85C5,  # W2: Jump! Jump Jump!
                "world_no": 2,
                "course_no": 10,
            },
            "course_result": 1,
            "goal_id": 1,
            "touch_goal_top_result": True,
            "world_mother_seed": False,
            "total_get_finish_seed_count": 0,
        }
        emitted = _handle_course_result(state, fields)
        self.assertEqual(
            [c.kind for c in emitted],
            [CheckKind.FAKE_EXIT, CheckKind.TOP_OF_FLAG])
        for c in emitted:
            self.assertEqual(c.stage_key, 0x9C6F85C5)
            self.assertEqual(c.metadata["goal_id"], 1)
            self.assertTrue(c.metadata["touch_goal_top"])

    def test_fake_exit_without_top_of_flag(self):
        """A Fake Exit clear without topping the fake flag fires only
        FAKE_EXIT -- mirrors the goal_id==0 / goal_id==1 paths."""
        state = BridgeState()
        fields = {
            "stage_info": {
                "stage_key": 0xCAAB3439,  # W5: Poison Ruins
                "world_no": 5,
                "course_no": 14,
            },
            "course_result": 1,
            "goal_id": 1,
            "touch_goal_top_result": False,
            "world_mother_seed": False,
            "total_get_finish_seed_count": 0,
        }
        emitted = _handle_course_result(state, fields)
        self.assertEqual([c.kind for c in emitted], [CheckKind.FAKE_EXIT])

    def test_secret_exit_unchanged_at_non_fake_exit_stage(self):
        """Regression guard for the FAKE_EXIT allowlist: a goal_id==1
        clear at a stage NOT in _FAKE_EXIT_STAGE_KEYS must still route
        to SECRET_EXIT, never FAKE_EXIT."""
        state = BridgeState()
        fields = {
            "stage_info": {
                "stage_key": W1_2_STAGE_KEY,  # Piranha Plants on Parade
                "world_no": 1,
                "course_no": 3,
            },
            "course_result": 1,
            "goal_id": 1,
            "touch_goal_top_result": False,
            "world_mother_seed": False,
            "total_get_finish_seed_count": 0,
        }
        emitted = _handle_course_result(state, fields)
        self.assertEqual([c.kind for c in emitted], [CheckKind.SECRET_EXIT])

    def test_fake_exit_stage_set_disjoint_from_other_routing_sets(self):
        """Routing sets must carve disjoint stage_key namespaces -- an
        overlap with palace/hub-house/break-time would create ambiguity."""
        self.assertEqual(
            _FAKE_EXIT_STAGE_KEYS & _HUB_HOUSE_STAGE_KEYS, frozenset())
        self.assertEqual(
            _FAKE_EXIT_STAGE_KEYS & _BREAK_TIME_STAGE_KEYS, frozenset())
        from ..processor import _PALACE_STAGE_KEYS
        self.assertEqual(
            _FAKE_EXIT_STAGE_KEYS & _PALACE_STAGE_KEYS, frozenset())

    def test_pause_menu_quit_suppressed(self):
        """In-course pause-menu quit emits a course_result PlayReport
        with course_result=3 and default goal_id=0 / touch_goal_top=False.
        Pre-fix the bridge classified that as NORMAL_EXIT and shipped
        an AP check (see Robbird Cove quit, 2026-05-26).  Must emit
        nothing now."""
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="course_result", payload=COURSE_RESULT_QUIT))
        self.assertEqual(emitted, [])
        self.assertFalse(state.has_emitted(
            CheckKind.NORMAL_EXIT, ROBBIRD_COVE_STAGE_KEY))
        self.assertFalse(state.has_emitted(
            CheckKind.TOP_OF_FLAG, ROBBIRD_COVE_STAGE_KEY))
        self.assertEqual(state.count_emitted(), 0)


# ---------------------------------------------------------------------------
# Hub-house remap: poplin houses + Sunbaked Desert House route their
# AP check off the exit event so replay-after-disconnect can re-fire.

ANGLER_POPLIN_STAGE_KEY = 0x3D3FE9D4   # PI: Angler Poplin's House


class TestHubHouseRemap(unittest.TestCase):

    def test_hub_house_set_membership(self):
        """Guard: all four hubs must be in the set so the remap fires."""
        self.assertIn(0x3D3FE9D4, _HUB_HOUSE_STAGE_KEYS)  # Angler
        self.assertIn(0x4A493BE1, _HUB_HOUSE_STAGE_KEYS)  # Master
        self.assertIn(0x9D67C898, _HUB_HOUSE_STAGE_KEYS)  # Sunbaked Desert
        self.assertIn(0x2A876538, _HUB_HOUSE_STAGE_KEYS)  # Loyal

    def test_wonder_seed_nerve_inside_hub_house_is_suppressed(self):
        """Seed-pickup nerve inside a hub-house must NOT fire an AP
        check -- the hub's WONDER_SEED check is routed off the exit
        event in _handle_course_result instead."""
        state = BridgeState()
        state.set_current_course(CurrentCourse(
            stage_key=ANGLER_POPLIN_STAGE_KEY, world_no=7, course_no=1))
        emitted = process_event(state, NerveFireMsg(
            kind=NerveKind.WONDER_SEED_AWARDED, seq=1))
        self.assertEqual(emitted, [])
        self.assertEqual(state.count_emitted(), 0)

    def test_hub_house_exit_emits_wonder_seed_not_secret_exit(self):
        """course_result with goal_id=1 (the shape the game emits when
        leaving Angler Poplin's House) at a hub-house stage_key must
        emit WONDER_SEED, never SECRET_EXIT."""
        state = BridgeState()
        fields = {
            "stage_info": {
                "stage_key": ANGLER_POPLIN_STAGE_KEY,
                "world_no": 7,
                "course_no": 1,
            },
            "course_result": 1,
            "goal_id": 1,
            "touch_goal_top_result": False,
            "world_mother_seed": False,
            "total_get_finish_seed_count": 0,
        }
        emitted = _handle_course_result(state, fields)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].kind, CheckKind.WONDER_SEED)
        self.assertEqual(emitted[0].stage_key, ANGLER_POPLIN_STAGE_KEY)
        self.assertFalse(
            state.has_emitted(CheckKind.SECRET_EXIT, ANGLER_POPLIN_STAGE_KEY))

    def test_hub_house_normal_exit_shape_also_emits_wonder_seed(self):
        """Defence-in-depth: even if a future game patch flipped the
        hub-house exit to goal_id=0, the remap still wins -- we want
        the AP check, not a NORMAL_EXIT to a location that doesn't
        exist in locations.json."""
        state = BridgeState()
        fields = {
            "stage_info": {
                "stage_key": ANGLER_POPLIN_STAGE_KEY,
                "world_no": 7,
                "course_no": 1,
            },
            "course_result": 1,
            "goal_id": 0,
            "touch_goal_top_result": False,
            "world_mother_seed": False,
            "total_get_finish_seed_count": 0,
        }
        emitted = _handle_course_result(state, fields)
        self.assertEqual([c.kind for c in emitted], [CheckKind.WONDER_SEED])

    def test_hub_house_replay_after_session_restart_re_fires(self):
        """The motivating use case: player picked up the seed but was
        disconnected from AP.  After restart (fresh BridgeState), the
        next exit must successfully fire the AP check."""
        # Session 1: pickup fires nothing (suppressed), exit fires once.
        state1 = BridgeState()
        state1.set_current_course(CurrentCourse(
            stage_key=ANGLER_POPLIN_STAGE_KEY, world_no=7, course_no=1))
        process_event(state1, NerveFireMsg(
            kind=NerveKind.WONDER_SEED_AWARDED, seq=1))
        self.assertEqual(state1.count_emitted(), 0)

        # Session 2 (post-restart): seed already collected in-game so
        # no nerve fires.  Exit still emits.
        state2 = BridgeState()
        fields = {
            "stage_info": {
                "stage_key": ANGLER_POPLIN_STAGE_KEY,
                "world_no": 7, "course_no": 1,
            },
            "course_result": 1,
            "goal_id": 1,
            "touch_goal_top_result": False,
            "world_mother_seed": False,
            "total_get_finish_seed_count": 0,
        }
        emitted = _handle_course_result(state2, fields)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].kind, CheckKind.WONDER_SEED)


# ---------------------------------------------------------------------------
# Break Time! courses — flagpole-less bonus stages with only a Wonder
# Seed AP location.  Same routing shape as hub houses but a separate
# set of stage_keys.  Regression target: the 2026-05-27 user report
# that a W4 Treasure Vault clear silently fired nothing because the
# processor was emitting NORMAL_EXIT (no such entry in locations.json
# for these stages).

TREASURE_VAULT_STAGE_KEY = 0xF9B39322  # W4: Treasure Vault — Break Time!


class TestBreakTimeRemap(unittest.TestCase):

    def test_break_time_set_contains_treasure_vault(self):
        """Guard: the live-captured fixture stage_key must be in the set
        or the rest of these tests are vacuous."""
        self.assertIn(TREASURE_VAULT_STAGE_KEY, _BREAK_TIME_STAGE_KEYS)

    def test_break_time_set_disjoint_from_hub_houses_and_palaces(self):
        """The three sets carve up distinct stage_key namespaces; an
        accidental overlap would create ambiguous routing."""
        from ..processor import _PALACE_STAGE_KEYS
        self.assertEqual(
            _BREAK_TIME_STAGE_KEYS & _HUB_HOUSE_STAGE_KEYS, frozenset())
        self.assertEqual(
            _BREAK_TIME_STAGE_KEYS & _PALACE_STAGE_KEYS, frozenset())

    def test_treasure_vault_clear_emits_wonder_seed_not_normal_exit(self):
        """The live-captured PlayReport: course_result=1, goal_id=0,
        touch_goal_top_result=False at a Break Time! stage_key must
        emit WONDER_SEED (the only AP location these stages have),
        NEVER NORMAL_EXIT."""
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="course_result", payload=BREAK_TIME_COURSE_RESULT))
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].kind, CheckKind.WONDER_SEED)
        self.assertEqual(emitted[0].stage_key, TREASURE_VAULT_STAGE_KEY)
        self.assertFalse(state.has_emitted(
            CheckKind.NORMAL_EXIT, TREASURE_VAULT_STAGE_KEY))

    def test_wonder_seed_nerve_inside_break_time_is_suppressed(self):
        """Same one-shot semantics as hub houses: the in-game seed
        only awards once per save, so the nerve path is suppressed in
        favor of the course_result re-fire path."""
        state = BridgeState()
        state.set_current_course(CurrentCourse(
            stage_key=TREASURE_VAULT_STAGE_KEY, world_no=5, course_no=37))
        emitted = process_event(state, NerveFireMsg(
            kind=NerveKind.WONDER_SEED_AWARDED, seq=1))
        self.assertEqual(emitted, [])
        self.assertEqual(state.count_emitted(), 0)

    def test_break_time_replay_after_session_restart_re_fires(self):
        """Disconnect at pickup → reconnect later: the next clear of
        the Break Time! course must still emit the AP check."""
        state2 = BridgeState()
        emitted = process_event(state2, PlayReportMsg(
            room="course_result", payload=BREAK_TIME_COURSE_RESULT))
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].kind, CheckKind.WONDER_SEED)
        self.assertEqual(emitted[0].stage_key, TREASURE_VAULT_STAGE_KEY)


# ---------------------------------------------------------------------------
# KO Arena courses (Issue 2, 2026-06-02) — combat-only arenas whose
# only AP location is a Wonder Seed (plus three 10-coin checks).  They
# share the Break Time! routing: the in-game wonder-seed nerve is
# suppressed and the seed routes off course_result as WONDER_SEED.
# Regression target: the 11:43 run where W1 Pipe-Rock Rumble cleared,
# logged "course_result → normal_exit" + "no AP location for
# kind=normal_exit ... (table needs extending)", silently dropping the
# Wonder Seed while its three 10-coin checks (off the unconditional
# big_flower_coin diff) still sent.

PIPE_ROCK_RUMBLE_STAGE_KEY = 0x495A535A  # W1: Pipe-Rock Rumble — KO Arena
KO_ARENA_STAGE_KEYS = frozenset({
    0x495A535A,  # W1: Pipe-Rock Rumble
    0x0FB8DE7F,  # W2: Fluff-Puff Kerfuff
    0x28382D44,  # PI: Petal Meddle
    0x389A6B66,  # W4: Sunbaked Skirmish
    0x82259180,  # W5: Fungi Funk
    0x086183F8,  # W6: Magma Flare-Up
})


class TestKoArenaRemap(unittest.TestCase):

    def test_all_ko_arenas_in_break_time_set(self):
        """Guard: every KO Arena stage_key must be in
        _BREAK_TIME_STAGE_KEYS or its clear misroutes to NORMAL_EXIT."""
        self.assertTrue(KO_ARENA_STAGE_KEYS <= _BREAK_TIME_STAGE_KEYS)

    def test_ko_arena_clear_emits_wonder_seed_and_ten_coins(self):
        """A KO Arena clear (course_result=1, goal_id=0,
        touch_goal_top=False) emits WONDER_SEED -- never NORMAL_EXIT --
        while the three 10-coin checks still fire off the big-flower-coin
        diff (the diff path is what already worked in the buggy run)."""
        state = BridgeState()
        fields = {
            "stage_info": {
                "stage_key": PIPE_ROCK_RUMBLE_STAGE_KEY,
                "world_no": 1,
                "course_no": 40,
            },
            "course_result": 1,
            "goal_id": 0,
            "touch_goal_top_result": False,
            "world_mother_seed": False,
            "total_get_finish_seed_count": 0,
            # All three big flower coins newly collected this run.
            "big_flower_coin_course_in": [False, False, False],
            "big_flower_coin_course_out": [True, True, True],
        }
        emitted = _handle_course_result(state, fields)
        kinds = [c.kind for c in emitted]
        self.assertEqual(kinds.count(CheckKind.WONDER_SEED), 1)
        self.assertEqual(kinds.count(CheckKind.TEN_COIN), 3)
        self.assertNotIn(CheckKind.NORMAL_EXIT, kinds)
        self.assertEqual(emitted[0].kind, CheckKind.WONDER_SEED)
        self.assertEqual(
            emitted[0].stage_key, PIPE_ROCK_RUMBLE_STAGE_KEY)
        self.assertFalse(state.has_emitted(
            CheckKind.NORMAL_EXIT, PIPE_ROCK_RUMBLE_STAGE_KEY))

    def test_wonder_seed_nerve_inside_ko_arena_is_suppressed(self):
        """The in-game wonder-seed nerve fires once per save inside a KO
        Arena; suppress it so the seed routes via the course_result
        re-fire path (same one-shot semantics as Break Time!)."""
        state = BridgeState()
        state.set_current_course(CurrentCourse(
            stage_key=PIPE_ROCK_RUMBLE_STAGE_KEY, world_no=1, course_no=40))
        emitted = process_event(state, NerveFireMsg(
            kind=NerveKind.WONDER_SEED_AWARDED, seq=1))
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

        # Expected outcome: WONDER_SEED + TOP_OF_FLAG + NORMAL_EXIT,
        # all attributed to W1-1 (Top of Flag is a strict superset of
        # Normal Exit so both clear checks fire).
        self.assertTrue(state.has_emitted(CheckKind.WONDER_SEED, W1_1_STAGE_KEY))
        self.assertTrue(state.has_emitted(CheckKind.TOP_OF_FLAG, W1_1_STAGE_KEY))
        self.assertTrue(state.has_emitted(CheckKind.NORMAL_EXIT, W1_1_STAGE_KEY))
        self.assertEqual(state.count_emitted(), 3)

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
        # The fixture has touch_goal_top_result=True so TOP_OF_FLAG also
        # fires.  TOP_OF_FLAG is a single per-course location shared by
        # the normal and secret flagpoles, so topping the secret flag
        # in a course where the normal flag has not yet been topped
        # still fires it.
        self.assertTrue(state.has_emitted(CheckKind.TOP_OF_FLAG, W1_2_STAGE_KEY))
        self.assertEqual(state.count_emitted(), 3)

    def test_palace_win_dual_event_fires_only_palace_clear(self):
        """Palace WIN emits BOTH course_result AND koopajr_result.  The
        bridge must collapse this to a single PALACE_CLEAR AP check.
        Both events now emit PALACE_CLEAR for the same stage_key and the
        second one dedups at emit_check; no NORMAL_EXIT contamination.
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

    def test_palace_clear_without_koopajr_still_fires_royal_seed(self):
        """Issue 4 (Royal-Seed check loss, 2026-06-02): when the world's
        Royal Seed bool is already set (AP granted the seed item before
        the player beat the palace), the game SKIPS the boss cutscene and
        no koopajr_result PlayReport is sent -- the only palace event the
        bridge sees is the course_result.  Live evidence from the 21:06
        run: W5 Operation Poplin Rescue logged the palace course_result
        but no koopajr_result ever followed (next event was world_result),
        so the - Royal Seed check was lost.  Beating the palace must send
        the PALACE_CLEAR (-> - Royal Seed) check off course_result alone,
        with NO trailing koopajr_result."""
        state = BridgeState()
        state.set_current_course(CurrentCourse(
            stage_key=PIPEROCK_PALACE_STAGE_KEY,
            world_no=1, course_no=30))

        # Only the course_result arrives -- the boss fight is skipped
        # because AP already set the seed bool, so no koopajr_result.
        emitted = process_event(state, PlayReportMsg(
            room="course_result", payload=PALACE_COURSE_RESULT))

        palace_clears = [e for e in emitted
                         if isinstance(e, CheckEmitted)
                         and e.kind == CheckKind.PALACE_CLEAR]
        self.assertEqual(len(palace_clears), 1)
        self.assertEqual(
            palace_clears[0].stage_key, PIPEROCK_PALACE_STAGE_KEY)
        self.assertTrue(state.has_emitted(
            CheckKind.PALACE_CLEAR, PIPEROCK_PALACE_STAGE_KEY))
        self.assertFalse(state.has_emitted(
            CheckKind.NORMAL_EXIT, PIPEROCK_PALACE_STAGE_KEY))


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

    def test_panel_game_result_is_a_noop(self):
        """Standee shop purchases fire panel_game_result with a
        distinct schema; the processor must explicitly suppress them
        so they don't trip the unknown-room warning."""
        state = BridgeState()
        # Minimal panel_game_result — exact schema doesn't matter; the
        # contract is "no emits, no warnings".
        emitted = process_event(state, PlayReportMsg(
            room="panel_game_result",
            payload=bytes.fromhex(
                "de 00 01"
                "ac 72 65 73 75 6c 74 5f 61 72 72 61 79 90".replace(" ", ""))))
        self.assertEqual(emitted, [])
        self.assertEqual(state.count_emitted(), 0)


# ---------------------------------------------------------------------------
# Poplin Shop purchases — general_shop_result PlayReport.  Only item_kind=1
# (Wonder Seed) produces a SHOP_SEED check; badge / consumable buys are
# dropped silently.  Dedup key includes the shop_slot (item_value) so the
# W4 Secret 3-slot shop's three priced seeds dedup independently.

class TestPoplinShopPurchase(unittest.TestCase):
    @staticmethod
    def _shop_payload(world_no_hex: str, npc_id_hex: str,
                       item_kind_hex: str, item_value_bytes: str) -> bytes:
        """Synthesize a general_shop_result payload mirroring the W1
        Poplin Shop capture shape, parametrized on the four discriminators."""
        hexstr = (
            "de 00 07"
            "ab 73 61 76 65 64 61 74 61 5f 69 64 d9 23"
            "62 38 31 33 65 36 37 35 2d 65 62 32 35 34 63 38 61 2d"
            "61 33 65 30 64 30 35 32 2d 64 66 31 61 66 61 64 30"
            "a9 70 6c 61 79 5f 6d 6f 64 65 01"
            "af 74 6f 74 61 6c 5f 70 6c 61 79 5f 74 69 6d 65"
            "d7 00 00 00 00 00 00 00 00 4c"
            "aa 73 74 61 67 65 5f 69 6e 66 6f"
            "83 a9 73 74 61 67 65 5f 6b 65 79 d3 00 00 00 00 d4 a6 26 5d"
            "aa 77 6f 72 6c 64 5f 6b 69 6e 64 00"
            f"a8 77 6f 72 6c 64 5f 6e 6f {world_no_hex}"
            f"a6 6e 70 63 5f 69 64 {npc_id_hex}"
            "af 69 74 65 6d 5f 69 6e 66 6f 5f 61 72 72 61 79"
            "91 82"
            f"a9 69 74 65 6d 5f 6b 69 6e 64 {item_kind_hex}"
            f"aa 69 74 65 6d 5f 76 61 6c 75 65 {item_value_bytes}"
            "b1 73 79 73 74 65 6d 5f 72 65 70 6f 72 74 5f 74 61 67"
            "ce 81 a7 03 b8")
        return bytes.fromhex(hexstr.replace(" ", ""))

    def test_wonder_seed_buy_emits_shop_seed(self):
        from .test_play_report import GENERAL_SHOP_RESULT_W1_SEED
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="general_shop_result",
            payload=GENERAL_SHOP_RESULT_W1_SEED))

        self.assertEqual(len(emitted), 1)
        check = emitted[0]
        self.assertIsInstance(check, CheckEmitted)
        self.assertEqual(check.kind, CheckKind.SHOP_SEED)
        # stage_key encoding: (world_no << 16) | npc_id = (1<<16) | 2 = 0x10002.
        self.assertEqual(check.stage_key, (1 << 16) | 2)
        self.assertEqual(check.metadata["world_no"], 1)
        self.assertEqual(check.metadata["npc_id"], 2)
        self.assertEqual(check.metadata["item_value"], 0)
        self.assertEqual(check.metadata["shop_slot"], 0)

    def test_wonder_seed_buy_is_deduped(self):
        from .test_play_report import GENERAL_SHOP_RESULT_W1_SEED
        state = BridgeState()
        first = process_event(state, PlayReportMsg(
            room="general_shop_result",
            payload=GENERAL_SHOP_RESULT_W1_SEED))
        second = process_event(state, PlayReportMsg(
            room="general_shop_result",
            payload=GENERAL_SHOP_RESULT_W1_SEED))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(state.count_emitted(CheckKind.SHOP_SEED), 1)

    def test_badge_buy_emits_badge_acquired(self):
        """A badge buy fires general_shop_result with item_kind=0 and
        item_value = the SMBW internal badge id.  The processor emits
        BADGE_ACQUIRED with stage_key=internal_id, parallel to the
        Switch-side bitfield-diff path (main.cpp 'M2.3 diff-on-overwrite').
        Dedup by (kind, stage_key=internal_id) means at most one AP
        LocationCheck goes out per badge even if both paths fire."""
        # Coin Reward Badge buy at W1 Poplin Shop, internal_id=55 was the
        # original W1 Poplin capture; in the badge_table the canonical
        # Coin Reward internal_id is 9.  Either way the processor just
        # forwards item_value as the stage_key.
        payload = self._shop_payload(
            world_no_hex="01", npc_id_hex="02",
            item_kind_hex="00", item_value_bytes="cc 37")  # item_value=55
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="general_shop_result", payload=payload))
        self.assertEqual(len(emitted), 1)
        check = emitted[0]
        self.assertIsInstance(check, CheckEmitted)
        self.assertEqual(check.kind, CheckKind.BADGE_ACQUIRED)
        self.assertEqual(check.stage_key, 55)
        self.assertEqual(check.metadata["source"], "shop_buy")
        self.assertEqual(check.metadata["world_no"], 1)
        self.assertEqual(check.metadata["npc_id"], 2)

    def test_badge_buy_dedups_against_replay(self):
        """The same shop badge buy seen twice (e.g. a PlayReport replay
        on AP reconnect) must dedup -- BridgeState keys BADGE_ACQUIRED by
        (kind, stage_key=internal_id, sub_key=0)."""
        payload = self._shop_payload(
            world_no_hex="01", npc_id_hex="02",
            item_kind_hex="00", item_value_bytes="cc 37")
        state = BridgeState()
        first = process_event(state, PlayReportMsg(
            room="general_shop_result", payload=payload))
        second = process_event(state, PlayReportMsg(
            room="general_shop_result", payload=payload))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(state.count_emitted(CheckKind.BADGE_ACQUIRED), 1)

    def test_consumable_buy_is_silent(self):
        """1-up buy: item_kind=2, item_value=1.  Not an AP check family."""
        payload = self._shop_payload(
            world_no_hex="01", npc_id_hex="02",
            item_kind_hex="02", item_value_bytes="01")
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="general_shop_result", payload=payload))
        self.assertEqual(emitted, [])

    def test_multi_item_buy_emits_one_check_per_seed(self):
        """A single shop transaction can include multiple items at
        once -- item_info_array is length-N, not always 1.  Two seeds
        bought in the same transaction must produce two distinct
        SHOP_SEED checks (different shop_slot sub_keys, so dedup
        doesn't collapse them)."""
        # Synthesize a payload with item_info_array opener 0x92 (array of 2):
        # two seed structs back-to-back at item_values 0 and 1.
        hexstr = (
            "de 00 07"
            "ab 73 61 76 65 64 61 74 61 5f 69 64 d9 23"
            "62 38 31 33 65 36 37 35 2d 65 62 32 35 34 63 38 61 2d"
            "61 33 65 30 64 30 35 32 2d 64 66 31 61 66 61 64 30"
            "a9 70 6c 61 79 5f 6d 6f 64 65 01"
            "af 74 6f 74 61 6c 5f 70 6c 61 79 5f 74 69 6d 65"
            "d7 00 00 00 00 00 00 00 00 4c"
            "aa 73 74 61 67 65 5f 69 6e 66 6f"
            "83 a9 73 74 61 67 65 5f 6b 65 79 d3 00 00 00 00 d4 a6 26 5d"
            "aa 77 6f 72 6c 64 5f 6b 69 6e 64 00"
            "a8 77 6f 72 6c 64 5f 6e 6f 05"
            "a6 6e 70 63 5f 69 64 05"
            "af 69 74 65 6d 5f 69 6e 66 6f 5f 61 72 72 61 79"
            "92"  # array of 2
            "82 a9 69 74 65 6d 5f 6b 69 6e 64 01 aa 69 74 65 6d 5f 76 61 6c 75 65 00"
            "82 a9 69 74 65 6d 5f 6b 69 6e 64 01 aa 69 74 65 6d 5f 76 61 6c 75 65 01"
            "b1 73 79 73 74 65 6d 5f 72 65 70 6f 72 74 5f 74 61 67"
            "ce 81 a7 03 b8")
        payload = bytes.fromhex(hexstr.replace(" ", ""))
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="general_shop_result", payload=payload))
        # Two distinct shop_slots → two distinct checks.
        self.assertEqual(len(emitted), 2)
        slots = sorted(c.metadata["shop_slot"] for c in emitted)
        self.assertEqual(slots, [0, 1])
        self.assertEqual(state.count_emitted(CheckKind.SHOP_SEED), 2)

    def test_mixed_seed_and_badge_buy_emits_both_checks(self):
        """A multi-item transaction can mix kinds (e.g. seed + badge).
        Both the seed (SHOP_SEED) and the badge (BADGE_ACQUIRED) fire
        from this path -- badges are now emitted parallel to the
        Switch-side bitfield-diff detector, with BridgeState dedup
        keeping at most one AP LocationCheck per badge."""
        hexstr = (
            "de 00 07"
            "ab 73 61 76 65 64 61 74 61 5f 69 64 d9 23"
            "62 38 31 33 65 36 37 35 2d 65 62 32 35 34 63 38 61 2d"
            "61 33 65 30 64 30 35 32 2d 64 66 31 61 66 61 64 30"
            "a9 70 6c 61 79 5f 6d 6f 64 65 01"
            "af 74 6f 74 61 6c 5f 70 6c 61 79 5f 74 69 6d 65"
            "d7 00 00 00 00 00 00 00 00 4c"
            "aa 73 74 61 67 65 5f 69 6e 66 6f"
            "83 a9 73 74 61 67 65 5f 6b 65 79 d3 00 00 00 00 d4 a6 26 5d"
            "aa 77 6f 72 6c 64 5f 6b 69 6e 64 00"
            "a8 77 6f 72 6c 64 5f 6e 6f 01"
            "a6 6e 70 63 5f 69 64 02"
            "af 69 74 65 6d 5f 69 6e 66 6f 5f 61 72 72 61 79"
            "92"  # array of 2
            "82 a9 69 74 65 6d 5f 6b 69 6e 64 01 aa 69 74 65 6d 5f 76 61 6c 75 65 00"
            "82 a9 69 74 65 6d 5f 6b 69 6e 64 00 aa 69 74 65 6d 5f 76 61 6c 75 65 cc 37"
            "b1 73 79 73 74 65 6d 5f 72 65 70 6f 72 74 5f 74 61 67"
            "ce 81 a7 03 b8")
        payload = bytes.fromhex(hexstr.replace(" ", ""))
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="general_shop_result", payload=payload))
        # Seed → SHOP_SEED check; badge (item_value=55) → BADGE_ACQUIRED.
        self.assertEqual(len(emitted), 2)
        kinds = sorted(c.kind.value for c in emitted)
        self.assertEqual(kinds, ["badge_acquired", "shop_seed"])
        seed = next(c for c in emitted if c.kind == CheckKind.SHOP_SEED)
        badge = next(c for c in emitted if c.kind == CheckKind.BADGE_ACQUIRED)
        self.assertEqual(seed.metadata["item_value"], 0)
        self.assertEqual(badge.stage_key, 55)
        self.assertEqual(badge.metadata["source"], "shop_buy")

    def test_w4_secret_three_slots_dedup_independently(self):
        """The W4 Poplin Shop (Secret) sells 3 Wonder Seeds at the same
        (world_no, npc_id); they share the SHOP_SEED stage_key but
        must NOT dedup against each other.  Distinguished by item_value
        (passed through as ``shop_slot``).

        npc_id=0x05 is a placeholder — W4 Secret's real npc_id is
        TBD until captured; this test only exercises the dedup logic,
        not the real shop identity."""
        state = BridgeState()
        e0 = process_event(state, PlayReportMsg(
            room="general_shop_result",
            payload=self._shop_payload("04", "05", "01", "00")))
        e1 = process_event(state, PlayReportMsg(
            room="general_shop_result",
            payload=self._shop_payload("04", "05", "01", "01")))
        e2 = process_event(state, PlayReportMsg(
            room="general_shop_result",
            payload=self._shop_payload("04", "05", "01", "02")))
        # Replay slot 0 — dedup must catch it.
        e_dup = process_event(state, PlayReportMsg(
            room="general_shop_result",
            payload=self._shop_payload("04", "05", "01", "00")))

        self.assertEqual(len(e0), 1)
        self.assertEqual(len(e1), 1)
        self.assertEqual(len(e2), 1)
        self.assertEqual(e_dup, [])
        self.assertEqual(state.count_emitted(CheckKind.SHOP_SEED), 3)


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
            [c.kind for c in emitted],
            [CheckKind.NORMAL_EXIT, CheckKind.TOP_OF_FLAG])
        self.assertEqual(state.count_emitted(CheckKind.TEN_COIN), 0)

    def test_w1_2_secret_exit_emits_no_ten_coin(self):
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="course_result", payload=W1_2_COURSE_RESULT_SECRET))
        # The fixture has touch_goal_top_result=True so the secret-exit
        # path emits both SECRET_EXIT and TOP_OF_FLAG.  No TEN_COIN.
        self.assertEqual(
            [c.kind for c in emitted],
            [CheckKind.SECRET_EXIT, CheckKind.TOP_OF_FLAG])
        self.assertEqual(state.count_emitted(CheckKind.TEN_COIN), 0)

    def test_palace_companion_emits_no_ten_coin(self):
        # PALACE_COURSE_RESULT has _in == _out for big_flower_coin so
        # the 10-coin diff naturally yields zero emits.  The exit-type
        # emit is now PALACE_CLEAR (the _PALACE_STAGE_KEYS branch routes
        # there rather than dropping it -- see the Royal-Seed check-loss
        # fix); the point of this test is that NO TEN_COIN fires when the
        # diff is empty.
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="course_result", payload=PALACE_COURSE_RESULT))
        self.assertEqual(
            [c.kind for c in emitted], [CheckKind.PALACE_CLEAR])
        self.assertEqual(state.count_emitted(CheckKind.TEN_COIN), 0)

    def test_palace_with_new_ten_coins_emits_them(self):
        """Regression: a palace clear that picks up new 10-coins must
        still emit TEN_COIN AP checks.  The palace branch now emits
        PALACE_CLEAR (not NORMAL_EXIT) and falls through to the 10-coin
        diff, so the headline emit is PALACE_CLEAR followed by the three
        TEN_COIN checks -- never NORMAL_EXIT."""
        state = BridgeState()
        fields = {
            "stage_info": {
                "stage_key": PIPEROCK_PALACE_STAGE_KEY,
                "world_no": 1,
                "course_no": 8,
            },
            "course_result": 1,
            "goal_id": 0,
            "touch_goal_top_result": False,
            "world_mother_seed": True,
            "total_get_finish_seed_count": 0,
            "big_flower_coin_course_in":  [False, False, False],
            "big_flower_coin_course_out": [True,  True,  True],
        }
        emitted = _handle_course_result(state, fields)
        # PALACE_CLEAR + three TEN_COIN checks; no NORMAL_EXIT.
        self.assertEqual(
            [c.kind for c in emitted],
            [CheckKind.PALACE_CLEAR, CheckKind.TEN_COIN,
             CheckKind.TEN_COIN, CheckKind.TEN_COIN])
        self.assertEqual(
            sorted(c.metadata["coin_index"]
                   for c in emitted if c.kind == CheckKind.TEN_COIN),
            [0, 1, 2])
        self.assertFalse(
            state.has_emitted(CheckKind.NORMAL_EXIT, PIPEROCK_PALACE_STAGE_KEY))


# ---------------------------------------------------------------------------
# Level-entry gate (course_in -> GateEntered; in_course flag lifecycle).

class TestLevelEntryGate(unittest.TestCase):
    """The processor emits a GateEntered when the player enters a gated
    course, and tracks the in_course flag across entry/exit."""

    @staticmethod
    def _course_in_fields(stage_key: int, world_no: int = 1,
                          course_no: int = 5) -> dict:
        return {"stage_info": {
            "stage_key": stage_key,
            "world_no": world_no,
            "course_no": course_no,
            "world_kind": 0,
        }}

    def test_course_in_sets_in_course_flag(self):
        state = BridgeState()
        self.assertFalse(state.in_course)
        process_event(state, PlayReportMsg(
            room="course_in", payload=W1_2_COURSE_IN))
        self.assertTrue(state.in_course)
        self.assertTrue(state.is_in_course())

    def test_course_result_clears_in_course_flag(self):
        state = BridgeState()
        process_event(state, PlayReportMsg(
            room="course_in", payload=W1_2_COURSE_IN))
        self.assertTrue(state.in_course)
        process_event(state, PlayReportMsg(
            room="course_result", payload=COURSE_RESULT))
        self.assertFalse(state.in_course)
        # current_course stays sticky for post-course attribution.
        self.assertIsNotNone(state.current_course)

    def test_course_quit_clears_in_course_flag(self):
        """A pause-quit (course_result=3) must clear in_course too --
        that's the grace path the gate kill cancels on."""
        state = BridgeState()
        process_event(state, PlayReportMsg(
            room="course_in", payload=W1_2_COURSE_IN))
        process_event(state, PlayReportMsg(
            room="course_result", payload=COURSE_RESULT_QUIT))
        self.assertFalse(state.in_course)

    def test_koopajr_result_clears_in_course_flag(self):
        state = BridgeState()
        process_event(state, PlayReportMsg(
            room="course_in", payload=W1_2_COURSE_IN))
        process_event(state, PlayReportMsg(
            room="koopajr_result", payload=KOOPAJR_RESULT_LOSS))
        self.assertFalse(state.in_course)

    def test_normal_course_emits_no_gate(self):
        """A course that grants no badge and isn't Bowser produces no
        GateEntered (W1-2 Piranha Plants on Parade)."""
        state = BridgeState()
        emitted = process_event(state, PlayReportMsg(
            room="course_in", payload=W1_2_COURSE_IN))
        self.assertEqual(emitted, [])

    def test_badge_challenge_emits_gate_entered(self):
        state = BridgeState()
        # 0xDADED63E = W1 Wall-Climb Jump I -> badge internal_id 34.
        emitted = _handle_course_in(
            state, self._course_in_fields(0xDADED63E, world_no=1, course_no=5))
        self.assertEqual(len(emitted), 1)
        gate = emitted[0]
        self.assertIsInstance(gate, GateEntered)
        self.assertEqual(gate.gate_kind, GateKind.BADGE)
        self.assertEqual(gate.requirement, 34)
        self.assertEqual(gate.stage_key, 0xDADED63E)
        self.assertEqual(gate.world_no, 1)
        self.assertEqual(gate.course_no, 5)

    def test_story_badge_course_also_gates(self):
        """Per spec, ALL course-based badge grants gate (not just the
        named Badge Challenges) -- e.g. the Wiggler Race that hands over
        Auto Super Mushroom."""
        state = BridgeState()
        # 0x954EB962 = W1 Mountaineering! (Wiggler Race) -> badge 46.
        emitted = _handle_course_in(
            state, self._course_in_fields(0x954EB962))
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].gate_kind, GateKind.BADGE)
        self.assertEqual(emitted[0].requirement, 46)

    def test_every_badge_table_stage_gates(self):
        """Every entry in _STAGE_TO_BADGE_INTERNAL_ID must produce a
        BADGE GateEntered carrying that internal_id."""
        for stage_key, internal_id in _STAGE_TO_BADGE_INTERNAL_ID.items():
            state = BridgeState()
            emitted = _handle_course_in(
                state, self._course_in_fields(stage_key))
            self.assertEqual(len(emitted), 1, f"stage 0x{stage_key:08x}")
            self.assertEqual(emitted[0].gate_kind, GateKind.BADGE)
            self.assertEqual(emitted[0].requirement, internal_id)

    def test_final_bowser_emits_royal_seed_gate(self):
        state = BridgeState()
        emitted = _handle_course_in(
            state, self._course_in_fields(
                _FINAL_BOWSER_STAGE_KEY, world_no=2, course_no=99))
        self.assertEqual(len(emitted), 1)
        gate = emitted[0]
        self.assertEqual(gate.gate_kind, GateKind.ROYAL_SEEDS)
        self.assertEqual(gate.requirement, 6)
        self.assertEqual(gate.stage_key, _FINAL_BOWSER_STAGE_KEY)

    def test_every_bowser_castle_course_emits_royal_seed_gate(self):
        """EVERY Bowser's Castle course -- not just the final Rage Stage --
        gates on the all-six-Royal-Seed requirement."""
        self.assertIn(_FINAL_BOWSER_STAGE_KEY, _BOWSER_CASTLE_STAGE_KEYS)
        self.assertEqual(len(_BOWSER_CASTLE_STAGE_KEYS), 5)
        for sk in _BOWSER_CASTLE_STAGE_KEYS:
            state = BridgeState()
            emitted = _handle_course_in(
                state, self._course_in_fields(sk, world_no=8, course_no=1))
            self.assertEqual(len(emitted), 1, f"stage 0x{sk:08x}")
            self.assertEqual(emitted[0].gate_kind, GateKind.ROYAL_SEEDS)
            self.assertEqual(emitted[0].requirement, 6)
            self.assertEqual(emitted[0].stage_key, sk)


# ---------------------------------------------------------------------------
# M2.3 badge acquisition.

class TestBadgeAcquired(unittest.TestCase):
    """Switch-detected in-game badge pickup -> CheckEmitted."""

    def test_known_internal_id_emits_check(self):
        state = BridgeState()
        emitted = process_event(
            state, BadgeAcquiredMsg(internal_id=4, seq=1))  # Spring Feet
        self.assertEqual(len(emitted), 1)
        self.assertIsInstance(emitted[0], CheckEmitted)
        self.assertEqual(emitted[0].kind, CheckKind.BADGE_ACQUIRED)
        self.assertEqual(emitted[0].stage_key, 4)
        self.assertEqual(emitted[0].metadata["seq"], 1)

    def test_unmapped_internal_id_still_emits(self):
        """Mapping is the location_table's concern; the processor emits
        a CheckEmitted for every distinct internal_id and lets the
        downstream lookup decide what to do (log + drop for unmapped)."""
        state = BridgeState()
        emitted = process_event(
            state, BadgeAcquiredMsg(internal_id=99, seq=42))
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].stage_key, 99)

    def test_duplicate_acquisition_dedups(self):
        state = BridgeState()
        first = process_event(
            state, BadgeAcquiredMsg(internal_id=4, seq=1))
        second = process_event(
            state, BadgeAcquiredMsg(internal_id=4, seq=2))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(state.count_emitted(CheckKind.BADGE_ACQUIRED), 1)

    def test_different_badges_emit_separately(self):
        state = BridgeState()
        process_event(state, BadgeAcquiredMsg(internal_id=4))
        process_event(state, BadgeAcquiredMsg(internal_id=9))
        process_event(state, BadgeAcquiredMsg(internal_id=46))
        self.assertEqual(state.count_emitted(CheckKind.BADGE_ACQUIRED), 3)
        self.assertTrue(state.has_emitted(CheckKind.BADGE_ACQUIRED, 4))
        self.assertTrue(state.has_emitted(CheckKind.BADGE_ACQUIRED, 9))
        self.assertTrue(state.has_emitted(CheckKind.BADGE_ACQUIRED, 46))

    def test_badge_acquired_does_not_emit_death_reported(self):
        state = BridgeState()
        emitted = process_event(
            state, BadgeAcquiredMsg(internal_id=4))
        self.assertFalse(any(isinstance(e, DeathReported) for e in emitted))


# ---------------------------------------------------------------------------
# Course-clear -> BADGE_ACQUIRED.  Layered on top of the existing
# exit-type checks; only fires for courses in _STAGE_TO_BADGE_INTERNAL_ID.

from ..processor import _STAGE_TO_BADGE_INTERNAL_ID

# Stage keys from location_table for the courses that grant badges.
_STAGE_SPRING_FEET_I = 0xF421DB55
_STAGE_MOUNTAINEERING = 0x954EB962
_STAGE_BADGE_HOUSE_PIPEROCK = 0xA3207D45
_STAGE_UPSHROOM_DOWNSHROOM = 0x54A60980
_STAGE_WONDER_STAGE = 0x2D438F37
_STAGE_JET_RUN_II = 0x69B9F9E6


class TestCourseClearBadge(unittest.TestCase):
    """Clearing a badge-granting course must emit a BADGE_ACQUIRED check
    alongside the normal exit-type checks.  Dedup keeps the AP server
    from seeing the same location twice when both the course path and
    the Switch-side bitfield-diff path fire for the same badge."""

    @staticmethod
    def _course_result_fields(stage_key: int, *, goal_id: int = 0,
                              top: bool = False, course_result: int = 1
                              ) -> dict:
        return {
            "stage_info": {
                "stage_key": stage_key,
                "world_no": 0,
                "course_no": 0,
            },
            "course_result": course_result,
            "goal_id": goal_id,
            "touch_goal_top_result": top,
            "world_mother_seed": False,
            "total_get_finish_seed_count": 0,
        }

    def test_clearing_spring_feet_i_emits_spring_feet_badge(self):
        state = BridgeState()
        emitted = _handle_course_result(
            state, self._course_result_fields(_STAGE_SPRING_FEET_I))
        badge_emits = [c for c in emitted if c.kind == CheckKind.BADGE_ACQUIRED]
        self.assertEqual(len(badge_emits), 1)
        # internal_id 4 == Spring Feet per badge_table._BADGES.
        self.assertEqual(badge_emits[0].stage_key, 4)
        self.assertEqual(badge_emits[0].metadata["source"], "course_clear")
        self.assertEqual(
            badge_emits[0].metadata["course_stage_key"], _STAGE_SPRING_FEET_I)

    def test_clearing_mountaineering_emits_auto_super_mushroom_badge(self):
        state = BridgeState()
        emitted = _handle_course_result(
            state, self._course_result_fields(_STAGE_MOUNTAINEERING))
        badge_emits = [c for c in emitted if c.kind == CheckKind.BADGE_ACQUIRED]
        self.assertEqual(len(badge_emits), 1)
        self.assertEqual(badge_emits[0].stage_key, 46)

    def test_clearing_pipe_rock_badge_house_emits_parachute_cap(self):
        state = BridgeState()
        emitted = _handle_course_result(
            state, self._course_result_fields(_STAGE_BADGE_HOUSE_PIPEROCK))
        badge_emits = [c for c in emitted if c.kind == CheckKind.BADGE_ACQUIRED]
        self.assertEqual(len(badge_emits), 1)
        self.assertEqual(badge_emits[0].stage_key, 35)

    def test_clearing_upshroom_emits_sensor(self):
        state = BridgeState()
        emitted = _handle_course_result(
            state, self._course_result_fields(_STAGE_UPSHROOM_DOWNSHROOM))
        badge_emits = [c for c in emitted if c.kind == CheckKind.BADGE_ACQUIRED]
        self.assertEqual(len(badge_emits), 1)
        self.assertEqual(badge_emits[0].stage_key, 32)

    def test_clearing_wonder_stage_emits_sound_off(self):
        state = BridgeState()
        emitted = _handle_course_result(
            state, self._course_result_fields(_STAGE_WONDER_STAGE))
        badge_emits = [c for c in emitted if c.kind == CheckKind.BADGE_ACQUIRED]
        self.assertEqual(len(badge_emits), 1)
        self.assertEqual(badge_emits[0].stage_key, 33)

    def test_ii_course_grants_same_badge_as_i(self):
        """The 'II' badge challenges don't normally re-grant in-game,
        but the apworld randomizer can route a player to a II first;
        emit defensively so they still get credit."""
        state = BridgeState()
        emitted = _handle_course_result(
            state, self._course_result_fields(_STAGE_JET_RUN_II))
        badge_emits = [c for c in emitted if c.kind == CheckKind.BADGE_ACQUIRED]
        self.assertEqual(len(badge_emits), 1)
        # internal_id 19 == Jet Run.
        self.assertEqual(badge_emits[0].stage_key, 19)

    def test_non_badge_course_does_not_emit_badge(self):
        """A normal course (W1-1) clearing must not emit a phantom badge."""
        state = BridgeState()
        # W1-1 stage_key is in the location table but not a badge source.
        W1_1 = 0xAF11F7FC
        self.assertNotIn(W1_1, _STAGE_TO_BADGE_INTERNAL_ID)
        emitted = _handle_course_result(
            state, self._course_result_fields(W1_1, top=True))
        self.assertEqual(
            state.count_emitted(CheckKind.BADGE_ACQUIRED), 0)
        # Exit-type checks still fire.
        self.assertGreater(len(emitted), 0)

    def test_quit_suppresses_badge_emission(self):
        """A pause-menu quit (course_result=3) must not award the badge,
        even at a badge-granting stage_key -- the early return in
        _handle_course_result drops everything including the badge."""
        state = BridgeState()
        emitted = _handle_course_result(
            state, self._course_result_fields(
                _STAGE_SPRING_FEET_I, course_result=3))
        self.assertEqual(emitted, [])
        self.assertEqual(state.count_emitted(), 0)

    def test_badge_emit_dedups_across_replay(self):
        state = BridgeState()
        first = _handle_course_result(
            state, self._course_result_fields(_STAGE_SPRING_FEET_I))
        second = _handle_course_result(
            state, self._course_result_fields(_STAGE_SPRING_FEET_I))
        badge_first = [c for c in first if c.kind == CheckKind.BADGE_ACQUIRED]
        badge_second = [c for c in second if c.kind == CheckKind.BADGE_ACQUIRED]
        self.assertEqual(len(badge_first), 1)
        self.assertEqual(badge_second, [])
        self.assertEqual(state.count_emitted(CheckKind.BADGE_ACQUIRED), 1)

    def test_badge_emit_dedups_against_switch_bitfield_diff(self):
        """If the course clear emits a badge and the Switch-side bitfield
        diff later sends a BadgeAcquiredMsg for the same internal_id,
        the second emit must be deduped -- both keyed by (BADGE_ACQUIRED,
        internal_id, sub_key=0)."""
        state = BridgeState()
        _handle_course_result(
            state, self._course_result_fields(_STAGE_SPRING_FEET_I))
        emitted = process_event(state, BadgeAcquiredMsg(internal_id=4))
        self.assertEqual(emitted, [])
        self.assertEqual(state.count_emitted(CheckKind.BADGE_ACQUIRED), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
