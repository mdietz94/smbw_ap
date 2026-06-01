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

// RequestEventCourseExitByAreaTag — the ACTIVE Nerve that fires on a natural
// course-out (pause→"Return to World Map", give-up, death-with-no-lives).
// Its execute (slot 8) is thunk_FUN_7100559f7c, so the NerveActivateOnce hook
// below already sees it fire, with `nerve` == the course-sequence controller.
// Capturing that controller's layout is the key unlock for the "bounce" (let
// a gated course load, then drive the game's own course-out).  Log-only.
// See docs/gate-entry-session3-handoff.md "Recommended NEXT STEPS" #1.
constexpr ::ptr kVtableOff_CourseExitByAreaTag = 0x33fd690;

// Log-only observability dump (gate-entry session 3).  Dumps `nwords`
// consecutive 64-bit words from `addr`, 2 per line, prefixed with the byte
// offset.  No fault guard (the subsdk has no SEH), so only call on pointers
// already shown live by a null-checked walk — here, Nerve `this` pointers and
// the course-out global chain, both valid at the moment their Nerve fires.
void dumpWords(const char* tag, const void* addr, unsigned nwords) {
    if (addr == nullptr) {
        SMBWAP_LOG_INFO("%s = <null>", tag);
        return;
    }
    const auto* w = reinterpret_cast<const unsigned long long*>(addr);
    for (unsigned i = 0; i < nwords; i += 2) {
        if (i + 1 < nwords) {
            SMBWAP_LOG_INFO("%s +0x%02x: %016llx %016llx",
                            tag, i * 8u, w[i], w[i + 1]);
        } else {
            SMBWAP_LOG_INFO("%s +0x%02x: %016llx", tag, i * 8u, w[i]);
        }
    }
}

// gate-entry session 3, capture #2c/#3 — runtime anchor finder v3 (log-only).
//
// capture #2c CLIMB RESULT (live): ctx 0x20bf799330 (vt Ghidra 0x71034a8200)
// -> L1 parent 0x20bf798dc0 (vt 0x7103517068) -> L2 grandparent 0x20bf796368
// (vt 0x71033f9660, a 0x208-byte al sequence container built by the virtual
// factory FUN_7100229fa4).  The +0x08 chain TOPS OUT at the grandparent (its
// +0x08 is not a parent pointer — reading it as one faulted last build), and
// the factory is reached by indirect dispatch, so there is no cheap static
// global->ctx path.  v3 therefore (a) makes EVERY read fault-safe via
// svc::QueryMemory (the prior crash was reading an in-window-but-unmapped
// value), and (b) re-enables a WIDE scan of the scene + its +0x40 sub-object
// for ctx/parent/grandparent so we learn which global/scene offset holds the
// tree.  ctx is stable at 0x20bf799330 across every capture (deterministic dev
// allocator), so once we know the scene->grandparent offset the walker is
// fixed.
//
// SAFE: QueryMemory gates every dereference (mapped + readable), with the
// last-good region cached so a contiguous scan costs a handful of SVCs.  Reads
// only; one-shot.
void anchorSearch(std::uintptr_t base, std::uintptr_t ctx,
                  std::uintptr_t parent, std::uintptr_t courseMgr) {
    auto ptrish = [](std::uintptr_t p) {
        return p >= 0x2000000000ULL && p < 0x2800000000ULL && (p & 7) == 0;
    };
    // Fault-safe qword read: returns false (without touching memory) unless
    // QueryMemory says the page is mapped + readable.  Caches the last good
    // [okBase, okEnd) so a sequential sweep re-queries only on region change.
    std::uintptr_t okBase = 1, okEnd = 0;
    auto rd = [&](std::uintptr_t p, std::uintptr_t& out) -> bool {
        if (!(p >= okBase && p + 8 <= okEnd)) {
            hk::svc::MemoryInfo info; std::uint32_t pg;
            if (!hk::svc::QueryMemory(&info, &pg,
                    static_cast<::ptr>(p)).succeeded()) { okBase = 1; okEnd = 0; return false; }
            if ((static_cast<std::uint32_t>(info.permission)
                 & hk::svc::MemoryPermission_Read) == 0) { okBase = 1; okEnd = 0; return false; }
            okBase = info.base_address;
            okEnd  = info.base_address + info.size;
            if (!(p >= okBase && p + 8 <= okEnd)) return false;
        }
        out = *reinterpret_cast<std::uintptr_t*>(p);
        return true;
    };
    auto isObject = [&](std::uintptr_t p) -> bool {
        if (!ptrish(p)) return false;
        std::uintptr_t vt;
        if (!rd(p, vt)) return false;
        return base != 0 && vt >= base && vt < base + 0x4000000ULL;
    };
    auto toGhidra = [&](std::uintptr_t rt) -> unsigned long long {
        return (rt && base) ? static_cast<unsigned long long>(
                                  0x7100000000ULL + (rt - base)) : 0ULL;
    };

    // (1) Ancestor climb via +0x08.  targets[] collects ctx + each ancestor.
    std::uintptr_t targets[18];
    int nt = 0;
    targets[nt++] = ctx;
    SMBWAP_LOG_INFO("ancestor climb (via +0x08):");
    std::uintptr_t cur = ctx;
    for (int lvl = 0; lvl < 16 && nt < 18; ++lvl) {
        std::uintptr_t vt;
        if (!isObject(cur) || !rd(cur, vt)) {
            SMBWAP_LOG_INFO("  L%d obj=0x%llx (stop)",
                            lvl, static_cast<unsigned long long>(cur));
            break;
        }
        SMBWAP_LOG_INFO("  L%d obj=0x%llx vt(ghidra)=0x%llx",
                        lvl, static_cast<unsigned long long>(cur), toGhidra(vt));
        std::uintptr_t up;
        if (!rd(cur + 0x08, up) || up == cur || !isObject(up)) break;
        cur = up;
        targets[nt++] = cur;
    }
    auto matchTarget = [&](std::uintptr_t v) -> int {
        if (v == parent) return 100;  // sentinel: matched parent specifically
        for (int i = 0; i < nt; ++i) if (v == targets[i]) return i;
        return -1;
    };
    auto label = [&](int ti) -> const char* {
        return ti == 100 ? "parent" : (ti == 0 ? "ctx" : "ancestor");
    };

    int hits = 0;

    // (2) Scan: wide sweep of the scene + its big +0x40 sub-object (now
    // QueryMemory-safe), plus a 2-hop sweep of the smaller globals, for ctx /
    // parent / any climbed ancestor (esp. the grandparent).
    std::uintptr_t scene = 0, scene40 = 0;
    std::uintptr_t areaMgr = 0, g1 = 0, a88 = 0, d90 = 0, cinmgr = 0;
    if (base) {
        rd(base + 0x3625850u, scene);
        if (isObject(scene)) rd(scene + 0x40, scene40);
        rd(base + 0x3628398u, areaMgr);
        rd(base + 0x3623670u, g1);
        rd(base + 0x3630a88u, a88);
        rd(base + 0x3628d90u, d90);
        rd(base + 0x3630d28u, cinmgr);
    }
    struct Seed { const char* name; std::uintptr_t root; unsigned win; bool hop2; };
    const Seed seeds[] = {
        {"scene",       scene,    0x1000, false},
        {"scene+0x40",  scene40,  0x2c00, false},
        {"areaMgr",     areaMgr,  0x200,  true},
        {"g1",          g1,       0x200,  true},
        {"DAT_3630a88", a88,      0x200,  true},
        {"DAT_3628d90", d90,      0x200,  true},
        {"courseInMgr", cinmgr,   0x200,  true},
        {"courseMgr",   courseMgr,0x200,  true},
    };
    for (const auto& s : seeds) {
        if (!isObject(s.root)) continue;
        for (unsigned k = 0; k < s.win; k += 8) {
            std::uintptr_t w;
            if (!rd(s.root + k, w)) continue;
            int ti = matchTarget(w);
            if (ti >= 0) {
                SMBWAP_LOG_INFO("ANCHOR: %s + 0x%x == %s[L%d]",
                                s.name, k, label(ti), ti == 100 ? 1 : ti);
                ++hits;
            }
            if (s.hop2 && w != s.root && isObject(w)) {
                for (unsigned j = 0; j < 0x200; j += 8) {
                    std::uintptr_t w2;
                    if (!rd(w + j, w2)) continue;
                    ti = matchTarget(w2);
                    if (ti >= 0) {
                        SMBWAP_LOG_INFO("ANCHOR: %s + 0x%x -> + 0x%x == %s[L%d]",
                                        s.name, k, j, label(ti),
                                        ti == 100 ? 1 : ti);
                        ++hits;
                    }
                }
            }
        }
    }

    SMBWAP_LOG_INFO("anchorSearch v3 done: %d hit(s), %d ancestor level(s) "
                    "(ctx=%p parent=%p)", hits, nt,
                    reinterpret_cast<void*>(ctx),
                    reinterpret_cast<void*>(parent));
}

