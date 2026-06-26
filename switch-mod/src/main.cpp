// SMBW Archipelago — hakkun edition entry point.
//
// Phases 2a-2d port the exlaunch-era trampolines from the legacy main.cpp
// (parked at src/program/main.cpp, excluded from the build) to hakkun:
//   * Phase 2a: 3 CORE_INIT hooks via installAtMainOffset.
//   * Phase 2b: 3 M1_EVENTS hooks (NerveActivateOnce + 2 Nerve execute hooks).
//   * Phase 2c: 4 PLAYREPORT hooks via installAtSym (mangled C++ symbols
//     resolved at runtime via hk::ro::lookupSymbol because we're built
//     with HK_DISABLE_SAIL).
//   * Phase 2d: 2 GRANTS hooks (the GameDataMgr container A/B writers
//     that the M3 sprint identified as the universal save-grant primitives).
//
// All phases are "install + observe" only. The smbwap::ap::* bridge
// wire-up, the WONDER_SEED_AWARDED vtable filter, the SCENE_TRANSITION
// death detector, the GmdContainerAWriter override, and the
// PlayReport-payload bridge enqueue all return in Phase 2d/e when the
// ap/ subsystem is restored to the build alongside the GRANTS hooks.
//
// Hook signatures stay intentionally minimal — `void*` for Nerves and
// PlayReport `this` pointers (we never dereference past one level),
// `const PrepoInArrayChar*` / `const PrepoInBuffer*` for the IPC layer
// (16-byte structs passed by const& through the AAPCS, received as
// pointer-to-stack-temp). No sead/nn headers needed.

#include <atomic>
#include <cstddef>
#include <cstdint>

#include "hk/hook/Trampoline.h"
#include "hk/ro/RoUtil.h"
#include "hk/ro/RoModule.h"
#include "hk/svc/api.h"
#include "hk/types.h"

#include "ap/ApClient.hpp"
#include "ap/ApFrameBridge.hpp"
#include "ap/ApProtocol.hpp"
#include "probe/BadgeShop.hpp"
#include "probe/DeathLink.hpp"
#include "probe/Gates.hpp"
#include "probe/Gmd.hpp"
#include "probe/ItemGetGate.hpp"
#include "ui/ApDebugConsole.hpp"
#include "ui/ImGuiNvnBootstrap.hpp"
#include "util/Log.hpp"

namespace nn::socket {
    // Forward-decl: declared without bodies; resolved by the runtime
    // RTLD symbol lookup that hakkun's installAtSym pattern uses. We
    // call this once from GameFrameworkInitialize::post-Orig to bring
    // up our own socket pool (768 KiB, matches the legacy build).
    unsigned int Initialize(void* pool, unsigned long pool_size,
                            unsigned long tcp_alloc, int max_concurrent);
}

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

