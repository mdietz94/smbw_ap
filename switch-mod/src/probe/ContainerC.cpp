// M3.2 badge-grant + M3.3 generic container-C bit setter (Phase 2g port).
//
// Container C lives at gmd+0x70..0x8c -- a hash-keyed bucket of typed
// sub-objects holding bitfields.  Discovered 2026-05-24 (CLAUDE.md
// "M3.2 GRANT PRIMITIVE" section); the badge owned bitfield is at hash
// 0x105df820.
//
// Layout (from sprint 2 decompile):
//   gmd+0x70  uint32_t bucket_count_upper (= limit on typed objs)
//   gmd+0x78  long     typed_object_array (each obj 0x40 bytes)
//   gmd+0x80  long     bucket array (each entry 8 bytes: u32 hash + u32 idx)
//   gmd+0x8c  uint32_t bucket_count
//
// Lookup: open-address probe from `hash % bucket_count`.  Each typed-sub-
// obj's data pointer sits at obj+0x28; data is a uint32_t[4] (128 bits
// live; only u32[0..1] are real owned bits, u32[2..3] mirror them per
// the M3.2 discovery -- we write both halves).
//
// Container-C writes are NOT lock-protected (the legacy probe pattern
// just writes through).  drainInbound gates writers behind
// isInSceneTransitionWindow() to avoid racing the game's natural
// transition-time mutations.  See [[smbwap-phase-2g-handoff]] critical
// gotchas.
//
// AP-authoritative badge sync (M4 follow-up #2): bridge holds the
// canonical _badge_mask derived from AP items_received and overwrites
// the entire bitfield on every ReceivedItems / HelloMsg replay / 2 s
// periodic tick.  setBadgeBitfieldAbsolute is the apply function.

#include <atomic>
#include <cstdint>
#include <cstring>

#include "ap/ApFrameBridge.hpp"
#include "probe/Gmd.hpp"
#include "util/Log.hpp"