// gate-entry session 6 (2026-05-31) — grandparent OWNER finder (log-only,
// QueryMemory-safe; runs at the natural course-out, where ctx is the live
// ExitCourseMgr-body arg).
//
// SETTLED in session 5: the whole exit tree (ctx -> parent -> grandparent, each
// via +0x08) is TRANSIENT — built on course-out into a fixed arena, torn down
// after; nothing up that chain persists during play.  The grandparent (al
// sequence container, vtable NSO +0x33f9660, 0x208 B) is created by the al
// SceneFactory (FUN_7100229fa4) and pushed onto a PERSISTENT al SceneController's
// scene stack.  The factory writes NO owner pointer (decompiled: only the vtable
// + self-referential intrusive-list sentinels), so the owner link must be a
// field elsewhere in the grandparent (a back-pointer to the controller base, or
// an intrusive-list node pointing into the controller), OR held one-directionally
// by the controller.  Find it three ways at the SAME instant:
//   (1) census the persistent g1 (DAT_3623670) SceneController cluster (the
//       play-time-reachable controllers the handoff found at g1+0x20, vtables
//       0x710349d…) so any link can be matched to a real persistent object;
//   (2) dump the grandparent's 0x208 bytes, flagging every ptr field that
//       targets a live main-module-vtable'd object (back-pointer to owner base)
//       or lands inside a censused controller (intrusive back-link);
//   (3) forward scan: does any censused controller hold gp / parent / ctx?
// Reads only (svc::QueryMemory-gated); one-shot.  The owner's class + vtable
// (mapped to Ghidra) is the next-session decompile target: its scene-push /
// change-to-exit-state method is the clean persistent lever.
void grandparentOwnerProbe(std::uintptr_t base, std::uintptr_t ctx) {
    if (base == 0 || ctx == 0) return;
    auto ptrish = [](std::uintptr_t p) {
        return p >= 0x2000000000ULL && p < 0x2800000000ULL && (p & 7) == 0;
    };
    std::uintptr_t okBase = 1, okEnd = 0;
    auto rd = [&](std::uintptr_t p, std::uintptr_t& out) -> bool {
        if (!(p >= okBase && p + 8 <= okEnd)) {
            hk::svc::MemoryInfo info; std::uint32_t pg;
            if (!hk::svc::QueryMemory(&info, &pg,
                    static_cast<::ptr>(p)).succeeded()) { okBase = 1; okEnd = 0; return false; }
            if ((static_cast<std::uint32_t>(info.permission)
                 & hk::svc::MemoryPermission_Read) == 0) { okBase = 1; okEnd = 0; return false; }
            okBase = info.base_address;
            okEnd  = info.base_address + info.size;
            if (!(p >= okBase && p + 8 <= okEnd)) return false;
        }
        out = *reinterpret_cast<std::uintptr_t*>(p);
        return true;
    };
    auto isObject = [&](std::uintptr_t p) -> bool {
        if (!ptrish(p)) return false;
        std::uintptr_t vt;
        if (!rd(p, vt)) return false;
        return vt >= base && vt < base + 0x4000000ULL;
    };
    auto toG = [&](std::uintptr_t rt) -> unsigned long long {
        return (rt && base) ? 0x7100000000ULL + (rt - base) : 0ULL;
    };

    // Derive parent (ctx+0x08) and grandparent (parent+0x08).  ctx is the live
    // body arg; parent is a live object at exit; reading parent+0x08 is the same
    // step the capture-#2c climb made successfully (it only faulted reading
    // grandparent+0x08, which we never do here).
    std::uintptr_t parent = 0, gp = 0;
    if (!rd(ctx + 0x08, parent) || !ptrish(parent)) {
        SMBWAP_LOG_INFO("gpOwnerProbe: parent unreadable (ctx=0x%llx)",
                        (unsigned long long)ctx);
        return;
    }
    if (!rd(parent + 0x08, gp) || !ptrish(gp)) {
        SMBWAP_LOG_INFO("gpOwnerProbe: grandparent unreadable (parent=0x%llx)",
                        (unsigned long long)parent);
        return;
    }
    std::uintptr_t cvt = 0, pvt = 0, gvt = 0;
    rd(ctx, cvt); rd(parent, pvt); rd(gp, gvt);
    SMBWAP_LOG_INFO("gpOwnerProbe: B_main=0x%llx", (unsigned long long)base);
    SMBWAP_LOG_INFO("  ctx=0x%llx vt(g)=0x%llx  parent=0x%llx vt(g)=0x%llx  "
                    "grandparent=0x%llx vt(g)=0x%llx",
                    (unsigned long long)ctx, toG(cvt),
                    (unsigned long long)parent, toG(pvt),
                    (unsigned long long)gp, toG(gvt));

    // (1) census the persistent g1 SceneController cluster (g1+0x20 + 1 hop).
    constexpr int kMaxCtrl = 40;
    std::uintptr_t ctrlAddr[kMaxCtrl]; int nCtrl = 0;
    auto addCtrl = [&](std::uintptr_t o, std::uintptr_t vt, const char* via,
                       unsigned off) {
        for (int i = 0; i < nCtrl; ++i) if (ctrlAddr[i] == o) return;
        if (nCtrl < kMaxCtrl) {
            ctrlAddr[nCtrl++] = o;
            SMBWAP_LOG_INFO("  g1ctrl[%d] obj=0x%llx vt(g)=0x%llx via %s+0x%x",
                            nCtrl - 1, (unsigned long long)o, toG(vt), via, off);
        }
    };
    std::uintptr_t g1 = 0;
    if (rd(base + 0x3623670u, g1) && isObject(g1)) {
        SMBWAP_LOG_INFO("  g1(DAT_3623670)=0x%llx", (unsigned long long)g1);
        for (unsigned k = 0; k < 0x200 && nCtrl < kMaxCtrl; k += 8) {
            std::uintptr_t w;
            if (!rd(g1 + k, w) || !isObject(w)) continue;
            std::uintptr_t wvt = 0; rd(w, wvt);
            addCtrl(w, wvt, "g1", k);
            for (unsigned j = 0; j < 0x200 && nCtrl < kMaxCtrl; j += 8) {
                std::uintptr_t w2;
                if (!rd(w + j, w2) || !isObject(w2)) continue;
                std::uintptr_t w2vt = 0; rd(w2, w2vt);
                addCtrl(w2, w2vt, "g1.sub", j);
            }
        }
    } else {
        SMBWAP_LOG_INFO("  g1(DAT_3623670) not an object (g1=0x%llx)",
                        (unsigned long long)g1);
    }

    // (2) dump the grandparent's 0x208 bytes; flag owner candidates.
    SMBWAP_LOG_INFO("  --- grandparent dump (0x208 B) ---");
    int objFields = 0, ownerHits = 0;
    for (unsigned k = 0; k < 0x208; k += 8) {
        std::uintptr_t w;
        if (!rd(gp + k, w) || !ptrish(w)) continue;
        const char* tag = "";
        if (w == ctx) tag = " ==ctx";
        else if (w == parent) tag = " ==parent";
        else if (w == gp) tag = " ==grandparent(self)";
        int inCtrl = -1; unsigned inOff = 0;
        for (int i = 0; i < nCtrl; ++i) {
            if (w >= ctrlAddr[i] && w < ctrlAddr[i] + 0x2000) {
                inCtrl = i; inOff = (unsigned)(w - ctrlAddr[i]); break;
            }
        }
        if (isObject(w)) {
            std::uintptr_t wvt = 0; rd(w, wvt);
            ++objFields;
            if (inCtrl >= 0 && tag[0] == '\0') ++ownerHits;
            SMBWAP_LOG_INFO("  GP +0x%03x: OBJ 0x%llx vt(g)=0x%llx%s%s", k,
                            (unsigned long long)w, toG(wvt), tag,
                            inCtrl >= 0 ? " [OWNER? base==g1ctrl]" : "");
        } else if (inCtrl >= 0) {
            ++ownerHits;
            SMBWAP_LOG_INFO("  GP +0x%03x: -> inside g1ctrl[%d]+0x%x (0x%llx)"
                            " [OWNER back-link]%s", k, inCtrl, inOff,
                            (unsigned long long)w, tag);
        } else {
            SMBWAP_LOG_INFO("  GP +0x%03x: ptr 0x%llx (non-obj)%s", k,
                            (unsigned long long)w, tag);
        }
    }

    // (3) forward owner search: does any censused controller hold gp/parent/ctx?
    int fwd = 0;
    for (int i = 0; i < nCtrl; ++i) {
        for (unsigned j = 0; j < 0x800; j += 8) {
            std::uintptr_t w;
            if (!rd(ctrlAddr[i] + j, w)) continue;
            const char* what = (w == gp) ? "grandparent"
                             : (w == parent) ? "parent"
                             : (w == ctx) ? "ctx" : nullptr;
            if (what) {
                ++fwd;
                SMBWAP_LOG_INFO("  FWD: g1ctrl[%d](0x%llx)+0x%x == %s "
                                "[OWNER holds it]", i,
                                (unsigned long long)ctrlAddr[i], j, what);
            }
        }
    }

    SMBWAP_LOG_INFO("gpOwnerProbe done: %d obj field(s), %d owner hit(s), "
                    "%d fwd hit(s), %d g1 ctrl(s)",
                    objFields, ownerHits, fwd, nCtrl);
}

// gate-entry session 6 capture #2 — persistent course-sequence controller
// state DUMP (log-only, QueryMemory-safe).
//
// Capture #1 (gpOwnerProbe) pinned the PERSISTENT controller: g1 =
// DAT_3623670 (0x20d2fd2db0 in dev), a multiply-inherited NerveExecutor whose
// primary vtable is NSO +0x349d300 and whose code cluster (0x7101bae…) sits
// right next to the course / ExitCourseMgr management code.  Its ctor
// (FUN_7101baeb20) lays out embedded subobjects at +0x08 (vt +0x34f3ac0),
// +0xf0 (vt +0x349d4d0), +0x118 (vt +0x349d4f0) — all within g1+0x00..0x140.
// The transient exit scene tree is NOT linked from this controller within the
// shallow census, so the lever is a FIELD on this controller that flips when
// course-out begins (a current/next-scene pointer, a NerveKeeper step word, or
// a course-out request flag).  Find it by DIFFING the controller's state during
// PLAY vs. at the EXIT moment: dump g1+0x00..0x160 at both; the changed field
// is the trigger.  Reads only (svc::QueryMemory-gated).
void dumpG1CourseSeqCtrl(const char* phase, std::uintptr_t base) {
    if (base == 0) return;
    auto ptrish = [](std::uintptr_t p) {
        return p >= 0x2000000000ULL && p < 0x2800000000ULL && (p & 7) == 0;
    };
    std::uintptr_t okBase = 1, okEnd = 0;
    auto rd = [&](std::uintptr_t p, std::uintptr_t& out) -> bool {
        if (!(p >= okBase && p + 8 <= okEnd)) {
            hk::svc::MemoryInfo info; std::uint32_t pg;
            if (!hk::svc::QueryMemory(&info, &pg,
                    static_cast<::ptr>(p)).succeeded()) { okBase = 1; okEnd = 0; return false; }
            if ((static_cast<std::uint32_t>(info.permission)
                 & hk::svc::MemoryPermission_Read) == 0) { okBase = 1; okEnd = 0; return false; }
            okBase = info.base_address;
            okEnd  = info.base_address + info.size;
            if (!(p >= okBase && p + 8 <= okEnd)) return false;
        }
        out = *reinterpret_cast<std::uintptr_t*>(p);
        return true;
    };
    auto toG = [&](std::uintptr_t rt) -> unsigned long long {
        return (rt && base) ? 0x7100000000ULL + (rt - base) : 0ULL;
    };
    std::uintptr_t g1 = 0;
    if (!rd(base + 0x3623670u, g1) || !ptrish(g1)) {
        SMBWAP_LOG_INFO("g1ctrlDump[%s]: g1 not readable (g1=0x%llx)",
                        phase, (unsigned long long)g1);
        return;
    }
    std::uintptr_t g1vt = 0; rd(g1, g1vt);
    SMBWAP_LOG_INFO("g1ctrlDump[%s]: g1=0x%llx vt(g)=0x%llx", phase,
                    (unsigned long long)g1, toG(g1vt));
    // Dump g1+0x00..0x160 (44 qwords), one per line, annotating values that are
    // (or whose &~3 tagged form is) a live main-module object, so a play-vs-exit
    // diff is easy to read.
    for (unsigned k = 0; k < 0x160; k += 8) {
        std::uintptr_t w;
        if (!rd(g1 + k, w)) { SMBWAP_LOG_INFO("  g1+0x%03x: <unmapped>", k); continue; }
        std::uintptr_t pw = w & ~3ULL, vt = 0;
        if (ptrish(pw) && rd(pw, vt) && vt >= base && vt < base + 0x4000000ULL) {
            SMBWAP_LOG_INFO("  g1+0x%03x: 0x%016llx OBJ vt(g)=0x%llx", k,
                            (unsigned long long)w, toG(vt));
        } else {
            SMBWAP_LOG_INFO("  g1+0x%03x: 0x%016llx", k, (unsigned long long)w);
        }
    }
    SMBWAP_LOG_INFO("g1ctrlDump[%s] done", phase);
}

