"""Bridge event processor.

Modeled on smo_archipelago's separation of state / protocol / processing.
Pure logic — no threading, no I/O.  Takes events (NerveFireMsg or
PlayReportMsg) and a BridgeState; mutates state and returns a list of
CheckEmitted produced by the event.

This is the M2.6 piece: course correlation via the `course_in`
PlayReport sets current_course; subsequent WONDER_SEED_AWARDED nerve
fires get attributed to it.

Also encodes the M2.5 exit-type discriminator table for course_result /
koopajr_result events (settled in the 2026-05-20 corpus).
"""

from __future__ import annotations

import logging
from typing import Any

from . import play_report
from .protocol import (
    BadgeAcquiredMsg,
    CheckEmitted,
    CheckKind,
    DeathReported,
    GoalCompleted,
    NerveFireMsg,
    NerveKind,
    PlayReportMsg,
)
from .state import BridgeState, CurrentCourse


# Anything the processor emits as a side-effect of consuming an event.
# CheckEmitted -> AP LocationChecks; DeathReported -> AP DeathLink Bounce;
# GoalCompleted -> AP StatusUpdate(CLIENT_GOAL).
ProcessorEmit = CheckEmitted | DeathReported | GoalCompleted


log = logging.getLogger("SMBW")


# Hub-house stage_keys: small interior "houses" whose only AP check is
# the Wonder Seed handed out inside (no Normal/Secret/Top-of-Flag in
# locations.json).  We route their AP check off the exit event rather
# than WONDER_SEED_AWARDED so a player disconnected from AP at pickup
# can re-fire it by re-entering and re-exiting -- the in-game seed
# only awards once per save, but course_result fires every exit.
# Stage keys mirror location_table.py constants.
_HUB_HOUSE_STAGE_KEYS: frozenset[int] = frozenset({
    0x3D3FE9D4,  # PI: Angler Poplin's House
    0x4A493BE1,  # W3: Master Poplin's House
    0x9D67C898,  # W4: Sunbaked Desert House
    0x2A876538,  # W5: Loyal Poplin's House
})


# Break Time! stage_keys: short, flagpole-less bonus courses whose only
# AP location is the Wonder Seed handed out at the end (no
# Normal/Secret/Top-of-Flag/10-Coin entries in locations.json).
# Mechanically identical to hub houses from the bridge's POV -- the
# game emits a regular ``course_result`` (course_result=1, goal_id=0,
# touch_goal_top_result=False) on clear, which would otherwise misroute
# to NORMAL_EXIT and silently drop because no such AP location exists
# for these stages.  Live-confirmed 2026-05-27 with W4 Treasure Vault
# (stage_key=0xF9B39322); see BREAK_TIME_COURSE_RESULT fixture in
# test_play_report.py.
#
# Tumble House (W5) is included here rather than in hub houses because
# the existing hub-house set predates this fix and we want a single
# code path for Break Time-shaped clears; functionally equivalent.
#
# Set membership derived from the location_table: every stage that has
# a WONDER_SEED entry and no NORMAL_EXIT / TOP_OF_FLAG / SECRET_EXIT /
# PALACE_CLEAR / FAKE_EXIT entry, minus the four named hub houses.
_BREAK_TIME_STAGE_KEYS: frozenset[int] = frozenset({
    0x2030D011,  # W1: Hurry, Hurry
    0x77B85684,  # W1: Wonder Token Tunes
    0x333F1388,  # W1: Pop Up, Hoppo!
    0x25CD59CE,  # W2: Puzzling Park
    0x093F62A1,  # W2: Kick It, Outmaway
    0x5380FCA8,  # W2: Fluff-Puff Peaks Cabin
    0x0B2722F6,  # W2: Cloud Cover
    0xDF2CCAC3,  # W2: Zip-Go-Round
    0x103647C3,  # W3: An Empty Park?
    0xDCD9A44A,  # W3: Unreachable Treasure?
    0xC21517B6,  # W3: Watery Wonder Tokens
    0x647768F3,  # W3: Timer-Switch Climb
    0x53835ED1,  # W3: Timer-Switch Dash
    0xEFDA9369,  # W4: Pipe Park
    0xF9B39322,  # W4: Treasure Vault
    0x54710BE6,  # W4: Raise the Stage
    0xF6633FB9,  # W4: Revver Run
    0x9813AE4B,  # W4: Floating Wonder Tokens
    0xA1C1F90F,  # W4: Bouncy Tunes
    0x813617AB,  # W4: Lights Out
    0x36ED2775,  # W5: Tumble House
    0x128C6CC8,  # W5: Trottin' Piranha Plants
    0x3B24E83C,  # W6: Observatory #1
    0x333AB7F4,  # W6: Item Park
    0x67BE7C0E,  # W6: Hot-Hot Rocks
    0x3217F3D0,  # W6: Observatory #2
    0x46D083C8,  # W6: Observatory #3
    0x86D3E8CB,  # W6: Observatory #4
    0x13F9E775,  # PI: Spelunking!
})


