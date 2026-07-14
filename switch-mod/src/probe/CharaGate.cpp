#include "probe/CharaGate.hpp"

#include <atomic>

#include "ap/ApFrameBridge.hpp"  // probe::isSaveLoaded / isInSceneTransitionWindow
#include "probe/Gmd.hpp"
#include "util/Log.hpp"

namespace probe {

namespace {

// Roster order == the game's character enum order (GameDataList enum for
// LocalPlayerCharaType; name table @0x71034efad8, verified 2026-07-08).
constexpr std::uint32_t kCharaCount = 12;

const char* const kCharaNames[kCharaCount] = {
    "Mario",         "Luigi",       "Peach",    "Daisy",
    "KinopioYellow", "KinopioBlue", "Kinopico", "Totten",
    "YoshiGreen",    "YoshiRed",    "YoshiYellow", "YoshiBlue",
};

// murmur3-32(seed 0) of each roster name -- the enum VALUES the
// EnumArray stores (GameDataList "Values" list, cross-checked against
// murmur3("Mario") / murmur3("Totten")).
constexpr std::uint32_t kCharaNameHashes[kCharaCount] = {
    0x43C14D27,  // Mario
    0x8CCA2EB8,  // Luigi
    0x2C231DE5,  // Peach
    0x64248334,  // Daisy
    0x69BD0F56,  // KinopioYellow
    0xFBD064BE,  // KinopioBlue
    0xF5BC9633,  // Kinopico (Toadette)
    0x371FE67B,  // Totten (Nabbit)
    0xCADE4A56,  // YoshiGreen
    0x5972230D,  // YoshiRed
    0x8C8091A9,  // YoshiYellow
    0xB3B183B5,  // YoshiBlue (Light-Blue)
};

// The two transient roster EnumArrays the sweep repairs.  The per-course
// last-played array (0x580b7eb4) is deliberately left alone -- it's
// display history, not selection state.
constexpr std::uint32_t kHashLocalPlayerCharaType = 0x6C05CCE3;
constexpr std::uint32_t kHashPlayerCharaType      = 0x71E6F035;

// gmd EnumArray machinery (decompiled 2026-07-08, this session):
//   writer FUN_7100387a84(gmd+0x2a8, value, hash, index) -> bool; pushes a
//     {value, index, hash} 0xc-byte entry into the container's deferred
//     ring (head at container+0x38, cap at +0x28 -- same shape and same
//     overflow->Abort as containers A/B/D, so honor backpressure);
//   reader FUN_710064f4f0(hash, index, int* out) -> bool; resolves the
//     stored name-hash back to the roster index via a 12-way string match.
constexpr std::uint32_t kEnumArrayWriterOff = 0x00387A84;
constexpr std::uint32_t kCharaReaderOff     = 0x0064F4F0;
constexpr std::uint32_t kEnumContainerOff   = 0x2A8;

using EnumArrayWriterFn = bool (*)(void* container, std::uint32_t value,
                                   std::uint32_t hash, std::int32_t index);
using CharaReaderFn = bool (*)(std::uint32_t hash, std::int32_t index,
                               std::int32_t* out_roster_idx);

// Only 4 local player slots exist in the transient arrays.
constexpr std::int32_t kPlayerSlots = 4;

std::atomic<std::uint32_t> s_unlocked_mask{0};
std::atomic<bool> s_sweep_pending{false};

// Monotonic log throttles (NOT the fetch_sub budget idiom -- that
// underflows and spams; see [[smbwap-log-budget-underflow]]).
std::atomic<std::uint32_t> s_commit_log_count{0};
std::atomic<std::uint32_t> s_sweep_log_count{0};

// xorshift32 for the random pick.  Game-thread only in practice, but
// atomic so a stray cross-thread call can't corrupt it.
std::atomic<std::uint32_t> s_rng{0x9E3779B9u};

std::uint32_t nextRand() {
    std::uint32_t x = s_rng.load(std::memory_order_relaxed);
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    s_rng.store(x, std::memory_order_relaxed);
    return x;
}

const char* rosterName(std::uint32_t idx) {
    return idx < kCharaCount ? kCharaNames[idx] : "<invalid>";
}

// Pick a random set bit of `mask` (roster indices only).  Returns
// kCharaCount if the mask has no roster bits (caller skips enforcement).
std::uint32_t pickUnlocked(std::uint32_t mask) {
    mask &= (1u << kCharaCount) - 1u;
    if (mask == 0) return kCharaCount;
    const std::uint32_t n = __builtin_popcount(mask);
    std::uint32_t pick = nextRand() % n;
    for (std::uint32_t i = 0; i < kCharaCount; ++i) {
        if (!((mask >> i) & 1u)) continue;
        if (pick == 0) return i;
        --pick;
    }
    return kCharaCount;  // unreachable
}

}  // namespace

void setUnlockedCharaMask(std::uint32_t mask) {
    const std::uint32_t prev =
        s_unlocked_mask.exchange(mask, std::memory_order_relaxed);
    if (prev != mask) {
        SMBWAP_LOG_INFO("[charagate] unlocked mask 0x%03x -> 0x%03x",
                        prev, mask);
        s_commit_log_count.store(0, std::memory_order_relaxed);
        s_sweep_log_count.store(0, std::memory_order_relaxed);
    }
    // Re-arm the sweep on EVERY receipt (the bridge resends ~2 s), so a
    // selection made while disconnected is repaired within a tick.
    s_sweep_pending.store(true, std::memory_order_relaxed);
}

std::uint32_t unlockedCharaMask() {
    return s_unlocked_mask.load(std::memory_order_relaxed);
}

void filterCharaCommitRecord(void* rec) {
    const std::uint32_t mask =
        s_unlocked_mask.load(std::memory_order_relaxed);
    if (rec == nullptr || mask == 0) return;
    auto* bytes = reinterpret_cast<std::uint8_t*>(rec);
    if (bytes[0x8] == 0) return;  // record inactive; orig() ignores it too
    auto* idx_p = reinterpret_cast<std::uint32_t*>(bytes + 0x30);
    const std::uint32_t idx = *idx_p;
    if (idx >= kCharaCount) return;          // 0xc = "none"; leave alone
    if ((mask >> idx) & 1u) return;          // already unlocked
    const std::uint32_t forced = pickUnlocked(mask);
    if (forced >= kCharaCount) return;       // defensive: no unlocked chara
    *idx_p = forced;
    const std::int32_t slot =
        *reinterpret_cast<std::int32_t*>(bytes + 0x10);
    if (s_commit_log_count.fetch_add(1, std::memory_order_relaxed) < 16) {
        SMBWAP_LOG_INFO(
            "[charagate] slot %d selected LOCKED %s (idx %u) -> forced %s "
            "(idx %u)",
            slot, rosterName(idx), idx, rosterName(forced), forced);
    }
}

void charaGateTick() {
    if (!s_sweep_pending.load(std::memory_order_relaxed)) return;
    const std::uint32_t mask =
        s_unlocked_mask.load(std::memory_order_relaxed);
    if (mask == 0) {
        s_sweep_pending.store(false, std::memory_order_relaxed);
        return;
    }
    // Same gates as the inbound grant drains: the gmd singleton must be
    // deserialized and we must be clear of the scene-transition window.
    if (!isSaveLoaded() || isInSceneTransitionWindow()) return;
    void* gmd = gmdSingleton();
    if (gmd == nullptr) return;

    // Backpressure on the EnumArray deferred ring (container at
    // gmd+0x2a8; head/cap at +0x38/+0x28 like every other container).
    // Newly-characterized write path -> conservative 50% refusal, same
    // policy as container D.
    const QueueDepth q =
        readQueueAt(kEnumContainerOff + 0x38, kEnumContainerOff + 0x28);
    if (q.cap != 0 && depthPct(q) >= kBackpressureRefusePctD) {
        // Keep the sweep pending; retry once the ring drains.
        return;
    }

    const std::uintptr_t base = mainBase();
    auto reader = reinterpret_cast<CharaReaderFn>(base + kCharaReaderOff);
    auto writer =
        reinterpret_cast<EnumArrayWriterFn>(base + kEnumArrayWriterOff);
    auto* container = reinterpret_cast<void*>(
        reinterpret_cast<std::uintptr_t>(gmd) + kEnumContainerOff);

    const std::uint32_t array_hashes[2] = {kHashLocalPlayerCharaType,
                                           kHashPlayerCharaType};
    for (std::uint32_t h = 0; h < 2; ++h) {
        for (std::int32_t slot = 0; slot < kPlayerSlots; ++slot) {
            std::int32_t idx = -1;
            if (!reader(array_hashes[h], slot, &idx)) continue;
            if (idx < 0 || idx >= static_cast<std::int32_t>(kCharaCount))
                continue;
            if ((mask >> idx) & 1u) continue;
            const std::uint32_t forced = pickUnlocked(mask);
            if (forced >= kCharaCount) continue;
            const bool ok = writer(container, kCharaNameHashes[forced],
                                   array_hashes[h], slot);
            if (s_sweep_log_count.fetch_add(1, std::memory_order_relaxed)
                < 16) {
                SMBWAP_LOG_INFO(
                    "[charagate] sweep: array 0x%08x slot %d had LOCKED %s "
                    "(idx %d) -> forced %s (idx %u) write=%s",
                    array_hashes[h], slot, rosterName(idx), idx,
                    rosterName(forced), forced, ok ? "ok" : "REFUSED");
            }
        }
    }
    s_sweep_pending.store(false, std::memory_order_relaxed);
}

std::int32_t currentChara(std::int32_t slot) {
    // Same liveness gates as the sweep: the reader walks the gmd EnumArray
    // container, so the singleton must be deserialized and we must be clear
    // of the scene-transition window.  A -1 return just makes the bridge
    // fall back to its (weaker) resolution and, worst case, drop the bump.
    if (slot < 0 || slot >= kPlayerSlots) return -1;
    if (!isSaveLoaded() || isInSceneTransitionWindow()) return -1;
    void* gmd = gmdSingleton();
    if (gmd == nullptr) return -1;
    const std::uintptr_t base = mainBase();
    auto reader = reinterpret_cast<CharaReaderFn>(base + kCharaReaderOff);
    std::int32_t idx = -1;
    if (!reader(kHashLocalPlayerCharaType, slot, &idx)) return -1;
    if (idx < 0 || idx >= static_cast<std::int32_t>(kCharaCount)) return -1;
    return idx;
}

}  // namespace probe
