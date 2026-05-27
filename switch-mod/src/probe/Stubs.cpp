// Phase 2f -- no-op stubs for the `probe::` namespace.
//
// The legacy exlaunch main.cpp had ~2700 lines of probe:: implementation:
// the M3 grant primitives (setBadgeBitfieldAbsolute, grantContainerACounter,
// grantContainerBBool, setPerCourseBitfieldAbsolute, setContainerCBit,
// synthKill), the M4.5 save-loaded gate, the M3.8 scene-transition gate,
// the Wonder Seed override, and the per-frame state machinery.
//
// Phase 2f restores ap/ but defers the probe:: restoration. ApFrameBridge
// forward-declares 12 probe:: functions; without implementations the link
// would fail. These stubs let ap/ link cleanly. Functional effect:
//   * Outbound flows work (Nerve fires + PlayReport captures push to the
//     bridge, which forwards to the AP server).
//   * Inbound dispatch lands in ApFrameBridge::drainInbound, finds the
//     stubs, logs "probe stub fired", returns false. Inbound grants ARE
//     received but never written to the live game state.
//
// Phase 2g (next slice) restores these functions for-real: starts with
// the gates (isSaveLoaded, isInSceneTransitionWindow) + the two writers
// the GRANTS hooks already cover (grantContainerACounter via
// FUN_710049F648, grantContainerBBool via FUN_710049EA24). The
// remaining 8 follow over Phase 2g-i.

#include <atomic>
#include <cstdint>

#include "util/Log.hpp"

namespace probe {

namespace {
// Bounded log: each stub logs up to 5 times then goes silent. Avoids
// flooding the ring during steady-state bridge traffic.
template <int Slot>
bool logStub(const char* name) {
    static std::atomic<int> s_count{0};
    const int n = s_count.fetch_add(1, std::memory_order_relaxed);
    if (n < 5) {
        SMBWAP_LOG_INFO("probe::%s stub fired #%d (Phase 2g will implement)",
                        name, n + 1);
    }
    return false;
}
}  // namespace

bool setBadgeBitfieldAbsolute(std::uint64_t /*bits*/) {
    return logStub<1>("setBadgeBitfieldAbsolute");
}

// grantContainerACounter + incrementContainerACounter moved to
// ContainerA.cpp in Phase 2g commit 2.

// grantContainerBBool moved to ContainerB.cpp in Phase 2g commit 3.

bool setPerCourseBitfieldAbsolute(std::uint32_t /*hash*/,
                                  std::uint32_t /*course_index*/,
                                  std::uint32_t /*bitmask*/) {
    return logStub<5>("setPerCourseBitfieldAbsolute");
}

bool setContainerCBit(std::uint32_t /*hash*/, std::uint32_t /*bit_index*/,
                      bool /*value*/) {
    return logStub<6>("setContainerCBit");
}

bool dumpSaveField(std::uint32_t /*base_nso_offset*/,
                   std::uint32_t /*field_offset*/) {
    return logStub<7>("dumpSaveField");
}

bool synthKill() {
    return logStub<8>("synthKill");
}

// isSaveLoaded() + isInSceneTransitionWindow() moved to Gates.cpp in
// Phase 2g commit 1 -- both are now driven by real game-thread signals
// (markSaveLoaded from gmd writer hooks; latchSceneTransitionTick from
// the SceneTransition Nerve callback).

void pushWonderSeedOverride(std::uint32_t /*value*/) {
    (void)logStub<10>("pushWonderSeedOverride");
}
void pushWonderSeedOverrideCurrentWorld() {
    (void)logStub<11>("pushWonderSeedOverrideCurrentWorld");
}

}  // namespace probe