// gate-entry session 6 capture #3 — WIDE holder reachability search
// (log-only, QueryMemory-safe, budget-capped).  Fired at the EXIT moment.
//
// cap #1: the grandparent has no owner back-pointer.  cap #2: the g1 root
// controller's first 0x160 B is unchanged play-vs-exit (g1 root is not the
// direct holder/driver of the transient exit tree).  So SOMETHING persistent
// still ticks the exit sequence (grandparent/parent/ctx) — find it by a
// breadth-first walk of the persistent object graph from the scene
// (DAT_3625850, the teardown's *(scene+0x40)+0x2c40 prime suspect), g1
// (DAT_3623670) and the area mgr (DAT_3628398), reporting any field that points
// to — or INTO — the exit tree (a raw pointer == ctx/parent/grandparent, or a
// list-node link landing inside one; tagged ptrs matched via &~3).  The holder
// object's class vtable (Ghidra-mapped) is the lever target.  Bounded: ≤512
// objects, ≤300k reads; every read svc::QueryMemory-gated; hit-only logging.
void holderSearchWide(std::uintptr_t base, std::uintptr_t ctx) {
    if (base == 0 || ctx == 0) return;
    auto ptrish = [](std::uintptr_t p) {
        return p >= 0x2000000000ULL && p < 0x2800000000ULL && (p & 7) == 0;
    };
    std::uintptr_t okBase = 1, okEnd = 0;
    auto rd = [&](std::uintptr_t p, std::uintptr_t& out) -> bool {
        if (!(p >= okBase && p + 8 <= okEnd)) {
            hk::svc::MemoryInfo info; std::uint32_t pg;
            if (!hk::svc::QueryMemory(&info, &pg,
                    static_cast<::ptr>(p)).succeeded()) { okBase = 1; okEnd = 0; return false; }
            if ((static_cast<std::uint32_t>(info.permission)
                 & hk::svc::MemoryPermission_Read) == 0) { okBase = 1; okEnd = 0; return false; }
            okBase = info.base_address;
            okEnd  = info.base_address + info.size;
            if (!(p >= okBase && p + 8 <= okEnd)) return false;
        }
        out = *reinterpret_cast<std::uintptr_t*>(p);
        return true;
    };
    auto isObject = [&](std::uintptr_t p) -> bool {
        if (!ptrish(p)) return false;
        std::uintptr_t vt;
        if (!rd(p, vt)) return false;
        return vt >= base && vt < base + 0x4000000ULL;
    };
    auto toG = [&](std::uintptr_t rt) -> unsigned long long {
        return (rt && base) ? 0x7100000000ULL + (rt - base) : 0ULL;
    };

    std::uintptr_t parent = 0, gp = 0;
    if (!rd(ctx + 8, parent) || !ptrish(parent)) {
        SMBWAP_LOG_INFO("holderSearch: parent unreadable"); return;
    }
    if (!rd(parent + 8, gp) || !ptrish(gp)) {
        SMBWAP_LOG_INFO("holderSearch: grandparent unreadable"); return;
    }
    SMBWAP_LOG_INFO("holderSearch: targets ctx=0x%llx parent=0x%llx gp=0x%llx",
                    (unsigned long long)ctx, (unsigned long long)parent,
                    (unsigned long long)gp);
    auto match = [&](std::uintptr_t w) -> const char* {
        std::uintptr_t v = w & ~3ULL;
        if (v == ctx) return "==ctx";
        if (v == parent) return "==parent";
        if (v == gp) return "==gp";
        if (v > ctx && v < ctx + 0x28) return "->ctx+";
        if (v > parent && v < parent + 0x100) return "->parent+";
        if (v > gp && v < gp + 0x208) return "->gp+";
        return nullptr;
    };

    constexpr int kNMax = 512;
    struct QE { std::uintptr_t a; unsigned win; };
    QE q[kNMax]; int qh = 0, qt = 0;
    std::uintptr_t seen[kNMax]; int ns = 0;
    auto seenHas = [&](std::uintptr_t o) {
        for (int i = 0; i < ns; ++i) if (seen[i] == o) return true; return false;
    };
    auto enq = [&](std::uintptr_t o, unsigned win) {
        if (!isObject(o) || seenHas(o) || ns >= kNMax || qt >= kNMax) return;
        seen[ns++] = o; q[qt].a = o; q[qt].win = win; ++qt;
    };

    std::uintptr_t scene = 0, scene40 = 0, areaMgr = 0, g1 = 0;
    if (rd(base + 0x3625850u, scene) && isObject(scene)) {
        enq(scene, 0x4000);
        if (rd(scene + 0x40, scene40) && isObject(scene40)) enq(scene40, 0x4000);
    }
    if (rd(base + 0x3628398u, areaMgr)) enq(areaMgr, 0x1000);
    if (rd(base + 0x3623670u, g1)) enq(g1, 0x1000);

    int hits = 0; long reads = 0; const long kBudget = 300000;
    while (qh < qt && reads < kBudget) {
        const std::uintptr_t o = q[qh].a; const unsigned win = q[qh].win; ++qh;
        for (unsigned k = 0; k < win && reads < kBudget; k += 8) {
            std::uintptr_t w; ++reads;
            if (!rd(o + k, w)) continue;
            const char* m = match(w);
            if (m) {
                std::uintptr_t ovt = 0; rd(o, ovt);
                ++hits;
                SMBWAP_LOG_INFO("HOLDER obj=0x%llx vt(g)=0x%llx +0x%x val=0x%llx %s",
                                (unsigned long long)o, toG(ovt), k,
                                (unsigned long long)w, m);
            }
            enq(w & ~3ULL, 0x800);
        }
    }
    SMBWAP_LOG_INFO("holderSearch done: %d hit(s), %d obj(s), %ld read(s) "
                    "(q drained=%d)", hits, ns, reads, (qh >= qt) ? 1 : 0);
}

