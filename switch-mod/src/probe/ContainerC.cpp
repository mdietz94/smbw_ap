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

    static std::atomic<std::int32_t> log_budget{16};
    if (::smbwap::util::takeBudget(log_budget)) {
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

    static std::atomic<std::int32_t> log_budget{32};
    if (::smbwap::util::takeBudget(log_budget)) {
        SMBWAP_LOG_INFO(
            "SetContainerCBit: hash=0x%08x bit=%u value=%d addr=%p "
            "word_idx=%u  before=0x%08x  after=0x%08x",
            hash, bit_index, value ? 1 : 0,
            data, word_idx, before, after);
    }
    return true;
}

// 2026-05-29.  Per-course Wonder Seed bitfield AP-authoritative
// absolute-set, mirror of `setBadgeBitfieldAbsolute` but for hash
// 0x60458608.
//
// Distinct from the badge case: badges use uint32_t[4] with u32[0..1] =
// real owned bits and u32[2..3] = a hardware-required mirror written
// by setBadgeBitfieldAbsolute.  For Wonder Seeds, the 2026-05-29 smoke
// test confirmed `setContainerCBit(0x60458608, 70, true)` wrote to
// data[2] (word_idx=2) and the write reached live memory -- meaning
// u32[2] is a REAL data slot for WS, NOT a mirror.  So we treat all
// 4 u32s as distinct: data[0..3] hold bits 0..127.
//
// Each course occupies one bit.  Vanilla SMBW has ~81 courses (per the
// FUN_71003D4110 lookup table), so bits 0..80 are populated; bits
// 81..127 are reserved.  Bridge derivation packs per-world ranges --
// see _recompute_wonder_seed_bits() in apworld/.../context.py.
//
// Triggers identical to badges: ReceivedItems, HelloMsg, periodic
// ~2 s tick.  Idempotent absolute-overwrite -- same input always
// produces the same final state, so a missed tick is silently
// recovered by the next one.
// ---------------------------------------------------------------------------
// Force-unequip AP-disabled badges (2026-06-10).
//
// The bug: when AP has NOT placed a badge, `setBadgeBitfieldAbsolute`
// clears its OWNED bit (good), but a level / shop / badge-house that
// grants+auto-equips the badge ALSO writes the badge's identity into
// the EQUIPPED field, which `setBadgeBitfieldAbsolute` never touches.
// Result: the disabled badge still shows as equipped/available in the
// badge menu even though it isn't owned.  Owned-clear alone is
// insufficient; we must ALSO clear the equipped field.
//
// The equipped-badge identity is a SEPARATE GameData field (resolved
// offline from RomFS GameDataList.Product.100 -- see smbw-romfs-
// datamining):
//
//   EquipBadgeSave.BadgeId      hash 0xcfba9bf8  EnumArray[4]  save=0  (persisted)
//   CoursePlayerEquipBadge.BadgeId hash 0xf30cb2e2 EnumArray[4] save=-1 (per-level)
//
// Both are EnumArrays of 4 u32 slots.  Each slot holds the *enum value*
// of the equipped badge (NOT the internal_id) -- index k+1 of the enum
// table is BadgeId{k}.  The sentinel "no badge" value is `Invalid` ==
// 2117934662 (0x7E3D1E46), which is also both arrays' DefaultValue.
// The decoded enum values match the save-file equipped identities in
// the RE map (BadgeId34=0xe41b1aba Wall-Climb, BadgeId46=0xb77086e2
// Auto Super Mushroom).
//
// EnumArrays live in the typed-virtual container "B-2" at gmd+0x2b0
// (bucket gmd+0x2c0, count gmd+0x2cc, limit gmd+0x2b0, struct array
// gmd+0x2b8 stride 0x50, data pointer at struct+0x28) -- per the
// FUN_7100221128 reader decompile (static-analysis-findings "Updated
// GameDataMgr container map").  The scalar Enum writer FUN_71003877c4
// targets the DIFFERENT B-1 container (gmd+0x260) and only stores a
// single u32, so it CANNOT write a 4-slot EnumArray; we write the slot
// array directly, mirroring findContainerCData's open-address probe.
//
// ⚠️ NEEDS LIVE TEST + OFFSET CONFIRMATION: the gmd+0x2b0 container
// layout is from static analysis only; it has never been live-written.
// The write is gated defensively (null/bounds checks, data pointer
// sanity) so a wrong offset degrades to a no-op rather than a crash.
//
// internal_id <-> enum-value table (BadgeId{internal_id} = enum value),
// generated from GameDataList EquipBadgeSave.BadgeId.Values.  Used to
// decode "which badge is in this equip slot" so we can compare against
// the AP owned mask.
struct BadgeEnumEntry { std::uint32_t enum_value; std::uint8_t internal_id; };
constexpr BadgeEnumEntry kBadgeEnumValues[] = {
    {0x6bcc257bu, 0}, {0x074affb5u, 1}, {0x1f65a370u, 2}, {0x8c9688b2u, 3},
    {0xe9f7789au, 4}, {0x780bcc5au, 5}, {0x6d1bc278u, 6}, {0x6b24a10bu, 7},
    {0xcd133ba3u, 8}, {0x6a0e48eau, 9}, {0x2b973461u, 10}, {0x3e79eb0eu, 11},
    {0x579b6c6fu, 12}, {0xcff9ebc4u, 13}, {0x62a664a5u, 14}, {0x5f56923eu, 15},
    {0x2e388bb6u, 16}, {0xcf21767au, 17}, {0xb7a10fcau, 18}, {0x2124ce3eu, 19},
    {0x6e17fb3bu, 20}, {0x5ef0d828u, 21}, {0x40963b31u, 22}, {0xf7dfa3f6u, 23},
    {0x1c68d88eu, 24}, {0xe1732189u, 25}, {0x5f280e18u, 26}, {0xb75be3acu, 27},
    {0x25bd3f73u, 28}, {0x2a683a70u, 29}, {0x7139a3b0u, 30}, {0x95b023cau, 31},
    {0x1f4ab322u, 32}, {0xc987232eu, 33}, {0xe41b1abau, 34}, {0xa8e86054u, 35},
    {0x750bd47au, 36}, {0xd8c87b70u, 37}, {0x549668a6u, 38}, {0x88c1807du, 39},
    {0x20bc18b1u, 40}, {0xd79571f3u, 41}, {0x3a666542u, 42}, {0x911775bfu, 43},
    {0xf890d349u, 44}, {0x2d82a63du, 45}, {0xb77086e2u, 46}, {0x897a6180u, 47},
    {0x37e75951u, 48}, {0x64c3909fu, 49}, {0xf221f7e9u, 50}, {0xee9f4657u, 51},
    {0xf6f2ccd6u, 52}, {0x2575e18cu, 53}, {0x8f45c1acu, 54}, {0xc0158662u, 55},
    {0xc602af3cu, 56}, {0x8b6447bdu, 57}, {0xdf593fcau, 58},
};
constexpr std::uint32_t kBadgeInvalidEnumValue = 2117934662u;  // 0x7E3D1E46
constexpr std::uint32_t kEquipBadgeSaveHash       = 0xcfba9bf8u;
constexpr std::uint32_t kCoursePlayerEquipHash    = 0xf30cb2e2u;
constexpr std::uint32_t kEquipSlotCount           = 4u;

