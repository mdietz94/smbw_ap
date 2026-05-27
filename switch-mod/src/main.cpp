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
            return prepoIpcSaveReportHook.orig(thisPtr, room, payload, flags);
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
            return prepoIpcSaveReportWithUserHook.orig(
                thisPtr, uid, room, payload, flags);
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
            static int s_fires = 0;
            ++s_fires;
            if (s_fires <= 50 || (s_fires & 0xFF) == 0) {
                SMBWAP_LOG_INFO(
                    "gmd.bool_writer hash=0x%08x value=%u substruct=%p (fire #%d)",
                    hash, value, substruct, s_fires);
            }
            return gmdBoolWriterHook.orig(substruct, value, hash);
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
    SMBWAP_LOG_INFO("Phase 2d: 3 CORE_INIT + 3 M1_EVENTS + 4 PLAYREPORT + 2 GRANTS hooks");

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

    SMBWAP_LOG_INFO("=== smbwap hkMain END ===");
}
