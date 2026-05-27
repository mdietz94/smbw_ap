// M4.5 save-loaded latch + M3.8 scene-transition gate -- hooks in main.cpp
// drive these via the two callbacks below.  The reader counterparts live
// in ApFrameBridge.hpp as `probe::isSaveLoaded()` /
// `probe::isInSceneTransitionWindow()` (called from drainInbound on the
// game thread).
//
// `markSaveLoaded`: flipped true on first observed call to either of the
// gmd container writers (GmdContainerAWriter / GmdBoolWriter).  Idempotent
// after the first call -- if the player swaps saves, the new container's
// writes still target the new gmd singleton, so we don't need to clear.
//
// `latchSceneTransitionTick`: stamped on every SceneTransition Nerve fire
// (vt_off == 0x33fd9a8).  Reader compares to the current svc system tick
// and gates if delta < kSceneTransitionGateTicks.

#pragma once

#include <cstdint>

namespace probe {

void markSaveLoaded(const char* via);
void latchSceneTransitionTick(std::uint64_t now_tick);

}  // namespace probe
