// Shared helpers for the probe:: namespace (Phase 2g).
//
// The probe:: M3 grant primitives operate on the live SMBW save state
// reached via the `gmd::GameDataMgr` singleton (NSO +0x363F0F0).  Several
// of them also share the M3.8 scene-transition gate window and the M4.5
// save-loaded latch.  Centralized here so all probe sources include one
// header.
//
// Direct-function-pointer calls into NSO (e.g. FUN_710049F648 the
// container-A writer) use `mainBase() + offset` cast to the appropriate
// function-pointer type.  Where a Phase 2d trampoline already sits on the
// offset (e.g. GmdContainerAWriter @ +0x49F648) the call routes through
// the trampoline, producing free observability of every grant.  This
// matches the legacy exlaunch pattern documented at
// switch-mod/src/program/main.cpp:1511-1515.
//
// There is NO sail-resolved symbol path for these anonymous game
// functions -- they have no SDK mangling and only exist as NSO offsets
// that the M3 static-analysis sprint pinned down.  See
// docs/static-analysis-findings.md for the full list.

#pragma once

#include <cstdint>

#include "hk/ro/RoModule.h"
#include "hk/ro/RoUtil.h"

namespace probe {

// NSO offset of the gmd::GameDataMgr::sInstance qword anchor (decompiled
// 2026-05-24, see CLAUDE.md "GameDataMgr (gmd::) save-data API").
//
// Dereferencing this gives the live GameDataMgr*.  Returns 0 (null) until
// the save deserializer runs (first save-select); see [[smbwap-hakkun-
// migration]] gates discussion.  All grant primitives must null-check.
inline constexpr std::uint32_t kGmdSInstanceNsoOffset = 0x0363F0F0;

inline std::uintptr_t mainBase() {
    const auto* mod = hk::ro::getMainModule();
    return mod ? mod->range().start() : 0;
}

inline void* gmdSingleton() {
    const std::uintptr_t base = mainBase();
    if (base == 0) return nullptr;
    return *reinterpret_cast<void**>(base + kGmdSInstanceNsoOffset);
}

// -----------------------------------------------------------------------
// Deferred-write queue depth observability (2026-05-27).
//
// The container-A and container-B writers (`FUN_710049F648` and
// `FUN_710049EA24 -> FUN_71001F263FC`) both push into bounded lock-free
// MPSC ring buffers stored on the GameDataMgr instance.  Once the ring
// head reaches its capacity the next push hits `FUN_7100472CF0` which
// is marked `do_not_return` and reaches `nn::diag::detail::Abort` --
// see [docs/static-analysis-findings.md](../../docs/static-analysis-findings.md)
// "2026-05-24 -- FUN_710049F648 decompiled" and the matching user-pasted
// decompile of `FUN_7101F26A5C` / `FUN_7101F263FC` (2026-05-27).
//
// Three rings exist; ALL are bounded and ALL share the head-overflow
// abort:
//
//   ringA_primary   -- container-A main dirty queue; reached when the
//                      hash already lives in the main bucket array at
//                      gmd+0xe0.  Substruct base = gmd+0xc8; absolute
//                      offsets ring=gmd+0xf8, head=gmd+0x100, cap=gmd+0xf0.
//
//   ringA_secondary -- container-A "insert new entry" path reached when
//                      the hash is NOT yet in the main bucket.  This is
//                      the ring that overflowed in the 2026-05-26
//                      level-entry abort (FUN_710049F750+0x0).  Substruct
//                      base = gmd+0x128; head=substruct+0x38,
//                      cap=substruct+0x28.
//
//   ringB           -- container-B bool deferred-write queue; substruct
//                      base = gmd+0x8.  This is the ring that the
//                      2026-05-27 9-hour SetRoyalSeedsAbsolute(mask=0)
//                      session aborted on (FUN_71001F263FC+0xF8).
//
// The head word is split: low 20 bits are the slot index (`& 0xfffff`),
// the high 12 bits are an ABA-generation counter for the LDXR/STXR
// reservation loop (`& 0xf0000000`, increments by 0x100000 on each
// publish).
//
// All reads here are racy-but-safe: producer threads bump the head with
// atomic LDXR/STXR pairs; we just observe.  Use `volatile` to keep the
// compiler from hoisting across trampoline boundaries.
// -----------------------------------------------------------------------

struct QueueDepth {
    std::uint32_t head{0};  // raw head word: low 20 bits = slot idx, high 12 = gen
    std::uint32_t cap{0};

