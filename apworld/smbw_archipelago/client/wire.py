"""Wire-format definitions for the Switch <-> Bridge TCP channel (M4).

Pure stdlib, asyncio-agnostic.  Modeled on
smo_archipelago/apworld/smo_archipelago/client/protocol.py, but adapted
to SMBW's event model: the Switch ships Nerve fires (M1) and raw
PlayReport payload bytes (M2.4) up, and receives SetBadgesAbsolute
(M4 follow-up #2; AP-authoritative badge sync), SetRoyalSeedsAbsolute
(2026-05-26; AP-authoritative Royal Seed sync), SetWonderSeedCounts
(AP-authoritative gate override), and GrantHashKeyed / IncrementHash-
Keyed (debug + 10-coin paths) commands down.

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
from typing import Any, ClassVar

from .protocol import (
    BadgeAcquiredMsg,
    CharBlockHitMsg,
    NerveFireMsg,
    NerveKind,
    PlayReportMsg,
)


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
class CharBlockHitWireMsg:
    """Switch -> Bridge.  Character-block sanity: a player-specific
    ``ObjectBlockClarityCharacter`` block was bumped.

    Wire form of :class:`...protocol.CharBlockHitMsg`.  The GetDamage-
    ReactionPlayerNo hook fires this on every resolve to a local player
    slot; the bridge filters by table (see CharBlockHitMsg docstring).

    ``player_slot`` (0-3) and ``seq`` are u32; ``chara`` is the hitting
    character (0-11, or -1 = "Switch didn't resolve it, bridge falls back
    to the course's chara_type_array").  ``pos`` is the block world
    position (``[0,0,0]`` in v1) as 3 floats for the future multi-block
    pass.
    """

    T = "char_block_hit"

    player_slot: int
    chara: int = -1
    pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    seq: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "t": self.T,
            "player_slot": self.player_slot,
            "chara": self.chara,
            "pos": list(self.pos),
            "seq": self.seq,
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> CharBlockHitWireMsg:
        raw_slot = d.get("player_slot")
        if not isinstance(raw_slot, int) or isinstance(raw_slot, bool):
            raise ProtocolError(
                f"char_block_hit.player_slot must be int, got {raw_slot!r}")
        # Allow the full u32 range defensively; the bridge only ever uses
        # slot 0-3 but a noisy hook shouldn't crash the decoder.
        if not (0 <= raw_slot < (1 << 32)):
            raise ProtocolError(
                f"char_block_hit.player_slot out of range: {raw_slot}")
        raw_chara = d.get("chara", -1)
        if not isinstance(raw_chara, int) or isinstance(raw_chara, bool):
            raise ProtocolError(
                f"char_block_hit.chara must be int, got {raw_chara!r}")
        if not (-1 <= raw_chara < (1 << 16)):
            raise ProtocolError(
                f"char_block_hit.chara out of range: {raw_chara}")
        raw_pos = d.get("pos", [0.0, 0.0, 0.0])
        if (not isinstance(raw_pos, list) or len(raw_pos) != 3
                or not all(isinstance(v, (int, float))
                           and not isinstance(v, bool) for v in raw_pos)):
            raise ProtocolError(
                f"char_block_hit.pos must be a 3-float list, got {raw_pos!r}")
        raw_seq = d.get("seq", 0)
        if not isinstance(raw_seq, int) or isinstance(raw_seq, bool):
            raise ProtocolError(
                f"char_block_hit.seq must be int, got {raw_seq!r}")
        return cls(
            player_slot=raw_slot,
            chara=raw_chara,
            pos=(float(raw_pos[0]), float(raw_pos[1]), float(raw_pos[2])),
            seq=raw_seq,
        )

    def to_event(self) -> CharBlockHitMsg:
        return CharBlockHitMsg(
            player_slot=self.player_slot,
            chara=self.chara,
            pos=self.pos,
            seq=self.seq,
        )

    @classmethod
    def from_event(cls, ev: CharBlockHitMsg) -> CharBlockHitWireMsg:
        return cls(
            player_slot=ev.player_slot,
            chara=ev.chara,
            pos=ev.pos,
            seq=ev.seq,
        )


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
class SetRoyalSeedsAbsoluteMsg:
    """Bridge -> Switch.  AP-authoritative Royal Seed sync.

    ``mask`` is the absolute 6-bit set of Royal Seeds AP has granted:
    bit 0 = W1, bit 1 = W2, ..., bit 5 = W6.  The Switch loops the 6
    container-B bool hashes (see ``ROYAL_SEED_HASHES`` in
    ``royal_seed_table.py`` for the canonical order, mirrored on the
    Switch side as ``kRoyalSeedHashes`` in ``ApFrameBridge.cpp``) and
    calls ``probe::grantContainerBBool(hash, (mask >> bit) & 1)`` for
    each one.  Idempotent absolute-overwrite: setting AP-granted seeds
    AND clearing any seed the player obtained in-game without AP
    (palace clear before AP released the matching item).

    Sent by the bridge on the same triggers as
    :class:`SetBadgesAbsoluteMsg`:
      1. Every AP ``ReceivedItems`` -- recompute mask + send.
      2. Every Switch ``HelloMsg`` (replay-on-reconnect) so the
         container-B bools survive save/reload and Switch reboots
         without depending on the in-game save flush.
      3. The periodic ~2 s tick -- reverts any in-game palace-clear
         pickup that ran ahead of AP within seconds.

    Replaces the M4.5 per-seed additive ``GrantHashKeyedMsg`` replay,
    which could set AP-granted seeds but never clear seeds the player
    obtained in-game.  Container-B bools ``COMPLETE_GAME`` and
    ``INTRO_CUTSCENE_COMPLETED`` are deliberately NOT in scope -- those
    aren't AP items and the player should keep them once earned.

    Range is ``[0, 2**6)`` -- only the 6 documented Royal Seed bits;
    higher bits are rejected by the decoder.
    """

    T = "set_royal_seeds_absolute"

    # Matches royal_seed_table.WORLD_COUNT; duplicated here so the
    # wire layer doesn't depend on the table module.
    WORLD_COUNT: int = 6

    mask: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "mask": self.mask}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> SetRoyalSeedsAbsoluteMsg:
        raw = d.get("mask")
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise ProtocolError(
                f"set_royal_seeds_absolute.mask must be int, got {raw!r}")
        limit = 1 << cls.WORLD_COUNT
        if not (0 <= raw < limit):
            raise ProtocolError(
                f"set_royal_seeds_absolute.mask out of range "
                f"[0, {limit}): {raw}")
        return cls(mask=raw)


@dataclass(frozen=True)
class SetRoutableWorldsAbsoluteMsg:
    """Bridge -> Switch.  Open-world mode: AP-authoritative set of worlds
    routable on the overworld FROM THE START (2026-06).

    ``mask`` bit N == the world with AP bucket N is routable: bit 0 = W1,
    ..., bit 5 = W6, bit 6 = Petal Isles, bit 7 = Special.  Bit 8
    (``CASTLE_BIT``) == the Castle / Bowser route is open -- set by the
    client once enough palaces are cleared, alongside a
    ``SetRoyalSeedsAbsolute(0x3F)`` that actually opens the vanilla route.

    Switch-side: ``ApFrameBridge::drainInbound`` caches the mask in
    ``g_routable_world_mask``; the ``FUN_7100935ce0`` (NSO +0x935ce0)
    trampoline forces a true return for any world whose bit is set, so it
    becomes routable, passing through to the original predicate otherwise.
    A mask of 0 means "open-world inactive" -- the Switch hook no-ops and
    vanilla world-unlock behavior is byte-identical.

    Idempotent absolute-overwrite, sent on the same triggers as
    :class:`SetBadgesAbsoluteMsg`: every ReceivedItems, every Switch
    HelloMsg (replay-on-reconnect), and the periodic ~2 s tick.

    9 meaningful bits; range is ``[0, 2**16)`` for headroom (the Switch
    decodes it as a u16).
    """

    T = "set_routable_worlds"

    # Bit position of the Castle/Bowser route in the mask.  Mirrors
    # ``kCastleMaskBit`` in ApFrameBridge.hpp.
    CASTLE_BIT: int = 8

    mask: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "mask": self.mask}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> SetRoutableWorldsAbsoluteMsg:
        raw = d.get("mask")
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise ProtocolError(
                f"set_routable_worlds.mask must be int, got {raw!r}")
        if not (0 <= raw < (1 << 16)):
            raise ProtocolError(
                f"set_routable_worlds.mask out of range [0, 2**16): {raw}")
        return cls(mask=raw)


@dataclass(frozen=True)
class SetForceClearedCoursesMsg:
    """Bridge -> Switch.  Force the transient IsInClearedCourse flag for
    secret-exit "replay" courses (2026-06-30; both modes 2026-07-01).

    ``mask`` bit N == the course at index N of
    ``force_cleared_table.FORCE_CLEARED_COURSES`` should be treated as
    already-cleared so its secret goal spawns and its wall blocks are
    removed.  The Switch (``kForceClearedCourses`` in ``main.cpp``) maps each
    bit to an in-game ``(world_val, CourseInfo.CourseId)`` identity and, at
    scene-load for a matching course, writes ``IsInClearedCourse``
    (``0xbef2db36``) via ``probe::grantContainerBBool`` so the secret path
    appears.  See :mod:`force_cleared_table` and the memory
    ``smbwap-secret-exit-isinclearedcourse`` for the full mechanism.

    A mask of 0 means "nothing to force" -- the Switch write no-ops and the
    courses behave vanilla (secret path stays hidden until a genuine replay).
    Idempotent absolute-overwrite; sent on the same triggers as
    :class:`SetRoutableWorldsAbsoluteMsg` (every ReceivedItems, every Switch
    HelloMsg, the periodic ~2 s tick) so it survives save/reload + reboots
    and tracks a newly-checked NORMAL_EXIT gating a course's inclusion.

    Carried as a u16 for headroom; the bit count matches
    ``FORCE_CLEARED_COURSES`` (2 today).
    """

    T = "set_force_cleared_courses"

    mask: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "mask": self.mask}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> SetForceClearedCoursesMsg:
        raw = d.get("mask")
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise ProtocolError(
                f"set_force_cleared_courses.mask must be int, got {raw!r}")
        if not (0 <= raw < (1 << 16)):
            raise ProtocolError(
                f"set_force_cleared_courses.mask out of range "
                f"[0, 2**16): {raw}")
        return cls(mask=raw)


@dataclass(frozen=True)
class SetItemGetDenyMaskMsg:
    """Bridge -> Switch.  Power-up pickup negation (M3.1 / M5 groundwork,
    2026-06-10).

    ``mask`` bits name *runtime item-get types* the player must NOT be able
    to pick up.  Switch-side the ``ItemGetMaskBuild`` trampoline (NSO
    +0x3c4050) strips these bits from the player ItemGet component's
    freshly rebuilt "can pick up" bitmask (u32 at component+0xB0), so the
    pickup sensor refuses the touch exactly like the vanilla DrillDig
    setting does: the item stays in the level, no pickup animation, no
    transform, no damage.  Applied directly on the Switch network thread
    (single atomic store); a mask of 0 restores vanilla pickups.

    Bit table (= RomFS ``ItemGetActorType`` enum + 1; mirrors
    ``probe/ItemGetGate.hpp``):

    ========  ===========================
    bit       item
    ========  ===========================
    1         Kinoko (Super Mushroom)
    2         FireFlower
    3         Star
    4         OneUpKinoko
    5         ElephantSuit
    10        Key
    12        DrillSuit
    18        AwaFlower (Bubble Flower)
    ========  ===========================

    Idempotent absolute-overwrite; safe to replay on HelloMsg / tick.
    """

    T = "set_itemget_deny"

    BIT_KINOKO: int = 1
    BIT_FIRE_FLOWER: int = 2
    BIT_STAR: int = 3
    BIT_ONE_UP_KINOKO: int = 4
    BIT_ELEPHANT_SUIT: int = 5
    BIT_KEY: int = 10
    BIT_DRILL_SUIT: int = 12
    BIT_AWA_FLOWER: int = 18

    #: The 4 AP Power-Up items (Fire / Elephant / Drill / Bubble).
    AP_POWER_UPS_MASK: int = (
        (1 << BIT_FIRE_FLOWER) | (1 << BIT_ELEPHANT_SUIT)
        | (1 << BIT_DRILL_SUIT) | (1 << BIT_AWA_FLOWER))

    mask: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "mask": self.mask}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> SetItemGetDenyMaskMsg:
        raw = d.get("mask")
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise ProtocolError(
                f"set_itemget_deny.mask must be int, got {raw!r}")
        if not (0 <= raw < (1 << 32)):
            raise ProtocolError(
                f"set_itemget_deny.mask out of range [0, 2**32): {raw}")
        return cls(mask=raw)


@dataclass(frozen=True)
class SetUnlockedCharasMsg:
    """Bridge -> Switch.  AP-authoritative character-selection gate
    (2026-07-08).

    ``mask`` bit i == roster index i is a character the player has
    received from AP.  Bit order is the GAME's roster-enum order (the
    GameDataList character enum / the name table @NSO 0x71034efad8; the
    same order ``char_block_table.CHARA_ITEM_NAMES`` uses since its
    PR #163 correction) -- note Nabbit (Totten) sits at roster index 7,
    before the Yoshis:

    ========  =================  ======================
    bit       game name          AP item name
    ========  =================  ======================
    0         Mario              Mario
    1         Luigi              Luigi
    2         Peach              Peach
    3         Daisy              Daisy
    4         KinopioYellow      Yellow Toad
    5         KinopioBlue        Blue Toad
    6         Kinopico           Toadette
    7         Totten             Nabbit
    8         YoshiGreen         Green Yoshi
    9         YoshiRed           Red Yoshi
    10        YoshiYellow        Yellow Yoshi
    11        YoshiBlue          Light-Blue Yoshi
    ========  =================  ======================

    Switch-side, the CharaSelectCommit trampoline (NSO +0x96e25c)
    rewrites any committed selection of a locked character to a random
    unlocked one, and a per-frame sweep repairs selections made before
    the bridge connected.  A mask of 0 disables the gate (vanilla
    selection); sending a full 12-bit mask also disables any forcing in
    practice since everything is allowed.

    Idempotent absolute-overwrite; sent on every ReceivedItems, every
    Switch HelloMsg, and the periodic ~2 s tick.
    """

    T = "set_unlocked_charas"

    #: bit index -> AP item name, in roster order (see class docstring).
    ROSTER_ITEM_NAMES: tuple[str, ...] = (
        "Mario", "Luigi", "Peach", "Daisy",
        "Yellow Toad", "Blue Toad", "Toadette", "Nabbit",
        "Green Yoshi", "Red Yoshi", "Yellow Yoshi", "Light-Blue Yoshi",
    )

    mask: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "mask": self.mask}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> SetUnlockedCharasMsg:
        raw = d.get("mask")
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise ProtocolError(
                f"set_unlocked_charas.mask must be int, got {raw!r}")
        if not (0 <= raw < (1 << 16)):
            raise ProtocolError(
                f"set_unlocked_charas.mask out of range [0, 2**16): {raw}")
        return cls(mask=raw)


@dataclass(frozen=True)
class SetBadgeShopStateMsg:
    """Bridge -> Switch.  AP-authoritative Poplin badge-shop ownership.

    Two badge-internal-id-indexed masks (bit == internal_id == the index
    into the owned bitfield):

      * ``managed`` -- badges whose shop display state AP owns.  Bits NOT
        in this mask keep the vanilla (in-game-bit-driven) shop behavior.
      * ``sold``    -- of the managed badges, the ones already obtained
        (AP location checked or check-in-flight) -> show SOLD OUT.  A
        managed badge not in ``sold`` shows purchasable regardless of the
        in-game owned/purchased bits, so an AP-granted-but-unbought badge
        can still be bought to complete its shop check.

    Idempotent absolute-overwrite; safe to replay on HelloMsg / tick.
    Switch-side: applied directly on the rx thread via
    ``probe::setBadgeShopState`` (consumed by the computeItemStates
    trampoline).  ``managed == 0`` restores vanilla shop behavior.
    """

    T = "set_badge_shop_state"

    managed: int = 0
    sold: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "managed": self.managed, "sold": self.sold}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> SetBadgeShopStateMsg:
        managed = d.get("managed")
        sold = d.get("sold")
        for label, raw in (("managed", managed), ("sold", sold)):
            if not isinstance(raw, int) or isinstance(raw, bool):
                raise ProtocolError(
                    f"set_badge_shop_state.{label} must be int, got {raw!r}")
            if not (0 <= raw < (1 << 64)):
                raise ProtocolError(
                    f"set_badge_shop_state.{label} out of range "
                    f"[0, 2**64): {raw}")
        return cls(managed=managed, sold=sold)


@dataclass(frozen=True)
class SetBadgeShopTextMsg:
    """Bridge -> Switch.  AP shop-text: custom description for one shop badge
    (by internal_id), shown in the badge-shop detail panel to reflect the AP
    check the purchase would send (e.g. the scouted "<player>: <item>").

    One badge per message; empty ``text`` clears the override.  ``text`` is
    UTF-8 and capped at :data:`TEXT_CAP` bytes to match the Switch's
    ``WireSetBadgeShopText`` buffer.  Idempotent; replayed on HelloMsg.
    """

    T = "set_badge_shop_text"

    #: Must match kBadgeShopTextCap in switch-mod ApProtocol.hpp.
    TEXT_CAP: int = 160

    id: int = 0
    text: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "id": self.id, "text": self.text}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> SetBadgeShopTextMsg:
        raw_id = d.get("id")
        if not isinstance(raw_id, int) or isinstance(raw_id, bool):
            raise ProtocolError(
                f"set_badge_shop_text.id must be int, got {raw_id!r}")
        if not (0 <= raw_id < (1 << 32)):
            raise ProtocolError(
                f"set_badge_shop_text.id out of range [0, 2**32): {raw_id}")
        text = d.get("text")
        if not isinstance(text, str):
            raise ProtocolError(
                f"set_badge_shop_text.text must be str, got {text!r}")
        # The encoder truncates on send; reject only if a peer sends an
        # over-long payload (defensive -- the Switch buffer is fixed).
        if len(text.encode("utf-8")) >= cls.TEXT_CAP:
            raise ProtocolError(
                f"set_badge_shop_text.text exceeds {cls.TEXT_CAP} UTF-8 bytes")
        return cls(id=raw_id, text=text)


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
class SetWonderSeedsAbsoluteMsg:
    """Bridge -> Switch.  AP-authoritative per-course Wonder Seed
    bitfield sync (2026-05-29).

    ``bits_lo`` and ``bits_hi`` together carry an absolute 128-bit
    bitfield that the Switch writes to container-C hash ``0x60458608``.
    Bit N == Wonder Seed for course with internal index N.  Vanilla
    SMBW has ~81 courses so bits 0..80 are meaningful; bits 81..127 are
    reserved.

    Switch-side: ``ApFrameBridge::drainInbound`` dedups within a drain
    call (last-write-wins) and applies via
    ``probe::setWonderSeedBitfieldAbsolute(bits_lo, bits_hi)`` -- a
    direct overwrite of all 4 u32s in the underlying container-C
    storage.  Same idempotent absolute-overwrite pattern as
    :class:`SetBadgesAbsoluteMsg`.

    Sent by the bridge on three triggers:
      1. Every AP ``ReceivedItems`` update -- recompute bits + send.
      2. Every Switch ``HelloMsg`` (replay-on-reconnect) so the
         bitfield survives save/reload and Switch restarts.
      3. A periodic ~2 s tick to revert any in-game pickup (Wonder
         phase grab) that bypasses AP.

    Bit derivation lives in
    :meth:`SMBWContext._recompute_wonder_seed_bits`: 16 bits per world
    bucket (W1=bits 0..15, W2=16..31, ..., Special=112..127), with the
    lowest ``count_for_world`` bits set in each range.  Indices are
    NOT semantically tied to specific in-game courses yet; this
    primitive sets a deterministic AP-derived bitfield that the
    persistence-test workflow can verify against the saved file.
    Refining the bit-to-course mapping requires additional RE of
    FUN_71003D4110 (Murmur3 course-name -> course-index lookup).

    Range per field is ``[0, 2**64)``; the wire decoder accepts each
    half as an int64 (top bit reserved -- typical AP scenarios put
    0..16 bits per world bucket so the masks fit comfortably).
    """

    T = "set_wonder_seeds_absolute"

    bits_lo: int = 0
    bits_hi: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "bits_lo": self.bits_lo, "bits_hi": self.bits_hi}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> SetWonderSeedsAbsoluteMsg:
        raw_lo = d.get("bits_lo")
        raw_hi = d.get("bits_hi")
        if not isinstance(raw_lo, int) or isinstance(raw_lo, bool):
            raise ProtocolError(
                f"set_wonder_seeds_absolute.bits_lo must be int, got {raw_lo!r}")
        if not isinstance(raw_hi, int) or isinstance(raw_hi, bool):
            raise ProtocolError(
                f"set_wonder_seeds_absolute.bits_hi must be int, got {raw_hi!r}")
        if not (0 <= raw_lo < (1 << 64)):
            raise ProtocolError(
                f"set_wonder_seeds_absolute.bits_lo out of range "
                f"[0, 2**64): {raw_lo}")
        if not (0 <= raw_hi < (1 << 64)):
            raise ProtocolError(
                f"set_wonder_seeds_absolute.bits_hi out of range "
                f"[0, 2**64): {raw_hi}")
        return cls(bits_lo=raw_lo, bits_hi=raw_hi)


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

    Coalesce caveat: because the Switch reader does NOT observe the
    dirty buffer, back-to-back RMWs without an intervening save all
    read the same ``cur`` and clobber each other in the dirty queue --
    only the last delta survives.  The bridge therefore folds every
    pending ``send_increment_hash_keyed(hash, *)`` call into a single
    outbound message per hash before letting the writer drain (see
    ``LanServer._DrainIncrementsSentinel``).  Senders never need to
    debounce themselves.
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
class OverlayNoticeMsg:
    """Bridge -> Switch.  Force the on-Switch ImGui debug overlay visible
    (even while the bridge is connected) and display ``text`` for
    ``ttl_ms`` milliseconds.

    Used by the level-entry death-gate countdown
    (:meth:`...context.SMBWContext._gate_kill_loop`) so the player sees
    *why* they're about to be bounced -- a "Level not in logic" banner
    plus a per-second countdown -- instead of dying with no explanation.

    The Switch applies this on its network rx thread; it only writes the
    overlay's notice state, so unlike the grant messages it does NOT go
    through the game-thread inbound ring.  An empty ``text`` or
    ``ttl_ms <= 0`` clears any active notice; ``ttl_ms`` is clamped to
    [0, 60000] by the Switch decoder.
    """

    T = "overlay_notice"

    text: str
    ttl_ms: int

    # MUST match kOverlayTextCap in
    # switch-mod/src/ap/ApProtocol.hpp -- bump both together.
    TEXT_CAP = 192

    def to_wire(self) -> dict[str, Any]:
        return {
            "t": self.T,
            "text": self.text[: self.TEXT_CAP],
            "ttl_ms": int(self.ttl_ms),
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> OverlayNoticeMsg:
        return cls(
            text=_req_str(d, "text"),
            ttl_ms=int(d.get("ttl_ms", 0)),
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


@dataclass(frozen=True)
class LogMsg:
    """Switch -> Bridge.  A single SMBWAP_LOG_* line relayed from the
    Switch so it appears in the AP client's log panel without requiring
    a Ryujinx log tail.

    ``level`` is one of ``"debug"``, ``"info"``, ``"warn"``, ``"error"``.
    ``msg`` is the formatted body WITHOUT the ``"[smbwap x] "`` prefix.
    """

    T = "log"

    level: str = "info"
    msg: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.T, "level": self.level, "msg": self.msg}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> LogMsg:
        return cls(
            level=str(d.get("level", "info")),
            msg=str(d.get("msg", "")),
        )


@dataclass(frozen=True)
class ApplyWorldUnlockMsg:
    """Bridge -> Switch.  Open-world world/course unlock batch (2026-06).

    Sends the world-discovered / course-exists unlock hashes (all written
    with value=1) split by GameDataList category, because the two switch
    writers are not interchangeable (see world_unlock_table docstring):

      - ``hashes``       Int-category  -> ``probe::grantContainerACounter``
      - ``bool_hashes``  Bool-category -> ``probe::grantContainerBBool``

    Derived from a fresh→100%-save diff; full table in
    ``world_unlock_table`` (2 Int + 84 Bool as of 2026-06-09).  Per-course
    CLEAR flags are NOT included, so PlayReport checks still fire on first
    real course clear.

    Sent at connect (Connected handler) and on every HelloMsg replay
    (reconnect).  NOT on the periodic 2 s tick -- these flags are set once
    and are not reverted by in-game actions (unlike badges/seeds).
    """

    T = "apply_world_unlock"

    MAX_HASHES: ClassVar[int] = 96

    hashes: tuple[int, ...]
    bool_hashes: tuple[int, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {
            "t": self.T,
            "hashes": list(self.hashes),
            "bool_hashes": list(self.bool_hashes),
        }

    @classmethod
    def _check_hash_list(cls, d: dict[str, Any], field: str,
                         required: bool) -> tuple[int, ...]:
        raw = d.get(field)
        if raw is None and not required:
            return ()
        if not isinstance(raw, list):
            raise ProtocolError(
                f"apply_world_unlock.{field} must be list, got {raw!r}")
        if len(raw) > cls.MAX_HASHES:
            raise ProtocolError(
                f"apply_world_unlock.{field} too long: "
                f"{len(raw)} > {cls.MAX_HASHES}")
        hashes: list[int] = []
        for i, h in enumerate(raw):
            if not isinstance(h, int) or isinstance(h, bool):
                raise ProtocolError(
                    f"apply_world_unlock.{field}[{i}] must be int, got {h!r}")
            if not (0 <= h < (1 << 32)):
                raise ProtocolError(
                    f"apply_world_unlock.{field}[{i}] out of range "
                    f"[0, 2**32): {h}")
            hashes.append(h)
        return tuple(hashes)

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> ApplyWorldUnlockMsg:
        # "bool_hashes" is optional on decode (pre-split senders omit it).
        return cls(
            hashes=cls._check_hash_list(d, "hashes", required=True),
            bool_hashes=cls._check_hash_list(d, "bool_hashes", required=False),
        )


# Union of all message types -- handy for type hints on decoder return.
WireMsg = (
    HelloMsg
    | HelloAckMsg
    | NerveFireWireMsg
    | BadgeAcquiredWireMsg
    | CharBlockHitWireMsg
    | PlayReportWireMsg
    | SetBadgesAbsoluteMsg
    | SetRoyalSeedsAbsoluteMsg
    | SetRoutableWorldsAbsoluteMsg
    | SetForceClearedCoursesMsg
    | ApplyWorldUnlockMsg
    | SetItemGetDenyMaskMsg
    | SetUnlockedCharasMsg
    | SetBadgeShopStateMsg
    | SetBadgeShopTextMsg
    | GrantHashKeyedMsg
    | IncrementHashKeyedMsg
    | SetWonderSeedCountsMsg
    | SetWonderSeedsAbsoluteMsg
    | KillMsg
    | OverlayNoticeMsg
    | ErrMsg
    | PingMsg
    | PongMsg
    | LogMsg
)


# Registry of T -> from_wire constructor.  Adding a new message type
# means: define the dataclass with class-level ``T`` + ``to_wire`` +
# ``from_wire``, then append to this dict.
_FROM_WIRE: dict[str, Any] = {
    HelloMsg.T: HelloMsg.from_wire,
    HelloAckMsg.T: HelloAckMsg.from_wire,
    NerveFireWireMsg.T: NerveFireWireMsg.from_wire,
    BadgeAcquiredWireMsg.T: BadgeAcquiredWireMsg.from_wire,
    CharBlockHitWireMsg.T: CharBlockHitWireMsg.from_wire,
    PlayReportWireMsg.T: PlayReportWireMsg.from_wire,
    SetBadgesAbsoluteMsg.T: SetBadgesAbsoluteMsg.from_wire,
    SetRoyalSeedsAbsoluteMsg.T: SetRoyalSeedsAbsoluteMsg.from_wire,
    SetRoutableWorldsAbsoluteMsg.T: SetRoutableWorldsAbsoluteMsg.from_wire,
    SetForceClearedCoursesMsg.T: SetForceClearedCoursesMsg.from_wire,
    ApplyWorldUnlockMsg.T: ApplyWorldUnlockMsg.from_wire,
    SetItemGetDenyMaskMsg.T: SetItemGetDenyMaskMsg.from_wire,
    SetUnlockedCharasMsg.T: SetUnlockedCharasMsg.from_wire,
    SetBadgeShopStateMsg.T: SetBadgeShopStateMsg.from_wire,
    SetBadgeShopTextMsg.T: SetBadgeShopTextMsg.from_wire,
    GrantHashKeyedMsg.T: GrantHashKeyedMsg.from_wire,
    IncrementHashKeyedMsg.T: IncrementHashKeyedMsg.from_wire,
    SetWonderSeedCountsMsg.T: SetWonderSeedCountsMsg.from_wire,
    SetWonderSeedsAbsoluteMsg.T: SetWonderSeedsAbsoluteMsg.from_wire,
    KillMsg.T: KillMsg.from_wire,
    OverlayNoticeMsg.T: OverlayNoticeMsg.from_wire,
    ErrMsg.T: ErrMsg.from_wire,
    PingMsg.T: PingMsg.from_wire,
    PongMsg.T: PongMsg.from_wire,
    LogMsg.T: LogMsg.from_wire,
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
