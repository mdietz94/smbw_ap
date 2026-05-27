#include "ExceptionHandler.h"
#include "framework/seadGameFramework.h"
#include "game/actor/ActorPlacementInfo.h"
#include "heap/seadHeapMgr.h"
#include "hook/inline.hpp"
#include "hook/replace.hpp"
#include "hook/trampoline.hpp"
#include <atomic>
#include <cstdint>
#include <cstring>
#include "imgui.h"
#include "imgui_nvn.h"
#include "lib.hpp"
#include "nn/fs.h"
#include "nn/prepo.h"
#include "pe/DbgGui/DbgGui.h"
#include "pe/Hacks/FSHacks.h"
#include "pe/Util/Log.h"
#include "util/Log.hpp"
#include "util/modules.hpp"
#include "util/sys/mem_layout.hpp"
#include "util/sys/rw_pages.hpp"
#include <sead/filedevice/seadFileDeviceMgr.h>
#include <sead/framework/seadGameFramework.h>
#include <sead/math/seadVector.h>

// M4 — LAN bridge to PC AP client.
#include "ap/ApClient.hpp"
#include "ap/ApFrameBridge.hpp"

// M3.8 -- forward declarations for the DeathLink loop-guard atomic and
// live_base latch that live in the `probe` namespace below.  The Nerve
// callback (NerveActivateOnce, ~line 305) references
// g_synthetic_death_this_frame from inside the scene-transition branch;
// the definition lives in the namespace probe block ~line 960 alongside
// synthKill.  An extern declaration here keeps the reference valid
// without rearranging the file structure.
namespace probe {
extern std::atomic<bool> g_synthetic_death_this_frame;
extern std::atomic<uintptr_t> g_live_base;
// Scene-transition gate for the GmdContainerAWriter WS override
// interceptor.  Latched (svcGetSystemTick) when the SceneTransition
// Nerve fires (death, course entry/exit, world map, palace, Poplin
// shop -- anything that changes scene).  Read on whatever thread the
// container-A writer runs on (ModuleSystemWorker2 observed) to skip
// our pre-Orig getfn during transitions -- the writer's secondary
// container at gmd+0x128 is not safe to probe concurrently with the
// game's transition-time mutations (observed abort inside
// FUN_710049F750).
extern std::atomic<std::uint64_t> g_last_scene_transition_tick;
// 19.2 MHz tick rate -- 3 s window.
constexpr std::uint64_t kSceneTransitionGateTicks = 3ULL * 19'200'000ULL;
static long gmdSingleton();
}

// svcGetSystemTick is declared in nx/kernel/svc.h; forward-declare here
// so we don't have to pull the whole svc header into main.cpp.  Matches
// the pattern used elsewhere in this codebase (random.cpp gets it via
// transitive include).
extern "C" std::uint64_t svcGetSystemTick(void);

static void initHeap()
{
    SMBWAP_LOG_INFO("smbwap: initHeap: creating PeepaHeap");
    pe::gui::getPeepaHeap() = sead::ExpHeap::create(1024 * 1024 * 16, "PeepaHeap", sead::HeapMgr::getRootHeap(0), 8, sead::ExpHeap::cHeapDirection_Forward, false);
    SMBWAP_LOG_INFO("smbwap: initHeap: PeepaHeap created, initializing pe::log");
    pe::initializeLog(pe::gui::getPeepaHeap());
    SMBWAP_LOG_INFO("smbwap: initHeap: done");
}

static void initDbgGui()
{
    SMBWAP_LOG_INFO("smbwap: initDbgGui: entering");
    sead::ScopedCurrentHeapSetter heapSetter(pe::gui::getPeepaHeap());
    pe::gui::DbgGui::createInstance(nullptr);
    SMBWAP_LOG_INFO("smbwap: initDbgGui: DbgGui created");
}

HOOK_DEFINE_TRAMPOLINE(CreateFileDeviceMgr) { static void Callback(sead::FileDeviceMgr * thisPtr); };
void CreateFileDeviceMgr::Callback(sead::FileDeviceMgr* thisPtr)
{
    SMBWAP_LOG_INFO("smbwap: CreateFileDeviceMgr::Callback: entering");
    Orig(thisPtr);
    SMBWAP_LOG_INFO("smbwap: CreateFileDeviceMgr::Callback: Orig() returned, mounting SD");
    nn::fs::MountSdCardForDebug("sd");
    SMBWAP_LOG_INFO("smbwap: CreateFileDeviceMgr::Callback: SD mounted");
}

HOOK_DEFINE_TRAMPOLINE(GameFrameworkInitialize) { static void Callback(sead::GameFramework * thisPtr, const sead::Framework::InitializeArg& arg); };

// M4: forward decl of nn::socket::Initialize.  No header for it in our tree;
// sail resolves the mangled symbol against main.nso.  Pool is the buffer
// the SDK manages internal bsd:u socket state in; 0x4000 / 0xe are values
// proven by smo_archipelago (TCP buffer size + a "concurrent FDs" config
// param, both inherited from wondar's defaults).
namespace nn::socket {
    std::uint32_t Initialize(void* pool, std::size_t pool_size,
                              std::size_t tcp_alloc, int max_concurrent);
}

void GameFrameworkInitialize::Callback(sead::GameFramework* thisPtr, const sead::Framework::InitializeArg& arg)
{
    SMBWAP_LOG_INFO("smbwap: GameFrameworkInitialize::Callback: entering");

    // M4: bring up nn::socket BEFORE Orig (and thus before the game can call
    // it itself).  768 KiB static pool, page-aligned -- avoids malloc, which
    // wouldn't satisfy nn::socket's alignment requirement.  A separate
    // NnSocketInitNoop trampoline (installed in exl_main) neutralizes a
    // later game-side Initialize call so first-call-wins semantics keep our
    // pool in service.
    {
        constexpr size_t kSocketPoolSize = 0xC0000;  // 768 KiB
        alignas(0x1000) static unsigned char s_socket_pool[kSocketPoolSize];
        const std::uint32_t rc = nn::socket::Initialize(
            s_socket_pool, kSocketPoolSize, 0x4000, 0xe);
        SMBWAP_LOG_INFO("[net] socket init rc=0x%x pool=%zu bytes",
                        rc, kSocketPoolSize);
    }

    Orig(thisPtr, arg);
    SMBWAP_LOG_INFO("smbwap: GameFrameworkInitialize::Callback: Orig() returned");
    initDbgGui();

    // M4: spawn the LAN client worker thread.  start() does nifm bring-up
    // first (must run on an nn-aware thread, which this is -- we're inside
    // a game framework callback), then svcCreateThread/svcStartThread the
    // worker loop.
    smbwap::ap::ApClient::instance().start();

    SMBWAP_LOG_INFO("smbwap: GameFrameworkInitialize::Callback: done");
}

void drawDbgGui()
{
    static int s_callCount = 0;
    if (s_callCount < 3) {
        SMBWAP_LOG_INFO("smbwap: drawDbgGui: call #%d", s_callCount);
    }
    s_callCount++;

    sead::ScopedCurrentHeapSetter heapSetter(pe::gui::getPeepaHeap());

    auto* dbgGui = pe::gui::DbgGui::instance();
    if (dbgGui)
        dbgGui->draw();
}

HOOK_DEFINE_TRAMPOLINE(InitActorPlacementInfo) { static void Callback(game::actor::ActorPlacementInfo * info, void* byamlIter); };

void InitActorPlacementInfo::Callback(game::actor::ActorPlacementInfo* info, void* byamlIter)
{
    Orig(info, byamlIter);
    pe::log("%s", info->mActorArchiveName);

    // M1.2: mirror to the SVC sink. Guard against a null name string
    // (defensive — pe::log handled it before, devkitA64 vsnprintf may not).
    const char* name = info->mActorArchiveName ? info->mActorArchiveName : "(null)";
    SMBWAP_LOG_INFO("actor_placement: %s hash=%016llx",
                    name,
                    static_cast<unsigned long long>(info->mHash));
}

HOOK_DEFINE_TRAMPOLINE(CreateRootHeap) { static void Callback(sead::HeapMgr * thisPtr); };

void CreateRootHeap::Callback(sead::HeapMgr* thisPtr)
{
    SMBWAP_LOG_INFO("smbwap: CreateRootHeap::Callback: entering");
    Orig(thisPtr);
    SMBWAP_LOG_INFO("smbwap: CreateRootHeap::Callback: Orig() returned");
    initHeap();
    SMBWAP_LOG_INFO("smbwap: CreateRootHeap::Callback: done");
}

// M1.3 dispatcher hook — DISABLED.
//
// FUN_7100299488 at NSO +0x299488 turned out to be a per-actor REGISTRATION
// handler, not a touch event. It fires once at level-load for goal-class actors
// to wire them into level-clear plumbing. Useless for detecting actual gameplay
// events. Keeping the code as a reference but not installing.
//
// Real event detection goes through Nintendo's named-event Nerve system —
// RequestEventGetFinishWonderSeed and siblings (see WonderSeedExecute below).
HOOK_DEFINE_TRAMPOLINE(GoalDispatcher) {
    static void Callback(void* param_1, void* param_2);
};
void GoalDispatcher::Callback(void* param_1, void* param_2)
{
    // Read the actor name from the param_2 struct: *(param_2 + 0xd8) gives a
    // pointer to actor info; that struct holds the name at offset +0x5c.
    // Defensive about nulls; if anything looks wrong, fall through to Orig.
    const char* name = nullptr;
    if (param_2) {
        auto* p = reinterpret_cast<uint8_t*>(param_2);
        auto* info = *reinterpret_cast<uint8_t**>(p + 0xd8);
        if (info) {
            name = reinterpret_cast<const char*>(info + 0x5c);
        }
    }

    // Phase 1: wide-survey logging — first 50 fires unconditionally, to find
    // out which actor classes flow through this dispatcher in practice. If
    // ItemWonderFinishWonderSead appears in the survey when we collect a
    // Wonder Seed, we've found our Wonder Seed hook with no extra RE.
    static int s_anyFires = 0;
    s_anyFires++;
    if (s_anyFires <= 50) {
        SMBWAP_LOG_INFO("dispatcher_fire_any #%d: name=%s p1=%p p2=%p",
                        s_anyFires, name ? name : "(null)", param_1, param_2);
    }

    // Phase 2: target-only logging — narrower hits for our M1 events,
    // unbounded count, so we can correlate fire timing with player actions.
    const bool isTarget =
        name && (std::strcmp(name, "ObjectGoalPole") == 0 ||
                 std::strcmp(name, "ObjectGoalPoleFort") == 0 ||
                 std::strcmp(name, "ItemWonderFinishWonderSead") == 0 ||
                 std::strcmp(name, "ObjectBigTenLuckyCoin") == 0);
    if (isTarget) {
        static int s_targetFires = 0;
        s_targetFires++;
        SMBWAP_LOG_INFO("dispatcher_target_fire #%d: name=%s p1=%p p2=%p",
                        s_targetFires, name, param_1, param_2);
    }

    Orig(param_1, param_2);
}

// M1.3 Wonder Seed Nerve execute hook — abandoned target.
//
// FUN_7101562fb4 was vtable slot 8 of RequestEventGetFinishWonderSeed Nerve.
// Even a no-op trampoline at this address crashed level loads ~2s after the
// first fire, suggesting exlaunch's prologue relocation breaks something
// specific to this function (PC-relative instruction in first 16 bytes, or
// a calling-convention quirk for vtable-entry functions). Leaving the
// declaration for reference but not installing.
HOOK_DEFINE_TRAMPOLINE(WonderSeedExecute) {
    static void Callback(void* nerve);
};
void WonderSeedExecute::Callback(void* nerve)
{
    static int s_fires = 0;
    s_fires++;
    if (s_fires == 1 || (s_fires % 60) == 0) {
        SMBWAP_LOG_INFO("wonder_seed_execute fire #%d: nerve=%p", s_fires, nerve);
    }
    Orig(nerve);
}

// M1.3 Goal hook — SetCourseClearFlagToGameData Nerve's execute method.
//
// FUN_7101bf28cc is slot 8 of the SetCourseClearFlagToGameData Nerve
// (vtable at NSO +0x34b14e8). The function name says it all — it writes
// the "course cleared" flag into save data (GameData). Only fires on
// actual successful clear:
//   - flagpole touch + level completion → fires (writes cleared flag)
//   - menu quit → doesn't fire (no clear)
//   - death/respawn → doesn't fire (no clear)
//   - palace boss clear → likely fires (palace is a course)
//
// The body reads as:
//   bl FUN_710059f894             ; open GameData accessor
//   mov w0, #0xdf82e9ab            ; hashed field name
//   bl FUN_71003d3fb0              ; write field by hash
//   bl FUN_71005e93fc; tbz w0,#0,L ; check success and gate
//
// Prologue is clean: 5 simple sp/stp instructions, no PC-relative loads
// in the first 16 bytes, so exlaunch's And64InlineHook can relocate it
// cleanly (unlike Wonder Seed's slot 8 which crashed when hooked).
HOOK_DEFINE_TRAMPOLINE(SetCourseClearFlagExecute) {
    static void Callback(void* nerve);
};
void SetCourseClearFlagExecute::Callback(void* nerve)
{
    // M4: drain any pending inbound grants (SetBadgesAbsolute /
    // GrantHashKeyed) on the game thread before doing the existing
    // course-clear work.  Bridge classifies course clears via the
    // course_result PlayReport (not the Nerve), so we don't enqueue
    // anything outbound here.
    smbwap::ap::drainInbound();

    static int s_fires = 0;
    s_fires++;
    SMBWAP_LOG_INFO("COURSE_CLEARED: nerve=%p (fire #%d)", nerve, s_fires);
    Orig(nerve);
}

// M3.7 Game-completion goal hook -- one-shot Nerve "SetFlagEndDispMsg
// FirstVisitedWorldAfterClearedLastBoss" execute (slot 8) at NSO +0x15b77a8.
//
// Discovery (2026-05-25):
//   - The string "SetFlagEndDispMsgFirstVisitedWorldAfterClearedLastBoss"
//     lives at NSO +0x295d801 (.rodata).
//   - Exactly one adrp+add pair in .text computes that address:
//     NSO +0x15b7790, a 3-instruction getter ending in `ret`.  This is
//     slot 0 of the Nerve's vtable.
//   - The getter pointer (NSO +0x15b7790) is referenced by exactly one
//     R_AARCH64_RELATIVE entry, at .data offset 0x3363330 -- the vtable.
//     Slot 8 of that vtable resolves to NSO +0x15b77a8 (execute).
//   - Slot 8 has zero direct BL callers and isn't in FUN_7100559f7c's
//     xref list -- it is invoked solely via the vtable, i.e. one-shot.
//   - Prologue is clean: `sub sp, sp, #0x60; stp x29, x30, ...; stp x20,
//     x19, ...; add x29, sp, #0x40; ldr x8, [x0, #0x28]`.  No PC-relative
//     loads in the first 5 instructions -- safe for And64InlineHook.
//
// Same shape as SetCourseClearFlagExecute (M1.3): direct trampoline on
// the execute method.  The function fires exactly once per save the first
// time the player defeats final Bowser; the bridge translates this into
// an AP ClientStatus.CLIENT_GOAL StatusUpdate so the multiworld marks
// this slot done.
HOOK_DEFINE_TRAMPOLINE(GameGoalReachedExecute) {
    static void Callback(void* nerve);
};
void GameGoalReachedExecute::Callback(void* nerve)
{
    // M4: drain pending inbound grants on the game thread before doing
    // the existing work -- same idiom as SetCourseClearFlagExecute.
    smbwap::ap::drainInbound();

    static int s_fires = 0;
    s_fires++;
    SMBWAP_LOG_INFO("GAME_GOAL_REACHED: nerve=%p (fire #%d)", nerve, s_fires);
    smbwap::ap::enqueueNerveFire(
        smbwap::ap::NerveKind::GameGoalReached,
        static_cast<std::uint32_t>(s_fires));
    Orig(nerve);
}

// M1.3 shared "Nerve one-shot activate" hook at NSO +0x559f7c.
//
// FUN_7100559f7c is the inner conditional callee inside Nerve execute methods
// (called from FUN_7101562fb4's `if (flag==0)` branch in the Wonder Seed Nerve,
// and from analogous one-shot branches in 13+ other event Nerves). Several
// vtables also list it directly as a slot.
//
// Hooking it once gives us every Nerve activation event in the engine,
// filterable by the calling Nerve's vtable (read from nerve[0] = vtable*).
// For M1 we filter to known event vtables (Wonder Seed today, Goal et al as
// we map them).
//
// Prologue is clean (no PC-relative in first 16 bytes):
//   stp x29, x30, [sp, #-0x20]!
//   str x19, [sp, #0x10]
//   mov x29, sp
//   ldr x8, [x0, #0x40]
// And64InlineHook will relocate these cleanly.
HOOK_DEFINE_TRAMPOLINE(NerveActivateOnce) {
    static void Callback(void* nerve);
};

namespace {
    // Known event Nerve vtable offsets, indexed by what we've seen them do
    // empirically during R-2026-05-20 test runs.
    //
    // NSO base when loaded is 0x7100000000; offsets here = vtable - base.
    constexpr uintptr_t kVtableOff_WonderSeedAwarded = 0x3345728;  // ✓ confirmed
    constexpr uintptr_t kVtableOff_RequestEventGoalBase = 0x3345bc0; // ✗ never fires
    constexpr uintptr_t kVtableOff_RequestEventGoalGateFinish = 0x3345cf8; // ✗ never fires
    constexpr uintptr_t kVtableOff_RequestEventGoalTreasureChest = 0x3345e30; // ✗ never fires
    // The Goal*-named Nerves above never fired during flag-touch testing;
    // those classes appear to be passive registration entries. The actual
    // course-exit fire goes through RequestEventCourseExitByAreaTag — the
    // "area tag" being the trigger zone around the flagpole. Should NOT
    // fire on menu-quit (different path entirely). Testing now.
    constexpr uintptr_t kVtableOff_RequestEventCourseExitByAreaTag = 0x33fd690;