# stage_key -> SMBW internal badge id (== container-C owned-bitfield
# bit position).  When the player clears one of these courses we emit
# a BADGE_ACQUIRED check in addition to the normal exit-type checks --
# this is a robustness layer on top of the Switch-side bitfield diff-
# on-overwrite detector in main.cpp, which we've seen miss pickups in
# practice.  Internal_ids come from badge_table._BADGES; stage_keys
# from location_table._STAGE_*.
#
# Coverage rationale (sources: mariowiki.com per-badge entries):
#   - Badge Challenge "I" courses hand the player the badge on first
#     clear; the "II" courses are harder follow-ups that don't normally
#     re-grant, but we include them so a player who somehow gets the II
#     first (or the apworld randomizer routes them oddly) still gets
#     credit.  Duplicate emits dedup at BridgeState.emit_check.
#   - Badge House in Pipe-Rock Plateau gives Parachute Cap; the house
#     fires course_result on completion like a normal course.
#   - Mountaineering! (Wiggler Race) gives Auto Super Mushroom on win.
#   - Ninji Jump Party gives Rhythm Jump on clear.
#   - Upshroom Downshroom: the Sensor badge is handed over mid-course
#     by the bridge-repair Poplin near the start.  Emit on clear so a
#     player who entered, got the badge, then completed the course gets
#     AP credit; replaying the course will re-fire and the AP server
#     dedups by location-already-checked.
#   - WONDER? (Special): the Sound Off? badge is handed over by the
#     talking flower inside this "course" (AP treats it as a Normal
#     Exit).  Emit on clear.
_STAGE_TO_BADGE_INTERNAL_ID: dict[int, int] = {
    # Action badges via Badge Challenge courses.
    0xDADED63E: 34,  # W1: Wall-Climb Jump I  -> Wall-Climb Jump
    0x283CE2B9: 34,  # W2: Wall-Climb Jump II -> Wall-Climb Jump
    0x1922DA45: 19,  # W1: Jet Run I          -> Jet Run
    0x69B9F9E6: 19,  # W6: Jet Run II         -> Jet Run
    0x83774434: 0,   # W2: Floating High Jump I  -> Floating High Jump
    0xB824B96F: 0,   # W6: Floating High Jump II -> Floating High Jump
    0xF421DB55: 4,   # W2: Spring Feet I  -> Spring Feet
    0xAE4E8917: 4,   # W6: Spring Feet II -> Spring Feet
    0x4B4B3D59: 29,  # PI: Dolphin Kick I  -> Dolphin Kick
    0xB8C524FB: 29,  # PI: Dolphin Kick II -> Dolphin Kick
    0xABE6B13D: 14,  # PI: Boosting Spin Jump I  -> Boosting Spin Jump
    0xC8047777: 14,  # W6: Boosting Spin Jump II -> Boosting Spin Jump
    0xA56D29C2: 39,  # W3: Crouching High Jump I  -> Crouching High Jump
    0x8CFF55FD: 39,  # W4: Crouching High Jump II -> Crouching High Jump
    0xC2CCC664: 53,  # W5: Grappling Vine I  -> Grappling Vine
    0x51FC5E9E: 53,  # W6: Grappling Vine II -> Grappling Vine
    0x715BCAE8: 38,  # W4: Invisibility I  -> Invisibility
    0xF065407A: 38,  # W6: Invisibility II -> Invisibility

    # Boost / story badges via non-challenge courses.
    0x954EB962: 46,  # W1: Mountaineering! (Wiggler Race) -> Auto Super Mushroom
    0x5524F03C: 47,  # W4: Ninji Jump Party -> Rhythm Jump

    # Parachute Cap -- the Pipe-Rock Plateau Badge House hands the
    # badge over as the player passes through.
    0xA3207D45: 35,  # W1: Badge House in Pipe-Rock Plateau -> Parachute Cap

    # Sensor -- handed over mid-course at the start of Upshroom
    # Downshroom by the bridge-repair Poplin.  Story trigger, not a
    # true challenge reward, but emit on clear so the AP check fires.
    0x54A60980: 32,  # W5: Upshroom Downshroom -> Sensor

    # Sound Off? -- the post-game cosmetic, awarded by the talking
    # flower inside the WONDER? stage.
    0x2D438F37: 33,  # Special: WONDER? -> Sound Off?
}