// gate-entry session 5 — course-sequence PARENT persistence diagnostic
// (log-only, one-shot, QueryMemory-safe).  Runs DURING PLAY (courseManager()
// valid) — unlike anchorSearch which ran at the exit moment.  Answers the
// gate-entry endgame question: the ExitCourseMgr controller (ctx, vtable NSO
// +0x34a8200) is TRANSIENT, but is its host PARENT sub-sequence (vtable NSO
// +0x3517068) — or the grandparent seq container (NSO +0x33f9660) — PERSISTENT
// and reachable while the player is in a course?  If so we can drive its
// transition into the exit state instead of latching the transient ctx.
//
// Three independent probes, all reads gated by svc::QueryMemory:
//   (A) read the latched parent's vtable during play (cheap, decisive-if-+ve);
//   (B) climb the al host-chain (+0x08) up from live objects (courseMgr, the
//       player-tick args, the latched parent) logging each level's Ghidra
//       vtable + flagging the 3 target classes;
//   (C) scan the engine globals + scene for the 3 target vtables AND census
//       the distinct main-module vtables reachable within 2 hops (so even a
//       partial reach names which persistent al objects hang off the scene).
void seqPersistDiag(std::uintptr_t base, std::uintptr_t courseMgr,
                    std::uintptr_t p1, std::uintptr_t p2) {
    if (base == 0) { SMBWAP_LOG_INFO("seqPersistDiag: base==0, skip"); return; }
    const std::uintptr_t kCtxVt = base + 0x34a8200u;   // ExitCourseMgr ctrl
    const std::uintptr_t kParVt = base + 0x3517068u;   // parent sub-sequence
    const std::uintptr_t kGrpVt = base + 0x33f9660u;   // grandparent container

    auto ptrish = [](std::uintptr_t p) {
        return p >= 0x2000000000ULL && p < 0x2800000000ULL && (p & 7) == 0;
    };
    std::uintptr_t okBase = 1, okEnd = 0;
    auto rd = [&](std::uintptr_t p, std::uintptr_t& out) -> bool {
        if (!(p >= okBase && p + 8 <= okEnd)) {
            hk::svc::MemoryInfo info; std::uint32_t pg;
            if (!hk::svc::QueryMemory(&info, &pg,
                    static_cast<::ptr>(p)).succeeded()) { okBase = 1; okEnd = 0; return false; }
            if ((static_cast<std::uint32_t>(info.permission)
                 & hk::svc::MemoryPermission_Read) == 0) { okBase = 1; okEnd = 0; return false; }
            okBase = info.base_address;
            okEnd  = info.base_address + info.size;
            if (!(p >= okBase && p + 8 <= okEnd)) return false;
        }
        out = *reinterpret_cast<std::uintptr_t*>(p);
        return true;
    };
    auto vtOf = [&](std::uintptr_t obj, std::uintptr_t& vt) -> bool {
        if (!ptrish(obj)) return false;
        if (!rd(obj, vt)) return false;
        return vt >= base && vt < base + 0x4000000ULL;  // a main-module vtable
    };
    auto toG = [&](std::uintptr_t rt) -> unsigned long long {
        return rt ? 0x7100000000ULL + (rt - base) : 0ULL;
    };
    auto flag = [&](std::uintptr_t vt) -> const char* {
        if (vt == kCtxVt) return " <== ctx(ExitCourseMgr ctrl)";
        if (vt == kParVt) return " <== PARENT sub-sequence";
        if (vt == kGrpVt) return " <== GRANDPARENT seq container";
        return "";
    };

    SMBWAP_LOG_INFO("seqPersistDiag: B_main=0x%llx targets ctx=0x%llx par=0x%llx grp=0x%llx",
                    (unsigned long long)base, (unsigned long long)kCtxVt,
                    (unsigned long long)kParVt, (unsigned long long)kGrpVt);

    // (A) latched-parent read during play (needs a prior natural course-out
    // to have latched a parent; empty on a fresh boot+play).
    {
        std::uintptr_t lp = reinterpret_cast<std::uintptr_t>(probe::courseSeqParent());
        std::uintptr_t vt = 0;
        bool ok = lp != 0 && vtOf(lp, vt);
        SMBWAP_LOG_INFO("  (A) latched parent=0x%llx readableObj=%d vt(ghidra)=0x%llx%s",
                        (unsigned long long)lp, ok ? 1 : 0,
                        (unsigned long long)(ok ? toG(vt) : 0), ok ? flag(vt) : "");
    }

    // (D) DEV-ONLY transience check.  The exit-time sequence tree is stable
    // across dev sessions (ctx 0x20bf799330; the dead-end test already saw ctx
    // read vt=0 mid-play).  Read the PARENT and GRANDPARENT slots too — they
    // are SEPARATE objects (ctx's host + container), so if the container
    // persists during play they'd still hold their vtables here, whereas the
    // transient ctx reads 0.  Settles "whole exit tree transient" vs "container
    // persists".  Every read QueryMemory-guarded; harmless if unmapped/reused.
    {
        struct DevAddr { const char* name; std::uintptr_t addr; };
        const DevAddr da[] = {
            {"ctx@0x20bf799330",         0x20bf799330ULL},
            {"parent@0x20bf798dc0",      0x20bf798dc0ULL},
            {"grandparent@0x20bf796368", 0x20bf796368ULL},
        };
        for (const auto& d : da) {
            std::uintptr_t vt = 0;
            const bool ok = rd(d.addr, vt);
            const bool inMod = ok && vt >= base && vt < base + 0x4000000ULL;
            SMBWAP_LOG_INFO("  (D) %s readable=%d vt=0x%llx vt(ghidra)=0x%llx%s",
                            d.name, ok ? 1 : 0, (unsigned long long)(ok ? vt : 0),
                            (unsigned long long)(inMod ? toG(vt) : 0),
                            inMod ? flag(vt) : "");
        }
    }

    // (B) host-chain climb (+0x08) from a few live seeds.
    struct ClimbSeed { const char* name; std::uintptr_t root; };
    const ClimbSeed climbs[] = {
        {"courseMgr", courseMgr},
        {"playerTick.p1", p1},
        {"playerTick.p2", p2},
        {"latchedParent", reinterpret_cast<std::uintptr_t>(probe::courseSeqParent())},
    };
    for (const auto& c : climbs) {
        std::uintptr_t vt;
        if (!vtOf(c.root, vt)) continue;
        SMBWAP_LOG_INFO("  (B) climb from %s=0x%llx:", c.name,
                        (unsigned long long)c.root);
        std::uintptr_t cur = c.root;
        for (int lvl = 0; lvl < 14; ++lvl) {
            std::uintptr_t cvt;
            if (!vtOf(cur, cvt)) break;
            SMBWAP_LOG_INFO("      L%d obj=0x%llx vt(ghidra)=0x%llx%s", lvl,
                            (unsigned long long)cur, (unsigned long long)toG(cvt),
                            flag(cvt));
            std::uintptr_t up;
            if (!rd(cur + 0x08, up) || up == cur || !ptrish(up)) break;
            cur = up;
        }
    }

    // (C) scan seeds for the target vtables + census distinct vtables.
    std::uintptr_t scene = 0, scene40 = 0, areaMgr = 0, g1 = 0,
                   a88 = 0, d90 = 0, cinmgr = 0;
    rd(base + 0x3625850u, scene);
    if (ptrish(scene)) rd(scene + 0x40, scene40);
    rd(base + 0x3628398u, areaMgr);
    rd(base + 0x3623670u, g1);
    rd(base + 0x3630a88u, a88);
    rd(base + 0x3628d90u, d90);
    rd(base + 0x3630d28u, cinmgr);
    struct Seed { const char* name; std::uintptr_t root; unsigned win; bool hop2; };
    const Seed seeds[] = {
        {"scene",       scene,    0x1000, false},
        {"scene+0x40",  scene40,  0x2c00, false},
        {"areaMgr",     areaMgr,  0x300,  true},
        {"g1",          g1,       0x300,  true},
        {"DAT_3630a88", a88,      0x300,  true},
        {"DAT_3628d90", d90,      0x300,  true},
        {"courseInMgr", cinmgr,   0x300,  true},
        {"courseMgr",   courseMgr,0x300,  true},
    };
    constexpr int kCensusMax = 48;
    std::uintptr_t census[kCensusMax]; int nCensus = 0; int targets = 0;
    auto consider = [&](std::uintptr_t obj, const char* sname, unsigned k,
                        unsigned j, bool two) {
        std::uintptr_t vt;
        if (!vtOf(obj, vt)) return;
        const char* fl = flag(vt);
        if (fl[0] != '\0') {
            ++targets;
            if (two) SMBWAP_LOG_INFO("  (C) TARGET %s+0x%x->+0x%x obj=0x%llx%s",
                                     sname, k, j, (unsigned long long)obj, fl);
            else SMBWAP_LOG_INFO("  (C) TARGET %s+0x%x obj=0x%llx%s",
                                 sname, k, (unsigned long long)obj, fl);
        }
        for (int i = 0; i < nCensus; ++i) if (census[i] == vt) return;
        if (nCensus < kCensusMax) {
            census[nCensus++] = vt;
            SMBWAP_LOG_INFO("  (C) census vt(ghidra)=0x%llx via %s+0x%x obj=0x%llx",
                            (unsigned long long)toG(vt), sname, k,
                            (unsigned long long)obj);
        }
    };
    for (const auto& s : seeds) {
        if (!ptrish(s.root)) continue;
        std::uintptr_t svt;
        if (!vtOf(s.root, svt)) continue;
        for (unsigned k = 0; k < s.win; k += 8) {
            std::uintptr_t w;
            if (!rd(s.root + k, w)) continue;
            consider(w, s.name, k, 0, false);
            if (s.hop2 && w != s.root && ptrish(w)) {
                std::uintptr_t wvt;
                if (!vtOf(w, wvt)) continue;
                for (unsigned j = 0; j < 0x300; j += 8) {
                    std::uintptr_t w2;
                    if (!rd(w + j, w2)) continue;
                    consider(w2, s.name, k, j, true);
                }
            }
        }
    }
    SMBWAP_LOG_INFO("seqPersistDiag done: %d target hit(s), %d distinct vtable(s)",
                    targets, nCensus);
}

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
            // PhaseA finding: state 0x00ff003700000084 (player-
            // controlled exit) fires on player-decided scene transitions
            // — course-in, world-map travel, palace, Poplin shop entry.
            // Aborting this Nerve activation does NOT block course-in
            // (entry has multiple driving Nerves; this one is just a
            // notification).  See docs/royal-seed-phase-a-findings.md
            // for the full negative result.
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

        // gate-entry session 3 — capture the natural course-out trigger.
        // RequestEventCourseExitByAreaTag fires when the player triggers a
        // course-out (pause→"Return to World Map", give-up, death w/ no
        // lives); `nerve` is the course-sequence controller we must drive
        // for the bounce.  Log-only: dump the controller head + the
        // NerveKeeper at +0x68 so we can RE (a) its reachable-from-a-global
        // path and (b) the heap exit-Nerve object it stores.  Bounded so a
        // give-up loop can't flood the ring.
        if (vt_off == kVtableOff_CourseExitByAreaTag && nerve) {
            static int s_exit_dumps = 0;
            if (s_exit_dumps < 8) {
                ++s_exit_dumps;
                SMBWAP_LOG_INFO(
                    "COURSE_EXIT_BY_AREATAG: controller=%p (fire #%d, "
                    "capture #%d)", nerve, s_fires, s_exit_dumps);
                dumpWords("  ctrl", nerve, 16);  // controller +0x00..0x80
                void* keeper = *reinterpret_cast<void* const*>(
                    reinterpret_cast<const std::uint8_t*>(nerve) + 0x68);
                SMBWAP_LOG_INFO("  keeper@+0x68 = %p", keeper);
                dumpWords("  keeper", keeper, 12);  // keeper +0x00..0x60
            }
        }

        nerveActivateOnceHook.orig(nerve);
    });

// gate-entry session 6 — grandparent OWNER finder gate.  LOG-ONLY / READ-ONLY
// (QueryMemory-gated), fired once at the natural course-out from inside
// exitCourseMgrBodyHook.  ✅ RAN (capture #1, 2026-05-31): 0 owner hits / 0 fwd
// hits — the grandparent has NO owner back-pointer and the shallow g1 census
// does not hold it; BUT it pinned the live persistent controller (g1 vt
// +0x349d300, code 0x7101bae…).  Turned OFF; superseded by the g1-controller
// state diff below (kG1CtrlDiag).  Left in place for reference / re-enable.
constexpr bool kGrandparentOwnerProbe = false;

// gate-entry session 6 capture #2 — persistent g1 course-sequence controller
// state DIFF.  LOG-ONLY / READ-ONLY (QueryMemory-gated).  Dumps g1+0x00..0x160
// at TWO moments — during PLAY (fired ~200 in-course ticks in from
// playerTickLatchHook) and at the EXIT moment (from exitCourseMgrBodyHook) — so
// the field that flips on course-out (the current/next-scene ptr, NerveKeeper
// step word, or course-out request flag) can be read off the diff.  That field
// is the persistent lever.  Safe to ship ON; REVERT to false after the capture.
// Procedure: boot -> enter ANY course -> play ~10 s (PLAY dump) -> pause ->
// "Return to World Map" (EXIT dump).  Grab every `g1ctrlDump[PLAY]` and
// `g1ctrlDump[EXIT]` line (strip NULs) and diff them.
//
// ✅ RAN (capture #2, 2026-05-31): PLAY and EXIT dumps were BYTE-IDENTICAL —
// g1's first 0x160 B does not change on course-out ⇒ g1 root is NOT the direct
// holder/driver of the transient exit tree.  Turned OFF; superseded by the wide
// holder reachability search below (kHolderSearch).
constexpr bool kG1CtrlDiag = false;

// gate-entry session 6 capture #3 — WIDE holder reachability search.
// LOG-ONLY / READ-ONLY (QueryMemory-gated, ≤512 objs / ≤300k reads).  Fired at
// the EXIT moment from exitCourseMgrBodyHook: BFS the persistent object graph
// from scene (DAT_3625850) + scene+0x40 + areaMgr (DAT_3628398) + g1
// (DAT_3623670), reporting any field that points to/into the transient exit
// tree (ctx/parent/grandparent).  The holder object's vtable (Ghidra) is the
// lever target.  Safe to ship ON; REVERT to false after the capture.
// Procedure: boot -> enter ANY course -> pause -> "Return to World Map".  Grab
// every `holderSearch`/`HOLDER ` line (strip NULs).
//
// ✅ RAN (capture #3, 2026-05-31): 0 hit(s), 512 obj(s), 133376 read(s), queue
// drained — the BFS exhausted the persistent object graph reachable from
// scene/scene+0x40/areaMgr/g1 (512 objs) and found NOTHING pointing to/into the
// exit tree.  ⇒ the transient exit sequence is not referenced by a plain
// object-pointer chain from those roots (held via an al update-list iterator /
// non-object intermediary / a different root).  Reverted to false (INERT).
// Next direction: the PauseResult data lever (see docs Session 6 decision tree).
constexpr bool kHolderSearch = false;

