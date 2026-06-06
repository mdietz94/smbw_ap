// Container-D (per-world Wonder Seed) persistence writer.
//
// 2026-05-29.  AP-authoritative per-world Wonder Seed count -> live
// container-D (gmd+0x800) write.  Driven by drainInbound() on every
// SetWonderSeedCounts message; see ap/ApFrameBridge.cpp.
//
// History: this file began as the Wonder Seed RE "seed-trace" observability
// module (3 trampolines + boot dumps + a write-test).  The RE concluded:
//   - The per-world Wonder Seed COUNT (gate + UI) is computed as
//       base(0x21f89ab1) + popcount(0x1faf41e5) + popcount(0xb9bd745d)
//     and the per-world base is the sum of popcounts over that world's
//     container-D u32[] array, keyed by a per-world hash from the
//     descriptor table @ NSO +0x29f0b34 (regular @ +0x10, special @ +0x14).
//   - The cosmetic per-course bitfield 0x60458608 is NOT the count source.
//   - Container-D is reachable for ALL worlds via the gmd+0x800 reader path
//     (FUN_71000e258c) -- a direct data[] write, no deferred queue, no
//     Abort.  This is the live READ cache: writes feed the game's count
//     recompute immediately but are NOT serialized to the save (the
//     serializer uses the gmd+0x788 deferred container).  AP re-applies
//     every session on connect, so the only un-persisted window is the
//     ~2 s before the bridge reconnects -- acceptable.
// The observability scaffolding was removed once the model was confirmed;
// what remains is the production write.

#include <atomic>
#include <cstdint>

#include "ap/ApFrameBridge.hpp"  // smbwap::ap::getWonderSeedCount + probe decl
#include "probe/Gmd.hpp"         // gmdSingleton, mainBase
#include "util/Log.hpp"

namespace {

// Live container-D data accessor at gmd+0x800 (the array FUN_71000e258c
// reads).  Walks the bucket -> typed-obj -> data[] (u32 array):
//   bucket = *(gmd+0x800)   bucket_count = *(gmd+0x80c)
//   objs   = *(gmd+0x7f8)   obj_limit    = *(gmd+0x7f0)
//   typed_obj = objs + idx*0x40;  element_count = *(typed_obj+0x20);
//   data (u32[]) = *(typed_obj+0x28)
// Returns the data pointer + element count, or nullptr if the hash isn't
// registered / gmd looks uninitialized.
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
        const std::uintptr_t entry =
            bucket + static_cast<std::uintptr_t>(cur) * 8;
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

}  // namespace

