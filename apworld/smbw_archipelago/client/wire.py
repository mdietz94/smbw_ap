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

from .protocol import BadgeAcquiredMsg, NerveFireMsg, NerveKind, PlayReportMsg


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
class BadgeAcquiredWireMsg:
    """Switch -> Bridge.  M2.3 outbound: an in-game badge acquisition
    that AP hadn't already granted.

    Detected Switch-side by ``probe::setBadgeBitfieldAbsolute`` diffing
    the live container-C bitfield against the AP-known mask just before
    each absolute-overwrite.  Any bit set in the live state but not in
    AP's mask is a Poplin-shop / badge-house / badge-medley / badge-
    challenge acquisition the bridge should report as a LocationCheck.
    The overwrite then strips the bit; the player will see the badge
    re-appear ~roundtrip later when AP swaps in the actual item.

    ``internal_id`` is the bit position in the container-C owned-badge
    bitfield (0..63), which IS the SMBW internal badge id.  ``seq`` is
    the Switch's per-message-kind fire counter, useful for log
    correlation.

    Sized as u32 on both fields to match the Switch ``WireBadgeAcquired``
    struct; in practice ``internal_id`` is small (<= 63 for currently-
    documented badges) but the wire range is the full u32.

    Kept as a separate type from the in-process
    :class:`apworld.smbw_archipelago.client.protocol.BadgeAcquiredMsg`
    so the wire layer can grow new fields (e.g. a per-fire timestamp)
    without polluting the processor's input dataclass.  Convert via
    :meth:`to_event` / :meth:`from_event`.
    """

    T = "badge_acquired"

    internal_id: int
    seq: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "internal_id": self.internal_id, "seq": self.seq}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> BadgeAcquiredWireMsg:
        raw_id = d.get("internal_id")
        if not isinstance(raw_id, int) or isinstance(raw_id, bool):
            raise ProtocolError(
                f"badge_acquired.internal_id must be int, got {raw_id!r}"
            )
        if not (0 <= raw_id < (1 << 32)):
            raise ProtocolError(
                f"badge_acquired.internal_id out of range [0, 2**32): {raw_id}"
            )
        raw_seq = d.get("seq", 0)
        if not isinstance(raw_seq, int) or isinstance(raw_seq, bool):
            raise ProtocolError(
                f"badge_acquired.seq must be int, got {raw_seq!r}"
            )
        if not (0 <= raw_seq < (1 << 32)):
            raise ProtocolError(
                f"badge_acquired.seq out of range [0, 2**32): {raw_seq}"
            )
        return cls(internal_id=raw_id, seq=raw_seq)

    def to_event(self) -> BadgeAcquiredMsg:
        return BadgeAcquiredMsg(internal_id=self.internal_id, seq=self.seq)

    @classmethod
    def from_event(cls, ev: BadgeAcquiredMsg) -> BadgeAcquiredWireMsg:
        return cls(internal_id=ev.internal_id, seq=ev.seq)


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
class SetWonderSeedCountsMsg:
    """Bridge -> Switch.  AP-authoritative Wonder Seed gate override.

    Carries an 8-element array of cumulative Wonder Seed counts received
    from AP, one per world bucket:

      index 0  W1 Wonder Seed
      index 1  W2 Wonder Seed
      index 2  W3 Wonder Seed
      index 3  W4 Wonder Seed
      index 4  W5 Wonder Seed
      index 5  W6 Wonder Seed
      index 6  Petal Isles Wonder Seed
      index 7  Special World Wonder Seed

    Switch-side: ``ApFrameBridge::drainInbound`` caches the array in
    static storage.  On each NerveActivateOnce tick (~2 s under normal
    play), the Switch reads container-A hash ``0x9f5ead3c`` (the live
    "current world index"), maps it to a bucket index, and calls
    ``probe::pushWonderSeedOverride(counts[bucket])`` -- which writes
    that value to all 5 mirror hashes of the per-current-world Wonder
    Seed count (``0x21f89ab1``, ``0x8c20ccb7``, ``0xeeff353b``,
    ``0x390eb960``, ``0xa0e5f253``).  ``0x390eb960`` is the one the
    gate predicate ``FUN_71001787b40`` reads when deciding whether the
    player has enough seeds to enter a level -- making AP the sole
    authority over Wonder Seed gating.

    Live-validated 2026-05-26: with all 5 hashes overridden to 99, a
    W3 gate that previously denied entry (player had 1 actual W3 seed)
    opened on the next attempt and the in-game UI counter showed 99.

    Sent by the bridge on the badge-sync triggers:
      1. Every AP ``ReceivedItems`` update -- recompute counts + send.
      2. Every Switch ``HelloMsg`` (replay-on-reconnect).
      3. The periodic ~2 s tick (the natural world-map transition
         resets the 5 mirror hashes from per-course-bitfield recompute;
         the tick re-asserts AP's authority within ~2 s).

    Counts are validated to ``[0, 2**32)``; the array length is fixed
    at 8 (asserted in both ``from_wire`` and ``to_wire``).
    """

    T = "set_wonder_seed_counts"

    WORLD_COUNT: int = 8

    counts: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if len(self.counts) != self.WORLD_COUNT:
            raise ProtocolError(
                f"set_wonder_seed_counts.counts must have length "
                f"{self.WORLD_COUNT}, got {len(self.counts)}")
        for i, v in enumerate(self.counts):
            if not isinstance(v, int) or isinstance(v, bool):
                raise ProtocolError(
                    f"set_wonder_seed_counts.counts[{i}] must be int, "
                    f"got {v!r}")
            if not (0 <= v < (1 << 32)):
                raise ProtocolError(
                    f"set_wonder_seed_counts.counts[{i}] out of range "
                    f"[0, 2**32): {v}")

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "counts": list(self.counts)}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> SetWonderSeedCountsMsg:
        raw = d.get("counts")
        if not isinstance(raw, list):
            raise ProtocolError(
                f"set_wonder_seed_counts.counts must be list, got {raw!r}")
        # __post_init__ validates length + per-element int + range.
        return cls(counts=tuple(raw))