    // Cross-confirmed in cross-level testing: NOT seed-specific.
    // 0x33fd9a8 fires on Mario death and post-seed cleanup — generic scene transition.
    // 0x33fd870 fires on damage AND power-up pickup — generic state-change request.
    // 0x3346330 fires on Wonder Flower touch — Wonder phase start.
    constexpr uintptr_t kVtableOff_SceneTransition   = 0x33fd9a8;
    constexpr uintptr_t kVtableOff_PlayerStateChange = 0x33fd870;
    constexpr uintptr_t kVtableOff_WonderFlowerTouched = 0x3346330;

    // String-search red herring — never fires in practice. The real Wonder Seed
    // Nerve is kVtableOff_WonderSeedAwarded above.
    constexpr uintptr_t kVtableOff_RequestEventGetFinishWonderSeed = 0x33457b8;
}

void NerveActivateOnce::Callback(void* nerve)
{
    // M4: drain any pending inbound grants on the game thread.  This fires
    // on every Nerve activation, so it's a high-frequency drain anywhere
    // the player is actively in a course.  The drain is a single atomic
    // load on an empty ring -- essentially free.
    smbwap::ap::drainInbound();

    // Read the nerve's vtable before the Wonder Seed override so we can skip
    // writes during scene transitions (crash fix: Secred.nss:0x2c8764
    // LockMutex(null) on W2->W1 transition).  The game resets the 5
    // per-world Wonder Seed count hashes on every world transition via this
    // same SceneTransition Nerve; if our override fires during that reset it
    // writes the old world's count back, leaving a stale non-zero value that
    // the game's world-navigation code treats as a struct pointer -> null deref.
    uintptr_t vt_off = 0;
    if (nerve) {
        const uintptr_t target_base = exl::util::modules::GetTargetStart();
        const uintptr_t vtable_addr = *reinterpret_cast<uintptr_t*>(nerve);
        if (vtable_addr >= target_base) {
            vt_off = vtable_addr - target_base;
        }
    }

    // AP-authoritative Wonder Seed gate override now lives in
    // GmdContainerAWriter::Callback (interceptor pattern -- rewrites every
    // write to the 5 mirror hashes to the AP count for the current world).
    // The previous NerveActivateOnce-driven push lost a tug-of-war against
    // the game's natural recompute, which fires every ~2 s and overwrote
    // our values.  See the WS override block in that callback for details.
    // vt_off computed above is still used by the SceneTransition branch
    // and the Nerve-target filter checks below.

    static int s_fires = 0;
    s_fires++;

    // Survey log: first 30 fires regardless of vtable — tells us what events
    // flow through this helper during normal play, so we can map their
    // vtable offsets and add them to filters.
    if (s_fires <= 30) {
        SMBWAP_LOG_INFO("nerve_activate #%d: nerve=%p vt_off=0x%llx",
                        s_fires, nerve, (unsigned long long)vt_off);
    }

    // Distinct-vtable survey (2026-05-26 follow-up to the level-entry abort):
    // log the FIRST time we see each unique vt_off so the operator can
    // identify Nerves we're not currently latching as scene transitions.
    // The kVtableOff_SceneTransition gate at 0x33fd9a8 doesn't appear to
    // fire for level-entry (FUN_710049F750 abort observed during level
    // entry on v0.2.2-alpha) -- if a new vt_off appears in the log right
    // before an abort, that's our missing scene-transition Nerve.  Small
    // fixed table to avoid heap usage; 64 slots is ample for the
    // ~20-30 distinct Nerves observed in steady-state play.
    if (vt_off != 0) {
        constexpr size_t kVtSeenSlots = 64;
        static std::atomic<std::uint64_t> s_vt_seen[kVtSeenSlots] = {};
        static std::atomic<std::uint32_t> s_vt_seen_count{0};
        bool already_seen = false;
        const auto cnt = s_vt_seen_count.load(std::memory_order_acquire);
        for (std::uint32_t i = 0; i < cnt && i < kVtSeenSlots; ++i) {
            if (s_vt_seen[i].load(std::memory_order_relaxed)
                == static_cast<std::uint64_t>(vt_off)) {
                already_seen = true;
                break;
            }
        }
        if (!already_seen) {
            // Atomically claim a slot.  Races between threads are fine --
            // worst case the same vt_off gets logged twice.
            const auto slot = s_vt_seen_count.fetch_add(
                1, std::memory_order_acq_rel);
            if (slot < kVtSeenSlots) {
                s_vt_seen[slot].store(static_cast<std::uint64_t>(vt_off),
                                      std::memory_order_release);
                SMBWAP_LOG_INFO(
                    "NERVE_NEW_VT: slot=%u vt_off=NSO+0x%llx fire=%d",
                    slot, (unsigned long long)vt_off, s_fires);
            }
        }
    }

    // Target match: log unconditionally when the Wonder Seed Nerve activates.
    // This is THE per-pickup signal we've been trying to find — fires exactly
    // once when the player collects the Wonder Seed at the end of the Wonder
    // phase. Empirically validated on run R-2026-05-20: fire at the moment
    // of grab, no false positives during 95s of unrelated gameplay.
    if (vt_off == kVtableOff_WonderSeedAwarded) {
        SMBWAP_LOG_INFO("WONDER_SEED_AWARDED: nerve=%p (fire #%d)", nerve, s_fires);
        // M4: forward to the LAN bridge.  The bridge attributes via
        // current_course (set by the most recent course_in PlayReport).
        smbwap::ap::enqueueNerveFire(
            smbwap::ap::NerveKind::WonderSeedAwarded,
            static_cast<std::uint32_t>(s_fires));
    }
    if (vt_off == kVtableOff_RequestEventGoalBase) {
        SMBWAP_LOG_INFO("GOAL_REACHED (base): nerve=%p (fire #%d)", nerve, s_fires);
    }
    if (vt_off == kVtableOff_RequestEventGoalGateFinish) {
        SMBWAP_LOG_INFO("GOAL_REACHED (gate-finish): nerve=%p (fire #%d)", nerve, s_fires);
    }
    if (vt_off == kVtableOff_RequestEventGoalTreasureChest) {
        SMBWAP_LOG_INFO("GOAL_REACHED (treasure-chest): nerve=%p (fire #%d)", nerve, s_fires);
    }
    if (vt_off == kVtableOff_RequestEventCourseExitByAreaTag) {
        SMBWAP_LOG_INFO("COURSE_EXITED (area-tag): nerve=%p (fire #%d)", nerve, s_fires);
    }

    // M3.8 -- the "scene transition" Nerve at 0x33fd9a8 fires on Mario
    // death, ~50ms after Wonder Seed grab (post-seed cleanup), and on
    // world-map travel.  Detection is a two-step process: (1) instrument
    // this branch with a wide hex dump of the Nerve's internal state for
    // the first N fires, (2) trigger each known scenario (intentional
    // pit-fall, world-map walk, Wonder Seed grab) and diff the dumps to
    // find which offset distinguishes a death from the others, (3) gate
    // the DEATH_DETECTED enqueue on that discriminator.
    //
    // The naive enqueue is OFF by default to keep AP's DeathLink Bounce
    // log clean during the observation pass.  Once the discriminator is
    // pinned down (kDeathDiscriminator_*), flip kEnableDeathDetect_Naive
    // false -> apply the filter -> reset s_scene_fires bound to 0 to
    // drop the dump from production.
    if (vt_off == kVtableOff_SceneTransition) {
        // Latch the tick so GmdContainerAWriter's WS override gates
        // for the next ~3 s.  Covers every transition family we've
        // observed (death, course/area entry+exit, world-map travel,
        // palace, Poplin shop entry).
        probe::g_last_scene_transition_tick.store(
            svcGetSystemTick(), std::memory_order_relaxed);

        static int s_scene_fires = 0;
        s_scene_fires++;
        if (s_scene_fires <= 30 && nerve) {
            const auto* p = reinterpret_cast<const std::uint64_t*>(nerve);
            SMBWAP_LOG_INFO(
                "SCENE_TRANSITION #%d nerve=%p "
                "+0x00=%016llx +0x08=%016llx +0x10=%016llx +0x18=%016llx "
                "+0x20=%016llx +0x28=%016llx +0x30=%016llx +0x38=%016llx "
                "+0x40=%016llx +0x48=%016llx +0x50=%016llx +0x58=%016llx",
                s_scene_fires, nerve,
                (unsigned long long)p[0], (unsigned long long)p[1],
                (unsigned long long)p[2], (unsigned long long)p[3],
                (unsigned long long)p[4], (unsigned long long)p[5],
                (unsigned long long)p[6], (unsigned long long)p[7],
                (unsigned long long)p[8], (unsigned long long)p[9],
                (unsigned long long)p[10], (unsigned long long)p[11]);
        }

        // Discriminator pinned 2026-05-25 (R-2026-05-25 5-event obs run):
        // two distinct nerve instances share vtable 0x33fd9a8 and the
        // game routes them by purpose:
        //   * Player-controlled exit (restart course, exit course) ->
        //     nerve_A (pointer stable across both events in one
        //     session), `*(u32*)(nerve+0x18) == 0x84`, high u32 ==
        //     0x00ff0037.
        //   * Mario death (pit, enemy hit, etc.) ->
        //     nerve_B (stable across all 3 observed deaths in one
        //     session), `*(u32*)(nerve+0x18) == 0x04`, high u32 ==
        //     0x00ff0006.
        //
        // Death is the WHITELIST -- we only enqueue DEATH_DETECTED when
        // the +0x18 low u32 reads 0x04.  Any other value (the 0x84
        // controlled-exit enum; any unobserved variant like world-map
        // travel, palace-clear, pause-quit) is logged + dropped.  This
        // is more conservative than blacklisting 0x84; if a new sibling
        // enum appears, we'd rather drop it than misfire DeathLinks.
        //
        // OPEN VERIFICATION: the diff was only against in-course events
        // (restart / exit / death).  World-map travel and palace-clear
        // were NOT in the observation pass.  If a non-death scenario
        // accidentally fires DEATH_DETECTED during M3.8 smoke testing,
        // the SCENE_TRANSITION dump (still enabled below for the first
        // 30 fires) will reveal the new enum -> refine the filter.
        constexpr std::ptrdiff_t kDeathDiscriminator_Off = 0x18;
        constexpr std::uint32_t  kDeathDiscriminator_Val = 0x00000004u;

        constexpr bool kEnableDeathDetect_Naive = true;
        if (kEnableDeathDetect_Naive && nerve) {
            const auto state_word = *reinterpret_cast<const std::uint32_t*>(
                reinterpret_cast<const std::uint8_t*>(nerve)
                + kDeathDiscriminator_Off);
            if (state_word != kDeathDiscriminator_Val) {
                // Controlled exit / other transition -- not a death.
                // Bounded log so a long session doesn't flood the file
                // with non-death scene-transition lines.
                if (s_scene_fires <= 30) {
                    SMBWAP_LOG_INFO(
                        "SCENE_TRANSITION non-death (state=0x%08x) "
                        "nerve=%p (fire #%d) -- not enqueuing",
                        static_cast<unsigned>(state_word),
                        nerve, s_scene_fires);
                }
            } else if (probe::g_synthetic_death_this_frame.exchange(
                    false, std::memory_order_acq_rel)) {
                // Loop-guard: probe::synthKill just fired from an
                // inbound DeathLink; this scene-transition Nerve is the
                // consequence of OUR synthetic kill -- don't echo it
                // back as an outbound Bounce.  Mirrors SMO's
                // ApState::synthetic_death_this_frame; the flag is
                // consume-once via exchange.
                SMBWAP_LOG_INFO(
                    "DEATH_DETECTED suppressed (synthetic kill) "
                    "nerve=%p (fire #%d)", nerve, s_scene_fires);
            } else {
                SMBWAP_LOG_INFO(
                    "DEATH_DETECTED: nerve=%p (fire #%d) state=0x%08x",
                    nerve, s_scene_fires,
                    static_cast<unsigned>(state_word));
                smbwap::ap::enqueueNerveFire(
                    smbwap::ap::NerveKind::DeathDetected,
                    static_cast<std::uint32_t>(s_scene_fires));
            }
        }
    }

    Orig(nerve);
}

// M2.4 — PlayReport hooks. Symbol-based install on the nn::prepo SDK API.
//
// PlayReports are structured Nintendo SDK telemetry the game emits at level
// transitions (and elsewhere). The koopajr_result report observed post-M1
// carries:
//     stage_info = { stage_key, world_kind, world_no, course_no }
//     battle_result, badge_id_array, koopajr_total_time, play_mode, ...
// `stage_key` is a stable 32-bit course identifier — exactly the per-course
// distinguisher that COURSE_CLEARED on its own can't produce. Hooking this
// surface gives us course identification for Goal clears AND palace clears
// AND end-of-level summaries in one stroke (see milestones.md M2.4).
//
// Strategy: hook the ctor (event name), each Save overload (commit point),
// and the most common Add overloads on PlayReport + the nested Struct.
// Log `this` so the parser correlates ctor → adds → save for one report.
//
// Symbols are taken verbatim from switch-mod/syms/100/sdk.sym; install via
// InstallAtSymbol so they survive any future SDK shuffle without re-hunting
// offsets, the same pattern pe::FSHacks uses for nn::fs::OpenFile.
HOOK_DEFINE_TRAMPOLINE(PlayReportCtor) {
    static void Callback(void* thisPtr, const char* event_id);
};
void PlayReportCtor::Callback(void* thisPtr, const char* event_id)
{
    SMBWAP_LOG_INFO("prepo.ctor this=%p event=%s",
                    thisPtr, event_id ? event_id : "(null)");
    Orig(thisPtr, event_id);
}

// Bisect step 2: SetEventId is the post-construction event-name setter.
// Games using the no-arg `PlayReport()` ctor then call SetEventId("room_name")
// before adding fields — and the first bisect run showed zero ctor fires
// despite known PlayReport activity, so the game appears to take this path.
HOOK_DEFINE_TRAMPOLINE(PlayReportSetEventId) {
    static nn::Result Callback(void* thisPtr, const char* event_id);
};
nn::Result PlayReportSetEventId::Callback(void* thisPtr, const char* event_id)
{
    SMBWAP_LOG_INFO("prepo.set_event this=%p event=%s",
                    thisPtr, event_id ? event_id : "(null)");
    return Orig(thisPtr, event_id);
}

HOOK_DEFINE_TRAMPOLINE(PlayReportSave) {
    static nn::Result Callback(void* thisPtr);
};
nn::Result PlayReportSave::Callback(void* thisPtr)
{
    SMBWAP_LOG_INFO("prepo.save this=%p", thisPtr);
    return Orig(thisPtr);
}

HOOK_DEFINE_TRAMPOLINE(PlayReportSaveUid) {
    static nn::Result Callback(void* thisPtr, const void* uid);
};
nn::Result PlayReportSaveUid::Callback(void* thisPtr, const void* uid)
{
    SMBWAP_LOG_INFO("prepo.save_uid this=%p uid=%p", thisPtr, uid);
    return Orig(thisPtr, uid);
}

HOOK_DEFINE_TRAMPOLINE(PlayReportAddInt) {
    static nn::Result Callback(void* thisPtr, const char* key, long value);
};
nn::Result PlayReportAddInt::Callback(void* thisPtr, const char* key, long value)
{
    SMBWAP_LOG_INFO("prepo.add_int this=%p key=%s val=%lld",
                    thisPtr, key ? key : "(null)",
                    static_cast<long long>(value));
    return Orig(thisPtr, key, value);
}

HOOK_DEFINE_TRAMPOLINE(PlayReportAddStr) {
    static nn::Result Callback(void* thisPtr, const char* key, const char* value);
};
nn::Result PlayReportAddStr::Callback(void* thisPtr, const char* key, const char* value)
{
    SMBWAP_LOG_INFO("prepo.add_str this=%p key=%s val=\"%s\"",
                    thisPtr, key ? key : "(null)",
                    value ? value : "(null)");
    return Orig(thisPtr, key, value);
}

HOOK_DEFINE_TRAMPOLINE(PlayReportAddBool) {
    static nn::Result Callback(void* thisPtr, const char* key, bool value);
};
nn::Result PlayReportAddBool::Callback(void* thisPtr, const char* key, bool value)
{
    SMBWAP_LOG_INFO("prepo.add_bool this=%p key=%s val=%s",
                    thisPtr, key ? key : "(null)",
                    value ? "true" : "false");
    return Orig(thisPtr, key, value);
}

HOOK_DEFINE_TRAMPOLINE(PlayReportAddStruct) {
    static nn::Result Callback(void* thisPtr, const char* key, const void* st);
};
nn::Result PlayReportAddStruct::Callback(void* thisPtr, const char* key, const void* st)
{
    SMBWAP_LOG_INFO("prepo.add_struct this=%p key=%s struct=%p",
                    thisPtr, key ? key : "(null)", st);
    return Orig(thisPtr, key, st);
}

