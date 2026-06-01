// M4.5 save-loaded gate + M3.8 scene-transition gate (Phase 2g port).
//
// Replaces the permissive Stubs.cpp defaults.  Wired into the game-thread
// signals from main.cpp:
//   * markSaveLoaded(...) -- called from gmdContainerAWriterHook AND
//     gmdBoolWriterHook before the bounded log.  Order matters: the
//     latch must run BEFORE the rate-limiter that drops calls past
//     fire #50, otherwise a fast-save-load burst could skip past it.
//   * latchSceneTransitionTick(now) -- called from nerveActivateOnceHook
//     when `vt_off == 0x33fd9a8` (the SceneTransition vtable).
//
// Reader side (called from ap::drainInbound):
//   * isSaveLoaded() -- false until first writer fire.  drainInbound
//     pops the SPSC ring AND defers all writes until this opens.
//   * isInSceneTransitionWindow() -- true while elapsed < 3 s (19.2 MHz
//     tick rate).  drainInbound defers ALL container writers during the
//     window; all bridge messages are either AP-replayed on next tick or
//     idempotent absolute-overwrite, so the wait costs at most ~3 s of
//     staleness with no progress loss.

#include "probe/Gates.hpp"

#include <atomic>
#include <cstdint>

#include "hk/svc/cpu.h"

#include "probe/Gmd.hpp"   // mainBase()
#include "util/Log.hpp"

