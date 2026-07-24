// M3.3 container-A counter writer + saturating increment (Phase 2g port).
//
// FUN_710049F648 at NSO +0x0049F648 is the universal container-A counter
// SET function (recovered by static-analysis sprint 2, 2026-05-24).
// Signature:
//     void FUN_710049F648(GameDataMgr*, uint32_t value, uint32_t hash);
// Lock-free + thread-safe via ARM exclusive-monitor atomics on the dirty
// queue at gmd->[+0xf8].  Deferred-write -- the value is queued and applied
// to the persistent container at next save.
//
// FUN_710012AE94 at NSO +0x0012AE94 is the corresponding READER (signature
// `(gmd, uint32_t* out, uint32_t hash)`).  Pairs with the writer to provide
// the read-modify-write increment.
//
// We call both directly by NSO offset cast -- no separate trampoline
// needed.  Note that NSO +0x49F648 is ALSO the install address of
// main.cpp's gmdContainerAWriterHook (Phase 2d), so our calls land at the
// patched branch instruction and route through the trampoline's lambda
// before reaching .orig.  That intentionally produces a gmd.A_writer log
// line on every grant (free observability).  Same pattern as the legacy
// exlaunch implementation (switch-mod/src/program/main.cpp:1511-1515).
//
// FALSIFIED 2026-05-25 for bool slots: calling FUN_710049F648 with a
// container-B bool-typed hash (Royal Seed, COMPLETE_GAME, INTRO) lands
// but the post-save value stays 0.  The writer is typed per-slot.
// drainInbound dispatch in ap/ApFrameBridge.cpp branches on isBoolHash
// to route bool grants through grantContainerBBool instead.

#include <atomic>
#include <cstdint>

#include "probe/Gmd.hpp"
#include "util/Log.hpp"