HkTrampoline<void, void*, const void*> gameFrameworkInitializeHook =
    hk::hook::trampoline(
        [](void* thisPtr, const void* arg) -> void {
            SMBWAP_LOG_INFO("hook: GameFrameworkInitialize fire (pre)");

            // ImGui debug overlay: stamp the boot tick and (when the addon is
            // compiled in) build the NVN backend pre-orig, before the engine
            // fragments the heap / brings up NVN. This mirrors the load-bearing
            // SMO pre-orig ordering invariant. No-op without the addon today;
            // see docs/handoff-imgui-overlay.md for the two open RE items.
            smbwap::ui::initDebugConsole();

            // Bring up nn::socket BEFORE Orig so our pool wins the
            // one-shot Initialize race (per the M4 legacy comment:
            // first call wins; SMBW's later call lands on a no-op
            // disarm trampoline we'd install if we cared, but we
            // don't yet because SMBW's pattern + pool sizing has
            // historically been a no-op).
            constexpr ::size kSocketPoolSize = 0xC0000;  // 768 KiB
            alignas(0x1000) static unsigned char s_socket_pool[kSocketPoolSize];
            const unsigned int rc = nn::socket::Initialize(
                s_socket_pool, kSocketPoolSize, 0x4000, 0xe);
            SMBWAP_LOG_INFO("[net] nn::socket::Initialize rc=0x%x pool=%lu bytes",
                            rc,
                            static_cast<unsigned long>(kSocketPoolSize));

            gameFrameworkInitializeHook.orig(thisPtr, arg);

            // Spawn the LAN client worker thread post-Orig. The
            // worker itself does nifm bring-up + the reconnect loop.
            // Idempotent -- repeat calls no-op.
            smbwap::ap::ApClient::instance().start();

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
// Vtable offsets we care about. WonderSeedAwarded is the load-bearing
// one for AP -- every Wonder Seed pickup funnels through here and gets
// translated into a Switch->bridge enqueueNerveFire(WonderSeedAwarded).
// SceneTransition is the M3.8 gate latch -- on every fire we stamp the
// system tick so the bridge's drainInbound gates container writers for
// the next ~3 s (covers death, course entry/exit, world-map, palace,
// Poplin shop, post-Wonder-Seed cleanup -- every "container state may
// be mid-mutation" window).  The other vtables are observability-only.
constexpr ::ptr kVtableOff_WonderSeedAwarded = 0x3345728;
constexpr ::ptr kVtableOff_SceneTransition   = 0x33fd9a8;

HkTrampoline<void, void*> nerveActivateOnceHook = hk::hook::trampoline(
    [](void* nerve) -> void {
        // M4: drain inbound grants on the game thread. NerveActivateOnce
        // fires on every Nerve activation -- many per second in active
        // gameplay -- which makes it the natural high-frequency drain
        // anywhere the player is in a course. Drain is single-atomic-load
        // early-return when the ring is empty: essentially free.
        smbwap::ap::drainInbound();

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
                    // [cblk-diag] CANDIDATE #2b (actor-identity survey).
                    // A character block that is bumped transitions its Nerve
                    // state (idle -> bumped -> reappear), so the block actor's
                    // Nerve flows through this shared NerveActivateOnce helper.
                    // For each NEW distinct nerve vtable we dump a small window
                    // of the nerve object so the block-bump nerve's owner can
                    // be correlated offline (its vtable offset is the anchor
                    // we'd hook next).  Purely additive logging -- no behaviour
                    // change to the load-bearing drain path.  All reads are
                    // guarded (nerve != 0 is checked above).
                    auto* nb = reinterpret_cast<std::uint8_t*>(nerve);
                    SMBWAP_LOG_INFO(
                        "[cblk-diag] nerveVT vt_off=NSO+0x%llx nerve=%p "
                        "[+0x08]=0x%llx [+0x10]=0x%llx [+0x18]=0x%llx "
                        "[+0x20]=0x%llx",
                        static_cast<unsigned long long>(vt_off), nerve,
                        static_cast<unsigned long long>(
                            *reinterpret_cast<std::uint64_t*>(nb + 0x08)),
                        static_cast<unsigned long long>(
                            *reinterpret_cast<std::uint64_t*>(nb + 0x10)),
                        static_cast<unsigned long long>(
                            *reinterpret_cast<std::uint64_t*>(nb + 0x18)),
                        static_cast<unsigned long long>(
                            *reinterpret_cast<std::uint64_t*>(nb + 0x20)));
                }
            }
        }

        // Target match: WONDER_SEED_AWARDED. Fires exactly once when the
        // player collects the Wonder Seed at the end of the Wonder phase.
        // The bridge attributes via current_course (set by the most recent
        // course_in PlayReport). DeathLink classification of scene
        // transitions (death vs. controlled exit vs. cleanup) comes back
        // in Phase 2g commit 6 alongside synthKill.
        if (vt_off == kVtableOff_WonderSeedAwarded) {
            SMBWAP_LOG_INFO("WONDER_SEED_AWARDED: nerve=%p (fire #%d)",
                            nerve, s_fires);
            smbwap::ap::enqueueNerveFire(
                smbwap::ap::NerveKind::WonderSeedAwarded,
                static_cast<unsigned>(s_fires));
        }

        // M3.8 scene-transition latch + DEATH_DETECTED classification.
        // Stamping every fire of vtable 0x33fd9a8 covers death, course/
        // area entry+exit, world-map, palace, Poplin shop entry, post-
        // Wonder-Seed cleanup -- every "container state may be mid-
        // mutation" window.  Reader (probe::isInSceneTransitionWindow)
        // compares the latched tick against svc::getSystemTick() and
        // gates drainInbound's container writers if elapsed < ~3 s.
        //
        // For DeathLink: distinct nerve instances share vtable 0x33fd9a8
        // and the game routes them by purpose; the type enum lives at
        // +0x18 as a u64.  Death whitelist:
        //   * 0x00ff000600000004 -- Mario death (pit, enemy, etc).
        //   * 0x00ff003700000084 -- player-controlled exit (not death).
        //   * 0x00ff000f00000004 -- post-Wonder-Seed-grab cleanup
        //     (same low u32 as death; whole-u64 match required).
        // Conservative whitelist: only emit DEATH_DETECTED on exact
        // match; log + drop other values (covers world-map travel /
        // palace-clear / pause-quit / file-select etc).
        if (vt_off == kVtableOff_SceneTransition) {
            probe::latchSceneTransitionTick(hk::svc::getSystemTick());

            constexpr std::ptrdiff_t kDeathDiscriminator_Off = 0x18;
            constexpr std::uint64_t  kDeathDiscriminator_Val = 0x00ff000600000004ull;
            if (nerve) {
                const auto state_word =
                    *reinterpret_cast<const std::uint64_t*>(
                        reinterpret_cast<const std::uint8_t*>(nerve)
                        + kDeathDiscriminator_Off);
                if (state_word == kDeathDiscriminator_Val) {
                    if (probe::consumeSyntheticDeathThisFrame()) {
                        // Loop guard: synthKill just fired from an
                        // inbound DeathLink.  Suppress the outbound
                        // echo.
                        SMBWAP_LOG_INFO(
                            "DEATH_DETECTED suppressed (synthetic kill) "
                            "nerve=%p (fire #%d)", nerve, s_fires);
                    } else {
                        SMBWAP_LOG_INFO(
                            "DEATH_DETECTED: nerve=%p (fire #%d) "
                            "state=0x%016llx",
                            nerve, s_fires,
                            static_cast<unsigned long long>(state_word));
                        smbwap::ap::enqueueNerveFire(
                            smbwap::ap::NerveKind::DeathDetected,
                            static_cast<unsigned>(s_fires));
                    }
                } else {
                    // Other state_words cover controlled exit /
                    // Wonder-Seed cleanup / world-map travel /
                    // palace-clear / pause-quit / file-select etc, and
                    // we drop them rather than emit DEATH_DETECTED.
                    // BUT: a player session with zero outbound
                    // DeathLinks despite the player dying tells us
                    // either Mario never died OR our discriminator
                    // misses a real death path -- and the original
                    // silent-drop left no telemetry to tell the two
                    // apart.  Log each NEW distinct state_word at
                    // most once, capped at kMaxDistinctStates total,
                    // so a post-mortem can spot a death path that
                    // never matches kDeathDiscriminator_Val.  The
                    // Nerve callback is single-threaded (same scope
                    // as the non-atomic s_fires counter), so plain
                    // static state is safe.
                    constexpr std::size_t kMaxDistinctStates = 16;
                    static std::uint64_t s_seen_states[kMaxDistinctStates] = {};
                    static std::size_t s_seen_count = 0;
                    bool already_seen = false;
                    for (std::size_t i = 0; i < s_seen_count; ++i) {
                        if (s_seen_states[i] == state_word) {
                            already_seen = true;
                            break;
                        }
                    }
                    if (!already_seen && s_seen_count < kMaxDistinctStates) {
                        s_seen_states[s_seen_count] = state_word;
                        ++s_seen_count;
                        SMBWAP_LOG_INFO(
                            "scene_transition: new state=0x%016llx "
                            "(fire #%d; %zu distinct non-death states "
                            "seen this session)",
                            static_cast<unsigned long long>(state_word),
                            s_fires, s_seen_count);
                    }
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
        // Drain inbound grants on the game thread before the existing
        // course-clear work. Bridge classifies course clears via the
        // course_result PlayReport (not this Nerve), so no outbound
        // enqueue here; we just log.
        smbwap::ap::drainInbound();
        static int s_fires = 0;
        ++s_fires;
        SMBWAP_LOG_INFO("COURSE_CLEARED: nerve=%p (fire #%d)", nerve, s_fires);

        // One-shot smoke test removed 2026-05-29 -- the AP-driven
        // SetWonderSeedsAbsolute push (ApFrameBridge.cpp drainInbound
        // dispatch -> probe::setWonderSeedBitfieldAbsolute) supersedes
        // it.  probe::triggerWonderSeedSmokeTest remains in SeedTrace
        // for ad-hoc developer use.

        setCourseClearFlagExecuteHook.orig(nerve);
    });

// PlayerTickLatch @ NSO +0x00273868 -- function-entry trampoline on
// FUN_7100273868(long param_1, long param_2), the per-frame player tick
// function.  Replaces the abandoned inline hook at +0x2743BC -- the
// 5-instruction patch window there corrupted execution silently, even
// though hakkun's / exlaunch's relocator nominally handle cbz/b.le.
// Function-entry trampoline avoids the relocator entirely.
//
// Every frame we re-walk to the HP-bearing struct and refresh
// probe::g_live_base (the struct is re-allocated per scene, so a one-time
// latch goes stale and synthKill would write to freed memory).  The same
// call also retries any pending inbound DeathLink now that the player tick
// is running.  See probe/DeathLink.cpp for the dereference chain
// (replicates the game's own walk at +0x2743A0).
HkTrampoline<void, void*, void*> playerTickLatchHook = hk::hook::trampoline(
    [](void* param_1, void* param_2) -> void {
        probe::serviceDeathLink(param_1);
        // Drain inbound grants here too -- the per-frame player tick is the
        // only steady (~60 Hz) drain site.  The other drain sites
        // (nerveActivateOnceHook, course-clear, game-goal) are all Nerve-
        // driven, and Nerve activations are BURSTY: they fire on state
        // changes (powerups, transitions, enemy events) but are nearly
        // absent during calm gameplay.  An inbound Kill (DeathLink / gate
        // bounce) that arrives while the player is just standing/running
        // would otherwise sit unpopped in the ring until the next Nerve
        // happened to fire -- the observed "received Kill ... never drained
        // in this level" bug (2026-06-05).  drainInbound is single-atomic-
        // load cheap when the ring is empty and self-gates on isSaveLoaded
        // / isInSceneTransitionWindow, so calling it every frame is safe.
        // Runs AFTER serviceDeathLink (so g_live_base/g_live_player are
        // refreshed this frame -> synthKill's freshBase succeeds) and
        // BEFORE orig (so a freshly-applied HP=0 is seen by the death check
        // inside the tick this same frame).  Same game thread as the Nerve
        // drain sites, so the SPSC ring keeps a single consumer.
        smbwap::ap::drainInbound();
        // Advance the badge-shop-text arm's freshness clock (cheap atomic
        // increment) so a stale shop arm expires within a few frames.
        probe::badgeShopTextTick();
        playerTickLatchHook.orig(param_1, param_2);
        // NOTE(imgui-overlay): the overlay's per-frame draw is intentionally
        // NOT driven from here — PlayerTickLatch is a logic tick. It's driven
        // from the NVN present shim in ui/ImGuiNvnBootstrap.cpp (game-agnostic,
        // holds the live command buffer). See docs/handoff-imgui-overlay.md.
    });

// GameGoalReachedExecute @ NSO +0x0015b77a8.
//
// Slot 8 of the SetFlagEndDispMsgFirstVisitedWorldAfterClearedLastBoss
// Nerve's vtable — fires exactly once per save the first time the player
// defeats final Bowser. Phase 2c will translate this into an AP
// ClientStatus.CLIENT_GOAL StatusUpdate; for Phase 2b we just log.
HkTrampoline<void, void*> gameGoalReachedExecuteHook = hk::hook::trampoline(
    [](void* nerve) -> void {
        // Drain inbound grants -- same idiom as SetCourseClearFlagExecute.
        smbwap::ap::drainInbound();
        static int s_fires = 0;
        ++s_fires;
        SMBWAP_LOG_INFO("GAME_GOAL_REACHED: nerve=%p (fire #%d)", nerve, s_fires);
        // Outbound: tell the bridge a CLIENT_GOAL StatusUpdate is due.
        smbwap::ap::enqueueNerveFire(
            smbwap::ap::NerveKind::GameGoalReached,
            static_cast<unsigned>(s_fires));
        gameGoalReachedExecuteHook.orig(nerve);
    });

// GetDamageReactionPlayerNo body @ NSO +0x0168d428.
//
// Character-block sanity.  This is the execute body of the AINB AI-node
// that runs when a block-type actor processes a damage reaction; it maps
// the damage invoker to a local player slot 0-3.  It is a SHARED node
// (regular ? blocks, etc. route through it too) -- the actor filter to
// ObjectBlockClarityCharacter can't be read cleanly from this body, so we
// over-fire CHAR_BLOCK_HIT and let the PC bridge filter by the offline
// (course, charaType) char-block table (only real character-block pairs
// resolve to an AP location; everything else is dropped).
//
// HOOK SAFETY: the body's prologue is clean (sub sp / stp / str / stp /
// add -- verified at +0x168d428).  Do NOT hook the wrapper at +0x168d3c0:
// it has a `bl #0x168d428` at wrapper+0x10, inside the 20-byte patch
// window (trampoline-corruption gotcha).
//
// SLOT READBACK: the body computes the resolved slot (0=P1..3=P4, -1=none)
// into w1 and stores it via FUN_7100594ae0(node_ctx+0x30, slot), which
// writes `slot` to `(*(uintptr*)(node_ctx+0x30) & ~3)`.  So AFTER orig we
// read it back:  slot = *(int32*)((*(uintptr*)(node_ctx+0x30)) & ~3).
// We emit only when slot is a valid local-player index (0..3); -1 (no
// local-player invoker) is dropped, which keeps the ring quiet on
// environmental / enemy damage.  chara is shipped as -1 (the bridge
// resolves the hitting character from the course's chara_type_array);
// position is zeros in v1 (the body doesn't expose the owning actor
// transform).
//
// The body return type is opaque (it forwards FUN_7100594ae0's return,
// a node value), so we declare the trampoline returning void* and ignore
// the value.
HkTrampoline<void*, void*> getDamageReactionPlayerNoHook =
    hk::hook::trampoline(
    [](void* node_ctx) -> void* {
        void* ret = getDamageReactionPlayerNoHook.orig(node_ctx);

        // Read back the slot the body just stored (see SLOT READBACK).
        // Guard every dereference: a malformed node ctx must never crash.
        std::int32_t slot = -1;
        if (node_ctx) {
            const auto raw = *reinterpret_cast<std::uintptr_t*>(
                reinterpret_cast<std::uint8_t*>(node_ctx) + 0x30);
            const auto store = raw & ~static_cast<std::uintptr_t>(3);
            if (store) {
                slot = *reinterpret_cast<std::int32_t*>(store);
            }
        }

        // [cblk-diag] CANDIDATE #2a (negative-confirm / cheap).
        // This is the KNOWN-DEAD GetDamageReactionPlayerNo node body (it never
        // fired in solo across a full level -- see the smbwap-character-block-
        // sanity memory).  We keep it instrumented so a single play session
        // re-confirms whether it is *still* dead (zero lines) or whether some
        // path does reach it after all.  The dump is widened so that IF it
        // fires we capture the node ctx + its input-source word + the node
        // vtable, which we can correlate offline.  Monotonic
        // count.fetch_add(...) < N idiom (the fetch_sub(...) > 0 throttle
        // underflows -- known footgun).
        static std::atomic<unsigned> s_diag{0};
        const unsigned d = s_diag.fetch_add(1, std::memory_order_relaxed);
        if (d < 40) {
            std::uint64_t vt = 0, w08 = 0, w18 = 0, w20 = 0, src = 0;
            if (node_ctx) {
                auto* b = reinterpret_cast<std::uint8_t*>(node_ctx);
                vt  = *reinterpret_cast<std::uint64_t*>(b + 0x00);
                w08 = *reinterpret_cast<std::uint64_t*>(b + 0x08);
                w18 = *reinterpret_cast<std::uint32_t*>(b + 0x18);
                w20 = *reinterpret_cast<std::uint64_t*>(b + 0x20);
                // The body loads node_ctx+0x20, masks &~3, then derefs [ptr]
                // as the node input source (a key/pointer).  Chase it once,
                // guarded, so a malformed value can never crash.
                const auto p = w20 & ~static_cast<std::uintptr_t>(3);
                if (p) src = *reinterpret_cast<std::uint64_t*>(p);
            }
            SMBWAP_LOG_INFO(
                "[cblk-diag] dmgReactNode #%u: node=%p vt=0x%llx slot=%d "
                "[+0x08]=0x%llx [+0x18]=0x%llx [+0x20]=0x%llx src=0x%llx",
                d, node_ctx,
                static_cast<unsigned long long>(vt), slot,
                static_cast<unsigned long long>(w08),
                static_cast<unsigned long long>(w18),
                static_cast<unsigned long long>(w20),
                static_cast<unsigned long long>(src));
        }

        // Emit only for a real local-player slot (0..3).  The bridge
        // filters to ObjectBlockClarityCharacter by table; chara=-1 means
        // "resolve from the course's chara_type_array".  pos zeros in v1.
        if (slot >= 0 && slot <= 3) {
            smbwap::ap::enqueueCharBlockHit(
                static_cast<std::uint32_t>(slot), /*chara=*/-1,
                0.0f, 0.0f, 0.0f);
        }
        return ret;
    });

// =========================================================================
// [cblk-diag2] CANDIDATE #3 -- GENERIC ACTOR-PLACEMENT ENUMERATION
// =========================================================================
//
// GOAL (iteration 3 keystone): positively identify the character block at
// LEVEL LOAD by its gyaml identity, from a generic, guaranteed-firing,
// clean-prologue choke point -- without depending on the runtime-built
// actor registry (no static hash constant exists; see the
// smbwap-character-block-sanity memory).
//
// CHOKE POINT: InitActorPlacementInfo @ NSO +0x5815c.  This is the engine's
// per-placement BYML parser: for EVERY placed actor in a BancMapUnit it
// reads the placement node and fills a placement-info struct.  RE'd fields
// (Ghidra-confirmed from the decompile of +0x5815c):
//   x0 = param_1 = placement-info struct (caller-owned -- survives orig)
//   x1 = param_2 = the BYML node for this one placement
//   struct+0x28  = char* Gyaml  (the actor's gyaml CLASS NAME as a STRING --
//                  e.g. "ObjectBlockClarityCharacter")  <-- IDENTITY
//   struct+0x50  = Dynamic params node handle (per-instance params incl.
//                  PlayerCharaType / ChildActorSelectName)
//   struct+0x75  = u8 "valid" flag (set to 1 only when parse fully succeeds)
// It fires once per placed actor during the load burst, so dedup-by-name
// keeps the ring quiet (a level has a few hundred DISTINCT gyaml classes).
//
// WHY THIS IS THE RIGHT GENERIC PATH (validation):
//   * "Gyaml" string @ 0x7102925483 is xref'd by InitActorPlacementInfo
//     (named symbol) -- this is the field-key read in the parser.
//   * Reached via the generic wrapper FUN_71008e9514 (zero-init struct +
//     InitActorPlacementInfo) whose caller FUN_7101a3dcbc is the per-actor
//     placement build+create (it then calls the actor finalize FUN_710012c328
//     / FUN_710022d174).  Blocks are placed actors -> they pass here.
//   * Because the identity is carried as a STRING, we DON'T need the runtime
//     gyaml hash 0x66d64769 at all -- we strcmp the name directly.
//
// PROLOGUE SAFETY (Ghidra disasm of the TRUE entry 0x5815c):
//   sub sp / stp x29,x30 / str x21 / stp x20,x19 / add x29  -- all stack
//   stores, NO pc-relative insn in the first 20 bytes.  CLEAN.
//   (The Gyaml adrp lives at +0x5817c, well outside the patch window.)
//
// WHAT IT LOGS (grep token: cblk-diag2):
//   [cblk-diag2] actor #<n> gyaml="<name>" plc=<struct> dyn=<dynNode>
//   For the block specifically (name match) it also dumps a wider window so
//   iteration 4 can chase from the placement record to the created actor.
//
// All reads are guarded; logging is AFTER orig so the struct is populated;
// dedup-by-name caps the ring.  Behaviour-neutral.
HkTrampoline<void, void*, void*> initActorPlacementInfoHook =
    hk::hook::trampoline([](void* plc, void* node) {
        initActorPlacementInfoHook.orig(plc, node);
        if (!plc) return;
        auto* b = reinterpret_cast<std::uint8_t*>(plc);
        // Only consider fully-parsed placements (valid flag at +0x75).
        const std::uint8_t valid = b[0x75];
        if (!valid) return;
        const char* gyaml =
            *reinterpret_cast<char* const*>(b + 0x28);
        if (!gyaml) return;
        void* dyn = *reinterpret_cast<void**>(b + 0x50);

        // Dedup by gyaml-name pointer.  Placement nodes for the SAME gyaml
        // class share the interned name pointer (the parser stores the BYML
        // string-pool pointer), so pointer-identity dedup is sound and cheap
        // and avoids any per-frame strlen flood.  Fixed 256-slot seen-set;
        // a level has only a few hundred distinct gyaml classes.
        constexpr unsigned kSeenSlots = 256;
        static std::atomic<std::uintptr_t> s_seen[kSeenSlots] = {};
        static std::atomic<unsigned> s_seen_n{0};
        const auto key = reinterpret_cast<std::uintptr_t>(gyaml);
        const unsigned have = s_seen_n.load(std::memory_order_acquire);
        for (unsigned i = 0; i < have && i < kSeenSlots; ++i) {
            if (s_seen[i].load(std::memory_order_relaxed) == key) return;
        }
        const unsigned slot = s_seen_n.fetch_add(1, std::memory_order_acq_rel);
        if (slot >= kSeenSlots) return;  // ring full -> stop logging
        s_seen[slot].store(key, std::memory_order_release);

        // strcmp against the target gyaml without pulling in <cstring>:
        // compare byte-by-byte, bounded, against the literal.
        const char* want = "ObjectBlockClarityCharacter";
        bool isBlock = true;
        for (unsigned i = 0; i < 64; ++i) {
            if (gyaml[i] != want[i]) { isBlock = false; break; }
            if (want[i] == '\0') break;
        }

        SMBWAP_LOG_INFO(
            "[cblk-diag2] actor #%u gyaml=\"%s\" plc=%p dyn=%p%s",
            slot, gyaml, plc, dyn, isBlock ? "  <== BLOCK" : "");

        if (isBlock) {
            // Wider dump for the block placement record: the engine writes
            // the per-instance banc Hash and SRTHash into this struct (see
            // the InitActorPlacementInfo decompile: +0x30 key, +0x40
            // SRTHash, +0x48 GroupGUID).  These let iteration 4 correlate
            // the live placement vs charblock_table.json (banc Hash) and
            // chase to the created actor object / its vtable.
            SMBWAP_LOG_INFO(
                "[cblk-diag2] BLOCK plc=%p [+0x30]=0x%llx [+0x40]=0x%llx "
                "[+0x48]=0x%llx [+0x50dyn]=%p translate=(%f,%f,%f)",
                plc,
                static_cast<unsigned long long>(
                    *reinterpret_cast<std::uint64_t*>(b + 0x30)),
                static_cast<unsigned long long>(
                    *reinterpret_cast<std::uint64_t*>(b + 0x40)),
                static_cast<unsigned long long>(
                    *reinterpret_cast<std::uint64_t*>(b + 0x48)),
                dyn,
                *reinterpret_cast<float*>(b + 0x00),
                *reinterpret_cast<float*>(b + 0x04),
                *reinterpret_cast<float*>(b + 0x08));
        }
    });

// =========================================================================
// [cblk-diag3] CANDIDATE #4 -- GENERIC ACTOR-CREATE DISPATCH + VTABLE RESOLVE
// =========================================================================
//
// GOAL (iteration 4 keystone): from the confirmed placement IDENTITY
// (diag2), capture the CONCRETE per-actor "create" function and the
// PlayerCharaType for the character block, so iteration 5 can hook the
// create fn directly (-> live actor `this` + vtable -> block-filtered HIT
// hook).  The block actor is data/registry-driven: there is NO clean,
// static, block-specific create/init/hit hook anchorable from the NSO
// (re-confirmed this round -- every clarity/reaction/block symbol lives in
// an AINB reflection/registration table with only DATA xrefs, and the banc
// placement tree never exposes the actor C++ object).  But the GENERIC
// actor-create DISPATCHER is clean and name-filterable:
//
// CHOKE POINT: FUN_71002ceac0 @ NSO +0x2ceac0.  The engine's per-actor
// create dispatcher.  Ghidra-confirmed:
//   x0=param_1, x1=param_2 (status out), x2=param_3 (placement BYML node),
//   x3=param_4 (-> *param_4 = the gyaml CLASS-NAME C-string)  <-- IDENTITY,
//   x4=param_5.
// It resolves the "engine__actor__ActorParam" handle for *param_4, checks a
// category bitfield, then -- on match -- calls the actor-system MANAGER's
// create method:  plVar8 = *(DAT_710361f8b0+0x30) (fallback +0x38);
//                 (**(code**)(*plVar8 + 0x50))(plVar8, node, name, &buf, p5);
// That vt[0x50] call instantiates the block actor.  Its address is a
// RUNTIME vtable slot, so we resolve it live INSIDE the hook (mgr=*(g+0x30);
// vt=*mgr; createFn=*(vt+0x50)) and LOG it: createFn is the concrete
// NSO-relative create fn iteration 5 hooks to grab `*(void**)actor`.
//
// PROLOGUE SAFETY (Ghidra disasm of 0x2ceac0):
//   sub sp,sp,#0x1c0 / stp x29,x30 / stp x28,x27 / stp x26,x25 / ...
//   -- all stack stores, NO pc-relative insn in the first 20 bytes.  CLEAN.
//
// PlayerCharaType: read from the placement node (param_3) via the engine's
// own BYML getters -- getSubNode(node,&dyn,"Dynamic") @ +0x583e4 then
// getInt(&dyn,&chara,"PlayerCharaType") @ +0x12dd08.  All guarded.
//
// WHAT IT LOGS (grep token: cblk-diag3):
//   [cblk-diag3] create gyaml="<name>"  (only the block; dedup-by-name)
//   [cblk-diag3] BLOCK chara=<n> mgr=<p> createVt=<p> createFn=NSO+0x<off>
// Behaviour-neutral: orig() is always called; all reads null-guarded.
//
// NSO-relative game-fn pointers (resolved off the live main-module base):
namespace cblk {
    // BYML getSubNode(parentNode, out16, "Key") -> bool, out16 is a
    // 16-byte node handle (ptr@+0, off@+8, type-tag@+0xc).
    using GetSubNodeFn = bool (*)(void* /*node*/, void* /*out16*/,
                                  const char* /*key*/);
    // BYML getInt(node16, &out, "Key") -> 1 if present & int-typed.
    using GetIntFn = unsigned char (*)(void* /*node16*/, unsigned int* /*out*/,
                                       const char* /*key*/);
    constexpr ::ptr kOffGetSubNode = 0x000583e4;
    constexpr ::ptr kOffGetInt     = 0x0012dd08;
    constexpr ::ptr kDatActorMgr   = 0x00361f8b0;  // DAT_710361f8b0 (.bss)
}

// NOTE: the real function returns a status word in x0 (the caller branches on
// `result & 1` to decide actor creation); the trampoline MUST declare the same
// return type and forward orig()'s result, or it would clobber x0 and break
// actor creation.  Every early-out returns `ret`.
HkTrampoline<unsigned long, void*, void**, void*, void**, void*>
    actorCreateDispatchHook = hk::hook::trampoline(
        [](void* a1, void** a2, void* node, void** namePtr, void* a5)
            -> unsigned long {
            const unsigned long ret =
                actorCreateDispatchHook.orig(a1, a2, node, namePtr, a5);

            if (!namePtr) return ret;
            const char* gyaml = reinterpret_cast<const char*>(*namePtr);
            if (!gyaml) return ret;

            // strcmp against the target gyaml, bounded, no <cstring>.
            const char* want = "ObjectBlockClarityCharacter";
            for (unsigned i = 0; i < 64; ++i) {
                if (gyaml[i] != want[i]) return ret;
                if (want[i] == '\0') break;
            }

            // Dedup: log at most a few block-create lines per boot.
            static std::atomic<unsigned> s_n{0};
            const unsigned n = s_n.fetch_add(1, std::memory_order_relaxed);
            if (n >= 16) return ret;

            const auto* main_mod = hk::ro::getMainModule();
            if (!main_mod) return ret;
            const ::ptr base = main_mod->range().start();

            // PlayerCharaType from the placement node's Dynamic sub-node.
            int chara = -1;
            if (node) {
                auto getSub = reinterpret_cast<cblk::GetSubNodeFn>(
                    base + cblk::kOffGetSubNode);
                auto getInt = reinterpret_cast<cblk::GetIntFn>(
                    base + cblk::kOffGetInt);
                std::uint8_t dyn[16] = {};
                if (getSub(node, dyn, "Dynamic")) {
                    unsigned int v = 0;
                    if (getInt(dyn, &v, "PlayerCharaType")) {
                        chara = static_cast<int>(v);
                    }
                }
            }

            // Resolve the concrete create fn = actor-manager vt[0x50].
            //
            // FIX (iter 5): the iter-4 build read `g[6]`/`g[7]` off a pointer
            // TO the DAT slot -- i.e. memory at (DAT_address + 0x30), never
            // dereferencing DAT to reach the root object first -- so the
            // logged createFn was garbage.  The decompile of FUN_71002ceac0
            // is unambiguous:  root = *(void**)(base + 0x361f8b0);
            //   mgrA = *(void**)(root + 0x30);  mgrB = *(void**)(root + 0x38);
            //   createFn = (**(code**)(*mgr + 0x50)).
            // Path A is taken when (*(byte*)(mgrA+0x849) & 1) and its create
            // returns &1; otherwise the game falls through to path B.  We
            // replicate the SAME selection so the logged offset is exactly the
            // method the engine dispatched to for THIS block.
            void* root = nullptr;
            void* mgrA = nullptr;
            void* mgrB = nullptr;
            void* mgr = nullptr;           // the path actually selected
            char  path = '?';
            void* createVt = nullptr;
            ::ptr createOff = 0;
            std::uint32_t prologue[4] = {0, 0, 0, 0};
            {
                root = *reinterpret_cast<void**>(base + cblk::kDatActorMgr);
                if (root) {
                    auto* rb = reinterpret_cast<std::uint8_t*>(root);
                    mgrA = *reinterpret_cast<void**>(rb + 0x30);
                    mgrB = *reinterpret_cast<void**>(rb + 0x38);
                }
                // Path-A enable gate per the decompile: (*(byte*)(m+0x849)&1).
                auto enabled = [](void* m) -> bool {
                    if (!m) return false;
                    const std::uint8_t f =
                        *reinterpret_cast<std::uint8_t*>(
                            reinterpret_cast<std::uint8_t*>(m) + 0x849);
                    return (f & 1u) != 0u;
                };
                if (enabled(mgrA))      { mgr = mgrA; path = 'A'; }
                else if (enabled(mgrB)) { mgr = mgrB; path = 'B'; }
                else if (mgrA)          { mgr = mgrA; path = 'a'; }  // fallback
                else if (mgrB)          { mgr = mgrB; path = 'b'; }
                if (mgr) {
                    void** vt = *reinterpret_cast<void***>(mgr);
                    createVt = vt;
                    if (vt) {
                        void* fn = vt[10];           // slot 0x50 / 8
                        const auto fa = reinterpret_cast<::ptr>(fn);
                        if (fa >= base) {
                            createOff = fa - base;
                            // Capture the create method's first 4 instructions
                            // so the NEXT iteration can confirm a CLEAN prologue
                            // (no adrp/ldr-literal/b/bl in the patch window)
                            // before hooking it -- no extra capture round-trip.
                            auto* ins = reinterpret_cast<std::uint32_t*>(fn);
                            for (int i = 0; i < 4; ++i) prologue[i] = ins[i];
                        }
                    }
                }
            }

            SMBWAP_LOG_INFO("[cblk-diag3] create gyaml=\"%s\"", gyaml);
            SMBWAP_LOG_INFO(
                "[cblk-diag3] BLOCK chara=%d node=%p root=%p mgrA=%p mgrB=%p "
                "path=%c mgr=%p createVt=%p createFn=NSO+0x%llx",
                chara, node, root, mgrA, mgrB, path, mgr, createVt,
                static_cast<unsigned long long>(createOff));
            SMBWAP_LOG_INFO(
                "[cblk-diag3] createFn prologue: %08x %08x %08x %08x",
                prologue[0], prologue[1], prologue[2], prologue[3]);
            return ret;
        });

// =========================================================================
// PLAYREPORT (Phase 2c)
// =========================================================================
//
// PlayReport is the SDK's telemetry/event API. Games emit reports during
// many gameplay moments (course-in, course-result, world-result, etc.);
// SMBW's PlayReport stream is the bridge's primary signal for "which
// course is the player in" and per-course classification. M2.4-M2.6 reverse-
// engineered the wire format end-to-end; the bridge handles the decode.
//
// We hook 4 mangled C++ symbols. Resolution path under HK_DISABLE_SAIL is
// hk::ro::lookupSymbol(literal) at install time. If the symbol is missing
// (firmware mismatch, SDK rebuild), installAtSym returns a failed Result
// rather than aborting — installHook() then logs the failure but lets the
// rest of hkMain proceed.
//
// CRITICAL gotcha (CLAUDE.md #3): hooking *any* PlayReport member function
// beyond ctor + SetEventId crashes the game via a delayed SDK validator
// abort on a different thread. The workaround is to drop below the
// PlayReport class to the IPC client layer (CmifProxyImpl<IPrepoService>::
// _nn_sf_sync_SaveReport[WithUser]) which sees the report already
// serialized and is below the audited state. Phase 2c installs:
//   * PlayReportCtor (safe per the gotcha)
//   * PlayReportSetEventId (safe per the gotcha)
//   * PrepoIpcSaveReport (IPC layer)
//   * PrepoIpcSaveReportWithUser (IPC layer)
// The Save / SaveUid / Add* PlayReport methods are NOT installed.

// 16-byte {ptr, size} struct that the IPC layer passes by const&. AAPCS
// flattens this onto the stack and our trampoline receives a pointer.
struct PrepoInArrayChar { const char* ptr; ::size size; };
struct PrepoInBuffer    { const void* ptr; ::size size; };

// Hex-dump the IPC payload across multiple log lines so a single ~355-byte
// report fits in the ring without truncation.  Direct port of the legacy
// helper.  kChunk=128 -> ~384 hex chars per line, well under the 512-byte
// log buffer cap.
void smbwapLogPayloadHex(const void* buf, ::size total_size) {
    if (!buf || total_size == 0) return;
    constexpr ::size kChunk = 128;
    constexpr ::size kMax   = 4096;
    const ::size to_dump = total_size < kMax ? total_size : kMax;
    const unsigned char* p = static_cast<const unsigned char*>(buf);
    static constexpr char kHex[] = "0123456789abcdef";
    char line[3 * kChunk + 1];
    for (::size off = 0; off < to_dump; off += kChunk) {
        const ::size n = (to_dump - off < kChunk) ? to_dump - off : kChunk;
        for (::size i = 0; i < n; ++i) {
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

// nn::prepo::PlayReport ctor with-event-id. Rarely used by SMBW (game uses
// no-arg ctor + SetEventId), but the M2.4 bisect found it safe to hook.
HkTrampoline<void, void*, const char*> playReportCtorHook = hk::hook::trampoline(
    [](void* thisPtr, const char* event_id) -> void {
        SMBWAP_LOG_INFO("prepo.ctor this=%p event=%s",
                        thisPtr, event_id ? event_id : "(null)");
        playReportCtorHook.orig(thisPtr, event_id);
    });

// nn::prepo::PlayReport::SetEventId — the post-construction event-name
// setter. SMBW takes the no-arg-ctor-then-SetEventId path, so this is the
// hook that actually reveals which "room" each report is for.
HkTrampoline<unsigned, void*, const char*> playReportSetEventIdHook =
    hk::hook::trampoline(
        [](void* thisPtr, const char* event_id) -> unsigned {
            SMBWAP_LOG_INFO("prepo.set_event this=%p event=%s",
                            thisPtr, event_id ? event_id : "(null)");
            return playReportSetEventIdHook.orig(thisPtr, event_id);
        });

// IPC client layer below PlayReport. The huge mangled name encodes the
// CmifProxyImpl<IPrepoService, ...> template instantiation chain plus the
// _nn_sf_sync_SaveReport member signature.
// Build a null-terminated room buffer + push to the bridge. Truncates
// to kRoomCap - 1 chars. Shared by both IPC hook callbacks.
void enqueuePlayReportFromIpc(const PrepoInArrayChar* room,
                              const void* pay_ptr,
                              ::size pay_size) {
    char room_buf[smbwap::ap::kRoomCap];
    const ::size room_len = room ? room->size : 0;
    const ::size take = room_len < sizeof(room_buf) - 1
        ? room_len : sizeof(room_buf) - 1;
    if (room && room->ptr) {
        for (::size i = 0; i < take; ++i) room_buf[i] = room->ptr[i];
    }
    room_buf[take] = '\0';
    smbwap::ap::enqueuePlayReport(room_buf, pay_ptr, pay_size);
}

// 2026-05-27 save-event queue-depth correlation.  Bracket Orig() of the
// prepo IPC save-report hook with a depth snapshot of all three
// GameDataMgr dirty queues so we can see whether prepo events drain the
// rings.
//
// First-session result (Ryujinx_1.3.3_2026-05-27_17-00-00.log, 6 min of
// active play): all 18 brackets showed qA=0->0  qA2=0->0  qB=0->0.
// Prepo IPC events are NOT a drain trigger.  Drain happens via some
// other code path (probably a per-frame or per-area-transition flusher),
// running fast enough that depth returns to 0 between RUN samples 30+ s
// apart.  Downgraded to DEBUG so the bracket is still available if we
// ever need to look again (e.g. an unknown future code path starts
// accumulating) but doesn't spam INFO output.
static void logQueueDepthPrePost(const char* label,
                                  const probe::QueueDepth& qa_pre,
                                  const probe::QueueDepth& qas_pre,
                                  const probe::QueueDepth& qb_pre) {
    const auto qa_post  = probe::readQueueA_primary();
    const auto qas_post = probe::readQueueA_secondary();
    const auto qb_post  = probe::readQueueB();
    SMBWAP_LOG_DEBUG(
        "[gmd] %s queue depth pre->post: "
        "qA=%u->%u/%u  qA2=%u->%u/%u  qB=%u->%u/%u",
        label,
        qa_pre.depth(),  qa_post.depth(),  qa_post.cap,
        qas_pre.depth(), qas_post.depth(), qas_post.cap,
        qb_pre.depth(),  qb_post.depth(),  qb_post.cap);
}

HkTrampoline<unsigned, void*, const PrepoInArrayChar*, const PrepoInBuffer*,
             unsigned long>
    prepoIpcSaveReportHook = hk::hook::trampoline(
        [](void* thisPtr, const PrepoInArrayChar* room,
           const PrepoInBuffer* payload, unsigned long flags) -> unsigned {
            const char* room_ptr =
                (room && room->ptr) ? room->ptr : "(null)";
            const ::size room_len = room ? room->size : 0;
            const void* pay_ptr = payload ? payload->ptr : nullptr;
            const ::size pay_size = payload ? payload->size : 0;
            SMBWAP_LOG_INFO(
                "prepo.ipc.save this=%p room=%.*s pay=%p size=%zu flags=0x%lx",
                thisPtr, static_cast<int>(room_len), room_ptr,
                pay_ptr, pay_size, flags);
            smbwapLogPayloadHex(pay_ptr, pay_size);
            // Forward to the bridge BEFORE Orig so a hypothetical
            // Orig-aborting path still gets the event recorded.
            enqueuePlayReportFromIpc(room, pay_ptr, pay_size);
            const auto qa_pre  = probe::readQueueA_primary();
            const auto qas_pre = probe::readQueueA_secondary();
            const auto qb_pre  = probe::readQueueB();
            const auto ret = prepoIpcSaveReportHook.orig(
                thisPtr, room, payload, flags);
            logQueueDepthPrePost("prepo.ipc.save", qa_pre, qas_pre, qb_pre);
            return ret;
        });

HkTrampoline<unsigned, void*, const void*, const PrepoInArrayChar*,
             const PrepoInBuffer*, unsigned long>
    prepoIpcSaveReportWithUserHook = hk::hook::trampoline(
        [](void* thisPtr, const void* uid, const PrepoInArrayChar* room,
           const PrepoInBuffer* payload, unsigned long flags) -> unsigned {
            const char* room_ptr =
                (room && room->ptr) ? room->ptr : "(null)";
            const ::size room_len = room ? room->size : 0;
            const void* pay_ptr = payload ? payload->ptr : nullptr;
            const ::size pay_size = payload ? payload->size : 0;
            SMBWAP_LOG_INFO(
                "prepo.ipc.save_uid this=%p uid=%p room=%.*s pay=%p size=%zu flags=0x%lx",
                thisPtr, uid, static_cast<int>(room_len), room_ptr,
                pay_ptr, pay_size, flags);
            smbwapLogPayloadHex(pay_ptr, pay_size);
            // Bridge treats either IPC variant identically; the uid is
            // irrelevant for AP routing.
            enqueuePlayReportFromIpc(room, pay_ptr, pay_size);
            const auto qa_pre  = probe::readQueueA_primary();
            const auto qas_pre = probe::readQueueA_secondary();
            const auto qb_pre  = probe::readQueueB();
            const auto ret = prepoIpcSaveReportWithUserHook.orig(
                thisPtr, uid, room, payload, flags);
            logQueueDepthPrePost("prepo.ipc.save_uid",
                                  qa_pre, qas_pre, qb_pre);
            return ret;
        });

// =========================================================================
// GRANTS (Phase 2d)
// =========================================================================
//
// GameDataMgr (gmd::) container writers identified by the M3 static-analysis
// sprint as the universal save-grant primitives. These are the same offsets
// the legacy `probe::grantContainerACounter` / `probe::grantContainerBBool`
// call from the bridge -- but Phase 2d hooks them only to OBSERVE the game's
// own write activity (and any bridge-driven grants once the ap/ subsystem
// returns in Phase 2f).
//
// Hook bodies are minimal in this PR (log hash + value + this-pointer +
// chain Orig). The legacy callbacks layered three things on top -- they
// also return in later phases:
//   * `probe::markSaveLoaded(...)` -- the M4.5 save-loaded latch that
//      ungates inbound grant replay.
//   * `probe::dumpContainerCDiff(...)` -- diff logging for the badge bitmap
//      that surfaced the container-C layout during M3.2.
//   * The Wonder Seed counter override that interceps writes to the 5
//      mirror hashes and substitutes the AP-authoritative count.
// All three depend on `probe::` state + the ap/ subsystem; both come back
// when PROBES (Phase 2e) and bridge (Phase 2f) restore.
//
// Signatures from the M3 sprint (docs/static-analysis-findings.md):
//   * FUN_710049F648 = void(GameDataMgr*, uint32_t value, uint32_t hash)
//   * FUN_7101F263FC = u64(GameDataMgr+8 substruct, u8 value & 1, uint32_t hash)
// gmd is a `void*` here -- we don't need the typed struct yet.

// GmdContainerAWriter @ NSO +0x0049F648 -- container-A counter SET.
// Universal counter writer for flower_coin, regular_coin, etc. Lock-free
// (uses ARM exclusive-monitor atomics on gmd->[+0xf8]). Deferred-write --
// value queued, applied to persistent container at next save.
HkTrampoline<void, void*, unsigned, unsigned> gmdContainerAWriterHook =
    hk::hook::trampoline(
        [](void* gmd, unsigned value, unsigned hash) -> void {
            // M4.5: latch the save-loaded gate BEFORE the bounded log so
            // a fast save-load burst (50+ writes in tight succession)
            // can never skip past the latch.  The save deserializer is
            // by induction the FIRST caller of this writer because
            // drainInbound never produces a writer call until
            // probe::isSaveLoaded() is already true.
            probe::markSaveLoaded("gmd.A_writer");

            // Bound the log so save-load deserialization doesn't fill the
            // ring. Save load fires dozens of these per slot.
            static int s_fires = 0;
            ++s_fires;
            if (s_fires <= 50 || (s_fires & 0xFF) == 0) {
                SMBWAP_LOG_INFO(
                    "gmd.A_writer hash=0x%08x value=%u gmd=%p (fire #%d)",
                    hash, value, gmd, s_fires);
            }
            gmdContainerAWriterHook.orig(gmd, value, hash);
        });

// GmdBoolWriter @ NSO +0x01F263FC -- container-B bool deferred-write
// delegate. Called by FUN_710049EA24 (high-level wrapper, NSO +0x49EA24)
// which gates on gmd+0x68 init/lock and delegates to this function with
// `substruct = gmd + 8`. Used for Royal Seeds + COMPLETE_GAME + INTRO.
HkTrampoline<unsigned long long, void*, unsigned char, unsigned>
    gmdBoolWriterHook = hk::hook::trampoline(
        [](void* substruct, unsigned char value, unsigned hash)
            -> unsigned long long {
            // Same M4.5 latch idiom as gmdContainerAWriterHook -- in
            // case the bool half of the save deserializer beats the
            // counter half (e.g., if save load touches a container-B
            // field before any container-A counter).
            probe::markSaveLoaded("gmd.bool_writer");

            static int s_fires = 0;
            ++s_fires;
            if (s_fires <= 50 || (s_fires & 0xFF) == 0) {
                SMBWAP_LOG_INFO(
                    "gmd.bool_writer hash=0x%08x value=%u substruct=%p (fire #%d)",
                    hash, value, substruct, s_fires);
            }
            return gmdBoolWriterHook.orig(substruct, value, hash);
        });

// =========================================================================
// WONDER SEED GATE OVERRIDE (reader-side substitution)
// =========================================================================
//
// ContainerAReader @ NSO +0x0012AE94 -- container-A counter GET function.
// Signature: `uint64_t FUN_710012AE94(GameDataMgr*, uint32_t* out, uint32_t hash)`.
// Pure: decompile shows no internal mutex, no LRU update, no side effects.
//
// AP-authoritative Wonder Seed gate override.  drainInbound caches the
// 8 per-world counts in g_wonder_seed_counts[]; this trampoline substitutes
// the AP value into *out_value for the 3 mirror hashes the gate predicate
// and HUD read from.  No proactive container write -- the writer-side
// pushWonderSeedOverride approach raced with FUN_710049F750 during scene
// transitions (PR #40 crash, never fully fixable).
//
// The 5 mirror hashes:
//   0x21f89ab1, 0x8c20ccb7, 0xeeff353b, 0x390eb960, 0xa0e5f253
// Only the first 3 are substituted (BISECT-C, 2026-05-26 in the retired
// exlaunch code at src/program/main.cpp:2362-2530).  Substituting all 5
// crashed -- 0xeeff353b and 0xa0e5f253 appear to feed seed-gain animation
// interpolation, and a Δ>1 OOBs an animation slot array.
//
//   0x390eb960 -- gate hash, read by FUN_71001787b40
//   0x21f89ab1 -- in-course HUD reads from this group
//   0x8c20ccb7 -- BISECT-C safe; wildcard from the same group
HkTrampoline<unsigned long long, long, unsigned*, unsigned>
    containerAReaderHook = hk::hook::trampoline(
        [](long gmd, unsigned* out_value, unsigned hash)
            -> unsigned long long {
            const unsigned long long result =
                containerAReaderHook.orig(gmd, out_value, hash);

            static constexpr unsigned kSafeWsSubstituteHashes[3] = {
                0x390eb960u,  // gate hash (read by FUN_71001787b40)
                0x21f89ab1u,  // in-course HUD
                0x8c20ccb7u,  // BISECT-C safe wildcard
                // 0xeeff353b / 0xa0e5f253 -- DO NOT substitute (animation
                // source/target buffers; substituting crashed in ATTEMPT 0).
            };
            bool is_substitute_target = false;
            for (unsigned h : kSafeWsSubstituteHashes) {
                if (h == hash) { is_substitute_target = true; break; }
            }
            if (out_value != nullptr && gmd != 0 && is_substitute_target
                && probe::isSaveLoaded()
                && !probe::isInSceneTransitionWindow()) {
                // Recursive Orig() bypasses our trampoline -- the reader is
                // pure so this is recursion-safe.  Fetches the live current
                // world index from container-A hash 0x9f5ead3c.
                unsigned world_val = 0;
                containerAReaderHook.orig(gmd, &world_val, 0x9f5ead3cu);
                // In-game world index -> AP bucket.  In-game order is
                // 1=W1, 2=Petal Isles, 3=W2..7=W6, 8="Castle" (Bowser;
                // not a player overworld), 9=Special.  AP buckets are
                // 0=W1..5=W6, 6=Petal Isles, 7=Special.  Corrected
                // 2026-05-28 from val=8 for Special after a live
                // PlayReport showed world_no=9 on Special World course_in
                // (matches .rodata internal-name table "Himitu"=9).
                static constexpr signed char kWorldValToBucket[10] = {
                    -1, 0, 6, 1, 2, 3, 4, 5, -1, 7,
                };
                if (world_val >= 1 && world_val <= 9
                    && kWorldValToBucket[world_val] >= 0) {
                    const unsigned bucket =
                        static_cast<unsigned>(kWorldValToBucket[world_val]);
                    const unsigned ap_count =
                        smbwap::ap::getWonderSeedCount(bucket);
                    const unsigned game_value = *out_value;
                    if (game_value != ap_count) {
                        // Signed counter so the budget actually caps -- the
                        // earlier `std::atomic<unsigned>` wrapped to UINT_MAX
                        // after 32 decrements and kept logging for billions
                        // of calls (300 MB session log, 215K of these
                        // lines).  Saturating-stop at 0 with CAS.
                        static std::atomic<int32_t> sub_log_budget{32};
                        int32_t b = sub_log_budget.load(
                            std::memory_order_relaxed);
                        while (b > 0 && !sub_log_budget.compare_exchange_weak(
                                b, b - 1, std::memory_order_relaxed)) {
                        }
                        if (b > 0) {
                            SMBWAP_LOG_INFO(
                                "WS read substitute hash=0x%08x world=%u "
                                "bucket=%u game_value=%u -> ap_count=%u",
                                hash, world_val, bucket,
                                game_value, ap_count);
                        }
                        *out_value = ap_count;
                    }
                }
            }
            return result;
        });

// =========================================================================
// OPEN-WORLD COURSE-NODE VISIBILITY OVERRIDE
// =========================================================================
//
// FUN_7100935c80 @ NSO +0x935c80 -- per-course-node visible predicate.
// `bool FUN(long actor)`.  Called by FUN_710179f100 for each course node
// actor on the world map to decide whether to show it.  First gate:
//
//   ldrb w8,[x0, #0x8d]   // load actor->initialized_byte
//   cbnz w8, success      // non-zero → proceed to lock+gmd checks
//   mov w0, wzr; ret      // zero → return false (hidden)
//
// For W1 the +0x8d byte is set at game start (W1 is always initialized).
// For W2-W6 it is set only when the player FIRST visits that world via
// normal progression.  On a fresh open-world save the player fast-travels
// to W2 before ever visiting it, so +0x8d stays 0 and all W2 courses
// remain invisible despite the WorldRoutable hook making W2 routable.
//
// Fix: when the AP routable mask is non-zero (open-world active) AND the
// actor's +0x8d byte is 0 (world never visited), force return true.  The
// original is still called for already-initialized actors (normal path).
// Access control for palace / Bowser / badge courses is handled by the
// level-entry death-gate hook; forcing visibility here is safe.
//
// PROLOGUE SAFETY: +0x935c80 is `sub sp / stp x29,x30 / add x29 / ldrb`.
// No PC-relative instruction in first 4; (0x935c80+8)&7==0 → count=4.
HkTrampoline<bool, long long> courseVisibleHook = hk::hook::trampoline(
    [](long long param_1) -> bool {
        const std::uint8_t flag_8d =
            *reinterpret_cast<const std::uint8_t*>(
                static_cast<std::uintptr_t>(param_1) + 0x8d);
        const bool orig_result = courseVisibleHook.orig(param_1);

        // In vanilla mode, always pass through.
        if (!probe::isSaveLoaded()) return orig_result;
        const std::uint32_t mask = smbwap::ap::getRoutableWorldMask();
        if (mask == 0) return orig_result;

        // Diagnostic: log every call when open-world is active so we can
        // see whether the hook fires for W2 course nodes and what state
        // they're in.  Budget-limited to avoid flooding.
        static std::atomic<std::int32_t> diag_budget{64};
        std::int32_t d = diag_budget.load(std::memory_order_relaxed);
        while (d > 0 && !diag_budget.compare_exchange_weak(
                   d, d - 1, std::memory_order_relaxed)) {
        }
        if (d > 0) {
            SMBWAP_LOG_INFO(
                "CourseVisible called: actor=0x%llx flag_8d=%u orig=%d "
                "mask=0x%03x",
                static_cast<unsigned long long>(param_1),
                static_cast<unsigned>(flag_8d),
                orig_result ? 1 : 0, mask);
        }

        // Open-world active: force ALL course nodes visible regardless of
        // flag_8d or gmd bool.  This surfaces W2 courses even when the world
        // has never been visited.  The death-gate hook handles access control
        // for badge/Bowser courses; we bypass only the visibility predicate.
        if (!orig_result) {
            static std::atomic<std::int32_t> force_budget{32};
            std::int32_t b = force_budget.load(std::memory_order_relaxed);
            while (b > 0 && !force_budget.compare_exchange_weak(
                       b, b - 1, std::memory_order_relaxed)) {
            }
            if (b > 0) {
                SMBWAP_LOG_INFO(
                    "CourseVisible force-true actor=0x%llx flag_8d=%u "
                    "mask=0x%03x",
                    static_cast<unsigned long long>(param_1),
                    static_cast<unsigned>(flag_8d), mask);
            }
            return true;
        }
        return orig_result;
    });

// =========================================================================
// OPEN-WORLD TRAVEL-CONFIRMATION OVERRIDE
// =========================================================================
//
// FUN_710055ed80 @ NSO +0x55ed80 -- travel-destination availability check.
// `bool FUN(int world_val)`.  Called by the travel UI (FUN_7101c235c4) to
// decide whether any courses are selectable as destinations in the chosen
// world.  It does an identical per-world-record lookup as FUN_7100935ce0
// (gmd+0x80 table, vtable[+0x20] count check, *(record+0x28) & 1) so it
// returns false for any world that has never been visited.
//
// Without this override the travel tab shows W2 (worldRoutableHook) but
// clicking W2 shows "L_Btn-T_World_NoOpen" (no courses available), and the
// player cannot confirm travel.  We force true for masked worlds so the
// travel completes; the player is placed at W2's default entry point and
// the world map loads, at which point courseVisibleHook makes the course
// nodes appear.
//
// PROLOGUE SAFETY: first 4 instrs are `sub/stp/str/stp` -- no PC-relative.
// adrp is at +0x1c (instruction 7), well past the 4-instruction patch.
// (0x55ed80+8)&7==0 → count=4.
HkTrampoline<bool, int> worldTravelHook = hk::hook::trampoline(
    [](int world_val) -> bool {
        const bool orig_result = worldTravelHook.orig(world_val);
        if (!probe::isSaveLoaded()) return orig_result;
        const std::uint32_t mask = smbwap::ap::getRoutableWorldMask();
        if (mask == 0) return orig_result;

        static constexpr signed char kWorldValToBucket[10] = {
            -1, 0, 6, 1, 2, 3, 4, 5, -1, 7,
        };
        const unsigned uval = static_cast<unsigned>(world_val);
        int bit = -1;
        if (uval == 8) {
            bit = static_cast<int>(smbwap::ap::kCastleMaskBit);
        } else if (uval <= 9 && kWorldValToBucket[uval] >= 0) {
            bit = kWorldValToBucket[uval];
        }
        if (bit >= 0 && (mask & (1u << bit))) {
            static std::atomic<std::int32_t> log_budget{16};
            std::int32_t b = log_budget.load(std::memory_order_relaxed);
            while (b > 0 && !log_budget.compare_exchange_weak(
                       b, b - 1, std::memory_order_relaxed)) {
            }
            if (b > 0) {
                SMBWAP_LOG_INFO(
                    "WorldTravel force-true world_val=%d bit=%d orig=%d "
                    "mask=0x%03x",
                    world_val, bit, orig_result ? 1 : 0, mask);
            }
            return true;
        }
        return orig_result;
    });

// =========================================================================
// OPEN-WORLD ROUTABILITY OVERRIDE
// =========================================================================
//
// FUN_7100935ce0 @ NSO +0x935ce0 -- per-world overworld-routability
// predicate.  `bool FUN(uint32_t world_val)`.  The accessible-worlds
// evaluator at +0x480f20 calls it 8x (world_vals 1,3,4,5,6,7,2,9 ==
// AP buckets 0..7) and tests bit 0 of each result to build the routable
// world list.  Castle (world_val 8) is intentionally never queried there.
//
// Open-world mode: drainInbound caches an AP-authoritative routable-world
// mask (g_routable_world_mask, AP-bucket-bit indexed).  This trampoline
// forces a `true` return for any world whose bit is set, so those worlds
// are routable from the start; otherwise it passes through to orig() so
// vanilla progression still works for un-forced worlds and for every
// non-open-world session (mask == 0 -> no-op, byte-identical to vanilla).
//
// PROLOGUE SAFETY: +0x935ce0 is `sub/stp/stp/stp` then `adrp` at +0x14.
// The relocator patches the first 4 instructions ((0x935ce0+8)&7==0 ->
// count=4), all clean -- the adrp is never touched.  Direct hook is safe.
//
// world_val -> AP bucket is the inverse of kWorldValToBucket; Castle
// (world_val 8) maps to kCastleMaskBit explicitly.
HkTrampoline<bool, unsigned> worldRoutableHook = hk::hook::trampoline(
    [](unsigned world_val) -> bool {
        const bool orig_result = worldRoutableHook.orig(world_val);

        // Gate like the WS reader substitute: only override once a save
        // is loaded.  No scene-transition gate -- this is a pure read
        // substitution with no game-memory write.
        if (!probe::isSaveLoaded()) return orig_result;

        const std::uint32_t mask = smbwap::ap::getRoutableWorldMask();
        if (mask == 0) return orig_result;  // open-world inactive

        // world_val -> AP-bucket bit.  Castle (8) -> kCastleMaskBit.
        static constexpr signed char kWorldValToBucket[10] = {
            -1, 0, 6, 1, 2, 3, 4, 5, /*8=Castle*/ -1, 7,
        };
        int bit = -1;
        if (world_val == 8) {
            bit = static_cast<int>(smbwap::ap::kCastleMaskBit);
        } else if (world_val <= 9 && kWorldValToBucket[world_val] >= 0) {
            bit = kWorldValToBucket[world_val];
        }

        if (bit >= 0 && (mask & (1u << bit))) {
            // Rate-limited observability (saturating-CAS budget, same idiom
            // as the WS reader substitute) so we don't flood the log on
            // every world-map refresh.
            static std::atomic<int32_t> log_budget{32};
            int32_t b = log_budget.load(std::memory_order_relaxed);
            while (b > 0 && !log_budget.compare_exchange_weak(
                       b, b - 1, std::memory_order_relaxed)) {
            }
            if (b > 0) {
                SMBWAP_LOG_INFO(
                    "WorldRoutable force-true world_val=%u bit=%d orig=%d "
                    "mask=0x%03x",
                    world_val, bit, orig_result ? 1 : 0, mask);
            }
            return true;
        }
        return orig_result;
    });

// =========================================================================
// OPEN-WORLD ROUTE-GATE FORCE-OPEN
// =========================================================================
//
// FUN_7100383418 @ NSO +0x383418 -- the world-map route / course-point
// "FlowerLock" gate body.  Called per route segment; it writes a lock byte
// at *(actor+0x20) & ~3 (0 = locked, 1 = open) from a BYML/RomFS-driven
// FlowerLock param + gmd flags (master bool 0x30bdd45c, 0x925d4260, the
// Wonder-Seed count 0x90d4d0f2) via the sub-predicate FUN_71016cbf58.
// Because the per-route lock is BYML-driven there is NO gmd hash to flip; the
// only clean lever is to force the lock byte open after the original runs.
//
// Open-world: force EVERY route open so the player can walk Petal Isles to
// each open world's entrance (entering plays the world's own FirstVisitDemo,
// which moves the Poplins / removes the in-world obstacle actor), and so the
// in-world seed-bar routes the player would otherwise be blocked on open up.
// The Bowser-castle route opens too; access control for Bowser is the
// level-entry Royal-Seed death-gate, not this route gate.  Open question this
// hook is meant to answer in-game: whether forcing the lock byte also lets the
// player physically pass, or whether the obstacle actor still blocks.
//
// PROLOGUE SAFETY: first 4 patched instrs are sub sp / stp x29,x30 / stp
// x26,x25 / stp x24,x23 -- all stack setup, no PC-relative.  First adrp is at
// +0x2c (instruction 12).  (0x383418+8)&7==0 -> count=4.  Safe.
HkTrampoline<std::uint64_t, long long> routeGateForceOpenHook =
    hk::hook::trampoline([](long long param_1) -> std::uint64_t {
        const std::uint64_t r = routeGateForceOpenHook.orig(param_1);
        if (!probe::isSaveLoaded()) return r;
        if (smbwap::ap::getRoutableWorldMask() == 0) return r;  // vanilla
        // Scope to PETAL ISLES (the hub the player navigates) + the CASTLE
        // (Bowser approach).  Worlds are reached on foot from PI and left
        // VANILLA -- forcing their internal seed-bar routes open would skip
        // the normal per-world Wonder-Seed progression the player wants.
        // In-game world index (container-A hash 0x9f5ead3c): 2 = Petal Isles,
        // 8 = Castle (see kWorldValToBucket in the WS-override hook).
        void* gmd = probe::gmdSingleton();
        if (gmd == nullptr) return r;
        unsigned world_val = 0;
        containerAReaderHook.orig(reinterpret_cast<long>(gmd), &world_val,
                                  0x9f5ead3cu);
        if (world_val != 2u && world_val != 8u) return r;  // vanilla world
        // Lock byte = *(actor+0x20) & ~3 (low 2 bits are flags).  Force open.
        const std::uintptr_t slot = *reinterpret_cast<std::uintptr_t*>(
            static_cast<std::uintptr_t>(param_1) + 0x20);
        auto* lock = reinterpret_cast<std::uint8_t*>(slot & ~std::uintptr_t(3));
        if (lock != nullptr && *lock != 1u) {
            *lock = 1u;
            static std::atomic<std::int32_t> log_budget{24};
            std::int32_t b = log_budget.load(std::memory_order_relaxed);
            while (b > 0 && !log_budget.compare_exchange_weak(
                       b, b - 1, std::memory_order_relaxed)) {
            }
            if (b > 0) {
                SMBWAP_LOG_INFO("RouteGate force-open actor=0x%llx lock=%p",
                                static_cast<unsigned long long>(param_1),
                                static_cast<void*>(lock));
            }
        }
        return r;
    });

// =========================================================================
// OPEN-WORLD PETAL-ISLES INTRA-WORLD COURSE ROADS (course-clear-gated)
// =========================================================================
//
// The roads WITHIN Petal Isles that connect its courses normally only draw
// after the player clears the prerequisite level.  In open-world the player
// walks/fast-travels in without ever clearing those levels, so the connecting
// roads stay undrawn and block navigation.
//
// The world-map route evaluator ProcessWorldMapRouteGate (NSO +0x377280) and
// its two sibling per-route evaluators (FUN_71005840f0, FUN_71003791a0)
// re-derive every route on each world-map (re)load by switching on the
// route's CoursePointInfo ConditionType (the enum from FUN_71009281bc):
//   1 ClearAppointedCourse · 2 WonderSeed-count · 3 ClearLinkedCourse ·
//   4 OpenAppointedCourse · 5 GrandPropellerFlowerDemo · 7 FlowerLock · ...
// When a case's predicate returns 1 the segment is added to the drawn-routes
// list (FUN_71003797f0 -> FUN_7101b75f60) -- so the predicate result IS
// "draw this road", and forcing it open draws the road with no cutscene
// (the same live-confirmed mechanism as the W3->W4/5/6 reveal roads; see
// docs/grand-propeller-flower-reveal-re-2026-06-08.md).
//
// Petal Isles' connecting roads (WorldMapInfo002 CourseTable, offline RE)
// use the course-clear-derived conditions:
//   * Course5  -> ClearLinkedCourse    (case 3, leaf FUN_7100584eb0)
//   * Course12 -> ClearAppointedCourse (case 1, leaf FUN_7100585420)
// OpenAppointedCourse (case 4, leaf FUN_71005852d0) is the sibling form used
// by other worlds' equivalents; we cover it too so the same hook generalises.
// The WonderSeed-count case (2) is already handled by the AP-authoritative
// Wonder Seed reader override (containerAReaderHook), and FlowerLock (7) by
// routeGateForceOpenHook -- so the only gap left for the intra-world roads is
// the course-clear cases.
//
// LEVER (task option (a) -- preferred): rather than granting course-clear
// flags (which would pollute AP check / goal state), we hook the three CLEAN
// leaf predicates and force them "open" when open-world is active AND the
// player is currently on the Petal Isles map (in-game world index == 2, read
// from container-A hash 0x9f5ead3c, identical to routeGateForceOpenHook).
// No persistent state is written; AP course-clear state is untouched.
//
// SCOPE: gated to Petal Isles (world_val 2) ONLY, mirroring
// routeGateForceOpenHook -- the other worlds are walked into on foot and left
// VANILLA so their per-world course progression is unaffected.  All three
// leaf predicates have only route-gate evaluators as callers
// (ProcessWorldMapRouteGate / FUN_71005840f0 / FUN_71003791a0), so forcing
// them is on-purpose for every call site.
//
// PROLOGUE SAFETY (verified via Ghidra disassembly):
//   * FUN_7100585420: sub sp / stp x4 -- first adrp at +0x44 (instr 17).
//   * FUN_71005852d0: sub sp / stp x4 -- first adrp at +0x78 (instr 30).
//   * FUN_7100584eb0: sub sp / stp x6 -- first adrp at +0xd4 (instr 53).
//   All clean in the first 4 instructions; (off+8)&7==0 -> count=4.

// Shared gate: returns true iff open-world is active, a save is loaded, and
// the player is currently on the Petal Isles world map (world_val == 2).
// Reads the in-game world index via the (already-installed) container-A
// reader trampoline -- recursive Orig() bypasses our substitution and is
// pure, so it is recursion-safe.
bool openWorldPetalIslesActive() {
    if (!probe::isSaveLoaded()) return false;
    if (smbwap::ap::getRoutableWorldMask() == 0) return false;  // vanilla
    void* gmd = probe::gmdSingleton();
    if (gmd == nullptr) return false;
    unsigned world_val = 0;
    containerAReaderHook.orig(reinterpret_cast<long>(gmd), &world_val,
                              0x9f5ead3cu);
    return world_val == 2u;  // 2 == Petal Isles (container-A 0x9f5ead3c)
}

// Rate-limited "[petal-road] forced open" log shared by the three hooks.
void logPetalRoadForce(const char* which) {
    static std::atomic<std::int32_t> log_budget{24};
    std::int32_t b = log_budget.load(std::memory_order_relaxed);
    while (b > 0 && !log_budget.compare_exchange_weak(
               b, b - 1, std::memory_order_relaxed)) {
    }
    if (b > 0) {
        SMBWAP_LOG_INFO("[petal-road] %s force-open (open-world, PI)", which);
    }
}

// FUN_7100585420 @ NSO +0x585420 -- ClearAppointedCourse route predicate
// (ConditionType case 1).  `uint FUN(route)` returning & 1 = road open.
HkTrampoline<unsigned, long> clearAppointedCourseHook = hk::hook::trampoline(
    [](long route) -> unsigned {
        const unsigned r = clearAppointedCourseHook.orig(route);
        if ((r & 1u) != 0u) return r;            // already open -> pass through
        if (!openWorldPetalIslesActive()) return r;
        logPetalRoadForce("ClearAppointedCourse");
        return 1u;
    });

// FUN_71005852d0 @ NSO +0x5852d0 -- OpenAppointedCourse route predicate
// (ConditionType case 4).  `uint FUN(route)` returning & 1 = road open.
HkTrampoline<unsigned, long> openAppointedCourseHook = hk::hook::trampoline(
    [](long route) -> unsigned {
        const unsigned r = openAppointedCourseHook.orig(route);
        if ((r & 1u) != 0u) return r;
        if (!openWorldPetalIslesActive()) return r;
        logPetalRoadForce("OpenAppointedCourse");
        return 1u;
    });

// FUN_7100584eb0 @ NSO +0x584eb0 -- ClearLinkedCourse leaf predicate
// (ConditionType case 3; `bool FUN(courseClearRecord, courseId)` -- reads the
// per-course-clear container-C bitfield for the linked prerequisite course).
// Forcing it true reports the linked course "cleared" for route-draw purposes
// only (no write to the course-clear container, so AP check state is intact).
HkTrampoline<bool, long, unsigned> clearLinkedCourseHook = hk::hook::trampoline(
    [](long record, unsigned course_id) -> bool {
        const bool r = clearLinkedCourseHook.orig(record, course_id);
        if (r) return true;                       // already cleared -> pass
        if (!openWorldPetalIslesActive()) return r;
        logPetalRoadForce("ClearLinkedCourse");
        return true;
    });

// =========================================================================
// SCENE-CHANGE-REQUEST PROBE (toward the Bowser final-level stage-warp)
// =========================================================================
//
// FUN_710061a3d8 @ NSO +0x61a3d8 -- "request scene change to <name>":
// memcpy(name -> seqObj+0x640) then set request byte seqObj+0x690 = flag&1.
// The world-map "enter course" path resolves a course-point to its
// BYML-loaded stage-NAME string and calls this; the controller update loop
// then performs the actual ChangeScene.  The final-Bowser castle barrier is
// BYML/demo-gated and NOT openable by a gmd write (RE 2026-06-05), but this
// loader is gate-agnostic -- it loads whatever stage name it is given.  So a
// direct warp = call this with the castle's stage name.
//
// We don't know the castle stage name statically (it's a RomFS/BYML field),
// so this PROBE logs every scene-change request's stage name + seq pointer.
// Enter the final castle once on a 100% save and the log reveals both the
// stage-name string to warp to AND a live seq-object pointer shape.
//
// PROLOGUE SAFETY: first 4 patched instrs are stp x29,x30 / stp x22,x21 /
// stp x20,x19 / mov x29,sp -- all stack setup, no PC-relative.
// (0x61a3d8+8)&7==0 -> count=4.  Safe.
HkTrampoline<std::uint64_t, long, long, int> sceneChangeReqHook =
    hk::hook::trampoline([](long seqObj, long name, int flag) -> std::uint64_t {
        if (probe::isSaveLoaded() && name != 0) {
            static std::atomic<std::int32_t> log_budget{200};
            if (log_budget.fetch_sub(1) > 0) {
                SMBWAP_LOG_INFO("[scene-req] stage='%s' flag=%d seq=0x%lx",
                                reinterpret_cast<const char*>(name), flag,
                                static_cast<unsigned long>(seqObj));
            }
        }
        return sceneChangeReqHook.orig(seqObj, name, flag);
    });

// FUN_710055ee48 @ NSO +0x55ee48 -- the world-map "enter course/level" name
// setter: builds the destination stage name and copies it into the world-map
// sequence object's buffer at *(seqObj+0x640).  This is the path a level-enter
// from the world map takes (the shop path uses FUN_710061a3d8 instead).  We
// read the name *after* orig runs (the buffer is filled by then) to capture
// the final-castle stage name when the player enters it.
// PROLOGUE SAFETY: first 4 instrs sub sp / stp x29,x30 / str x21 / stp x20,x19
// -- no PC-relative (first adrp at +0x14, instr 6).  (0x55ee48+8)&7==0 -> 4.
HkTrampoline<std::uint64_t, long, long, long, long> courseEnterNameHook =
    hk::hook::trampoline([](long seqObj, long a2, long a3, long a4) -> std::uint64_t {
        const std::uint64_t r = courseEnterNameHook.orig(seqObj, a2, a3, a4);
        if (probe::isSaveLoaded()) {
            const char* name = *reinterpret_cast<const char* const*>(
                static_cast<std::uintptr_t>(seqObj) + 0x640);
            static std::atomic<std::int32_t> log_budget{100};
            if (name != nullptr && log_budget.fetch_sub(1) > 0) {
                SMBWAP_LOG_INFO("[course-enter] stage='%s' seq=0x%lx",
                                name, static_cast<unsigned long>(seqObj));
            }
        }
        return r;
    });

// ItemGetMaskBuild @ NSO +0x3c4050.
//
// Rebuilds the player ItemGet component's per-item-type "can pick up"
// bitmask (u32 at component+0xB0, bit = runtime item type) from the active
// game::actor::component::ItemGetSetting.  Called from the per-frame player
// tick (FUN_7100274244 -> +0x275400) plus setting-change paths, so a deny-
// mask change lands within a frame.  After orig() we strip the AP-denied
// bits; the pickup sensor check (`mask >> type & 1`, canonical reader NSO
// +0x182c8bc) then refuses the touch exactly like the vanilla DrillDig
// setting does -- item stays in the level, no pickup, no transform.
// PROLOGUE SAFETY: first 5 instrs sub sp / stp x29,x30 / stp x22,x21 /
// stp x20,x19 / add x29 -- no PC-relative.
HkTrampoline<void, void*, void*> itemGetMaskBuildHook = hk::hook::trampoline(
    [](void* component, void* a2) -> void {
        itemGetMaskBuildHook.orig(component, a2);
        probe::applyItemGetDenyMask(component);
    });

// BadgeShopComputeItemStates @ NSO +0x1c3f6a4
// (UIBadgeShopScreen_computeItemStates).  Rebuilds every shop row's
// display-state enum (item+0x20) from the badge owned/purchased GameData
// bits.  After orig() we override the AP-managed badge rows so the shop
// reflects AP's view of ownership rather than the in-game bits -- see
// probe/BadgeShop.hpp.  Inert until the bridge sets a non-zero managed
// mask.  PROLOGUE SAFETY: first 5 instrs sub sp / stp x29,x30 / stp x28,x27
// / stp x26,x25 / stp x24,x23 -- no PC-relative (first adrp is instr ~25).
HkTrampoline<void, void*> badgeShopComputeStatesHook = hk::hook::trampoline(
    [](void* screen) -> void {
        badgeShopComputeStatesHook.orig(screen);
        probe::applyBadgeShopItemStates(screen);
    });

// BadgeShopPurchaseCommit @ NSO +0x1c4072c (FUN_7101c4072c, the
// UIBadgeShopScreen "cPurchaseConfirmation" state execute).  On a confirmed
// BADGE buy it grants the badge exactly once, gated behind an affordability
// check, and sets a write-once done byte at screen+0x6f8.  We detect that
// single 0->1 edge across orig() -- with the dispatch kind at screen+0x6a8
// == 0 (badge case) -- and emit a BadgeAcquired so the bridge fires the
// shop check even when AP had already set the owned bit (the case the
// bitfield-diff path in setBadgeBitfieldAbsolute misses).  The just-bought
// badge internal_id stays live at screen+0x6ac after the grant.  PROLOGUE
// SAFETY: first 5 instrs stp x29,x30,[sp,#-0x50]! / str x25 / stp x24,x23 /
// stp x22,x21 / stp x20,x19 -- no PC-relative (first adrp deep in body).
HkTrampoline<void, void*> badgeShopPurchaseCommitHook = hk::hook::trampoline(
    [](void* screen) -> void {
        std::int32_t kind = 0;
        std::uint8_t done_before = 1;
        if (screen != nullptr) {
            auto* base = reinterpret_cast<unsigned char*>(screen);
            kind = *reinterpret_cast<std::int32_t*>(base + 0x6a8);
            done_before = *reinterpret_cast<std::uint8_t*>(base + 0x6f8);
        }
        badgeShopPurchaseCommitHook.orig(screen);
        if (screen == nullptr) return;
        auto* base = reinterpret_cast<unsigned char*>(screen);
        const std::uint8_t done_after =
            *reinterpret_cast<std::uint8_t*>(base + 0x6f8);
        // kind 0 == badge buy; done byte transitions 0->1 only inside the
        // afford-checked, id-valid success block -- a true per-purchase edge.
        if (kind == 0 && done_before == 0 && done_after != 0) {
            const std::int32_t id =
                *reinterpret_cast<std::int32_t*>(base + 0x6ac);
            probe::onBadgeShopPurchase(id);
        }
    });

// BadgeShopPaneResolve @ NSO +0x1c3e560 (UIBadgeShopScreen_resolvePaneContent).
// Builds the msbt label for a shop detail pane; the only caller that resolves
// BADGE name/desc panes.  We arm the AP shop-text context here (selected badge
// id @ screen+0x6ac when the selected type @ +0x6a8 is a badge) so the global
// msbt-resolver override below fires ONLY for the shop -- the badge equip menu
// (a different screen) never calls this, so its descriptions stay vanilla.
// PROLOGUE SAFETY: stp x29,x30 / str x21 / stp x20,x19 / mov x29 / ldr w8 --
// no PC-relative in the first 5 (cbz is 6th).
HkTrampoline<void, void*, int*, void*, void*, void*> badgeShopPaneResolveHook =
    hk::hook::trampoline(
        [](void* screen, int* paneId, void* a2, void* a3, void* msgLabelObj)
            -> void {
            if (screen != nullptr) {
                auto* base = reinterpret_cast<unsigned char*>(screen);
                const std::int32_t type =
                    *reinterpret_cast<std::int32_t*>(base + 0x6a8);
                const std::int32_t id =
                    *reinterpret_cast<std::int32_t*>(base + 0x6ac);
                probe::armBadgeShopText(type == 0 ? id : -1);
            }
            badgeShopPaneResolveHook.orig(screen, paneId, a2, a3, msgLabelObj);
        });

// MsbtLabelResolve @ NSO +0x250dec (FUN_7100250dec): the game-wide msbt
// label->text resolver.  ABI: (x0 store, x1 OUT handle {char16_t*;u32 len;u64
// id}, x2 file C-string, x3 -> label-builder whose *(char**) is the label).
// Tightly gated by probe::tryServeBadgeShopText (armed-shop + badge file +
// "BadgeId%02d" label) so it overrides ONLY the selected shop badge's
// description; everything else runs vanilla.  On a hit we fill the OUT handle
// and return 0 (success) without orig.  PROLOGUE SAFETY: sub sp / stp x29,x30 /
// str x28 / stp x24,x23 / stp x22,x21 -- no PC-relative in the first 5.
HkTrampoline<std::uint64_t, void*, void*, void*, void*> msbtLabelResolveHook =
    hk::hook::trampoline(
        [](void* store, void* out_handle, void* file, void* label_holder)
            -> std::uint64_t {
            if (out_handle != nullptr && file != nullptr
                && label_holder != nullptr) {
                const char* f = reinterpret_cast<const char*>(file);
                const char* label =
                    *reinterpret_cast<const char* const*>(label_holder);
                if (probe::tryServeBadgeShopText(f, label, out_handle)) {
                    return 0;  // success -- caller renders our UTF-16 text
                }
            }
            return msbtLabelResolveHook.orig(store, out_handle, file,
                                             label_holder);
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

void installSymHook(const char* friendly, hk::Result rc) {
    if (rc.failed()) {
        SMBWAP_LOG_ERROR("install %s by symbol FAILED rc=0x%x (symbol missing?)",
                         friendly, static_cast<unsigned>(rc.getValue()));
    } else {
        SMBWAP_LOG_INFO("install %s by symbol OK", friendly);
    }
}

}  // namespace

extern "C" void hkMain() {
    SMBWAP_LOG_INFO("=== smbwap hkMain START ===");
    SMBWAP_LOG_INFO("Phase 2g: 20 hooks + ap/ subsystem + real probe:: grants + WS reader override + open-world routability + course visibility + travel confirm + route-gate force-open + Petal-Isles intra-world course roads");

    // CORE_INIT (Phase 2a)
    installHook("CreateRootHeap",          0x005a66f8,
                createRootHeapHook.installAtMainOffset(0x005a66f8));
    installHook("GameFrameworkInitialize", 0x005a5cfc,
                gameFrameworkInitializeHook.installAtMainOffset(0x005a5cfc));

    // M1_EVENTS (Phase 2b) + PlayerTickLatch (Phase 2g.6 for DeathLink)
    installHook("NerveActivateOnce",        0x00559f7c,
                nerveActivateOnceHook.installAtMainOffset(0x00559f7c));
    installHook("SetCourseClearFlagExecute", 0x01bf28cc,
                setCourseClearFlagExecuteHook.installAtMainOffset(0x01bf28cc));
    installHook("GameGoalReachedExecute",    0x015b77a8,
                gameGoalReachedExecuteHook.installAtMainOffset(0x015b77a8));
    installHook("PlayerTickLatch",           0x00273868,
                playerTickLatchHook.installAtMainOffset(0x00273868));

    // CHARACTER-BLOCK SANITY: GetDamageReactionPlayerNo body @ +0x168d428.
    // Fires CHAR_BLOCK_HIT on every resolve to a local player slot; the PC
    // bridge filters to ObjectBlockClarityCharacter by the (course, chara)
    // table.  Shared-node over-fire is intentional (see hook comment).
    installHook("GetDamageReactionPlayerNo", 0x0168d428,
                getDamageReactionPlayerNoHook.installAtMainOffset(0x0168d428));

    // [cblk-diag2] GENERIC ACTOR-PLACEMENT ENUMERATION: InitActorPlacementInfo
    // @ +0x5815c.  Logs each DISTINCT gyaml class name placed in the level at
    // load (dedup-by-name), flagging "ObjectBlockClarityCharacter".  Clean
    // prologue (sub/stp/str/stp/add).  Iteration-3 keystone -- see the hook
    // comment + the smbwap-character-block-sanity memory.
    installHook("InitActorPlacementInfo", 0x0005815c,
                initActorPlacementInfoHook.installAtMainOffset(0x0005815c));

    // [cblk-diag3] GENERIC ACTOR-CREATE DISPATCH @ +0x2ceac0.  Fires once per
    // actor-create request; filters to "ObjectBlockClarityCharacter" by the
    // gyaml name (*param_4), reads PlayerCharaType from the placement node's
    // Dynamic sub-node, and runtime-resolves the actor-manager create fn
    // (vt[0x50]).  ITER 5: the manager-pointer chase is now CORRECT --
    // root=*(base+0x361f8b0); mgr=*(root+0x30 path A / +0x38 path B), gated on
    // (*(byte*)(mgr+0x849)&1) exactly like the decompile; createFn=*(*mgr+0x50);
    // it also dumps the create method's first 4 instrs so the NEXT iteration
    // can confirm a clean prologue + hook it for the live actor `this`+vtable.
    // (Iter-4 logged garbage because it read off a pointer TO the DAT slot
    // without dereferencing the root first.)  Clean prologue (sub/stp x6).
    // See the hook comment + the smbwap-character-block-sanity memory.
    installHook("ActorCreateDispatch", 0x002ceac0,
                actorCreateDispatchHook.installAtMainOffset(0x002ceac0));

    // PLAYREPORT (Phase 2c)
    installSymHook("PlayReportCtor",
        playReportCtorHook.installAtSym<"_ZN2nn5prepo10PlayReportC2EPKc">());
    installSymHook("PlayReportSetEventId",
        playReportSetEventIdHook.installAtSym<
            "_ZN2nn5prepo10PlayReport10SetEventIdEPKc">());
    installSymHook("PrepoIpcSaveReport",
        prepoIpcSaveReportHook.installAtSym<
            "_ZN2nn2sf4cmif6client6detail13CmifProxyImplINS_5prepo6detail3ipc13IPrepoServiceENS2_19CmifDomainProxyKindINS0_4hipc6client38Hipc2ClientSessionManagedProxyKindBaseINSB_18Hipc2ProxyKindBaseILNSA_6detail11MessageTypeE6EEEEEEENS0_30MemoryResourceAllocationPolicyES8_NS3_19ProcessModifierImplINS2_21DefaultProxyFilterTagEEEE22_nn_sf_sync_SaveReportERKNS0_7InArrayIcEERKNS0_8InBufferEm">());
    installSymHook("PrepoIpcSaveReportWithUser",
        prepoIpcSaveReportWithUserHook.installAtSym<
            "_ZN2nn2sf4cmif6client6detail13CmifProxyImplINS_5prepo6detail3ipc13IPrepoServiceENS2_19CmifDomainProxyKindINS0_4hipc6client38Hipc2ClientSessionManagedProxyKindBaseINSB_18Hipc2ProxyKindBaseILNSA_6detail11MessageTypeE6EEEEEEENS0_30MemoryResourceAllocationPolicyES8_NS3_19ProcessModifierImplINS2_21DefaultProxyFilterTagEEEE30_nn_sf_sync_SaveReportWithUserERKNS_7account3UidERKNS0_7InArrayIcEERKNS0_8InBufferEm">());

    // GRANTS (Phase 2d)
    installHook("GmdContainerAWriter", 0x0049f648,
                gmdContainerAWriterHook.installAtMainOffset(0x0049f648));
    installHook("GmdBoolWriter",       0x01f263fc,
                gmdBoolWriterHook.installAtMainOffset(0x01f263fc));

    // WONDER SEED GATE OVERRIDE (reader-side substitution)
    installHook("ContainerAReader",    0x0012ae94,
                containerAReaderHook.installAtMainOffset(0x0012ae94));

    // OPEN-WORLD COURSE VISIBILITY + ROUTABILITY OVERRIDES
    // CourseVisible: FUN_7100935c80 -- forces course-node actors with an
    //   uninitialized +0x8d byte visible when open-world mask is non-zero.
    installHook("CourseVisible",       0x00935c80,
                courseVisibleHook.installAtMainOffset(0x00935c80));
    // WorldRoutable: FUN_7100935ce0 -- forces travel-TAB to show masked worlds.
    installHook("WorldRoutable",       0x00935ce0,
                worldRoutableHook.installAtMainOffset(0x00935ce0));
    // WorldTravel: FUN_710055ed80 -- forces travel-CONFIRM to allow masked
    //   worlds even when no courses have been visited yet.
    installHook("WorldTravel",         0x0055ed80,
                worldTravelHook.installAtMainOffset(0x0055ed80));
    // RouteGateForceOpen: FUN_7100383418 -- forces every world-map route
    //   (Petal Isles bridges + in-world seed-bar paths + castle route) open
    //   so the player can walk to each open world.  Bowser stays gated by the
    //   Royal-Seed death-gate, not this route gate.
    installHook("RouteGateForceOpen",  0x00383418,
                routeGateForceOpenHook.installAtMainOffset(0x00383418));
    // OPEN-WORLD PETAL-ISLES INTRA-WORLD COURSE ROADS: force the three
    //   course-clear-derived route-gate leaf predicates open (only on the
    //   Petal Isles map, only in open-world) so the roads that connect PI's
    //   courses draw without the player having cleared the prerequisite
    //   levels.  No persistent / AP course-clear state is written.
    //   ClearAppointedCourse (case 1): FUN_7100585420.
    installHook("ClearAppointedCourse", 0x00585420,
                clearAppointedCourseHook.installAtMainOffset(0x00585420));
    //   OpenAppointedCourse (case 4): FUN_71005852d0.
    installHook("OpenAppointedCourse",  0x005852d0,
                openAppointedCourseHook.installAtMainOffset(0x005852d0));
    //   ClearLinkedCourse (case 3): FUN_7100584eb0.
    installHook("ClearLinkedCourse",    0x00584eb0,
                clearLinkedCourseHook.installAtMainOffset(0x00584eb0));
    // SceneChangeReq: FUN_710061a3d8 -- logs every scene-change request's
    //   stage name + seq pointer, to capture the final-castle stage name for
    //   a direct Bowser warp (the castle barrier is BYML/demo-gated).
    installHook("SceneChangeReq",      0x0061a3d8,
                sceneChangeReqHook.installAtMainOffset(0x0061a3d8));
    // CourseEnterName: FUN_710055ee48 -- captures the stage name when entering
    //   a level/course from the world map (the final-castle enter path).
    installHook("CourseEnterName",     0x0055ee48,
                courseEnterNameHook.installAtMainOffset(0x0055ee48));
    // ItemGetMaskBuild: FUN_71003c4050 -- power-up pickup negation choke
    //   point.  Inert while probe::deniedItemGetMask() == 0; see
    //   probe/ItemGetGate.hpp for the RE notes + per-item bit table.
    installHook("ItemGetMaskBuild",    0x003c4050,
                itemGetMaskBuildHook.installAtMainOffset(0x003c4050));
    // BADGE SHOP AP-AUTHORITATIVE OWNERSHIP (2026-06-10).  Both inert until
    // the bridge pushes a non-zero managed mask (SetBadgeShopState).  See
    // probe/BadgeShop.hpp for the RE + the proven UIBadgeShopScreen offsets.
    installHook("BadgeShopComputeStates", 0x01c3f6a4,
                badgeShopComputeStatesHook.installAtMainOffset(0x01c3f6a4));
    installHook("BadgeShopPurchaseCommit", 0x01c4072c,
                badgeShopPurchaseCommitHook.installAtMainOffset(0x01c4072c));
    // BADGE SHOP AP TEXT (2026-06-10): show the AP check a badge purchase
    // would send in the shop detail panel.  Pane-resolve arms the shop
    // context; the global msbt resolver serves our text only while armed.
    // Both inert until the bridge pushes set_badge_shop_text.
    installHook("BadgeShopPaneResolve", 0x01c3e560,
                badgeShopPaneResolveHook.installAtMainOffset(0x01c3e560));
    installHook("MsbtLabelResolve",     0x00250dec,
                msbtLabelResolveHook.installAtMainOffset(0x00250dec));
    // DEBUG OVERLAY: NVN present-hook chain that drives the ImGui overlay.
    // No-op unless built with SMBWAP_HAS_DEBUG_RENDERER (see CMakeLists.txt
    // + docs/handoff-imgui-overlay.md).
    smbwap::ui::installNvnImGuiHooks();

    SMBWAP_LOG_INFO("=== smbwap hkMain END ===");
}
