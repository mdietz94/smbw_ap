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

}  // namespace probe
