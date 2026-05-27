// M3.8 DeathLink inbound synthetic-kill primitive (Phase 2g port).
//
// Anchor: the death-check code path at NSO +0x2743C0 reads HP as a
// signed halfword and branches if <= 0:
//     +0x2743BC: cbz   x9, ...
//     +0x2743C0: ldrsh w9, [x9, #0x38]    ; HP halfword (int16)
//     +0x2743C4: cmp   w9, #0
//     +0x2743C8: b.le  death_handler
// Writing 0 to HP makes the next tick re-evaluate as dead.  The
// HamletDuFromage "Disable Death" cheat's +0x1C "alive flag" write is a
// different hack; the actual HP byte is the int16 at +0x38.
//
// Latch strategy: hook the player tick function entry at NSO +0x273868
// (FUN_7100273868(long param_1, long param_2)) and replicate its
// internal dereference chain on first call to land at the HP struct.
// Trampoline at function entry avoids the relocator-corrupting inline
// hook at +0x2743BC that an earlier attempt tried.
//
// Once g_live_base is latched, synthKill writes 0 to base+0x38 (int16)
// and sets g_synthetic_death_this_frame.  The Nerve callback's
// SceneTransition branch consumes the flag and suppresses the outbound
// DEATH_DETECTED echo so we don't bounce our own kill back to AP.
//
// Direct port of switch-mod/src/program/main.cpp:1960-2077.

#include "probe/DeathLink.hpp"

#include <atomic>
#include <cstdint>

#include "util/Log.hpp"

namespace probe {

namespace {

// Stable pointer to the live HP-bearing struct.  0 means "not yet
// latched"; synthKill no-ops in that case.  Set by tryLatchPlayerHpStruct
// (called from main.cpp's PlayerTickLatch trampoline).
std::atomic<std::uintptr_t> g_live_base{0};

// Loop guard.  Set by synthKill right before the HP=0 write so the
// SceneTransition Nerve callback can drop the outbound DEATH_DETECTED
// echo (the death IS our doing).  Mirrors SMO's
// ApState::synthetic_death_this_frame.  Consume-once via exchange.
std::atomic<bool> g_synthetic_death_this_frame{false};

inline std::uintptr_t deref8(std::uintptr_t p, std::ptrdiff_t off) {
    return *reinterpret_cast<std::uintptr_t*>(p + off);
}
inline std::uint32_t deref4(std::uintptr_t p, std::ptrdiff_t off) {
    return *reinterpret_cast<std::uint32_t*>(p + off);
}

}  // namespace

void tryLatchPlayerHpStruct(void* param_1) {
    if (g_live_base.load(std::memory_order_relaxed) != 0) return;
    if (param_1 == nullptr) return;

    // Replicates the player-tick's own walk at +0x2743A0..+0x2743B8:
    //   x8       = *(param_1 + 0x10)        ; tick context
    //   arr      = *(x8 + 0x208)            ; sub-array
    //   ver      = *(x8 + 0x200)            ; version-style discriminator
    //   off      = (ver > 0x23) ? 0x118 : 0 ; selects which slot
    //   hp_struct = *(arr + off)            ; HP-bearing object
    //   ; HP int16 at hp_struct + 0x38
    const std::uintptr_t p1 = reinterpret_cast<std::uintptr_t>(param_1);
    const std::uintptr_t x8 = deref8(p1, 0x10);
    if (x8 == 0) return;
    const std::uintptr_t arr = deref8(x8, 0x208);
    if (arr == 0) return;
    const std::uint32_t ver = deref4(x8, 0x200);
    const std::ptrdiff_t off = (ver > 0x23) ? 0x118 : 0;
    const std::uintptr_t hp_struct = deref8(arr, off);
    if (hp_struct == 0) return;

    std::uintptr_t expected = 0;
    if (g_live_base.compare_exchange_strong(
            expected, hp_struct, std::memory_order_release)) {
        SMBWAP_LOG_INFO(
            "PlayerTickLatch: latched live_base=%p "
            "(HP int16 at +0x38; ver=0x%x off=0x%lx)",
            reinterpret_cast<void*>(hp_struct), ver,
            static_cast<unsigned long>(off));
    }
}

bool consumeSyntheticDeathThisFrame() {
    return g_synthetic_death_this_frame.exchange(
        false, std::memory_order_acq_rel);
}

bool synthKill() {
    const std::uintptr_t base = g_live_base.load(std::memory_order_relaxed);
    if (base == 0) {
        SMBWAP_LOG_INFO(
            "synthKill: no live_base latched yet; dropping "
            "(enter a level + walk a few frames so the player tick "
            "trampoline can capture the HP struct)");
        return false;
    }
    // Set the loop-guard BEFORE the write so a death-handler tick that
    // races against us still sees the flag.
    g_synthetic_death_this_frame.store(true, std::memory_order_release);
    // HP is a signed int16 at +0x38.  Writing 0 makes the next
    // `LDRSH W9, [X9, #0x38]; CMP W9, #0; B.LE death` re-evaluate and
    // take the death branch.
    *reinterpret_cast<volatile std::int16_t*>(base + 0x38) = 0;
    SMBWAP_LOG_INFO(
        "synthKill: wrote HP=0 (int16) at live_base=%p +0x38",
        reinterpret_cast<void*>(base));
    return true;
}

}  // namespace probe