namespace probe {

namespace {

using GmdSetCounterFn = void (*)(void* gmd, std::uint32_t value, std::uint32_t hash);
using GmdGetCounterFn = void (*)(void* gmd, std::uint32_t* out, std::uint32_t hash);

constexpr std::uint32_t kContainerAWriterOffset = 0x0049F648;
constexpr std::uint32_t kContainerAReaderOffset = 0x0012AE94;

// Scalar-ENUM reader.  FUN_71003D3FB0 at NSO +0x003D3FB0, signature
//     bool FUN_71003D3FB0(uint32_t hash, int32_t* out_index);
// (note: hash FIRST, out second -- the opposite order from the container-A
// reader above).  Enum-category flags do NOT live in container A; this
// walks the Enum hash table at gmd+0x260 (values at gmd+0x258, stride
// 0x38, field +0x1c), falling back to the EnumArray table at gmd+0x2c0.
//
// It does NOT return the stored value.  The stored value is a murmur3
// name-hash; the function passes it to FUN_71003D4110 @ +0x003D4110, which
// linear-scans the 81-entry course-name string table at
// PTR_s_Course1_71034dec90, murmur3-ing each name until one matches, and
// writes that entry's INDEX.
//
// *** OFF-BY-ONE TRAP ***  That string table starts at "Course1", so
// index 0 == "Course1" -- it is NOT the same indexing as GameDataList's
// RawValues, whose index 0 is "Invalid".  Verified by reading the live
// table: index 10 -> "Course11", index 60 -> "Course61".  So a CourseId
// enum name "CourseN" resolves to index N-1.  Comparing against the
// GameDataList ordinal (or against the raw name-hash) is silently wrong.
//
// Derived 2026-07-21 from the game's own read site in the course-clear
// nerve FUN_7101BF28CC, which does exactly
//     FUN_710059F894(&h, 0); FUN_71003D3FB0(0xdf82e9ab, &h); FUN_71005E93FC(&h);
constexpr std::uint32_t kEnumScalarReaderOffset = 0x003D3FB0;

using GmdGetEnumIndexFn = bool (*)(std::uint32_t hash, std::int32_t* out_index);

GmdGetEnumIndexFn enumScalarReader() {
    return reinterpret_cast<GmdGetEnumIndexFn>(
        mainBase() + kEnumScalarReaderOffset);
}

GmdSetCounterFn containerAWriter() {
    return reinterpret_cast<GmdSetCounterFn>(mainBase() + kContainerAWriterOffset);
}

GmdGetCounterFn containerAReader() {
    return reinterpret_cast<GmdGetCounterFn>(mainBase() + kContainerAReaderOffset);
}

// Per-hash shadow table for incrementContainerACounter -- mirrors the
// legacy comment block at switch-mod/src/program/main.cpp:1656-1687.
// The dirty queue at gmd+0xf8 is write-only from the reader's
// perspective; getfn at +0x12AE94 reads the *persistent* container.
// Two quick RMW calls both read the same persistent value and each
// writes cur+delta to the queue -- the second clobbers the first
// unless we cache our own effective value.  Self-resets when the
// persistent value diverges from what we last saw (= save flush
// happened OR the game wrote to the same hash via setfn).
//
// 8 slots is ample: AP-targeted counter hashes are <=2 today
// (flower_coin, regular_coin).  Linear scan + relaxed atomics --
// concurrent access could see torn shadow fields but the worst case
// is one lost-update RMW, no worse than no shadow.
struct IncrementShadow {
    std::uint32_t hash;             // 0 means slot is empty
    std::uint32_t last_persistent;  // what getfn returned at last call
    std::uint32_t shadow_value;     // what we wrote (effective live total)
};
constexpr std::size_t kShadowSlots = 8;
IncrementShadow s_increment_shadow[kShadowSlots] = {};

}  // namespace

std::uint32_t readContainerAValue(std::uint32_t hash) {
    // Pure read of a container-A **Int** scalar by hash via the
    // persistent-container reader FUN_710012ae94.  Returns 0 when gmd isn't
    // live yet or the hash isn't in container-A (a miss leaves out=0).  No
    // dirty-queue write, so no backpressure / scene-transition concern --
    // safe to call from the SceneTransition hook.  Used for the live world
    // index (0x9f5ead3c, GameDataList category Int).
    //
    // NOT for Enum-category flags.  It silently returns 0 for them -- that
    // was the CourseId (0xdf82e9ab) bug: Enum flags live in a different
    // container entirely.  Use readEnumIndex() below.
    void* gmd = gmdSingleton();
    if (gmd == nullptr) return 0;
    std::uint32_t out = 0;
    containerAReader()(gmd, &out, hash);
    return out;
}

std::int32_t readEnumIndex(std::uint32_t hash) {
    // Read an Enum-category flag, returning the resolved name-table INDEX,
    // or -1 if gmd isn't live / the hash isn't an Enum / the stored
    // name-hash matched no table entry.
    //
    // For CourseId (0xdf82e9ab) the index is over the course-name table
    // whose entry 0 is "Course1" -- so enum name "CourseN" => index N-1.
    // See the kEnumScalarReaderOffset comment for the derivation and the
    // off-by-one trap.
    if (gmdSingleton() == nullptr) return -1;
    std::int32_t out = -1;
    if (!enumScalarReader()(hash, &out)) return -1;
    return out;
}

bool grantContainerACounter(std::uint32_t hash, std::uint32_t value) {
    void* gmd = gmdSingleton();
    if (gmd == nullptr) return false;

    const auto bp = checkContainerA();
    if (bp.refuse) {
        static std::atomic<std::int32_t> defer_budget{32};
        if (::smbwap::util::takeBudget(defer_budget)) {
            SMBWAP_LOG_WARN(
                "[backpressure] refused grantContainerACounter(hash=0x%08x, "
                "value=%u): %s at %u%% of cap (>= %u%%)",
                hash, value, bp.tightest_ring, bp.max_pct,
                kBackpressureRefusePct);
        }
        return false;
    }
    if (bp.warn) {
        static std::atomic<std::int32_t> warn_budget{32};
        if (::smbwap::util::takeBudget(warn_budget)) {
            SMBWAP_LOG_WARN(
                "[backpressure] grantContainerACounter near cap: %s at %u%% "
                "(>= %u%%)",
                bp.tightest_ring, bp.max_pct, kBackpressureWarnPct);
        }
    }

    containerAWriter()(gmd, value, hash);

    static std::atomic<std::int32_t> log_budget{16};
    if (::smbwap::util::takeBudget(log_budget)) {
        SMBWAP_LOG_INFO(
            "GrantHashKeyed: hash=0x%08x value=%u gmd=%p",
            hash, value, gmd);
    }
    return true;
}

bool ensureContainerACounterFloor(std::uint32_t hash, std::uint32_t floor) {
    // Raise a container-A counter to `floor` only when the live value is
    // below it (absolute set via grantContainerACounter); never lowers, so
    // a higher gameplay/AP value is preserved.  Used once at open-world
    // start to seed the player with purple coins -- some worlds gate their
    // first area on spending purple coins that a walk-/fast-in player lacks.
    void* gmd = gmdSingleton();
    if (gmd == nullptr) return false;
    std::uint32_t cur = 0;
    containerAReader()(gmd, &cur, hash);
    if (cur >= floor) return false;
    return grantContainerACounter(hash, floor);
}

bool incrementContainerACounter(std::uint32_t hash, std::int32_t delta) {
    void* gmd = gmdSingleton();
    if (gmd == nullptr) return false;

    const auto bp = checkContainerA();
    if (bp.refuse) {
        // Non-idempotent path -- refusing here loses the delta because
        // the bridge doesn't re-enqueue on false return.  The 2026-05-27
        // observability session showed peak qA depth of 2/140 in 6 min
        // of active play, so a refusal here means we're already in
        // catastrophic territory under some unknown code path; better to
        // surface that with a CRITICAL warning than abort the game.  If
        // this fires in practice, add a bridge-side retry path for
        // IncrementHashKeyed.
        SMBWAP_LOG_ERROR(
            "[backpressure] CRITICAL: refused incrementContainerACounter("
            "hash=0x%08x, delta=%d): %s at %u%% of cap -- delta LOST; "
            "bridge does not re-enqueue IncrementHashKeyed",
            hash, static_cast<int>(delta),
            bp.tightest_ring, bp.max_pct);
        return false;
    }
    if (bp.warn) {
        static std::atomic<std::int32_t> warn_budget{32};
        if (::smbwap::util::takeBudget(warn_budget)) {
            SMBWAP_LOG_WARN(
                "[backpressure] incrementContainerACounter near cap: %s at "
                "%u%% (>= %u%%)",
                bp.tightest_ring, bp.max_pct, kBackpressureWarnPct);
        }
    }

    const auto getfn = containerAReader();
    const auto setfn = containerAWriter();

    std::uint32_t cur_persistent = 0;
    getfn(gmd, &cur_persistent, hash);

    IncrementShadow* slot = nullptr;
    IncrementShadow* free_slot = nullptr;
    for (auto& s : s_increment_shadow) {
        if (s.hash == hash) { slot = &s; break; }
        if (s.hash == 0 && free_slot == nullptr) free_slot = &s;
    }

    std::uint32_t effective;
    bool used_shadow = false;
    if (slot != nullptr && slot->last_persistent == cur_persistent) {
        // Queue not flushed since last write -- persistent reader still
        // shows the pre-our-write value.  Trust our shadow.
        effective = slot->shadow_value;
        used_shadow = true;
    } else {
        effective = cur_persistent;
        if (slot != nullptr && slot->last_persistent != cur_persistent
            && slot->shadow_value != cur_persistent) {
            SMBWAP_LOG_DEBUG(
                "IncrementHashKeyed: hash=0x%08x shadow reset "
                "(persistent=%u was last_seen=%u shadow=%u)",
                hash, cur_persistent,
                slot->last_persistent, slot->shadow_value);
        }
    }

    std::uint32_t next;
    if (delta >= 0) {
        const std::uint32_t udelta = static_cast<std::uint32_t>(delta);
        next = effective + udelta;  // u32 wrap; writer truncates per-slot
    } else {
        const std::uint32_t udec =
            static_cast<std::uint32_t>(-static_cast<std::int64_t>(delta));
        next = (effective >= udec) ? (effective - udec) : 0u;  // saturate
    }
    setfn(gmd, next, hash);

    if (slot == nullptr) slot = free_slot;
    if (slot != nullptr) {
        slot->hash = hash;
        slot->last_persistent = cur_persistent;
        slot->shadow_value = next;
    }

    static std::atomic<std::int32_t> log_budget{16};
    if (::smbwap::util::takeBudget(log_budget)) {
        SMBWAP_LOG_INFO(
            "IncrementHashKeyed: hash=0x%08x persistent=%u effective=%u "
            "(%s) delta=%d -> %u gmd=%p",
            hash, cur_persistent, effective,
            used_shadow ? "shadow" : "persistent",
            static_cast<int>(delta), next, gmd);
    }
    return true;
}

}  // namespace probe
