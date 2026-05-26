// Wire format mirror for the SMBW Switch <-> Bridge channel.
//
// Authoritative spec lives in bridge/wire.py; this file mirrors it on
// the Switch side.  Single persistent TCP connection, one '\n'-terminated
// line of UTF-8 JSON per message, "t" field is the discriminator, max
// line 8 KiB.
//
// Fixed-size char buffers throughout (NO std::string, NO std::vector)
// per the libstdc++-allocator hazard (subsdk9 allocator NULL-derefs
// once heap state drifts).

#pragma once

#include <cstdint>
#include <cstddef>

#include "../util/Json.hpp"

namespace smbwap::ap {

inline constexpr std::size_t kMaxLineBytes = 8 * 1024;

// Field caps. Sized to comfortably hold the max observed payload in each
// shape with 2x headroom.
inline constexpr std::size_t kModVerCap   = 32;
inline constexpr std::size_t kGameVerCap  = 16;
inline constexpr std::size_t kRoomCap     = 32;     // PlayReport room names ≤ ~20 chars
// Live play observed course_result payloads at 1575 bytes (M4 first run,
// 2026-05-24).  The M2 corpus undersold the upper bound -- bumped to 6 KiB
// to leave headroom for the M5 ten-coin / palace_result variants we
// haven't measured yet.  Hex-encoded 6 KiB = 12 KB which would blow past
// the 8 KiB line cap, BUT in practice payloads only approach 1.5-2 KiB
// (hex 3-4 KB, well under 8 KiB line cap).  The 6 KiB struct cap is just
// a defensive ceiling on the raw bytes we'll attempt to ship.
inline constexpr std::size_t kPayloadCap  = 6 * 1024;
inline constexpr std::size_t kReasonCap   = 256;
inline constexpr std::size_t kBridgeVerCap = 32;

// NerveKind discriminator -- wire form is the lowercase string ("wonder_seed_awarded").
// Defined here as integer constants for use in the tagged-union outbound event
// struct; the wire encoder maps to the string form in encodeNerveFire.
enum class NerveKind : std::uint8_t {
    WonderSeedAwarded = 0,
    CourseCleared = 1,
    DeathDetected = 2,
    // M3.7 -- one-shot Nerve "SetFlagEndDispMsgFirstVisitedWorldAfterClearedLastBoss"
    // (NSO+0x15b77a8, vtable +0x3363330).  Fires exactly once per save the
    // first time the player defeats final Bowser; the bridge translates
    // this into an AP ClientStatus.CLIENT_GOAL StatusUpdate.
    GameGoalReached = 3,
};
const char* toWire(NerveKind k);  // "wonder_seed_awarded" / ...

// ---- Outbound: Switch -> Bridge ----------------------------------------

struct WireHello {
    char mod_ver[kModVerCap] = {};
    char game_ver[kGameVerCap] = {};
    std::uint32_t pid = 0;
};

struct WireNerveFire {
    NerveKind kind = NerveKind::WonderSeedAwarded;
    std::uint32_t seq = 0;
};

// M2.3 -- outbound badge-acquisition detection.  Emitted by
// probe::setBadgeBitfieldAbsolute when it observes a bit set in the
// live container-C bitfield that's NOT in the AP-authoritative mask
// it's about to write (== the player picked up a badge in-game via
// Poplin shop / badge house / badge medley / badge challenge).  The
// bridge converts each fire into a "<Badge> Obtained" LocationCheck.
// `internal_id` is the bit position == SMBW internal badge id.
struct WireBadgeAcquired {
    std::uint32_t internal_id = 0;
    std::uint32_t seq = 0;
};

struct WirePlayReport {
    char room[kRoomCap] = {};
    std::uint16_t payload_len = 0;
    std::uint8_t payload[kPayloadCap] = {};
};

struct WirePing {
    std::int64_t ts_ms = 0;
};

// Tagged union of outbound events -- one element of the outbound SPSC
// ring.  Fixed-size POD; safe to memcpy across the thread boundary.
enum class OutboundKind : std::uint8_t {
    None = 0,
    Hello = 1,
    NerveFire = 2,
    PlayReport = 3,
    Ping = 4,
    BadgeAcquired = 5,
};

struct OutboundEvent {
    OutboundKind kind = OutboundKind::None;
    union {
        WireHello hello;
        WireNerveFire nerve;
        WirePlayReport play_report;
        WirePing ping;
        WireBadgeAcquired badge_acquired;
    };
    // Default-construct the inactive members; the consumer reads via `kind`.
    OutboundEvent() : kind(OutboundKind::None), hello{} {}
};

void encodeHello(util::json::LineBuffer& out, const WireHello& msg);
void encodeNerveFire(util::json::LineBuffer& out, const WireNerveFire& msg);
void encodeBadgeAcquired(util::json::LineBuffer& out, const WireBadgeAcquired& msg);
void encodePlayReport(util::json::LineBuffer& out, const WirePlayReport& msg);
void encodePing(util::json::LineBuffer& out, const WirePing& msg);
void encodePong(util::json::LineBuffer& out, std::int64_t ts_ms);

// Dispatch helper: encode whichever variant `ev.kind` selects.  Returns false
// if the kind is None / unknown (caller logs + drops).
bool encodeOutbound(util::json::LineBuffer& out, const OutboundEvent& ev);

// ---- Inbound: Bridge -> Switch -----------------------------------------

// AP-authoritative badge sync.  `bits` is the absolute desired contents of
// SMBW's container-C owned-badge bitfield (hash 0x105df820, 128 bits in
// live memory but only the low 64 carry real badge ownership; the high 64
// is a live mirror written by setBadgeBitfieldAbsolute).  Bit N == owned
// badge with internal_id N.  Bridge sends this on every ReceivedItems
// update, on every HelloAck (replay-on-reconnect), and on a periodic
// ~2 s tick so any in-game pickup (Poplin shop, badge house) gets
// reverted to AP's view within seconds.  Replaces the M3.2 per-bit
// GrantBadge primitive; AP is now the sole authority over the badge pool.
struct WireSetBadgesAbsolute {
    std::uint64_t bits = 0;
};

// M3.3 / M3.3b -- container-A counter writer grant.  `hash` is the SMBW
// save-data field hash (e.g. 0x55815859 for GRAND_SEED_WORLD1); `value` is
// the new counter value (FUN_710049F648 is a setter, not an incrementer,
// and truncates u32 -> u8 internally for typed-bool slots).  The full u32
// range is valid on the wire; the bridge enforces application-level
// sensible values (e.g. value=1 for Royal Seeds).
struct WireGrantHashKeyed {
    std::uint32_t hash = 0;
    std::uint32_t value = 0;
};

// Saturating add/sub on a container-A counter.  Sibling of
// WireGrantHashKeyed; differs in semantic: the Switch reads the current
// counter via FUN_710012AE94 (NSO +0x0012AE94, signature
// `(gmd, uint32_t* out, uint32_t hash)`), computes saturating(cur + delta)
// in [0, 2**32), and writes the sum via FUN_710049F648.  `delta` is
// signed (i32 range) so the bridge can express both grants (positive,
// e.g. +10 per "10 Coin" item received) and refunds (negative, e.g.
// -10 per TEN_COIN check fired by an in-game pickup, so AP stays the
// sole authority over what each 10-coin block actually awards).
// Saturates at 0 on underflow; writer truncates per-slot internally
// (u8/u16) so even a delta that overflows u16 lands cleanly.
struct WireIncrementHashKeyed {
    std::uint32_t hash = 0;
    std::int32_t  delta = 0;
};

// M3.3 -- Container-D (per-course bitfield) absolute write.  Container D
// holds (hash, course_index) -> u32 bitmask entries; each bit represents
// an exit type cleared for that course.  Set bit 0 of GoalSeed/Normal's
// course_index 0 = "Wonder Seed earned at normal exit of W1-1", etc.
//
// Writer is FUN_7101F2B354 (NSO +0x01F2B354, signature
// `(gmd, value, hash, course_index)`); reader is FUN_71000E258C.  Discovered
// 2026-05-25 via forward-trace of the M1 SetCourseClearFlagToGameData hook
// (see docs/static-analysis-findings.md M3.3 round-1/round-2 sections).
//
// AP-authoritative pattern: bridge holds canonical (hash, course_index)
// masks and pushes absolute sets on every ReceivedItems / HelloMsg /
// periodic tick (mirrors M4 follow-up #2 badge sync).  Idempotent.  Bitmask
// is u32 because each per-course slot is a u32 in the save file.
//
// The `hash` field's full set of valid values isn't known statically -- they
// live in the course descriptor structs and surface only as probe trampoline
// output.  GmdContainerDWriter trampoline in main.cpp logs every fire so
// the bridge can populate its hash table from observed values.
struct WireSetPerCourseBitfield {
    std::uint32_t hash = 0;
    std::uint32_t course_index = 0;
    std::uint32_t bitmask = 0;
};

// M3.3 -- Container-C single-bit set/clear.  Generic version of the badge
// bitfield primitive that walks container-C for any `hash` and sets/clears
// bit `bit_index` in the underlying u32[] storage.  Used for the per-course
// Wonder Phase seed flag (container-C bitfield `0xb9bd745d`, one bit per
// course, discovered 2026-05-25 via the dump_C trampoline catching the seed
// pickup write at 00:02:18 of the Hoppos clear run).
//
// Unlike SetBadgesAbsolute which OVERWRITES the entire u64 to AP's mask,
// this primitive toggles ONE bit at a time -- Wonder Seeds have a real
// in-game pickup path that we don't want to revert.  The bridge ORs in AP
// grants on top of the player's progress by calling once per granted seed.
//
// `value` is u8 carrying boolean (1 = set, 0 = clear); the wire decoder
// accepts any non-negative int and treats nonzero as set.
struct WireSetContainerCBit {
    std::uint32_t hash = 0;
    std::uint32_t bit_index = 0;
    std::uint8_t  value = 0;
};

// M3.3 Phase B verification probe -- dump a SaveDataField at the given byte
// offset within a singleton struct.  Singleton ptr is read from
// NSO+`base_nso_offset`; field is at `(deref_singleton + field_offset)`.
// Logs the assumed 48-byte SaveDataField header (vtable, type_desc,
// count/state, data_ptr, +0x20, +0x28) plus the first 81 u32s at data_ptr
// if it looks like a valid heap address.  Read-only; used to verify the
// SaveDataField layout hypothesis before shipping the AP-authoritative
// Wonder Seed overwrite primitive.
//
// Known singleton bases:
//   0x3632e88   sead-managed save-data sub-singleton (0x3428 bytes -- too
//               small to hold per-course Wonder Phase seeds at offset 0x3AF8)
//   0x363f0f0   gmd::GameDataMgr::sInstance (large; expected primary base)
struct WireDumpSaveField {
    std::uint32_t base_nso_offset = 0;
    std::uint32_t field_offset = 0;
};

// AP-authoritative Royal Seed sync (2026-05-26).  Same idempotent
// absolute-overwrite pattern as WireSetBadgesAbsolute: the bridge holds
// the canonical 6-bit mask of which Royal Seeds AP has granted (bit
// 0 = W1, bit 1 = W2, ..., bit 5 = W6) and pushes it on every
// ReceivedItems / HelloMsg / periodic ~2 s tick.  The Switch dispatch
// in ApFrameBridge.cpp loops the 6 container-B bool hashes (see
// kRoyalSeedHashes; same order as bridge/royal_seed_table.py
// ROYAL_SEED_HASHES) and calls probe::grantContainerBBool(hash,
// (mask >> bit) & 1) for each.  This BOTH grants AP-granted seeds and
// clears any seed the player obtained in-game without AP releasing the
// matching item (palace clear that ran ahead of AP).
//
// Range is [0, 2**6) -- only the 6 documented Royal Seed bits.  The
// completion bools COMPLETE_GAME (0x5D3EC9B4) and INTRO_CUTSCENE_-
// COMPLETED (0x89F1CC52) are deliberately out of scope -- not AP
// items, player keeps them once earned.
struct WireSetRoyalSeedsAbsolute {
    std::uint8_t mask = 0;
};

// AP-authoritative Wonder Seed gate override.  Carries an 8-element
// array of cumulative Wonder Seed counts received from AP, indexed by
// world bucket (W1=0..W6=5, Petal Isles=6, Special=7).  The Switch
// caches the array and ticks at NerveActivateOnce cadence (~2 s under
// normal play): reads container-A hash 0x9f5ead3c (live current-world
// index), maps to bucket, calls probe::pushWonderSeedOverride with the
// bucket's count.  That writes the value to the 5 mirror hashes
// including 0x390eb960 -- the one the gate predicate FUN_71001787b40
// reads -- making AP the sole authority over Wonder Seed gating.
//
// Idempotent absolute-overwrite (same pattern as WireSetBadgesAbsolute).
// Sent on every ReceivedItems, every HelloMsg, and a periodic ~2 s tick.
//
// See [wonder_seed_table.py](../../../apworld/smbw_archipelago/client/wonder_seed_table.py)
// for the bucket convention.
inline constexpr std::size_t kWorldCount = 8;
struct WireSetWonderSeedCounts {
    std::uint32_t counts[kWorldCount] = {};
};

// M3.8 -- DeathLink inbound apply.  Sent when AP bounces a DeathLink for
// our slot.  `source` is the originating AP slot name; `cause` is the
// free-form reason (typically "mario_died").  Sizes MUST match KillMsg
// caps in bridge/wire.py (SOURCE_CAP=48, CAUSE_CAP=128) -- if you bump
// one, bump the other.  Game thread drains this from drainInbound and
// calls probe::synthKill which writes 0 to live_base + 0x1C.
inline constexpr std::size_t kKillSourceCap = 48;
inline constexpr std::size_t kKillCauseCap  = 128;
struct WireKill {
    char source[kKillSourceCap] = {};
    char cause[kKillCauseCap]   = {};
};

struct WireHelloAck {
    bool ok = false;
    int wire_ver = 0;
    char bridge_ver[kBridgeVerCap] = {};
    char reason[kReasonCap] = {};
};

struct WireErr {
    char reason[kReasonCap] = {};
};

struct WirePong {
    std::int64_t ts_ms = 0;
};

enum class InboundKind : std::uint8_t {
    None = 0,
    HelloAck = 1,
    SetBadgesAbsolute = 2,
    Err = 3,
    Pong = 4,
    GrantHashKeyed = 5,
    Kill = 6,
    IncrementHashKeyed = 7,
    SetPerCourseBitfield = 8,
    SetContainerCBit = 9,
    DumpSaveField = 10,
    SetWonderSeedCounts = 11,
    SetRoyalSeedsAbsolute = 12,
};

struct InboundMsg {
    InboundKind kind = InboundKind::None;
    union {
        WireHelloAck hello_ack;
        WireSetBadgesAbsolute set_badges_absolute;
        WireErr err;
        WirePong pong;
        WireGrantHashKeyed grant_hash_keyed;
        WireKill kill;
        WireIncrementHashKeyed increment_hash_keyed;
        WireSetPerCourseBitfield set_per_course_bitfield;
        WireSetContainerCBit set_container_c_bit;
        WireDumpSaveField dump_save_field;
        WireSetWonderSeedCounts set_wonder_seed_counts;
        WireSetRoyalSeedsAbsolute set_royal_seeds_absolute;
    };
    InboundMsg() : kind(InboundKind::None), hello_ack{} {}
};

// Parse one wire line (no trailing newline, or with one -- decoder
// trims).  Returns true on success and fills `out`; false on any malformed
// input (caller logs + drops + keeps the TCP connection alive).
//
// `data` must be writable -- the JSON Reader decodes escape sequences in
// place.  Caller passes the recv buffer directly.
bool decodeInbound(char* data, std::size_t len, InboundMsg& out);

// ---- Small utility -----------------------------------------------------

// Copy a C-string into a fixed buffer, null-terminating.  Null src -> empty.
template <std::size_t N>
inline void copyFixed(char (&dst)[N], const char* src) {
    if (!src) { dst[0] = '\0'; return; }
    std::size_t i = 0;
    while (i + 1 < N && src[i] != '\0') {
        dst[i] = src[i];
        ++i;
    }
    dst[i] = '\0';
}

// Length-bounded variant for source spans that aren't null-terminated.
template <std::size_t N>
inline void copyFixedN(char (&dst)[N], const char* src, std::size_t n) {
    const std::size_t take = (n < N - 1) ? n : (N - 1);
    for (std::size_t i = 0; i < take; ++i) dst[i] = src[i];
    dst[take] = '\0';
}

}  // namespace smbwap::ap