HOOK_DEFINE_TRAMPOLINE(PlayReportAddArray) {
    static nn::Result Callback(void* thisPtr, const char* key, const void* arr);
};
nn::Result PlayReportAddArray::Callback(void* thisPtr, const char* key, const void* arr)
{
    SMBWAP_LOG_INFO("prepo.add_array this=%p key=%s arr=%p",
                    thisPtr, key ? key : "(null)", arr);
    return Orig(thisPtr, key, arr);
}

// M2.4 bisect step 4 — drop below the PlayReport class to the IPC client.
//
// Hooking PlayReport::Save{,Uid} crashed the SDK on a delayed validation
// pass (different thread each time — first ModuleSystemWorker1, then
// gmd::SaveDataMgr). Theory: the SDK audits "PlayReport state ordering"
// across subsystems and our trampoline injects latency or breaks an
// inlined invariant. The IPC client layer below the PlayReport class
// (CmifProxyImpl<IPrepoService>::_nn_sf_sync_SaveReport*) sees the report
// *already serialized*, so it's after the audited state and may be safe.
//
// Signatures (verified from sdk.sym):
//   SaveReport(this, InArray<char>&, InBuffer&, ulong)
//   SaveReportWithUser(this, Uid&, InArray<char>&, InBuffer&, ulong)
// InArray<char> and InBuffer are {ptr, size} pairs (16 bytes each) passed
// by const reference. Uid is a 16-byte {u64[2]} also by const reference.
struct PrepoInArrayChar { const char* ptr; size_t size; };
struct PrepoInBuffer    { const void* ptr; size_t size; };

namespace {
    // Dump the IPC payload as hex, chunked across multiple log lines so a
    // single report can exceed the ~512-char SMBWAP log buffer.
    //
    // Each line dumps up to kChunk bytes (=> 3*kChunk hex chars).  With
    // kChunk=128, each line is ~384 hex chars + an offset prefix, fitting
    // comfortably under the log buffer.  A whole 355-byte report becomes
    // 3 lines.
    //
    // kMax bounds total bytes dumped per call as a safety net — if a
    // report ever blows past this we'll see the truncation marker.
    void smbwap_log_payload_hex(const void* buf, size_t total_size) {
        if (!buf || total_size == 0) return;
        constexpr size_t kChunk = 128;
        constexpr size_t kMax   = 4096;
        const size_t to_dump = total_size < kMax ? total_size : kMax;
        const unsigned char* p = static_cast<const unsigned char*>(buf);
        static constexpr char kHex[] = "0123456789abcdef";
        char line[3 * kChunk + 1];
        for (size_t off = 0; off < to_dump; off += kChunk) {
            const size_t n = (to_dump - off < kChunk) ? to_dump - off : kChunk;
            for (size_t i = 0; i < n; ++i) {
                const unsigned char v = p[off + i];
                line[i * 3 + 0] = kHex[v >> 4];
                line[i * 3 + 1] = kHex[v & 0x0F];
                line[i * 3 + 2] = ' ';
            }
            line[n * 3] = '\0';
            SMBWAP_LOG_INFO("prepo.ipc.bytes(%zu..%zu/%zu): %s",
                            off, off + n, total_size, line);
        }
        if (total_size > kMax) {
            SMBWAP_LOG_INFO("prepo.ipc.bytes: TRUNCATED at %zu/%zu",
                            kMax, total_size);
        }
    }
}

HOOK_DEFINE_TRAMPOLINE(PrepoIpcSaveReport) {
    static nn::Result Callback(void* thisPtr,
                               const PrepoInArrayChar* room,
                               const PrepoInBuffer* payload,
                               unsigned long flags);
};
nn::Result PrepoIpcSaveReport::Callback(void* thisPtr,
                                         const PrepoInArrayChar* room,
                                         const PrepoInBuffer* payload,
                                         unsigned long flags)
{
    const char* room_ptr = (room && room->ptr) ? room->ptr : "(null)";
    const size_t room_len = (room) ? room->size : 0;
    const void* pay_ptr = payload ? payload->ptr : nullptr;
    const size_t pay_size = payload ? payload->size : 0;
    SMBWAP_LOG_INFO("prepo.ipc.save this=%p room=%.*s pay=%p size=%zu flags=0x%lx",
                    thisPtr, (int)room_len, room_ptr, pay_ptr, pay_size, flags);
    smbwap_log_payload_hex(pay_ptr, pay_size);
    // M4: ship a copy of the raw payload to the bridge.  Forwarding
    // happens before Orig so a hypothetical Orig-aborting code path
    // still gets the event recorded.
    {
        char room_buf[smbwap::ap::kRoomCap];
        const size_t take = room_len < sizeof(room_buf) - 1
            ? room_len : sizeof(room_buf) - 1;
        if (room_ptr) {
            for (size_t i = 0; i < take; ++i) room_buf[i] = room_ptr[i];
        }
        room_buf[take] = '\0';
        smbwap::ap::enqueuePlayReport(room_buf, pay_ptr, pay_size);
    }
    return Orig(thisPtr, room, payload, flags);
}

HOOK_DEFINE_TRAMPOLINE(PrepoIpcSaveReportWithUser) {
    static nn::Result Callback(void* thisPtr,
                               const void* uid,
                               const PrepoInArrayChar* room,
                               const PrepoInBuffer* payload,
                               unsigned long flags);
};
nn::Result PrepoIpcSaveReportWithUser::Callback(void* thisPtr,
                                                 const void* uid,
                                                 const PrepoInArrayChar* room,
                                                 const PrepoInBuffer* payload,
                                                 unsigned long flags)
{
    const char* room_ptr = (room && room->ptr) ? room->ptr : "(null)";
    const size_t room_len = (room) ? room->size : 0;
    const void* pay_ptr = payload ? payload->ptr : nullptr;
    const size_t pay_size = payload ? payload->size : 0;
    SMBWAP_LOG_INFO("prepo.ipc.save_uid this=%p uid=%p room=%.*s pay=%p size=%zu flags=0x%lx",
                    thisPtr, uid, (int)room_len, room_ptr, pay_ptr, pay_size, flags);
    smbwap_log_payload_hex(pay_ptr, pay_size);
    // M4: same as PrepoIpcSaveReport above -- bridge handles either variant
    // identically (the user/uid is irrelevant to AP routing).
    {
        char room_buf[smbwap::ap::kRoomCap];
        const size_t take = room_len < sizeof(room_buf) - 1
            ? room_len : sizeof(room_buf) - 1;
        if (room_ptr) {
            for (size_t i = 0; i < take; ++i) room_buf[i] = room_ptr[i];
        }
        room_buf[take] = '\0';
        smbwap::ap::enqueuePlayReport(room_buf, pay_ptr, pay_size);
    }
    return Orig(thisPtr, uid, room, payload, flags);
}

// Struct::Add — nested struct field setters. `stage_info` is a Struct, and
// the {stage_key, world_kind, world_no, course_no} fields go through these.
HOOK_DEFINE_TRAMPOLINE(StructAddInt) {
    static nn::Result Callback(void* thisPtr, const char* key, long value);
};
nn::Result StructAddInt::Callback(void* thisPtr, const char* key, long value)
{
    SMBWAP_LOG_INFO("prepo.struct.add_int this=%p key=%s val=%lld",
                    thisPtr, key ? key : "(null)",
                    static_cast<long long>(value));
    return Orig(thisPtr, key, value);
}

HOOK_DEFINE_TRAMPOLINE(StructAddBool) {
    static nn::Result Callback(void* thisPtr, const char* key, bool value);
};
nn::Result StructAddBool::Callback(void* thisPtr, const char* key, bool value)
{
    SMBWAP_LOG_INFO("prepo.struct.add_bool this=%p key=%s val=%s",
                    thisPtr, key ? key : "(null)",
                    value ? "true" : "false");
    return Orig(thisPtr, key, value);
}

// ============================================================================
// M3.2 badge-grant probe hooks (added 2026-05-24, sprint run #13).
//
// Goal: identify the container-C bitfield WRITER for the badge ownership u64.
// Static analysis (12 Ghidra rounds) located the storage container and the
// per-bit reader, but the writer is hidden in a cluster of ~20 unidentified
// 0x7101f2**** typed-virtual sibling functions.  Rather than brute-force
// decompile, we instrument the known writers and the reader, then unlock a
// badge in Ryujinx and watch which hook fires.
//
// Expected outcomes (see docs/static-analysis-findings.md run #13):
//   * GmdContainerAWriter fires   -> badges are container-A counter (1 hash)
//   * GmdBoolWriter fires         -> badges are gmd+8 bool, one hash per badge
//   * GmdContainerCBitReader      -> UI queries; reveals badge hash via post-
//                                     unlock query args
//   * Nothing fires               -> writer is yet another function (iterate)
//
// PROLOGUE SAFETY NOTE: per CLAUDE.md gotcha #2, And64InlineHook patches the
// first 5 instructions.  Before merging this build, verify each target's
// prologue in Ghidra has NO `adrp` / `ldr` (literal) / `b` / `bl` in insns
// 0..4.  Container-C reader at +0x124134 has a clean `cbz w2, ...` prologue
// (param_3 null check) so should be safe.  Writers at +0x49F648 and +0x1F263FC
// touch atomic ring buffers — if prologue uses `adrp` for the gmd singleton,
// hooking will corrupt the trampoline.  TEST OUTCOME: if game crashes within
// seconds of any hook firing, suspect prologue corruption and roll that hook
// back individually.
// ============================================================================

