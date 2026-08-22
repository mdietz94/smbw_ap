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
// 2026-06-05 reliability rework #2 (gate-kill / DeathLink "fires but never
// lands" -- a synthKill storm with no resulting DEATH_DETECTED, captured
// live).  Root cause found by decompiling the player tick's death check
// (PlayerTickLatchTarget, NSO +0x2743c0..+0x710027594c):
//
//     if (*(short*)(hp_struct + 0x38) < 1) {            // HP <= 0
//       if (*(char*)(player + 0x110f) == 0              // EDGE LATCH must be 0
//           && (*(char*)(player + 0x110f) = 1,          //   <-- set to 1 FIRST,
//               DAT_7103625850 != 0)                    //   THEN scene-validity test
//           && *(long*)(DAT_7103625850 + 0x40) != 0) {
//         /* write the per-character death-request flag into scene+0x40 */
//       }
//     }
//     // the HP>0 branch (NSO +0x2743cc) clears player+0x110f back to 0.
//
// Death is EDGE-triggered: it fires only on the frame HP first goes <=0
// *and* the latch at player+0x110f is 0.  The latch is set to 1 BEFORE
// the scene-validity test, so any frame the scene global is momentarily
// null (scene transition, Wonder effect, i-frames, or the `tbnz w8,#0xe`
// guard at +0x7100274394 skipping the whole check) poisons the latch to 1
// WITHOUT requesting a death -- and because we keep HP pinned at 0, the
// HP>0 branch that would clear the latch never runs.  A one-shot HP write
// then no-ops forever (the observed storm).
//
// Fix: a synthetic kill is no longer a single HP write.  An inbound Kill
// opens a short "kill window" (kKillActiveTtlTicks); while it's open,
// serviceDeathLink RE-ARMS the edge latch (player+0x110f = 0) AND rewrites
// HP=0 EVERY frame, so the death fires on the next frame the scene is
// valid regardless of an earlier poisoned latch.  The window closes the
// instant a DEATH_DETECTED is observed (consumeSyntheticDeathThisFrame) or
// the TTL expires.  Clearing player+0x110f is exactly what the game's own
// HP>0 branch does each living frame, so the write is safe.
//
// 2026-08-19 in-level settle gate ("DeathLink applied at a weird moment").
// Player reports: an inbound DeathLink could land in the first instants of
// a level -- during the fade-in, before control is handed over, or on the
// respawn frame after a previous death -- producing deaths that look like
// they came out of nowhere, chain-deaths on respawn, and (with the kill
// window above) the occasional double-kill across a scene boundary.
//
// Freshness alone ("the player tick ran within 0.5 s") is NOT a good
// "the player is actually playing" signal: the tick starts running the
// moment the course scene comes up, well before the player can act.  So
// we now track the GAMEPLAY SESSION -- the current uninterrupted run of
// player-tick frames -- and require it to be at least kInLevelSettleTicks
// (10 s) old before a DeathLink-sourced kill may be applied.  The session
// clock restarts on:
//   * first sight of the HP struct (pre-first-level),
//   * a gap in the player tick -- it stops outside a course and across a
//     scene load, so a gap IS "the player just (re-)entered play", and
//   * any frame inside the 3 s scene-transition window (probe::Gates), so
//     a death/respawn or an in-course area change also re-arms the wait.
// A kill that arrives before the session has settled is not dropped: it
// goes on the existing pending-retry slot and fires on the first settled
// frame (TTL below).
//
// GATE KILLS BYPASS THIS.  The level-entry logic gate (client
// _gate_kill_loop, source "SMBW Gate") uses the same Kill message to
// bounce the player out of a course they can't legally be in; delaying
// that would be wrong.  Those arrive with `immediate` set on the wire and
// take the old freshness-only path.
//
// Direct port of switch-mod/src/program/main.cpp:1960-2077, reworked.

#include "probe/DeathLink.hpp"

#include <atomic>
#include <cstdint>

#include "hk/svc/cpu.h"

#include "probe/Gates.hpp"
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

// In-level settle grace (2026-08-19).  A DeathLink-sourced kill may only
// be applied once the current gameplay session (see g_session_start_tick)
// has been running this long -- i.e. the player has been actually playing
// the course for 10 s, not merely "the player tick started this frame".
// Gate kills (`immediate`) skip this.
constexpr std::uint64_t kInLevelSettleTicks = 10ULL * kTicksPerSec;  // 10 s