# Palace stage_keys (one per Royal Seed location).  Used by
# ``_handle_course_result`` to suppress the misleading default-fields
# course_result that palace WINs emit alongside their koopajr_result --
# previous code gated on the ``world_mother_seed`` PlayReport field, but
# that flag is "player owns the Royal Seed for this world" (a persistent
# state), NOT "this event is a palace clear".  Live-reproduced 2026-05-27
# in user run 10-08-58: W1 course 3 ("Bulrush Coming Through!") emitted
# course_result with world_mother_seed=True simply because the player had
# already cleared the W1 palace earlier, causing every subsequent W1
# course clear to be silently dropped.
#
# Stage-key allowlist is the right discriminator: it identifies the
# course directly, not the player's progression state.  Mirrors the seven
# entries in :mod:`.location_table` that map to ``CheckKind.PALACE_CLEAR``
# (W1/W2 palaces, W3 mansion, W4/W5/W6 palaces, PI Bowser's Rage Stage).
#
# Edge case -- W3 mansion + W5 Operation Poplin Rescue ALSO have
# ``SECRET_EXIT`` locations.  The suppression below is gated on
# ``goal_id == 0`` so a secret-exit clear (goal_id == 1) at those stages
# still fires the SECRET_EXIT branch normally.
_PALACE_STAGE_KEYS: frozenset[int] = frozenset({
    0x89927C97,  # W1: Pipe-Rock Plateau Palace
    0xB2B07454,  # W2: Fluff-Puff Peaks Palace
    0xA5E2BB3A,  # W3: Royal Seed Mansion
    0x1969941E,  # W4: Sunbaked Desert Palace
    0x87E6D263,  # W5: Operation Poplin Rescue
    0x7E523816,  # W6: Deep Magma Bog Palace
    0x6895BF00,  # PI: Bowser's Rage Stage
})


# ---------------------------------------------------------------------------
# Top-level dispatch.

def process_event(state: BridgeState, event: Any) -> list[ProcessorEmit]:
    """Route an inbound event to its handler.  Returns the list of
    emits produced (empty if none — the event may just have updated
    state).  Emits are heterogeneous: CheckEmitted for AP location
    checks, DeathReported for DeathLink bounces."""
    if isinstance(event, NerveFireMsg):
        return _handle_nerve_fire(state, event)
    if isinstance(event, BadgeAcquiredMsg):
        return _handle_badge_acquired(state, event)
    if isinstance(event, PlayReportMsg):
        return _handle_play_report(state, event)
    log.warning("process_event: unknown event type %r", type(event).__name__)
    return []


# ---------------------------------------------------------------------------
# Nerve handlers.

