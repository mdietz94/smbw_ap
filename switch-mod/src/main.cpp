// SMBW Archipelago — hakkun edition entry point.
//
// Phase 2b of the hakkun migration (docs/hakkun-migration-plan.md): port
// the 3 M1_EVENTS trampolines (NerveActivateOnce, SetCourseClearFlagExecute,
// GameGoalReachedExecute) from the legacy exlaunch main.cpp. These are the
// core AP location-check hooks — Wonder Seed pickup, course clear, and
// final-Bowser game-goal.
//
// Phase 2b is "install + observe" only:
//   * Each callback chains through Orig and logs the fire.
//   * NerveActivateOnce additionally reads `nerve[0]` (the vtable pointer)
//     and logs the NSO-relative vtable offset, with a distinct-vtable dedup
//     so we don't flood the ring (this helper fires on every Nerve
//     one-shot activation — many per second in active gameplay).
//
// What Phase 2b explicitly does NOT do (returns in Phase 2c+):
//   * The smbwap::ap::enqueueNerveFire() bridge wire-up.
//   * The WONDER_SEED_AWARDED vtable filter that converts a NerveActivateOnce
//     fire into an outbound AP location check.
//   * The SCENE_TRANSITION death detector + DeathLink enqueue.
//   * The GmdContainerAWriter override that keeps the Wonder Seed gate
//     pinned to the AP-authoritative count.
// Those depend on the ap/ subsystem (still parked under src/program/ap/,
// excluded from the build) and on the GRANTS hooks (Phase 2c).
//
// Hook signatures are intentionally `void(void*)` rather than the typed
// game::nerve::Nerve* the legacy code used — Phase 2b doesn't dereference
// past nerve[0] (a single uintptr_t read), so we sidestep the lib/ headers.

#include <atomic>

#include "hk/hook/Trampoline.h"
#include "hk/ro/RoUtil.h"
#include "hk/ro/RoModule.h"
#include "hk/svc/api.h"
#include "hk/types.h"

#include "util/Log.hpp"

