// Observability hooks + write-path primitives for the Wonder Seed RE re-open
// (2026-05-28).  Header doc + rationale in SeedTrace.hpp; the plan and
// success criteria for each hook are in
// [docs/wonder-seed-re-reopen-2026-05-28.md](../../../docs/wonder-seed-re-reopen-2026-05-28.md).

#include "probe/SeedTrace.hpp"

#include <atomic>
#include <cstdint>

#include "hk/hook/Trampoline.h"
#include "hk/ro/RoModule.h"

#include "ap/ApFrameBridge.hpp"   // probe::setContainerCBit
#include "probe/Gmd.hpp"
#include "util/Log.hpp"

namespace {

// ---------------------------------------------------------------------------
// Hash filter constants.
// ---------------------------------------------------------------------------

// Per-course Wonder Seed bitfield hash.  Documented in
// docs/static-analysis-findings.md (iteration 7, 2026-05-26):
//   - Read via FUN_7100124134(gmd, &out, 0x60458608, bit_index)
//   - Written via FUN_710049EA24(gmd, value, 0x60458608, bit_index) -- the
//     4-arg overload claim, NOT yet verified live.
// One bit per course; ~81 courses, so the storage occupies <= 3 u32s.
constexpr std::uint32_t kWonderSeedBitfieldHash = 0x60458608u;

// Adjacent per-course bitfield discovered alongside 0x60458608 (per
// docs/static-analysis-findings.md "additional confirmed facts" 2026-05-26).
// Believed to hold per-course course-state bits (cleared/uncleared / top-of-
// flag / etc).  Worth logging too so we can disambiguate.
constexpr std::uint32_t kPerCourseStateHash = 0x580b7eb4u;

// NSO offsets of the three trampoline targets.
constexpr std::uint32_t kContainerBWrapperOffset = 0x0049EA24;
constexpr std::uint32_t kPerCourseBitReaderOffset = 0x00124134;
constexpr std::uint32_t kPerCourseBitfieldWriterOffset = 0x01F2B354;

// ---------------------------------------------------------------------------
// Distinct-tuple dedup (lock-free, bounded).
//
// All three trampolines fire orders of magnitude more often than is useful
// for log analysis (the per-course bit reader fires on every world-map state
// recompute, dozens of calls per second).  We log:
//   * the first N fires unconditionally
//   * every previously-unseen (hash, course_index) tuple after that
//   * every N-th fire as a heartbeat
// The 64-slot table is per-hook.  Slots store a packed (hash << 32) |
// (idx & 0xffffffff) so we can dedup with relaxed atomics.
// ---------------------------------------------------------------------------

struct DistinctTable {
    std::atomic<std::uint64_t> slots[64];
    std::atomic<std::uint32_t> count;
};

DistinctTable g_bwrap_seen{};
DistinctTable g_reader_seen{};
DistinctTable g_pcwriter_seen{};

// Returns true iff this is the first time we've observed (hash, idx) in `t`.
// Stores up to 64 distinct tuples; subsequent novel tuples beyond cap are
// silently dropped (the heartbeat fallback still surfaces them periodically).
bool noteDistinct(DistinctTable& t,
                  std::uint32_t hash, std::uint32_t idx) {
    const std::uint64_t key =
        (static_cast<std::uint64_t>(hash) << 32)
        | static_cast<std::uint64_t>(idx & 0xffffffffu);
    const std::uint32_t n = t.count.load(std::memory_order_acquire);
    for (std::uint32_t i = 0; i < n && i < 64; ++i) {
        if (t.slots[i].load(std::memory_order_relaxed) == key) return false;
    }
    const std::uint32_t slot = t.count.fetch_add(1, std::memory_order_acq_rel);
    if (slot >= 64) return false;
    t.slots[slot].store(key, std::memory_order_release);
    return true;
}

// ---------------------------------------------------------------------------
// Container-D queue-depth sampler (2026-05-29).
//
// Logs the gmd+0x788 deferred-write ring depth on each new high-water mark
// and every ~1024th call, so a normal play session reveals whether the
// queue backs up -- and WHEN.  Hypothesis (per user): like the container-A/B
// overflow, it only accumulates while parked on the overworld (the save
// flusher doesn't tick there); entering a course drains it.  Pure
// observation -- no writes.  This is what tells us whether AP-driven
// container-D writes are safe behind the 50% backpressure gate.
// ---------------------------------------------------------------------------
void sampleContainerDQueue(const char* via) {
    const auto q = probe::readQueueD();
    if (q.cap == 0) return;
    const std::uint32_t pct = probe::depthPct(q);
    static std::atomic<std::uint32_t> s_max{0};
    static std::atomic<std::uint32_t> s_calls{0};

    std::uint32_t prev = s_max.load(std::memory_order_relaxed);
    bool new_max = false;
    while (pct > prev) {
        if (s_max.compare_exchange_weak(prev, pct,
                                        std::memory_order_relaxed)) {
            new_max = true;
            break;
        }
    }
    const std::uint32_t n = s_calls.fetch_add(1, std::memory_order_relaxed);
    if (new_max || (n & 0x3FFu) == 0) {
        SMBWAP_LOG_INFO(
            "[seed-persist] qD depth=%u cap=%u pct=%u%% (max=%u%%) via=%s",
            q.depth(), q.cap, pct,
            new_max ? pct : s_max.load(std::memory_order_relaxed), via);
    }
}

// ---------------------------------------------------------------------------
// Hook A: ContainerBWrapper4Arg @ NSO +0x49EA24.
//
// Currently called by probe::grantContainerBBool() as 3-arg
// (gmd, value, hash) -- per CLAUDE.md "GameDataMgr (gmd::) save-data API"
// the function gates on gmd+0x68 lock and delegates to FUN_7101F263FC.
// The 4-arg overload claim (docs/static-analysis-findings.md iteration 7,
// 2026-05-26) is that for hash 0x60458608 the function also reads w3 as a
// bit_index for the per-course bitfield.  This trampoline logs all four
// register slots on every call so we can:
//
//   1. Detect game-natural calls with hash == 0x60458608 (which would
//      confirm the game has a code path that writes the per-course seed
//      bitfield via this wrapper -- presumably during a Wonder Seed grab).
//   2. Validate the w3 register usage: if game-natural calls show
//      bit_index in (0..160] for hash == 0x60458608 but garbage values
//      for the documented bool hashes (Royal Seeds, COMPLETE_GAME,
//      INTRO), the 4-arg overload hypothesis holds and
//      `setPerCourseWonderSeedBit` will work.
// ---------------------------------------------------------------------------
HkTrampoline<void, void*, std::uint32_t, std::uint32_t, std::uint32_t>
    containerBWrapper4ArgHook = hk::hook::trampoline(
        [](void* gmd, std::uint32_t value, std::uint32_t hash,
           std::uint32_t w3) -> void {
            // First-fire one-shot gmd dump (the writer is one of the first
            // things the save deserializer calls; this is a cheap moment
            // to snapshot the substruct layout).
            probe::dumpGmdSubstructsOnce("gmd.B_wrapper_4arg");
            probe::dumpSeedPersistenceMapOnce();

            static std::atomic<std::uint32_t> s_fires{0};
            const std::uint32_t fire =
                s_fires.fetch_add(1, std::memory_order_relaxed);

            // Always log Wonder Seed bitfield / per-course state hash hits
            // -- those are what we're looking for and they're rare.
            const bool is_target =
                (hash == kWonderSeedBitfieldHash)
                || (hash == kPerCourseStateHash);

            const bool is_new = noteDistinct(g_bwrap_seen, hash, w3);

            if (is_target || is_new || fire < 64
                || (fire & 0x7FFu) == 0) {
                SMBWAP_LOG_INFO(
                    "[seed-trace] B_wrap fire=%u hash=0x%08x val=%u w3=%u "
                    "gmd=%p new=%d target=%d",
                    fire, hash, value, w3, gmd,
                    is_new ? 1 : 0, is_target ? 1 : 0);
            }
            containerBWrapper4ArgHook.orig(gmd, value, hash, w3);
        });

// ---------------------------------------------------------------------------
// Hook B: PerCourseBitReader @ NSO +0x124134.
//
// FUN_7100124134(gmd, out*, hash, bit_index) -- per-course bit reader.
// 55+ xrefs in the binary; we filter to hash == 0x60458608 (Wonder Seed
// bitfield) and hash == 0x580b7eb4 (adjacent per-course state) only.
// Result of out_bit is captured AFTER the orig call so we see the
// game's view of the bit's current value.
// ---------------------------------------------------------------------------
HkTrampoline<std::uint64_t, void*, std::uint8_t*, std::uint32_t,
             std::uint32_t>
    perCourseBitReaderHook = hk::hook::trampoline(
        [](void* gmd, std::uint8_t* out_bit, std::uint32_t hash,
           std::uint32_t bit_index) -> std::uint64_t {
            const auto rc =
                perCourseBitReaderHook.orig(gmd, out_bit, hash, bit_index);
            probe::dumpSeedPersistenceMapOnce();
            sampleContainerDQueue("D_read");
            // Write-test disabled: superseded by the real persistence path
            // (probe::pushWonderSeedContainerDCounts, driven by AP's
            // SetWonderSeedCounts in drainInbound).  Leaving it off so boots
            // don't modify the save.
            const bool is_target =
                (hash == kWonderSeedBitfieldHash)
                || (hash == kPerCourseStateHash);
            if (!is_target) return rc;

            static std::atomic<std::uint32_t> s_fires{0};
            const std::uint32_t fire =
                s_fires.fetch_add(1, std::memory_order_relaxed);
            const bool is_new = noteDistinct(g_reader_seen, hash, bit_index);
            if (is_new || fire < 64 || (fire & 0xFFu) == 0) {
                SMBWAP_LOG_INFO(
                    "[seed-trace] D_read fire=%u hash=0x%08x bit=%u value=%u "
                    "rc=%llu gmd=%p new=%d",
                    fire, hash, bit_index,
                    out_bit ? *out_bit : 0xFFu,
                    static_cast<unsigned long long>(rc), gmd,
                    is_new ? 1 : 0);
            }
            return rc;
        });

// ---------------------------------------------------------------------------
// Hook C: PerCourseBitfieldWriter @ NSO +0x1F2B354.
//
// FUN_7101F2B354(gmd, value, hash, course_index) -- the documented per-
// course u32 bitmask writer used by `probe::setPerCourseBitfieldAbsolute`
// for course-clear / goal-seed bitmasks.  Trampoline logs every call so
// we can see what the game writes during a Wonder Seed grab (which hash,
// which course_index, what value).  Especially: if `value` for hash
// 0x60458608 is a u32 bitmask, the storage scheme is "u32 per course"
// rather than "1 bit per course in a global field" -- correcting our
// hypothesis.
// ---------------------------------------------------------------------------
HkTrampoline<void, void*, std::uint32_t, std::uint32_t, std::uint32_t>
    perCourseBitfieldWriterHook = hk::hook::trampoline(
        [](void* gmd, std::uint32_t value, std::uint32_t hash,
           std::uint32_t course_index) -> void {
            static std::atomic<std::uint32_t> s_fires{0};
            const std::uint32_t fire =
                s_fires.fetch_add(1, std::memory_order_relaxed);
            const bool is_target =
                (hash == kWonderSeedBitfieldHash)
                || (hash == kPerCourseStateHash);
            const bool is_new = noteDistinct(g_pcwriter_seen, hash, course_index);
            if (is_target || is_new || fire < 64 || (fire & 0x1FFu) == 0) {
                SMBWAP_LOG_INFO(
                    "[seed-trace] D_write fire=%u hash=0x%08x course=%u "
                    "value=0x%08x gmd=%p new=%d target=%d",
                    fire, hash, course_index, value, gmd,
                    is_new ? 1 : 0, is_target ? 1 : 0);
            }
            sampleContainerDQueue("D_write");
            perCourseBitfieldWriterHook.orig(gmd, value, hash, course_index);
        });

void installHook(const char* name, std::uint32_t offset, hk::Result rc) {
    if (rc.failed()) {
        SMBWAP_LOG_ERROR(
            "[seed-trace] install %s @ +0x%x FAILED rc=0x%x",
            name, offset, static_cast<unsigned>(rc.getValue()));
    } else {
        SMBWAP_LOG_INFO("[seed-trace] install %s @ +0x%x OK", name, offset);
    }
}

}  // namespace

