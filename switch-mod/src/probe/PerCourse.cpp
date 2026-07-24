// M3.3 per-course bitfield writer + Wonder Seed gate override
// (Phase 2g port).
//
// PER-COURSE BITFIELDS (container D, mapped 2026-05-25):
// Per-course flag arrays (CourseClear/Normal at file offset 0x43F0,
// GoalSeed at 0x3348, CourseClear/Badge at 0x4438, etc.) live in a
// hash-keyed (hash, course_index) -> u32 bitmask container.  Each course
// type has its own hash; each u32 holds 1 bit per exit type for that
// course (bit 0 = normal pole, bit 1 = secret exit, ...).
//   * Reader: FUN_71000E258C  -- (gmd, uint32_t* out, hash, course_index)
//   * Writer: FUN_7101F2B354  -- (gmd, value, hash, course_index)
// Discovered via forward-trace of the M1 SetCourseClearFlagToGameData
// hook (NSO +0x1bf28cc); see docs/static-analysis-findings.md round 1/2.
//
// Bridge holds the canonical AP-authoritative mask and pushes the
// absolute set every ReceivedItems / HelloMsg / periodic tick -- same
// M4 follow-up #2 idempotent pattern as setBadgeBitfieldAbsolute.
//
// WONDER SEED GATE OVERRIDE (5-hash mirror group, mapped 2026-05-26):
// The Wonder Seed gate predicate at NSO +0x1787b40 reads container-A
// hash 0x390eb960 as the player's count.  4 other hashes update in
// lockstep with the count (per-current-world mirror group):
//   0x21f89ab1, 0x8c20ccb7, 0xeeff353b, 0x390eb960, 0xa0e5f253
// All reset to 0 on every world transition (W2->W3 wipe observed in run
// 09-34-58 at 03:30), so a one-shot write doesn't stick.  pushWonder-
// SeedOverride writes `value` to all 5 via the container-A writer; the
// _CurrentWorld variant looks up the player's world via hash 0x9f5ead3c,
// maps to the AP bucket, and uses that bucket's cached count.
//
// NOT WIRED INTO drainInbound IN PHASE 2G.  The legacy SetWonderSeed-
// Counts handler used to call pushWonderSeedOverrideCurrentWorld()
// but live-reproduced an abort inside FUN_710049F750 (container-A
// secondary insert during a scene transition; see ApFrameBridge.cpp
// notes 2026-05-26).  The implementations exist here for archaeology
// + a future ContainerAReader-side interceptor; the current bridge
// caches counts only.

#include <atomic>
#include <cstdint>

#include "ap/ApFrameBridge.hpp"
#include "probe/Gmd.hpp"
#include "util/Log.hpp"

