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
// internal dereference chain EVERY frame to land at the live HP struct.
// Trampoline at function entry avoids the relocator-corrupting inline
// hook at +0x2743BC that an earlier attempt tried.
//
// 2026-05-29 reliability rework (DeathLink "inconsistent" bug).  Three
// defects fixed, all rooted in the original latch-once + sticky-bool
// design:
//   1. live_base was latched ONCE and never refreshed.  The HP struct is
//      re-allocated per scene, so after the first level synthKill wrote
//      HP=0 into a stale pointer and silently no-op'd.  Fix: re-walk and
//      refresh live_base every frame; stamp the refresh tick so synthKill
//      can tell "active gameplay" from "menu / world-map / teardown".
//   2. synthKill returned true on the WRITE succeeding, not on a death
//      resulting, and armed a sticky loop-guard bool.  A synthetic kill
//      that didn't land left the guard armed; the next GENUINE death was
//      then consumed by that stale guard and never sent to AP.  Fix: the
//      guard is now a deadline tick that auto-expires (~1 s); an expired
//      guard at consume time is logged and treated as a genuine death.
//   3. an inbound Kill that arrived while not in a killable state was
//      dropped outright.  Fix: arm a bounded pending retry that fires on
//      the next active-gameplay frame (so the death lands when you're
//      back in play instead of being lost).
//
// Direct port of switch-mod/src/program/main.cpp:1960-2077, reworked.

#include "probe/DeathLink.hpp"

#include <atomic>
#include <cstdint>

#include "hk/svc/cpu.h"

#include "util/Log.hpp"

