// See TargetActorProbe.hpp.
//
// We don't want to flood the log with one line per actor (the level dump
// already proved that's ~6k+ entries). Filter to a hard-coded target list
// matching M1's outgoing-hook surface, and for each target log:
//
//   - vtable NSO-relative offset (so it can be pasted into Ghidra)
//   - the first 8 virtual-slot function offsets (IActor.h says slot 5 is
//     ~IActor(); Itanium ABI splits destructors into D1/D0, so slot 5 +
//     slot 6 typically *are* the two destructor variants)
//
// Each unique vtable is logged ONCE per session — multiple instances of
// the same class share a vtable, so subsequent constructions are noisy
// duplicates.

#include "util/TargetActorProbe.hpp"

#include <cstdint>
#include <cstring>

#include "engine/actor/IActor.h"
#include "game/actor/ActorPlacementInfo.h"
#include "util/Log.hpp"
#include "util/modules.hpp"

namespace smbwap::probe {

namespace {

constexpr const char* kTargetNames[] = {
    "ObjectGoalPole",
    "ObjectGoalPoleFort",
    "ItemWonderFinishWonderSead",
    "ObjectBigTenLuckyCoin",
    "ObjectMiniLuckyCoin",
};

// Track up to N seen vtables (per target). Bounded array, no allocation.
constexpr int kMaxSeen = 16;
uintptr_t g_seen_vtables[kMaxSeen] = {0};
int g_seen_count = 0;

bool already_logged(uintptr_t vtable) {
    for (int i = 0; i < g_seen_count; ++i) {
        if (g_seen_vtables[i] == vtable) return true;
    }
    if (g_seen_count < kMaxSeen) {
        g_seen_vtables[g_seen_count++] = vtable;
    }
    return false;
}

bool is_target(const char* name) {
    if (!name) return false;
    for (const char* candidate : kTargetNames) {
        if (std::strcmp(name, candidate) == 0) return true;
    }
    return false;
}

}  // namespace

void onActorConstructed(engine::actor::IActor* /*actor*/, void* /*x1*/) {
    // Disabled: ActorBase ctor at 0x231204 doesn't see game-level actors on
    // this build, so this probe can never match a target. Function kept as
    // a link-time stub for the existing call site in ActorBrowser.cpp.
    return;
}

// Original target-vtable probe body, retained for reference. Reactivate
// once we find a ctor or init hook point that actually sees the leaf
// game actors. The current path:
//   1. Use Ghidra to locate the vtable for ObjectGoalPole etc.
//   2. Install HOOK_DEFINE_TRAMPOLINE on a chosen virtual slot directly.
#if 0
void onActorConstructed_orig(engine::actor::IActor* actor, void* x1) {
    if (!actor) return;
    const char* name = nullptr;
    if (x1) {
        auto* pi = reinterpret_cast<game::actor::ActorPlacementInfo*>(x1);
        name = pi->mActorArchiveName;
    }
    if (!is_target(name)) return;

    uintptr_t vtable = *reinterpret_cast<uintptr_t*>(actor);
    if (already_logged(vtable)) return;

    const uintptr_t targetBase = exl::util::modules::GetTargetStart();
    const uintptr_t vt_off = vtable - targetBase;

    // Dump the first 8 virtual slots. IActor.h's declared layout:
    //   slot 0: field_10
    //   slot 1: getComponentPropMap
    //   slot 2: getActorName
    //   slot 3: getActorName2
    //   slot 4: getHostIOIcon
    //   slot 5: ~IActor (D1, complete-object dtor under Itanium ABI)
    //   slot 6: ~IActor (D0, deleting dtor — usually the right hook target
    //                    for "actor was destroyed and freed")
    //   slot 7+: subclass virtuals (init, update, kill, onTouch, etc.)
    uintptr_t* vtbl = reinterpret_cast<uintptr_t*>(vtable);
    SMBWAP_LOG_INFO("target_vtable: %s this=%p vt_nso=0x%llx slots=[%llx %llx %llx %llx %llx %llx %llx %llx]",
                    name,
                    actor,
                    static_cast<unsigned long long>(vt_off),
                    static_cast<unsigned long long>(vtbl[0] - targetBase),
                    static_cast<unsigned long long>(vtbl[1] - targetBase),
                    static_cast<unsigned long long>(vtbl[2] - targetBase),
                    static_cast<unsigned long long>(vtbl[3] - targetBase),
                    static_cast<unsigned long long>(vtbl[4] - targetBase),
                    static_cast<unsigned long long>(vtbl[5] - targetBase),
                    static_cast<unsigned long long>(vtbl[6] - targetBase),
                    static_cast<unsigned long long>(vtbl[7] - targetBase));
}
#endif

}  // namespace smbwap::probe