// ----------------------------------------------------------------------------
// Container-C diff dumper.  Walks gmd's container C (gmd+0x70..0x8c), reads
// the first 4 uint32s of every typed-sub-object's data buffer, and compares
// against the prior snapshot for that hash.  Logs ONLY on change.
//
// Goal: when a badge is purchased, the badge bitfield in container C will
// flip one bit.  This dumper logs exactly the hash + before/after for the
// changed bitfield, giving us: (a) the badge hash, (b) the bit→internal_id
// mapping, (c) the LIVE address of the bitfield (since we read it directly).
// ----------------------------------------------------------------------------
namespace probe {

// M4.5 save-loaded gate.  Flips true the FIRST time the game writes to
// container A (FUN_710049F648) or the container-B bool delegate
// (FUN_71001F263FC) -- both routes are exclusively taken by the save
// deserializer at this stage of boot, because our own grants don't fire
// until drainInbound is ungated (chicken-and-egg makes the first
// observed write provably game-initiated).  Once set, stays set for the
// process lifetime (no need to clear -- if the player swaps saves the
// new container-A writes still target the new save's gmd).
//
// drainInbound (ap/ApFrameBridge.cpp) reads this on every NerveActivateOnce
// fire and skips popping the inbound ring until the flag is true.  The
// SPSC ring (256 entries) holds the entire HelloMsg replay backlog while
// we're sitting on the title / save-select screen.
static std::atomic_bool s_save_loaded{false};

// Shared scene-transition gate -- exposed via ApFrameBridge.hpp so
// drainInbound can defer all container-touching writes during the
// 3 s window.  Same check the GmdContainerAWriter interceptor uses
// in-line; centralized here so both readers stay consistent.
bool isInSceneTransitionWindow()
{
    const auto last_trans = g_last_scene_transition_tick.load(
        std::memory_order_relaxed);
    if (last_trans == 0) return false;
    const auto now = svcGetSystemTick();
    return (now - last_trans) < kSceneTransitionGateTicks;
}

bool isSaveLoaded()
{
    return s_save_loaded.load(std::memory_order_acquire);
}

static void markSaveLoaded(const char* via)
{
    bool prev = s_save_loaded.exchange(true, std::memory_order_acq_rel);
    if (!prev) {
        SMBWAP_LOG_INFO(
            "save data loaded (via %s); AP grants will now drain", via);
    }
}

// ----------------------------------------------------------------------------
// 2026-05-26 obs-run-3: per-world record table dumper.
//
// Iteration 7 of the Wonder Seed gate investigation identified 3 runtime-
// populated tables in .bss at NSO +0x29f0b34, +0x29f0f94, +0x29f13f4 that
// hold per-world records with a hash at offset +0x68 of each record.  Those
// hashes are the per-world subtotal keys we need — one hash per AP bucket
// (W1..W6 + Petal Isles + Special) keying a container-A counter.  Statically
// the tables are zeroes; only runtime can reveal the populated contents.
//
// This dumper runs ONCE per process (atomic_flag gated), reads 256 bytes
// from each table base, and logs the raw u32-quartets.  Triggered from the
// first SeedBitfieldRead fire (a known one-shot at save-load).  Safe — pure
// reads, no state mutation.
// ----------------------------------------------------------------------------
static std::atomic_flag s_per_world_dumped = ATOMIC_FLAG_INIT;

void dumpPerWorldTables()
{
    if (s_per_world_dumped.test_and_set(std::memory_order_acquire)) return;

    const uintptr_t base = exl::util::modules::GetTargetStart();
    constexpr uint32_t kOffsets[3] = {0x29f0b34, 0x29f0f94, 0x29f13f4};
    constexpr size_t kBytesPerTable = 0x100;          // 256 bytes = 16 rows
    static_assert(kBytesPerTable % 16 == 0, "");

    for (int t = 0; t < 3; ++t) {
        const uint32_t off = kOffsets[t];
        const uintptr_t addr = base + off;
        SMBWAP_LOG_INFO(
            "pwr table=%d base=NSO+0x%x abs=%p", t, off,
            reinterpret_cast<void*>(addr));
        for (size_t row = 0; row < kBytesPerTable; row += 16) {
            const uint32_t* p =
                reinterpret_cast<const uint32_t*>(addr + row);
            SMBWAP_LOG_INFO(
                "pwr table=%d off=+0x%03zx: %08x %08x %08x %08x",
                t, row, p[0], p[1], p[2], p[3]);
        }
    }
}

// ----------------------------------------------------------------------------
// Per-(hash, caller_pc)-pair sample budget for ContainerAReader (iteration #4).
//
// FUN_710012ae94 has 66+ xrefs and fires constantly.  Per-hash budget in
// iteration #3 (run 09-34-58) ran out before the gate-decision reads at
// 5:11+, because save-load + course-clear bursts consumed all 5 slots for
// the per-current-world hashes.  Solution: budget per UNIQUE
// (hash, caller_pc) PAIR so each distinct call site gets its own 5-fire
// quota.  Each new gate-check caller PC then surfaces with at least 5
// captures.
//
// Also blocklists `caller_ret == 0x89c8d4` (call site at NSO+0x89c8d0):
// the dominant "batch reader" identified in run 09-34-58 hitting nearly
// every hash on every UI tick.  It's almost certainly telemetry/UI bulk
// readout, NOT the gate.  Skipping it frees ~95% of the log budget for
// signal callers.
//
// 256 slots × 12 B = 3 KB BSS.  ~60 unique pairs were observed in the
// last run; 256 leaves comfortable headroom for a gameplay session that
// adds new gate-decision pairs.
//
// Race-tolerant: relaxed atomics; occasional duplicate or missed log on
// concurrent access is acceptable.  Hash 0 is sentinel (never logged).
// Slot key = (hash << 32) | (caller_pc & 0xFFFFFFFF) — fits in u64.
// ----------------------------------------------------------------------------
static constexpr size_t kReaderBudgetSlots = 256;
static constexpr uint32_t kReaderBudgetPerPair = 5;
static constexpr uintptr_t kBatchReaderCallSite = 0x89c8d0;  // observed in run 09-34-58

static std::atomic<uint64_t> s_reader_keys[kReaderBudgetSlots]{};
static std::atomic<uint32_t> s_reader_counts[kReaderBudgetSlots]{};

bool readerBudgetTake(uint32_t hash, uintptr_t caller_pc_nso)
{
    if (hash == 0) return false;
    if (caller_pc_nso == kBatchReaderCallSite + 4) return false;  // ret = call+4
    if (caller_pc_nso == kBatchReaderCallSite) return false;
    const uint64_t key = (static_cast<uint64_t>(hash) << 32)
                       | static_cast<uint32_t>(caller_pc_nso);
    for (size_t i = 0; i < kReaderBudgetSlots; ++i) {
        uint64_t cur = s_reader_keys[i].load(std::memory_order_acquire);
        if (cur == key) {
            return s_reader_counts[i].fetch_add(
                1, std::memory_order_relaxed) < kReaderBudgetPerPair;
        }
        if (cur == 0) {
            uint64_t expected = 0;
            if (s_reader_keys[i].compare_exchange_strong(
                    expected, key,
                    std::memory_order_acq_rel,
                    std::memory_order_acquire)) {
                s_reader_counts[i].fetch_add(1, std::memory_order_relaxed);
                return true;
            }
            if (s_reader_keys[i].load(std::memory_order_acquire) == key) {
                return s_reader_counts[i].fetch_add(
                    1, std::memory_order_relaxed) < kReaderBudgetPerPair;
            }
        }
    }
    return false;  // table full
}

struct BitfieldSnapshot {
    uint32_t hash;
    uint32_t data[4];   // 128 bits — enough for any expected bitfield width
    uint32_t valid;
};

// Cap was 64 — too small.  Save-diff (2026-05-24) confirmed the badge
// bitfield serializes to file offset 0x0EA0, immediately after
// 0x2309a645's slot at 0x0E88, so it IS in container C — but our 64-
// slot cap was filling up before we reached its bucket index.  256 is
// well above container C's true entry count (~75-100) with margin.
static constexpr size_t kSnapshotCap = 256;
static BitfieldSnapshot g_snapshots[kSnapshotCap]{};
static std::atomic_flag g_dumpLock = ATOMIC_FLAG_INIT;

static long gmdSingleton()
{
    const uintptr_t base = exl::util::modules::GetTargetStart();
    return *reinterpret_cast<long*>(base + 0x0363F0F0);
}

static void dumpContainerCDiff(const char* trigger_tag,
                               uint32_t trigger_hash,
                               uint32_t trigger_value)
{
    if (g_dumpLock.test_and_set(std::memory_order_acquire)) return;

    long gmd = gmdSingleton();
    if (gmd == 0) {
        g_dumpLock.clear(std::memory_order_release);
        return;
    }

    long bucket = *reinterpret_cast<long*>(gmd + 0x80);
    uint32_t bucket_count = *reinterpret_cast<uint32_t*>(gmd + 0x8c);
    uint32_t limit = *reinterpret_cast<uint32_t*>(gmd + 0x70);
    long obj_array = *reinterpret_cast<long*>(gmd + 0x78);

    if (bucket == 0 || bucket_count == 0 || bucket_count > 1024 ||
        obj_array == 0 || limit > 1024) {
        g_dumpLock.clear(std::memory_order_release);
        return;
    }

    for (uint32_t i = 0; i < bucket_count; ++i) {
        long entry = bucket + static_cast<long>(i) * 8;
        uint32_t key = *reinterpret_cast<uint32_t*>(entry);
        uint32_t idx = *reinterpret_cast<uint32_t*>(entry + 4);
        if (key == 0) continue;
        if (idx >= limit) continue;

        long obj = obj_array + static_cast<long>(idx) * 0x40;
        long data_ptr = *reinterpret_cast<long*>(obj + 0x28);
        if (data_ptr == 0) continue;

        uint32_t cur[4];
        std::memcpy(cur, reinterpret_cast<void*>(data_ptr), sizeof(cur));

        BitfieldSnapshot* snap = nullptr;
        for (auto& s : g_snapshots) {
            if (s.valid && s.hash == key) { snap = &s; break; }
        }
        bool first_seen = false;
        if (!snap) {
            for (auto& s : g_snapshots) {
                if (!s.valid) {
                    snap = &s;
                    snap->hash = key;
                    snap->valid = 1;
                    std::memset(snap->data, 0, sizeof(snap->data));
                    first_seen = true;
                    break;
                }
            }
        }
        if (!snap) continue;

        if (first_seen || std::memcmp(snap->data, cur, sizeof(cur)) != 0) {
            SMBWAP_LOG_INFO(
                "dump_C %s trig=0x%08x/%u CHANGE: hash=0x%08x addr=%p "
                "was=%08x %08x %08x %08x  now=%08x %08x %08x %08x",
                trigger_tag, trigger_hash, trigger_value,
                key, reinterpret_cast<void*>(data_ptr),
                snap->data[0], snap->data[1], snap->data[2], snap->data[3],
                cur[0], cur[1], cur[2], cur[3]);
            std::memcpy(snap->data, cur, sizeof(cur));
        }
    }

    g_dumpLock.clear(std::memory_order_release);
}

// ----------------------------------------------------------------------------
// M3.2 GRANT PRIMITIVE — confirmed 2026-05-24.
//
// Badge ownership bitfield lives in container C under hash 0x6d1b5c25.
// Walking gmd's container-C bucket array (gmd+0x80) for this hash gives
// the typed-sub-object index (limit at gmd+0x70), and dereferencing the
// data pointer at sub-obj+0x28 of the typed-obj array at gmd+0x78
// yields the live uint32_t[] storage.
//
// Encoding: u32[0] = badges 0-31, u32[1] = badges 32-63 (this u64
// serializes to file offset 0x0EA0).  The bitfield is mirrored at
// u32[2]+u32[3] (= bit_idx 64-127); we write both halves to be safe.
// ----------------------------------------------------------------------------

// Corrected 2026-05-24 after save-diff revealed our earlier hash 0x6d1b5c25
// wrote to file offset 0x1204 (an auxiliary "displayable in slot" bitmap),
// not the canonical owned bitfield at file offset 0x0EA0.  The real owned
// hash is 0x105df820 at live addr 0x20d3da8c70 — value matches exactly
// (bits 9, 34, 35, 46 = the user's 4 actually-owned badges).
static constexpr uint32_t kBadgeOwnedHash = 0x105df820;
// The "displayable/equippable in slot" bitmap — queried by the badge
// inventory UI but doesn't represent ownership.  Kept for reference; we
// may also need to flip bits here to make a newly-granted badge appear
// in the equip UI (separate UI-polish concern).
static constexpr uint32_t kBadgeUiSlotHash = 0x6d1b5c25;

// Walk gmd's container-C bucket for the given hash; return the data
// pointer for that hash's typed-sub-object, or nullptr if not found.
static uint32_t* findContainerCData(uint32_t hash)
{
    long gmd = gmdSingleton();
    if (gmd == 0) return nullptr;

    long bucket = *reinterpret_cast<long*>(gmd + 0x80);
    uint32_t bucket_count = *reinterpret_cast<uint32_t*>(gmd + 0x8c);
    uint32_t limit = *reinterpret_cast<uint32_t*>(gmd + 0x70);
    long obj_array = *reinterpret_cast<long*>(gmd + 0x78);
    if (bucket == 0 || bucket_count == 0 || obj_array == 0) return nullptr;

    uint32_t initial = hash % bucket_count;
    uint32_t cur = initial;
    do {
        uint32_t key = *reinterpret_cast<uint32_t*>(bucket + cur * 8);
        if (key == hash) {
            uint32_t idx = *reinterpret_cast<uint32_t*>(bucket + cur * 8 + 4);
            if (idx >= limit) idx = 0;
            return *reinterpret_cast<uint32_t**>(
                obj_array + static_cast<long>(idx) * 0x40 + 0x28);
        }
        if (key == 0) return nullptr;
        cur = (cur + 1) % bucket_count;
    } while (cur != initial);
    return nullptr;
}

// Overwrite the container-C owned-badge bitfield to the absolute value
// `bits`.  AP is the sole authority over the badge pool: this is called
// on every ReceivedItems update, on every Switch HelloMsg (replay-on-
// reconnect), and on a periodic ~2 s tick by the bridge.  Any in-game
// pickup (Poplin shop, badge house, badge medley) that AP didn't grant
// gets reverted within seconds.
//
// Layout: container-C data ptr returns a uint32_t* to a uint32_t[4]
// (128 bits live; only u32[0..1] are the real owned bits, u32[2..3]
// mirror them per the M3.2 discovery).  We write all four.
//
// External linkage so ap/ApFrameBridge.cpp can resolve
// `probe::setBadgeBitfieldAbsolute` at link time -- the inbound drain
// path on the game thread calls this.
bool setBadgeBitfieldAbsolute(uint64_t bits)
{
    uint32_t* data = findContainerCData(kBadgeOwnedHash);
    if (data == nullptr) return false;

    const uint32_t lo = static_cast<uint32_t>(bits & 0xFFFFFFFFu);
    const uint32_t hi = static_cast<uint32_t>((bits >> 32) & 0xFFFFFFFFu);
    const uint32_t before_lo = data[0];
    const uint32_t before_hi = data[1];

    // M2.3 -- diff-on-overwrite: any bit set in the live bitfield but
    // NOT in the AP-authoritative mask we're about to write is an
    // in-game pickup (Poplin shop / badge house / badge medley / badge
    // challenge).  Emit one BadgeAcquired per stripped bit so the
    // bridge can fire a "<Badge> Obtained" LocationCheck before the
    // overwrite below strips the bit.  The player will see the badge
    // re-appear on the next overwrite cycle after AP returns the
    // corresponding item in `ReceivedItems`.
    const uint64_t actual = static_cast<uint64_t>(before_lo)
                          | (static_cast<uint64_t>(before_hi) << 32);
    const uint64_t acquired = actual & ~bits;
    {
        uint64_t to_emit = acquired;
        while (to_emit != 0) {
            const uint32_t bit = static_cast<uint32_t>(__builtin_ctzll(to_emit));
            to_emit &= to_emit - 1;
            smbwap::ap::enqueueBadgeAcquired(bit);
        }
    }

    data[0] = lo;
    data[1] = hi;
    data[2] = lo;  // live mirror -- M3.2 sprint discovered the bitfield is
                   // u32[4] with u32[2..3] mirroring u32[0..1]; both must
                   // be written for in-game inventory UI consistency.
    data[3] = hi;

    static std::atomic<uint32_t> log_budget{16};
    if (log_budget.fetch_sub(1) > 0) {
        SMBWAP_LOG_INFO(
            "SetBadgesAbsolute: addr=%p bits=0x%016llx before=0x%08x%08x "
            "after=0x%08x%08x acquired=0x%016llx",
            reinterpret_cast<void*>(data),
            static_cast<unsigned long long>(bits),
            before_hi, before_lo, hi, lo,
            static_cast<unsigned long long>(acquired));
    }
    return true;
}

// ----------------------------------------------------------------------------
// M3.3 GENERIC container-C bit setter.
//
// ⚠️ WARNING — DO NOT use this primitive for AP-AUTHORITATIVE Wonder Seed
// reversion (M3.3 Phase B).  Empirically falsified 2026-05-25 22:41 via
// /grant_container_c_bit 0xb9bd745d 0 1 + save-diff: setting bit 0 of
// 0xb9bd745d causes the game to BUMP the lifetime counter 0x8c20ccb7
// (live), MOVE the bit to acknowledgement bitfield 0x1faf41e5, then CLEAR
// 0x1faf41e5 at save time -- producing ZERO change to save bytes at
// file offset 0x3AF8 (the persistent per-course Wonder Phase seed
// array).  The Hoppos save bit at file offset 0x3B08 that we initially
// attributed to 0xb9bd745d was actually written by the in-game Wonder
// Phase pickup nerve at 00:02:18 via a DIFFERENT (still-unknown) writer
// -- our /grant after the fact reused the queue WITHOUT writing the
// persistent flag.
//
// What 0xb9bd745d actually is: an AP-grant IN-FLIGHT QUEUE.  Setting a
// bit queues one wonder-seed credit; the game consumes it on the next
// course load that needs a seed (writes to the persistent storage we
// haven't yet identified, if the entered course doesn't already have
// that seed).  This makes setContainerCBit useful for PHASE A
// (queue-grant injection, where the player still gets their own pickups
// and AP adds extras) but UNHELPFUL FOR PHASE B (AP-authoritative
// reversion, where in-game pickups must be undone within ~2 s).
//
// For Phase B: the persistent per-course Wonder Seed storage container
// is currently UNKNOWN.  Suspected NOT in container C at all -- the
// dump_C trampoline never caught a write to a persistent-looking entry
// during a Wonder Phase pickup.  Likely candidates: a brand new
// container, or container A/B with hash-keyed entries we haven't
// enumerated, or direct gmd struct offsets accessed via cheat-engine-
// style pointer chains.  Find via scripts/ghidra/find_offset_constant_xrefs.py
// targeting 0x3AF8 / 0x3B08 / 0x3348 (see docs/m3.3-phase-b-prompt.md).
//
// Generalization of setBadgeBitfieldAbsolute's direct-memory-write pattern
// for any container-C bitfield identified by `hash`.  Single-bit toggle
// (not absolute overwrite); caller batches by repeated calls.
//
// Returns false if gmd singleton not initialized, the hash isn't found in
// container C (so the bitfield doesn't exist for this save profile), or
// bit_index would write past the bitfield's typed length (we don't query
// vtable slot 0x20 to get the exact bit_count; we conservatively assume
// the data array is at least 4 u32s = 128 bits, matching what the
// dump_C snapshot reads).
// ----------------------------------------------------------------------------
bool setContainerCBit(uint32_t hash, uint32_t bit_index, bool value)
{
    if (bit_index >= 128) return false;  // conservative guard

    uint32_t* data = findContainerCData(hash);
    if (data == nullptr) return false;

    const uint32_t word_idx = bit_index >> 5;
    const uint32_t bit_in_word = bit_index & 0x1Fu;
    const uint32_t mask = 1u << bit_in_word;

    const uint32_t before = data[word_idx];
    const uint32_t after = value ? (before | mask) : (before & ~mask);
    data[word_idx] = after;

    static std::atomic<uint32_t> log_budget{32};
    if (log_budget.fetch_sub(1) > 0) {
        SMBWAP_LOG_INFO(
            "SetContainerCBit: hash=0x%08x bit=%u value=%d addr=%p "
            "word_idx=%u  before=0x%08x  after=0x%08x",
            hash, bit_index, value ? 1 : 0,
            reinterpret_cast<void*>(data), word_idx, before, after);
    }
    return true;
}

// ----------------------------------------------------------------------------
// M3.3 Phase B verification probe — dumpSaveField.
//
// Reads a singleton pointer at NSO+`base_nso_offset`, walks to byte offset
// `field_offset` within the dereferenced struct, and dumps the assumed
// SaveDataField layout there:
//
//   +0x00  uint64_t vtable_ptr
//   +0x08  uint64_t type_desc
//   +0x10  uint32_t count_or_state
//   +0x18  uint32_t* data_ptr       // points to heap u32 array
//   +0x20  uint64_t
//   +0x28  uint64_t
//
// If data_ptr looks like a valid heap address, also dumps the first 81
// u32s pointed to (= 81 SMBW courses).
//
// First user (2026-05-26):
//   - NSO+0x3632e88 (the sead-managed save-data sub-singleton initialized
//     by FUN_7100424504 -> FUN_710042e9dc, allocated 0x3428 bytes) covers
//     ONLY early save fields up to ~0x3428.  Per-course Wonder Phase
//     seeds at file offset 0x3AF8 are NOT in this struct.
//   - The deserializer FUN_71003e8e30 takes a LARGER param_1 (extends to
//     at least 0x3E10) and accesses param_1 + 0x3af8 directly (line 397
//     of the Ghidra decompile: `plVar60 = param_1 + 0x75f;`).  param_1
//     has a vtable and is sead-managed -- almost certainly the
//     gmd::GameDataMgr singleton at NSO+0x363F0F0.
//
// Used to verify the SaveDataField layout hypothesis before shipping
// the AP-authoritative absolute-overwrite primitive
// (probe::setWonderPhaseSeed).  If the dump matches expected save bytes
// (slots 0, 1, 2 = 1; slot 3 = 0; slot 4 = 1 for the Hoppos pickup),
// the primitive is buildable as a direct memory write to
// data_ptr[course_slot].
// ----------------------------------------------------------------------------
// Captured save struct pointer.  Set by SaveDeserializerHook trampoline
// each time FUN_71003e8e30 (the save-field-registry setup) is called --
// param_1 to that function IS the live save struct (heap-alloc'd 0x3E40
// bytes inside FUN_71003e33b4).  Latest value wins on multi-call
// scenarios (e.g., the game allocates per save slot).
//
// Reading from this captured pointer is safe IF the pointer is non-null
// AND the game hasn't freed/reallocated the struct since.  For our
// verification dumps, the game shouldn't free this until save data is
// reset.
std::atomic<uintptr_t> g_captured_save_struct{0};

bool dumpSaveField(uint32_t base_nso_offset, uint32_t field_offset)
{
    const uintptr_t base = exl::util::modules::GetTargetStart();
    long sd = 0;

    if (base_nso_offset == 0) {
        // Sentinel: use the captured save struct pointer (set by
        // SaveDeserializerHook).  This is the empirically-correct way to
        // find the live save struct -- it's heap-alloc'd inside
        // FUN_71003e33b4 and stored in an internal manager array.
        // Static singleton chase failed (gmd+0x3af8 was unrelated game
        // state and crashed the game when read).
        sd = static_cast<long>(g_captured_save_struct.load(
            std::memory_order_acquire));
        if (sd == 0) {
            SMBWAP_LOG_INFO(
                "dumpSaveField(base=0 [captured], off=0x%x): no save "
                "struct captured yet (FUN_71003e8e30 hasn't fired; save "
                "not loaded?)",
                field_offset);
            return false;
        }
        SMBWAP_LOG_INFO(
            "dumpSaveField(base=0 [captured], off=0x%x): using captured "
            "save struct ptr 0x%llx",
            field_offset, static_cast<unsigned long long>(sd));
    } else {
        const uintptr_t singleton_ptr_addr = base + base_nso_offset;
        sd = *reinterpret_cast<long*>(singleton_ptr_addr);

        if (sd == 0) {
            SMBWAP_LOG_INFO(
                "dumpSaveField(base=NSO+0x%x, off=0x%x): singleton ptr "
                "is null (save not loaded? wrong base?)",
                base_nso_offset, field_offset);
            return false;
        }
    }

    const uintptr_t field_addr = static_cast<uintptr_t>(sd) + field_offset;
    const uint64_t vtable      = *reinterpret_cast<uint64_t*>(field_addr + 0x00);
    const uint64_t type_desc   = *reinterpret_cast<uint64_t*>(field_addr + 0x08);
    const uint64_t count_state = *reinterpret_cast<uint64_t*>(field_addr + 0x10);
    const uint64_t data_ptr    = *reinterpret_cast<uint64_t*>(field_addr + 0x18);
    const uint64_t f20         = *reinterpret_cast<uint64_t*>(field_addr + 0x20);
    const uint64_t f28         = *reinterpret_cast<uint64_t*>(field_addr + 0x28);

    SMBWAP_LOG_INFO(
        "dumpSaveField(base=NSO+0x%x, off=0x%x): sd=%p field=%p "
        "vtable=0x%016llx type=0x%016llx",
        base_nso_offset, field_offset,
        reinterpret_cast<void*>(sd),
        reinterpret_cast<void*>(field_addr),
        static_cast<unsigned long long>(vtable),
        static_cast<unsigned long long>(type_desc));
    SMBWAP_LOG_INFO(
        "dumpSaveField(off=0x%x): count/state=0x%016llx data_ptr=0x%016llx "
        "f+0x20=0x%016llx f+0x28=0x%016llx",
        field_offset,
        static_cast<unsigned long long>(count_state),
        static_cast<unsigned long long>(data_ptr),
        static_cast<unsigned long long>(f20),
        static_cast<unsigned long long>(f28));

    // 2026-05-26 update: dump inline bytes of the field FIRST (the 48-
    // byte header turned out NOT to be a vtable+u32-array layout -- it's
    // a manager with sub-pointers).  The per-course u32 array might live
    // INLINE in the field struct at some offset past the 48-byte header.
    // Dump 256 bytes from field+0 so we can spot the pattern (e.g.,
    // `01 01 01 00 01 00 ...` matching the user's known save state).
    {
        constexpr size_t kInlineDumpBytes = 256;
        SMBWAP_LOG_INFO(
            "dumpSaveField(off=0x%x): inline dump of %zu bytes at field=0x%llx",
            field_offset, kInlineDumpBytes,
            static_cast<unsigned long long>(field_addr));
        char buf[256];
        for (size_t row = 0; row < kInlineDumpBytes; row += 32) {
            int pos = snprintf(buf, sizeof(buf), "  inline[+0x%03zx]:", row);
            for (size_t i = 0; i < 8 && (row + i * 4) < kInlineDumpBytes &&
                              pos < static_cast<int>(sizeof(buf)) - 12; ++i) {
                const uint32_t v = *reinterpret_cast<uint32_t*>(
                    field_addr + row + i * 4);
                pos += snprintf(buf + pos, sizeof(buf) - pos, " %08x", v);
            }
            SMBWAP_LOG_INFO("%s", buf);
        }
    }

    // Sanity guard: data_ptr must look like a Switch heap address.
    // Empirically heap is in 0x20XXXXXXXX range.
    if (data_ptr < 0x2000000000ULL || data_ptr >= 0x2200000000ULL) {
        SMBWAP_LOG_INFO(
            "dumpSaveField(off=0x%x): data_ptr=0x%016llx outside expected "
            "heap range [0x2000000000, 0x2200000000); skipping data dump",
            field_offset, static_cast<unsigned long long>(data_ptr));
        return true;
    }

    // 81 = SMBW course count (per FUN_71003D4110 Murmur3 hash table).
    constexpr size_t kDumpCount = 81;
    SMBWAP_LOG_INFO(
        "dumpSaveField(off=0x%x): dumping %zu u32s at data_ptr=0x%016llx",
        field_offset, kDumpCount,
        static_cast<unsigned long long>(data_ptr));

    char buf[256];
    for (size_t row = 0; row < kDumpCount; row += 8) {
        size_t end = row + 8;
        if (end > kDumpCount) end = kDumpCount;
        int pos = snprintf(buf, sizeof(buf), "  data[%2zu..%2zu]:", row, end - 1);
        for (size_t i = row; i < end && pos < static_cast<int>(sizeof(buf)) - 12; ++i) {
            const uint32_t v = *reinterpret_cast<uint32_t*>(data_ptr + i * 4);
            pos += snprintf(buf + pos, sizeof(buf) - pos, " %08x", v);
        }
        SMBWAP_LOG_INFO("%s", buf);
    }
    return true;
}

// ----------------------------------------------------------------------------
// M3.3 GRANT PRIMITIVE — container-A counter writer.
//
// FUN_710049F648 at NSO +0x0049F648 is the universal counter setter for the
// hash-keyed table at gmd->[+0x40..0xf0] (container A).  Signature recovered
// statically (see docs/static-analysis-findings.md "Sprint 2" entry):
//     void FUN_710049F648(GameDataMgr*, uint32_t value, uint32_t hash);
//
// It's lock-free + thread-safe via ARM exclusive-monitor atomics on the dirty
// queue at gmd->[+0xf8] and is deferred-write — the value is queued and
// applied to the persistent container at the next save.
//
// FALSIFIED 2026-05-25 for bool slots: calling this writer with a bool-typed
// hash (e.g., 0x55815859 GRAND_SEED_WORLD1 value=1) lands the call but the
// post-save value stays at 0.  The writer is typed per-slot and silently
// no-ops on non-counter fields.  Bool grants must route through
// grantContainerBBool (container-B writer at +0x0049EA24) instead -- see
// dispatch branch in ap/ApFrameBridge.cpp drainInbound.
//
// External linkage so ap/ApFrameBridge.cpp can resolve probe::grantContainer-
// ACounter at link time -- the inbound drain path on the game thread calls
// this.
//
// HAZARD: GmdContainerAWriter is also installed as a probe trampoline at
// the same NSO offset (see installation below in exl_main).  Our calls will
// route THROUGH that trampoline, which logs every call.  Acceptable for now
// (free observability) but worth knowing -- the gmd.A_writer log line will
// fire on every grant.
// ----------------------------------------------------------------------------
using GmdSetCounterFn = void (*)(long gmd, uint32_t value, uint32_t hash);

bool grantContainerACounter(uint32_t hash, uint32_t value)
{
    long gmd = gmdSingleton();
    if (gmd == 0) return false;
    const uintptr_t base = exl::util::modules::GetTargetStart();
    auto fn = reinterpret_cast<GmdSetCounterFn>(base + 0x0049F648);
    fn(gmd, value, hash);

    static std::atomic<uint32_t> log_budget{16};
    if (log_budget.fetch_sub(1) > 0) {
        SMBWAP_LOG_INFO(
            "GrantHashKeyed: hash=0x%08x value=%u gmd=%p",
            hash, value, reinterpret_cast<void*>(gmd));
    }
    return true;
}

// ----------------------------------------------------------------------------
// Iteration #5 (2026-05-26): 5-hash "per-current-world Wonder Seed count"
// override.
//
// Run #4 (observability) confirmed the Wonder Seed gate predicate
// `FUN_71001787b40` reads `0x390eb960` via the container-A reader as the
// player's count.  Cross-correlation of save-load / course-clear / Poplin
// purchase events showed FIVE hashes update in lockstep with the count:
//
//   0x21f89ab1
//   0x8c20ccb7   (CLAUDE.md called this "lifetime" — wrong; it's per-current-world)
//   0xeeff353b
//   0x390eb960   ← the one the gate predicate reads directly
//   0xa0e5f253
//
// They reset to 0 on every world transition (W2→W3 wipe observed at
// 03:30 in run 09-34-58), so a one-shot write doesn't stick across
// movement.  This function pushes `value` to all five via the existing
// container-A counter writer.  Caller is expected to invoke it on a
// periodic tick (~2 s) to override the natural recompute.
//
// HYPOTHESIS UNDER TEST: writing value=99 to all 5 hashes should make
// any seed-gated path pass (all in-game thresholds are ≤ 25 per
// regions.json).  If gates open with this override active and the
// player has 1 actual W3 seed, the path forward is to drive these
// writes from the bridge with per-current-world AP counts.
// ----------------------------------------------------------------------------
void pushWonderSeedOverride(uint32_t value)
{
    long gmd = gmdSingleton();
    if (gmd == 0) return;
    const uintptr_t base = exl::util::modules::GetTargetStart();
    auto fn = reinterpret_cast<GmdSetCounterFn>(base + 0x0049F648);
    static constexpr uint32_t kHashes[5] = {
        0x21f89ab1u, 0x8c20ccb7u, 0xeeff353bu, 0x390eb960u, 0xa0e5f253u,
    };
    for (uint32_t h : kHashes) {
        fn(gmd, value, h);
    }

    static std::atomic<uint32_t> log_budget{8};
    if (log_budget.fetch_sub(1) > 0) {
        SMBWAP_LOG_INFO(
            "pushWonderSeedOverride: wrote value=%u to all 5 per-world hashes",
            value);
    }
}

// Active push variant: looks up the player's current world via container-A
// hash 0x9f5ead3c, picks the matching AP bucket, and writes that bucket's
// AP-known count to all 5 mirror hashes.  This is the proactive companion
// to the GmdContainerAWriter interceptor below — the interceptor only fires
// when the game itself writes, but the game does not appear to recompute
// the mirror hashes mid-area (only on world / area transitions), so AP
// grants received during gameplay would otherwise sit in g_wonder_seed_counts
// and not reach the gate predicate until the next transition.
//
// Called from drainInbound on every SetWonderSeedCounts (i.e. HelloMsg
// replay, ReceivedItems, and the bridge's 2 s periodic tick).  No-ops
// pre-save-load (the world hash isn't readable yet).
void pushWonderSeedOverrideCurrentWorld()
{
    long gmd = gmdSingleton();
    if (gmd == 0) return;
    if (!isSaveLoaded()) return;
    const uintptr_t base = exl::util::modules::GetTargetStart();
    using GmdGetFn = void (*)(long, uint32_t*, uint32_t);
    auto getfn = reinterpret_cast<GmdGetFn>(base + 0x0012AE94);
    uint32_t world_val = 0;
    getfn(gmd, &world_val, 0x9f5ead3c);
    // Same in-game world index → AP bucket mapping as the
    // GmdContainerAWriter interceptor.  Keep in sync if either ever changes.
    static constexpr int8_t kWorldValToBucket[9] = {
        -1,  // 0: unused
         0,  // 1: W1
         6,  // 2: Petal Isles
         1,  // 3: W2
         2,  // 4: W3
         3,  // 5: W4
         4,  // 6: W5
         5,  // 7: W6
         7,  // 8: Special World
    };
    if (world_val < 1 || world_val > 8) return;
    const uint32_t bucket =
        static_cast<uint32_t>(kWorldValToBucket[world_val]);
    const uint32_t ap_count = smbwap::ap::getWonderSeedCount(bucket);
    pushWonderSeedOverride(ap_count);
}

// ----------------------------------------------------------------------------
// Container-A counter SATURATING add/sub primitive.
//
// FUN_710012AE94 at NSO +0x0012AE94 is the universal counter reader for the
// hash-keyed table at gmd->[+0x40..0xf0] (container A).  Signature
// corrected by static-analysis sprint 2 (docs/static-analysis-findings.md
// line 2336):
//     void FUN_710012AE94(GameDataMgr*, uint32_t* out, uint32_t hash);
//
// Pairs with the writer at +0x49F648 to provide a read-modify-write add
// with signed delta.  Used for AP items whose semantic is "shift the
// running total by N" rather than "set to N" -- the counter is shared
// between in-game-earned and AP-granted progress, so an absolute set
// would clobber the player's own gameplay.  Two users today:
//   * +10 per "10 Coin" AP item received (grant).
//   * -10 per TEN_COIN check fired (refund the in-game pickup so AP
//     stays the sole authority over what each 10-coin block awards).
//
// Saturates at 0 on underflow: a refund at cur=0 stays 0, never wraps
// to ~4.3 billion.  The writer truncates per-slot internally (u8/u16),
// so even an over-budget positive delta lands cleanly modulo the slot
// width.
//
// Save-survival caveat (same as grantContainerACounter): the writer
// queues to the dirty buffer at gmd+0xf8 and flushes on next save.  A
// load-before-flush silently drops the increment.  The bridge does NOT
// replay container-A counter increments on HelloMsg -- replay would
// double-count.  Accepted for coin-style filler; if a counter ever
// becomes progression-critical, see IncrementHashKeyedMsg in
// apworld/.../wire.py for the dedup story we'd need.
//
// Same-session clobber (2026-05-26): the dirty queue at gmd+0xf8 is
// write-only from the reader's perspective -- getfn at +0x12AE94 reads
// the *persistent* container, which doesn't see queued writes until
// save flush.  So two RMW calls in quick succession (e.g. 10 "10 Coin"
// AP items arriving as 10 IncrementHashKeyed messages during one play
// session) both read the same persistent cur and each writes
// cur+delta to the queue, with the second clobbering the first --
// only the final delta survives.  Bridge-side coalescing
// (LanServer._pending_increments) handles same-event-loop bursts but
// can't help when the deltas arrive as separate ReceivedItems hundreds
// of ms apart (confirmed empirically in the 2026-05-26 16:12 Ryujinx
// log: 10 +10s landed 111 -> 121 instead of 111 -> 211).
//
// Fix: a small per-hash shadow table tracks the effective post-write
// value.  On each call, if the persistent reader returns the value we
// remember from the last call's input (-> the queue hasn't been
// flushed yet, our previous setfn is still pending), use our shadow
// total as the basis for the new RMW.  If the persistent value has
// diverged from what we last saw, a save flush has happened and the
// shadow is stale -- discard it and trust the persistent read.
// Self-resetting; no save-flush hook required.
//
// Trade-off: if the game itself writes to the same hash via setfn
// while our shadow is hot (between AP grants, before save flush), our
// next RMW would clobber the game's queued write.  Empirically the
// game doesn't appear to use setfn for the AP-targeted counters
// (flower_coin, regular_coin) during play -- it tracks them through a
// separate live-state struct, with setfn only invoked at save
// serialization time.  If that ever turns out to be wrong, the right
// fix is to read+write the live-state struct directly (latch TBD);
// the warning emitted below would surface the mismatch.
// ----------------------------------------------------------------------------
using GmdGetCounterFn = void (*)(long gmd, uint32_t* out, uint32_t hash);

namespace {
    // Small fixed-size shadow table.  Linear-scan lookup is fine at
    // this scale -- AP-targeted counter hashes are <=2 today
    // (flower_coin, regular_coin); kShadowSlots leaves headroom.  No
    // STL containers because the subsdk has no TLS allocator wired up
    // and heap-allocating containers in module-init code is hazardous
    // (same family of crash as the thread_local one in CLAUDE.md).
    struct IncrementShadow {
        uint32_t hash;             // 0 means slot is empty
        uint32_t last_persistent;  // what getfn returned at last call
        uint32_t shadow_value;     // what we wrote (effective live total)
    };
    constexpr size_t kShadowSlots = 8;
    IncrementShadow s_increment_shadow[kShadowSlots] = {};
}

bool incrementContainerACounter(uint32_t hash, int32_t delta)
{
    long gmd = gmdSingleton();
    if (gmd == 0) return false;
    const uintptr_t base = exl::util::modules::GetTargetStart();
    auto getfn = reinterpret_cast<GmdGetCounterFn>(base + 0x0012AE94);
    auto setfn = reinterpret_cast<GmdSetCounterFn>(base + 0x0049F648);

    uint32_t cur_persistent = 0;
    getfn(gmd, &cur_persistent, hash);

    // Find shadow slot for this hash (or allocate an empty one).  Hash
    // of 0 is treated as "slot empty" -- the bridge never sends hash=0,
    // so no collision with a real key.
    IncrementShadow* slot = nullptr;
    IncrementShadow* free_slot = nullptr;
    for (auto& s : s_increment_shadow) {
        if (s.hash == hash) { slot = &s; break; }
        if (s.hash == 0 && free_slot == nullptr) free_slot = &s;
    }

    uint32_t effective;
    bool used_shadow = false;
    if (slot != nullptr && slot->last_persistent == cur_persistent) {
        // Queue not flushed since last write -- persistent reader still
        // shows the pre-our-write value.  Trust our shadow as the real
        // live total.
        effective = slot->shadow_value;
        used_shadow = true;
    } else {
        // Either no prior write for this hash, or the persistent value
        // has changed since (save flush happened).  Trust the
        // persistent read; reset shadow.
        effective = cur_persistent;
        if (slot != nullptr && slot->last_persistent != cur_persistent
            && slot->shadow_value != cur_persistent) {
            // Persistent diverged from BOTH our last input AND our last
            // output -- a save flush happened OR a game-side setfn
            // landed.  Either way the shadow is stale; the log below
            // surfaces the case in case it ever becomes a game-side
            // collision worth investigating.
            SMBWAP_LOG_DEBUG(
                "IncrementHashKeyed: hash=0x%08x shadow reset "
                "(persistent=%u was last_seen=%u shadow=%u)",
                hash, cur_persistent,
                slot->last_persistent, slot->shadow_value);
        }
    }

    uint32_t next;
    if (delta >= 0) {
        const uint32_t udelta = static_cast<uint32_t>(delta);
        next = effective + udelta;  // u32 wrap; writer truncates per-slot
    } else {
        const uint32_t udec = static_cast<uint32_t>(-static_cast<int64_t>(delta));
        next = (effective >= udec) ? (effective - udec) : 0u;  // saturate at 0
    }
    setfn(gmd, next, hash);

    // Install or update the shadow entry.  If we couldn't find a slot
    // and the free_slot is also null (>kShadowSlots distinct hashes in
    // play) we silently lose the shadow for this hash -- worst case
    // the next call clobbers as before, no worse than today.
    if (slot == nullptr) slot = free_slot;
    if (slot != nullptr) {
        slot->hash = hash;
        slot->last_persistent = cur_persistent;
        slot->shadow_value = next;
    }

    static std::atomic<uint32_t> log_budget{16};
    if (log_budget.fetch_sub(1) > 0) {
        SMBWAP_LOG_INFO(
            "IncrementHashKeyed: hash=0x%08x persistent=%u effective=%u "
            "(%s) delta=%d -> %u gmd=%p",
            hash, cur_persistent, effective,
            used_shadow ? "shadow" : "persistent",
            static_cast<int>(delta), next, reinterpret_cast<void*>(gmd));
    }
    return true;
}

// ----------------------------------------------------------------------------
// M3.3b GRANT PRIMITIVE — container-B bool writer.
//
// FUN_710049EA24 at NSO +0x0049EA24 is the high-level bool setter for the
// gmd+8 substruct (hash-keyed bools).  Signature parallel to the container-A
// writer (recovered from docs/static-analysis-findings.md line 3215 + body
// decompile at line 3170-3171):
//     void FUN_710049EA24(GameDataMgr*, uint32_t value, uint32_t hash);
// Body:
//     if (FUN_710049ea78(gmd + 0x68) & 1) return;     // init/lock gate
//     FUN_7101f263fc(gmd + 8, value & 1, hash);       // delegate, value masked
//
// Used for the 8 documented bool hashes (6 Royal Seeds + COMPLETE_GAME +
// INTRO_CUTSCENE_COMPLETED -- see kBoolHashes in ApFrameBridge.hpp).  The
// existing GmdBoolWriter trampoline at NSO +0x01F263FC will log every call
// for free observability (substruct should equal gmd + 8).
//
// External linkage so ap/ApFrameBridge.cpp can resolve probe::grantContainer-
// BBool at link time.
//
// Same deferred-write semantics as container A: the bool is queued to the
// dirty buffer and flushed at next save.  M4.5 bridge replay-on-HelloMsg
// covers the save-survival path (re-emit every received item on reconnect).
// ----------------------------------------------------------------------------
using GmdSetBoolFn = void (*)(long gmd, uint32_t value, uint32_t hash);

bool grantContainerBBool(uint32_t hash, uint32_t value)
{
    long gmd = gmdSingleton();
    if (gmd == 0) return false;
    const uintptr_t base = exl::util::modules::GetTargetStart();
    auto fn = reinterpret_cast<GmdSetBoolFn>(base + 0x0049EA24);
    fn(gmd, value, hash);

    static std::atomic<uint32_t> log_budget{16};
    if (log_budget.fetch_sub(1) > 0) {
        SMBWAP_LOG_INFO(
            "GrantHashKeyedBool: hash=0x%08x value=%u gmd=%p",
            hash, value, reinterpret_cast<void*>(gmd));
    }
    return true;
}

// ----------------------------------------------------------------------------
// M3.3 PER-COURSE BITFIELD primitive — Container D (newly mapped 2026-05-25).
//
// Per-course flag arrays (CourseClear/Normal at file offset 0x43F0, GoalSeed
// at 0x3348, CourseClear/Badge at 0x4438, etc.) live in a previously
// undocumented hash-keyed (hash, course_index) -> u32 bitmask container.
// Each course type has its own hash (stored in the course descriptor struct
// returned by FUN_71000D33E0); each u32 holds 1 bit per exit type for that
// course (bit 0 = normal pole, bit 1 = secret exit, etc.).  The reader is
// FUN_71000E258C; the writer is FUN_7101F2B354.
//
// Discovered via forward-trace of the M1 SetCourseClearFlagToGameData hook
// (NSO +0x1bf28cc) using scripts/ghidra/find_per_course_array_writer.py +
// scripts/ghidra/dump_per_course_writer_candidates.py.  See round 1 / round
// 2 sections of docs/static-analysis-findings.md for the dataflow chain.
//
// Read-modify-write the bitmask for (hash, course_index) by ORing in bits
// new_bits.  Returns true if both read AND write took the gmd path; false
// if gmd was null (pre-save-select) or the read failed (hash unknown to the
// game).  Idempotent: calling with the same new_bits multiple times leaves
// the same final state.
//
// `value=read-modify-write semantics` is provided as a separate helper
// because most callers will want the absolute-set path (overwrite the
// bitmask with the bridge's canonical desired set) for save-survival /
// AP-authoritative reasons, same pattern as setBadgeBitfieldAbsolute.
//
// `set_per_course_bitfield_absolute` writes the exact value the bridge
// computed (no read first; idempotent under repeated calls).  This is the
// production grant path -- the bridge holds the authoritative mask and
// pushes the absolute set every ReceivedItems / HelloMsg / periodic tick,
// matching the M4 follow-up #2 badge sync pattern.
//
// Save-survival: same deferred-write semantics as container A/B (queues in
// the gmd dirty buffer, drains on next save).  The bridge replay-on-Hello
// pattern handles load-before-flush reverts.
// ----------------------------------------------------------------------------
using GmdGetPerCourseFn = void (*)(long gmd, uint32_t* out, uint32_t hash,
                                   uint32_t course_index);
using GmdSetPerCourseFn = void (*)(long gmd, uint32_t value, uint32_t hash,
                                   uint32_t course_index);

bool readPerCourseBitfield(uint32_t hash, uint32_t course_index, uint32_t* out)
{
    if (!out) return false;
    *out = 0;
    long gmd = gmdSingleton();
    if (gmd == 0) return false;
    const uintptr_t base = exl::util::modules::GetTargetStart();
    auto fn = reinterpret_cast<GmdGetPerCourseFn>(base + 0x000E258C);
    fn(gmd, out, hash, course_index);
    return true;
}

bool setPerCourseBitfieldAbsolute(uint32_t hash, uint32_t course_index,
                                  uint32_t bitmask)
{
    long gmd = gmdSingleton();
    if (gmd == 0) return false;
    const uintptr_t base = exl::util::modules::GetTargetStart();
    auto fn = reinterpret_cast<GmdSetPerCourseFn>(base + 0x001F2B354);
    fn(gmd, bitmask, hash, course_index);

    static std::atomic<uint32_t> log_budget{32};
    if (log_budget.fetch_sub(1) > 0) {
        SMBWAP_LOG_INFO(
            "SetPerCourseBitfield: hash=0x%08x course=%u value=0x%08x gmd=%p",
            hash, course_index, bitmask, reinterpret_cast<void*>(gmd));
    }
    return true;
}

// Read-modify-write helper: OR `new_bits` into the existing bitmask for
// (hash, course_index).  Used by the slash-command path for ad-hoc bit
// setting during hash discovery; production path is the absolute setter
// above (bridge holds the canonical mask).
bool grantPerCourseBits(uint32_t hash, uint32_t course_index, uint32_t new_bits)
{
    uint32_t cur = 0;
    if (!readPerCourseBitfield(hash, course_index, &cur)) return false;
    return setPerCourseBitfieldAbsolute(hash, course_index, cur | new_bits);
}

// ----------------------------------------------------------------------------
// M3.8 DeathLink — inbound synthetic-kill primitive.
//
// Anchor: the death-check code path at NSO +0x2743C0 reads HP as a
// signed halfword and branches if it's <= 0:
//     +0x2743BC: cbz   x9, LAB_71002743D0      ; null-check HP struct
//     +0x2743C0: ldrsh w9, [x9, #0x38]         ; load HP halfword (int16)
//     +0x2743C4: cmp   w9, #0
//     +0x2743C8: b.le  LAB_710027593c          ; <= 0 -> death handler
// HamletDuFromage's "Disable Death" cheat overwrites the LDRSH/CMP with
// `MOV W11,#1; STRB W11,[X9,#0x1C]` so the b.le never trips.  That +0x1C
// write is the cheat's "alive flag" hack, NOT the HP byte itself.  The
// actual HP is the signed int16 at +0x38 of the same struct; X9 here is
// the HP-bearing struct base.
//
// Latch strategy: install an inline hook at +0x2743BC (the cbz).  At
// that instruction, X9 has just been computed by the preceding indexed
// load from a sub-object chain rooted at the player tick function's arg
// and is about to be null-checked.  Capturing X9 here gives us the
// (validated-non-null on the next instruction) HP struct pointer
// without needing to replicate the deep dereference chain in C++.
//
// Patch-window safety:
//   * 5 instructions at +0x2743BC..+0x2743CF: cbz, ldrsh, cmp, b.le, strb.
//   * cbz and b.cond are PC-relative but exlaunch's relocator
//     (__fix_cond_comp_test_branch in hook_impl.cpp) handles both,
//     including the long-jump fallback when an offset overflows 19 bits.
//   * Patch ends exactly at +0x2743D0 -- the start of LAB_71002743D0,
//     a jump target from 6 callers.  At 20-byte stride the patch stops
//     one byte BEFORE that label, so the external branches still land
//     on intact code.  (Installing at +0x2743C0 instead would put the
//     label inside the patched region and corrupt those branches.)
//
// Once g_live_base is latched, synthKill writes 0 to base+0x38 as a
// signed int16.  On the next tick the death-check above re-reads HP,
// sees it <= 0, and branches to the death handler.  The loop-guard
// atomic suppresses the outbound DEATH_DETECTED echo so we don't
// bounce our own kill back to AP.
//
// History: the prior implementation latched X22 from the flower_coin
// writer at +0x49253C and wrote a uint8 0 to live_base+0x1C.  Live-
// falsified 2026-05-25 -- X22 there is a "stats / counters" sub-object
// (param_1[7] of FUN_7100491f60), NOT the HP struct.  +0x1C and +0x38
// belong to two different structs that the cheat-DB anchors happen to
// share the "live_base" naming for.
// ----------------------------------------------------------------------------

// Stable pointer to the live HP-bearing struct.  0 means "not yet
// latched"; synthKill no-ops in that case rather than risk writing to
// junk.  Captured by PlayerHpStructLatch (inline hook at +0x2743BC).
std::atomic<uintptr_t> g_live_base{0};

// Set by synthKill right before the HP=0 write so the
// NerveActivateOnce scene-transition branch can suppress the outbound
// DEATH_DETECTED that fires as a consequence of our own kill
// (loop-guard).  Mirrors SMO's ApState::synthetic_death_this_frame.
std::atomic<bool> g_synthetic_death_this_frame{false};

// Latched by NerveActivateOnce when the SceneTransition Nerve fires
// (vt_off == 0x33fd9a8 -- covers death, course entry/exit, world-map
// travel, palace, Poplin shop entry, post-Wonder-Seed cleanup).  The
// GmdContainerAWriter WS override interceptor reads this on whatever
// thread the container-A writer runs from (ModuleSystemWorker2
// observed) and skips its pre-Orig getfn while the elapsed delta is
// below kSceneTransitionGateTicks (3 s).  Rationale: an abort inside
// FUN_710049F750 (writer's secondary-container insert path at
// gmd+0x128) was reproducible on transitions; the secondary container
// isn't safe to probe concurrently with the game's transition-time
// bucket mutations.  3 s is generous -- gameplay is paused or
// non-interactive during transitions, so a brief gate has no visible
// cost; missing 3 s of override coverage post-transition is invisible
// because Wonder Seed gates only matter inside courses, not during
// transitions.
std::atomic<std::uint64_t> g_last_scene_transition_tick{0};

// External linkage so ap/ApFrameBridge.cpp can resolve probe::synthKill
// at link time -- the inbound drain path on the game thread calls this.
bool synthKill()
{
    const uintptr_t base = g_live_base.load(std::memory_order_relaxed);
    if (base == 0) {
        SMBWAP_LOG_INFO(
            "synthKill: no live_base latched yet; dropping "
            "(enter a level + walk a few frames so the death-check "
            "code path runs PlayerHpStructLatch)");
        return false;
    }
    // Set the loop-guard BEFORE the write so a death-handler tick that
    // races against us still sees the flag.
    g_synthetic_death_this_frame.store(true, std::memory_order_release);
    // HP is a signed int16 at +0x38.  Writing 0 makes the next
    // `LDRSH W9, [X9, #0x38]; CMP W9, #0; B.LE death` re-evaluate and
    // take the death branch.
    *reinterpret_cast<volatile int16_t*>(base + 0x38) = 0;
    SMBWAP_LOG_INFO(
        "synthKill: wrote HP=0 (int16) at live_base=%p +0x38",
        reinterpret_cast<void*>(base));
    return true;
}

}  // namespace probe

