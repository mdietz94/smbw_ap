// M4.5 save-loaded gate + M3.8 scene-transition gate (Phase 2g port).
//
// Replaces the permissive Stubs.cpp defaults.  Wired into the game-thread
// signals from main.cpp:
//   * markSaveLoaded(...) -- called from gmdContainerAWriterHook AND
//     gmdBoolWriterHook before the bounded log.  Order matters: the
//     latch must run BEFORE the rate-limiter that drops calls past
//     fire #50, otherwise a fast-save-load burst could skip past it.
//   * latchSceneTransitionTick(now) -- called from nerveActivateOnceHook
//     when `vt_off == 0x33fd9a8` (the SceneTransition vtable).
//
// Reader side (called from ap::drainInbound):
//   * isSaveLoaded() -- false until first writer fire.  drainInbound
//     pops the SPSC ring AND defers all writes until this opens.
//   * isInSceneTransitionWindow() -- true while elapsed < 3 s (19.2 MHz
//     tick rate).  drainInbound defers ALL container writers during the
//     window; all bridge messages are either AP-replayed on next tick or
//     idempotent absolute-overwrite, so the wait costs at most ~3 s of
//     staleness with no progress loss.

#include "probe/Gates.hpp"

#include <atomic>
#include <cstdint>

#include "hk/svc/cpu.h"

#include "util/Log.hpp"

namespace probe {

namespace {

// Flips true the FIRST time the save deserializer touches container A
// (FUN_710049F648) or the container-B bool delegate (FUN_71001F263FC).
// Both paths are exclusively taken by the deserializer at this stage of
// boot because drainInbound never produces a writer call until
// isSaveLoaded() is true (chicken-and-egg makes the first observed
// write provably game-initiated).  Once set, stays set for the process
// lifetime -- if the player swaps saves, the new save's container
// writes still target the new gmd singleton.
std::atomic_bool s_save_loaded{false};

// Stamped (svc::getSystemTick()) when the SceneTransition Nerve at
// vt_off == 0x33fd9a8 fires.  Covers every transition family we've
// observed (death, course/area entry+exit, world-map travel, palace,
// Poplin shop entry, post-Wonder-Seed cleanup).  3 s @ 19.2 MHz tick
// rate is generous -- gameplay is paused or non-interactive during
// transitions, so a brief gate has no visible cost.
std::atomic<std::uint64_t> s_last_scene_transition_tick{0};

// 19.2 MHz ARM generic timer tick rate -> 3 second window.  This must
// stay in sync with the legacy constant of the same name in
// switch-mod/src/program/main.cpp around line 52.
constexpr std::uint64_t kSceneTransitionGateTicks = 3ULL * 19'200'000ULL;

}  // namespace

void markSaveLoaded(const char* via) {
    const bool prev = s_save_loaded.exchange(true, std::memory_order_acq_rel);
    if (!prev) {
        SMBWAP_LOG_INFO(
            "save data loaded (via %s); AP grants will now drain", via);
    }
}

void latchSceneTransitionTick(std::uint64_t now_tick) {
    s_last_scene_transition_tick.store(now_tick, std::memory_order_relaxed);
}

bool isSaveLoaded() {
    return s_save_loaded.load(std::memory_order_acquire);
}

bool isInSceneTransitionWindow() {
    const auto last_trans =
        s_last_scene_transition_tick.load(std::memory_order_relaxed);
    if (last_trans == 0) return false;
    const auto now = hk::svc::getSystemTick();
    return (now - last_trans) < kSceneTransitionGateTicks;
}

}  // namespace probe