    std::uint32_t depth() const { return head & 0xfffffu; }
    std::uint32_t gen() const { return (head >> 20) & 0xfffu; }
};

inline QueueDepth readQueueAt(std::size_t head_off, std::size_t cap_off) {
    auto* gmd = gmdSingleton();
    if (gmd == nullptr) return {};
    auto* bytes = reinterpret_cast<volatile std::uint8_t*>(gmd);
    return {
        *reinterpret_cast<volatile std::uint32_t*>(bytes + head_off),
        *reinterpret_cast<volatile std::uint32_t*>(bytes + cap_off),
    };
}

// Container-A main dirty queue.  Head = gmd+0x100, cap = gmd+0xf0.
inline QueueDepth readQueueA_primary() {
    return readQueueAt(0x100, 0xf0);
}

// Container-A "insert new entry" path.  Substruct at gmd+0x128.
// Head = gmd+0x160, cap = gmd+0x150.  This is the ring that overflowed
// in the 2026-05-26 level-entry abort.
inline QueueDepth readQueueA_secondary() {
    return readQueueAt(0x128 + 0x38, 0x128 + 0x28);
}

// Container-B bool deferred-write queue.  Substruct at gmd+0x8.
// Head = gmd+0x40, cap = gmd+0x30.
inline QueueDepth readQueueB() {
    return readQueueAt(0x8 + 0x38, 0x8 + 0x28);
}

// -----------------------------------------------------------------------
// Writer backpressure.
//
// Refuse container-A/B writer calls once the relevant ring's depth crosses
// `kBackpressureRefusePct` of cap; warn (rate-limited) once it crosses
// `kBackpressureWarnPct`.
//
// Why bother given the 2026-05-27 observability session showed peak depths
// of only 2-5 slots out of caps 140/510?  Because that session was 6
// minutes of active gameplay, where the game's drain worker tracks
// production easily.  The historical 9 h afk pattern from 5ca82aa
// (15,294 redundant container-B writes) accumulated because the bridge
// keeps producing the periodic-tick triplet every 2 s even when the game
// is parked in a state that doesn't tick its save flusher (file-select,
// pause menu, world-map idle, focused-out window).  This gate puts a
// hard floor below the abort regardless of game-state-dependent drain
// behavior.
//
// For idempotent absolute-overwrite kinds (SetBadgesAbsolute,
// SetRoyalSeedsAbsolute), the bridge resends every 2 s, so a refused
// write naturally retries once drain catches up.  Increments are
// non-idempotent, but the observed near-zero depth means a refusal there
// requires the queue to first reach catastrophic territory under some
// unknown future code path -- if that happens, the bridge needs a
// retry-on-false story for IncrementHashKeyed; we'll surface that as a
// separate problem rather than silently letting the abort happen.
// -----------------------------------------------------------------------

inline constexpr std::uint32_t kBackpressureWarnPct = 50;
inline constexpr std::uint32_t kBackpressureRefusePct = 80;

struct BackpressureCheck {
    bool warn{false};
    bool refuse{false};
    std::uint32_t max_pct{0};
    const char* tightest_ring{"<none>"};
};

inline std::uint32_t depthPct(const QueueDepth& q) {
    if (q.cap == 0) return 0;
    return (q.depth() * 100u) / q.cap;
}

inline void evaluateBackpressure(BackpressureCheck& out,
                                  std::uint32_t pct,
                                  const char* ring_name) {
    if (pct > out.max_pct) {
        out.max_pct = pct;
        out.tightest_ring = ring_name;
    }
}

// Both container-A rings -- a write to a hash already in the main bucket
// queues into the primary; a write to a hash not yet in the main bucket
// (rare in steady state, but the historical 2026-05-26 level-entry abort
// path) queues into the secondary.  We don't know which path a given
// (gmd, hash) takes without re-implementing the writer's bucket probe,
// so we honor the tighter of the two.
inline BackpressureCheck checkContainerA() {
    BackpressureCheck r{};
    evaluateBackpressure(r, depthPct(readQueueA_primary()), "qA");
    evaluateBackpressure(r, depthPct(readQueueA_secondary()), "qA2");
    r.warn   = r.max_pct >= kBackpressureWarnPct;
    r.refuse = r.max_pct >= kBackpressureRefusePct;
    return r;
}

inline BackpressureCheck checkContainerB() {
    BackpressureCheck r{};
    evaluateBackpressure(r, depthPct(readQueueB()), "qB");
    r.warn   = r.max_pct >= kBackpressureWarnPct;
    r.refuse = r.max_pct >= kBackpressureRefusePct;
    return r;
}

}  // namespace probe