// Trampoline hook on the player tick function entry (NSO +0x273868 =
// FUN_7100273868(long param_1, long param_2)).  Replaces the
// abandoned inline hook at +0x2743BC -- that site's 5-instruction
// patch window appeared to corrupt the surrounding code path even
// though exlaunch's relocator nominally handles cbz/b.le.
// PlayerHpStructLatch (the inline-hook attempt) installed cleanly per
// the boot log but the function crashed silently the first time
// execution reached the patched region (2026-05-25 R-2026-05-25
// 08-53-58 log: no LiveBaseLatch fire before game termination, even
// though a SCENE_TRANSITION nerve had fired in the meantime).
//
// Function-entry trampoline avoids the relocator entirely.  We read
// X0 = param_1 (the "manager / scene root" the player tick walks
// from), then replicate the same dereference chain the function uses
// internally to reach the HP-bearing struct at +0x2743A0..+0x2743B8:
//   x8  = *(param_1 + 0x10)             // tick context
//   arr = *(x8 + 0x208)                 // sub-array
//   ver = *(x8 + 0x200)                 // version-style discriminator
//   off = (ver > 0x23) ? 0x118 : 0      // selects which slot
//   hp_struct = *(arr + off)            // HP-bearing object
//   ; HP int16 at hp_struct + 0x38
HOOK_DEFINE_TRAMPOLINE(PlayerTickLatch) {
    static void Callback(long param_1, long param_2);
};

