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
#include "probe/DeathLink.hpp"
#include "probe/Gates.hpp"
#include "probe/Gmd.hpp"
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
        playerTickLatchHook.orig(param_1, param_2);
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
    SMBWAP_LOG_INFO("Phase 2g: 13 hooks + ap/ subsystem + real probe:: grants + WS reader override");

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

    SMBWAP_LOG_INFO("=== smbwap hkMain END ===");
}
