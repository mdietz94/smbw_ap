# Royal Seed gate-entry — Phase A findings + next-session prompt

Phase A of the AP-controlled course-entry gating effort
(see [royal-seed-gate-entry-design.md](royal-seed-gate-entry-design.md))
ran over a single session on 2026-05-30. **Negative result for the
"block any course from AP" goal**, but the RE produced a lot of
actionable knowledge for follow-up work.

This doc is the durable record. The next agent should read it before
touching any of the Phase A probes.

## Bottom line

**Mario Wonder's engine has no single hookable per-course entry
gate.** Course-points come in tiers, each gated by a different
mechanism, and regular courses (and palaces — the case we cared about
for Royal Seeds) bypass all the discoverable gate Nerves entirely.

The bridge-side auto-resolve (already shipped) **remains the right
production answer** for the Royal Seed check-loss bug. Phase A's
negative result is the conclusive evidence for that.

The Phase A observability hooks are still installed in `main.cpp` —
reverted from active to log-only. They produce no behavior change but
the course-key extraction walk and Nerve signatures are available for
Phase B work if/when a path forward is found.

## What we confirmed works

The hakkun trampoline + Nerve-output-slot mechanism is solid:

- Trampolines on game functions land cleanly (no prologue corruption)
- The `*((this+0x20) & ~3)` packed-pointer pattern is the standard for
  Nerve output slots; reading and writing it works across multiple
  Nerve types (verified on IsDisplayFlowerLockUI and
  GetNeedBadgeIdEnterCourseForDirectCourseIn)
- Course-point key extraction via
  `*([nerve+8]→[+8]→[+0x1f8]→[+0x350]→[+0x108])` reliably returns an
  8-byte opaque course identity (verified for 3 distinct course-points
  on W1 across two test sessions)
- State-word filtering on the SceneTransition Nerve (vt offset
  `0x33fd9a8`) precisely identifies sub-types: `0x00ff003700000084` =
  player-controlled exit, `0x00ff000600000004` = death (the existing
  DeathLink discriminator was already documented)
- Force-writing 0 to the IsDisplayFlowerLockUI gate's lock byte
  successfully blocks press-A on **Wonder-Seed-locked course-points
  only** (verified: course-points the player walked to refused entry)
- `__builtin_return_address(0..3)` walks the frame-pointer chain
  through hakkun's trampoline thunk into the game's Nerve framework
  call sites (3 frames deep before the chain wrapped)

## What we tested and ruled out

In iteration order:

### 1. `IsDisplayFlowerLockUI` gate (vtable `0x71033c1ad8`)

Two execute slots (`FUN_710085134c` slot 8 and `FUN_71003833dc` slot
17) both call `FUN_7100383418` (NSO+0x383418). The function reads:

- Bool hash `0x46d90c82` via `FUN_71003838ac` (purpose unknown)
- Bool hash `0x925d4260` via virtual call on `gmd+8 vtable[+0x20]`
  (purpose unknown — primary decision flag based on the decompile)
- Counter hash `0x90d4d0f2` via `FUN_710012ae94` (likely Wonder Seed
  count vs threshold)
- Per-course descriptor byte at `+0x89` from the course descriptor
  found by binary search

Force-write to the lock byte at `*((nerve+0x20) & ~3)` works but only
applies to the small subset of course-points that have this Nerve
attached at construction time (3 cycling per world, the Wonder-Seed-
locked subset like the path to 1-Bowser Jr. that needs N seeds). The
Nerve doesn't fire for regular course points or palaces.

**Why we can't escalate this**: to apply the gate to palaces, we'd
need to attach the IsDisplayFlowerLockUI Nerve to palace course-points
at actor construction. That requires hooking the course-point actor's
constructor and injecting a Nerve into its state machine — multi-day
RE with high crash risk.

### 2. `GetNeedBadgeIdEnterCourseForDirectCourseIn` Nerve (vtable `0x71034b9300`)