// Pending inbound-kill TTL.  If a DeathLink arrives while not killable,
// retry on the next gameplay frame for up to this long, then give up so
// a buried kill can't fire minutes later in an unrelated level.
//
// 30 s -> 60 s with the settle gate (2026-08-19): the retry budget now has
// to cover BOTH "not in a course yet" and the 10 s settle that follows
// entering one, so the old 30 s could expire a death the player was about
// to be eligible for.  60 s keeps the "never fires minutes later" property.
constexpr std::uint64_t kPendingKillTtlTicks = 60ULL * kTicksPerSec;  // 60 s

// Active "kill window" TTL.  While open, serviceDeathLink re-arms the
// death edge latch + rewrites HP=0 every frame so a transient poisoned
// latch / scene-null frame can't permanently block the kill (see the
// 2026-06-05 header).  1.5 s is many gameplay frames -- ample to catch a
// scene-valid frame -- yet short enough that it stops hammering quickly if
// the death genuinely can't be applied (it overlaps the 30 s pending retry
// for the longer not-killable-at-all case).  Closed early on the first
// observed DEATH_DETECTED.
constexpr std::uint64_t kKillActiveTtlTicks = (3ULL * kTicksPerSec) / 2;  // 1.5 s

// Offset of the player's death EDGE latch (byte).  PlayerTickLatchTarget
// reads it at the HP<=0 branch (NSO +0x710027593c) -- death fires only when
// it is 0 -- and the HP>0 branch (+0x2743cc) clears it each living frame.
// x23 = player + 0x794; latch = [x23 + 0x97b] => player + 0x110f.
constexpr std::ptrdiff_t kDeathEdgeLatchOff = 0x110f;

// Stable pointer to the live HP-bearing struct, refreshed every frame.
// 0 means "not yet seen" (pre-first-level).
std::atomic<std::uintptr_t> g_live_base{0};
// The player object (PlayerTickLatch param_1), refreshed every frame in
// lockstep with g_live_base.  Needed to re-arm the edge latch above (it
// lives on the player, not on the HP sub-struct).  0 = not yet seen.
std::atomic<std::uintptr_t> g_live_player{0};
// svc system tick of the last successful g_live_base refresh.  Drives the
// freshness check above.
std::atomic<std::uint64_t> g_live_base_tick{0};

// Start tick of the current gameplay session -- the uninterrupted run of
// player-tick frames the player is in right now (0 = no live session yet).
// Restarted by serviceDeathLink on a tick gap, an HP-struct re-allocation,
// or any frame inside the scene-transition window.  `now - this` is the
// "how long have you actually been playing" clock the settle gate reads.
std::atomic<std::uint64_t> g_session_start_tick{0};

// Deadline tick of the open kill window (0 = closed).  While now < this,
// serviceDeathLink re-applies the synthetic kill every fresh frame.
std::atomic<std::uint64_t> g_kill_active_deadline{0};

// Loop guard as a DEADLINE tick (0 = disarmed), not a sticky bool.  Set by
// synthKill right before the HP=0 write so the SceneTransition Nerve
// callback can drop the outbound DEATH_DETECTED echo (the death IS our
// doing) -- but only within kSynthGuardTicks of the write.
std::atomic<std::uint64_t> g_synth_guard_deadline{0};

// Pending inbound DeathLink deadline (0 = none pending).  Set when an
// inbound Kill can't fire immediately; serviced by serviceDeathLink on
// the next fresh (and, for DeathLinks, settled) frame, expired after
// kPendingKillTtlTicks.
std::atomic<std::uint64_t> g_pending_kill_deadline{0};

// Whether the pending kill above is an `immediate` one (a gate kill) and
// therefore skips the in-level settle gate.  Only meaningful while
// g_pending_kill_deadline != 0.  An overlapping arm can escalate this to
// true but never back to false -- a queued gate bounce must not be turned
// into a delayed one by a DeathLink landing on top of it.
std::atomic_bool g_pending_kill_immediate{false};

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

// freshBase() plus the in-level settle requirement: the current gameplay
// session must be at least kInLevelSettleTicks old.  This is the gate an
// inbound DeathLink goes through -- "the player tick is running" is true
// during a course fade-in and on the respawn frame, which is exactly when
// applying a foreign death felt broken (2026-08-19 header).
std::uintptr_t settledBase(std::uint64_t now) {
    const auto base = freshBase(now);
    if (base == 0) return 0;
    const auto start = g_session_start_tick.load(std::memory_order_acquire);
    if (start == 0 || (now - start) < kInLevelSettleTicks) return 0;
    return base;
}

// The killable-base test for a kill of the given urgency: gate kills
// (`immediate`) keep the historical freshness-only rule, DeathLinks must
// also have settled.
std::uintptr_t killableBase(std::uint64_t now, bool immediate) {
    return immediate ? freshBase(now) : settledBase(now);
}

