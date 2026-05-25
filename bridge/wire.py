"""Wire-format definitions for the Switch <-> Bridge TCP channel (M4).

Pure stdlib, asyncio-agnostic.  Modeled on
smo_archipelago/apworld/smo_archipelago/client/protocol.py, but adapted
to SMBW's event model: the Switch ships Nerve fires (M1) and raw
PlayReport payload bytes (M2.4) up, and receives GrantBadge commands
(M3.2) down.

Frame format: one JSON object per line, terminated by `b"\\n"`, capped
at ``MAX_LINE_BYTES`` bytes per line.  The discriminator field is
``"t"`` (kept short for byte efficiency on the Switch encoder).

Versioning: the HELLO line carries ``wire_ver``; the bridge ACKs with
its own version.  Mismatch = the bridge MAY refuse the session (M5
hardens; M4 only logs).

This module is intentionally separate from :mod:`bridge.protocol` --
``protocol`` defines the in-process event shapes the M2.6 processor
consumes, while ``wire`` defines the serialization shapes the M4 LAN
transport carries.  ``wire`` knows how to translate between them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .protocol import NerveFireMsg, NerveKind, PlayReportMsg


# ---------------------------------------------------------------------------
# Constants.

WIRE_VERSION = 1
"""Bumped on incompatible schema changes.  Both ends compare at HELLO."""

MAX_LINE_BYTES = 8192
"""Hard cap on the size of one framed JSON line, including the trailing
``\\n``.  The Switch encoder allocates a single 8 KiB ``LineBuffer``;
the bridge's ``asyncio.StreamReader.readuntil`` is bounded to match.
PlayReport payloads observed in the wild top out at ~355 bytes (~710
hex chars), so this leaves comfortable headroom."""


# ---------------------------------------------------------------------------
# Errors.


class ProtocolError(ValueError):
    """Raised when a wire line cannot be decoded into a known message.

    Covers: malformed JSON, missing or unknown ``"t"`` discriminator,
    missing required fields, wrong field type, value out of range.
    Callers (the LAN server) should log + drop the line and continue;
    the connection itself stays alive."""


# ---------------------------------------------------------------------------
# Message dataclasses.  Each carries a ``T`` class-level string that
# names the wire discriminator and an instance ``to_wire()`` /
# classmethod ``from_wire()`` pair.  Discriminators are short to keep
# the Switch encoder's hand-rolled JSON output compact.


@dataclass(frozen=True)
class HelloMsg:
    """Switch -> Bridge.  First line on a fresh TCP session.

    Carries client identity so the bridge can log + sanity-check.  The
    bridge replies with :class:`HelloAckMsg`.
    """

    T = "hello"

    mod_ver: str
    game_ver: str
    pid: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "t": self.T,
            "wire_ver": WIRE_VERSION,
            "mod_ver": self.mod_ver,
            "game_ver": self.game_ver,
            "pid": self.pid,
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> HelloMsg:
        return cls(
            mod_ver=_req_str(d, "mod_ver"),
            game_ver=_req_str(d, "game_ver"),
            pid=int(d.get("pid", 0)),
        )


@dataclass(frozen=True)
class HelloAckMsg:
    """Bridge -> Switch.  Reply to :class:`HelloMsg`.

    ``ok=False`` with a ``reason`` is the polite refusal (e.g. wire
    version mismatch); the bridge closes the connection right after.
    M4 only logs mismatches and accepts; M5 will refuse.
    """

    T = "hello_ack"

    ok: bool
    bridge_ver: str
    wire_ver: int = WIRE_VERSION
    reason: str = ""

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "t": self.T,
            "ok": bool(self.ok),
            "wire_ver": self.wire_ver,
            "bridge_ver": self.bridge_ver,
        }
        if self.reason:
            out["reason"] = self.reason
        return out

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> HelloAckMsg:
        return cls(
            ok=bool(d.get("ok", False)),
            bridge_ver=_req_str(d, "bridge_ver"),
            wire_ver=int(d.get("wire_ver", WIRE_VERSION)),
            reason=str(d.get("reason", "")),
        )


@dataclass(frozen=True)
class NerveFireWireMsg:
    """Switch -> Bridge.  Wire form of :class:`bridge.protocol.NerveFireMsg`.

    Kept as a separate type from the in-process ``NerveFireMsg`` so the
    wire layer can grow new fields (e.g. a per-fire timestamp) without
    polluting the processor's input dataclass.  Convert via
    :meth:`to_event` / :meth:`from_event`.

    M4 only ever sends ``WONDER_SEED_AWARDED`` on this channel; the
    bridge classifies course clears from PlayReport ``course_result``
    rooms instead (see ``processor._handle_nerve_fire`` line 75 --
    Nerve-source ``COURSE_CLEARED`` is explicitly dropped as a
    "precursor signal").  The wire format still admits all
    :class:`NerveKind` values so M3.8 DeathLink can add ``DEATH_DETECTED``
    later without a schema bump.
    """

    T = "nerve"

    kind: NerveKind
    seq: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "kind": self.kind.value, "seq": self.seq}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> NerveFireWireMsg:
        kind_raw = _req_str(d, "kind")
        try:
            kind = NerveKind(kind_raw)
        except ValueError as e:
            raise ProtocolError(f"unknown nerve kind: {kind_raw!r}") from e
        return cls(kind=kind, seq=int(d.get("seq", 0)))

    def to_event(self) -> NerveFireMsg:
        return NerveFireMsg(kind=self.kind, seq=self.seq)

    @classmethod
    def from_event(cls, ev: NerveFireMsg) -> NerveFireWireMsg:
        return cls(kind=ev.kind, seq=ev.seq)


@dataclass(frozen=True)
class PlayReportWireMsg:
    """Switch -> Bridge.  Wire form of :class:`bridge.protocol.PlayReportMsg`.

    The raw CBOR-ish payload bytes are hex-encoded.  We picked hex over
    base64 because the Switch encoder is hand-rolled in fixed-buffer C++
    and hex is two table lookups per byte with no padding state machine,
    whereas base64 needs a 64-entry alphabet and bit-shuffling.  At
    PlayReport payload sizes (~150-355 B observed), hex's 2x overhead vs
    base64's 1.33x still fits comfortably under the 8 KiB line cap.
    """

    T = "play_report"

    room: str
    payload_hex: str

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "room": self.room, "payload": self.payload_hex}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> PlayReportWireMsg:
        return cls(
            room=_req_str(d, "room"),
            payload_hex=_req_str(d, "payload"),
        )

    def to_event(self) -> PlayReportMsg:
        try:
            payload = bytes.fromhex(self.payload_hex)
        except ValueError as e:
            raise ProtocolError(f"play_report payload not valid hex: {e}") from e
        return PlayReportMsg(room=self.room, payload=payload)

    @classmethod
    def from_event(cls, ev: PlayReportMsg) -> PlayReportWireMsg:
        return cls(room=ev.room, payload_hex=ev.payload.hex())


@dataclass(frozen=True)
class GrantBadgeMsg:
    """Bridge -> Switch.  M4's single inbound item-grant type.

    ``internal_id`` is the bit position in SMBW's container-C badge
    bitfield (hash ``0x105df820``).  The Switch worker thread enqueues
    onto its inbound SPSC ring; the game thread drains and calls
    ``probe::grantBadgeBit(internal_id)``.

    Range is bounded by the badge registry (max ~50ish badges); the
    Switch caller still validates the registry before applying.
    Values outside ``[0, 63]`` are dropped here so the wire never carries
    nonsense.
    """

    T = "grant_badge"

    internal_id: int

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "internal_id": self.internal_id}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> GrantBadgeMsg:
        raw = d.get("internal_id")
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise ProtocolError(f"grant_badge.internal_id must be int, got {raw!r}")
        if not (0 <= raw < 64):
            raise ProtocolError(
                f"grant_badge.internal_id out of range [0, 64): {raw}"
            )
        return cls(internal_id=raw)


@dataclass(frozen=True)
class ErrMsg:
    """Either direction.  Protocol-level error notification.

    Used for malformed-input notices, unknown-message-type bounces, and
    HELLO refusals.  Not a fatal close signal -- the receiver should log
    and keep the connection alive.
    """

    T = "err"

    reason: str

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "reason": self.reason}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> ErrMsg:
        return cls(reason=_req_str(d, "reason"))


@dataclass(frozen=True)
class PingMsg:
    """Either direction.  Optional liveness probe; the peer SHOULD reply
    with a matching :class:`PongMsg`.  No-op in M4 -- defined for forward
    compat with M5 keepalive."""

    T = "ping"

    ts_ms: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "ts_ms": self.ts_ms}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> PingMsg:
        return cls(ts_ms=int(d.get("ts_ms", 0)))


@dataclass(frozen=True)
class PongMsg:
    """Either direction.  Reply to a :class:`PingMsg`; echoes the
    original ``ts_ms`` so the prober can measure round-trip."""

    T = "pong"

    ts_ms: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "ts_ms": self.ts_ms}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> PongMsg:
        return cls(ts_ms=int(d.get("ts_ms", 0)))


# Union of all message types -- handy for type hints on decoder return.
WireMsg = (
    HelloMsg
    | HelloAckMsg
    | NerveFireWireMsg
    | PlayReportWireMsg
    | GrantBadgeMsg
    | ErrMsg
    | PingMsg
    | PongMsg
)


# Registry of T -> from_wire constructor.  Adding a new message type
# means: define the dataclass with class-level ``T`` + ``to_wire`` +
# ``from_wire``, then append to this dict.
_FROM_WIRE: dict[str, Any] = {
    HelloMsg.T: HelloMsg.from_wire,
    HelloAckMsg.T: HelloAckMsg.from_wire,
    NerveFireWireMsg.T: NerveFireWireMsg.from_wire,
    PlayReportWireMsg.T: PlayReportWireMsg.from_wire,
    GrantBadgeMsg.T: GrantBadgeMsg.from_wire,
    ErrMsg.T: ErrMsg.from_wire,
    PingMsg.T: PingMsg.from_wire,
    PongMsg.T: PongMsg.from_wire,
}


# ---------------------------------------------------------------------------
# Codec.


def encode(msg: WireMsg) -> bytes:
    """Serialize ``msg`` to a single newline-terminated JSON line.

    Output is UTF-8 with no extraneous whitespace -- the Switch decoder
    looks for ``"\\n"`` as the only frame boundary.  Raises
    :class:`ValueError` if the encoded payload would exceed
    :data:`MAX_LINE_BYTES`.
    """
    payload = json.dumps(msg.to_wire(), separators=(",", ":"), ensure_ascii=False)
    line = payload.encode("utf-8") + b"\n"
    if len(line) > MAX_LINE_BYTES:
        raise ValueError(
            f"encoded message exceeds MAX_LINE_BYTES ({len(line)} > {MAX_LINE_BYTES}): "
            f"type={getattr(msg, 'T', type(msg).__name__)}"
        )
    return line


def decode(line: bytes | str) -> WireMsg:
    """Parse one wire line into the matching dataclass.

    Accepts ``bytes`` (raw from the socket, with or without the trailing
    newline) or ``str``.  Raises :class:`ProtocolError` on any failure
    -- the caller should log + drop and continue reading.
    """
    if isinstance(line, bytes):
        if len(line) > MAX_LINE_BYTES:
            raise ProtocolError(
                f"line exceeds MAX_LINE_BYTES ({len(line)} > {MAX_LINE_BYTES})"
            )
        # Trim a single trailing newline if present -- ``readuntil`` keeps it.
        if line.endswith(b"\n"):
            line = line[:-1]
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ProtocolError(f"line is not valid UTF-8: {e}") from e
    else:
        text = line.rstrip("\n")

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"line is not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ProtocolError(f"top-level JSON must be an object, got {type(obj).__name__}")

    t = obj.get("t")
    if not isinstance(t, str):
        raise ProtocolError(f"missing or non-string discriminator 't': {obj!r}")

    ctor = _FROM_WIRE.get(t)
    if ctor is None:
        raise ProtocolError(f"unknown message type 't'={t!r}")
    return ctor(obj)


# ---------------------------------------------------------------------------
# Internal helpers.


def _req_str(d: dict[str, Any], key: str) -> str:
    """Pull a required string field from a decoded JSON dict, or raise."""
    v = d.get(key)
    if not isinstance(v, str):
        raise ProtocolError(
            f"missing or non-string field {key!r}: got {type(v).__name__}={v!r}"
        )
    return v