Slot 8 = `FUN_7101bffe94`, body writes `-1` to its output slot via
`FUN_71005949e0`. We hijacked it to write `0x42` (a badge ID the
player doesn't own) hoping the badge-challenge UI would block entry.

**Result**: zero fires across a full session, including when the
player pressed A on a regular course. This Nerve only runs for actual
badge-challenge courses, never for regular courses or palaces.

### 3. SceneTransition Nerve abort (vt offset `0x33fd9a8`,
state `0x00ff003700000084`)

Added an abort branch in the existing `nerveActivateOnceHook`: when
SceneTransition activates with the player-controlled-exit state word,
skip `Orig()` so the Nerve never activates.

The state-word filter is **precise** — fires only on player-decided
scene transitions, never spuriously. But aborting the activation does
NOT block course entry: the player still entered the course. This
means the SceneTransition Nerve is a **notification**, not the
driver — entry has multiple redundant driving Nerves (likely active-
Nerve ticks that don't go through `NerveActivateOnce`), and blocking
just this one is insufficient.

### 4. Return-address chain via `__builtin_return_address(0..3)`

Captured when SceneTransition fires with player-controlled-exit. Three
distinct call-site addresses observed:

- ret[0] = NSO+0x8be34 → `FUN_710008bda4` — Nerve framework activator
  (180+ line decompile, generic activation utility, calls vtable
  methods at +0x108, +0x110, +0xa0, +0xb0, +0xb8)
- ret[1] = NSO+0x248a3c → `FUN_7100248908` — Nerve framework helper
- ret[2] = NSO+0x1e0a494 → inside `FUN_7101e0a474` — slot 8 execute
  of the `Element_RandomSelector` Nerve (vtable at NSO+0x35198a8).
  Generic state-machine state-selector framework primitive.

All three are framework infrastructure. The game-side press-A handler
is HIGHER on the stack than ret[3], beyond what frame-pointer walking
reaches. Hakkun's trampoline thunk likely doesn't preserve frame
pointers all the way to the original call site.

### 5. Active-Nerve per-frame tick (`FUN_7100005390`)

Decompile of this function turned out to be a 180+ line atomic state-
update primitive (ARM exclusive-monitor ops, mutex locking, complex
bit manipulation across 8-byte spans). Not a clean per-Nerve hook
point: `param_1` is a state-machine sub-struct pointer, not a Nerve
`this`, so there's no easy way to recover Nerve identity from inside
the hook. Would fire thousands of times per frame across all active
Nerves; probe was never shipped.

### 6. WorldMapPlayerControl component vtable (`0x710342eb78`)

Found the component's vtable. **It's not Nerve-shaped** — no typeinfo
at offset -8 in the standard pattern, slot 0 holds something else.
Component vtables have init/update/destroy slots in a different
arrangement that we didn't fully decode. The first slot we decompiled
turned out to be a buffer/pool management primitive
(`FUN_71012a27b0`), not the input handler we hoped for.

### 7. `BeginEnterStage` / `NotifyStartCourseIn` / `ResetCourseMgr` / course_in PlayReport

All confirmed to be **downstream of the entry decision**. By the time
these Nerves fire, the course has already started loading. Aborting
them would leave the engine in a half-loaded state (scene started,
internals corrupt). Not viable hook points.

### 8. World-map state-machine layers

`WorldMapPlayerStateSelector` (vtable `0x71033f9128`, slot 8 =
`FUN_7101451338`) is a one-line dispatcher to vtable slots 0x120 and
0xb0 of the StateSelector instance. The slot index 0x120 = slot 36
runs out of the standard Nerve vtable bounds, suggesting this is a
subclass with extended vtable. We didn't fully RE the extended layout.

## Hashes / Nerves / vtables catalogued during Phase A

For future-you to grep for:

| Symbol | NSO offset / vtable | Note |
|---|---|---|
| `IsDisplayFlowerLockUI` vtable | `0x71033c1ad8` (typeinfo) | Wonder-Seed-locked course-points |
| `IsDisplayFlowerLockUI` execute body | `+0x383418` (`FUN_7100383418`) | Shared by 2 slot-8 wrappers |
| `GetNeedBadgeIdEnterCourseForDirectCourseIn` vtable | `0x71034b9300` | Badge-challenge courses only |
| `GetNeedBadgeIdEnterCourseForDirectCourseIn` execute | `+0x1bffe94` (`FUN_7101bffe94`) | Writes -1 by default |
| `WorldMapPlayerStateSelector` vtable | `0x71033f9128` | Player state machine root |
| `WorldMapPlayerStateSelector` execute | `+0x1451338` (`FUN_7101451338`) | Dispatches to vtable slots |
| `WorldMapPlayerControl` component vtable | `0x710342eb78` | NOT a Nerve vtable — component layout |
| `BeginEnterStage` vtable | `0x71034a2780` | Stage-load state machine, downstream |
| `NotifyStartCourseIn` vtable | `0x71033fb920` | Notification, downstream |
| `ResetCourseMgr` vtable | `0x71034afa48` | Course-mgr reset, where course_in fires |
| `Element_RandomSelector` execute | `+0x1e0a474` (`FUN_7101e0a474`) | Framework primitive on the call chain |
| Nerve framework activator | `+0x8bda4` (`FUN_710008bda4`) | Generic Nerve activate utility |
| Atomic state-bump primitive | `+0x5390` (`FUN_7100005390`) | Active-Nerve advance, too low-level |
| SceneTransition vtable offset | `0x33fd9a8` | Already known from DeathLink work |
| SceneTransition player-exit state | `0x00ff003700000084` | At Nerve+0x18, fires on course-in |
| Gate function reads bool `0x46d90c82` | (hash) | Purpose unknown |
| Gate function reads bool `0x925d4260` | (hash, custom vtable call) | Primary decision flag |
| Gate function reads counter `0x90d4d0f2` | (hash) | Likely Wonder Seed count |
| Course descriptor flag at `+0x89` | (byte) | `bVar5` in gate decompile, "is-locked"-ish |

## Course identity walk (Phase B starting point)

For any Nerve whose `this` points into a CoursePointInfo context:

```
course_key = *((nerve+8) → (+8) → (+0x1f8) → (+0x350) → (+0x108))
```

8-byte opaque value, almost certainly a pointer to a course descriptor
(binary-searched as a pointer-key in `FUN_7100383418`). Three distinct
keys observed in W1 testing — `0xa2eb3083`, `0xfd003c9f`, `0xd8c1c618`
— corresponding to the 3 Wonder-Seed-locked course-points cycling in
the world-map UI. Phase B (if we ever build out the gate-entry sync)
would need to build a `course_key → AP-index` map by walking the
course descriptor table at boot.

## What's currently in the repo from Phase A

### Code (kept, observability-only)

In `switch-mod/src/main.cpp`:

- `flowerLockGateBodyHook` (NSO+0x383418) — installed, reverted from
  force-lock to log + call-Orig. Logs first 5 fires with course-key,
  packed pointer, lock-byte address. No behavior change.
- `needBadgeIdEnterCourseExecuteHook` (NSO+0x1bffe94) — installed,
  reverted from hijack to log + call-Orig. Logs first 30 fires (none
  observed in normal play).

Both hooks add ~0 cost when not firing, ~minimal cost when firing.
Safe to ship in production.

### Code (reverted)

The SceneTransition return-address probe + abort branch was REMOVED
from `nerveActivateOnceHook`. The existing scene-transition tick latch
and DeathLink discriminator are untouched.

### Docs

- [royal-seed-check-loss-re-findings.md](royal-seed-check-loss-re-findings.md)
  — original RE pass that established the data-model constraint
- [royal-seed-gate-entry-feasibility.md](royal-seed-gate-entry-feasibility.md)
  — pre-Phase-A architectural design
- [royal-seed-gate-entry-design.md](royal-seed-gate-entry-design.md)
  — detailed Phase A/B/C/D rollout plan (now invalidated by Phase A
  negative result; kept for the RE catalog)
- This doc

## Next-session prompt

If a future agent picks this up, here's what to read and what to try
first.

### Required reading (in order)

1. [royal-seed-check-loss-re-findings.md](royal-seed-check-loss-re-findings.md) —
   why the seed bool is both "palace cleared" and "player has seed"
2. This doc — Phase A's negative result and full RE catalog
3. The Phase A probe code in `switch-mod/src/main.cpp` — search for
   `PhaseA` to find both hooks; both are log-only now

### Constraints to honor

- Bridge-side auto-resolve is shipped and working — don't propose
  another data-model workaround for the seed problem specifically
- Don't propose mutating the seed bool itself (foreclosed in the
  original RE pass)
