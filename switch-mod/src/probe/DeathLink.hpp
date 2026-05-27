// M3.8 DeathLink helpers exposed to main.cpp.
//
// The PlayerTickLatch trampoline in main.cpp (NSO +0x273868) calls
// `tryLatchPlayerHpStruct` on first entry to capture the live HP-bearing
// struct.  The SceneTransition branch in NerveActivateOnce calls
// `consumeSyntheticDeathThisFrame` to suppress the outbound DEATH_DETECTED
// echo when the death was caused by our own probe::synthKill (loop guard).
//
// All implementations live in DeathLink.cpp; this header only exposes the
// two helpers main.cpp needs.

#pragma once

#include <cstdint>

namespace probe {

// Called once per frame from PlayerTickLatch trampoline with param_1 (=
// the scene/manager root the player tick walks from).  Walks the same
// dereference chain the game's internal death-check would (replicates
// the body of FUN_7100273868 at +0x2743A0..+0x2743B8) to land at the HP
// struct, then CAS-installs it into the static g_live_base atomic.
// Once latched, subsequent calls are an atomic load + branch.  No-op
// if param_1 == 0 or any deref in the chain hits null.
void tryLatchPlayerHpStruct(void* param_1);

// Atomically test-and-clear the synthetic-death loop guard.  Returns
// true exactly once per probe::synthKill() fire -- the next
// SceneTransition Nerve callback should drop its DEATH_DETECTED enqueue
// (the death IS our doing).  Subsequent calls return false until the
// next synthKill.
bool consumeSyntheticDeathThisFrame();

}  // namespace probe
