"""Wire-format definitions for the Switch <-> Bridge TCP channel (M4).

Pure stdlib, asyncio-agnostic.  Modeled on
smo_archipelago/apworld/smo_archipelago/client/protocol.py, but adapted
to SMBW's event model: the Switch ships Nerve fires (M1) and raw
PlayReport payload bytes (M2.4) up, and receives SetBadgesAbsolute
(M4 follow-up #2; AP-authoritative badge sync) and GrantHashKeyed
(M3.3) commands down.

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
class SetBadgesAbsoluteMsg:
    """Bridge -> Switch.  AP-authoritative badge sync (M4 follow-up #2).

    ``bits`` is the absolute desired contents of SMBW's container-C
    owned-badge bitfield (hash ``0x105df820``).  Bit N == owned badge
    with internal_id N.  The Switch worker enqueues onto its inbound
    SPSC ring; the game thread drains and calls
    ``probe::setBadgeBitfieldAbsolute(bits)`` which overwrites all 128
    bits of the live container-C bitfield (low 64 = owned, high 64 =
    mirror).

    Sent by the bridge on three triggers:
      1. Every AP ``ReceivedItems`` update -- recompute mask + send.
      2. Every Switch ``HelloMsg`` (replay-on-reconnect) so the bitfield
         survives save/reload and game restarts.
      3. A periodic ~2 s tick to revert any in-game badge pickup
         (Poplin shop, badge house, badge medley) that AP didn't grant
         -- AP is the sole authority over the badge pool.

    Replaces the M3.2 per-bit ``GrantBadgeMsg``; the absolute-write
    primitive is idempotent (same input always produces the same final
    state), which subsumes both the M3.2 incremental grant and the
    planned M4.5 replay-on-HelloMsg work for badges.

    Range is ``[0, 2**64)``; the Switch parses as int64 so practical
    badge masks (currently fit in u32, max bit position 46 = bit 46) are
    well below the int64 limit.
    """

    T = "set_badges_absolute"

    bits: int

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "bits": self.bits}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> SetBadgesAbsoluteMsg:
        raw = d.get("bits")
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise ProtocolError(
                f"set_badges_absolute.bits must be int, got {raw!r}")
        if not (0 <= raw < (1 << 64)):
            raise ProtocolError(
                f"set_badges_absolute.bits out of range [0, 2**64): {raw}")
        return cls(bits=raw)


@dataclass(frozen=True)
class GrantHashKeyedMsg:
    """Bridge -> Switch.  M3.3 / M3.3b inbound grant type.

    Pairs a SMBW save-data field ``hash`` (e.g. ``0x55815859`` for
    GRAND_SEED_WORLD1) with the counter ``value`` to set.  The Switch
    routes this through ``probe::grantContainerACounter`` which calls
    ``FUN_710049F648(gmd, value, hash)`` -- the GameDataMgr container-A
    setter.  It's a SETTER, not an incrementer: the new value overwrites
    the current one.  The writer truncates u32 -> u8 internally for
    typed-bool slots (Royal Seeds, COMPLETE_GAME, INTRO), so the same
    primitive serves both u16 counters and bool flags.

    Both fields are validated to the full u32 range ``[0, 2**32)``;
    semantic validation (e.g. "value=1 for Royal Seeds") lives in the
    table layer above (``royal_seed_table``).
    """

    T = "grant_hash_keyed"

    hash: int
    value: int

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "hash": self.hash, "value": self.value}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> GrantHashKeyedMsg:
        raw_hash = d.get("hash")
        if not isinstance(raw_hash, int) or isinstance(raw_hash, bool):
            raise ProtocolError(
                f"grant_hash_keyed.hash must be int, got {raw_hash!r}"
            )
        if not (0 <= raw_hash < (1 << 32)):
            raise ProtocolError(
                f"grant_hash_keyed.hash out of range [0, 2**32): {raw_hash}"
            )
        raw_value = d.get("value")
        if not isinstance(raw_value, int) or isinstance(raw_value, bool):
            raise ProtocolError(
                f"grant_hash_keyed.value must be int, got {raw_value!r}"
            )
        if not (0 <= raw_value < (1 << 32)):
            raise ProtocolError(
                f"grant_hash_keyed.value out of range [0, 2**32): {raw_value}"
            )
        return cls(hash=raw_hash, value=raw_value)


@dataclass(frozen=True)
class KillMsg:
    """Bridge -> Switch.  M3.8 DeathLink incoming half.

    Sent when an AP DeathLink Bounce arrives for a slot that has the
    ``DeathLink`` tag.  The Switch dispatcher drains this on the game
    thread and calls ``probe::synthKill()`` which writes 0 to the live
    HP byte (``live_base + 0x1C``) to force Mario's death-handler tick.

    ``source`` and ``cause`` are free-form strings from the AP Bounce's
    ``data`` field.  Sizes are bounded so the Switch's fixed-buffer
    decoder doesn't have to grow: ``source`` is typically an AP slot
    name (we cap at 48 chars to match the Switch's WireKill struct);
    ``cause`` is short human-readable text capped at 128.  Longer
    incoming values are truncated on the wire encoder side; the bridge
    validator below enforces those caps so a misbehaving sender never
    overflows the Switch buffer.
    """

    T = "kill"

    source: str
    cause: str

    # Source/cause caps -- MUST match WireKill char buffer sizes in
    # switch-mod/src/program/ap/ApProtocol.hpp.  Bumping either side
    # requires bumping the other.
    SOURCE_CAP = 48
    CAUSE_CAP = 128

    def to_wire(self) -> dict[str, Any]:
        return {
            "t": self.T,
            "source": self.source[: self.SOURCE_CAP],
            "cause": self.cause[: self.CAUSE_CAP],
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> KillMsg:
        return cls(
            source=_req_str(d, "source"),
            cause=_req_str(d, "cause"),
        )


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
    | SetBadgesAbsoluteMsg
    | GrantHashKeyedMsg
    | KillMsg
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
    SetBadgesAbsoluteMsg.T: SetBadgesAbsoluteMsg.from_wire,
    GrantHashKeyedMsg.T: GrantHashKeyedMsg.from_wire,
    KillMsg.T: KillMsg.from_wire,
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
