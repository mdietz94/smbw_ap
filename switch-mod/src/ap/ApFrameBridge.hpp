// Glue between game-thread hook callbacks and the LAN client's SPSC rings.
//
// All `enqueue*` functions push onto the outbound ring; they are safe to
// call from any thread (the ring is lock-free).
//
// `drainInbound()` pops from the inbound ring and applies each grant.
// It MUST run on the game thread because probe::setBadgeBitfieldAbsolute
// is a direct memory write to the live `gmd` container with no
// synchronization (see CLAUDE.md "M3.2 badge-grant" section).
//
// Wired into main.cpp's NerveActivateOnce::Callback and
// SetCourseClearFlagExecute::Callback at the top of each.

#pragma once

#include <cstddef>
#include <cstdint>

#include "ApProtocol.hpp"

namespace smbwap::ap {

// M3.3b bool-typed hash keys.  Container-A writer (FUN_710049F648) is
// typed and silently no-ops on these slots (live-falsified 2026-05-25
// with hash 0x55815859); they must route through the container-B bool
// writer (FUN_710049EA24) via probe::grantContainerBBool instead.
// Shared between drainInbound() dispatch and the M3.3b boot smoke
// test in NerveActivateOnce::Callback.
inline constexpr std::uint32_t kBoolHashes[] = {
    0x55815859,  // GRAND_SEED_WORLD1
    0x49ABBA86,  // GRAND_SEED_WORLD2
    0xB550D8D6,  // GRAND_SEED_WORLD3
    0x1DCF7F6E,  // GRAND_SEED_WORLD4
    0x0D5A3E00,  // GRAND_SEED_WORLD5
    0xD4660D2B,  // GRAND_SEED_WORLD6
    0x5D3EC9B4,  // COMPLETE_GAME
    0x89F1CC52,  // INTRO_CUTSCENE_COMPLETED
};

constexpr bool isBoolHash(std::uint32_t h) {
    for (auto x : kBoolHashes) if (x == h) return true;
    return false;
}

// Royal Seed hashes in mask-bit order: index N is the bit-N position
// in WireSetRoyalSeedsAbsolute::mask.  Bit 0 = W1, ..., bit 5 = W6.
// This ordering is part of the wire contract -- it MUST match
// ``ROYAL_SEED_HASHES`` in
// apworld/smbw_archipelago/client/royal_seed_table.py.
//
// Subset of kBoolHashes: COMPLETE_GAME / INTRO_CUTSCENE_COMPLETED are
// deliberately omitted because they aren't AP items and the player
// should keep them once earned.
inline constexpr std::uint32_t kRoyalSeedHashes[] = {
    0x55815859,  // bit 0 -- W1 Royal Seed
    0x49ABBA86,  // bit 1 -- W2 Royal Seed
    0xB550D8D6,  // bit 2 -- W3 Royal Seed
    0x1DCF7F6E,  // bit 3 -- W4 Royal Seed
    0x0D5A3E00,  // bit 4 -- W5 Royal Seed
    0xD4660D2B,  // bit 5 -- W6 Royal Seed
};
inline constexpr std::size_t kRoyalSeedCount =
    sizeof(kRoyalSeedHashes) / sizeof(kRoyalSeedHashes[0]);

// Push a Nerve fire onto the outbound ring.  Safe to call from any
// thread including the worker.  Returns false if the ring is full
// (event is dropped, caller may log).
bool enqueueNerveFire(NerveKind kind, std::uint32_t seq);

// M2.3 -- push an in-game badge-acquisition event onto the outbound
// ring.  Called from probe::setBadgeBitfieldAbsolute for each bit that
// was set in the live state but not in the AP-authoritative mask
// (== a Poplin shop / badge house / badge medley / badge challenge
// pickup the bridge should report as a LocationCheck).  `seq` is
// auto-assigned from a private per-message-kind counter so log
// correlation stays simple even if Nerve fires and badge acquisitions
// interleave.
bool enqueueBadgeAcquired(std::uint32_t internal_id);

// Character-block sanity -- push a CHAR_BLOCK_HIT event onto the outbound
// ring.  Called from the GetDamageReactionPlayerNo hook every time that
// AI-node body resolves the damage invoker to a local player slot 0-3.
// `player_slot` is 0-3; `chara` is the hitting PlayerCharaType (0-11) or
// -1 if unresolved (the bridge then falls back to the course's character);
// `x`/`y`/`z` are the block world position (zeros in v1).  `seq` is
// auto-assigned from a private per-message-kind counter for log
// correlation.  Safe to call from the game thread.
bool enqueueCharBlockHit(std::uint32_t player_slot, std::int32_t chara,
                         float x, float y, float z);

// Push a PlayReport capture onto the outbound ring.  `room` is a
// null-terminated event-id string; `payload` is the already-serialized
// CBOR-ish payload bytes captured by the IPC SaveReport hook.  Truncates
// to kPayloadCap with a log line.
bool enqueuePlayReport(const char* room,
                       const void* payload, std::size_t payload_len);

// Relay a SMBWAP_LOG_* line to the PC bridge.  `level` is one of
// "debug", "info", "warn", "error"; `msg` is the formatted body
// WITHOUT the "[smbwap x] " prefix.  Safe to call from any thread.
// No-ops silently when disconnected or when the ring is full.
// MUST NOT call SMBWAP_LOG_* internally -- would recurse via util::log().
void enqueueLog(const char* level, const char* msg);

// Game-thread-only.  Pops every queued inbound message and applies it.
// Called from the top of NerveActivateOnce::Callback and
// SetCourseClearFlagExecute::Callback.
void drainInbound();

// Read the AP-authoritative Wonder Seed count for world bucket `bucket`
// (0 = W1, 1 = W2, ..., 5 = W6, 6 = Petal Isles, 7 = Special).  Returns
// 0 if the bucket is out of range or no SetWonderSeedCountsMsg has
// arrived yet.  Source of truth is a static atomic array updated by
// drainInbound; safe to call from any thread (but in practice called
// from the game-thread NerveActivateOnce tick that drives
// probe::pushWonderSeedOverride).
std::uint32_t getWonderSeedCount(std::uint32_t bucket);

// Open-world mode (2026-06): bit position of the Castle/Bowser route in
// the routable-world mask.  Worlds W1..W6 occupy bits 0..5, Petal Isles
// bit 6, Special bit 7; the Castle is bit 8.  Mirrors
// wire.SetRoutableWorldsAbsoluteMsg.CASTLE_BIT on the client.
inline constexpr std::uint32_t kCastleMaskBit = 8;

// Read the AP-authoritative routable-world mask cached by drainInbound on
// SetRoutableWorldsAbsolute.  Bit N (AP-bucket order) set == that world is
// routable from the start; bit kCastleMaskBit set == Castle/Bowser route
// open.  0 == open-world inactive (the FUN_7100935ce0 hook no-ops).  Safe
// from any thread; read from the game-thread predicate trampoline.
std::uint32_t getRoutableWorldMask();

// Read the AP-authoritative "force IsInClearedCourse" mask cached by
// drainInbound on SetForceClearedCourses.  Bit N set == the Nth secret-exit
// replay course (kForceClearedCourses in main.cpp) should have
// IsInClearedCourse forced true at scene-load so its secret path spawns.
// 0 == nothing to force (the SceneTransition hook no-ops).  Safe from any
// thread; read from the game-thread SceneTransition hook.
std::uint32_t getForceClearedCoursesMask();

}  // namespace smbwap::ap

