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
        if course.stage_key in _HUB_HOUSE_STAGE_KEYS:
            # Hub-house wonder seeds only award once per save -- replay
            # after disconnect can't re-fire this nerve.  Suppress here
            # and let _handle_course_result emit the AP check off the
            # exit event instead.
            log.info(
                "wonder_seed_awarded fire #%d inside hub-house "
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
        goal_id == 0 + touch_goal_top      → Top of Flag
        goal_id == 0 + !touch_goal_top     → Normal Exit
        goal_id == 1                       → Secret Exit
        goal_id == 2                       → Fake Exit (guessed)

    A Top of Flag clear is a strict superset of a Normal Exit, so when
    ``goal_id == 0`` and ``touch_goal_top`` we emit BOTH TOP_OF_FLAG
    and NORMAL_EXIT — the apworld has separate AP locations for each
    and players should get credit for both on the same clear.

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

    if fields.get("world_mother_seed"):
        # Palace WIN's companion course_result.  Suppress — the
        # koopajr_result event handles it.
        log.debug("course_result with world_mother_seed=True; deferring to koopajr_result")
        return []

    emitted: list[CheckEmitted] = []

    goal_id = fields.get("goal_id", 0)
    top = bool(fields.get("touch_goal_top_result", False))
    stage_key = stage_info["stage_key"]
    if stage_key in _HUB_HOUSE_STAGE_KEYS:
        # Hub-house exits route to the WONDER_SEED AP location.  The
        # in-game wonder-seed nerve fires once-per-save (suppressed in
        # _handle_nerve_fire); the exit event re-fires on every visit,
        # so a disconnected player can retry by re-entering the house.
        kinds = [CheckKind.WONDER_SEED]
    elif goal_id == 0:
        kinds = [CheckKind.NORMAL_EXIT]
        if top:
            kinds.append(CheckKind.TOP_OF_FLAG)
    elif goal_id == 1:
        kinds = [CheckKind.SECRET_EXIT]
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


# item_kind values observed in general_shop_result.item_info_array
# (corpus 2026-05-25, captured across W1 / PI / W2 / W3 / W4 shops):
#
#   0 = badge       (item_value = badge internal_id, e.g. 8=Fast Dash,
#                    55=Coin Reward).  NOT emitted here — the Switch-side
#                    ``probe::setBadgeBitfieldAbsolute`` already does
#                    read-diff-then-write (main.cpp ~L1142:
#                    "M2.3 -- diff-on-overwrite") so any badge bit set
#                    in-game produces a BadgeAcquiredMsg on the next
#                    ~2 s tick.  That's the canonical path for the
#                    "<Badge> Obtained" AP location family; emitting
#                    the same check here would be redundant.
#   1 = Wonder Seed (item_value = per-shop slot index; 0 for single-seed
#                    shops, 0/1/2 for the W4 Secret 3-slot shop in
#                    cheapest-first shelf order).  Fires SHOP_SEED.
#   2 = consumable  (1-up; item_value = count).  Not an AP location family;
#                    silently dropped.
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
        ==0 (badge) and ==2 (consumable) → silently dropped.  Badges
            are covered by the Switch-side bitfield-diff path
            (BadgeAcquiredMsg via probe::setBadgeBitfieldAbsolute);
            consumables aren't an AP location family.

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

        if kind != _SHOP_ITEM_KIND_WONDER_SEED:
            # item_kind=0 (badge) — covered by the Switch-side
            # bitfield-diff path, see module-level comment.
            # item_kind=2 (consumable) — not an AP family.
            # Any other kind — log and drop until we have data.
            log.debug(
                "general_shop_result: dropping kind=%r value=%r "
                "at (world=%d, npc=%d)", kind, value, world_no, npc_id)
            continue
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