namespace probe {

void installSeedTraceHooks() {
    installHook("ContainerBWrapper4Arg",
                kContainerBWrapperOffset,
                containerBWrapper4ArgHook.installAtMainOffset(
                    kContainerBWrapperOffset));
    installHook("PerCourseBitReader",
                kPerCourseBitReaderOffset,
                perCourseBitReaderHook.installAtMainOffset(
                    kPerCourseBitReaderOffset));
    installHook("PerCourseBitfieldWriter",
                kPerCourseBitfieldWriterOffset,
                perCourseBitfieldWriterHook.installAtMainOffset(
                    kPerCourseBitfieldWriterOffset));
}

bool setPerCourseWonderSeedBit(std::uint32_t course_index,
                               std::uint32_t value) {
    // REWRITTEN 2026-05-29.  The previous implementation called
    // FUN_710049EA24 with a 4-arg shape under the "4-arg overload"
    // hypothesis, which decompile_container_chain.py FALSIFIED:
    // FUN_710049EA24 is strictly 3-arg, masks `value` with `& 1`,
    // ignores w3.  The correct write path is the container-C bucket
    // walk at hash 0x60458608 -- proven via FUN_7100124134's read
    // pattern matching the M3.2 badge bitfield exactly.  Route through
    // the existing `setContainerCBit` primitive.
    //
    // setContainerCBit caps bit_index < 128 internally (the badge
    // bitfield storage is uint32_t[4] = 128 bits).  ~81 courses fit
    // easily.  If we ever discover Wonder Seeds use a narrower
    // storage (e.g., uint32_t[3] = 96 bits), course_index >= 96
    // would write into mirror slots -- harmless for badges (M3.2
    // proved u32[2..3] are mirrors of u32[0..1]) but unknown for
    // Wonder Seeds.  Smoke test will tell.
    const bool ok =
        setContainerCBit(kWonderSeedBitfieldHash, course_index,
                         value != 0);
    SMBWAP_LOG_INFO(
        "[seed-trace] setPerCourseWonderSeedBit: course=%u value=%u "
        "hash=0x%08x rc=%d (container-C write path)",
        course_index, value, kWonderSeedBitfieldHash, ok ? 1 : 0);
    return ok;
}

void triggerWonderSeedSmokeTest() {
    SMBWAP_LOG_INFO(
        "[seed-trace] ===== Wonder Seed write SMOKE TEST START =====");
    SMBWAP_LOG_INFO(
        "[seed-trace] writing bits 70 and 71 of container-C hash 0x60458608");
    SMBWAP_LOG_INFO(
        "[seed-trace] (bit 70 -> data[2] bit 6, bit 71 -> data[2] bit 7;");
    SMBWAP_LOG_INFO(
        "[seed-trace]  bits chosen to land in u32[2] which is the badge");
    SMBWAP_LOG_INFO(
        "[seed-trace]  mirror slot -- if WS uses the same shape, the write");
    SMBWAP_LOG_INFO(
        "[seed-trace]  goes into the live owned bitfield; if WS uses a");
    SMBWAP_LOG_INFO(
        "[seed-trace]  shorter array, we may OOB into adjacent storage.)");

    // Write two bits so a save-diff can tell us:
    //   - Both flipped at the same byte offset (+1 apart bit-wise)
    //     -> storage matches our model.
    //   - Neither flipped -> write didn't land in persistent storage
    //     OR the bits were already set in this save.
    //   - One flipped, one didn't -> mid-byte corruption / partial OOB.
    const bool ok_70 =
        setContainerCBit(0x60458608u, 70, true);
    const bool ok_71 =
        setContainerCBit(0x60458608u, 71, true);

    // Also write bit 0 (course #0 -- likely course "W1-1 Welcome to
    // the Flower Kingdom" which the player has already cleared in any
    // saved game, so this should be idempotent).  Useful as a control:
    // if before=0x...01 and after=0x...01, the write reached the live
    // bitfield even though no bit changed.
    const bool ok_0 = setContainerCBit(0x60458608u, 0, true);

    SMBWAP_LOG_INFO(
        "[seed-trace] smoke test results: bit0=%d bit70=%d bit71=%d",
        ok_0 ? 1 : 0, ok_70 ? 1 : 0, ok_71 ? 1 : 0);
    SMBWAP_LOG_INFO(
        "[seed-trace] ===== Wonder Seed write SMOKE TEST END =====");
    SMBWAP_LOG_INFO(
        "[seed-trace] Next: save+quit+reload and save-diff against");
    SMBWAP_LOG_INFO(
        "[seed-trace] file offset 0x3AF8 (per docs/save-diff-findings.md");
    SMBWAP_LOG_INFO(
        "[seed-trace] 'per-course Wonder Seed flag array').  Confirm bits");
    SMBWAP_LOG_INFO(
        "[seed-trace] 0, 70, 71 are set in the persisted bitfield.");
}

void dumpSeedPersistenceMapOnce() {
    // Allow a bounded number of re-arms: hook call sites fire during save
    // deserialization (container-D not yet populated) as well as after it.
    // We commit (stop re-arming) only once we see real data, or after the
    // attempt cap so a save that genuinely has zero seeds doesn't loop.
    static std::atomic<bool> s_done{false};
    static std::atomic<std::uint32_t> s_attempts{0};

    bool expected = false;
    if (!s_done.compare_exchange_strong(expected, true,
                                        std::memory_order_acq_rel)) {
        return;
    }

    const std::uint32_t attempt =
        s_attempts.fetch_add(1, std::memory_order_relaxed);

    void* gmd = probe::gmdSingleton();
    if (gmd == nullptr) {
        s_done.store(false, std::memory_order_release);  // re-arm
        return;
    }

    using DReadFn =
        std::uint32_t (*)(void*, std::uint32_t*, std::uint32_t, std::uint32_t);
    using AReadFn = std::uint32_t (*)(void*, std::uint32_t*, std::uint32_t);
    using CBitFn =
        std::uint32_t (*)(void*, std::uint8_t*, std::uint32_t, std::uint32_t);
    const auto dread =
        reinterpret_cast<DReadFn>(probe::mainBase() + 0x000e258c);  // container-D
    const auto aread =
        reinterpret_cast<AReadFn>(probe::mainBase() + 0x0012ae94);  // container-A
    const auto cbit =
        reinterpret_cast<CBitFn>(probe::mainBase() + 0x00124134);   // container-C bit

    struct World {
        std::uint32_t wv;
        const char* name;
        std::uint32_t reg;
        std::uint32_t spec;
    };
    // Per-world seed-bitmask hashes, extracted from the descriptor table at
    // NSO +0x29f0b34 (stride 0x70; +0x10 = regular, +0x14 = special),
    // indexed by the game's world_val.  See docs handoff 2026-05-29.
    static const World kWorlds[] = {
        {1, "W1",      0xa6140d7cu, 0x6f9dfc59u},
        {2, "Petal",   0x46721422u, 0xb161b8abu},
        {3, "W2",      0x4cbd45f6u, 0xcdd48384u},
        {4, "W3",      0xce0879edu, 0x5aaa3cf1u},
        {5, "W4",      0x008c08feu, 0xd9031404u},
        {6, "W5",      0x95a3ed25u, 0x9afd6f27u},
        {7, "W6",      0x2542d582u, 0xe0ce1f69u},
        {8, "Castle",  0x122217a2u, 0x92d1845au},
        {9, "Special", 0x9878250fu, 0x8ba1cb58u},
    };

    SMBWAP_LOG_INFO(
        "[seed-persist] ===== persistence map dump START (attempt=%u) gmd=%p =====",
        attempt, gmd);

    std::uint32_t cur_world = 0, base = 0, anim = 0, result = 0;
    aread(gmd, &cur_world, 0x9f5ead3c);
    aread(gmd, &base, 0x21f89ab1);
    aread(gmd, &anim, 0x390eb960);
    aread(gmd, &result, 0x8c20ccb7);
    SMBWAP_LOG_INFO(
        "[seed-persist] current world_val=%u  base(0x21f89ab1)=%u  "
        "anim(0x390eb960)=%u  result(0x8c20ccb7)=%u",
        cur_world, base, anim, result);

    {
        std::uint32_t reg_mask = 0, spec_mask = 0;
        for (std::uint32_t b = 0; b < 31; ++b) {
            std::uint8_t v = 0;
            cbit(gmd, &v, 0x1faf41e5u, b);
            if (v) reg_mask |= (1u << b);
            v = 0;
            cbit(gmd, &v, 0xb9bd745du, b);
            if (v) spec_mask |= (1u << b);
        }
        SMBWAP_LOG_INFO(
            "[seed-persist] live current-world bitfields: "
            "0x1faf41e5=0x%08x (pop=%d)  0xb9bd745d=0x%08x (pop=%d)",
            reg_mask, __builtin_popcount(reg_mask),
            spec_mask, __builtin_popcount(spec_mask));
    }

    bool any_found = false;
    for (const auto& w : kWorlds) {
        for (std::uint32_t idx = 0; idx < 16; ++idx) {
            std::uint32_t rv = 0, sv = 0;
            const std::uint32_t r_rc = dread(gmd, &rv, w.reg, idx) & 1u;
            const std::uint32_t s_rc = dread(gmd, &sv, w.spec, idx) & 1u;
            if (r_rc || s_rc) any_found = true;
            // Log idx 0 always (anchor) + any non-zero payload at any idx.
            if (idx == 0 || rv != 0 || sv != 0) {
                SMBWAP_LOG_INFO(
                    "[seed-persist] %-7s wv=%u idx=%2u  reg[0x%08x]=0x%08x"
                    "(rc=%u,pop=%d)  spec[0x%08x]=0x%08x(rc=%u,pop=%d)",
                    w.name, w.wv, idx,
                    w.reg, rv, r_rc, __builtin_popcount(rv),
                    w.spec, sv, s_rc, __builtin_popcount(sv));
            }
        }
    }

    SMBWAP_LOG_INFO(
        "[seed-persist] ===== persistence map dump END (any_found=%d) =====",
        any_found ? 1 : 0);

    // If we found nothing and still have attempts left, the save likely
    // isn't loaded yet -- re-arm so a later hook fire retries.
    if (!any_found && attempt < 8) {
        s_done.store(false, std::memory_order_release);
    }
}

// Direct accessor for the LIVE container-D data array at gmd+0x800 (the
// primary container FUN_71000e258c reads; gmd+0x788 is the cap=2 deferred
// queue we deliberately bypass).  Mirrors findContainerCData but at the
// container-D offsets:
//   bucket = *(gmd+0x800)   bucket_count = *(gmd+0x80c)
//   objs   = *(gmd+0x7f8)   obj_limit    = *(gmd+0x7f0)
//   typed_obj = objs + idx*0x40;  element_count = *(typed_obj+0x20);
//   data (u32[]) = *(typed_obj+0x28)
// Holds ALL worlds' per-course bitmasks keyed by per-world hash, so writes
// here can target any world.  Returns the data pointer + element count, or
// nullptr if the hash isn't registered / gmd looks uninitialized.
std::uint32_t* findContainerDData(void* gmd_v, std::uint32_t hash,
                                  std::uint32_t* out_count) {
    auto* gmd = reinterpret_cast<unsigned char*>(gmd_v);
    if (gmd == nullptr) return nullptr;
    auto d8 = [&](std::size_t o) {
        return *reinterpret_cast<std::uintptr_t*>(gmd + o);
    };
    auto d4 = [&](std::size_t o) {
        return *reinterpret_cast<std::uint32_t*>(gmd + o);
    };
    const std::uintptr_t bucket = d8(0x800);
    const std::uint32_t bucket_count = d4(0x80c);
    const std::uint32_t obj_limit = d4(0x7f0);
    const std::uintptr_t objs = d8(0x7f8);
    if (bucket == 0 || bucket_count == 0 || bucket_count > 4096 ||
        objs == 0 || obj_limit > 4096) {
        return nullptr;
    }
    std::uint32_t initial = hash % bucket_count;
    std::uint32_t cur = initial;
    do {
        const std::uintptr_t entry = bucket + static_cast<std::uintptr_t>(cur) * 8;
        const std::uint32_t key = *reinterpret_cast<std::uint32_t*>(entry);
        if (key == hash) {
            std::uint32_t idx = *reinterpret_cast<std::uint32_t*>(entry + 4);
            if (idx >= obj_limit) idx = 0;
            const std::uintptr_t obj =
                objs + static_cast<std::uintptr_t>(idx) * 0x40;
            if (out_count != nullptr) {
                *out_count = *reinterpret_cast<std::uint32_t*>(obj + 0x20);
            }
            return *reinterpret_cast<std::uint32_t**>(obj + 0x28);
        }
        if (key == 0) return nullptr;
        cur = (cur + 1) % bucket_count;
    } while (cur != initial);
    return nullptr;
}

// Direct overwrite of container-D[hash][index] = value (no deferred queue,
// no Abort risk).  Bounds-checked against the typed object's element count.
bool setContainerDWordDirect(std::uint32_t hash, std::uint32_t index,
                             std::uint32_t value) {
    void* gmd = probe::gmdSingleton();
    if (gmd == nullptr) return false;
    std::uint32_t count = 0;
    std::uint32_t* data = findContainerDData(gmd, hash, &count);
    if (data == nullptr) return false;
    if (index >= count) return false;  // OOB guard
    data[index] = value;
    return true;
}

void pushWonderSeedContainerDCounts() {
    void* gmd = probe::gmdSingleton();
    if (gmd == nullptr) return;

    // AP bucket -> (regular, special) per-world seed-bitmask hashes, from
    // the per-world descriptor table @ NSO +0x29f0b34 (validated live).
    struct Bucket {
        std::uint32_t reg;
        std::uint32_t spec;
    };
    static const Bucket kBucket[8] = {
        {0xa6140d7cu, 0x6f9dfc59u},  // 0  W1
        {0x4cbd45f6u, 0xcdd48384u},  // 1  W2
        {0xce0879edu, 0x5aaa3cf1u},  // 2  W3
        {0x008c08feu, 0xd9031404u},  // 3  W4
        {0x95a3ed25u, 0x9afd6f27u},  // 4  W5
        {0x2542d582u, 0xe0ce1f69u},  // 5  W6
        {0x46721422u, 0xb161b8abu},  // 6  Petal Isles
        {0x9878250fu, 0x8ba1cb58u},  // 7  Special
    };
    // Diff cache: only rewrite a world whose count changed (0xffffffff =
    // never written, forces the first write).
    static std::uint32_t s_last[8] = {
        0xffffffffu, 0xffffffffu, 0xffffffffu, 0xffffffffu,
        0xffffffffu, 0xffffffffu, 0xffffffffu, 0xffffffffu};

    for (std::uint32_t b = 0; b < 8; ++b) {
        const std::uint32_t count = smbwap::ap::getWonderSeedCount(b);
        if (count == s_last[b]) continue;

        std::uint32_t reg_cnt = 0, spec_cnt = 0;
        std::uint32_t* reg = findContainerDData(gmd, kBucket[b].reg, &reg_cnt);
        std::uint32_t* spec = findContainerDData(gmd, kBucket[b].spec, &spec_cnt);
        if (reg == nullptr) continue;  // world's hash not registered yet

        // Encode count as `count` distinct course slots each carrying 1 bit
        // (bit 0 == that course's first seed -> always valid, strip-safe);
        // sum of popcounts over the array == count.  Zero the regular tail
        // and the whole special bitmask so the world total is exactly count.
        const std::uint32_t rcap = (reg_cnt > 81u) ? 81u : reg_cnt;
        for (std::uint32_t c = 0; c < rcap; ++c) {
            reg[c] = (c < count) ? 1u : 0u;
        }
        std::uint32_t scap = 0;
        if (spec != nullptr) {
            scap = (spec_cnt > 81u) ? 81u : spec_cnt;
            for (std::uint32_t c = 0; c < scap; ++c) spec[c] = 0u;
        }
        s_last[b] = count;

        static std::atomic<std::uint32_t> log_budget{64};
        if (log_budget.fetch_sub(1) > 0) {
            SMBWAP_LOG_INFO(
                "[grant] container-D persist: bucket=%u count=%u "
                "reg=0x%08x(%u slots) spec=0x%08x(%u slots)",
                b, count, kBucket[b].reg, rcap, kBucket[b].spec, scap);
        }
    }
}

void triggerSeedPersistWriteTestOnce() {
    static std::atomic<bool> s_done{false};
    bool expected = false;
    if (!s_done.compare_exchange_strong(expected, true,
                                        std::memory_order_acq_rel)) {
        return;
    }
    void* gmd = probe::gmdSingleton();
    if (gmd == nullptr) {
        s_done.store(false, std::memory_order_release);  // re-arm
        return;
    }

    using DReadFn =
        std::uint32_t (*)(void*, std::uint32_t*, std::uint32_t, std::uint32_t);
    const auto dread =
        reinterpret_cast<DReadFn>(probe::mainBase() + 0x000e258c);

    // W6 regular-seed-bitmask hash; W6 == 0 in the loaded save, so any
    // nonzero here is unambiguously ours.  W6 is NOT the current world (we
    // load into W1) -- this also tests cross-world targeting via gmd+0x800.
    constexpr std::uint32_t kW6Reg = 0x2542d582u;

    std::uint32_t count = 0;
    std::uint32_t* data = findContainerDData(gmd, kW6Reg, &count);
    SMBWAP_LOG_INFO(
        "[seed-persist] DIRECT WRITE TEST: W6 reg hash=0x%08x data=%p "
        "element_count=%u -- writing data[0]=0x7. *** SAVE MODIFIED IF YOU "
        "SAVE -- back up game_data.sav ***",
        kW6Reg, data, count);

    const bool ok = setContainerDWordDirect(kW6Reg, 0, 0x7u);

    // Read back via BOTH the raw data pointer (immediate) and the game's
    // reader FUN_71000e258c (should now agree -- no flush delay since this
    // is a direct write to the live container).
    std::uint32_t raw = (data != nullptr) ? data[0] : 0xffffffffu;
    std::uint32_t via_reader = 0;
    const std::uint32_t rc = dread(gmd, &via_reader, kW6Reg, 0) & 1u;
    SMBWAP_LOG_INFO(
        "[seed-persist] DIRECT WRITE TEST read-back: ok=%d  raw data[0]=0x%08x"
        "  reader[0]=0x%08x(rc=%u)  (want 0x7/0x7)",
        ok ? 1 : 0, raw, via_reader, rc);
}

void dumpGmdSubstructsOnce(const char* via) {
    static std::atomic<bool> s_dumped{false};
    bool expected = false;
    if (!s_dumped.compare_exchange_strong(expected, true,
                                          std::memory_order_acq_rel)) {
        return;
    }

    void* gmd = gmdSingleton();
    if (gmd == nullptr) {
        SMBWAP_LOG_WARN("[seed-trace] dumpGmdSubstructs (%s): gmd null", via);
        s_dumped.store(false, std::memory_order_release);
        return;
    }

    auto* gmd_bytes = reinterpret_cast<volatile std::uint8_t*>(gmd);

    SMBWAP_LOG_INFO(
        "[seed-trace] gmd substruct dump via=%s gmd=%p", via, gmd);

    // Known anchor offsets, see docs/static-analysis-findings.md
    // "GameDataMgr (gmd::) save-data API" + Gmd.hpp queue layout notes.
    constexpr struct {
        const char* label;
        std::size_t off;
    } kPtrSlots[] = {
        {"container_B_substruct (gmd+0x08)", 0x08},
        {"container_B_init_lock (gmd+0x68)", 0x68},
        {"container_C_buckets (gmd+0x78)",   0x78},
        {"container_C_anchor (gmd+0x80)",    0x80},
        {"container_A_substruct (gmd+0xC8)", 0xC8},
        {"container_A_dirty_ring (gmd+0xF8)", 0xF8},
        {"container_A_secondary (gmd+0x128)", 0x128},
        // Speculative: per-course / container-D anchor candidates.
        {"speculative (gmd+0x140)", 0x140},
        {"speculative (gmd+0x158)", 0x158},
        {"speculative (gmd+0x170)", 0x170},
        {"speculative (gmd+0x180)", 0x180},
        {"speculative (gmd+0x1A0)", 0x1A0},
        {"speculative (gmd+0x1C0)", 0x1C0},
        {"speculative (gmd+0x1E0)", 0x1E0},
        {"speculative (gmd+0x200)", 0x200},
        {"speculative (gmd+0x220)", 0x220},
        {"speculative (gmd+0x240)", 0x240},
        {"speculative (gmd+0x260)", 0x260},
        {"speculative (gmd+0x280)", 0x280},
        {"speculative (gmd+0x2A0)", 0x2A0},
        {"speculative (gmd+0x2C0)", 0x2C0},
        {"speculative (gmd+0x2E0)", 0x2E0},
    };
    for (const auto& slot : kPtrSlots) {
        const auto val = *reinterpret_cast<volatile std::uint64_t*>(
            gmd_bytes + slot.off);
        SMBWAP_LOG_INFO("[seed-trace]   %s = 0x%llx",
                        slot.label,
                        static_cast<unsigned long long>(val));
    }

    auto read_u32 = [&](std::size_t off) -> std::uint32_t {
        return *reinterpret_cast<volatile std::uint32_t*>(gmd_bytes + off);
    };
    const std::uint32_t qA_head  = read_u32(0x100);
    const std::uint32_t qA_cap   = read_u32(0x0F0);
    const std::uint32_t qA2_head = read_u32(0x128 + 0x38);
    const std::uint32_t qA2_cap  = read_u32(0x128 + 0x28);
    const std::uint32_t qB_head  = read_u32(0x008 + 0x38);
    const std::uint32_t qB_cap   = read_u32(0x008 + 0x28);
    SMBWAP_LOG_INFO(
        "[seed-trace]   queues qA head=0x%08x cap=%u  qA2 head=0x%08x cap=%u "
        "qB head=0x%08x cap=%u",
        qA_head, qA_cap, qA2_head, qA2_cap, qB_head, qB_cap);
}

}  // namespace probe