- The `IsDisplayFlowerLockUI` gate genuinely DOES work as a hook
  point — for Wonder-Seed-locked courses only. If you have a use case
  where blocking those specific courses helps (rare), the existing
  hook can be flipped from log-only to force-lock in one line

### Open paths in order of likely tractability

1. **Ghidra script execution fix.** The MCP server's
   `GHIDRA_MCP_ALLOW_SCRIPTS=1` env var isn't being read despite the
   user restarting. If/when fixed, a bulk script can:
   - Find every function that loads vtable address `0x33fd9a8`
     (SceneTransition Nerve) as a literal — reveals every site that
     can activate it
   - Find every function that writes to the WorldMapPlayer actor's
     "selected course point" field — reveals the press-A consumer
   - Find every movz/movk pair loading `0x55815859` (W1 seed hash) —
     would have shortcut the original RE work
   - Cost: 0 RE iterations if scripts work, otherwise blocked. Worth
     trying first in any new session.

2. **Course-point actor constructor injection.** Hook the constructor
   for the WorldMapPlayer (or the per-course-point actor) at the
   moment it's about to attach its Nerves, and inject a custom
   AP-controlled Nerve into the state machine. Reuses the
   IsDisplayFlowerLockUI gate machinery. Estimated cost: 3-5 days, high
   crash risk, requires finding the actor's constructor first.