// ExitCourseMgr body @ NSO +0x01be3a5c (FUN_7101be3a5c).
//
// gate-entry session 3, log-only.  This is the Nerve-execute body that, in
// vanilla, performs the course-out teardown: it walks the course-out global
// chain (DAT_7103628398 → +0x30 → +8 → *(+8) → +0x118 → courseMgr) and calls
// the course-out executor FUN_7101a612cc.  We hook it purely to (a) confirm
// ordering vs. the RequestEventCourseExitByAreaTag controller capture in
// nerveActivateOnceHook and (b) snapshot the live course-out chain (the two
// globals named in the handoff) so we can drive it ourselves for the bounce.
// Calls Orig unconditionally — no behavior change.  Bounded to a few fires.
HkTrampoline<void, void*> exitCourseMgrBodyHook = hk::hook::trampoline(
    [](void* ctx) -> void {
        // Latch the course-sequence controller for the bounce.  `ctx` is the
        // live ExitCourseMgr-body arg (validated inside by vtable); refreshed
        // on every natural course-out so the latched pointer stays current.
        probe::latchCourseSeqController(ctx);
        // Also latch the host PARENT sub-sequence (ctx+0x08).  ctx is a live
        // object here so reading +0x08 is safe; latchCourseSeqParent validates
        // the parent vtable (NSO +0x3517068) before storing.  Used by the
        // gate-entry session-5 persistence diagnostic in PlayerTickLatch to
        // test whether a parent-class object still lives at that address
        // during play (unlike the transient ctx).
        if (ctx != nullptr) {
            void* parent = *reinterpret_cast<void* const*>(
                reinterpret_cast<const std::uint8_t*>(ctx) + 0x08);
            probe::latchCourseSeqParent(parent);
        }

        static int s_dumps = 0;
        if (s_dumps < 8) {
            ++s_dumps;
            SMBWAP_LOG_INFO("EXIT_COURSE_MGR_BODY: ctx=%p (capture #%d)",
                            ctx, s_dumps);
            dumpWords("  ctx", ctx, 16);  // ctx +0x00..0x80

            ::ptr base = 0;
            const auto* main_mod = hk::ro::getMainModule();
            if (main_mod) base = main_mod->range().start();
            if (base != 0) {
                const auto b = static_cast<std::uintptr_t>(base);
                // Confirm the runtime main base (B_main).  Static RE pinned it
                // at 0x08504000 on dev Ryujinx (memory reference_smbw_runtime_
                // main_base); this lets us map dumped 0x0bxxxxxx vtables to
                // Ghidra.  Logged so a new env (real HW ASLR) re-pins it.
                SMBWAP_LOG_INFO("  B_main(getMainModule) = 0x%llx",
                                static_cast<unsigned long long>(b));
                // DAT_7103628398 — course-out chain anchor (+0x3628398).
                void* g0 = *reinterpret_cast<void* const*>(b + 0x3628398u);
                SMBWAP_LOG_INFO("  DAT_3628398 = %p", g0);
                dumpWords("  g0", g0, 8);   // chain head +0x00..0x40
                // DAT_7103623670 — sibling global named in the session-3
                // handoff for the controller's global path (+0x3623670).
                void* g1 = *reinterpret_cast<void* const*>(b + 0x3623670u);
                SMBWAP_LOG_INFO("  DAT_3623670 = %p", g1);
                dumpWords("  g1", g1, 8);
            }
            // Cross-check: the course manager resolved via the known walk.
            void* cmgr = probe::courseManager();
            SMBWAP_LOG_INFO("  courseManager() = %p", cmgr);

            // capture #2b — ancestor climb + global anchor search.  Gated on a
            // VALID course manager: courseManager() is non-null ONLY inside a
            // fully-loaded course (the safe context, exactly where capture #1/#2
            // succeeded); it is null on the world map and mid-transition, where
            // walking the sequence tree could touch half-constructed state.
            // Run-once via its OWN latch (not s_dumps==1) so a world-map exit
            // firing first doesn't consume the one shot.
            static bool s_anchor_done = false;
            if (!s_anchor_done && ctx != nullptr && cmgr != nullptr) {
                s_anchor_done = true;
                const auto cptr = reinterpret_cast<std::uintptr_t>(ctx);
                const std::uintptr_t parent =
                    *reinterpret_cast<std::uintptr_t*>(cptr + 0x08);
                SMBWAP_LOG_INFO("  parent(ctx+0x08) = 0x%llx",
                                static_cast<unsigned long long>(parent));
                if (parent >= 0x2000000000ULL && parent < 0x2800000000ULL) {
                    dumpWords("  parent", reinterpret_cast<void*>(parent), 32);
                }
                if (kHolderSearch) {
                    // session-6 capture #3: BFS the persistent object graph for
                    // whoever holds/ticks the transient exit tree.
                    holderSearchWide(static_cast<std::uintptr_t>(base), cptr);
                } else if (kG1CtrlDiag) {
                    // session-6 capture #2: EXIT-side snapshot of the persistent
                    // course-sequence controller (g1).  Diff vs. the PLAY-side
                    // snapshot from playerTickLatchHook to find the course-out
                    // trigger field.
                    dumpG1CourseSeqCtrl("EXIT", static_cast<std::uintptr_t>(base));
                } else if (kGrandparentOwnerProbe) {
                    // session-6 capture #1: find the persistent owner that pushes
                    // the exit scene (dumps grandparent + g1 controller census).
                    grandparentOwnerProbe(static_cast<std::uintptr_t>(base), cptr);
                } else {
                    anchorSearch(static_cast<std::uintptr_t>(base), cptr, parent,
                                 reinterpret_cast<std::uintptr_t>(cmgr));
                }
            }
        }
        exitCourseMgrBodyHook.orig(ctx);
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
// Phase B course-out ("bounce") smoke test (2026-05-30).  RESULT: the
// direct call to the course-out executor FUN_7101a612cc is SAFE (no crash,
// courseManager chain resolved, requestCourseOut->1) but INCOMPLETE — it
// runs the teardown/prep only and does NOT change scene back to the world
// map.  In vanilla the ExitCourseMgr Nerve runs FUN_7101a612cc AND then
// advances the course-sequence controller's Nerve, and that state advance
// is what actually loads the world map.  A direct call skips the advance,
// so nothing visible happens.  exit_type 0 vs 1 makes no difference (the
// exit_type==1 extras are gmd stat-recording, not a scene change).
//
// Disabled.  The COMPLETE bounce was since found (capture #1..#3, 2026-05-31):
// the ExitCourseMgr Nerve body FUN_7101be3a5c(ctx) does the teardown AND the
// controller-Nerve step advance that loads the world map — see
// probe::bounceCourseOut() and the kBounceSmokeTest below, which supersede
// this requestCourseOut()-only probe.
constexpr bool kCourseOutSmokeTest = false;

// Bounce smoke test (gate-entry, 2026-05-31).  Verifies that
// probe::bounceCourseOut() — FUN_7101be3a5c on the latched course-sequence
// controller — performs a COMPLETE course-out (the scene change that the
// requestCourseOut() teardown alone was missing).  Gated OFF by default.
// Test flow: enter course A → pause→"Return to World Map" (latches ctx) →
// enter course B → ~300 ticks later fire bounceCourseOut().
//
// ❌ TESTED 2026-05-31, RESULT: latch approach is DEAD.  Diagnostic showed the
// latched address (0x20bf799330) is readable but its vtable is 0x0 the whole
// time during course B — the ExitCourseMgr controller is a TRANSIENT object
// created when "Return to World Map" is invoked and freed afterward, NOT a
// persistent one.  A pre-latched ctx can never be valid mid-play;
// bounceCourseOut() correctly returned 0 (vtable re-check rejected the freed
// object — no crash).  The sequence tree has no cheap global anchor either
// (capture #3 = 0 hits; factory FUN_7100229fa4 is a stateless al SceneFactory).
// Conclusion: drive the PERSISTENT pause-menu "Return to World Map" request
// instead (next session).  Reverted to false; primitives kept for reference.
constexpr bool kBounceSmokeTest = false;

// gate-entry session 5 — course-sequence PARENT persistence diagnostic.
// LOG-ONLY / READ-ONLY (QueryMemory-guarded).  Was the intentional one-shot
// test the user asked for ("read the PARENT slot during play").
//
// ✅ RESULT (2026-05-31, SETTLED): the parent is NOT persistent.  Probe (D)
// read the known exit-tree addresses DURING PLAY: ctx@0x20bf799330 vt=0,
// parent@0x20bf798dc0 vt=0xc (freed), grandparent@0x20bf796368 vt=0 — all
// mapped-but-empty; at the exit moment those same slots hold the sequence-tree
// vtables (0x71034a8200 / 0x7103517068 / 0x71033f9660).  Probe (C) found 0
// objects of those classes reachable during play (every live object was in the
// 0x20dab…/0x2092…/0x209fab…/0x20d2fd… clusters; the exit tree lives in
// 0x20bf79…, absent during play).  ⇒ the ENTIRE exit sequence tree
// (ctx + parent + grandparent) is TRANSIENT — built on course-out into a fixed
// arena, torn down after.  No persistent object up that chain to drive.  Next:
// drive the persistent layer that BUILDS the exit scene — the al SceneController
// (g1 = DAT_3623670; SceneFactory vtable 0x710349cf38, ctor FUN_7100942e70;
// exit-scene create = factory table 0x710349cf38 slot +0x20 == FUN_7100229fa4;
// ctx state-key 0x99137dfe).  See docs/gate-entry-session3-handoff.md "Session 5".
//
// Reverted to false (diag inert; code kept for reference / easy re-enable).
constexpr bool kSeqPersistDiag = false;

HkTrampoline<void, void*, void*> playerTickLatchHook = hk::hook::trampoline(
    [](void* param_1, void* param_2) -> void {
        probe::serviceDeathLink(param_1);
        playerTickLatchHook.orig(param_1, param_2);

        // gate-entry session 5 — one-shot parent-persistence diagnostic.
        // Requires: save loaded, in a loaded course (courseManager() valid),
        // and NOT mid-transition; counts ~200 in-course ticks so the scene
        // tree is fully constructed before the scan; resets on the world map
        // so it only fires once we're settled in a course.
        if (kSeqPersistDiag) {
            // Fire ONCE per course-entry (~200 ticks in), re-arming on the
            // world map, capped at 6 total fires.  This lets the proven
            // protocol (course A -> pause->Return-to-Map [latches parent] ->
            // course B) exercise probe A's latched-parent read in course B,
            // while still capturing the census/transience probes in any course.
            static std::atomic<int>  s_dt{0};
            static std::atomic<bool> s_firedThisCourse{false};
            static std::atomic<int>  s_totalFires{0};
            if (probe::isSaveLoaded() && !probe::isInSceneTransitionWindow()) {
                void* cmgr = probe::courseManager();
                if (cmgr != nullptr) {
                    if (!s_firedThisCourse.load(std::memory_order_relaxed)
                        && s_totalFires.load(std::memory_order_relaxed) < 6) {
                        const int n = s_dt.fetch_add(1, std::memory_order_relaxed) + 1;
                        if (n >= 200) {
                            s_firedThisCourse.store(true, std::memory_order_relaxed);
                            const int fno = s_totalFires.fetch_add(
                                1, std::memory_order_relaxed) + 1;
                            ::ptr base = 0;
                            const auto* mm = hk::ro::getMainModule();
                            if (mm) base = mm->range().start();
                            SMBWAP_LOG_INFO(
                                "seqPersistDiag: firing #%d at in-course tick #%d "
                                "courseMgr=%p p1=%p p2=%p", fno, n, cmgr,
                                param_1, param_2);
                            seqPersistDiag(
                                static_cast<std::uintptr_t>(base),
                                reinterpret_cast<std::uintptr_t>(cmgr),
                                reinterpret_cast<std::uintptr_t>(param_1),
                                reinterpret_cast<std::uintptr_t>(param_2));
                        }
                    }
                } else {
                    s_dt.store(0, std::memory_order_relaxed);            // reset tick count on map
                    s_firedThisCourse.store(false, std::memory_order_relaxed);  // re-arm next course
                }
            }
        }

        // gate-entry session 6 capture #2 — PLAY-side snapshot of the persistent
        // course-sequence controller (g1).  Same firing discipline as the diag
        // above: once per course-entry, ~200 in-course ticks in, re-arms on the
        // world map, capped.  Diff this against the EXIT-side dump from
        // exitCourseMgrBodyHook to find the course-out trigger field on g1.
        if (kG1CtrlDiag) {
            static std::atomic<int>  s_gt{0};
            static std::atomic<bool> s_gFired{false};
            static std::atomic<int>  s_gTotal{0};
            if (probe::isSaveLoaded() && !probe::isInSceneTransitionWindow()) {
                void* cmgr = probe::courseManager();
                if (cmgr != nullptr) {
                    if (!s_gFired.load(std::memory_order_relaxed)
                        && s_gTotal.load(std::memory_order_relaxed) < 6) {
                        const int n = s_gt.fetch_add(1, std::memory_order_relaxed) + 1;
                        if (n >= 200) {
                            s_gFired.store(true, std::memory_order_relaxed);
                            const int fno = s_gTotal.fetch_add(
                                1, std::memory_order_relaxed) + 1;
                            ::ptr base = 0;
                            const auto* mm = hk::ro::getMainModule();
                            if (mm) base = mm->range().start();
                            SMBWAP_LOG_INFO(
                                "g1ctrlDump: PLAY firing #%d at in-course tick #%d",
                                fno, n);
                            dumpG1CourseSeqCtrl(
                                "PLAY", static_cast<std::uintptr_t>(base));
                        }
                    }
                } else {
                    s_gt.store(0, std::memory_order_relaxed);        // reset on map
                    s_gFired.store(false, std::memory_order_relaxed);  // re-arm next course
                }
            }
        }

        if (kCourseOutSmokeTest) {
            static std::atomic<int> s_pt{0};
            const int n = s_pt.fetch_add(1, std::memory_order_relaxed) + 1;
            // Diagnostic: confirm the course-manager chain resolves while
            // the player tick runs (i.e. we're in a loaded course).
            if (n == 1 || n == 60 || n == 120) {
                SMBWAP_LOG_INFO(
                    "PhaseB courseout smoke: playerTick #%d courseManager=%p",
                    n, probe::courseManager());
            }
            if (n == 180) {
                SMBWAP_LOG_INFO(
                    "PhaseB courseout smoke: calling requestCourseOut() at "
                    "playerTick #%d", n);
                const bool ok = probe::requestCourseOut();
                SMBWAP_LOG_INFO(
                    "PhaseB courseout smoke: requestCourseOut -> %d "
                    "(one-shot, disarmed)", ok ? 1 : 0);
            }
        }

        // Bounce smoke test + DIAGNOSTIC (2026-05-31): the latched-at-exit ctx
        // was stale mid-play (bounceCourseOut -> 0, vtable re-check failed).
        // Log the ACTUAL vtable at the latched address at a few ticks in the
        // re-entered course vs. the expected controller vtable, to learn
        // whether the controller is per-course / transient.  Then still attempt
        // the bounce at tick 300.
        if (kBounceSmokeTest &&
            probe::courseSequenceController() != nullptr) {
            static std::atomic<int> s_bt{0};
            static std::atomic<bool> s_fired{false};
            if (!s_fired.load(std::memory_order_relaxed)) {
                void* cmgr = probe::courseManager();
                if (cmgr != nullptr) {
                    const int n =
                        s_bt.fetch_add(1, std::memory_order_relaxed) + 1;
                    if (n == 1 || n == 150 || n == 300) {
                        void* ctrl = probe::courseSequenceController();
                        ::ptr base = 0;
                        const auto* mm = hk::ro::getMainModule();
                        if (mm) base = mm->range().start();
                        // Fault-safe read of *(ctrl): the latched address may
                        // be freed/unmapped mid-play (that's what we're testing).
                        hk::svc::MemoryInfo mi; std::uint32_t pgi;
                        std::uintptr_t vt = 0;
                        const bool readable = ctrl != nullptr &&
                            hk::svc::QueryMemory(&mi, &pgi,
                                reinterpret_cast<::ptr>(ctrl)).succeeded() &&
                            (static_cast<std::uint32_t>(mi.permission)
                             & hk::svc::MemoryPermission_Read) != 0;
                        if (readable) vt = *reinterpret_cast<std::uintptr_t*>(ctrl);
                        SMBWAP_LOG_INFO(
                            "PhaseB bounce diag: tick #%d ctrl=%p readable=%d "
                            "vt=0x%llx expect=0x%llx cmgr=%p", n, ctrl,
                            readable ? 1 : 0,
                            static_cast<unsigned long long>(vt),
                            static_cast<unsigned long long>(
                                static_cast<std::uintptr_t>(base) + 0x34a8200u),
                            cmgr);
                    }
                    if (n >= 300) {
                        s_fired.store(true, std::memory_order_relaxed);
                        const bool ok = probe::bounceCourseOut();
                        SMBWAP_LOG_INFO(
                            "PhaseB bounce smoke: bounceCourseOut -> %d "
                            "(one-shot, disarmed)", ok ? 1 : 0);
                    }
                } else {
                    s_bt.store(0, std::memory_order_relaxed);  // reset on map
                }
            }
        }
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
// FLOWER LOCK GATE — Phase A observability (REVERTED from force-lock)
// =========================================================================
//
// PHASE A NEGATIVE RESULT (2026-05-30): this Nerve only fires for the
// small subset of Wonder-Seed-locked course-points on the current
// world (3 cycling keys per world).  Forcing the lock byte to 0
// successfully blocks press-A on THOSE course-points, but regular
// courses and palaces have no equivalent gate Nerve — they enter
// freely.  Force-lock mode is therefore reverted to observability-only
// to avoid blocking the Wonder-Seed-locked-courses gameplay path.
// See docs/royal-seed-phase-a-findings.md.
//
// The hook stays installed so the course-key extraction
// remains available as a Phase B starting point if/when the engine-
// wide gate is found.  Calls Orig normally; just logs course keys.
//
// Original force-lock comment block preserved below for context:
//
// Phase A of the AP-controlled course-entry gating effort (see
// docs/royal-seed-gate-entry-design.md).  Trampolines on
// FUN_7100383418 (NSO +0x383418), the body shared by the two execute
// slots of the IsDisplayFlowerLockUI Nerve (vtable at NSO +0x33c1ad8).
//
// The decompile (docs/royal-seed-gate-entry-design.md) shows the body
// always returns 1 in normal operation — the lock decision lives in
// a side-effect byte at *((this+0x20) & ~3).  Phase A unconditionally
// writes 0 to that byte (= locked) and returns 1 without calling
// Orig, to verify that:
//
//   1. We can safely intercept the function (prologue is clean — five
//      sub/stp instructions, no PC-relative loads per CLAUDE.md
//      gotcha #2 — so the trampoline relocation is safe).
//   2. Forcing the lock byte to 0 actually blocks course-point entry
//      (the consumer of that byte is the gating decision; if entry is
//      blocked, Phase B can build the course → AP-index map and
//      Phase C wires it to a bridge-controlled bitfield).
//
// What we expect to see in Ryujinx after this lands:
//   * Boot to the world map.
//   * Every course point on every world shows locked / refuses
//     press-A entry.
//   * Log line "PhaseA flower_lock force-locked" fires repeatedly
//     (the UI tick polls the gate Nerve continuously).
//   * The first 30 fires also dump the extracted course key from
//     the [+8]→[+8]→[+0x1f8]→[+0x350]→[+0x108] walk — Phase B
//     reuses that walk to build the course → AP-index map.
//
// What the Nerve's two execute wrappers do with the return value:
//   * FUN_71003833dc (default-fail): state = bits 30+31, then
//     `if ((ret & 1) != 0) state = bit 31 only` — so return 1 ⇒
//     SUCCESS state (bit 31 set, bit 30 cleared).
//   * FUN_710085134c (default-success): state = bit 31, then
//     `if ((ret & 1) == 0) state = bits 30+31` — so return 1 ⇒
//     SUCCESS state.
// Both wrappers treat return=1 as "Nerve processing complete,
// proceed to next state" — exactly what we want.
HkTrampoline<unsigned long long, void*> flowerLockGateBodyHook =
    hk::hook::trampoline(
        [](void* nerve) -> unsigned long long {
            // Bail (let Orig run normally) if the Nerve `this` is null —
            // we'd just be replicating the function's own null-pointer
            // fast-fail path that returns 0.  Note: returning 0 here
            // would make the wrappers see "failure" state, which has no
            // bearing on the lock byte but might confuse the Nerve
            // scheduler if it special-cases bit 30.
            if (!nerve) {
                return flowerLockGateBodyHook.orig(nerve);
            }

            // Walk the pointer chain to the course key for Phase B
            // observability.  Bail (defer to Orig) if any link is null —
            // that means we're earlier in init than the Phase A guarantee
            // assumes, and forcing the lock byte without a valid course
            // context could hide a real engine init bug.
            std::uint64_t course_key = 0;
            do {
                auto* p0 = *reinterpret_cast<void**>(
                    static_cast<std::uint8_t*>(nerve) + 8);
                if (!p0) break;
                auto* p1 = *reinterpret_cast<void**>(
                    static_cast<std::uint8_t*>(p0) + 8);
                if (!p1) break;
                auto* p2 = *reinterpret_cast<void**>(
                    static_cast<std::uint8_t*>(p1) + 0x1f8);
                if (!p2) break;
                auto* p3 = *reinterpret_cast<void**>(
                    static_cast<std::uint8_t*>(p2) + 0x350);
                if (!p3) break;
                course_key = *reinterpret_cast<std::uint64_t*>(
                    static_cast<std::uint8_t*>(p3) + 0x108);
            } while (false);

            // PhaseA REVERTED: was force-writing 0 to the lock byte
            // at *((nerve+0x20) & ~3).  That works (verified
            // Ryujinx_..._14-43-14.log) but only for Wonder-Seed-
            // locked course-points which are a narrow subset.  Now
            // observability-only: read packed but don't write.
            const std::uint64_t packed = *reinterpret_cast<std::uint64_t*>(
                static_cast<std::uint8_t*>(nerve) + 0x20);
            char* lock_byte = reinterpret_cast<char*>(packed & ~3ULL);
            (void)lock_byte;  // available for future Phase B work

            // Bounded log.  The world-map UI ticks the gate Nerves
            // continuously while the player sits on the world map;
            // unbounded logging would flood the ring.  First 30 fires
            // verbose, then every 256th.
            static std::atomic<int> s_fires{0};
            const int n = s_fires.fetch_add(1, std::memory_order_relaxed)
                          + 1;
            if (n <= 5) {
                SMBWAP_LOG_INFO(
                    "PhaseA flower_lock observed (no-op): nerve=%p "
                    "course_key=0x%016llx packed=0x%016llx "
                    "lock_byte=%p (fire #%d)",
                    nerve,
                    static_cast<unsigned long long>(course_key),
                    static_cast<unsigned long long>(packed),
                    lock_byte, n);
            }

            // Call Orig — let the original gate logic run.
            return flowerLockGateBodyHook.orig(nerve);
        });

// =========================================================================
// NEED-BADGE-ID — Phase A observability (REVERTED from hijack)
// =========================================================================
//
// PHASE A NEGATIVE RESULT (2026-05-30): the
// GetNeedBadgeIdEnterCourseForDirectCourseIn Nerve never fires for
// regular courses or palaces in normal play (zero fires observed in
// Ryujinx_..._14-53-11.log).  It's narrow to badge-challenge courses
// only.  Hijacking it can't gate the courses we care about.
// Hook stays installed for observability — if it ever fires, we'd
// want to know.  Reverted from overwriting 0x20 to call-Orig + log
// only.  See docs/royal-seed-phase-a-findings.md.
//
// Original hijack comment block preserved below for context:
//
// Second Phase A probe (see royal-seed-gate-entry-design.md).  The
// flower-lock probe above turned out to only affect the
// IsDisplayFlowerLockUI Nerves — which control the seed-bar Wonder Seed
// lock on specific course points (the "needs N Wonder Seeds to unlock
// this course" UI), NOT every course-point's entry gate.  Locking them
// blocks crossing those particular seed-locked course points but leaves
// every unlocked course directly enterable.
//
// This probe trampolines the OTHER candidate identified during RE:
// FUN_7101bffe94 (NSO +0x1bffe94), slot 8 execute of the
// `GetNeedBadgeIdEnterCourseForDirectCourseIn` Nerve (vtable at
// NSO +0x34b9300).  In vanilla operation that Nerve writes -1 to its
// output slot (`*(this+0x20) & ~3` — same packed-pointer pattern as
// the flower-lock Nerve), meaning "no badge required".  The engine's
// existing badge-challenge gate consumes this value and refuses
// press-A entry if the player doesn't own the named badge.
//
// We call Orig (so the standard TLS/state init runs and -1 is written
// as a fallback), then overwrite the output slot with `0x42` — a
// badge ID the player almost certainly doesn't own on a fresh save.
// Three possible outcomes:
//
//   1. Press-A entry blocked + "you need badge ???" UI: the Nerve
//      output IS the badge-challenge gate input, our hook works,
//      Phase A is confirmed for per-course entry gating.  Phase C
//      can wire this to an AP-controlled per-course bitfield.
//
//   2. Press-A entry blocked, weird/silent UI: gate works but UI
//      doesn't render unknown badge IDs cleanly.  Acceptable for a
//      proof-of-concept; final design would pick a real but
//      always-locked badge or extend the badge UI.
//
//   3. Press-A entry still works: the Nerve only fires for
//      badge-CHALLENGE courses (a small subset of the world's
//      course-points), not regular courses or palaces.  Negative
//      result → we need a third hook target.  Likely candidates if
//      this falls through: the WipeCourseIn entry Nerve, the
//      NotifyStartCourseIn execute (NSO +0x88 75c4), or the input
//      handler on WorldMapPlayerControl.
HkTrampoline<void, void*> needBadgeIdEnterCourseExecuteHook =
    hk::hook::trampoline(
        [](void* nerve) -> void {
            // PhaseA REVERTED: was overwriting [+0x20] with fake
            // badge ID 0x42 to force-block via the badge-challenge
            // UI.  In testing this Nerve never fired for the courses
            // we care about, so the hijack had no effect.  Now
            // observability-only: call Orig + log if it ever fires.
            needBadgeIdEnterCourseExecuteHook.orig(nerve);

            static std::atomic<int> s_fires{0};
            const int n = s_fires.fetch_add(1, std::memory_order_relaxed)
                          + 1;
            if (n <= 30) {
                SMBWAP_LOG_INFO(
                    "PhaseA need_badge_id observed (no-op): nerve=%p "
                    "(fire #%d)", nerve, n);
            }
        });

// =========================================================================
// COURSE-IN ENTER GATE — Phase B block-all confirm test (2026-05-30)
// =========================================================================
//
// CourseInEnterGate @ NSO +0x164201c (FUN_710164201c) — the enterable
// check inside the world-map player's course-in state machine.  This is
// the universal pre-commit entry gate the Phase A session concluded did
// NOT exist; bulk static RE this session (now that
// GHIDRA_MCP_ALLOW_SCRIPTS=1 unblocks scripts) located it.  See the
// Phase B addendum in docs/royal-seed-phase-a-findings.md for the full
// derivation and the surrounding Nerve-state-machine map.
//
// Call chain (world-map walk-up entry):
//   free-walk -> press A -> CheckWorldMapPlayerGoToCoursePointCenter
//   (Nerve exec FUN_7101641fd4, NSO +0x1641fd4) -> THIS (its sole caller)
//
// What FUN_710164201c does (decompile):
//   FUN_710032aea4(nerve+0x28, 0);                 // publish "not ready"
//   info = resolve(**(nerve+0x20));                // current course point
//   if (!info) return 0;
//   key  = *(*(info+0x350)+0x108);                 // 8-byte course key
//   ent  = binsearch(courseStateRegistry, key);    // FUN_7100613870
//   if (!ent) return 0;
//   if (ent[0xa0]==0 && ent[0xb0]==0 &&
//       ent[0xc0]==0 && ent[0xd0]==0) {            // all 4 "lock" ptrs null
//       FUN_710032aea4(nerve+0x28, 1);             // publish "ready"
//       return 1;                                  // ENTER
//   }
//   return 0;                                       // BLOCK (vanilla lock)
//
// The wrapper (FUN_7101641fd4) sends the GoToCenter Nerve to fail-state
// (0xc0000000) when our return bit0 == 0, so course-in never begins — a
// clean PRE-commit block, no half-loaded scene (unlike the downstream
// load Nerves Phase A ruled out).  This gate fires for the generic
// "reached the node, can I enter?" check, so it is expected to cover
// regular courses AND palaces — the exact universality Phase A lacked.
//
// CONFIRM TEST: with kBlockAllCourseEntry == true we force EVERY course
// point to block.  Expected in Ryujinx:
//   * Boot to world map; walk onto any course point or palace, press A.
//   * The player figure walks to the node center, then entry is REFUSED
//     for every course type — proving the gate is universal.
//   * "PhaseB course_in_gate" log fires repeatedly; natural_allow shows
//     whether the vanilla gate would have permitted entry.
// Things to watch (each is a finding):
//   * Does the player gracefully return to free-walk after the refusal,
//     or get stuck re-running the GoToCenter Nerve (soft-lock)?
//   * Does any boot cutscene / first-visit world-map demo that relies on
//     this gate succeeding hang?
//
// Flip kBlockAllCourseEntry to false to revert to observability-only
// (call Orig, log, no behavior change) — same revert idiom as the
// Phase A hooks above.
//
// 2026-05-30: set to FALSE (log-only). Two test runs proved this gate
// NEVER FIRES on the walk-up course-entry path — the player entered a
// course (course_in PlayReport emitted) with zero `course_in_gate` log
// lines, while FlowerLockGateBody fired.  So FUN_710164201c is not on
// the entry path used (its sole caller, the GoToCoursePointCenter Nerve,
// didn't run).  The diagnostic that established this — log-only probes
// on CheckCourseInUIKey (FUN_710022a964) and SetCourseInFlag
// (FUN_710088cfdc) — has been removed; SetCourseInFlag was found to fire
// at commit but is hook-sensitive (crashed the commit->load window).
// This hook stays installed log-only as a harmless observability control
// while the real entry-decision point is located via static RE.  See the
// Phase B addendum in docs/royal-seed-phase-a-findings.md.
constexpr bool kBlockAllCourseEntry = false;

HkTrampoline<unsigned, void*> courseInEnterGateHook = hk::hook::trampoline(
    [](void* nerve) -> unsigned {
        // Always run Orig first: it owns all the refcount / read-lock
        // bookkeeping (FUN_71001d7520 acquires a hold on the resolved
        // course-point-info; FUN_710164201c releases it).  We only
        // override its two output signals afterward — never skip it.
        const unsigned natural = courseInEnterGateHook.orig(nerve);

        // Cheap distinct-course-point id for telemetry: the raw selected-
        // course-point handle the gate resolves from (**(nerve+0x20)).
        const void* cp = nullptr;
        if (nerve) {
            auto* p0 = *reinterpret_cast<void**>(
                static_cast<std::uint8_t*>(nerve) + 0x20);
            if (p0) cp = *reinterpret_cast<void**>(p0);
        }

        // Bounded log: this Nerve ticks continuously while the player is
        // on/approaching a node, so first 20 fires verbose then every
        // 512th, to avoid flooding the ring.
        static std::atomic<int> s_fires{0};
        const int n = s_fires.fetch_add(1, std::memory_order_relaxed) + 1;
        if (n <= 20 || (n & 0x1FF) == 0) {
            SMBWAP_LOG_INFO(
                "PhaseB course_in_gate: nerve=%p cp=%p natural_allow=%u "
                "block_all=%d (fire #%d)",
                nerve, cp, natural,
                static_cast<int>(kBlockAllCourseEntry), n);
        }

        if (kBlockAllCourseEntry) {
            // Reproduce the vanilla "blocked" output exactly: force the
            // "ready" byte at nerve+0x28 back to 0 (Orig leaves it at 1
            // only on the natural-allow path) and return 0 so the
            // GoToCenter Nerve fail-states.  Direct guarded write mirrors
            // FUN_710032aea4's packed-pointer handling (presence flag at
            // bit 1; byte at slot & ~3).  The TLS dirty-mark is omitted
            // intentionally — the consumer re-reads the byte each tick,
            // so the value (0) is what wins.
            if (nerve) {
                const std::uint64_t packed = *reinterpret_cast<std::uint64_t*>(
                    static_cast<std::uint8_t*>(nerve) + 0x28);
                if ((packed >> 1) & 1) {
                    auto* ready_byte =
                        reinterpret_cast<std::uint8_t*>(packed & ~3ULL);
                    if (ready_byte) *ready_byte = 0;
                }
            }
            return 0;
        }
        return natural;
    });

// =========================================================================
// COURSE-IN KILL-SWITCH GATE — pre-commit block via the engine's own
// "course-in forbidden" flag (2026-05-30 session 2, user-approved path)
// =========================================================================
//
// CheckCourseInUIKeyGate @ NSO +0x22a964 (FUN_710022a964) — the world-map
// course-in INPUT POLL.  Proven hook-stable in Phase B: it ran 512x as a
// log-only probe with no issue.  (The crash that session was the separate
// SetCourseInFlag trampoline, FUN_710088cfdc — NOT this function.)
//
// The input poll only starts a course-in when the engine's course-in
// manager flag is clear (decompile of FUN_710022a964):
//     if ((DAT_7103630d28 == 0 || *(char*)(DAT_7103630d28 + 0x70) == 0)
//          && <predicate/player-state>) {
//         publish requested=1; (**vtable[0x128])(param_1);   // drive commit
//     }
// DAT_7103630d28 = *(mainBase + 0x3630d28) is a stable sead singleton (the
// course-in/sequence manager, 0x98 bytes, allocated once in FUN_7100655408,
// vtable in the same 0x710348exxx region as the pause/exit nerve names).
// Byte +0x70 is the engine's own "course-in forbidden" latch — the engine
// sets it during cutscenes/demos.  Forcing it to 1 makes the poll refuse to
// start ANY course-in: a clean PRE-commit block, no scene load, none of the
// commit->load crash window that bit the SetCourseInFlag trampoline.
//
// We set +0x70 = 1 ONLY for the duration of Orig (the poll) and restore the
// prior value afterward — surgical: only the poll observes the forced value,
// nothing else in the frame is affected, and the poll only READS +0x70 (so
// the restore never clobbers an engine write).  This is also the exact shape
// the per-course version will take (gate +0x70 by the hovered node's AP
// status instead of unconditionally).
//
// CONFIRM TEST (kForceCourseInForbidden == true): walk onto any node/palace,
// press A -> entry refused for EVERY course type, player stays free to walk;
// no scene load.  "PhaseB coursein_killswitch" logs the natural +0x70 value
// (expected 0 when entry is normally allowed).  Flip to false for no-op
// (Orig only, no behavior change).  Per-course selectivity is the follow-up
// (reads the hovered node identity; see docs/royal-seed-phase-a-findings.md).
//
// 2026-05-30 confirm test PASSED (block held, no crash/soft-lock, manager
// resolved every poll).  Now FALSE so the build is playable while we run the
// log-only IDENTITY PROBE below (validate the hovered-node key chain before
// any per-course gating ships).
constexpr bool kForceCourseInForbidden = false;

// DAT_7103630d28 (course-in/sequence manager pointer) and its "course-in
// forbidden" byte.  manager = *(mainBase + offset); flag = manager + 0x70.
constexpr std::uint32_t kCourseInMgrGlobalNsoOffset = 0x3630d28;
constexpr std::uint32_t kCourseInForbiddenByteOff   = 0x70;

// "Looks like a live guest-heap pointer" check.  The earlier permissive
// range (0x1000000..0x1_0000_0000_0000) let a stale/garbage value through
// during a course-entry transition and probe v5 dereferenced it -> guest
// access violation (0xC0000005).  Tighten to the actual Ryujinx guest heap
// window (all observed live objects are 0x20_xxxxxxxx: courseManager
// 0x20dab50848, world-map actors 0x20bf7xxxxx) plus 8-byte alignment, so
// transient garbage is rejected instead of faulted.
// NOTE: this is the DEV (Ryujinx) heap base; real-hardware (M6) ASLR may
// place the heap elsewhere -- revisit before shipping the gate on console.
constexpr bool kLooksLikePtr(std::uintptr_t p) {
    return p >= 0x2000000000ULL && p < 0x2800000000ULL && (p & 7) == 0;
}

// Per-course gate v9 — CANCEL-RESERVATION (2026-05-30).  Block course-in for
// the hovered node by invalidating its "reserved open course" entry, the same
// thing id_fb names, AFTER the driver runs.  Validated live (probe v6):
// 1-1 -> id_fb 33, 1-2 -> 1362.  The real feature swaps the single test id for
// a bridge-pushed gated-id set (mirrors badge-sync).  -1 disables the gate.
// ⚠️ id_fb cross-boot stability unconfirmed; 33==1-1 holds for the test boot.
//
// 2026-05-30 v9 RESULT: clearing the reservation (id_fb -> -1) BROKE world-map
// movement entirely — the driver does NOT re-reserve, so the player loses its
// current-node anchor and can't move.  The "reserved open course" slot is
// load-bearing NAVIGATION state, not a separable entry target.  Combined with
// v7 (skip-driver freezes identity) and v8 (+0x30 clear: no block + teardown
// crash), conclusion: course-IN is woven into world-map navigation; no clean
// pre-commit chokepoint found.  Gate DISABLED pending a different strategy
// (likely the let-load-then-bounce approach).
constexpr std::int32_t kTestGatedIdFb = -1;  // -1 = gate disabled (safe build)

// World-map registry: scene = *(mainBase+0x3625850); registry = *(scene+0x40).
constexpr std::uint32_t kWorldMapSceneGlobalNsoOffset = 0x3625850;
// Player-0 fallback "reserved open course" slot: registry + 0x100; its id
// (the value id_fb reads) is at slot + 0x10.  FUN_7100191380 treats id == -1
// as "no reservation" -> the course-in resolver finds no course to enter.
constexpr std::uint32_t kReservedSlotIdOffset = 0x100 + 0x10;

HkTrampoline<void, void*> checkCourseInUiKeyGateHook = hk::hook::trampoline(
    [](void* param_1) -> void {
        // Run the poll + driver FIRST.  The driver (vtable[0x128] =
        // FUN_710074b578) RESERVES the hovered node (sets the slot id_fb reads)
        // — it must run every frame or the identity freezes (v7's +0x70
        // kill-switch skipped it and mis-gated every node).
        checkCourseInUiKeyGateHook.orig(param_1);

        if (kTestGatedIdFb < 0 && !kForceCourseInForbidden) return;  // gate off

        // Transition guard: the world-map scene global goes STALE during the
        // course-load teardown; dereferencing it then faults (v8's 0xC0000005).
        // Skip while any scene transition is in flight.
        if (probe::isInSceneTransitionWindow()) return;

        const std::uintptr_t base = probe::mainBase();
        if (base == 0) return;
        const std::uintptr_t scene =
            *reinterpret_cast<std::uintptr_t*>(base + kWorldMapSceneGlobalNsoOffset);
        if (!kLooksLikePtr(scene)) return;
        const std::uintptr_t reg =
            *reinterpret_cast<std::uintptr_t*>(scene + 0x40);
        if (!kLooksLikePtr(reg)) return;

        auto* slot_id =
            reinterpret_cast<std::int32_t*>(reg + kReservedSlotIdOffset);
        const std::int32_t id_fb = *slot_id;
        const bool gated =
            kForceCourseInForbidden ||
            (kTestGatedIdFb >= 0 && id_fb == kTestGatedIdFb);

        bool cancelled = false;
        if (gated && id_fb != -1) {
            // Invalidate the reservation: the entry resolver (FUN_7100191380)
            // short-circuits on id == -1, so no course point resolves and
            // course-in cannot start.  The driver re-reserves next frame, so
            // id_fb stays fresh — no freeze.  Plain int write (no holder /
            // refcount touched), reg already range-validated.
            *slot_id = -1;
            cancelled = true;
        }

        // Bounded log on id_fb change.
        static std::atomic<int> s_fires{0};
        static std::atomic<std::int32_t> s_last{-3};
        const std::int32_t prev = s_last.load(std::memory_order_relaxed);
        const bool changed = id_fb != prev;
        if (changed) s_last.store(id_fb, std::memory_order_relaxed);
        const int n = s_fires.fetch_add(1, std::memory_order_relaxed) + 1;
        if (n <= 6 || changed || (n & 0x1FF) == 0) {
            SMBWAP_LOG_INFO(
                "PhaseB coursein_gate: poll #%d id_fb=%d gated=%d cancelled=%d "
                "changed=%d", n, id_fb,
                (kTestGatedIdFb >= 0 && id_fb == kTestGatedIdFb) ? 1 : 0,
                cancelled ? 1 : 0, changed ? 1 : 0);
        }
    });

// -------------------------------------------------------------------------
// Phase B entry-path diagnostic probes — REMOVED 2026-05-30 (served their
// purpose; one of them crashed the commit->load transition).
// -------------------------------------------------------------------------
//
// Two log-only trampolines on the course-in state machine
// (CheckCourseInUIKey body FUN_710022a964 and SetCourseInFlag exec
// FUN_710088cfdc) ran one diagnostic session (Ryujinx_..._16-32-41.log)
// and answered the entry-path question:
//   * course_in_gate (FUN_710164201c / GoToCenter) -- SILENT again.
//     Confirmed OFF the walk-up entry path (2 runs).
//   * check_courseinuikey (FUN_710022a964) -- ticks continuously on the
//     world map for the 3 selectable nodes.  The input poll.
//   * set_course_in_flag (FUN_710088cfdc) -- fired EXACTLY ONCE at the
//     commit moment.  The confirmed on-entry chokepoint.
// Entry path: input poll -> commit (SetCourseInFlag) -> scene load ->
// course_in PlayReport.
//
// CRASH: that run died in the commit->load window -- after
// set_course_in_flag fired, before course_in PlayReport -- whereas the
// prior build WITHOUT these probes cleared the same window fine.  Timing
// implicates the SetCourseInFlag trampoline: FUN_710088cfdc is
// hook-SENSITIVE, so it is NOT a safe block point.  Both probes removed.
// Next step is pure static RE (no test runs) to find the decision point
// upstream of SetCourseInFlag and a safe intercept.  See the Phase B
// addendum in docs/royal-seed-phase-a-findings.md.

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
    SMBWAP_LOG_INFO("Phase 2g + PhaseA observability + PhaseB per-course gate INERT + gate-entry session-6 INERT/SAFE (persistent course-seq ctrl g1 vt +0x349d300 pinned; exit tree holder NOT found via scan; next = PauseResult data lever): 18 hooks + ap/ subsystem + real probe:: grants + WS reader override");

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

    // PhaseA observability hooks.  Both probes are REVERTED from
    // active force-lock/hijack to log-only after the negative results
    // documented in docs/royal-seed-phase-a-findings.md:
    //   * FlowerLockGateBody only gates Wonder-Seed-locked course
    //     points (narrow subset, not a per-course-entry gate).
    //   * NeedBadgeId never fires for regular courses or palaces.
    // The hooks stay installed so the course-key walk and Nerve
    // signatures are available as a Phase B starting point if the
    // engine-wide entry gate is found in a later session.
    installHook("FlowerLockGateBody",            0x00383418,
                flowerLockGateBodyHook.installAtMainOffset(0x00383418));
    installHook("NeedBadgeIdEnterCourseExecute", 0x01bffe94,
                needBadgeIdEnterCourseExecuteHook.installAtMainOffset(0x01bffe94));

    // PhaseB course-in entry gate — block-all confirm test.  Unlike the
    // two reverted Phase A probes above, this one is ACTIVE: with
    // kBlockAllCourseEntry == true it force-blocks every course point to
    // verify FUN_710164201c is the universal pre-commit entry gate.  Flip
    // the constexpr to false for observability-only.  See the Phase B
    // addendum in docs/royal-seed-phase-a-findings.md.
    installHook("CourseInEnterGate",             0x0164201c,
                courseInEnterGateHook.installAtMainOffset(0x0164201c));

    // PhaseB course-in KILL-SWITCH gate — pre-commit block via the engine's
    // own "course-in forbidden" byte (*(DAT_7103630d28+0x70)).  Hooks the
    // hook-stable input poll FUN_710022a964 and, while kForceCourseInForbidden
    // is true, forces that byte to 1 around Orig to block ALL course-in with
    // no scene load.  User-approved confirm test of the pre-commit lever; flip
    // the constexpr to false for observability-only.  See the session-2
    // addendum in docs/royal-seed-phase-a-findings.md.
    installHook("CheckCourseInUIKeyGate",        0x0022a964,
                checkCourseInUiKeyGateHook.installAtMainOffset(0x0022a964));

    // gate-entry session 3 — log-only course-out capture (the bounce
    // unlock).  ExitCourseMgr body snapshots the live course-out global
    // chain; RequestEventCourseExitByAreaTag (handled inside
    // nerveActivateOnceHook above) snapshots the course-sequence controller.
    // No behavior change.  See docs/gate-entry-session3-handoff.md
    // "Recommended NEXT STEPS" #1.
    installHook("ExitCourseMgrBody",             0x01be3a5c,
                exitCourseMgrBodyHook.installAtMainOffset(0x01be3a5c));

    SMBWAP_LOG_INFO("=== smbwap hkMain END ===");
}