def _handle_nerve_fire(state: BridgeState, event: NerveFireMsg) -> list[ProcessorEmit]:
    emitted: list[ProcessorEmit] = []
    if event.kind == NerveKind.WONDER_SEED_AWARDED:
        # M2.6 core: attribute to the current course.
        course = state.current_course
        if course is None:
            # Player isn't in a course context — likely a noisy fire
            # or a misordered event stream.  Log and drop; don't fire
            # an AP check from a nerve we can't attribute.
            log.warning(
                "wonder_seed_awarded fire #%d with no current_course; dropping",
                event.seq)
            return []
        if (course.stage_key in _HUB_HOUSE_STAGE_KEYS
                or course.stage_key in _BREAK_TIME_STAGE_KEYS):
            # Hub-house and Break Time! wonder seeds only award once
            # per save -- replay after disconnect can't re-fire this
            # nerve.  Suppress here and let _handle_course_result emit
            # the AP check off the exit event instead (which fires on
            # every re-entry).
            log.info(
                "wonder_seed_awarded fire #%d inside seed-only "
                "stage_key=0x%08x; suppressing (routed via course_result)",
                event.seq, course.stage_key)
            return []
        check = CheckEmitted(
            kind=CheckKind.WONDER_SEED,
            stage_key=course.stage_key,
            metadata={"world_no": course.world_no, "course_no": course.course_no},
        )
        if state.emit_check(check):
            emitted.append(check)
        return emitted

    if event.kind == NerveKind.COURSE_CLEARED:
        # Just a precursor signal; the actual classification happens
        # when the course_result PlayReport arrives ~8 ms later.  We
        # don't emit a check from this alone — that would double-fire
        # with the PlayReport handler.
        log.debug("course_cleared nerve fire #%d (awaiting course_result)", event.seq)
        return []

    if event.kind == NerveKind.DEATH_DETECTED:
        # The Switch-side discriminator (Phase 2) decides what counts as
        # a real death; by the time it reaches us here, the death is
        # confirmed.  Bump the local counter for diagnostics and emit a
        # DeathReported so the AP layer can decide whether to bounce
        # (gated on the per-slot DeathLink tag).
        state.bump_death_count()
        log.info(
            "death_detected fire #%d (total deaths: %d) -> DeathReported",
            event.seq, state.death_count)
        return [DeathReported(seq=event.seq)]

    if event.kind == NerveKind.GAME_GOAL_REACHED:
        # M3.7 -- one-shot Nerve guaranteed by the engine to fire exactly
        # once per save the first time the player defeats final Bowser.
        # mark_goal_complete is dedup'd so even if the engine somehow
        # re-fires (e.g. save reload + post-Bowser cutscene replay) the
        # AP server only sees one StatusUpdate.
        if not state.mark_goal_complete():
            log.info(
                "game_goal_reached fire #%d already marked; suppressing",
                event.seq)
            return []
        log.info(
            "game_goal_reached fire #%d -> GoalCompleted", event.seq)
        return [GoalCompleted(seq=event.seq)]

    log.warning("unknown nerve kind: %r", event.kind)
    return []


# ---------------------------------------------------------------------------
# Badge handler (M2.3).

def _handle_badge_acquired(
    state: BridgeState, event: BadgeAcquiredMsg,
) -> list[ProcessorEmit]:
    """Switch detected an in-game badge pickup (Poplin shop / badge
    house / badge medley / badge challenge) — emit a CheckEmitted so the
    AP layer can fire the "<Badge> Obtained" LocationCheck.

    No course-correlation needed: badge AP locations are per-badge, not
    per-acquisition-site.  The downstream ``location_table.lookup_name``
    handles unmapped internal_ids by returning ``None`` (logs + drops);
    no AP error.  M2.3 ships with 3 mapped (Spring Feet, Coin Reward,
    Auto Super Mushroom); the remaining 21 mappings fill in incrementally
    as gameplay reveals their bit positions (see scripts/badge_map_builder.py).
    """
    check = CheckEmitted(
        kind=CheckKind.BADGE_ACQUIRED,
        stage_key=event.internal_id,
        metadata={"seq": event.seq},
    )
    if state.emit_check(check):
        log.info(
            "badge_acquired internal_id=%d seq=%d -> CheckEmitted",
            event.internal_id, event.seq)
        return [check]
    log.debug(
        "badge_acquired internal_id=%d seq=%d (dup; dropped)",
        event.internal_id, event.seq)
    return []


# ---------------------------------------------------------------------------
# PlayReport handlers — one per room name we care about.

def _handle_play_report(state: BridgeState, event: PlayReportMsg) -> list[CheckEmitted]:
    # Decode once; route on room name.
    try:
        decoded = play_report.decode_play_report(event.payload)
    except play_report.DecodeError as e:
        log.error("PlayReport %r failed to decode: %s", event.room, e)
        return []

    fields = decoded.fields
    room = event.room

    if room == "course_in":
        return _handle_course_in(state, fields)
    if room == "course_result":
        return _handle_course_result(state, fields)
    if room == "koopajr_result":
        return _handle_koopajr_result(state, fields)
    if room == "general_shop_result":
        return _handle_general_shop_result(state, fields)
    if room == "world_result":
        # The transition report; we COULD use next_stage_info to
        # pre-populate current_course but the subsequent course_in
        # already does that authoritatively.  Skip for now.
        return []
    if room == "panel_game_result":
        # Standee shop purchases (Poplin Standee).  Distinct schema with
        # result_array / get_id / get_count / panel_type_count — NOT an
        # AP check family.  Explicitly suppress to keep the warning log
        # from firing on every standee buy.
        return []
    # Other rooms (world_activity, bootup_time, erepo_*, game_option) are
    # boring telemetry from the bridge's perspective.
    log.debug("PlayReport %r ignored (not in our routing table)", room)
    return []