void PlayerTickLatch::Callback(long param_1, long param_2)
{
    // Once-only latch -- this function is called every frame so the
    // first non-null walk captures, subsequent calls are an atomic
    // load + branch.
    if (probe::g_live_base.load(std::memory_order_relaxed) == 0 && param_1 != 0) {
        const auto deref8 = [](uintptr_t p, std::ptrdiff_t off) -> uintptr_t {
            return *reinterpret_cast<uintptr_t*>(p + off);
        };
        const auto deref4 = [](uintptr_t p, std::ptrdiff_t off) -> std::uint32_t {
            return *reinterpret_cast<std::uint32_t*>(p + off);
        };
        const uintptr_t x8 = deref8(static_cast<uintptr_t>(param_1), 0x10);
        if (x8 != 0) {
            const uintptr_t arr = deref8(x8, 0x208);
            if (arr != 0) {
                const std::uint32_t ver = deref4(x8, 0x200);
                const std::ptrdiff_t off = (ver > 0x23) ? 0x118 : 0;
                const uintptr_t hp_struct = deref8(arr, off);
                if (hp_struct != 0) {
                    uintptr_t expected = 0;
                    if (probe::g_live_base.compare_exchange_strong(
                            expected, hp_struct,
                            std::memory_order_release)) {
                        SMBWAP_LOG_INFO(
                            "PlayerTickLatch: latched live_base=%p "
                            "(HP int16 at +0x38; ver=0x%x off=0x%lx)",
                            (void*)hp_struct, (unsigned)ver,
                            static_cast<unsigned long>(off));
                    }
                }
            }
        }
    }
    Orig(param_1, param_2);
}

HOOK_DEFINE_TRAMPOLINE(GmdContainerAWriter) {
    static void Callback(long gmd, uint32_t value, uint32_t hash);
};
void GmdContainerAWriter::Callback(long gmd, uint32_t value, uint32_t hash)
{
    // M4.5: the game's save deserializer is the very first caller of
    // FUN_710049F648 -- our own grantContainerACounter only runs after
    // drainInbound ungates on s_save_loaded, so by induction the first
    // observed call is always game-initiated.  Use it as the save-loaded
    // signal.
    probe::markSaveLoaded("gmd.A_writer");

    // Wonder Seed substitution moved to the READER side (ContainerAReader)
    // to bypass the per-world UI cache invalidation gating.  The trampoline
    // remains installed here purely for markSaveLoaded + container-C diff
    // logging.

    SMBWAP_LOG_INFO("gmd.A_writer hash=0x%08x value=%u gmd=%p",
                    hash, value, reinterpret_cast<void*>(gmd));
    Orig(gmd, value, hash);
    probe::dumpContainerCDiff("A_writer", hash, value);
}

HOOK_DEFINE_TRAMPOLINE(GmdBoolWriter) {
    static uint64_t Callback(long substruct, uint8_t value, uint32_t hash);
};
uint64_t GmdBoolWriter::Callback(long substruct, uint8_t value, uint32_t hash)
{
    // M4.5: same gate as GmdContainerAWriter, in case the bool half of
    // the save deserializer beats the counter half (e.g., if save load
    // touches a container-B field before any container-A counter).
    probe::markSaveLoaded("gmd.bool_writer");
    SMBWAP_LOG_INFO("gmd.bool_writer hash=0x%08x value=%u substruct=%p",
                    hash, value, reinterpret_cast<void*>(substruct));
    uint64_t result = Orig(substruct, value, hash);
    probe::dumpContainerCDiff("bool_writer", hash, value);
    return result;
}

// Second per-bit reader (FUN_71001242d4 NSO +0x1242d4).  Signature:
//   (GameDataMgr*, byte* out, uint32_t hash, uint32_t bit_idx)
// Probes gmd+0x68's overlay container FIRST via vtable slot 0x20, then
// falls through to container C if not found.  This is the reader the
// game UI uses when asking "do I own badge X?" for the badge inventory
// screen.  By logging successful HITS with bit set (i.e., "yes, the
// player owns this"), the badge ownership hash surfaces directly —
// it's the hash queried with multiple bit_idx values where the result
// is "set".
HOOK_DEFINE_TRAMPOLINE(GmdC2BitReader) {
    static uint64_t Callback(long gmd, uint8_t* out, uint32_t hash, uint32_t bit_idx);
};
uint64_t GmdC2BitReader::Callback(long gmd, uint8_t* out,
                                  uint32_t hash, uint32_t bit_idx)
{
    uint64_t result = Orig(gmd, out, hash, bit_idx);
    // Filter: only log SUCCESSFUL queries where the bit is SET and the
    // bit_idx is in plausible badge range (u64 = 64 bits).  Skips the
    // bulk of UI-noise queries where the bit is 0 or out of badge range.
    if ((result & 1) && out && *out == 1 && bit_idx < 64) {
        static std::atomic<uint32_t> log_budget{2000};
        if (log_budget.fetch_sub(1) > 0) {
            SMBWAP_LOG_INFO("gmd.C2_hit hash=0x%08x bit=%u", hash, bit_idx);
        }
    }

    // M3.2 grant probe disabled — the primitive is validated.  Future
    // grant calls will come from the AP bridge via a deliberate trigger
    // (M4 work), not from inside this reader callback.
    return result;
}

