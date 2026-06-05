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

bool grantContainerACounter(std::uint32_t hash, std::uint32_t value) {
    void* gmd = gmdSingleton();
    if (gmd == nullptr) return false;

    const auto bp = checkContainerA();
    if (bp.refuse) {
        static std::atomic<std::uint32_t> defer_budget{32};
        if (defer_budget.fetch_sub(1) > 0) {
            SMBWAP_LOG_WARN(
                "[backpressure] refused grantContainerACounter(hash=0x%08x, "
                "value=%u): %s at %u%% of cap (>= %u%%)",
                hash, value, bp.tightest_ring, bp.max_pct,
                kBackpressureRefusePct);
        }
        return false;
    }
    if (bp.warn) {
        static std::atomic<std::uint32_t> warn_budget{32};
        if (warn_budget.fetch_sub(1) > 0) {
            SMBWAP_LOG_WARN(
                "[backpressure] grantContainerACounter near cap: %s at %u%% "
                "(>= %u%%)",
                bp.tightest_ring, bp.max_pct, kBackpressureWarnPct);
        }
    }

    containerAWriter()(gmd, value, hash);

    static std::atomic<std::uint32_t> log_budget{16};
    if (log_budget.fetch_sub(1) > 0) {
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
        static std::atomic<std::uint32_t> warn_budget{32};
        if (warn_budget.fetch_sub(1) > 0) {
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

    static std::atomic<std::uint32_t> log_budget{16};
    if (log_budget.fetch_sub(1) > 0) {
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