def _handle_course_in(state: BridgeState, fields: dict[str, Any]) -> list[CheckEmitted]:
    """course_in fires when a course actually loads.  Sets current_course
    so subsequent nerve fires (notably WONDER_SEED_AWARDED) can attribute."""
    stage_info = fields.get("stage_info")
    if not isinstance(stage_info, dict):
        log.warning("course_in missing stage_info; ignoring")
        return []
    state.set_current_course(CurrentCourse(
        stage_key=stage_info["stage_key"],
        world_no=stage_info.get("world_no", 0),
        course_no=stage_info.get("course_no", 0),
        world_kind=stage_info.get("world_kind", 0),
    ))
    # M4 location_table playtest-sweep helper: tag the line with
    # "STAGEKEY" so the operator can `Select-String STAGEKEY` against
    # the AP client log file to filter out everything but course
    # entries.  Both decimal (as PlayReport encodes it) and hex
    # (what we paste into location_table.py constants) emit side by
    # side -- see docs/playtest-stage-key-sweep.md.
    sk = stage_info["stage_key"]
    log.info("STAGEKEY  world=%d course=%d  stage_key=%d (0x%08X)",
             stage_info.get("world_no", 0),
             stage_info.get("course_no", 0),
             sk, sk & 0xFFFFFFFF)
    return []