@dataclass(frozen=True)
class IncrementHashKeyedMsg:
    """Bridge -> Switch.  Saturating add/sub on a container-A counter.

    Sibling of :class:`GrantHashKeyedMsg`; differs in semantic: the
    Switch reads the current counter value via ``FUN_710012AE94`` (the
    container-A reader, signature ``(gmd, uint32_t* out, uint32_t hash)``
    at NSO ``+0x0012AE94``), computes ``saturating(cur + delta)`` in the
    range ``[0, 2**32)``, and writes the result via ``FUN_710049F648``
    (the container-A writer used by ``GrantHashKeyed``).  The writer
    truncates to the slot's typed width internally (u8/u16) just like
    an absolute set.

    Delta is signed (i32 range, ``[-2**31, 2**31)``) so the bridge can
    express both grants (positive) and refunds (negative).  Two users
    today:

      * ``+10`` per "10 Coin" AP item received -- the existing route
        adds 10 to the player's purple-coin running total per filler
        grant.
      * ``-10`` per TEN_COIN CheckEmitted -- when the player picks up
        a 10-coin block in game, the game increments the live counter
        by 10 *before* the bridge sees the location check.  AP is now
        the authority over what the block actually grants, so we
        refund the +10 at the same moment we report the check.  If
        AP's payload happens to be a "10 Coin" item routed back to us,
        the inbound side's ``+10`` cancels the refund and the net is
        +10; if AP scattered the item elsewhere (or replaced it with
        a badge), the refund stands and the player gets the actual
        item rather than free coins.

    Saturation is on the Switch side: a refund that would underflow
    clamps to 0 (so picking up a 10-coin block when at 0 purple coins
    -- which the game shouldn't really allow but defensive arithmetic
    is cheap -- doesn't wrap to ~4.3 billion via the u16 truncate).

    Save-survival caveat: the writer is deferred-write (queues to
    ``gmd+0xf8`` dirty buffer, flushes on next save).  A load-before-
    flush would silently drop the increment.  Unlike Royal Seeds,
    container-A counter grants are NOT replayed on HelloMsg -- replay
    would double-count.  Acceptable for coin-style filler; if a
    counter ever becomes progression-critical, a dedup story (per-AP-
    item-index applied-set persisted across reconnects) is required.
    """

    T = "increment_hash_keyed"

    # The signed-i32 range matches what the Switch decoder accepts via
    # nextInt (int64-typed) after the [INT32_MIN, INT32_MAX] cap; keep
    # the Python validator in sync.
    _DELTA_MIN = -(1 << 31)
    _DELTA_MAX = (1 << 31) - 1

    hash: int
    delta: int

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "hash": self.hash, "delta": self.delta}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> IncrementHashKeyedMsg:
        raw_hash = d.get("hash")
        if not isinstance(raw_hash, int) or isinstance(raw_hash, bool):
            raise ProtocolError(
                f"increment_hash_keyed.hash must be int, got {raw_hash!r}"
            )
        if not (0 <= raw_hash < (1 << 32)):
            raise ProtocolError(
                f"increment_hash_keyed.hash out of range [0, 2**32): {raw_hash}"
            )
        raw_delta = d.get("delta")
        if not isinstance(raw_delta, int) or isinstance(raw_delta, bool):
            raise ProtocolError(
                f"increment_hash_keyed.delta must be int, got {raw_delta!r}"
            )
        if not (cls._DELTA_MIN <= raw_delta <= cls._DELTA_MAX):
            raise ProtocolError(
                f"increment_hash_keyed.delta out of i32 range "
                f"[{cls._DELTA_MIN}, {cls._DELTA_MAX}]: {raw_delta}"
            )
        return cls(hash=raw_hash, delta=raw_delta)


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
    | BadgeAcquiredWireMsg
    | PlayReportWireMsg
    | SetBadgesAbsoluteMsg
    | GrantHashKeyedMsg
    | IncrementHashKeyedMsg
    | SetWonderSeedCountsMsg
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
    BadgeAcquiredWireMsg.T: BadgeAcquiredWireMsg.from_wire,
    PlayReportWireMsg.T: PlayReportWireMsg.from_wire,
    SetBadgesAbsoluteMsg.T: SetBadgesAbsoluteMsg.from_wire,
    GrantHashKeyedMsg.T: GrantHashKeyedMsg.from_wire,
    IncrementHashKeyedMsg.T: IncrementHashKeyedMsg.from_wire,
    SetWonderSeedCountsMsg.T: SetWonderSeedCountsMsg.from_wire,
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
