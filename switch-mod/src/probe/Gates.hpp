// M4.5 save-loaded latch + M3.8 scene-transition gate -- hooks in main.cpp
// drive these via the two callbacks below.  The reader counterparts live
// in ApFrameBridge.hpp as `probe::isSaveLoaded()` /
// `probe::isInSceneTransitionWindow()` (called from drainInbound on the
// game thread).
//
// `markSaveLoaded`: flipped true on first observed call to either of the
// gmd container writers (GmdContainerAWriter / GmdBoolWriter).  Idempotent
// after the first call -- if the player swaps saves, the new container's
// writes still target the new gmd singleton, so we don't need to clear.
//
// `latchSceneTransitionTick`: stamped on every SceneTransition Nerve fire
// (vt_off == 0x33fd9a8).  Reader compares to the current svc system tick
// and gates if delta < kSceneTransitionGateTicks.

#pragma once

#include <cstdint>

namespace probe {

void markSaveLoaded(const char* via);
void latchSceneTransitionTick(std::uint64_t now_tick);

// -----------------------------------------------------------------------
// Course-out ("bounce") trigger — Phase B gate-entry, 2026-05-30.
//
// The world-map walk-up entry gate (FUN_710164201c) turned out to be off
// the common entry path, and the on-path commit Nerve (SetCourseInFlag)
// is hook-sensitive (crashed).  The robust alternative is to let entry
// proceed and then invoke the game's OWN course-out — the same teardown
// it runs when you pause→return-to-map.
//
// The course-out executor is FUN_7101a612cc(courseMgr, &params), called
// in vanilla only by the ExitCourseMgr Nerve (FUN_7101be3a5c).  The
// course manager is reached via the global chain
//   DAT_7103628398 (NSO +0x3628398) → +0x30 → +8 → *(+8) → +0x118
// and params is {byte 0, uint32 exit_type, uint32 *(mgr+0x98)} with
// exit_type 0 = normal course-out.  See docs/royal-seed-phase-a-findings.md.
// -----------------------------------------------------------------------

// Returns the live in-course "course manager" (the +0x118 manager off the
// DAT_7103628398 chain), or nullptr if the chain is not currently valid
// (e.g. not in a loaded course).  Every link is null-checked.
void* courseManager();

// Invokes the game's course-out executor on the current course manager to
// return to the world map (exit_type 0 = normal).  Returns true if a valid
// course manager was found and FUN_7101a612cc was called; false (no-op) if
// the chain was invalid.  MUST be called from a safe per-frame context
// (e.g. after the player tick's Orig) — never mid-load.
bool requestCourseOut();

// -----------------------------------------------------------------------
// Course-out "bounce" via the latched course-sequence controller — the
// gate-entry endgame (2026-05-31).
//
// RE (capture #1..#3) established: the ExitCourseMgr Nerve body
// FUN_7101be3a5c(ctx) performs the COMPLETE course-out — the
// requestCourseOut() teardown PLUS the controller-Nerve step advance
// `*(ctx+0x18) = (.. & 0x3fffffff) | 0x80000000` that the framework reads
// to transition ExitCourseMgr → CheckNextGoToWorldMap → world-map load.
// `ctx` is the course-sequence controller (class vtable NSO +0x34a8200);
// it has no cheap global anchor (its al sequence tree roots past a
// runtime sweep), but it IS the live first arg of the ExitCourseMgr body
// hook and is stable across a session.  So we LATCH it there (validated by
// vtable) and fire the bounce from a safe per-frame context.
// -----------------------------------------------------------------------

// Latch the course-sequence controller `ctx` (validated: its vtable must be
// mainBase()+0x34a8200).  Idempotent; call on every natural ExitCourseMgr
// body fire to keep the pointer fresh.  No-op if ctx is null / mis-typed.
void latchCourseSeqController(void* ctx);

// The latched controller, or nullptr if none latched yet / last latch was
// rejected.  (Caller should still re-validate before driving it.)
void* courseSequenceController();

// Fire the engine's own ExitCourseMgr body FUN_7101be3a5c on the latched
// controller = the COMPLETE bounce (teardown + scene change to world map).
// Gated internally on: a latched controller whose vtable still matches, and
// a valid courseManager() (i.e. a loaded course).  MUST be called from a
// safe per-frame context (after the player tick's Orig), never mid-load.
// Returns true if the body was invoked.
bool bounceCourseOut();

// -----------------------------------------------------------------------
// Course-sequence PARENT persistence probe — gate-entry session 5
// (2026-05-31).  The latch+bounce of the TRANSIENT ExitCourseMgr
// controller (ctx) was a dead end: ctx is created at course-out-request
// time and freed afterward (vt=0 mid-play).  But ctx is hosted by a
// PARENT al sub-sequence (ctx+0x08, class vtable NSO +0x3517068) which —
// being the thing that RUNS the course — is likely PERSISTENT during
// play.  If so, we can find it live and drive ITS transition into the
// exit state (al state-factory key 0x99137dfe -> ctx ctor FUN_7101be3934).
// We latch the parent at the natural exit (validated by vtable) purely to
// TEST, during the next play session, whether a parent-class object still
// lives at that (dev-stable) address — unlike ctx.  Read-only diagnostic.
// -----------------------------------------------------------------------

// Latch the host PARENT sub-sequence (validated: vtable must be
// mainBase()+0x3517068).  Idempotent; call on every natural exit.  No-op
// if parent is null / mis-typed.
void latchCourseSeqParent(void* parent);

// The latched parent, or nullptr if none latched / last latch rejected.
void* courseSeqParent();

}  // namespace probe