// ----------------------------------------------------------------------------
// M3.3 Container-D writer probe (FUN_7101F2B354 at NSO +0x01F2B354).
//
// Signature (recovered 2026-05-25 from the call site in FUN_7101B5C3A0,
// which is the tail of SetCourseClearFlagToGameData -> FUN_7101BF2E40 chain):
//   void FUN_7101F2B354(GameDataMgr* gmd,
//                       uint32_t new_value,    // full bitmask
//                       uint32_t hash,         // which per-course array
//                       uint32_t course_index);// which slot
//
// Every "course cleared" Nerve fire eventually calls this with the new u32
// bitmask for the cleared course.  The hash distinguishes per-course-type
// arrays (CourseClear/Normal vs CourseClear/Badge vs GoalSeed/Normal etc.)
// stored in the course descriptor struct; the course_index is the slot
// within that array.  bit `goal_id` of the new value corresponds to the
// exit type that was just cleared.
//
// The probe trampoline logs every fire so we can capture the (hash,
// course_index, value) triplet on each clear.  Play through one course of
// each type (Normal W1, Badge W1, Palace, Arena, Break Time) and the log
// will reveal every per-course-array hash + the course_index <-> course-
// name mapping.  Once the hashes are known, wonder_seed_table.py on the
// bridge side maps AP "W{N} Wonder Seed" item counts to a set of
// (hash, course_index) calls to setPerCourseBitfieldAbsolute.
//
// Same save-loaded gate as the container-A/B writer probes -- the first
// game-initiated call is the save deserializer (also the first caller of
// FUN_7101F2B354 we'll observe), so we use it as the canonical save-loaded
// signal alongside markSaveLoaded's other triggers.
// ----------------------------------------------------------------------------
HOOK_DEFINE_TRAMPOLINE(GmdContainerDWriter) {
    static void Callback(long gmd, uint32_t value, uint32_t hash,
                         uint32_t course_index);
};
void GmdContainerDWriter::Callback(long gmd, uint32_t value, uint32_t hash,
                                   uint32_t course_index)
{
    probe::markSaveLoaded("gmd.D_writer");
    SMBWAP_LOG_INFO(
        "gmd.D_writer hash=0x%08x course=%u value=0x%08x gmd=%p",
        hash, course_index, value, reinterpret_cast<void*>(gmd));
    Orig(gmd, value, hash, course_index);
}

// ----------------------------------------------------------------------------
// M3.3 Phase B -- Save struct capture trampoline.
//
// FUN_71003e8e30 at NSO +0x3e8e30 is the save-field-registry setup function:
// it walks param_1's struct, CRC32-hashes field names, and registers each
// `(name_hash, &param_1[offset])` pair into a lookup table for later
// access.  `param_1` is the LIVE SAVE STRUCT we've been hunting -- it's
// heap-alloc'd (0x3E40 bytes via sead::Heap::operator new[]) inside
// FUN_71003e33b4 just before the call.  Field offset 0x3af8 within it is
// the WonderSeed/Normal SaveDataField.
//
// Hook stashes param_1 into the global `probe::g_captured_save_struct`
// atomic so the /dump_save_field probe can read from the right address
// without us having to chase the manager singleton chain.
//
// Trampoline-safe: FUN_71003e8e30 prologue is `stp x29,x30,...; stp x28...;
// stp x26...; stp x24...; stp x22,x21,...; stp x20,x19,...` (verified via
// the longfunc decompile).  No PC-relative loads in the first 5 insns.
// ----------------------------------------------------------------------------
HOOK_DEFINE_TRAMPOLINE(SaveDeserializerHook) {
    static void Callback(long param_1, uint32_t* param_2, long param_3);
};
void SaveDeserializerHook::Callback(long param_1, uint32_t* param_2,
                                    long param_3)
{
    // Minimal & safe: just record the pointer.  The previous version
    // tried to read 4 u64 signatures from the struct -- crashed when
    // the pointer was to partially-constructed memory (param_1 is set
    // by the caller BEFORE the struct is fully initialized in some
    // cases).  Reading any bytes from it from a hook callback races
    // against the caller's stores.
    if (param_1 != 0) {
        const uintptr_t prev = probe::g_captured_save_struct.exchange(
            static_cast<uintptr_t>(param_1), std::memory_order_release);
        if (prev != static_cast<uintptr_t>(param_1)) {
            SMBWAP_LOG_INFO(
                "SaveDeserializerHook: captured save_struct=0x%llx "
                "(was 0x%llx)",
                static_cast<unsigned long long>(
                    static_cast<uintptr_t>(param_1)),
                static_cast<unsigned long long>(prev));
        }
    }
    Orig(param_1, param_2, param_3);
}

// ----------------------------------------------------------------------------
// 2026-05-26: Wonder Seed gate observability hooks (LOGGING ONLY, no state
// mutation).  See docs/wonder-seed-observability-hook-prompt.md and the
// 2026-05-26 sections of docs/static-analysis-findings.md for the full RE
// context.  After 7 iterations of static analysis we converged on
// FUN_7100935ce0 @ NSO +0x935ce0 as the highest-likelihood per-world unlock
// gate candidate (8-call hardcoded dispatch over world IDs {1,3,4,5,6,7,2,9}
// matching the 8 AP Wonder Seed buckets).  The per-world record table that
// holds the queried hash at +0x68 is runtime-populated (.bss), so static
// analysis cannot determine what the function checks -- only RUNTIME
// observation can distinguish three hypotheses:
//   H1: it IS the gate (false -> true when seed count crosses threshold)
//   H2: it's a UI/static-progress reader (stable returns; fires at world-
//       map-load only)
//   H3: unrelated to the gate (rarely fires or only fires at startup)
//
// PROLOGUE SAFETY: both targets verified trampoline-safe via
// scripts/dump_prologue.py — no PC-relative ops in first 5 insns.
//   FUN_7100935ce0:  sub+stp+stp+stp+add  (clean stack frame setup)
//   FUN_7100124134:  stp+str+stp+stp+mov  (clean stack frame setup)
// ----------------------------------------------------------------------------
HOOK_DEFINE_TRAMPOLINE(WorldUnlockCheck) {
    static uint64_t Callback(uint32_t world_id);
};
uint64_t WorldUnlockCheck::Callback(uint32_t world_id)
{
    // 2026-05-26 obs-run-2: capture caller PC.  exlaunch's And64InlineHook
    // patches the game function with `BR X17` (unconditional branch, not
    // `BLR`), so x30 = game-caller's return address on entry.
    // __builtin_return_address(0) reads from the slot where our C++
    // prologue spilled x30, stable across Orig().  `caller_ret` is the
    // instruction AFTER the game's `bl FUN_7100935ce0`; subtract 4 to
    // get the `bl` instruction address itself (= `call_at`).
    const void* caller_ret = __builtin_return_address(0);
    uint64_t result = Orig(world_id);
    const uintptr_t base = exl::util::modules::GetTargetStart();
    const uintptr_t caller_abs = reinterpret_cast<uintptr_t>(caller_ret);
    const uintptr_t caller_nso =
        (caller_abs >= base) ? (caller_abs - base) : caller_abs;
    SMBWAP_LOG_INFO(
        "WorldUnlockCheck(world_id=%u) -> %llu  saveLoaded=%d  "
        "caller_ret=NSO+0x%llx call_at=NSO+0x%llx",
        world_id,
        static_cast<unsigned long long>(result),
        probe::isSaveLoaded() ? 1 : 0,
        static_cast<unsigned long long>(caller_nso),
        static_cast<unsigned long long>(caller_nso - 4));
    return result;
}

// SeedBitfieldRead — observability on the per-bit container-C reader
// FUN_7100124134.  Signature (recovered iteration 4):
//   undefined8 FUN_7100124134(GameDataMgr* gmd, byte* out_bit,
//                             uint32_t hash, uint32_t bit_index)
// 55+ xrefs in the binary, so we MUST filter on hash == 0x60458608 (the
// per-course Wonder Seed bitfield confirmed in iteration 7) to avoid log
// spam.  Capture *out AFTER Orig to see the resolved bit value.
//
// 2026-05-26 obs-run-2 addition: capture caller_ret via
// __builtin_return_address(0) BEFORE Orig().  This is the key
// instrumentation that unblocks gate discovery — the 2026-05-26 obs run
// established that 0x60458608 is read ONCE at save-load only (not on
// the gate-decision path), so the single fire's caller IS the save
// deserializer that populates the live-state cache.  Decompiling that
// caller reveals where the bits get stored, which is the next step
// toward finding the actual gate.
HOOK_DEFINE_TRAMPOLINE(SeedBitfieldRead) {
    static uint64_t Callback(long gmd, uint8_t* out_bit,
                             uint32_t hash, uint32_t bit_index);
};
uint64_t SeedBitfieldRead::Callback(long gmd, uint8_t* out_bit,
                                    uint32_t hash, uint32_t bit_index)
{
    const void* caller_ret = __builtin_return_address(0);
    uint64_t result = Orig(gmd, out_bit, hash, bit_index);
    if (hash == 0x60458608u) {
        const uintptr_t base = exl::util::modules::GetTargetStart();
        const uintptr_t caller_abs = reinterpret_cast<uintptr_t>(caller_ret);
        const uintptr_t caller_nso =
            (caller_abs >= base) ? (caller_abs - base) : caller_abs;
        SMBWAP_LOG_INFO(
            "SeedBitfieldRead(hash=0x60458608, bit=%u) -> success=%llu value=%u  "
            "saveLoaded=%d  caller_ret=NSO+0x%llx call_at=NSO+0x%llx",
            bit_index,
            static_cast<unsigned long long>(result),
            out_bit ? static_cast<unsigned>(*out_bit) : 0xFFu,
            probe::isSaveLoaded() ? 1 : 0,
            static_cast<unsigned long long>(caller_nso),
            static_cast<unsigned long long>(caller_nso - 4));
        // 2026-05-26 obs-run-3: ride the save-load fire to dump per-world
        // record tables.  One-shot; subsequent fires are no-ops.
        probe::dumpPerWorldTables();
    }
    return result;
}

// ----------------------------------------------------------------------------
// 2026-05-26 obs-run-3: container-A counter reader observability.
//
// FUN_710012ae94 @ NSO +0x12ae94 is the primary container-A getter
// (gmd, &out_value, hash).  ~66 xrefs; called heavily on every world-map
// refresh, gate decision, telemetry build, and UI tick.  Per-hash budget
// (kReaderBudgetPerHash=5) prevents log flood while still surfacing the
// caller PC for each distinct hash on the first few fires.
//
// Goal of this hook: when the per-world record tables (dumped above) reveal
// the per-world subtotal hashes, those hashes will show up here with
// caller PCs.  Cross-referencing those caller PCs against world-map travel
// Nerves and gate UI handlers points at the gate function.
//
// PROLOGUE SAFETY NOTE: FUN_710012ae94's 5th insn is `cbz w2, #0x12afe0`
// (null-hash early-return).  Per CLAUDE.md gotcha #2, only adrp/adr/
// ldr-literal/b/bl are documented to corrupt the trampoline; cbz is
// relocator-handled (see [hook_impl.cpp:210 __fix_cond_comp_test_branch]).
// The relocator emits a 6-insn replacement that's well within the 50-insn
// trampoline buffer.  Practical precedent: the PlayerHpStructLatch crash
// at +0x2743BC was an INLINE hook (mid-function), not a trampoline at
// entry — different code path with different bug surface.  TEST RESULT
// PENDING: if game crashes within ~30 s of boot with the same gmd::
// SaveDataMgr abort signature, this hook is the cause and should be
// rolled back.
// ----------------------------------------------------------------------------
HOOK_DEFINE_TRAMPOLINE(ContainerAReader) {
    static uint64_t Callback(long gmd, uint32_t* out_value, uint32_t hash);
};
uint64_t ContainerAReader::Callback(long gmd, uint32_t* out_value,
                                    uint32_t hash)
{
    const void* caller_ret = __builtin_return_address(0);
    uint64_t result = Orig(gmd, out_value, hash);

    // ------------------------------------------------------------------
    // AP-authoritative Wonder Seed substitution at the READER.
    //
    // Goal: when the game reads a Wonder Seed count from container A,
    // return the AP-authoritative count for the current world instead of
    // the game's natural count.  This makes the gate-unlock predicate at
    // FUN_71001787b40 (which gates world-map travel on accumulated
    // seeds) honor the AP item state, and propagates to the world-map
    // UI count display.
    //
    // The 5 mirror hashes group:
    //   0x21f89ab1, 0x8c20ccb7, 0xeeff353b, 0x390eb960, 0xa0e5f253
    // All five are written together with the same value in steady state
    // (writer-storm pattern observed at 10.913-10.979 of crash log
    // Ryujinx_1.3.3_2026-05-26_19-16-12.log; values are u32 counts of
    // collected seeds for the current world).  Per
    // memory/smbwap_wonder_seed_gate_solved.md: writing to this mirror
    // group unlocks gates; the gate predicate specifically reads
    // 0x390eb960.
    //
    // ------------------------------------------------------------------
    // Bisect history (2026-05-26) -- DO NOT compress without keeping
    // the conclusions; this is load-bearing context if the flake returns.
    // ------------------------------------------------------------------
    //
    // ATTEMPT 0 -- substitute all 5.  CRASHED flakily within 2 course
    // in/outs.  Last successful log line before crash: an A_reader fire
    // on 0x9f5ead3c (world index) from FUN_71004cceb0; multi-thread
    // reader storm visible on threads 88/89/46 in the lead-up.  Crashed
    // hard enough that Ryujinx didn't flush the fatal block to disk.
    //
    // BISECT-A -- keep trampoline + recursive Orig but disable the
    // *out_value mutation.  Stable across 7 course in/outs.  Conclusion:
    // the substituted value reaching a downstream consumer is necessary
    // for the crash.  The trampoline itself + recursive Orig of the
    // world index from inside the callback are both safe.  Reader is
    // pure (decompile of FUN_710012ae94 confirms no internal mutex / no
    // LRU update / no side effects).
    //
    // BISECT-B -- substitute ONLY the gate hash 0x390eb960.  Stable AND
    // the world-map UI count displayed the AP value correctly.  Two
    // findings from this iteration:
    //
    //   (1) The world-map UI count reads from the gate hash, so the
    //       "per-world UI cache" we'd been hypothesizing as the reason
    //       to move from writer-side to reader-side substitution
    //       probably doesn't exist -- the original write-side
    //       interceptor most likely failed for a different reason
    //       (scene-transition timing race fixed in PR #40).
    //
    //   (2) UX quirk: upon ENTERING a course the displayed count
    //       briefly swaps to the natural value because the
    //       isInSceneTransitionWindow() gate suppresses substitution
    //       during transitions.  This may matter for gate predicates
    //       that fire mid-transition.  The transition gate was carried
    //       over from the write-side code path (which had a real race
    //       with FUN_710049F750); for the reader-side path the gate
    //       is theoretically not needed but kept for safety until
    //       proven harmful.
    //
    // BISECT-C -- substitute gate + first-half of the 4 non-gate
    // mirrors (0x21f89ab1, 0x8c20ccb7).  Stable AND fixed the bisect-B
    // UX quirk: the in-course HUD now also shows the AP value because
    // one of these two hashes is what it reads from.  This is the
    // CURRENT shipping configuration.
    //
    // Conclusion + animation hypothesis (live-validated):
    //   The 5 mirrors serve animation interpolation roles -- some
    //   subset are "previous"/"current" buffers used to drive seed-count
    //   gain animations.  Substituting all 5 forced the animation
    //   engine to compute a Delta of 10 (game=6 -> ap=16 in the crash
    //   case) when it expects Delta <= 1 per frame, OOBing some
    //   fixed-size animation slot array.  Leaving 0xeeff353b and
    //   0xa0e5f253 untouched starves the animation engine of the input
    //   that triggers the OOB, so it never fires.
    //
    // Why exactly these 3 hashes are safe but the other 2 aren't:
    //   Unknown.  Hypotheses:
    //     * 0xeeff353b / 0xa0e5f253 are the "animation source/target"
    //       buffers and reading a discrepancy triggers a tween
    //     * One of them is the "world clear celebration" trigger
    //     * One is the in-course HUD's "previous frame" used to detect
    //       gain events for sound/particle effects
    //   None of these are confirmed; the bisect just proved which set
    //   produces stable behavior.  If the flake returns, the next bisect
    //   step is to peel back 0x8c20ccb7 first (it had zero callers in
    //   the crash log -- least observed), then 0x21f89ab1, returning to
    //   the gate-only configuration as a fallback.
    //
    // ------------------------------------------------------------------
    //
    // Implementation gates (preserved across the bisect):
    //   * isSaveLoaded() -- gmd singleton ready, container A populated
    //   * !isInSceneTransitionWindow() -- inherited from write-side
    //     path; see BISECT-B finding (2) above
    //   * Recursive Orig(gmd, &world_val, 0x9f5ead3c) to fetch the
    //     world index bypasses our trampoline so it's recursion-safe
    //     (reader is pure per the decompile)
    //   * world_val -> bucket via kWorldValToBucket -- in-game world
    //     index is NOT the AP bucket order: 1=W1, 2=Petal Isles,
    //     3=W2, 4=W3, 5=W4, 6=W5, 7=W6, 8=Special, while AP buckets are
    //     0=W1, 1=W2..5=W6, 6=Petal Isles, 7=Special.
    // ------------------------------------------------------------------
    static constexpr uint32_t kSafeWsSubstituteHashes[3] = {
        0x390eb960u,  // gate hash (BISECT-B safe; read by FUN_71001787b40)
        0x21f89ab1u,  // BISECT-C safe; in-course HUD reads from this group
        0x8c20ccb7u,  // BISECT-C safe; zero callers in crash log (wildcard)
        // 0xeeff353b -- NOT substituted; suspected animation trigger
        // 0xa0e5f253 -- NOT substituted; suspected animation trigger
    };
    bool is_substitute_target = false;
    for (uint32_t h : kSafeWsSubstituteHashes) {
        if (h == hash) { is_substitute_target = true; break; }
    }
    if (out_value != nullptr && gmd != 0 && is_substitute_target
        && probe::isSaveLoaded()
        && !probe::isInSceneTransitionWindow()) {
        uint32_t world_val = 0;
        Orig(gmd, &world_val, 0x9f5ead3c);
        static constexpr int8_t kWorldValToBucket[9] = {
            -1, 0, 6, 1, 2, 3, 4, 5, 7,
        };
        if (world_val >= 1 && world_val <= 8) {
            const uint32_t bucket =
                static_cast<uint32_t>(kWorldValToBucket[world_val]);
            const uint32_t ap_count =
                smbwap::ap::getWonderSeedCount(bucket);
            const uint32_t game_value = *out_value;
            if (game_value != ap_count) {
                static std::atomic<uint32_t> sub_log_budget{32};
                if (sub_log_budget.fetch_sub(1) > 0) {
                    SMBWAP_LOG_INFO(
                        "WS read substitute hash=0x%08x world=%u "
                        "bucket=%u game_value=%u -> ap_count=%u",
                        hash, world_val, bucket, game_value, ap_count);
                }
                *out_value = ap_count;
            }
        }
    }

    const uintptr_t base = exl::util::modules::GetTargetStart();
    const uintptr_t caller_abs = reinterpret_cast<uintptr_t>(caller_ret);
    const uintptr_t caller_nso =
        (caller_abs >= base) ? (caller_abs - base) : caller_abs;
    // Iteration #4: budget per (hash, caller_pc) pair, not per hash.
    // Blocklist on 0x89c8d0 (the dominant batch caller) lives inside.
    if (probe::readerBudgetTake(hash, caller_nso - 4)) {
        SMBWAP_LOG_INFO(
            "A_reader hash=0x%08x value=%u success=%llu  saveLoaded=%d  "
            "caller_ret=NSO+0x%llx call_at=NSO+0x%llx",
            hash,
            out_value ? *out_value : 0xFFFFFFFFu,
            static_cast<unsigned long long>(result),
            probe::isSaveLoaded() ? 1 : 0,
            static_cast<unsigned long long>(caller_nso),
            static_cast<unsigned long long>(caller_nso - 4));
    }
    return result;
}

