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


@dataclass(frozen=True)
class NerveFireMsg:
    """A tracked Nerve activated on the Switch."""

    kind: NerveKind
    # Monotonic per-source counter (the M1 logs already prefix "fire #N").
    # Useful for sequence reconciliation if a TCP reconnect drops events.
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
    """The six AP location families this bridge classifies events into."""

    TOP_OF_FLAG = "top_of_flag"          # 89 AP checks
    NORMAL_EXIT = "normal_exit"          # 96 AP checks
    SECRET_EXIT = "secret_exit"          # 9 AP checks
    FAKE_EXIT = "fake_exit"              # 5 AP checks (goal_id=2 guessed)
    PALACE_CLEAR = "palace_clear"        # 7 AP checks (Royal Seed)
    WONDER_SEED = "wonder_seed"          # 124 AP checks (mid-course)
    TEN_COIN = "ten_coin"                # 306 AP checks (102 courses × 3)
                                         # CheckEmitted.metadata["coin_index"]
                                         # is 0/1/2 — see docs/m2.2-runbook.md.


@dataclass(frozen=True)
class CheckEmitted:
    """A bridge-side classification of an in-game event into an AP
    location-family + per-course identifier.

    The bridge processor emits these in response to incoming events; a
    later layer (M5 apworld glue) translates `(kind, stage_key)` → the
    canonical AP location name.

    Fields:
        kind:       which AP family (see CheckKind)
        stage_key:  unique 32-bit course identifier from
                     stage_info.stage_key (M2.4)
        metadata:   free-form dict for extra context (course_no,
                     world_no, total_get_finish_seed_count, etc.) the
                     downstream consumer may want for diagnostics
    """

    kind: CheckKind
    stage_key: int
    metadata: dict[str, Any] = field(default_factory=dict)