namespace {

// Decode an equip-slot enum value to its badge internal_id, or return
// -1 for the "Invalid" sentinel / any unrecognized value.
int badgeInternalIdForEnum(std::uint32_t enum_value) {
    if (enum_value == kBadgeInvalidEnumValue) return -1;
    for (const auto& e : kBadgeEnumValues) {
        if (e.enum_value == enum_value) return e.internal_id;
    }
    return -1;
}

// Walk container B-2 (the typed-virtual container at gmd+0x2b0) for
// `hash`; return the data pointer to its EnumArray storage (a
// uint32_t[OriginalSize]), or nullptr if not found.  Mirrors
// findContainerCData but for the B-2 layout.
std::uint32_t* findEnumArrayData(std::uint32_t hash) {
    auto* gmd = reinterpret_cast<unsigned char*>(gmdSingleton());
    if (gmd == nullptr) return nullptr;

    auto deref8 = [](unsigned char* p, std::ptrdiff_t off) -> std::uintptr_t {
        return *reinterpret_cast<std::uintptr_t*>(p + off);
    };
    auto deref4 = [](unsigned char* p, std::ptrdiff_t off) -> std::uint32_t {
        return *reinterpret_cast<std::uint32_t*>(p + off);
    };

    const std::uint32_t  limit        = deref4(gmd, 0x2b0);  // struct count cap
    const std::uintptr_t obj_array    = deref8(gmd, 0x2b8);  // struct array (stride 0x50)
    const std::uintptr_t bucket       = deref8(gmd, 0x2c0);  // bucket array (8-byte entries)
    const std::uint32_t  bucket_count = deref4(gmd, 0x2cc);
    if (bucket == 0 || bucket_count == 0 || bucket_count > 4096
        || obj_array == 0 || limit == 0 || limit > 4096) {
        return nullptr;
    }

    std::uint32_t initial = hash % bucket_count;
    std::uint32_t cur = initial;
    do {
        const std::uintptr_t entry = bucket + static_cast<std::uintptr_t>(cur) * 8;
        const std::uint32_t key = *reinterpret_cast<std::uint32_t*>(entry);
        if (key == hash) {
            std::uint32_t idx = *reinterpret_cast<std::uint32_t*>(entry + 4);
            if (idx >= limit) return nullptr;
            return *reinterpret_cast<std::uint32_t**>(
                obj_array + static_cast<std::uintptr_t>(idx) * 0x50 + 0x28);
        }
        if (key == 0) return nullptr;
        cur = (cur + 1) % bucket_count;
    } while (cur != initial);
    return nullptr;
}

// Clear any equip slot of `hash`'s EnumArray that references a badge
// NOT present in `owned_mask` (bit == internal_id).  Returns the number
// of slots cleared (>=0), or -1 if the field couldn't be located.
int clearEquipFieldNotOwned(std::uint32_t hash, std::uint64_t owned_mask) {
    std::uint32_t* slots = findEnumArrayData(hash);
    if (slots == nullptr) return -1;

    int cleared = 0;
    for (std::uint32_t s = 0; s < kEquipSlotCount; ++s) {
        const std::uint32_t cur = slots[s];
        if (cur == kBadgeInvalidEnumValue) continue;
        const int internal_id = badgeInternalIdForEnum(cur);
        if (internal_id < 0) continue;  // unrecognized value -- leave it alone
        const bool owned = (owned_mask >> internal_id) & 1u;
        if (!owned) {
            slots[s] = kBadgeInvalidEnumValue;
            ++cleared;
        }
    }
    return cleared;
}

}  // namespace