namespace probe {

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

// One-shot diagnostic: walk the entire gmd+0x800 container-D hash table and
// log every registered (hash, element_count).  This tells us exactly which
// per-course bitfields exist for the loaded save so we can identify the one
// that drives World-Map level-icon visibility (the FlowerLock 0x948e540d
// approach was the castle gate, not general course visibility).  Budgeted so
// it fires once per boot when open-world first becomes active.
void dumpContainerDHashesOnce(void* gmd_v) {
    static std::atomic<bool> s_done{false};
    if (s_done.exchange(true)) return;

    auto* gmd = reinterpret_cast<unsigned char*>(gmd_v);
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
    SMBWAP_LOG_INFO(
        "[cD-dump] gmd+0x800 bucket=0x%llx count=%u obj_limit=%u objs=0x%llx",
        static_cast<unsigned long long>(bucket), bucket_count, obj_limit,
        static_cast<unsigned long long>(objs));
    if (bucket == 0 || bucket_count == 0 || bucket_count > 65536 ||
        objs == 0 || obj_limit > 65536) {
        SMBWAP_LOG_INFO("[cD-dump] container looks uninitialized; aborting dump");
        return;
    }
    std::uint32_t logged = 0;
    for (std::uint32_t i = 0; i < bucket_count && logged < 128; ++i) {
        const std::uintptr_t entry = bucket + static_cast<std::uintptr_t>(i) * 8;
        const std::uint32_t key = *reinterpret_cast<std::uint32_t*>(entry);
        if (key == 0) continue;
        std::uint32_t idx = *reinterpret_cast<std::uint32_t*>(entry + 4);
        std::uint32_t ecount = 0;
        std::uint32_t first = 0, nonzero = 0;
        if (idx < obj_limit) {
            const std::uintptr_t obj =
                objs + static_cast<std::uintptr_t>(idx) * 0x40;
            ecount = *reinterpret_cast<std::uint32_t*>(obj + 0x20);
            auto* data = *reinterpret_cast<std::uint32_t**>(obj + 0x28);
            if (data != nullptr && ecount > 0 && ecount <= 4096) {
                first = data[0];
                const std::uint32_t lim = (ecount > 256) ? 256 : ecount;
                for (std::uint32_t k = 0; k < lim; ++k)
                    if (data[k] != 0) ++nonzero;
            }
        }
        SMBWAP_LOG_INFO("[cD-dump] hash=0x%08x count=%u data[0]=%u nonzero=%u",
                        key, ecount, first, nonzero);
        ++logged;
    }
    SMBWAP_LOG_INFO("[cD-dump] done (%u entries logged)", logged);
}

// One-shot diagnostic: dump the three per-world descriptor tables at NSO
// +0x29f0b34 / +0x29f0f94 / +0x29f13f4.  Each row is a per-world record whose
// u32 fields are the runtime hashes for that world's container-D arrays (WS
// reg @ +0x10, spec @ +0x14, and -- crucially -- the OTHER per-course
// attribute hashes at the sibling offsets).  Correlated with the container-D
// dump's nonzero counts, this maps every (world, attribute) -> hash so we can
// write the EXACT W2 route/visibility array instead of guessing.  Ported from
// the retired exlaunch dumpPerWorldTables (src/program/main.cpp).
void dumpPerWorldTablesOnce() {
    static std::atomic<bool> s_done{false};
    if (s_done.exchange(true)) return;

    const std::uintptr_t base = probe::mainBase();
    if (base == 0) return;
    // Tables 0 (hashes) and 2 (per-entry classifier) only; table 1 was all-zero
    // in the first dump.  Extended to 0x400 bytes to capture all 8 per-world
    // records (W1..Special) so W2's per-course array hashes are visible.
    static constexpr std::uint32_t kOffsets[2] = {0x29f0b34u, 0x29f13f4u};
    static constexpr std::size_t kBytesPerTable = 0x400;  // 64 rows of 16 B
    for (int t = 0; t < 2; ++t) {
        const std::uintptr_t addr = base + kOffsets[t];
        SMBWAP_LOG_INFO("[pwr] table=%d base=NSO+0x%x", t, kOffsets[t]);
        for (std::size_t row = 0; row < kBytesPerTable; row += 16) {
            const auto* p = reinterpret_cast<const std::uint32_t*>(addr + row);
            SMBWAP_LOG_INFO("[pwr] t=%d +0x%03zx: %08x %08x %08x %08x",
                            t, row, p[0], p[1], p[2], p[3]);
        }
    }
}

// One-shot EMPIRICAL TEST (user hypothesis 2026-06-05): write a Wonder Seed
// bit into an arbitrary W2 course (container-D reg array 0x4cbd45f6, slot 0)
// to see whether having any per-course record makes the W2 node appear on the
// World Map.  Direct live write, same safe path as pushWonderSeedContainerDCounts.
// If this surfaces a node we've learned node visibility ties to per-course
// container-D presence; if not, we rule it out.  Revert after testing.
void testWriteW2WonderSeedOnce(void* gmd) {
    static std::atomic<bool> s_done{false};
    if (s_done.exchange(true)) return;
    static constexpr std::uint32_t kW2RegHash = 0x4cbd45f6u;  // W2 WS reg
    std::uint32_t count = 0;
    std::uint32_t* data = findContainerDData(gmd, kW2RegHash, &count);
    if (data == nullptr || count == 0) {
        SMBWAP_LOG_INFO("[w2-test] W2 reg hash 0x%08x not found (count=%u)",
                        kW2RegHash, count);
        return;
    }
    data[0] = 1u;
    SMBWAP_LOG_INFO("[w2-test] wrote WS bit data[0]=1 into W2 reg 0x%08x "
                    "(count=%u) -- check if W2-1 node appears",
                    kW2RegHash, count);
}

// Generic hash-table lookup over a gmd custom container with the standard
// {bucket, cap, limit, objs} quad at the given byte offsets.  Returns the
// typed-object pointer (objs + objidx*obj_stride) for `hash`, or nullptr.
const unsigned char* gmdContainerObj(unsigned char* gmd, std::size_t off_bucket,
                                     std::size_t off_cap, std::size_t off_limit,
                                     std::size_t off_objs,
                                     std::uint32_t obj_stride,
                                     std::uint32_t hash) {
    auto d8 = [&](std::size_t o) {
        return *reinterpret_cast<std::uintptr_t*>(gmd + o);
    };
    auto d4 = [&](std::size_t o) {
        return *reinterpret_cast<std::uint32_t*>(gmd + o);
    };
    const std::uintptr_t bucket = d8(off_bucket);
    const std::uint32_t cap = d4(off_cap);
    const std::uint32_t limit = d4(off_limit);
    const std::uintptr_t objs = d8(off_objs);
    if (bucket == 0 || cap == 0 || cap > 65536 || objs == 0 || limit > 65536)
        return nullptr;
    std::uint32_t initial = hash % cap, cur = initial;
    do {
        const std::uintptr_t entry = bucket + static_cast<std::uintptr_t>(cur) * 8;
        const std::uint32_t key = *reinterpret_cast<std::uint32_t*>(entry);
        if (key == hash) {
            std::uint32_t idx = *reinterpret_cast<std::uint32_t*>(entry + 4);
            if (idx >= limit) idx = 0;
            return reinterpret_cast<const unsigned char*>(
                objs + static_cast<std::uintptr_t>(idx) * obj_stride);
        }
        if (key == 0) return nullptr;
        cur = (cur + 1) % cap;
    } while (cur != initial);
    return nullptr;
}

// One-shot: compare W1 vs W2 across gmd+0x20 (stride 0x18, value byte @ +0x16)
// and gmd+0x80 (stride 0x40, data[0] @ +0x28, count @ +0x20) for every field
// of their per-world descriptor records.  W1 hashes / W2 hashes captured from
// the table-0 dump (records at +0x070 / +0x150, stride 0x70, 28 u32 fields).
void dumpW1vsW2TravelContainersOnce(void* gmd_v) {
    static std::atomic<bool> s_done{false};
    if (s_done.exchange(true)) return;
    auto* gmd = reinterpret_cast<unsigned char*>(gmd_v);

    static const std::uint32_t kW1[28] = {
        0x0235d948,0x6303c6d1,0xd8163612,0xeb41ddd5,0xa6140d7c,0x6f9dfc59,
        0xe7773731,0x406f7425,0xf3d3849c,0xa4dbcac9,0xb465ad47,0x6d1b5c25,
        0xcea26ca1,0x48e0dec7,0x72128f1d,0xf1c0ef95,0x4f262e62,0x6d2e5a9f,
        0x7ae70f64,0x43318285,0x55815859,0x64fe8cc4,0xfc951a99,0x6af4303e,
        0x05015b41,0xdf9a528a,0x5db62877,0x2c51d948};
    static const std::uint32_t kW2[28] = {
        0x6486ee21,0x1acdef12,0xe8f0029c,0x3c420b13,0x4cbd45f6,0xcdd48384,
        0x2f0b10b9,0x9ec77a1a,0x09bfe967,0x0e079bd1,0xf052cf51,0x6cf0be20,
        0x57a2c8f4,0x5bd1c9a9,0xad281095,0x5fab7f7d,0x84b51cc0,0x1abddd26,
        0x5acdeded,0xeb5da755,0x49abba86,0x06fa9d91,0xdaeb15b2,0xb994f3f9,
        0x745217bc,0x81407b66,0x9cf94a91,0x9ceeac14};

    auto probe = [&](std::uint32_t hash, int& c20, int& c80) {
        c20 = -1; c80 = -1;
        const auto* o20 = gmdContainerObj(gmd, 0x20, 0x2c, 0x10, 0x18, 0x18, hash);
        if (o20) c20 = *reinterpret_cast<const std::uint8_t*>(o20 + 0x16);
        const auto* o80 = gmdContainerObj(gmd, 0x80, 0x8c, 0x70, 0x78, 0x40, hash);
        if (o80) {
            auto* data = *reinterpret_cast<std::uint32_t* const*>(o80 + 0x28);
            c80 = data ? static_cast<int>(data[0]) : -2;
        }
    };

    SMBWAP_LOG_INFO("[cmp] field: W1hash g20/g80  |  W2hash g20/g80  "
                    "(g20=byte@+0x16, g80=data[0], -1=absent)");
    for (int i = 0; i < 28; ++i) {
        int a20, a80, b20, b80;
        probe(kW1[i], a20, a80);
        probe(kW2[i], b20, b80);
        SMBWAP_LOG_INFO("[cmp] +0x%02x: %08x %d/%d | %08x %d/%d",
                        i * 4, kW1[i], a20, a80, kW2[i], b20, b80);
    }
}

// One-shot: dump the FULL gmd+0x80 per-world record arrays for the six fields
// where W1 (nodes) differs from W2 (no nodes), then COPY W1's array contents
// into W2's (min element count).  This is the decisive test of whether the
// gmd+0x80 per-world record is the Course-Map node source: if W2 nodes appear
// after the copy, it is (and we then synthesise W2's real values); if not,
// node data lives elsewhere (RomFS/scene).  Direct write, no deferred queue.
void dumpAndCopyGmd80RecordOnce(void* gmd_v) {
    static std::atomic<bool> s_done{false};
    if (s_done.exchange(true)) return;
    auto* gmd = reinterpret_cast<unsigned char*>(gmd_v);
    // (record offset, W1 hash, W2 hash) for the six differing gmd+0x80 fields.
    struct Pair { std::uint32_t off, w1, w2; };
    static const Pair kPairs[6] = {
        {0x04, 0x6303c6d1u, 0x1acdef12u},
        {0x08, 0xd8163612u, 0xe8f0029cu},
        {0x0c, 0xeb41ddd5u, 0x3c420b13u},
        {0x20, 0xf3d3849cu, 0x09bfe967u},
        {0x2c, 0x6d1b5c25u, 0x6cf0be20u},
        {0x30, 0xcea26ca1u, 0x57a2c8f4u},
    };
    for (const auto& p : kPairs) {
        auto* o1 = const_cast<unsigned char*>(
            gmdContainerObj(gmd, 0x80, 0x8c, 0x70, 0x78, 0x40, p.w1));
        auto* o2 = const_cast<unsigned char*>(
            gmdContainerObj(gmd, 0x80, 0x8c, 0x70, 0x78, 0x40, p.w2));
        std::uint32_t c1 = o1 ? *reinterpret_cast<std::uint32_t*>(o1 + 0x20) : 0;
        std::uint32_t c2 = o2 ? *reinterpret_cast<std::uint32_t*>(o2 + 0x20) : 0;
        auto* d1 = o1 ? *reinterpret_cast<std::uint32_t**>(o1 + 0x28) : nullptr;
        auto* d2 = o2 ? *reinterpret_cast<std::uint32_t**>(o2 + 0x28) : nullptr;
        // Dump first 8 elements of each.
        auto sample = [](std::uint32_t* d, std::uint32_t c, std::uint32_t* out) {
            for (int k = 0; k < 8; ++k) out[k] = (d && (std::uint32_t)k < c) ? d[k] : 0;
        };
        std::uint32_t s1[8], s2[8];
        sample(d1, c1, s1); sample(d2, c2, s2);
        SMBWAP_LOG_INFO("[g80] +0x%02x W1 0x%08x cnt=%u [%u %u %u %u %u %u %u %u]",
                        p.off, p.w1, c1, s1[0],s1[1],s1[2],s1[3],s1[4],s1[5],s1[6],s1[7]);
        SMBWAP_LOG_INFO("[g80] +0x%02x W2 0x%08x cnt=%u [%u %u %u %u %u %u %u %u]",
                        p.off, p.w2, c2, s2[0],s2[1],s2[2],s2[3],s2[4],s2[5],s2[6],s2[7]);
        // The gate arrays are +0x04 and +0x08 (empty for an unvisited world).
        // Their data is a repeating 4-u32 per-course block with GENERIC values
        // (W1's two reached courses are identical: [20,0,0,0] / [28,1<<28,0,0]),
        // so they're "course reached/visible" state, not course IDs.  Replicate
        // W1's block[i%4] across ALL of W2's slots to surface every course node
        // (vs the 2 W1 happened to have).  Other fields were already populated
        // for W2 -- leave them (verbatim copy is a harmless no-op there).
        const bool is_gate = (p.off == 0x04 || p.off == 0x08);
        if (d1 && d2 && c1 && c2) {
            std::uint32_t n = c1 < c2 ? c1 : c2;
            if (n > 4096) n = 4096;
            if (is_gate) {
                const std::uint32_t blk[4] = {d1[0], d1[1], d1[2], d1[3]};
                for (std::uint32_t k = 0; k < n; ++k) d2[k] = blk[k & 3];
                SMBWAP_LOG_INFO("[g80] filled W2 +0x%02x: block[%u %u %u %u] x%u slots",
                                p.off, blk[0], blk[1], blk[2], blk[3], n);
            } else {
                for (std::uint32_t k = 0; k < n; ++k) d2[k] = d1[k];
                SMBWAP_LOG_INFO("[g80] copied W1->W2 for +0x%02x: %u elems", p.off, n);
            }
        }
    }
}

// =========================================================================
// OPEN-WORLD ENTRY (generalised, 2026-06-05).
//
// Make an AP-opened world's course nodes appear in the Course-Map travel list
// so the player can travel into the world and reach its first course.  Driven
// by the routable-world mask (set by SetRoutableWorldsAbsolute) and the
// per-world descriptor table at NSO+0x29f0ba4 (record 0 = W1; stride 0x70;
// order W1,Petal,W2,W3,W4,W5,W6,Special).  Each record's u32 fields are that
// world's container hashes.
//
// The two gmd+0x80 "course state" arrays at record +0x04 and +0x08 are the
// gate: empty for an unvisited world (no nodes), and filling them with the
// generic per-course block ([20,0,0,0] / [28,1<<28,0,0]) surfaces the course
// nodes (live-confirmed for W2, log 09-06-35).  The four gmd+0x20 bool bytes
// at record +0x3c/+0x48/+0x68/+0x6c are the "world discovered" flags (W1 has
// byte@+0x16==1, an unvisited world 0).  AP state is NOT loaded from saves, so
// the "marks courses reached" side effect is irrelevant -- the only goal is
// travelability.
//
// Guarded: a gmd+0x80 array is filled only when its data[0]==0 (a fresh,
// unvisited world), so a world the player has real progress in is never
// clobbered.  Runs every drain; the guards make it naturally idempotent.
void applyOpenWorldEntry(void* gmd_v) {
    auto* gmd = reinterpret_cast<unsigned char*>(gmd_v);
    const std::uint32_t mask = smbwap::ap::getRoutableWorldMask();
    if (mask == 0) return;
    const std::uintptr_t base = probe::mainBase();
    if (base == 0) return;

    // Per-world descriptor table: record(i) base = NSO+0x29f0ba4 + i*0x70.
    // Record order rec0..8 = W1, Petal, W2, W3, W4, W5, W6, Special, Castle.
    // (Record 8 is the Bowser-castle slot between Special and the sibling
    // table at +0x29f0f94; its hashes are non-zero in the NSO -- +0x04
    // 0x776ba6e8, +0x08 0xefe46736, +0x3c 0x85a5d6fc.)
    const std::uintptr_t kRec0 = base + 0x29f0ba4u;
    static constexpr std::uint32_t kStride = 0x70u;
    // routable-mask bit -> descriptor record index.  Bits 0..7 are
    // W1,W2,W3,W4,W5,W6,Petal,Special; bit 8 (kCastleMaskBit) -> rec 8
    // (Castle/Bowser).  The castle bit is only set once the client opens
    // the route (palaces threshold met), so the castle is filled then.
    static_assert(smbwap::ap::kCastleMaskBit == 8,
                  "applyOpenWorldEntry assumes the castle is mask bit 8");
    static const int kBitToRec[9] = {0, 2, 3, 4, 5, 6, 1, 7, 8};
    static const std::uint32_t kFillOff[2] = {0x04u, 0x08u};
    static const std::uint32_t kBlk[2][4] = {
        {20u, 0u, 0u, 0u}, {28u, 0x10000000u, 0u, 0u}};
    // gmd+0x20 "world discovered" byte.  +0x3c marks the world discovered
    // for the world-level travel predicate (FUN_710055f330).  The sibling
    // bytes +0x48/+0x68/+0x6c surface Poplin/badge shop nodes (live-confirmed
    // 2026-06-05: +0x6c = Master Poplin's house, +0x48 = badge house) -- we
    // deliberately do NOT set those: a shop only appears *with* the fill, and
    // first-course-only entry was the chosen design (no houses).
    static constexpr std::uint32_t kDiscoveredOff = 0x3cu;
    static const char* const kWorldName[9] = {
        "W1", "W2", "W3", "W4", "W5", "W6", "Petal", "Special", "Castle"};

    // Monotonic count-up capped at 24 (NOT a fetch_sub budget: an unsigned
    // fetch_sub underflows past 0 to UINT32_MAX and then logs forever -- this
    // function runs per-frame, so that produced ~1600 spam lines/session).
    static std::atomic<std::uint32_t> log_count{0};
    for (int bit = 0; bit < 9; ++bit) {
        if ((mask & (1u << bit)) == 0) continue;
        const std::uintptr_t rec =
            kRec0 + static_cast<std::uintptr_t>(kBitToRec[bit]) * kStride;

        // gmd+0x80 course-state arrays (+0x04, +0x08).  For a WORLD, fill
        // only the first per-course block (4 u32 = course index 0) so just
        // its first node is travelable; the game's own progression unlocks
        // the rest.  For PETAL ISLES (bit 6, the hub) fill ALL nodes so the
        // whole hub is fast-travelable -- the player teleports to PI and
        // walks into each open world's entrance on foot (triggering that
        // world's FirstVisitDemo, which clears its route obstacle).  Guarded
        // by data[0]==0 so a progressed world is never clobbered, and by a
        // null container so a record without these arrays is skipped.
        const bool fill_all = (bit == 6);  // Petal Isles hub
        for (int f = 0; f < 2; ++f) {
            const std::uint32_t hash =
                *reinterpret_cast<std::uint32_t*>(rec + kFillOff[f]);
            auto* obj = const_cast<unsigned char*>(
                gmdContainerObj(gmd, 0x80, 0x8c, 0x70, 0x78, 0x40, hash));
            if (obj == nullptr) continue;
            const std::uint32_t cnt = *reinterpret_cast<std::uint32_t*>(obj + 0x20);
            auto* data = *reinterpret_cast<std::uint32_t**>(obj + 0x28);
            if (data == nullptr || cnt == 0) continue;
            if (data[0] != 0) continue;  // progressed world -> preserve
            std::uint32_t n = fill_all ? cnt : (cnt < 4 ? cnt : 4);
            if (n > 4096) n = 4096;  // sanity bound
            for (std::uint32_t k = 0; k < n; ++k) data[k] = kBlk[f][k & 3];
        }

        // gmd+0x20 "world discovered" byte (+0x3c).
        const std::uint32_t dhash =
            *reinterpret_cast<std::uint32_t*>(rec + kDiscoveredOff);
        auto* dobj = const_cast<unsigned char*>(
            gmdContainerObj(gmd, 0x20, 0x2c, 0x10, 0x18, 0x18, dhash));
        if (dobj != nullptr) {
            auto* by = reinterpret_cast<std::uint8_t*>(dobj + 0x16);
            if (*by == 0) *by = 1u;
        }

        if (log_count.fetch_add(1, std::memory_order_relaxed) < 24) {
            SMBWAP_LOG_INFO(
                "[open-world] %s (bit=%d rec=%d) first-course entry applied",
                kWorldName[bit], bit, kBitToRec[bit]);
        }
    }
}

void pushFlowerLockUnlock() {
    // Only run in open-world mode (g_routable_world_mask != 0).
    if (smbwap::ap::getRoutableWorldMask() == 0) return;

    void* gmd = probe::gmdSingleton();
    if (gmd == nullptr) return;

    // Open-world: seed 100 purple coins ONCE per boot.  Some worlds gate
    // their first area on spending purple coins, which a player who walks/
    // fast-travels into a world out of order may not have.  One-shot so it
    // never tops up after the player spends them; the floor helper only
    // raises a live value that is below 100, never lowers a higher one.
    static std::atomic<bool> s_coins_seeded{false};
    if (!s_coins_seeded.exchange(true)) {
        constexpr std::uint32_t kFlowerCoinHash = 0xF4EE6827u;
        if (ensureContainerACounterFloor(kFlowerCoinHash, 100u)) {
            SMBWAP_LOG_INFO("[open-world] seeded purple coins to floor 100");
        }
    }

    // Generalised open-world entry: surface every AP-opened world's course
    // nodes so the player can travel in and reach the first course.
    applyOpenWorldEntry(gmd);
}

}  // namespace probe