namespace probe {

namespace {

// Global anchor for the area/course manager hierarchy (DAT_7103628398).
constexpr std::uint32_t kAreaGlobalNsoOffset   = 0x3628398;
// FUN_7101a612cc — the course-out executor (return-to-world-map teardown).
constexpr std::uint32_t kCourseOutExecNsoOffset = 0x1a612cc;

inline std::uintptr_t deref(std::uintptr_t p) {
    return p ? *reinterpret_cast<std::uintptr_t*>(p) : 0;
}

}  // namespace

namespace {

// Flips true the FIRST time the save deserializer touches container A
// (FUN_710049F648) or the container-B bool delegate (FUN_71001F263FC).
// Both paths are exclusively taken by the deserializer at this stage of
// boot because drainInbound never produces a writer call until
// isSaveLoaded() is true (chicken-and-egg makes the first observed
// write provably game-initiated).  Once set, stays set for the process
// lifetime -- if the player swaps saves, the new save's container
// writes still target the new gmd singleton.
std::atomic_bool s_save_loaded{false};

// Stamped (svc::getSystemTick()) when the SceneTransition Nerve at
// vt_off == 0x33fd9a8 fires.  Covers every transition family we've
// observed (death, course/area entry+exit, world-map travel, palace,
// Poplin shop entry, post-Wonder-Seed cleanup).  3 s @ 19.2 MHz tick
// rate is generous -- gameplay is paused or non-interactive during
// transitions, so a brief gate has no visible cost.
std::atomic<std::uint64_t> s_last_scene_transition_tick{0};

// 19.2 MHz ARM generic timer tick rate -> 3 second window.  This must
// stay in sync with the legacy constant of the same name in
// switch-mod/src/program/main.cpp around line 52.
constexpr std::uint64_t kSceneTransitionGateTicks = 3ULL * 19'200'000ULL;

}  // namespace

void markSaveLoaded(const char* via) {
    const bool prev = s_save_loaded.exchange(true, std::memory_order_acq_rel);
    if (!prev) {
        SMBWAP_LOG_INFO(
            "save data loaded (via %s); AP grants will now drain", via);
    }
}

void latchSceneTransitionTick(std::uint64_t now_tick) {
    s_last_scene_transition_tick.store(now_tick, std::memory_order_relaxed);
}

bool isSaveLoaded() {
    return s_save_loaded.load(std::memory_order_acquire);
}

bool isInSceneTransitionWindow() {
    const auto last_trans =
        s_last_scene_transition_tick.load(std::memory_order_relaxed);
    if (last_trans == 0) return false;
    const auto now = hk::svc::getSystemTick();
    return (now - last_trans) < kSceneTransitionGateTicks;
}

void* courseManager() {
    const std::uintptr_t base = mainBase();
    if (base == 0) return nullptr;
    // DAT_7103628398 → +0x30 → +8 → *(+8) → +0x118  (mirrors FUN_7101be3a5c).
    const std::uintptr_t g = deref(base + kAreaGlobalNsoOffset);
    if (g == 0) return nullptr;
    const std::uintptr_t a = deref(g + 0x30);
    if (a == 0) return nullptr;
    const std::uintptr_t b = deref(a + 8);
    if (b == 0) return nullptr;
    const std::uintptr_t c = deref(deref(b + 8));   // **(b+8)
    if (c == 0) return nullptr;
    const std::uintptr_t d = deref(c + 0x118);
    return reinterpret_cast<void*>(d);
}

bool requestCourseOut() {
    auto* mgr = courseManager();
    if (mgr == nullptr) return false;
    const std::uintptr_t base = mainBase();
    if (base == 0) return false;

    // params layout from FUN_7101be3a5c: {+0: byte 0, +4: exit_type,
    // +8: *(mgr+0x98)}.  FUN_7101a612cc reads only *(params+4) (exit_type);
    // 0 = normal course-out.  We fill +8 to match the vanilla caller.
    struct alignas(4) Params {
        std::uint8_t  flag;
        std::uint8_t  pad[3];
        std::uint32_t exit_type;
        std::uint32_t f8;
    } params{};
    params.flag = 0;
    params.exit_type = 0;
    params.f8 = *reinterpret_cast<std::uint32_t*>(
        reinterpret_cast<std::uint8_t*>(mgr) + 0x98);

    using CourseOutFn = void (*)(void*, void*);
    auto fn = reinterpret_cast<CourseOutFn>(base + kCourseOutExecNsoOffset);
    fn(mgr, &params);
    return true;
}

namespace {

// FUN_7101be3a5c — the ExitCourseMgr Nerve body (complete course-out).
constexpr std::uint32_t kExitCourseMgrBodyNsoOffset = 0x1be3a5c;
// Course-sequence controller class vtable (runtime = mainBase()+this).
constexpr std::uint32_t kCourseSeqVtableNsoOffset   = 0x34a8200;
// Host PARENT sub-sequence class vtable (ctx+0x08).
constexpr std::uint32_t kCourseSeqParentVtableNsoOffset = 0x3517068;

// Latched controller pointer (the live ExitCourseMgr-body first arg).
std::atomic<void*> s_course_seq_ctrl{nullptr};
// Latched host PARENT sub-sequence pointer (ctx+0x08 at exit time).
std::atomic<void*> s_course_seq_parent{nullptr};

// True iff `ctx` currently looks like the course-sequence controller
// (vtable == mainBase()+0x34a8200).  Guards both latch and use so a stale
// pointer (controller torn down / re-created elsewhere) can't be driven.
inline bool isCourseSeqController(void* ctx, std::uintptr_t base) {
    if (ctx == nullptr || base == 0) return false;
    const std::uintptr_t vt = *reinterpret_cast<std::uintptr_t*>(ctx);
    return vt == base + kCourseSeqVtableNsoOffset;
}

// True iff `p` currently looks like the host parent sub-sequence
// (vtable == mainBase()+0x3517068).
inline bool isCourseSeqParent(void* p, std::uintptr_t base) {
    if (p == nullptr || base == 0) return false;
    const std::uintptr_t vt = *reinterpret_cast<std::uintptr_t*>(p);
    return vt == base + kCourseSeqParentVtableNsoOffset;
}

}  // namespace

void latchCourseSeqController(void* ctx) {
    const std::uintptr_t base = mainBase();
    if (!isCourseSeqController(ctx, base)) return;
    s_course_seq_ctrl.store(ctx, std::memory_order_release);
}

void* courseSequenceController() {
    return s_course_seq_ctrl.load(std::memory_order_acquire);
}

bool bounceCourseOut() {
    void* ctx = s_course_seq_ctrl.load(std::memory_order_acquire);
    const std::uintptr_t base = mainBase();
    // Re-validate the latched controller and require a loaded course.
    if (!isCourseSeqController(ctx, base)) return false;
    if (courseManager() == nullptr) return false;

    using ExitBodyFn = void (*)(void*);
    auto fn = reinterpret_cast<ExitBodyFn>(base + kExitCourseMgrBodyNsoOffset);
    fn(ctx);
    return true;
}

void latchCourseSeqParent(void* parent) {
    const std::uintptr_t base = mainBase();
    if (!isCourseSeqParent(parent, base)) return;
    s_course_seq_parent.store(parent, std::memory_order_release);
}

void* courseSeqParent() {
    return s_course_seq_parent.load(std::memory_order_acquire);
}

}  // namespace probe