namespace {

// =========================================================================
// CORE_INIT (Phase 2a — unchanged)
// =========================================================================

HkTrampoline<void, void*> createRootHeapHook = hk::hook::trampoline(
    [](void* thisPtr) -> void {
        SMBWAP_LOG_INFO("hook: CreateRootHeap fire (pre)");
        createRootHeapHook.orig(thisPtr);
        SMBWAP_LOG_INFO("hook: CreateRootHeap fire (post)");
    });

HkTrampoline<void, void*> createFileDeviceMgrHook = hk::hook::trampoline(
    [](void* thisPtr) -> void {
        SMBWAP_LOG_INFO("hook: CreateFileDeviceMgr fire (pre)");
        createFileDeviceMgrHook.orig(thisPtr);
        SMBWAP_LOG_INFO("hook: CreateFileDeviceMgr fire (post) — draining to SD");
        smbwap::util::drainPendingToFile();
    });

HkTrampoline<void, void*, const void*> gameFrameworkInitializeHook =
    hk::hook::trampoline(
        [](void* thisPtr, const void* arg) -> void {
            SMBWAP_LOG_INFO("hook: GameFrameworkInitialize fire (pre)");
            gameFrameworkInitializeHook.orig(thisPtr, arg);
            SMBWAP_LOG_INFO("hook: GameFrameworkInitialize fire (post)");
        });

// =========================================================================
// M1_EVENTS (Phase 2b)
// =========================================================================

// NerveActivateOnce @ NSO +0x00559f7c.
//
// Shared inner helper that every one-shot Nerve activation funnels through.
// Hooking it once gives us every Nerve activation event in the engine,
// filterable by vtable. Empirically: many per second in active gameplay.
//
// Per CLAUDE.md "Critical gotchas" #2, this function's prologue is clean
// (no PC-relative loads in the first 16 bytes) so the trampoline relocation
// is safe — verified working under exlaunch's And64InlineHook. Hakkun's
// trampoline uses similar machinery; if this Phase 2b run shows the
// callback firing without crashes, the relocation is safe under hakkun too.
HkTrampoline<void, void*> nerveActivateOnceHook = hk::hook::trampoline(
    [](void* nerve) -> void {
        // Cheap vtable-offset read. nerve[0] is the C++ vtable pointer;
        // subtract the loaded NSO base to get the NSO-relative offset.
        ::ptr vt_off = 0;
        if (nerve) {
            const auto* main_mod = hk::ro::getMainModule();
            if (main_mod) {
                const ::ptr target_base = main_mod->range().start();
                const ::ptr vtable_addr = *reinterpret_cast<::ptr*>(nerve);
                if (vtable_addr >= target_base) {
                    vt_off = vtable_addr - target_base;
                }
            }
        }

        // Bounded survey log: first 30 fires regardless of vtable.
        static int s_fires = 0;
        ++s_fires;
        if (s_fires <= 30) {
            SMBWAP_LOG_INFO("nerve_activate #%d: nerve=%p vt_off=0x%llx",
                            s_fires, nerve,
                            static_cast<unsigned long long>(vt_off));
        }

        // Distinct-vtable dedup: log the FIRST time we see each unique
        // vt_off so we can identify which Nerves flow through this helper
        // without flooding the ring. Same fixed-table pattern as the
        // legacy exlaunch code; 64 slots covers the 20-30 distinct Nerves
        // observed in steady-state play.
        if (vt_off != 0) {
            constexpr ::size kVtSeenSlots = 64;
            static std::atomic<unsigned long long> s_vt_seen[kVtSeenSlots] = {};
            static std::atomic<unsigned> s_vt_seen_count{0};
            bool already_seen = false;
            const unsigned cnt = s_vt_seen_count.load(std::memory_order_acquire);
            for (unsigned i = 0; i < cnt && i < kVtSeenSlots; ++i) {
                if (s_vt_seen[i].load(std::memory_order_relaxed)
                    == static_cast<unsigned long long>(vt_off)) {
                    already_seen = true;
                    break;
                }
            }
            if (!already_seen) {
                const unsigned slot = s_vt_seen_count.fetch_add(
                    1, std::memory_order_acq_rel);
                if (slot < kVtSeenSlots) {
                    s_vt_seen[slot].store(
                        static_cast<unsigned long long>(vt_off),
                        std::memory_order_release);
                    SMBWAP_LOG_INFO(
                        "NERVE_NEW_VT: slot=%u vt_off=NSO+0x%llx fire=%d",
                        slot, static_cast<unsigned long long>(vt_off),
                        s_fires);
                }
            }
        }

        nerveActivateOnceHook.orig(nerve);
    });

// SetCourseClearFlagExecute @ NSO +0x001bf28cc.
//
// Slot 8 of the SetCourseClearFlagToGameData Nerve vtable. Direct trampoline
// on the execute method (not a NerveActivateOnce-routed call). Fires once per
// successful course clear: flagpole touch + level completion → fires; menu
// quit / death → doesn't fire. Palace boss clear also fires (palace is a
// course). Prologue is clean per CLAUDE.md notes.
HkTrampoline<void, void*> setCourseClearFlagExecuteHook = hk::hook::trampoline(
    [](void* nerve) -> void {
        static int s_fires = 0;
        ++s_fires;
        SMBWAP_LOG_INFO("COURSE_CLEARED: nerve=%p (fire #%d)", nerve, s_fires);
        setCourseClearFlagExecuteHook.orig(nerve);
    });

// GameGoalReachedExecute @ NSO +0x0015b77a8.
//
// Slot 8 of the SetFlagEndDispMsgFirstVisitedWorldAfterClearedLastBoss
// Nerve's vtable — fires exactly once per save the first time the player
// defeats final Bowser. Phase 2c will translate this into an AP
// ClientStatus.CLIENT_GOAL StatusUpdate; for Phase 2b we just log.
HkTrampoline<void, void*> gameGoalReachedExecuteHook = hk::hook::trampoline(
    [](void* nerve) -> void {
        static int s_fires = 0;
        ++s_fires;
        SMBWAP_LOG_INFO("GAME_GOAL_REACHED: nerve=%p (fire #%d)", nerve, s_fires);
        gameGoalReachedExecuteHook.orig(nerve);
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
    SMBWAP_LOG_INFO("Phase 2b: 3 CORE_INIT + 3 M1_EVENTS hooks");

    // CORE_INIT (Phase 2a)
    installHook("CreateRootHeap",          0x005a66f8,
                createRootHeapHook.installAtMainOffset(0x005a66f8));
    installHook("CreateFileDeviceMgr",     0x005a6110,
                createFileDeviceMgrHook.installAtMainOffset(0x005a6110));
    installHook("GameFrameworkInitialize", 0x005a5cfc,
                gameFrameworkInitializeHook.installAtMainOffset(0x005a5cfc));

    // M1_EVENTS (Phase 2b)
    installHook("NerveActivateOnce",        0x00559f7c,
                nerveActivateOnceHook.installAtMainOffset(0x00559f7c));
    installHook("SetCourseClearFlagExecute", 0x01bf28cc,
                setCourseClearFlagExecuteHook.installAtMainOffset(0x01bf28cc));
    installHook("GameGoalReachedExecute",    0x015b77a8,
                gameGoalReachedExecuteHook.installAtMainOffset(0x015b77a8));

    SMBWAP_LOG_INFO("=== smbwap hkMain END ===");
}