namespace probe {

namespace {

using GmdSetPerCourseFn = void (*)(void* gmd, std::uint32_t value,
                                   std::uint32_t hash,
                                   std::uint32_t course_index);
using GmdSetCounterFn = void (*)(void* gmd, std::uint32_t value,
                                 std::uint32_t hash);
using GmdGetCounterFn = void (*)(void* gmd, std::uint32_t* out,
                                 std::uint32_t hash);

constexpr std::uint32_t kPerCourseWriterOffset = 0x01F2B354;
constexpr std::uint32_t kContainerAWriterOffset = 0x0049F648;
constexpr std::uint32_t kContainerAReaderOffset = 0x0012AE94;

// Per-current-world Wonder Seed mirror hashes.  Iteration #5 of the
// gate investigation pinned these as the 5 lockstep-updated container-A
// counters that all reflect the active world's seed count; the gate
// predicate at NSO +0x1787b40 reads 0x390eb960 directly.
constexpr std::uint32_t kWonderSeedMirrorHashes[5] = {
    0x21f89ab1u,
    0x8c20ccb7u,
    0xeeff353bu,
    0x390eb960u,  // gate-predicate reader
    0xa0e5f253u,
};

// In-game world index (read via container-A hash 0x9f5ead3c) ->
// AP bucket.  Keep in sync with the legacy table at
// switch-mod/src/program/main.cpp:1608-1618 AND with kWorldValToBucket
// inside whatever future ContainerAReader interceptor we install.
//   in-game val | meaning      | AP bucket
//   ------------+--------------+-----------
//   1           | W1           | 0
//   2           | Petal Isles  | 6  (yes, Petal Isles is val=2, not 7)
//   3           | W2           | 1
//   4           | W3           | 2
//   5           | W4           | 3
//   6           | W5           | 4
//   7           | W6           | 5
//   8           | "Castle"     | -1 (Bowser-related, aliased to slot 2
//                                    per .rodata internal-name table;
//                                    not seen on the player overworld)
//   9           | Special      | 7 (corrected 2026-05-28 from val=8;
//                                   evidence: live PlayReport world_no=9
//                                   on Special World course_in + .rodata
//                                   "Himitu"=9 in the internal name table)
constexpr std::int8_t kWorldValToBucket[10] = {
    -1,  // 0: unused
     0,  // 1: W1
     6,  // 2: Petal Isles
     1,  // 3: W2
     2,  // 4: W3
     3,  // 5: W4
     4,  // 6: W5
     5,  // 7: W6
    -1,  // 8: "Castle" (Bowser; not a player overworld)
     7,  // 9: Special World
};

}  // namespace

bool setPerCourseBitfieldAbsolute(std::uint32_t hash,
                                  std::uint32_t course_index,
                                  std::uint32_t bitmask) {
    void* gmd = gmdSingleton();
    if (gmd == nullptr) return false;

    // Container-D writes (FUN_7101F2B354 -> FUN_7101f2b140) push into the
    // gmd+0x788 deferred-write ring, which Aborts on overflow exactly like
    // container-A/B.  Refuse at >= 50% (kBackpressureRefusePctD) so a
    // parked-on-overworld backlog can never reach the Abort.  Idempotent
    // absolute-set callers retry on the next tick once drain catches up.
    const auto bp = checkContainerD();
    if (bp.refuse) {
        static std::atomic<std::int32_t> defer_budget{32};
        if (::smbwap::util::takeBudget(defer_budget)) {
            SMBWAP_LOG_WARN(
                "[backpressure] setPerCourseBitfieldAbsolute refused: "
                "qD at %u%% of cap (>= %u%%)  hash=0x%08x course=%u",
                bp.max_pct, kBackpressureRefusePctD, hash, course_index);
        }
        return false;
    }

    const auto fn = reinterpret_cast<GmdSetPerCourseFn>(
        mainBase() + kPerCourseWriterOffset);
    const auto q_before = readQueueD();
    fn(gmd, bitmask, hash, course_index);
    const auto q_after = readQueueD();

    static std::atomic<std::int32_t> log_budget{64};
    if (::smbwap::util::takeBudget(log_budget)) {
        SMBWAP_LOG_INFO(
            "SetPerCourseBitfield: hash=0x%08x course=%u value=0x%08x gmd=%p "
            "qD depth %u->%u / cap %u (%u%%)",
            hash, course_index, bitmask, gmd,
            q_before.depth(), q_after.depth(), q_after.cap,
            depthPct(q_after));
    }
    return true;
}

void pushWonderSeedOverride(std::uint32_t value) {
    void* gmd = gmdSingleton();
    if (gmd == nullptr) return;
    const auto fn = reinterpret_cast<GmdSetCounterFn>(
        mainBase() + kContainerAWriterOffset);
    for (std::uint32_t h : kWonderSeedMirrorHashes) {
        fn(gmd, value, h);
    }

    static std::atomic<std::int32_t> log_budget{8};
    if (::smbwap::util::takeBudget(log_budget)) {
        SMBWAP_LOG_INFO(
            "pushWonderSeedOverride: wrote value=%u to all 5 per-world hashes",
            value);
    }
}

void pushWonderSeedOverrideCurrentWorld() {
    // 2026-05-29 -- re-enabled with safety gates.  User hypothesis:
    // PR #40's crash inside FUN_710049F750 was caused by the bitfield-
    // counter inconsistency tripping an internal game assertion (not
    // the container-A writer call itself being unsafe).  Now that we
    // own the bitfield via SetWonderSeedsAbsolute, owning the counter
    // here keeps both consistent and the assertion should never fire.
    //
    // Belt-and-suspenders against the original race PR #40 also hit:
    //   * isSaveLoaded() -- gmd singleton fully deserialized
    //   * !isInSceneTransitionWindow() -- 3 s gate from the SceneTransition
    //     Nerve hook (vt 0x33fd9a8) so the game's scene-transition
    //     recompute has finished writing the mirror hashes before we
    //     overwrite them
    //   * checkContainerA() backpressure refusal -- avoids the dirty-
    //     queue overflow that aborts inside FUN_710049F750 when the
    //     ring is near cap
    void* gmd = gmdSingleton();
    if (gmd == nullptr) return;
    if (!isSaveLoaded()) return;
    if (isInSceneTransitionWindow()) return;

    const auto bp = checkContainerA();
    if (bp.refuse) {
        static std::atomic<std::int32_t> defer_budget{16};
        if (::smbwap::util::takeBudget(defer_budget)) {
            SMBWAP_LOG_WARN(
                "[backpressure] pushWonderSeedOverrideCurrentWorld refused: "
                "%s at %u%% of cap (>= %u%%)",
                bp.tightest_ring, bp.max_pct, kBackpressureRefusePct);
        }
        return;
    }

    const auto getfn = reinterpret_cast<GmdGetCounterFn>(
        mainBase() + kContainerAReaderOffset);
    std::uint32_t world_val = 0;
    getfn(gmd, &world_val, 0x9f5ead3c);
    if (world_val < 1 || world_val > 9) return;
    const std::int8_t mapped = kWorldValToBucket[world_val];
    if (mapped < 0) return;  // "Castle" (world_val=8) has no AP bucket.
    const std::uint32_t bucket = static_cast<std::uint32_t>(mapped);
    const std::uint32_t ap_count = smbwap::ap::getWonderSeedCount(bucket);
    pushWonderSeedOverride(ap_count);
}

}  // namespace probe