bool clearEquippedBadgesNotOwned(std::uint64_t owned_mask) {
    const int saved  = clearEquipFieldNotOwned(kEquipBadgeSaveHash, owned_mask);
    const int course = clearEquipFieldNotOwned(kCoursePlayerEquipHash, owned_mask);

    static std::atomic<std::int32_t> log_budget{16};
    if (::smbwap::util::takeBudget(log_budget)) {
        SMBWAP_LOG_INFO(
            "ClearEquippedNotOwned: owned=0x%016llx EquipBadgeSave=%d "
            "CoursePlayerEquip=%d",
            static_cast<unsigned long long>(owned_mask), saved, course);
    }
    // Success if at least one of the two fields was located.  A -1/-1
    // result means neither EnumArray field is live yet (pre-save-select
    // / wrong offset) -- report false so the caller knows nothing was
    // applied, but the badge owned-clear still ran independently.
    return saved >= 0 || course >= 0;
}

bool setWonderSeedBitfieldAbsolute(std::uint64_t bits_lo, std::uint64_t bits_hi) {
    constexpr std::uint32_t kWonderSeedBitfieldHash = 0x60458608u;
    std::uint32_t* data = findContainerCData(kWonderSeedBitfieldHash);
    if (data == nullptr) return false;

    const std::uint32_t lo_a = static_cast<std::uint32_t>(bits_lo & 0xFFFFFFFFu);
    const std::uint32_t lo_b = static_cast<std::uint32_t>((bits_lo >> 32) & 0xFFFFFFFFu);
    const std::uint32_t hi_a = static_cast<std::uint32_t>(bits_hi & 0xFFFFFFFFu);
    const std::uint32_t hi_b = static_cast<std::uint32_t>((bits_hi >> 32) & 0xFFFFFFFFu);

    const std::uint32_t before_0 = data[0];
    const std::uint32_t before_1 = data[1];
    const std::uint32_t before_2 = data[2];
    const std::uint32_t before_3 = data[3];

    data[0] = lo_a;
    data[1] = lo_b;
    data[2] = hi_a;
    data[3] = hi_b;

    static std::atomic<std::int32_t> log_budget{16};
    if (::smbwap::util::takeBudget(log_budget)) {
        SMBWAP_LOG_INFO(
            "SetWonderSeedsAbsolute: addr=%p bits_lo=0x%016llx bits_hi=0x%016llx "
            "before=[0x%08x,0x%08x,0x%08x,0x%08x] after=[0x%08x,0x%08x,0x%08x,0x%08x]",
            data,
            static_cast<unsigned long long>(bits_lo),
            static_cast<unsigned long long>(bits_hi),
            before_0, before_1, before_2, before_3,
            lo_a, lo_b, hi_a, hi_b);
    }
    return true;
}

}  // namespace probe