namespace probe {

namespace {

// 19.2 MHz ARM generic timer tick rate (matches Gates.cpp).
constexpr std::uint64_t kTicksPerSec = 19'200'000ULL;

// Player-tick freshness window.  serviceDeathLink refreshes g_live_base
// every frame the player tick runs (~60 Hz in active gameplay).  If the
// last refresh is older than this, we are NOT in a killable state (menu,
// world-map, scene teardown) and synthKill must not write -- the struct
// is stale or mid-tear-down.  0.5 s tolerates frame hitches without
// admitting a whole scene transition.
constexpr std::uint64_t kLiveBaseFreshTicks = kTicksPerSec / 2;  // 0.5 s

// Loop-guard lifetime.  A synthetic HP=0 produces its DEATH_DETECTED
// nerve fire within ~30 ms in practice (observed 1-26 ms in live logs).
// 1 s is a generous ceiling.  Past it the guard auto-disarms so it can
// never suppress a *later* genuine death (defect #2 above).
constexpr std::uint64_t kSynthGuardTicks = kTicksPerSec;  // 1 s

// Pending inbound-kill TTL.  If a DeathLink arrives while not killable,
// retry on the next gameplay frame for up to this long, then give up so
// a buried kill can't fire minutes later in an unrelated level.
constexpr std::uint64_t kPendingKillTtlTicks = 30ULL * kTicksPerSec;  // 30 s

// Stable pointer to the live HP-bearing struct, refreshed every frame.
// 0 means "not yet seen" (pre-first-level).
std::atomic<std::uintptr_t> g_live_base{0};
// svc system tick of the last successful g_live_base refresh.  Drives the
// freshness check above.
std::atomic<std::uint64_t> g_live_base_tick{0};

// Loop guard as a DEADLINE tick (0 = disarmed), not a sticky bool.  Set by
// synthKill right before the HP=0 write so the SceneTransition Nerve
// callback can drop the outbound DEATH_DETECTED echo (the death IS our
// doing) -- but only within kSynthGuardTicks of the write.
std::atomic<std::uint64_t> g_synth_guard_deadline{0};

// Pending inbound DeathLink deadline (0 = none pending).  Set when an
// inbound Kill can't fire immediately; serviced by serviceDeathLink on
// the next fresh frame, expired after kPendingKillTtlTicks.
std::atomic<std::uint64_t> g_pending_kill_deadline{0};

inline std::uintptr_t deref8(std::uintptr_t p, std::ptrdiff_t off) {
    return *reinterpret_cast<std::uintptr_t*>(p + off);
}
inline std::uint32_t deref4(std::uintptr_t p, std::ptrdiff_t off) {
    return *reinterpret_cast<std::uint32_t*>(p + off);
}

// Replicates the player-tick's own walk at +0x2743A0..+0x2743B8 to reach
// the HP-bearing struct.  Returns 0 if param_1 is null or any deref in the
// chain hits null (expected outside active gameplay).
//   x8        = *(param_1 + 0x10)        ; tick context
//   arr       = *(x8 + 0x208)            ; sub-array
//   ver       = *(x8 + 0x200)            ; version-style discriminator
//   off       = (ver > 0x23) ? 0x118 : 0 ; selects which slot
//   hp_struct = *(arr + off)             ; HP-bearing object (HP int16 @ +0x38)
std::uintptr_t walkToHpStruct(void* param_1) {
    if (param_1 == nullptr) return 0;
    const std::uintptr_t p1 = reinterpret_cast<std::uintptr_t>(param_1);
    const std::uintptr_t x8 = deref8(p1, 0x10);
    if (x8 == 0) return 0;
    const std::uintptr_t arr = deref8(x8, 0x208);
    if (arr == 0) return 0;
    const std::uint32_t ver = deref4(x8, 0x200);
    const std::ptrdiff_t off = (ver > 0x23) ? 0x118 : 0;
    return deref8(arr, off);
}

// Returns a non-null base only if g_live_base is set AND was refreshed
// within the freshness window (player tick actively running -> killable).
std::uintptr_t freshBase(std::uint64_t now) {
    const auto base = g_live_base.load(std::memory_order_acquire);
    if (base == 0) return 0;
    const auto t = g_live_base_tick.load(std::memory_order_acquire);
    if (now - t > kLiveBaseFreshTicks) return 0;
    return base;
}

// Arm the loop guard and write HP=0.  Caller guarantees `base` is fresh.
void fireSynthKill(std::uintptr_t base, std::uint64_t now) {
    // Arm the guard BEFORE the write so a death-handler tick that races
    // against us still sees it.
    g_synth_guard_deadline.store(now + kSynthGuardTicks,
                                 std::memory_order_release);
    *reinterpret_cast<volatile std::int16_t*>(base + 0x38) = 0;
}

}  // namespace

void serviceDeathLink(void* param_1) {
    const std::uint64_t now = hk::svc::getSystemTick();

    // 1) Refresh the live HP struct every frame.  Latch-once goes stale
    //    after the first scene transition (the struct is re-allocated).
    const std::uintptr_t hp_struct = walkToHpStruct(param_1);
    if (hp_struct != 0) {
        const auto prev = g_live_base.exchange(hp_struct,
                                               std::memory_order_acq_rel);
        g_live_base_tick.store(now, std::memory_order_release);
        if (prev == 0) {
            // First acquisition only -- not per frame.
            SMBWAP_LOG_INFO(
                "[deathlink] live_base acquired %p (HP int16 @ +0x38)",
                reinterpret_cast<void*>(hp_struct));
        }
    }

    // 2) Service a pending inbound DeathLink that couldn't fire when it
    //    arrived (player was mid-transition / in a menu).  Fire as soon as
    //    we're back in a killable state; expire if we never get there.
    const auto deadline = g_pending_kill_deadline.load(std::memory_order_acquire);
    if (deadline != 0) {
        if (now > deadline) {
            g_pending_kill_deadline.store(0, std::memory_order_release);
            SMBWAP_LOG_WARN(
                "[deathlink] pending kill expired unfired "
                "(no killable frame within 30 s)");
        } else {
            const auto base = freshBase(now);
            if (base != 0) {
                g_pending_kill_deadline.store(0, std::memory_order_release);
                fireSynthKill(base, now);
                SMBWAP_LOG_INFO(
                    "[deathlink] pending kill applied on return to "
                    "gameplay (HP=0 @ %p)", reinterpret_cast<void*>(base));
            }
        }
    }
}

bool consumeSyntheticDeathThisFrame() {
    const std::uint64_t now = hk::svc::getSystemTick();
    const auto deadline = g_synth_guard_deadline.exchange(
        0, std::memory_order_acq_rel);
    if (deadline == 0) {
        return false;  // not armed -> genuine death, report it
    }
    if (now > deadline) {
        // The synthetic kill that armed this guard never produced a timely
        // DEATH_DETECTED, so THIS fire is a later, genuine death.  Pre-fix
        // it would have been silently swallowed (defect #2).
        SMBWAP_LOG_WARN(
            "[deathlink] stale synthetic-death guard expired; treating "
            "this DEATH_DETECTED as genuine (not suppressing)");
        return false;
    }
    return true;  // within window -> this death is our synthetic one, suppress
}

bool synthKill() {
    const std::uint64_t now = hk::svc::getSystemTick();
    const auto base = freshBase(now);
    if (base == 0) {
        // Either never seen, or live_base went stale: player isn't in an
        // active, killable gameplay frame.  Caller arms a pending retry.
        return false;
    }
    // Poison detector: an already-armed guard means the PRIOR synthetic
    // death's DEATH_DETECTED was never observed -- that kill likely didn't
    // land.  Harmless now (guard auto-expires) but worth surfacing.
    if (g_synth_guard_deadline.load(std::memory_order_acquire) != 0) {
        SMBWAP_LOG_WARN(
            "[deathlink] synthKill: prior synthetic death's DEATH_DETECTED "
            "never observed -- previous kill may not have landed");
    }
    fireSynthKill(base, now);
    SMBWAP_LOG_INFO("[deathlink] synthKill: HP=0 @ %p (live_base fresh)",
                    reinterpret_cast<void*>(base));
    return true;
}

void requestPendingDeathLink() {
    const std::uint64_t now = hk::svc::getSystemTick();
    g_pending_kill_deadline.store(now + kPendingKillTtlTicks,
                                  std::memory_order_release);
}

}  // namespace probe