// Arm the loop guard, re-arm the death edge latch, and write HP=0.  Caller
// guarantees `base` is fresh.  `player` may be 0 (latch re-arm skipped) but
// is normally the live player paired with `base`.
//
// Re-arming the edge latch (player+0x110f = 0) is the crux of the
// 2026-06-05 fix: it guarantees the HP<=0 branch sees an un-poisoned latch
// this frame, so the death request fires even if a prior attempt set the
// latch to 1 without completing a death.
void fireSynthKill(std::uintptr_t base, std::uintptr_t player,
                   std::uint64_t now) {
    // Arm the guard BEFORE the write so a death-handler tick that races
    // against us still sees it.
    g_synth_guard_deadline.store(now + kSynthGuardTicks,
                                 std::memory_order_release);
    if (player != 0) {
        *reinterpret_cast<volatile std::int8_t*>(player + kDeathEdgeLatchOff)
            = 0;
    }
    *reinterpret_cast<volatile std::int16_t*>(base + 0x38) = 0;
}

}  // namespace

void serviceDeathLink(void* param_1) {
    const std::uint64_t now = hk::svc::getSystemTick();

    // 1) Refresh the live HP struct + player every frame, in lockstep.
    //    Latch-once goes stale after the first scene transition (the
    //    struct is re-allocated).  hp_struct derives from param_1, so a
    //    non-null hp_struct implies a non-null player.
    const std::uintptr_t hp_struct = walkToHpStruct(param_1);
    if (hp_struct != 0) {
        const auto prev = g_live_base.exchange(hp_struct,
                                               std::memory_order_acq_rel);
        const auto prev_tick = g_live_base_tick.exchange(
            now, std::memory_order_acq_rel);
        g_live_player.store(reinterpret_cast<std::uintptr_t>(param_1),
                            std::memory_order_release);
        if (prev == 0) {
            // First acquisition only -- not per frame.
            SMBWAP_LOG_INFO(
                "[deathlink] live_base acquired %p (HP int16 @ +0x38)",
                reinterpret_cast<void*>(hp_struct));
        }

        // 1b) Maintain the gameplay-session clock the settle gate reads.
        //     The session RESTARTS on either of:
        //       * a gap in the player tick -- it stops entirely outside a
        //         course (world map, menus) and across a scene load, so a
        //         gap is exactly "the player just (re-)entered play"; and
        //       * any frame inside the scene-transition window, which the
        //         SceneTransition Nerve stamps on course/area entry+exit,
        //         death/respawn, palace and shop entry (probe/Gates.cpp).
        //     Deliberately NOT keyed on the HP-struct pointer changing:
        //     the player tick can legitimately be called with different
        //     roots inside one session, and a pointer that alternates
        //     frame-to-frame would restart the clock forever and never let
        //     a DeathLink through.  Every real scene change shows up as a
        //     tick gap and/or a transition window anyway.
        const bool tick_gap =
            prev == 0 || prev_tick == 0 ||
            (now - prev_tick) > kLiveBaseFreshTicks;
        if (tick_gap) {
            // An open kill window belongs to the session it was opened in;
            // never let one bleed across a scene boundary and kill the
            // player again on the other side.
            if (g_kill_active_deadline.exchange(0, std::memory_order_acq_rel)
                != 0) {
                SMBWAP_LOG_INFO(
                    "[deathlink] kill window closed by session change "
                    "(player tick gap)");
            }
            SMBWAP_LOG_INFO(
                "[deathlink] gameplay session started; inbound DeathLinks "
                "hold for %u s of play",
                static_cast<unsigned>(kInLevelSettleTicks / kTicksPerSec));
        }
        if (tick_gap || isInSceneTransitionWindow()) {
            g_session_start_tick.store(now, std::memory_order_release);
        }
    }

    // 2) Service a pending inbound DeathLink that couldn't fire when it
    //    arrived (player was mid-transition / in a menu).  Fire as soon as
    //    we're back in a killable state; expire if we never get there.
    //    Firing opens the kill window so step 3 keeps re-applying it (a
    //    single write is unreliable -- see the 2026-06-05 header).
    const auto deadline = g_pending_kill_deadline.load(std::memory_order_acquire);
    if (deadline != 0) {
        if (now > deadline) {
            g_pending_kill_deadline.store(0, std::memory_order_release);
            g_pending_kill_immediate.store(false, std::memory_order_release);
            SMBWAP_LOG_WARN(
                "[deathlink] pending kill expired unfired (no killable, "
                "settled frame within %u s)",
                static_cast<unsigned>(kPendingKillTtlTicks / kTicksPerSec));
        } else {
            const bool immediate =
                g_pending_kill_immediate.load(std::memory_order_acquire);
            const auto base = killableBase(now, immediate);
            if (base != 0) {
                g_pending_kill_deadline.store(0, std::memory_order_release);
                g_pending_kill_immediate.store(false,
                                               std::memory_order_release);
                g_kill_active_deadline.store(now + kKillActiveTtlTicks,
                                             std::memory_order_release);
                fireSynthKill(base, g_live_player.load(std::memory_order_acquire),
                              now);
                SMBWAP_LOG_INFO(
                    "[deathlink] pending %s applied (HP=0 @ %p) after %u ms "
                    "in the current gameplay session",
                    immediate ? "gate kill" : "DeathLink",
                    reinterpret_cast<void*>(base),
                    static_cast<unsigned>(
                        (now - g_session_start_tick.load(
                                   std::memory_order_acquire))
                        * 1000ULL / kTicksPerSec));
            }
        }
    }

    // 3) Keep an open kill window alive: while it hasn't expired and a
    //    DEATH_DETECTED hasn't yet closed it, re-arm the death edge latch
    //    and rewrite HP=0 on EVERY fresh frame.  This is what makes a
    //    synthetic kill land reliably across a poisoned latch / transient
    //    scene-null frame (the death fires on the first frame the scene is
    //    valid).  No per-frame log -- this runs at ~60 Hz while armed.
    const auto kill_deadline =
        g_kill_active_deadline.load(std::memory_order_acquire);
    if (kill_deadline != 0) {
        if (now > kill_deadline) {
            g_kill_active_deadline.store(0, std::memory_order_release);
        } else {
            const auto base = freshBase(now);
            if (base != 0) {
                fireSynthKill(base,
                              g_live_player.load(std::memory_order_acquire),
                              now);
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
    // Our synthetic death landed -- close the kill window so step 3 stops
    // re-applying HP=0 (otherwise it would keep hammering for the rest of
    // the TTL and could clobber the respawn).
    g_kill_active_deadline.store(0, std::memory_order_release);
    return true;  // within window -> this death is our synthetic one, suppress
}

bool synthKill(bool immediate) {
    const std::uint64_t now = hk::svc::getSystemTick();
    const auto base = killableBase(now, immediate);
    if (base == 0) {
        // Not killable right now: live_base never seen or gone stale
        // (menu / world map / teardown), or -- for a DeathLink -- the
        // current gameplay session hasn't settled yet (course fade-in,
        // respawn, mid-transition).  Caller arms a pending retry, which
        // fires this on the first frame that does qualify.
        return false;
    }
    // Open the kill window only now that we're actually firing.  Opening
    // it before the check would let step 3 of serviceDeathLink re-apply
    // HP=0 on the next fresh frame and walk straight past the settle gate.
    g_kill_active_deadline.store(now + kKillActiveTtlTicks,
                                 std::memory_order_release);
    // Poison detector: an already-armed guard means the PRIOR synthetic
    // death's DEATH_DETECTED was never observed -- that kill likely didn't
    // land.  Harmless now (guard auto-expires + the window re-applies) but
    // worth surfacing.
    if (g_synth_guard_deadline.load(std::memory_order_acquire) != 0) {
        SMBWAP_LOG_WARN(
            "[deathlink] synthKill: prior synthetic death's DEATH_DETECTED "
            "never observed -- previous kill may not have landed");
    }
    fireSynthKill(base, g_live_player.load(std::memory_order_acquire), now);
    SMBWAP_LOG_INFO("[deathlink] synthKill(%s): HP=0 @ %p (live_base fresh; "
                    "kill window open ~1.5s, re-arming edge latch)",
                    immediate ? "immediate" : "settled",
                    reinterpret_cast<void*>(base));
    return true;
}

void requestPendingDeathLink(bool immediate) {
    const std::uint64_t now = hk::svc::getSystemTick();
    // Escalate-only on the urgency flag: a fresh arm takes the caller's
    // value, but a DeathLink arriving on top of an already-queued gate
    // kill must not downgrade that bounce into a delayed one.
    if (immediate ||
        g_pending_kill_deadline.load(std::memory_order_acquire) == 0) {
        g_pending_kill_immediate.store(immediate, std::memory_order_release);
    }
    g_pending_kill_deadline.store(now + kPendingKillTtlTicks,
                                  std::memory_order_release);
}

}  // namespace probe
