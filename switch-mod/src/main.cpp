// SMBW Archipelago — hakkun edition entry point.
//
// Phase 2a of the hakkun migration (docs/hakkun-migration-plan.md):
// port the 3 CORE_INIT trampolines from the legacy exlaunch main.cpp
// (still on disk at src/program/main.cpp, excluded from build) to
// HkTrampoline + installAtMainOffset(). This is the smallest meaningful
// slice that proves hakkun's offset-trampoline machinery works on SMBW
// and unblocks the SD-log diagnostic story by giving us a callback we
// can drive `drainPendingToFile()` from.
//
// Callbacks are intentionally *minimal* in this PR:
//   - Each fires once during init; we log entry/exit and call Orig.
//   - CreateFileDeviceMgr's callback also calls drainPendingToFile(),
//     which is safe here because Orig has already returned (the sead
//     FileDeviceMgr is constructed and the SD-card device is mountable).
//     drainPendingToFile() does its own one-shot nn::fs::Mount inside.
//   - The full callback bodies from the legacy main.cpp (heap init,
//     socket bring-up, ApClient::start) stay parked until Phase 2b/c
//     when we restore the ap/ subsystem and the sead/nn headers it
//     depends on.
//
// Hook signatures are `void(void*)` and `void(void*, const void*)`
// rather than `void(sead::FileDeviceMgr*, ...)` etc. — Phase 2a doesn't
// need to dereference the params, so we sidestep the lib/ headers
// entirely. Phase 2b can tighten the types.

#include "hk/hook/Trampoline.h"
#include "hk/svc/api.h"
#include "hk/types.h"

#include "util/Log.hpp"

namespace {

// CORE_INIT: CreateRootHeap @ NSO +0x005a66f8.
// In the legacy build this is followed by smbwap-specific `initHeap()`;
// Phase 2a just chains through Orig so the game's allocator still ends
// up wired the way the rest of SMBW expects.
HkTrampoline<void, void*> createRootHeapHook = hk::hook::trampoline(
    [](void* thisPtr) -> void {
        SMBWAP_LOG_INFO("hook: CreateRootHeap fire (pre)");
        createRootHeapHook.orig(thisPtr);
        SMBWAP_LOG_INFO("hook: CreateRootHeap fire (post)");
    });

// CORE_INIT: CreateFileDeviceMgr @ NSO +0x005a6110.
// First hook where on-device fs is available after Orig — we use it as
// the one-shot drain trigger for sd:/smbwap_boot.log. The drain itself
// is idempotent via a static atomic_bool, so repeat fires (shouldn't
// happen) are harmless.
HkTrampoline<void, void*> createFileDeviceMgrHook = hk::hook::trampoline(
    [](void* thisPtr) -> void {
        SMBWAP_LOG_INFO("hook: CreateFileDeviceMgr fire (pre)");
        createFileDeviceMgrHook.orig(thisPtr);
        SMBWAP_LOG_INFO("hook: CreateFileDeviceMgr fire (post) — draining to SD");
        smbwap::util::drainPendingToFile();
    });

// CORE_INIT: GameFrameworkInitialize @ NSO +0x005a5cfc.
// Phase 2b will restore the socket-init + ApClient::start logic that
// the legacy callback ran here. Phase 2a is just chain-through.
HkTrampoline<void, void*, const void*> gameFrameworkInitializeHook =
    hk::hook::trampoline(
        [](void* thisPtr, const void* arg) -> void {
            SMBWAP_LOG_INFO("hook: GameFrameworkInitialize fire (pre)");
            gameFrameworkInitializeHook.orig(thisPtr, arg);
            SMBWAP_LOG_INFO("hook: GameFrameworkInitialize fire (post)");
        });

void installHook(const char* name, ::ptr offset, hk::Result rc) {
    if (rc.failed()) {
        SMBWAP_LOG_ERROR("install %s @ +0x%lx FAILED rc=0x%x",
                         name, static_cast<unsigned long>(offset),
                         static_cast<unsigned>(rc.getValue()));
    } else {
        SMBWAP_LOG_INFO("install %s @ +0x%lx OK", name,
                        static_cast<unsigned long>(offset));
    }
}

}  // namespace

extern "C" void hkMain() {
    SMBWAP_LOG_INFO("=== smbwap hkMain START ===");
    SMBWAP_LOG_INFO("Phase 2a: 3 CORE_INIT hooks via HkTrampoline.installAtMainOffset");

    installHook("CreateRootHeap", 0x005a66f8,
                createRootHeapHook.installAtMainOffset(0x005a66f8));
    installHook("CreateFileDeviceMgr", 0x005a6110,
                createFileDeviceMgrHook.installAtMainOffset(0x005a6110));
    installHook("GameFrameworkInitialize", 0x005a5cfc,
                gameFrameworkInitializeHook.installAtMainOffset(0x005a5cfc));

    SMBWAP_LOG_INFO("=== smbwap hkMain END ===");
}
