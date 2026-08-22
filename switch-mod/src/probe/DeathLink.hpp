// M3.8 DeathLink helpers exposed to main.cpp.
//
// The PlayerTickLatch trampoline in main.cpp (NSO +0x273868) calls
// `serviceDeathLink` every frame to (a) refresh the live HP-bearing struct
// and (b) retry a pending inbound DeathLink once the player is back in a
// killable state.  The SceneTransition branch in NerveActivateOnce calls
// `consumeSyntheticDeathThisFrame` to suppress the outbound DEATH_DETECTED
// echo when the death was our own probe::synthKill (loop guard).
//
// `synthKill` / `requestPendingDeathLink` live in ApFrameBridge.hpp (called
// from drainInbound's Kill case).  All implementations live in
// DeathLink.cpp; this header exposes only the two helpers main.cpp needs.
//
// serviceDeathLink also maintains the in-level settle clock (2026-08-19):
// an inbound DeathLink is held until the player has been playing the
// current course for 10 s, so a foreign death can't land during a course
// fade-in, on a respawn frame, or mid-transition.  Gate kills bypass it.

#pragma once

#include <cstdint>

namespace probe {

// Called once per frame from the PlayerTickLatch trampoline with param_1
// (= the scene/manager root the player tick walks from).  Re-walks the
// same dereference chain the game's death-check uses (FUN_7100273868 at
// +0x2743A0..+0x2743B8) to the HP struct and refreshes g_live_base + its
// freshness tick EVERY frame -- the struct is re-allocated per scene, so a
// one-time latch goes stale.  Also services a pending inbound DeathLink:
// if one is queued and the player is now in a killable state, it fires the
// synthetic kill here.  No-op on the parts whose preconditions aren't met
// (null param_1 / null deref / no pending kill).
void serviceDeathLink(void* param_1);

// Atomically test-and-clear the synthetic-death loop guard.  Returns true
// iff a probe::synthKill fired within the last ~1 s (kSynthGuardTicks) --
// in which case the caller (the death-discriminator branch) should drop
// its DEATH_DETECTED enqueue because the death is our own.  An armed but
// EXPIRED guard returns false (and warns): the synthetic kill never landed
// a timely death, so this fire is a later genuine death and must be sent.
bool consumeSyntheticDeathThisFrame();

}  // namespace probe
