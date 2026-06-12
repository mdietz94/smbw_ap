"""Wire-format definitions for the Switch ↔ Bridge channel.

Modeled on smo_archipelago/apworld/smo_archipelago/client/protocol.py.

The Switch mod will eventually stream one JSON line per event over a
persistent TCP connection (M4). For M2.6 we just need the in-memory
dataclasses — the bridge processor consumes them, the TCP server (M4)
will deserialize them from the wire.

Two event sources:

1. **Nerve fires** — direct signals from the M1 hooks. The Switch sends
   one of these whenever a tracked Nerve activates. Currently:
       - WONDER_SEED_AWARDED  (mid-course Wonder Phase seed grab)
       - COURSE_CLEARED       (every successful flag-touch / boss clear)
       - DEATH_DETECTED       (M3.8, TBD — currently planned)

2. **PlayReport captures** — bytes captured by the SetEventId + IPC
   SaveReport hooks (M2.4). The Switch sends the room name and the
   already-serialized payload; the bridge decodes via play_report.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class NerveKind(str, Enum):
    """Classes of M1 nerve fire the Switch can send.

    Wire form is the lowercase string ("wonder_seed", etc.) so the
    enum's `.value` is the JSON-friendly tag.
    """

    WONDER_SEED_AWARDED = "wonder_seed_awarded"
    COURSE_CLEARED = "course_cleared"
    DEATH_DETECTED = "death_detected"
    # M3.7 -- the SetFlagEndDispMsgFirstVisitedWorldAfterClearedLastBoss
    # Nerve fires exactly once per save the first time the player defeats
    # final Bowser.  Bridge translates this to AP ClientStatus.CLIENT_GOAL.
    GAME_GOAL_REACHED = "game_goal_reached"


@dataclass(frozen=True)
class NerveFireMsg:
    """A tracked Nerve activated on the Switch."""

    kind: NerveKind
    # Monotonic per-source counter (the M1 logs already prefix "fire #N").
    # Useful for sequence reconciliation if a TCP reconnect drops events.
    seq: int = 0


@dataclass(frozen=True)
class BadgeAcquiredMsg:
    """An in-game badge pickup the Switch detected via diff-on-overwrite.

    Distinct from :mod:`apworld.smbw_archipelago.client.wire`'s
    ``BadgeAcquiredWireMsg`` so the processor's input shape can evolve
    independently of the wire format.  ``internal_id`` is the bit
    position in the container-C owned-badge bitfield, which is itself
    the SMBW internal badge id.
    """

    internal_id: int
    seq: int = 0


@dataclass(frozen=True)
class CharBlockHitMsg:
    """A player-specific ``ObjectBlockClarityCharacter`` block was bumped
    on the Switch (character-block sanity).

    Emitted by the GetDamageReactionPlayerNo hook (NSO +0x168d428) every
    time that AI-node body resolves the damage invoker to a local player
    slot 0-3.  The node is SHARED by many block AIs (regular ? blocks
    etc.), so this event over-fires: the bridge filters it down to a real
    AP check by intersecting ``(current course, hitting character)``
    against the offline char-block table -- only (course, charaType) pairs
    that actually have a placed character block resolve to a location.

    ``player_slot`` is the local-player index (0-3) the node body returned;
    ``pos`` is the block world position when the Switch could read it
    (``[0,0,0]`` in v1 -- the shared node body doesn't expose the owning
    actor's transform cleanly, so position is reserved for the future
    multi-block-per-course disambiguation pass).  ``chara`` is the hitting
    player's PlayerCharaType (0-11) if the Switch resolved it, else -1 (the
    bridge then falls back to the current course's character from the
    ``chara_type_array`` PlayReport field).
    """

    player_slot: int
    chara: int = -1
    pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    seq: int = 0


@dataclass(frozen=True)
class PlayReportMsg:
    """A PlayReport was committed to the prepo IPC layer on the Switch.

    `room` is the event id captured by the SetEventId hook (one of
    `world_activity`, `world_result`, `course_in`, `course_result`,
    `koopajr_result`, etc.).  `payload` is the already-serialized
    Nintendo CBOR-ish byte stream captured by the IPC hook — the bridge
    decodes via `play_report.decode_play_report()`.
    """

    room: str
    payload: bytes


# ---------------------------------------------------------------------------
# Outgoing from the bridge (what we'd send to AP).
#
# Modeled on SMO's CheckEvent / ItemEvent — but at this M2.6 stage the
# bridge isn't connected to AP yet, so these are "what we'd send if we
# could".  The processor emits them as a side-effect of consuming events,
# and tests assert on their shape.


class CheckKind(str, Enum):
    """The AP location families this bridge classifies events into."""

    TOP_OF_FLAG = "top_of_flag"          # 89 AP checks: emitted whenever
                                         # touch_goal_top_result is True,
                                         # regardless of which flagpole
                                         # (normal or secret) was topped.
                                         # One AP location per course.
    NORMAL_EXIT = "normal_exit"          # 96 AP checks
    SECRET_EXIT = "secret_exit"          # 9 AP checks
    FAKE_EXIT = "fake_exit"              # 5 AP checks (goal_id=2 guessed)
    PALACE_CLEAR = "palace_clear"        # 7 AP checks (Royal Seed)
    WONDER_SEED = "wonder_seed"          # 124 AP checks (mid-course)
    TEN_COIN = "ten_coin"                # 306 AP checks (102 courses × 3)
                                         # CheckEmitted.metadata["coin_index"]
                                         # is 0/1/2 — see docs/m2.2-runbook.md.
    BADGE_ACQUIRED = "badge_acquired"    # 10 AP checks (shop badges only;
                                         # course-granted badges are items
                                         # but not checks) — stage_key holds
                                         # the badge bit position
                                         # (== SMBW internal_id).
    CHARACTER_BLOCK = "character_block"  # ~154 AP checks (one per
                                         # (course, charaType) hidden
                                         # ObjectBlockClarityCharacter
                                         # block).  stage_key holds the
                                         # course stage_key; the character
                                         # discriminator lives in
                                         # CheckEmitted.metadata["chara"]
                                         # (0-11) and is the per-course
                                         # dedup sub_key.  Gated on the
                                         # character_block_sanity slot_data
                                         # toggle.
    SHOP_SEED = "shop_seed"              # ≤18 AP checks (Poplin Shops +
                                         # Poplin Houses).  stage_key encodes
                                         # the shop identity as
                                         # ``(world_no << 16) | npc_id``;
                                         # CheckEmitted.metadata["shop_slot"]
                                         # is the PlayReport's ``item_value``
                                         # so a multi-slot shop (W4 Secret's
                                         # 3 seeds) dedups its slots
                                         # independently.  Fired from the
                                         # ``general_shop_result`` PlayReport
                                         # only when ``item_kind == 1``
                                         # (Wonder Seed).  See location_table
                                         # for the (world_no, npc_id,
                                         # item_value) -> name map.


@dataclass(frozen=True)
class GoalCompleted:
    """M3.7 game-completion -- the SetFlagEnd...AfterClearedLastBoss Nerve
    fired on the Switch, meaning Mario just defeated final Bowser for the
    first time on this save.

    The processor emits this exactly once per AP session (deduplicated
    via :meth:`BridgeState.mark_goal_complete`); the LanServer routes
    it to ``handle_goal_completed`` in the AP layer, which sends a
    StatusUpdate with ``ClientStatus.CLIENT_GOAL`` so the multiworld
    marks this slot done.

    ``seq`` is the Switch-side fire counter -- useful for diagnostics.
    """

    seq: int = 0


@dataclass(frozen=True)
class DeathReported:
    """M3.8 DeathLink outbound -- a Switch ``DEATH_DETECTED`` nerve fire
    that survived the discriminator.

    The processor emits this; the LanServer routes it to the AP layer's
    ``handle_death_reported`` callback, which (when the slot is
    DeathLink-tagged) sends a ``Bounce`` packet to the AP server.

    ``seq`` is the Switch-side fire counter -- useful for diagnostics
    when correlating bridge logs against Ryujinx logs.  Not used for
    dedup: every detected death produces an emit; the AP server is the
    one authority on how multiple bounces interact.
    """

    seq: int = 0


class GateKind(str, Enum):
    """Which AP-state requirement a gated course entry is checked against.

    Used by :class:`GateEntered`.  ``BADGE`` means "the player must own
    the AP-granted badge whose container-C internal_id is
    :attr:`GateEntered.requirement`"; ``ROYAL_SEEDS`` means "the player
    must hold at least :attr:`GateEntered.requirement` AP-granted Royal
    Seeds" (the final-Bowser gate).
    """

    BADGE = "badge"
    ROYAL_SEEDS = "royal_seeds"


@dataclass(frozen=True)
class GateEntered:
    """The player entered a course that AP logic gates behind an item the
    Switch can't physically block entry to (the Switch-side pre-commit
    gate is a documented dead end -- see
    ``docs/gate-entry-session3-handoff.md``).

    The processor emits this from ``_handle_course_in`` whenever the
    entered stage is one of the gated courses (a badge-granting course,
    or the final Bowser stage).  The LanServer routes it to
    ``SMBWContext.handle_gate_entered``, which -- if the player does NOT
    satisfy ``gate_kind``/``requirement`` -- arms a delayed-kill loop:
    after ~10 s (enough grace to pause and leave) it sends a
    ``KillMsg`` via the DeathLink ``synthKill`` route, re-arming every
    ~10 s while the player is still inside the gated course without the
    item.

    ``requirement`` is overloaded by ``gate_kind``: the badge
    container-C internal_id for ``BADGE``, or the required Royal-Seed
    count for ``ROYAL_SEEDS``.
    """

    stage_key: int
    gate_kind: GateKind
    requirement: int
    world_no: int = 0
    course_no: int = 0


@dataclass(frozen=True)
class CheckEmitted:
    """A bridge-side classification of an in-game event into an AP
    location-family + per-course identifier.

    The bridge processor emits these in response to incoming events; a
    later layer (M5 apworld glue) translates `(kind, stage_key)` → the
    canonical AP location name.

    Fields:
        kind:       which AP family (see CheckKind)
        stage_key:  the per-kind identifier.  For course-clear / wonder-
                     seed / ten-coin kinds this is the unique 32-bit
                     ``stage_info.stage_key`` (M2.4).  For
                     ``BADGE_ACQUIRED`` (M2.3) this is the badge bit
                     position == SMBW internal_id.  The state dedup key
                     ``(kind, stage_key, sub_key)`` works uniformly
                     because no two kinds share a numeric namespace.
        metadata:   free-form dict for extra context (course_no,
                     world_no, total_get_finish_seed_count, etc.) the
                     downstream consumer may want for diagnostics
    """

    kind: CheckKind
    stage_key: int
    metadata: dict[str, Any] = field(default_factory=dict)