extern "C" void exl_main(void* x0, void* x1)
{
    // Trace every step so we can identify which call kills the JIT.
    // svcOutputDebugString is callable before exl::hook::Initialize, so
    // this line proves we entered exl_main at all.
    SMBWAP_LOG_INFO("smbwap: exl_main entered (pre-init)");

    exl::hook::Initialize();
    SMBWAP_LOG_INFO("smbwap: exl::hook::Initialize() returned");

    using Patcher = exl::patch::CodePatcher;
    using namespace exl::patch::inst;

    SMBWAP_LOG_INFO("smbwap: installing InitActorPlacementInfo @ 0x5815c");
    InitActorPlacementInfo::InstallAtOffset(0x0005815c);

    SMBWAP_LOG_INFO("smbwap: installing CreateRootHeap @ 0x5a66f8");
    CreateRootHeap::InstallAtOffset(0x005a66f8);

    SMBWAP_LOG_INFO("smbwap: installing CreateFileDeviceMgr @ 0x5a6110");
    CreateFileDeviceMgr::InstallAtOffset(0x005a6110);

    SMBWAP_LOG_INFO("smbwap: installing GameFrameworkInitialize @ 0x5a5cfc");
    GameFrameworkInitialize::InstallAtOffset(0x005a5cfc);

    // M4: nn::socket::Initialize now runs INSIDE GameFrameworkInitialize::Callback
    // (pre-Orig) with a static 768 KiB pool.  Old commented block kept here
    // for archaeology -- see the new callback for the live code path.

    // M1 triage outcome: with both nvnImGui and FSHacks disabled the game
    // boots stably. ImGui rendering hooks NVN at hardcoded offsets and was
    // crashing on this user's build; we don't need the in-game overlay for
    // M1 (svcOutputDebugString → Ryujinx log is sufficient). FSHacks hooks
    // nn::fs::OpenFile by SYMBOL name (`_ZN2nn2fs8OpenFileEPNS0_10FileHandleEPKci`),
    // not by offset — much safer across builds, so re-enable.
    SMBWAP_LOG_INFO("smbwap: M1: SKIPPING nvnImGui hooks (not needed)");
    // nvnImGui::InstallHooks();
    // nvnImGui::addDrawFunc(drawDbgGui);

    SMBWAP_LOG_INFO("smbwap: installing pe::FSHacks");
    pe::installFSHacks();

    // GoalDispatcher disabled — turned out to be a level-load registration
    // handler, not a touch event. See its callback comment.
    // GoalDispatcher::InstallAtOffset(0x00299488);

    // M1.3 Wonder Seed event hook — abandoned (see WonderSeedExecute comment).
    // WonderSeedExecute::InstallAtOffset(0x00562fb4);

    // M1.3 shared Nerve-one-shot-activate hook. Catches every event Nerve
    // activation (Wonder Seed, Goal touched, etc.) by intercepting the
    // shared inner helper they all call. We filter by reading the nerve's
    // vtable at Callback entry.
    SMBWAP_LOG_INFO("smbwap: installing NerveActivateOnce @ 0x559f7c");
    NerveActivateOnce::InstallAtOffset(0x00559f7c);

    // M1.3 Goal hook — direct trampoline on the SetCourseClearFlagToGameData
    // Nerve execute (slot 8). Fires when the engine writes the "course
    // cleared" flag to save data, which happens only on successful clear.
    SMBWAP_LOG_INFO("smbwap: installing SetCourseClearFlagExecute @ 0x1bf28cc");
    SetCourseClearFlagExecute::InstallAtOffset(0x001bf28cc);

    // M3.7 Game-completion goal hook — direct trampoline on the
    // SetFlagEndDispMsgFirstVisitedWorldAfterClearedLastBoss Nerve execute
    // (slot 8 of vtable +0x3363330).  Fires once per save the first time
    // the player defeats final Bowser.  See the hook definition above for
    // discovery notes.
    SMBWAP_LOG_INFO("smbwap: installing GameGoalReachedExecute @ 0x15b77a8");
    GameGoalReachedExecute::InstallAtOffset(0x0015b77a8);

    // M2.4 — nn::prepo::PlayReport instrumentation.
    //
    // ROLLED BACK 2026-05-20 after two consecutive guest aborts:
    //   - run 15:19 — crashed entering Bulrush Coming Through (17 min in)
    //   - run 15:43 — crashed entering W1-1 from a new save (26 s in)
    // Both aborts: GuestBrokeExecutionException → nn::diag::detail::AbortImpl
    // on ModuleSystemWorker1, inside nn::account::ShowUserCreator (sdk +0x198284),
    // called from game code (Secred.nss +0x2330b0).  Aborts fire ~6 s after our
    // last prepo.save_uid, suggesting the SDK background worker validates
    // something (account state? report ordering?) and finds the hooks made it
    // inconsistent. The hooks themselves install fine and fire correctly — 137
    // prepo lines captured cleanly across the first crash run — so this is
    // *not* an install error or an immediate crash.
    //
    // To re-enable for bisecting:
    //   1. Confirm rollback fixes by playing W1-1 with this build.
    //   2. Uncomment Ctor + Save (minimum diagnostic).  Test W1-1.  If crash
    //      returns, the worker validates report-call counts and even no-op
    //      hooking is too disruptive.  If not, continue.
    //   3. Add the Add overloads one family at a time (int → str → struct →
    //      array → bool).  Test W1-1 after each.  The first one that
    //      reintroduces the abort is the trigger.
    // Hook definitions are left intact above so re-enabling is comment-toggle.
    // M2.4 bisect step 1 — ctor-only.  If W1-1 still aborts with only this
    // one hook installed, even trampolining the prepo ctor is enough to
    // upset the SDK worker-thread validation; we abandon symbol-based prepo
    // hooks entirely and move to a different identification strategy. If
    // W1-1 survives, we have partial M2.4 (event-id capture) and can
    // continue uncommenting the Add hooks one family at a time.
    // M2.4 bisect step 4 — drop the PlayReport::Save hooks (they crashed
    // gmd::SaveDataMgr), keep the safe ctor + SetEventId, and probe the IPC
    // client layer below the PlayReport class for buffer extraction.
    SMBWAP_LOG_INFO("smbwap: installing ctor + SetEventId + IPC SaveReport (M2.4 bisect step 4)");
    PlayReportCtor::InstallAtSymbol("_ZN2nn5prepo10PlayReportC2EPKc");
    PlayReportSetEventId::InstallAtSymbol("_ZN2nn5prepo10PlayReport10SetEventIdEPKc");
    // Save hooks rolled back — these crash the SDK on a delayed audit:
    // PlayReportSave::InstallAtSymbol("_ZN2nn5prepo10PlayReport4SaveEv");
    // PlayReportSaveUid::InstallAtSymbol("_ZN2nn5prepo10PlayReport4SaveERKNS_7account3UidE");
    PrepoIpcSaveReport::InstallAtSymbol(
        "_ZN2nn2sf4cmif6client6detail13CmifProxyImplINS_5prepo6detail3ipc13IPrepoServiceENS2_19CmifDomainProxyKindINS0_4hipc6client38Hipc2ClientSessionManagedProxyKindBaseINSB_18Hipc2ProxyKindBaseILNSA_6detail11MessageTypeE6EEEEEEENS0_30MemoryResourceAllocationPolicyES8_NS3_19ProcessModifierImplINS2_21DefaultProxyFilterTagEEEE22_nn_sf_sync_SaveReportERKNS0_7InArrayIcEERKNS0_8InBufferEm");
    PrepoIpcSaveReportWithUser::InstallAtSymbol(
        "_ZN2nn2sf4cmif6client6detail13CmifProxyImplINS_5prepo6detail3ipc13IPrepoServiceENS2_19CmifDomainProxyKindINS0_4hipc6client38Hipc2ClientSessionManagedProxyKindBaseINSB_18Hipc2ProxyKindBaseILNSA_6detail11MessageTypeE6EEEEEEENS0_30MemoryResourceAllocationPolicyES8_NS3_19ProcessModifierImplINS2_21DefaultProxyFilterTagEEEE30_nn_sf_sync_SaveReportWithUserERKNS_7account3UidERKNS0_7InArrayIcEERKNS0_8InBufferEm");
    // PlayReportAddInt::InstallAtSymbol("_ZN2nn5prepo10PlayReport3AddEPKcl");
    // PlayReportAddStr::InstallAtSymbol("_ZN2nn5prepo10PlayReport3AddEPKcS3_");
    // PlayReportAddBool::InstallAtSymbol("_ZN2nn5prepo10PlayReport3AddEPKcb");
    // PlayReportAddStruct::InstallAtSymbol("_ZN2nn5prepo10PlayReport3AddEPKcRKNS0_6StructE");
    // PlayReportAddArray::InstallAtSymbol("_ZN2nn5prepo10PlayReport3AddEPKcRKNS0_5ArrayE");
    // StructAddInt::InstallAtSymbol("_ZN2nn5prepo6Struct3AddEPKcl");
    // StructAddBool::InstallAtSymbol("_ZN2nn5prepo6Struct3AddEPKcb");

    // M1: wondar's "just dont crash, idiot" SDK patch — hardcoded to a
    // specific NSO layout. Disabled while we determine whether it's the
    // crash source on this user's v1.0.0 build. Re-enable if needed once
    // the rest of boot is verified.
    // exl::util::RwPages a(exl::util::GetSdkModuleInfo().m_Total.m_Start + 0x00399790, 4);
    // *reinterpret_cast<u32*>(a.GetRw()) = 0xD65F03C0;

    // M3.2 probe hooks — see comment block above the hook definitions.
    // Iteration 2 (after run #13 hook fires showed no badge-specific writer):
    // we drop the noisy C_reader hook and add an inline container-C diff
    // dumper that runs on every writer fire.  The dumper logs ONLY on changed
    // bitfields, so the badge purchase event will produce ONE "dump_C CHANGE"
    // line per bitfield that flipped — naming the badge hash directly.
    SMBWAP_LOG_INFO("smbwap: M3.2 probe — installing GmdContainerAWriter @ 0x49f648");
    GmdContainerAWriter::InstallAtOffset(0x0049f648);

    SMBWAP_LOG_INFO("smbwap: M3.2 probe — installing GmdBoolWriter @ 0x1f263fc");
    GmdBoolWriter::InstallAtOffset(0x01f263fc);

    // GmdContainerCBitReader hook intentionally NOT installed — replaced by
    // the inline container-C diff dumper called from each writer's Callback.

    // Iteration 3 — install the SECOND per-bit reader (FUN_71001242d4).  Save
    // diff confirmed the badge bitfield is u64 at file offset 0x0EA0 but is
    // NOT in container C (no matching value among 156 captured entries).
    // So it lives in gmd+0x68 which this reader probes FIRST.  Successful
    // hits with bit set will surface the badge ownership hash.
    SMBWAP_LOG_INFO("smbwap: M3.2 probe — installing GmdC2BitReader @ 0x1242d4");
    GmdC2BitReader::InstallAtOffset(0x001242d4);

    // M3.3 — Container-D writer probe.  Logs every (hash, course_index, value)
    // tuple FUN_7101F2B354 is called with.  Required for hash-discovery: play
    // through one course of each type and the log will name every per-course
    // bitfield's hash + the course_index <-> course mapping.  See
    // docs/static-analysis-findings.md M3.3 round-2 for the discovery details.
    SMBWAP_LOG_INFO("smbwap: M3.3 probe — installing GmdContainerDWriter @ 0x1f2b354");
    GmdContainerDWriter::InstallAtOffset(0x001f2b354);

    // M3.3 Phase B -- capture the live save struct pointer (heap-alloc'd
    // 0x3E40 bytes inside FUN_71003e33b4, passed as param_1 to
    // FUN_71003e8e30).  Lets /dump_save_field read from the right
    // address without chasing a manager singleton chain.
    SMBWAP_LOG_INFO("smbwap: M3.3 Phase B -- installing SaveDeserializerHook @ 0x3e8e30");
    SaveDeserializerHook::InstallAtOffset(0x003e8e30);

    // 2026-05-26: Wonder Seed gate observability — LOGGING ONLY.  See the
    // hook definitions above and docs/wonder-seed-observability-hook-prompt.md.
    SMBWAP_LOG_INFO("smbwap: observability — installing WorldUnlockCheck @ 0x935ce0");
    WorldUnlockCheck::InstallAtOffset(0x00935ce0);
    SMBWAP_LOG_INFO("smbwap: observability — installing SeedBitfieldRead @ 0x124134 (filtered on hash=0x60458608)");
    SeedBitfieldRead::InstallAtOffset(0x00124134);
    SMBWAP_LOG_INFO("smbwap: observability — installing ContainerAReader @ 0x12ae94 (cbz-prologue; per-hash budget=5)");
    ContainerAReader::InstallAtOffset(0x0012ae94);

    // M3.8 -- PlayerTickLatch (trampoline hook on FUN_7100273868 entry,
    // NSO +0x273868).  Replaces the abandoned PlayerHpStructLatch
    // inline hook at +0x2743BC which crashed Ryujinx silently when the
    // patched region was reached (R-2026-05-25 08-53-58 log: hook
    // installed but never fired before termination; even though
    // exlaunch's relocator nominally handles cbz/b.le, something in the
    // patch window corrupted the path).  Trampoline hooks on function
    // entries are simpler -- no relocator concerns, just a single
    // function-pointer indirection per call -- and the player tick
    // function is called every frame, so the latch captures within a
    // frame of entering a level.
    //
    // Callback replicates the dereference chain at +0x2743A0..+0x2743B8
    // to find the HP-bearing struct from param_1.
    constexpr bool kEnableLiveBaseLatch = true;
    if (kEnableLiveBaseLatch) {
        SMBWAP_LOG_INFO("smbwap: M3.8 — installing PlayerTickLatch @ 0x273868 (FUN_7100273868 entry)");
        PlayerTickLatch::InstallAtOffset(0x00273868);
    } else {
        SMBWAP_LOG_INFO(
            "smbwap: M3.8 — PlayerTickLatch DISABLED (kEnableLiveBaseLatch=false). "
            "synthKill will no-op until enabled.");
    }

    SMBWAP_LOG_INFO("smbwap: exl_main returning normally");
}

extern "C" NORETURN void exl_exception_entry()
{
    /* TODO: exception handling */
    EXL_ABORT(0x420);
}