namespace probe {

namespace {

// The "owned" badge bitfield.  Corrected 2026-05-24 from the earlier
// 0x6d1b5c25 guess -- save-diff revealed 0x6d1b5c25 writes to file
// offset 0x1204 (UI-slot bitmap), not the canonical owned bitfield at
// 0x0EA0.  0x105df820 is the real owned hash; live addr matched the
// 4-badge save state exactly (bits 9, 34, 35, 46).
constexpr std::uint32_t kBadgeOwnedHash = 0x105df820;

// Walk gmd's container-C bucket for `hash`; return the data pointer for
// that hash's typed-sub-object, or nullptr if not found.  Open-address
// probe (linear) starting from `hash % bucket_count`.  Defensive: bails
// on null pointers or bucket counts > 1024 (which would indicate
// uninitialized gmd state).
std::uint32_t* findContainerCData(std::uint32_t hash) {
    auto* gmd = reinterpret_cast<unsigned char*>(gmdSingleton());
    if (gmd == nullptr) return nullptr;

    auto deref8 = [](unsigned char* p, std::ptrdiff_t off) -> std::uintptr_t {
        return *reinterpret_cast<std::uintptr_t*>(p + off);
    };
    auto deref4 = [](unsigned char* p, std::ptrdiff_t off) -> std::uint32_t {
        return *reinterpret_cast<std::uint32_t*>(p + off);
    };

    const std::uintptr_t bucket = deref8(gmd, 0x80);
    const std::uint32_t  bucket_count = deref4(gmd, 0x8c);
    const std::uint32_t  limit = deref4(gmd, 0x70);
    const std::uintptr_t obj_array = deref8(gmd, 0x78);
    if (bucket == 0 || bucket_count == 0 || bucket_count > 1024
        || obj_array == 0 || limit > 1024) {
        return nullptr;
    }

    std::uint32_t initial = hash % bucket_count;
    std::uint32_t cur = initial;
    do {
        const std::uintptr_t entry = bucket + static_cast<std::uintptr_t>(cur) * 8;
        const std::uint32_t key = *reinterpret_cast<std::uint32_t*>(entry);
        if (key == hash) {
            std::uint32_t idx =
                *reinterpret_cast<std::uint32_t*>(entry + 4);
            if (idx >= limit) idx = 0;
            return *reinterpret_cast<std::uint32_t**>(
                obj_array + static_cast<std::uintptr_t>(idx) * 0x40 + 0x28);
        }
        if (key == 0) return nullptr;
        cur = (cur + 1) % bucket_count;
    } while (cur != initial);
    return nullptr;
}

}  // namespace

bool setBadgeBitfieldAbsolute(std::uint64_t bits) {
    std::uint32_t* data = findContainerCData(kBadgeOwnedHash);
    if (data == nullptr) return false;

    const std::uint32_t lo = static_cast<std::uint32_t>(bits & 0xFFFFFFFFu);
    const std::uint32_t hi = static_cast<std::uint32_t>((bits >> 32) & 0xFFFFFFFFu);
    const std::uint32_t before_lo = data[0];
    const std::uint32_t before_hi = data[1];

    // M2.3 -- diff-on-overwrite.  Any bit set in the live bitfield but
    // NOT in the AP-authoritative mask we're about to write is an
    // in-game pickup (Poplin shop / badge house / badge medley / badge
    // challenge).  Emit one BadgeAcquired per stripped bit BEFORE we
    // overwrite so the bridge can fire a "<Badge> Obtained"
    // LocationCheck.  The player will see the badge re-appear on the
    // next overwrite cycle after AP returns the corresponding item in
    // ReceivedItems.
    const std::uint64_t actual = static_cast<std::uint64_t>(before_lo)
                               | (static_cast<std::uint64_t>(before_hi) << 32);
    const std::uint64_t acquired = actual & ~bits;
    {
        std::uint64_t to_emit = acquired;
        while (to_emit != 0) {
            const std::uint32_t bit =
                static_cast<std::uint32_t>(__builtin_ctzll(to_emit));
            to_emit &= to_emit - 1;
            smbwap::ap::enqueueBadgeAcquired(bit);
        }
    }

    // The bitfield storage is uint32_t[4] -- u32[0..1] = live owned
    // bits, u32[2..3] = mirror (per M3.2 discovery; both halves must
    // be written for in-game inventory UI consistency).
    data[0] = lo;
    data[1] = hi;
    data[2] = lo;
    data[3] = hi;

    static std::atomic<std::uint32_t> log_budget{16};
    if (log_budget.fetch_sub(1) > 0) {
        SMBWAP_LOG_INFO(
            "SetBadgesAbsolute: addr=%p bits=0x%016llx before=0x%08x%08x "
            "after=0x%08x%08x acquired=0x%016llx",
            data,
            static_cast<unsigned long long>(bits),
            before_hi, before_lo, hi, lo,
            static_cast<unsigned long long>(acquired));
    }
    return true;
}

bool setContainerCBit(std::uint32_t hash, std::uint32_t bit_index, bool value) {
    // Conservative guard: the bitfield storage is uint32_t[4] = 128
    // bits live (matching what dump_C snapshots observe).  We don't
    // query vtable slot 0x20 for the typed bit_count, so cap at 128.
    if (bit_index >= 128) return false;

    std::uint32_t* data = findContainerCData(hash);
    if (data == nullptr) return false;

    const std::uint32_t word_idx = bit_index >> 5;
    const std::uint32_t bit_in_word = bit_index & 0x1Fu;
    const std::uint32_t mask = 1u << bit_in_word;

    const std::uint32_t before = data[word_idx];
    const std::uint32_t after = value ? (before | mask) : (before & ~mask);
    data[word_idx] = after;

    static std::atomic<std::uint32_t> log_budget{32};
    if (log_budget.fetch_sub(1) > 0) {
        SMBWAP_LOG_INFO(
            "SetContainerCBit: hash=0x%08x bit=%u value=%d addr=%p "
            "word_idx=%u  before=0x%08x  after=0x%08x",
            hash, bit_index, value ? 1 : 0,
            data, word_idx, before, after);
    }
    return true;
}

}  // namespace probe
