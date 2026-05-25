"""Bridge-side mirror of game state.

Modeled on smo_archipelago/apworld/smo_archipelago/client/state.py.

The bridge maintains an authoritative snapshot independently of any
connection: AP can drop, the Switch can reboot, the bridge keeps the
state. Both sides resync from this snapshot on reconnect (planned for
M4 onward).

For M2.6 the state is just:
  - current_course        (set by every course_in PlayReport)
  - emitted_checks        (dedup set of (kind, stage_key) tuples that
                           already fired; processor returns False on a
                           dup so the AP-side never double-counts)
  - death_count           (M3.8 DeathLink prep)

Everything is guarded by a single RLock; the processor and (future)
TCP server can mutate it from different threads.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .protocol import CheckEmitted, CheckKind


@dataclass
class CurrentCourse:
    """Snapshot of which course the player is currently inside.

    Set by every `course_in` PlayReport — gives us a per-fire context
    for nerve events (notably WONDER_SEED_AWARDED in M2.6) that don't
    themselves carry a stage_key.

    `None` means the player is on the overworld / file-select / a
    pre-course cutscene — any nerve fire received in this state is
    NOT attributed to a specific course.
    """

    stage_key: int
    world_no: int = 0
    course_no: int = 0
    world_kind: int = 0


class BridgeState:
    """Thread-safe state snapshot."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

        # Current player position. None when not in a course.
        self.current_course: CurrentCourse | None = None

        # Every check the bridge has emitted, in arrival order. The
        # `_emitted_keys` set is the dedup index so a repeat fire (e.g.
        # a re-cleared course on a fresh save) doesn't re-fire the AP
        # check.
        self.emitted_checks: list[CheckEmitted] = []
        # Dedup key is (kind, stage_key, sub_key).  `sub_key` is 0 for
        # everything except TEN_COIN, where it's the 0/1/2 coin index
        # so that a single course can dedup its three coin checks
        # independently.
        self._emitted_keys: set[tuple[str, int, int]] = set()

        # M3.8 DeathLink groundwork — incremented on every detected
        # Mario death.  Bridge will forward to AP later.
        self.death_count: int = 0

        # M3.7 game-completion flag.  Flipped True the first time a
        # GAME_GOAL_REACHED nerve fire reaches the processor on this AP
        # session; subsequent fires (e.g. a replay after the player
        # reloads the cleared save) are deduped to one AP
        # StatusUpdate(CLIENT_GOAL).
        self.goal_complete: bool = False

    # ---- Mutators -----------------------------------------------------

    def set_current_course(self, course: CurrentCourse | None) -> None:
        """Update the active course on a course_in PlayReport. Pass None
        when entering a course-less context (overworld, etc.)."""
        with self._lock:
            self.current_course = course

    def emit_check(self, check: CheckEmitted) -> bool:
        """Record a CheckEmitted from the processor. Returns True if
        newly added, False if a duplicate of an earlier emit.

        Dedup key is (kind, stage_key, sub_key).  For most kinds
        sub_key is 0 — a single course produces at most one
        TOP_OF_FLAG / NORMAL_EXIT / etc. check.  For TEN_COIN the
        sub_key is the per-course coin index (0/1/2) from
        ``metadata["coin_index"]`` so the 3 distinct 10-coin checks
        dedup independently."""
        sub_key = int(check.metadata.get("coin_index", 0))
        key = (check.kind.value, check.stage_key, sub_key)
        with self._lock:
            if key in self._emitted_keys:
                return False
            self._emitted_keys.add(key)
            self.emitted_checks.append(check)
            return True

    def bump_death_count(self) -> None:
        """M3.8 DeathLink — incremented on each DEATH_DETECTED nerve.
        Bridge forwards to AP later."""
        with self._lock:
            self.death_count += 1

    def mark_goal_complete(self) -> bool:
        """M3.7 — mark this save as goal-complete.  Returns True on the
        first call (so the caller should emit one ``GoalCompleted`` /
        AP StatusUpdate), False on subsequent calls so duplicate Nerve
        fires (e.g. the player re-loads and replays the post-Bowser
        scene) don't generate redundant AP traffic."""
        with self._lock:
            if self.goal_complete:
                return False
            self.goal_complete = True
            return True

    # ---- Read accessors -----------------------------------------------

    def has_emitted(
        self,
        kind: CheckKind,
        stage_key: int,
        coin_index: int = 0,
    ) -> bool:
        """Cheap dedup-status read, mostly for tests.  Pass ``coin_index``
        for TEN_COIN; defaults to 0 for all other kinds."""
        with self._lock:
            return (kind.value, stage_key, coin_index) in self._emitted_keys

    def count_emitted(self, kind: CheckKind | None = None) -> int:
        """Number of CheckEmitted entries, optionally filtered by kind."""
        with self._lock:
            if kind is None:
                return len(self.emitted_checks)
            return sum(1 for c in self.emitted_checks if c.kind == kind)