def _handle_course_result(state: BridgeState, fields: dict[str, Any]) -> list[CheckEmitted]:
    """course_result fires on every successful flagpole/boss clear,
    INCLUDING palace WINs which also fire a concurrent koopajr_result.
    It ALSO fires when the player aborts mid-course via the pause-menu
    "quit" -- discriminated by the ``course_result`` field (1 = cleared,
    3 = quit-from-pause-menu observed live 2026-05-26 on Robbird Cove).

    M2.5 classification logic (empirically derived from the 2026-05-20
    corpus):

        course_result != 1                 → not a clear (quit / abort);
                                             emit nothing
        world_mother_seed == True          → palace clear (defer to
                                             koopajr_result; emit nothing
                                             here to avoid double-firing)
        goal_id == 0 + touch_goal_top      → Top of Flag + Normal Exit
        goal_id == 0 + !touch_goal_top     → Normal Exit
        goal_id == 1 + touch_goal_top      → Top of Secret Flag + Secret Exit
        goal_id == 1 + !touch_goal_top     → Secret Exit
        goal_id == 2                       → Fake Exit (guessed)

    A Top of Flag clear is a strict superset of a Normal Exit, so when
    ``goal_id == 0`` and ``touch_goal_top`` we emit BOTH TOP_OF_FLAG
    and NORMAL_EXIT — the apworld has separate AP locations for each
    and players should get credit for both on the same clear.  The
    same applies to the secret flagpole: TOP_OF_SECRET_FLAG is
    emitted alongside SECRET_EXIT when the secret flag is topped.

    M2.2 10-coin layer (added 2026-05-25): after the exit-type emit,
    diff ``big_flower_coin_course_in`` against ``_out`` and emit one
    TEN_COIN per newly-True index.  Suppressed on palace courses (the
    early-return above keeps us out of this path) and on rooms where
    the field is absent (palace-shaped fixtures have it `[F,F,F]/[F,F,F]`
    so the diff naturally yields zero emits anyway).  See
    docs/m2.2-runbook.md for the field semantics + the unproven
    diff-interpretation caveat.
    """
    stage_info = fields.get("stage_info")
    if not isinstance(stage_info, dict):
        log.warning("course_result missing stage_info; ignoring")
        return []

    result_code = fields.get("course_result")
    if result_code != 1:
        # The player quit / aborted mid-course (or some other non-clear
        # outcome).  Without this guard, the default goal_id=0 +
        # touch_goal_top_result=False fields would misclassify the quit
        # as a Normal Exit and fire the corresponding AP location.  Live
        # repro 2026-05-26: a 1-second Robbird Cove pause-menu quit
        # emitted course_result=3, which previously sent the W2 Wonder
        # Seed back to its slot.
        log.info(
            "course_result=%r at stage_key=%d (not a clear); suppressing",
            result_code, stage_info["stage_key"])
        return []

    emitted: list[CheckEmitted] = []

    goal_id = fields.get("goal_id", 0)
    top = bool(fields.get("touch_goal_top_result", False))
    stage_key = stage_info["stage_key"]

    # Palace WIN duplicate-fire suppression.  Palace clears emit BOTH
    # course_result AND koopajr_result ~1 ms apart for the same event;
    # _handle_koopajr_result is the authoritative palace-clear handler
    # (emits PALACE_CLEAR), and this course_result would otherwise
    # double-fire as NORMAL_EXIT because palace course_result reports
    # goal_id=0 + touch_goal_top_result=False (the AAPCS-default zeroed
    # fields when the palace clear path doesn't set them).  Gated on
    # goal_id == 0 so a SECRET_EXIT at W3 Royal Seed Mansion or W5
    # Operation Poplin Rescue (goal_id == 1) still fires normally.
    # See _PALACE_STAGE_KEYS docstring for the history of the previous
    # world_mother_seed-based gate, which was a false positive.
    if stage_key in _PALACE_STAGE_KEYS and goal_id == 0:
        log.debug(
            "course_result at palace stage_key=0x%08x goal_id=0; "
            "deferring to koopajr_result",
            stage_key)
        return []
    if (stage_key in _HUB_HOUSE_STAGE_KEYS
            or stage_key in _BREAK_TIME_STAGE_KEYS):
        # Hub-house and Break Time! exits route to the WONDER_SEED AP
        # location -- these stages have no flagpole and the seed IS
        # the goal of the course, so the location_table only registers
        # WONDER_SEED for them.  The in-game wonder-seed nerve fires
        # once-per-save (suppressed in _handle_nerve_fire); the exit
        # event re-fires on every visit, so a disconnected player can
        # retry by re-entering the course.
        kinds = [CheckKind.WONDER_SEED]
    elif goal_id == 0:
        kinds = [CheckKind.NORMAL_EXIT]
        if top:
            kinds.append(CheckKind.TOP_OF_FLAG)
    elif goal_id == 1:
        kinds = [CheckKind.SECRET_EXIT]
        if top:
            kinds.append(CheckKind.TOP_OF_SECRET_FLAG)
    elif goal_id == 2:
        kinds = [CheckKind.FAKE_EXIT]
    else:
        log.warning("course_result unknown goal_id=%r at stage_key=%d",
                    goal_id, stage_key)
        kinds = []

    for kind in kinds:
        clear = CheckEmitted(
            kind=kind,
            stage_key=stage_info["stage_key"],
            metadata={
                "world_no": stage_info.get("world_no", 0),
                "course_no": stage_info.get("course_no", 0),
                "goal_id": goal_id,
                "touch_goal_top": top,
                "got_finish_seed": bool(fields.get("total_get_finish_seed_count", 0)),
            },
        )
        if state.emit_check(clear):
            log.info("course_result → %s at stage_key=%d", kind.value, clear.stage_key)
            emitted.append(clear)

    emitted.extend(_emit_ten_coin_checks(state, stage_info, fields))
    emitted.extend(_emit_course_clear_badge(state, stage_info))
    return emitted


def _emit_course_clear_badge(
    state: BridgeState,
    stage_info: dict[str, Any],
) -> list[CheckEmitted]:
    """Emit a BADGE_ACQUIRED check if the cleared course is one that
    awards a badge in-game (badge challenge, badge house, story trigger).

    This is a robustness layer parallel to the Switch-side container-C
    bitfield diff-on-overwrite detector: in-game badge pickups SHOULD
    be detected by that path, but it relies on the AP-authoritative
    overwrite tick firing while the bit is still set live, which races
    with scene transitions and has been observed to miss in practice.
    Emitting on course clear is reliable -- the course_result PlayReport
    is queued by the IPC layer and reaches the bridge regardless of
    scene state.  Dedup at BridgeState.emit_check means a re-clear of
    the same course is a no-op rather than a double-fire.
    """
    badge_id = _STAGE_TO_BADGE_INTERNAL_ID.get(stage_info["stage_key"])
    if badge_id is None:
        return []
    check = CheckEmitted(
        kind=CheckKind.BADGE_ACQUIRED,
        stage_key=badge_id,
        metadata={
            "source": "course_clear",
            "course_stage_key": stage_info["stage_key"],
            "world_no": stage_info.get("world_no", 0),
            "course_no": stage_info.get("course_no", 0),
        },
    )
    if state.emit_check(check):
        log.info(
            "course_result → badge_acquired internal_id=%d (course stage_key=%d)",
            badge_id, stage_info["stage_key"])
        return [check]
    return []