// Forward declarations for the grant primitives that live in main.cpp's
// probe namespace.  All have external linkage so ApFrameBridge.cpp can
// resolve them at link time.
namespace probe {
// AP-authoritative badge sync.  Overwrites the entire container-C owned-
// badge bitfield (hash 0x105df820) to the absolute value `bits`.  Bit N
// = owned badge with internal_id N.  Replaces the M3.2 per-bit
// grantBadgeBit primitive (deleted).
bool setBadgeBitfieldAbsolute(std::uint64_t bits);

// 2026-06-10 -- force-unequip AP-disabled badges.  Companion to
// setBadgeBitfieldAbsolute: clearing the OWNED bitfield does NOT clear
// the EQUIPPED field, so a level/shop that grants+auto-equips an
// AP-disabled badge still shows it equipped/available.  This walks both
// equipped-badge EnumArrays (EquipBadgeSave.BadgeId 0xcfba9bf8 saved,
// CoursePlayerEquipBadge.BadgeId 0xf30cb2e2 per-level) and resets any
// slot referencing a badge NOT in `owned_mask` (bit == internal_id) to
// the "Invalid" sentinel.  Called on the same triggers as
// setBadgeBitfieldAbsolute (ReceivedItems / HelloMsg / ~2 s tick).
// Returns true if at least one equip field was located.
bool clearEquippedBadgesNotOwned(std::uint64_t owned_mask);

// 2026-05-29 -- AP-authoritative per-course Wonder Seed bitfield sync.
// Overwrites the entire 128-bit container-C bitfield at hash 0x60458608
// to the absolute value (bits_lo, bits_hi).  Bit N = Wonder Seed for
// course with internal index N.  Vanilla SMBW has ~81 courses so bits
// 0..80 are meaningful; bits 81..127 are reserved.  Same idempotent
// absolute-overwrite triggers as badges: ReceivedItems, HelloMsg
// (replay-on-reconnect), and the periodic ~2 s tick.
bool setWonderSeedBitfieldAbsolute(std::uint64_t bits_lo,
                                   std::uint64_t bits_hi);

// M3.3 -- container-A counter writer.  Calls FUN_710049F648 via
// function pointer with the live gmd singleton.  Used for counters
// (flower_coin, regular_coin); typed and silently no-ops on bool
// slots, so the GrantHashKeyed dispatch routes bool hashes through
// grantContainerBBool instead.
bool grantContainerACounter(std::uint32_t hash, std::uint32_t value);

// Pure read of a container-A scalar (Int/Enum) by hash via FUN_710012ae94.
// Returns 0 when gmd isn't live or the hash isn't in container-A.  No
// dirty-queue write -> safe from the SceneTransition hook.  Used to identify
// the current course (world_val 0x9f5ead3c + CourseInfo.CourseId 0xdf82e9ab)
// for the open-world secret-exit unlock.
std::uint32_t readContainerAValue(std::uint32_t hash);

// Open-world seed: raise a container-A counter to `floor` only when the
// live value is below it (never lowers, preserving a higher value).  Used
// to seed the player with purple coins at open-world start.
bool ensureContainerACounterFloor(std::uint32_t hash, std::uint32_t floor);

// Saturating add/sub on a container-A counter.  Reads the current value
// via FUN_710012AE94 (NSO +0x0012AE94, signature
// `(gmd, uint32_t* out, uint32_t hash)`) and writes `saturating(cur +
// delta)` back via FUN_710049F648.  Delta is signed: positive grants
// (e.g. +10 per "10 Coin" item received), negative refunds (e.g. -10
// per TEN_COIN check fired by an in-game pickup).  Saturates at 0 on
// underflow; writer truncates per-slot internally (u8/u16).
bool incrementContainerACounter(std::uint32_t hash, std::int32_t delta);

// M3.3b -- container-B bool writer.  Calls FUN_710049EA24 (the
// high-level wrapper, NSO +0x0049EA24) which gates on the gmd+0x68
// init/lock and delegates to FUN_7101F263FC(gmd+8, value & 1, hash)
// -- the deferred-write bool setter for the gmd+8 substruct.  Used
// for Royal Seeds, COMPLETE_GAME, INTRO_CUTSCENE_COMPLETED.  The
// existing GmdBoolWriter trampoline at NSO +0x01F263FC will log
// every call for free observability.
bool grantContainerBBool(std::uint32_t hash, std::uint32_t value);

// M3.3 -- container-D (per-course bitfield) absolute writer.  Calls
// FUN_7101F2B354 (NSO +0x01F2B354, signature
// `(gmd, value, hash, course_index)`).  Used for per-course CourseClear /
// GoalSeed / WonderSeed bitfields where each bit corresponds to one exit
// type.  Bridge holds the canonical mask and pushes absolute sets on
// ReceivedItems / HelloMsg / periodic tick -- same AP-authoritative
// pattern as setBadgeBitfieldAbsolute.
bool setPerCourseBitfieldAbsolute(std::uint32_t hash,
                                  std::uint32_t course_index,
                                  std::uint32_t bitmask);

// M3.3 -- container-C single-bit set/clear.  Generic version of
// setBadgeBitfieldAbsolute's direct-memory-write pattern: walks container-C
// (gmd+0x80) for the given `hash`, sets/clears bit `bit_index` in the
// underlying uint32_t[] storage.  Used for the per-course Wonder Phase
// seed flag (container-C bitfield 0xb9bd745d).  Unlike SetBadgesAbsolute,
// this toggles ONE bit and never reverts in-game pickups -- the bridge
// ORs in AP grants on top of player progress by calling once per granted
// seed.  Returns false if hash unknown or bit_index >= 128.
bool setContainerCBit(std::uint32_t hash, std::uint32_t bit_index, bool value);

// M3.3 Phase B verification probe.  Dereferences the singleton pointer at
// NSO+`base_nso_offset`, walks to the SaveDataField at `field_offset`
// within that struct, and logs the assumed 48-byte SaveDataField header
// plus the first 81 u32s at data_ptr.  Read-only; no game-state mutation.
// `base_nso_offset` lets us test multiple candidate singletons without
// rebuilds (0x3632e88 = sub-singleton, 0x363f0f0 = gmd::sInstance).
bool dumpSaveField(std::uint32_t base_nso_offset, std::uint32_t field_offset);

// M3.8 -- inbound DeathLink apply.  Writes 0 to the HP int16 at
// live_base + 0x38 and arms the synthetic-death loop guard so the
// outbound DEATH_DETECTED echo gets suppressed in main.cpp's nerve
// callback.  Returns false if the player isn't currently in a killable
// state (live_base unset or stale -- menu / world-map / scene teardown);
// the caller should then arm requestPendingDeathLink() to retry.
bool synthKill();

// M3.8 -- queue an inbound DeathLink for retry.  Call when synthKill()
// returned false: serviceDeathLink (per-frame player-tick hook) fires the
// synthetic kill on the next killable frame, or expires the request after
// ~30 s.  Avoids dropping deaths that arrive while in a menu / transition.
void requestPendingDeathLink();

// M4.5 save-loaded gate.  drainInbound checks this before applying any
// grant -- pre-save-select the gmd singleton points at title-screen
// data (or is null), and our grants would land in the wrong container
// and be wiped on save load.  Set on first observed call to the
// GmdContainerAWriter or GmdBoolWriter trampolines (game's save
// deserializer is the first caller; our own grant code only runs
// after this gate is open, so the first observed write is always
// game-initiated).
bool isSaveLoaded();

// Scene-transition gate.  Returns true while the elapsed delta from
// the last SceneTransition Nerve fire (vt_off 0x33fd9a8: death, course
// entry/exit, world-map, palace, Poplin shop, post-Wonder-Seed cleanup)
// is below kSceneTransitionGateTicks (3 s @ 19.2 MHz).  drainInbound
// uses this as a top-level skip: all container writers
// (FUN_710049F648 container-A, FUN_710049EA24/FUN_71001F263FC
// container-B, setBadgeBitfieldAbsolute container-C, FUN_7101F2B354
// container-D, synthKill HP write) race with game-natural writes
// during transitions.  All bridge grants are idempotent
// absolute-overwrite or AP-replayed-on-next-tick, so deferring during
// the window costs at most ~3 s of staleness with no progress loss.
bool isInSceneTransitionWindow();

// Iteration #5 (2026-05-26) — Wonder Seed gate override smoke test.
// Writes `value` to all 5 per-current-world Wonder Seed count hashes
// (0x21f89ab1, 0x8c20ccb7, 0xeeff353b, 0x390eb960, 0xa0e5f253) via the
// container-A counter writer.  Called periodically from NerveActivateOnce
// to keep gates passable as long as the override is active.  See the
// definition in main.cpp for the hypothesis under test.
void pushWonderSeedOverride(std::uint32_t value);

// Active push for the player's current world.  Reads the current-world
// index from container-A hash 0x9f5ead3c, maps to AP bucket, and writes
// that bucket's cached AP count (from g_wonder_seed_counts) to all 5
// mirror hashes.  Called from drainInbound on every SetWonderSeedCounts
// so AP grants take effect immediately, without waiting for the game to
// re-write the mirror hashes on the next area transition.
void pushWonderSeedOverrideCurrentWorld();

// 2026-05-29 -- PERSISTENCE: write each AP world's per-course Wonder Seed
// bitmask into the LIVE container-D (gmd+0x800) so the recomputed per-world
// count survives save/reload.  For each AP bucket reads the cached count
// (smbwap::ap::getWonderSeedCount) and encodes it as `count` low-bits across
// `count` distinct course slots of that world's regular-seed hash (1 bit per
// slot -> popcount sum == count; bit 0 of each course is always a valid
// seed, dodging the per-course serializer strip), zeroing the regular tail
// and the entire special-seed hash.  Direct write via findContainerDData --
// no deferred queue, no Abort.  Diffs against the last written count per
// world so a steady state is a no-op.  AP-authoritative: overwrites the
// game's per-course seed detail (cosmetic) to make the world total match AP.
// Defined in SeedTrace.cpp.
void pushWonderSeedContainerDCounts();

// 2026-06-04 -- OPEN-WORLD COURSE VISIBILITY: write bit 0 of the FlowerLock
// per-course route bitfield (container-D hash 0x948e540d, confirmed via NSO
// static analysis of FUN_71000E258C @ NSO+0xE258C) for every course index
// 0..80.  The FlowerLock container at gmd+0x800 controls which course nodes
// appear on the world map and in the teleport list.  A fresh world has all
// FlowerLock slots at 0 -> no course nodes draw, empty teleport list.
// Writing 1 to each slot marks every route "open" so all 81 course nodes
// appear; the worldRoutableHook (main.cpp) already restricts which world maps
// are accessible, so no extra scoping by world is needed.  Direct live write
// like pushWonderSeedContainerDCounts -- no deferred queue, no Abort.
// Write-if-zero so existing game-set values (non-zero) are preserved.
// Only runs when g_routable_world_mask != 0 (open-world mode active).
// Defined in SeedTrace.cpp.
void pushFlowerLockUnlock();
}
