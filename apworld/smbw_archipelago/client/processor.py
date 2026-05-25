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
    CheckEmitted,
    CheckKind,
    DeathReported,
    NerveFireMsg,
    NerveKind,
    PlayReportMsg,
)
from .state import BridgeState, CurrentCourse


# Anything the processor emits as a side-effect of consuming an event.
# CheckEmitted -> AP LocationChecks; DeathReported -> AP DeathLink Bounce.
ProcessorEmit = CheckEmitted | DeathReported


log = logging.getLogger("SMBW")


# ---------------------------------------------------------------------------
# Top-level dispatch.

def process_event(state: BridgeState, event: Any) -> list[ProcessorEmit]:
    """Route an inbound event to its handler.  Returns the list of
    emits produced (empty if none — the event may just have updated
    state).  Emits are heterogeneous: CheckEmitted for AP location
    checks, DeathReported for DeathLink bounces."""
    if isinstance(event, NerveFireMsg):
        return _handle_nerve_fire(state, event)
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

    log.warning("unknown nerve kind: %r", event.kind)
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
    if room == "world_result":
        # The transition report; we COULD use next_stage_info to
        # pre-populate current_course but the subsequent course_in
        # already does that authoritatively.  Skip for now.
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
    log.info("course_in: now in stage_key=%d (world %d, course %d)",
             stage_info["stage_key"],
             stage_info.get("world_no", 0),
             stage_info.get("course_no", 0))
    return []


def _handle_course_result(state: BridgeState, fields: dict[str, Any]) -> list[CheckEmitted]:
    """course_result fires on every successful flagpole/boss clear,
    INCLUDING palace WINs which also fire a concurrent koopajr_result.

    M2.5 classification logic (empirically derived from the 2026-05-20
    corpus):

        world_mother_seed == True          → palace clear (defer to
                                             koopajr_result; emit nothing
                                             here to avoid double-firing)
        goal_id == 0 + touch_goal_top      → Top of Flag
        goal_id == 0 + !touch_goal_top     → Normal Exit
        goal_id == 1                       → Secret Exit
        goal_id == 2                       → Fake Exit (guessed)

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

    if fields.get("world_mother_seed"):
        # Palace WIN's companion course_result.  Suppress — the
        # koopajr_result event handles it.
        log.debug("course_result with world_mother_seed=True; deferring to koopajr_result")
        return []

    emitted: list[CheckEmitted] = []

    goal_id = fields.get("goal_id", 0)
    top = bool(fields.get("touch_goal_top_result", False))
    if goal_id == 0:
        kind = CheckKind.TOP_OF_FLAG if top else CheckKind.NORMAL_EXIT
    elif goal_id == 1:
        kind = CheckKind.SECRET_EXIT
    elif goal_id == 2:
        kind = CheckKind.FAKE_EXIT
    else:
        log.warning("course_result unknown goal_id=%r at stage_key=%d",
                    goal_id, stage_info["stage_key"])
        kind = None

    if kind is not None:
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
    return emitted


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