def _emit_ten_coin_checks(
    state: BridgeState,
    stage_info: dict[str, Any],
    fields: dict[str, Any],
) -> list[CheckEmitted]:
    """Diff ``big_flower_coin_course_in`` vs ``_out`` and emit a
    TEN_COIN ``CheckEmitted`` per newly-True index.  No-op if either
    array is missing/non-list or no index flipped False → True."""
    in_arr = fields.get("big_flower_coin_course_in")
    out_arr = fields.get("big_flower_coin_course_out")
    if not isinstance(in_arr, list) or not isinstance(out_arr, list):
        return []

    emitted: list[CheckEmitted] = []
    stage_key = stage_info["stage_key"]
    for idx in range(min(len(in_arr), len(out_arr))):
        if bool(out_arr[idx]) and not bool(in_arr[idx]):
            check = CheckEmitted(
                kind=CheckKind.TEN_COIN,
                stage_key=stage_key,
                metadata={
                    "world_no": stage_info.get("world_no", 0),
                    "course_no": stage_info.get("course_no", 0),
                    "coin_index": idx,
                },
            )
            if state.emit_check(check):
                log.info("course_result → ten_coin #%d at stage_key=%d",
                         idx + 1, stage_key)
                emitted.append(check)
    return emitted


# item_kind values observed in general_shop_result.item_info_array
# (corpus 2026-05-25, captured across W1 / PI / W2 / W3 / W4 shops):
#
#   0 = badge       (item_value = badge internal_id, e.g. 8=Fast Dash,
#                    55=Coin Reward).  Fires BADGE_ACQUIRED.  This is a
#                    parallel path to the Switch-side container-C
#                    bitfield diff-on-overwrite detector in main.cpp
#                    (``probe::setBadgeBitfieldAbsolute`` "M2.3 -- diff-
#                    on-overwrite"); both can fire for the same buy and
#                    BridgeState.emit_check dedups by (kind, stage_key=
#                    internal_id) so only one AP LocationCheck goes out.
#                    The shop-result path is more reliable in practice
#                    because it doesn't race the 2 s overwrite tick or
#                    the scene-transition gate -- the PlayReport is
#                    queued by the IPC layer and reaches the bridge
#                    regardless of scene state.
#   1 = Wonder Seed (item_value = per-shop slot index; 0 for single-seed
#                    shops, 0/1/2 for the W4 Secret 3-slot shop in
#                    cheapest-first shelf order).  Fires SHOP_SEED.
#   2 = consumable  (1-up; item_value = count).  Not an AP location family;
#                    silently dropped.
_SHOP_ITEM_KIND_BADGE: int = 0
_SHOP_ITEM_KIND_WONDER_SEED: int = 1


def _shop_key(world_no: int, npc_id: int) -> int:
    """Pack a Poplin Shop's identity into a single int for use as the
    SHOP_SEED ``CheckEmitted.stage_key``.  Reversible: high 16 bits are
    world_no, low 16 bits are npc_id."""
    return (int(world_no) << 16) | int(npc_id)