3. **WorldMapPlayerControl component update hook.** RE the component
   vtable at `0x710342eb78`, identify the per-frame update slot, hook
   it. The press-A input handling lives inside this component. 2-3
   days, moderate risk. We started this in Phase A but didn't finish
   identifying the update slot.

4. **Replay the dynamic call trace with a wider aperture.** Hook
   `FUN_710008bda4` (the Nerve framework activator) and log its
   ACTOR argument's vtable. Every Nerve activation goes through this
   function; logging the actor identity for every activation around
   press-A reveals the press-A actor without needing to find it
   statically. Heavier probe but more directed than the active-Nerve
   tick search.

5. **Try a different problem first.** Phase A was specifically chasing
   "block course entry from AP". If the next user-driven goal isn't
   that exact thing — e.g., if they want to inject items mid-course
   or do per-course-clear telemetry — the Phase A negative result
   doesn't block them.

### What NOT to repeat

- Don't try aborting `nerveActivateOnceHook` for SceneTransition with
  player-exit state. We know this is a notification, not the driver.
  Aborting it does nothing.
- Don't try the NeedBadgeId hijack expecting it to gate regular
  courses. It only fires for badge-challenge courses.
- Don't try forcing the lock byte on IsDisplayFlowerLockUI expecting
  it to gate palaces. The Nerve never fires for palaces; there's
  nothing to lock.
- Don't try hooking the course_in PlayReport emitter
  (`FUN_7101a5d3e8`), `NotifyStartCourseIn`, `BeginEnterStage`,
  `ResetCourseMgr` to block entry. All downstream of the decision;
  aborting them risks corrupting engine state.
- Don't try the `FUN_7100005390` (atomic-state-bump) probe. Too noisy,
  no Nerve identity recoverable from inside the hook, may impact game
  timing.

### Most useful single thing you could do

If the MCP script-execution bug is still present, the highest-leverage
move is to **diagnose and fix the
`GHIDRA_MCP_ALLOW_SCRIPTS` flag handling**. Even if you only restore
script execution, the next Phase A attempt becomes order-of-magnitude
cheaper because we can do bulk vtable / xref analyses that aren't
practical via the per-call tool surface.