def _handle_general_shop_result(
    state: BridgeState, fields: dict[str, Any],
) -> list[CheckEmitted]:
    """Poplin Shop purchase — fires once per buy at the moment of purchase.

    Schema (from the W1 Poplin Shop corpus, 2026-05-25):

        stage_info.world_no  → which world the shop sits on
        npc_id               → which Poplin NPC inside that world (the
                                shop identity is the (world_no, npc_id) pair)
        item_info_array      → list of {item_kind, item_value} structs;
                                typically length-1 since the player buys
                                one item at a time

    item_kind discriminates within a single transaction:
        ==1 (Wonder Seed) → SHOP_SEED check.  Shop identity is
            (world_no, npc_id); slot within the shop is item_value
            (0 for single-seed shops; 0/1/2 for the W4 Secret triple in
            cheapest-first shelf order).  Dedup sub_key is shop_slot
            so a multi-slot shop's seeds dedup independently.
        ==0 (badge) → BADGE_ACQUIRED check.  item_value is the SMBW
            internal badge id (== container-C bitfield bit position ==
            badge_table._BADGES[*][1]).  Parallel to the Switch-side
            bitfield-diff path; both can fire and the BridgeState
            dedup by (kind, stage_key=internal_id) collapses them.
        ==2 (consumable) → silently dropped (not an AP location family).

    item_info_array can carry multiple items per transaction (a multi-buy
    where the player nets two seeds in one purchase, for example).
    Each item is processed independently against the rules above.
    """
    stage_info = fields.get("stage_info")
    if not isinstance(stage_info, dict):
        log.warning("general_shop_result missing stage_info; ignoring")
        return []
    world_no = int(stage_info.get("world_no", 0))
    npc_id_obj = fields.get("npc_id")
    if not isinstance(npc_id_obj, int):
        log.warning("general_shop_result missing npc_id; ignoring")
        return []
    npc_id = int(npc_id_obj)

    items = fields.get("item_info_array")
    if not isinstance(items, list):
        log.warning(
            "general_shop_result @ (world=%d, npc=%d) missing item_info_array",
            world_no, npc_id)
        return []

    emitted: list[CheckEmitted] = []
    shop_key = _shop_key(world_no, npc_id)
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("item_kind")
        value = item.get("item_value")
        ivalue = int(value) if isinstance(value, int) else 0

        if kind == _SHOP_ITEM_KIND_WONDER_SEED:
            check = CheckEmitted(
                kind=CheckKind.SHOP_SEED,
                stage_key=shop_key,
                metadata={
                    "world_no": world_no,
                    "npc_id": npc_id,
                    "item_value": ivalue,
                    "shop_slot": ivalue,
                },
            )
            if state.emit_check(check):
                log.info(
                    "general_shop_result → shop_seed at (world=%d, npc=%d, slot=%d)",
                    world_no, npc_id, ivalue)
                emitted.append(check)
            else:
                log.debug(
                    "general_shop_result dup at (world=%d, npc=%d, slot=%d); dropped",
                    world_no, npc_id, ivalue)
            continue

        if kind == _SHOP_ITEM_KIND_BADGE:
            check = CheckEmitted(
                kind=CheckKind.BADGE_ACQUIRED,
                stage_key=ivalue,
                metadata={
                    "source": "shop_buy",
                    "world_no": world_no,
                    "npc_id": npc_id,
                },
            )
            if state.emit_check(check):
                log.info(
                    "general_shop_result → badge_acquired internal_id=%d "
                    "at (world=%d, npc=%d)", ivalue, world_no, npc_id)
                emitted.append(check)
            else:
                log.debug(
                    "general_shop_result badge dup internal_id=%d "
                    "at (world=%d, npc=%d); dropped", ivalue, world_no, npc_id)
            continue

        # item_kind=2 (consumable) and anything unrecognized.
        log.debug(
            "general_shop_result: dropping kind=%r value=%r "
            "at (world=%d, npc=%d)", kind, value, world_no, npc_id)
    return emitted


def _handle_koopajr_result(state: BridgeState, fields: dict[str, Any]) -> list[CheckEmitted]:
    """Palace boss fight result.  Royal Seed AP check fires only on a
    WIN (battle_result == True); LOSS reports get logged but no AP
    activity."""
    stage_info = fields.get("stage_info")
    if not isinstance(stage_info, dict):
        log.warning("koopajr_result missing stage_info; ignoring")
        return []

    won = bool(fields.get("battle_result", False))
    if not won:
        log.info("koopajr_result LOSS at stage_key=%d (no AP check)",
                 stage_info["stage_key"])
        return []

    check = CheckEmitted(
        kind=CheckKind.PALACE_CLEAR,
        stage_key=stage_info["stage_key"],
        metadata={
            "world_no": stage_info.get("world_no", 0),
            "course_no": stage_info.get("course_no", 0),
            "koopajr_total_time": fields.get("koopajr_total_time", 0),
            "challenge_count": fields.get("koopajr_challenge_count", 0),
        },
    )
    if state.emit_check(check):
        log.info("koopajr_result WIN → palace_clear at stage_key=%d", check.stage_key)
        return [check]
    return []
