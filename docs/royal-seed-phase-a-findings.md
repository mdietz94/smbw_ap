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

## Next-session prompt (Phase A — SUPERSEDED)

> ⚠️ **This Phase A prompt is obsolete.** The scripts bug it hinges on is
> fixed, and its open paths were resolved/refuted by the Phase B work
> below. **Use the current handoff at the very end of this doc:
> "Next-session prompt (gate-entry continuation, 2026-05-30)".** Kept only
> for historical context.

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

---

# Phase B breakthrough (2026-05-30, second session) — the universal gate DOES exist

`GHIDRA_MCP_ALLOW_SCRIPTS=1` was fixed, `run_script_inline` now works,
and bulk static RE **overturns the Phase A bottom line**. Mario Wonder
*does* have a single hookable per-course entry gate — Phase A was
looking in the wrong Nerve family.

## What Phase A missed and why

Phase A searched the **notification** Nerve (SceneTransition), the
**downstream load** Nerves (BeginEnterStage / NotifyStartCourseIn /
ResetCourseMgr), two **special-case** gates (FlowerLock = seed-locked
points only; NeedBadgeId = badge courses only), and the too-low atomic
tick. It never decoded the **world-map player's own course-in state
machine** — the FSM that runs *between* "press A" and "scene load":

```
free-walk → press A → GoToCoursePointCenter (walk onto node)
          → [enterable check]  ← THE GATE
          → CourseIn (commit) → scene load
```

The `[enterable check]` is a real, single-caller, pre-commit predicate.

## The gate — `FUN_710164201c` (NSO +0x164201c)

Sole caller: the `CheckWorldMapPlayerGoToCoursePointCenter` Nerve
execute (`FUN_7101641fd4`, NSO +0x1641fd4, vtable `0x710338b768`).
Decompile:

```c
undefined4 FUN_710164201c(long nerve) {
    FUN_710032aea4(nerve + 0x28, 0);                 // publish "not ready"
    FUN_71001d7520(&info, **(nerve + 0x20));          // resolve current course point (thread-safe, refcounted)
    if (info == 0) return 0;
    courseKey = *(*(info + 0x350) + 0x108);           // ← the SAME 8-byte key Phase A validated
    mgr = FUN_71005657b0();                            // course-state registry manager
    ent = FUN_7100613870(mgr, &courseKey);            // binary search by key
    if (ent == 0) return 0;
    ret = 0;
    if (ent[0xa0]==0 && ent[0xb0]==0 &&               // ← the actual gate condition:
        ent[0xc0]==0 && ent[0xd0]==0) {               //   all four "lock" pointers null → enterable
        ret = 1;
        FUN_710032aea4(nerve + 0x28, 1);              // publish "ready"
    }
    return ret;                                        // 1 = enter, 0 = block
}
```

Wrapper behaviour (`FUN_7101641fd4`): `if ((ret & 1) == 0)` it sets the
GoToCenter Nerve to **fail-state `0xc0000000`**, otherwise success
`0x80000000`. Return 0 ⇒ Nerve fails ⇒ **course-in never starts.**

Why this is the right target — and addresses every Phase A objection:

- **Pre-commit.** Returns 0 → no scene load begins → none of the
  "half-loaded / corrupt engine state" risk that killed the
  downstream-Nerve approach (#7 above).
- **Universal.** It's the generic "reached the node, can I enter?"
  check on the walk-up path — not gated to seed-locked or badge course
  points. Expected to fire for regular courses *and* palaces.
- **Identity already extracted.** Uses the identical
  `+0x350 → +0x108` walk Phase A reverse-engineered. The existing
  course-key infrastructure transfers directly.
- **Single caller, clean prologue** (`sub sp / stp / str / stp / add`;
  first PC-relative insn is the `bl` at +0x24, well past the 20-byte
  patch window) → safe trampoline, low blast radius.

## The full course-in Nerve state machine (mapped this session)

All are one-shot dispatch Nerves on the world-map player (shared
typeinfo `0x71000ac930`, name-getter slot 0, execute slot 8):

| Nerve | Vtable | Execute | Role |
|---|---|---|---|
| `CheckWorldMapPlayerGoToCoursePointCenter` | `0x710338b768` | `FUN_7101641fd4` (+0x1641fd4) | Walk onto node; **calls the gate** |
| `CheckCourseInUIKey` | `0x71034b80c8` | body `FUN_710022a964` (+0x22a964) | Publishes button-press state to output slots (publisher, not driver) |
| `SetCourseInFlag` | `0x71034013e0` | `FUN_710088cfdc` (+0x88cfdc) | On entry, writes a container-B bool via `FUN_710049ea24(gmd,1,hash,inst)` keyed to current course |
| `GetPlayerOnCoursePointGateId` | `0x710340ed28` | `FUN_7101793c7c` | Publishes the node's gate id |
| `IsActivePlayerCourseInAction` | `0x71033bb258` | `FUN_71016bc89c` | Predicate |
| `LogWorldMapOpenCourseSelect` | `0x71034a9d40` | `FUN_7101be5e7c` | Logging |

## The course-state registry + identity map (Phase C, now cheap)

`FUN_7100613870(mgr, &key)` is a **binary search** over a sorted table:
count at `mgr+0x38`, sorted pointer array at `mgr+0x40`, each entry's
first qword = the 8-byte course key. Manager fetched by
`FUN_71005657b0` (globals walk). The four `+0xa0..+0xd0` pointers on an
entry are the vanilla "locked / in-transition" state (smells like the
`ReservedOpenCourse` / `LockRoute` mechanism — strings
`SetReservedOpenCourseInstanceIdByLockRoute`,
`IsExistReservedSameTimingOpenCourseInfo`).

Separately, `SetCourseInFlag` resolves the course descriptor via
`FUN_71000d33e0` → returns `tableBase + index*0x70` from one of two
descriptor tables at `0x71029f0b34` / `0x71029f0f94` (sentinel
`0x71029f13f4` = "no course"), each entry+8 = a hash. These tables sit
right beside the Royal Seed hash table (`0x71029f0bf4`). **Implication:
the `courseKey → AP-index` map the design doc wanted can now be built
*statically* by walking these tables — no runtime descriptor-walk
required.**

## What was shipped this session — the block-all confirm hook

`switch-mod/src/main.cpp` now installs `CourseInEnterGate` on
`FUN_710164201c` (NSO +0x164201c). With `constexpr bool
kBlockAllCourseEntry = true` it force-blocks **every** course point:
calls Orig (for refcount/lock bookkeeping), then resets the `nerve+0x28`
"ready" byte to 0 and returns 0. Built clean (LLVM 19.1.7) and deployed
to the Ryujinx exefs.

**This is the Phase A "confirm" test, on a far better target.** Expected
in Ryujinx: walk onto any course point or palace, press A → player walks
to the node center, then entry is **refused for every course type**.
Log line `PhaseB course_in_gate: ... natural_allow=N block_all=1`.

### Dynamic-test checklist (next session / user)

1. **Universality** — does it block regular courses *and* palaces?
   (The whole point.)
2. **Player behaviour on block** — does the player gracefully return to
   free-walk, or get stuck re-running the GoToCenter Nerve (soft-lock)?
   This decides whether the gate hook is shippable as-is or needs the
   "bounce" fallback instead.
3. **Boot / cutscene safety** — does any first-visit world-map demo or
   intro auto-course-in that relies on this gate succeeding hang?
4. Flip `kBlockAllCourseEntry` to `false` to revert to observability.

### Remaining open RE questions (do NOT block the confirm test)

- **DirectCourseIn path.** `IsDirectCourseIn` is a *mode flag* on the
  course-in controller (bound as a property in `FUN_7101c00048`, not a
  separate entry function) — most plausibly the post-death *retry*
  path, which requires having already entered, so it likely can't reach
  a never-entered gated course. Not proven; a belt-and-suspenders
  second hook on the direct path would close it if the confirm test
  shows a bypass.
- **The four `+0xa0..+0xd0` lock pointers** — what writes them (the
  vanilla lock mechanism). If we can populate one safely, the
  data-driven lever (drive the game's own lock) becomes viable and
  would gate all entry paths uniformly with correct UI — but it needs
  constructing an engine struct, so the hook is the safer first ship.

### Status of the Phase A bottom line

The "Mario Wonder's engine has no single hookable per-course entry gate"
verdict at the top of this doc is **superseded** for the world-map
walk-up entry path. The bridge-side Royal Seed auto-resolve remains
shipped and correct as a belt-and-suspenders.

## Phase B dynamic tests (2026-05-30) — the gate is OFF the entry path

Two Ryujinx runs of the block-all/diagnostic builds **refuted** the
`FUN_710164201c` gate hypothesis:

**Run 1 (block-all, 16 hooks, `Ryujinx_…_15-12-27.log` boot #2):** the
player entered a course (`course_in` PlayReport emitted, `course_result`
followed) with **zero `PhaseB course_in_gate` fires**, while
`FlowerLockGateBody` fired 5× (W1 keys `a2eb3083 / fd003c9f /
d8c1c618`). The hook installed (`install CourseInEnterGate @ +0x164201c
OK`) but `FUN_710164201c` was never called — its sole caller, the
`GoToCoursePointCenter` Nerve, didn't run for this entry. (Note: that
log contains *two* game boots — the clock resets per boot — boot #1 was
the old 15-hook build; only one `smbwap`/`subsdk9` mod is ever loaded,
confirmed by `ModLoader … Found enabled mod 'smbwap'` once per boot.)

**Run 2 (entry-path diagnostic, 18 hooks, `Ryujinx_…_16-32-41.log`):**
log-only probes mapped the real path:

| Probe | Result |
|---|---|
| `course_in_gate` (`FUN_710164201c`) | **silent** — confirmed off-path (2nd run) |
| `check_courseinuikey` (`FUN_710022a964`) | ticks continuously on the world map for the 3 selectable nodes — the **input poll**; hook ran 512× with no issue (**hook-stable**) |
| `set_course_in_flag` (`FUN_710088cfdc`) | fired **exactly once at commit** — the confirmed on-entry chokepoint |

**Entry path:** world-map input poll (`CheckCourseInUIKey`) → commit
(`SetCourseInFlag`) → scene load → `course_in` PlayReport. The
`GoToCoursePointCenter` gate sits on a *different* sub-behavior (likely
the cursor walking to a distant node) that this entry didn't use.

**CRASH:** Run 2 died in the **commit→load window** — after
`set_course_in_flag` fired, through the save-writes (`gmd.A_writer
#44–50`), **before** the `course_in` PlayReport — with a Ryujinx "Fatal
error" / no dump. Run 1 (without these probes) cleared the same window
fine. A `JIT Cache Region 0 exhausted → new 512 MB region` appeared ~2 s
prior (host had ~5.8 GB free), so a host/JIT fault can't be fully
excluded, but the timing implicates the **`SetCourseInFlag`
(`FUN_710088cfdc`) trampoline**: treat that function as **hook-sensitive
— not a safe block point.** Both diagnostic probes were removed; the
`CourseInEnterGate` hook remains installed log-only (proven harmless — it
never fires) as an observability control. Build returned to the stable
16-hook config.

### Revised plan (static-RE-only from here)

No more deep-hook test runs until a single high-confidence intercept is
identified. Two viable strategies, both informed by Run 2:

1. **Pre-commit input intercept at `CheckCourseInUIKey`
   (`FUN_710022a964`)** — it is hook-stable (ran 512× cleanly), runs
   *before* the fragile commit, and is where the A-press is evaluated
   (its success path sets the published "course-in requested" byte at
   `param_1[6]`/`+0x30` and calls `vtable[+0x128]`). **Open RE:** recover
   the *selected course identity* from this Nerve's context (its `+0x20…`
   are output slots, not the course point — need to walk to the actor's
   current selection) so we can suppress the commit only for AP-gated
   courses. Cleanest if feasible (pre-commit, no visual flash, stable
   hook).

2. **Bounce via the game's own CourseOut** — let entry proceed, detect a
   gated course (identity already available from `course_in` /
   `SetCourseInFlag`'s descriptor walk via `FUN_71000d33e0`), then
   trigger the engine's course-exit. Primitives located:
   `RequestEventCourseExitByAreaTag` (`0x710293d37b`), `ExitCourseMgr`
   (`0x71028f0778`), `CheckNextGoToWorldMap` (`0x71028fb877`),
   `IsExecuteNormalCourseOutDemoForWorldMapPlayer` (`0x71028d49b5`).
   Robust (uses the game's own teardown, hooks nothing in the fragile
   entry path) but the course briefly loads (visual flash / momentarily
   playable) before the bounce.

Recommended: scope strategy 1 statically (can we get course identity at
`CheckCourseInUIKey`?); fall back to strategy 2 if not. The
`GoToCoursePointCenter` gate is reusable only for entries that route
through it (not the common case), so it is not the primary path.

### Bounce (strategy 2) — RE + smoke test (2026-05-30)

The course-out executor is **`FUN_7101a612cc(courseMgr, &params)`** (NSO
`+0x1a612cc`), called in vanilla only by the `ExitCourseMgr` Nerve
(`FUN_7101be3a5c`). It is a 548-byte multi-subsystem teardown (sets
`mgr+0x94=3`, tears down player/camera/UI/audio sub-objects, touches
several globals) — the same path a normal pause→return-to-map runs.

Course manager reached via the global chain (mirrors `FUN_7101be3a5c`,
and matches the registry getter `FUN_71005657b0`):
```
DAT_7103628398 (NSO +0x3628398) → +0x30 → +8 → *(+8) → +0x118  = courseMgr
```
`params = {+0: byte 0, +4: uint32 exit_type, +8: *(mgr+0x98)}`;
`FUN_7101a612cc` reads only `*(params+4)` (exit_type). `exit_type 0` =
normal course-out; `exit_type 1` triggers extra calls (`FUN_7101a683a0`,
`FUN_7101b77448`).

Implemented as `probe::courseManager()` / `probe::requestCourseOut()` in
`switch-mod/src/probe/Gates.{hpp,cpp}`, exercised by a one-shot smoke
trigger in `PlayerTickLatch` (`kCourseOutSmokeTest`).

**SMOKE-TEST RESULT (2026-05-30, `Ryujinx_…_17-03-48.log`):** the direct
call is **SAFE but INCOMPLETE.**
- `courseManager` resolved to a stable non-null pointer (`0x20dab50848`)
  at player ticks 1/60/120 → **the global chain is correct in-course.**
- At tick 180: `requestCourseOut -> 1` → `FUN_7101a612cc` was called with
  no crash; the player kept playing (later `course_result` + a second
  `course_in`).
- **No visible bounce.** `FUN_7101a612cc` is only the teardown/prep step;
  the actual scene change to the world map is driven by the course-
  sequence controller's Nerve *advancing* after the `ExitCourseMgr` Nerve
  body runs — which a direct call skips. `exit_type` is irrelevant here:
  the `exit_type==1` extras (`FUN_7101a683a0`, `FUN_7101b77448`) are
  medal/counter + gmd stat-recording, not a scene change.

**Conclusion:** direct-calling `FUN_7101a612cc` cannot complete the
bounce. The course-out must be driven through the Nerve framework. Next
RE step: drive the **course-sequence controller** (the actor whose Nerve
body is `ExitCourseMgr` = `FUN_7101be3a5c`, vtable `0x71034a8290`) into
that Nerve via the engine's own request — i.e. find either
`al::setNerve(controller, &ExitCourseMgrNerve)` + the Nerve object, or the
high-level "request return to world map" the pause menu uses. The smoke
trigger is disabled (`kCourseOutSmokeTest=false`); the
`requestCourseOut()`/`courseManager()` primitives are kept for that work.

### Where the gate-entry effort stands (2026-05-30 end of session)

Ruled out: GoToCenter gate `FUN_710164201c` (off the common entry path),
hooking `SetCourseInFlag` (crashes the commit→load), direct-calling the
course-out executor (incomplete). Still-open viable paths, all needing
more RE: (1) **bounce via setNerve** into `ExitCourseMgr`; (2) **pre-commit
input intercept** at the hook-stable `CheckCourseInUIKey`
(`FUN_710022a964`) — needs the selected-course identity from its context.
This is a genuine multi-session feature (matches the original
`royal-seed-gate-entry-feasibility.md` ~4–5 day estimate). The bridge-side
Royal Seed auto-resolve remains the shipped answer for the immediate bug.

---

# Next-session prompt (gate-entry continuation, 2026-05-30)

**This is the authoritative handoff. Read it first.** Paste-ready prompt
for a fresh session is in the project response; everything it needs is
below.

## Goal

Make the SMBW mod **block entry to chosen courses/palaces under AP-state
conditions** (the Royal-Seed gate-entry feature). The bridge-side
auto-resolve already fixes the immediate Royal-Seed check-loss bug, so
this is the *general* gating capability, not an emergency.

## Required reading (in order)

1. This doc, top-to-bottom — especially "Phase B" onward. The Phase A
   "no gate exists" verdict and the Phase A "Next-session prompt" are
   **superseded**; ignore their action items.
2. [royal-seed-check-loss-re-findings.md](royal-seed-check-loss-re-findings.md)
   — the data-model constraint (seed bool == "palace cleared").
3. `switch-mod/src/main.cpp` — search `PhaseB`. The `CourseInEnterGate`
   hook (`FUN_710164201c`) is installed **log-only** (harmless control,
   never fires). `kCourseOutSmokeTest` is **false**.
4. `switch-mod/src/probe/Gates.{hpp,cpp}` — `probe::courseManager()` and
   `probe::requestCourseOut()` (the course-out primitive; chain verified,
   call is crash-safe but on its own incomplete).

## State of the world (what's proven)

- **Engine version:** SMBW v1.0.0, NSO base `0x7100000000`, Ghidra project
  "Wonder" / `main.nso`. `GHIDRA_MCP_ALLOW_SCRIPTS=1` works — but the MCP
  bridge starts with **no program open**: call `open_program("/main.nso")`
  first, then `run_script_inline` (write a `GhidraScript` with a `run()`
  override; `DecompInterface` at 90 s beats the timing-out
  `decompile_function` wrapper). See memory `[[reference-ghidra-mcp]]`.
- **Entry path (walk-up):** input poll `CheckCourseInUIKey`
  (`FUN_710022a964`, +0x22a964; **hook-stable**, ticks every frame for the
  ~3 selectable nodes) → commit `SetCourseInFlag` (`FUN_710088cfdc`,
  +0x88cfdc; fires once at commit; **hook-sensitive — do NOT trampoline,
  it crashed the commit→load**) → scene load → `course_in` PlayReport.
- **GoToCenter gate** `FUN_710164201c` (+0x164201c): a clean per-course
  enterable check, but it is **off the common entry path** (never fired in
  2 runs). Only used for some "walk to a distant node" sub-behaviour.
- **Course-out (bounce) mechanism:** executor `FUN_7101a612cc` (+0x1a612cc)
  = teardown/prep only; reached via global chain `DAT_7103628398
  (+0x3628398) → +0x30 → +8 → *(+8) → +0x118 = courseMgr`. Calling it
  directly is **crash-safe but does NOT change scene** — the real return-
  to-map is the course-sequence controller's Nerve **advancing** after the
  `ExitCourseMgr` Nerve body (`FUN_7101be3a5c`, vtable `0x71034a8290`).
- **Course identity:** 8-byte course key via `coursePtInfo+0x350 → +0x108`
  (validated Phase A/B); course descriptor tables at `0x71029f0b34` /
  `0x71029f0f94` (stride 0x70, entry+8 = a hash), beside the Royal-Seed
  hash table `0x71029f0bf4`. Registry binary-search: `FUN_7100613870`
  (count `mgr+0x38`, sorted array `mgr+0x40`, entry[0]=key); registry mgr
  getter `FUN_71005657b0`. `SetCourseInFlag` resolves the descriptor via
  `FUN_71000d33e0(courseInfo)` (sentinel `0x71029f13f4` = none).

## Two viable strategies (recommended first move = #1)

**1. Bounce via `setNerve` (most direct to a working result).** Drive the
course-sequence controller into the `ExitCourseMgr` Nerve so the framework
runs the *full* sequence (teardown + advance + scene change). RE needed:
- The controller actor whose Nerve body is `FUN_7101be3a5c` (NOT the
  course manager `D=courseMgr`; that's the object the Nerve *operates on*).
- The engine's nerve-change call (`al::setNerve(actor, const Nerve*)` or
  equivalent) and the `ExitCourseMgr` **Nerve object** (static; vtable
  `0x71034a8290`). Find via: who references that vtable / what transitions
  into this Nerve; or trace the pause-menu "Return to World Map" handler
  (it does exactly this user-initiated course-out). Pause vocab is all
  granular flags — look at `game::sequence::PauseResult`,
  `OpenPauseScreen`, `IsEnablePauseMenuRestart` and follow the menu→exit
  request.
- Trigger from a safe per-frame context (the existing `PlayerTickLatch`,
  after `Orig`), gated by the bridge (see Wiring below).

**2. Pre-commit input intercept at `CheckCourseInUIKey`
(`FUN_710022a964`).** It's hook-stable and runs before the fragile commit;
its success path sets the published "course-in requested" byte at
`param_1[6]`/`+0x30` and calls `vtable[+0x128]`. RE needed: recover the
*selected course identity* from this Nerve's context (its `+0x20…` are
output slots, not the course — walk to the actor's current selection),
then suppress the commit only for AP-gated courses. Cleanest UX (no flash)
if the identity is reachable.

## Wiring (once a trigger works)

Mirror the badge-sync pattern. Bridge holds the AP-gated-course set;
decodes `course_in` (already does) → if the entered/selected course is
gated, push a `RequestCourseOutMsg` (or a gated-course bitfield like
`SetBadgesAbsoluteMsg`); Switch consults it from the safe per-frame
context and fires the bounce / suppresses the commit. Course identity on
both sides via the 8-byte key + descriptor tables above.

## Hard constraints — do NOT repeat

- Do **not** trampoline `SetCourseInFlag` (`FUN_710088cfdc`) — crashes the
  commit→load window.
- Do **not** expect direct-calling `FUN_7101a612cc` to bounce — it's
  teardown only; `exit_type` 0/1 both irrelevant.
- Do **not** rely on the GoToCenter gate `FUN_710164201c` for the common
  entry — it's off the path.
- Do **not** re-try the Phase A dead-ends (abort SceneTransition / hijack
  NeedBadgeId / force the FlowerLock byte for palaces / hook the
  downstream load Nerves). All documented above as confirmed no-ops.
- Minimise live test runs: each is a ~3 min boot and crashes cost a
  restart with no dump. Do RE statically; only build+deploy when you have
  a single high-confidence change. Confirm-test first (one gated change),
  then wire AP.

## Build / deploy / test loop (Windows, this worktree)

```pwsh
& "C:\Program Files\CMake\bin\cmake.exe" --build `
  "C:\Users\maxwe\Documents\git\smbw_ap\.claude\worktrees\sad-poincare-05cf59\switch-mod\build"
# deploy (note: real Ryujinx is Desktop\ryujinx-1.3.3, NOT Desktop\Switch\...;
#  it's non-portable so it reads %APPDATA%\Ryujinx\mods). subsdk9 lives under build\sd\...:
$dst = "$env:APPDATA\Ryujinx\mods\contents\010015100b514000\smbwap\exefs"
$sub = Get-ChildItem "...\switch-mod\build" -Recurse -Filter subsdk9 |
       Sort LastWriteTime -Desc | Select -First 1
Copy-Item $sub.FullName "$dst\subsdk9" -Force
Copy-Item "...\switch-mod\build\main.npdm" "$dst\main.npdm" -Force
```
Tail: `Get-ChildItem "C:\Users\maxwe\Desktop\ryujinx-1.3.3\Logs\Ryujinx_*.log" |
sort LastWriteTime -desc | select -first 1` then `Get-Content -Wait … |
Select-String 'PhaseB|course_in'`. Strip NULs (`-replace "`0",""`) when
parsing offline. Ryujinx resets its log clock per game boot, so one
`*.log` file can hold several boots — match by hook-count summary line.

> ⚠️ **CLAUDE.md is stale** on two paths: the Ryujinx log dir
> (`Desktop\Switch\ryujinx-1.3.3` doesn't exist — use
> `Desktop\ryujinx-1.3.3`) and the repo build path (use this worktree).

---

# Gate-entry RE session 2 (2026-05-30 PM) — architecture fully mapped, static-only

This session did **no live tests** (static RE only, per the constraint).
It refined the two strategies from the handoff above with hard decompile
evidence. Net: the architecture is now fully understood, **detection is
already solved with hooks we ship today**, and the one remaining blocker
is narrowed to "make the bounce complete the scene change."

## Confirmed by decompile this session

### Entry path — both ends decompiled

**Input poll `CheckCourseInUIKey` (`FUN_710022a964`, +0x22a964):**
`param_1` is the course-in controller actor. Its `+0x20/+0x28/+0x30`
are **al value-ports** (packed-pointer pattern: bit1 = presence,
`&~3` = byte/int slot), NOT the course point. On the success path it
publishes `requested=1` to the `+0x30` port and calls the actor's
`vtable[0x128]` driver. **There is a global pre-commit kill-switch:**
the whole commit is skipped unless
`DAT_7103630d28 == 0 || *(char*)(DAT_7103630d28 + 0x70) == 0`.
Setting `*(DAT_7103630d28 + 0x70) = 1` **suppresses ALL course-in,
engine-side, per frame, pre-commit** — but it is global, not per-course,
and no per-course identity is reachable in this nerve's context (the
node lives on a *different* actor; see below).

**Commit `SetCourseInFlag` (`FUN_710088cfdc`, +0x88cfdc):**
```c
void FUN_710088cfdc(long param_1) {
  puVar3 = FUN_71000d33e0(**(param_1 + 0x20));     // courseInfo -> descriptor
  if (puVar3 != &DAT_71029f13f4) {                 // sentinel "no course"
    uVar1 = *(u32*)(puVar3 + 8);                    // descriptor+8 = PER-COURSE hash
    uVar2 = FUN_7100263418(**(param_1 + 0x20));
    FUN_710049ea24(gmd::sInstance, 1, uVar1, uVar2); // container-B bool write
  }
  *(param_1+0x18) = ...|0x80000000;                 // nerve success -> scene load
}
```

### ★ DETECTION IS ALREADY SOLVED (new finding)

The commit's `FUN_710049ea24(gmd, 1, perCourseHash, inst)` is the **same
container-B writer whose inner delegate `FUN_71001f263fc` we already
trampoline as `GmdBoolWriter`.** So **at the moment any course is
entered, our existing hook already observes the per-course entry hash**
(`descriptor+8`) — no new hook, and crucially no trampoline on the
fragile `SetCourseInFlag`. Per-course identity for the gate decision is
therefore available switch-side *for free*, and the bridge independently
knows the entered course from the `course_in` PlayReport. **Identity is
not the blocker for either side.**

### Course-out architecture — fully mapped

`ExitCourseMgr` nerve body (`FUN_7101be3a5c`, vtable `0x71034a8290`
slot 8) decompiled:
```c
void FUN_7101be3a5c(long ctx) {           // ctx = nerve-execute context
  courseMgr = DAT_7103628398->+0x30->+8->*(+8)->+0x118;   // global chain
  if (courseMgr) {
    params = { 0, exit_type=*(*(ctx+0x20)&~3), *(courseMgr+0x98) };
    FUN_7101a612cc(courseMgr, &params);   // teardown (== probe::requestCourseOut)
  }
  *(ctx+0x18) = ...|0x80000000;            // SUCCESS -> framework advances the
}                                          //            controller to the next nerve
```
The scene change is **not** in the teardown (`FUN_7101a612cc`); it is the
framework **advancing the controller's nerve** after `ctx+0x18` is set to
success. Confirmed why `probe::requestCourseOut()` is incomplete: a
direct call runs teardown but never sets a nerve result, so nothing
advances. Sequence is `… → ExitCourseMgr → CheckNextGoToWorldMap
(FUN_710163e944, vtable 0x710338a698) → world-map load`, each nerve doing
its work then signalling advance via `+0x18 |= 0x80000000`.

`RequestEventCourseExitByAreaTag` (vtable `0x71033fd690`) slot 8 =
`thunk_FUN_7100559f7c` → it is an **active Nerve using the shared
one-shot helper `FUN_7100559f7c` that we already hook as
`NerveActivateOnce`.** Implication: when a *natural* course-out happens
(pause→exit, give-up, clear), our existing hook already sees this nerve's
vtable fire — a free observation point and a way to **latch the live
controller pointer** if needed.

### Why "setNerve into ExitCourseMgr" is harder than the handoff hoped

- The vtable `0x71034a8290` is referenced by **no static qword anywhere
  in the image** (scanned all initialized memory) and **no `adrp;add
  #0x290`** materialises it. The nerve objects are **heap-constructed
  per-controller** (ctors like `FUN_7101be3934` `alloc(0x28); *obj =
  &vtable; …`). There is **no static nerve-object address** to
  `setNerve` with, and `al::setNerve` itself is **not a named symbol**
  (binary is stripped of game symbols; only SDK/gmd syms imported).
- So the manual-setNerve path needs, at runtime: (1) the live controller
  actor, (2) `al::setNerve` (still unlocated), and (3) the controller's
  live heap **exit-nerve object** (walked from the controller). That is
  three runtime unknowns and bypasses the framework's own guards.

## Where each strategy actually stands now

| | Pre-commit suppress | Bounce (let load, then exit) |
|---|---|---|
| Detection | global kill-switch only (no per-course identity in the input-poll nerve) | ✅ solved (existing `GmdBoolWriter` hook + bridge `course_in`) |
| Prevention | `*(DAT_7103630d28+0x70)=1` suppresses **all** course-in, per frame, pre-commit, **no scene load** (safest) — but global | needs the controller's nerve to **advance**; `requestCourseOut()` teardown alone is confirmed insufficient |
| Crash risk | low (no scene load; the kill-switch is engine-polled state) | medium (acts in/after the commit→load window; setNerve bypasses guards) |
| UX | clean (entry never starts) if made per-course | brief course flash before bounce |
| Residual RE | recover selected-course identity at a *stable* per-frame context (node lives on the gate/commit actor `+0x20`; world-map player is `*(DAT_7103625850+0x98)`, course-point registry `*(DAT_7103625850+0x40)`) | locate `al::setNerve` + obtain live controller + live exit-nerve object, **or** a high-level "request course exit" entry |

## New leads worth one probe each (not yet run)

- **Identity from a global** (unlocks the safe pre-commit gate): the
  world-map player is `*(DAT_7103625850+0x98)`; a course-point registry
  is `*(DAT_7103625850+0x40)` (indexed by a course index — see
  `FUN_71001d5aa0` in `FUN_710163ad74`). If the player exposes its
  "current course point", identity is readable from a global at any
  stable hook, and the global kill-switch becomes a **per-course** gate.
- **Latch the live controller** from the existing `NerveActivateOnce`
  hook by recognising a vtable owned by the course-sequence controller,
  then RE `al::setNerve` to drive it into the exit nerve.

## Hard constraints reconfirmed (do NOT repeat)

Everything in the "Hard constraints" list of the previous next-session
prompt still holds. Add: **the nerve singleton objects are heap, not
static — do not look for a static `&ExitCourseMgrNerve`.**

## Decision (2026-05-30, user-approved): pre-commit kill-switch

User chose the **pre-commit kill-switch** over the bounce. Plan:
1. **Confirm-test (this build):** hook the hook-stable input poll
   `FUN_710022a964` and force `*(courseInMgr+0x70)=1` around `Orig`
   (surgical save/restore) to block **all** course-in. Validates the
   lever with no scene load.
2. **Make per-course:** read the hovered node's identity at the poll and
   gate only AP-locked nodes.
3. **Wire AP:** bridge pushes the gated-course set (badge-sync pattern);
   switch consults it per frame.

### Kill-switch internals (decompiled)

- `courseInMgr = *(mainBase + 0x3630d28)` (`DAT_7103630d28`). Stable sead
  singleton, 0x98 bytes, allocated once in `FUN_7100655408`
  (vtable `PTR_FUN_710348ef28`, same `0x710348exxx` region as the
  pause/exit nerve names — this is the course-in/sequence manager).
- The input poll commits course-in only when
  `courseInMgr == 0 || *(char*)(courseInMgr + 0x70) == 0`. Byte `+0x70` is
  the engine's own "course-in forbidden" latch (the engine sets it during
  cutscenes/demos). Forcing it to 1 makes the poll refuse to start any
  course-in — pre-commit, no scene load.
- Separately, `+0x68` is a u32 bitfield the course-out executors
  (`FUN_7101a612cc` sets bit 3, `FUN_7101a610bc` sets bit 1) atomically
  OR during teardown — confirms the engine actively manages this manager's
  control flags. We use `+0x70` (the poll's read), not `+0x68`.

### ★ Confirm-test PASSED (2026-05-30, `Ryujinx_…_20-39-54.log`)

Block-all build (`kForceCourseInForbidden=true`), clean save, world map.
Result — the pre-commit kill-switch works:
- `coursein_killswitch` fired every frame on the world map (polls #1…#3584),
  **`natural_forbidden=0 forced=1` on every single poll** → the manager
  pointer (`*(mainBase+0x3630d28)`) resolved reliably from the very first
  frame; the engine's normal `+0x70` value is `0` (entry allowed).
- **1-1 refused entry**; player stayed free to walk. **Zero `course_in` /
  `course_result`** PlayReports the entire session → no course ever loaded.
- **No crash / abort / soft-lock.** (Only benign boot FS-fixup warnings and
  the normal window-close `DequeueBuffer: Busy` at the ~63 s manual quit.)
- Palace universality untested (clean save can't reach one), but palaces use
  the *same* input poll → high confidence the same byte gates them.

Lever confirmed. Next: per-course selectivity, then AP wiring.

### Per-course identity — RE follow-up (2026-05-30 PM)

- **Dead end:** `FUN_7101b70f30` (`GetPlayerOnCoursePointGateId` source,
  keyed by `0x6c259974`) resolves a **gmd→WorldMapNpcId** mapping
  (`FUN_71005233c0` maps the hash to `WorldMapNpcId01..30`). That's NPC
  identity, not the course point. Discard the gate-id lead.
- **Strong lead — the current-node chain.** `FUN_7100383418`
  (FlowerLockGate, already hooked log-only) reaches the hovered node and
  its course key with:
  ```
  node = *(*(*(param_1 + 8) + 8) + 0x1f8)
  key  = *(*(node + 0x350) + 0x108)        // the same 8-byte key the GoToCenter gate uses
  ```
  This `*(*(*(actor+8)+8)+0x1f8)` "current course point" chain is shared by
  several world-map nerves (camera `FUN_710163ad74`, course-out prep
  `FUN_7101a610bc`). FlowerLockGate also uses `param_1+0x20` as a value-port
  — the **same port pattern as the input poll** (`FUN_710022a964`, which
  uses ports `+0x20/+0x28/+0x30`) — strong evidence the poll's `param_1`
  is the **same world-map course-in actor**, so the chain is reachable at
  the poll. (Static byte-scan: 32 functions use the `+0x350 → +0x108` key
  walk, incl. the GoToCenter gate `FUN_710164201c` and FlowerLockGate.)
- **Key → AP mapping.** The 8-byte node key is matched against the course
  registry (`courseMgr+0x38` count / `+0x40` sorted array, `entry[0]=key`)
  via binary search `FUN_7100613870`. Separately the commit resolves the
  course via `FUN_71000d33e0(courseInfo)` → `descriptorTable[index]`
  (tables `0x71029f0b34` / `0x71029f0f94`, stride 0x70, selector at
  `courseInfo+0x34`, index = `*FUN_71000d3524`), and **`descriptor+8` =
  the 32-bit per-course hash** the existing AP protocol already uses.
- **Next step (proposed): safe log-only identity probe.** Add to the poll
  hook a fully null-checked read of the chain above → log the 8-byte key
  each poll (block OFF so courses can still be entered). Cross-check the
  logged key tracks the hovered node and matches the entered course
  (vs `course_in` / the commit's `descriptor+8`). Once validated, gate
  `+0x70` only when the key is in the bridge-pushed gated set. No risky
  derefs ship until the chain is confirmed live.

### Per-course identity — live-probe findings (2026-05-30 PM, iterative)

The poll actor's vtable is **NSO 0x34b8798**; its course-in driver is
**slot 0x128 = FUN_710074b578**, predicate slot 0x120 = FUN_7101bfee00
(returns 1).  The driver finds the hovered node POSITIONALLY (iterates the
world-map registry by player index, matches player position) — there is no
single stored "hovered node id" on the poll actor's ports (ports +0x20/
+0x28 carry mode + **player index**, not course identity; confirmed live).

What DID resolve live:
- **World-map scene/registry:** `scene = *(mainBase+0x3625850)`,
  `registry = *(scene+0x40)` — both valid every poll on the world map
  (`scene_ok=1 reg_ok=1`).  (Note: `courseManager()`/`+0x118` is the
  IN-COURSE manager — null on the world map, so its `+0x38`/`+0x760` keys
  are NOT the world-map cursor; probe v3 dead-end.)
- **Per-player current-course slot** (from FUN_7100191380): primary at
  `registry + 0x1c0 + idx*0x18` (was always `id=-1` empty), **fallback at
  `registry + 0x100 + idx*0x18` is the live one**; `slot+0x10` = current
  course id (`id_fb`), `slot+8` = holder, `*(holder+0x40)` = course point
  (`cp`), `*(cp+0x24)` = type (4 = settled-on-node; 1/2/3 = transitioning).
- **`id_fb` tracks the node** (33 while settled on a node; changed to 248
  while moving) — a SAFE single int read off the validated registry.
- **AP hash from cp** (static replication of the commit's FUN_71000d33e0):
  `sel=*(int*)(cp+0x34)`, `idx=*(int*)(cp+0x38)`,
  `descriptor = (sel? 0x29f0f94 : 0x29f0b34) + idx*0x70`,
  `hash = *(u32*)(descriptor+8)`.  Promising but the cp walk is
  transition-fragile.

⚠️ **CRASH (probe v5, 0xC0000005):** during the course-entry transition
the `holder`/`cp` chain goes stale; the old permissive `kLooksLikePtr`
(0x1000000..2^48) let a garbage in-range value through and the deref
faulted.  **Hardened (v6):** `kLooksLikePtr` now = guest-heap window
`[0x2000000000, 0x2800000000)` + 8-byte alignment (all live objects are
`0x20_xxxxxxxx`), and the `holder`/`cp` walk only runs when `id_fb` is a
sane course id (`0 <= id_fb < 0x4000`).  **Real-HW (M6) ASLR will move the
heap — the hardcoded window is a DEV-only guard; revisit before console.**

Open: confirm `id_fb`/`hash` is stable + unique per node and matches the
entered course (needs a clean, non-transition reading + a `course_in`
correlation).  Fallback if the cp walk stays fragile: use `id_fb` alone
(safe) with a bridge-learned `id_fb -> course` map.

### Per-course gate — live results + two blockers (2026-05-30 PM)

**Identity CONFIRMED.** `id_fb = *(int*)(*(*(mainBase+0x3625850)+0x40)+0x110)`
(world-map scene → registry → player-0 fallback slot `+0x10`) uniquely and
stably identifies the **settled** hovered node: **1-1 = 33, 1-2 = 1362,
next = 2545** (`cp_type=4` when settled; 1/2/3 while walking).  Read live in
probe v6 with no crash.  (`id_fb` cross-boot stability still unconfirmed.)

**Two blockers stopped the per-course gate (need STATIC RE, not more live
trial — 3 live crashes/mis-gates already):**

1. **`+0x70` kill-switch freezes identity.**  The proven block works by
   making the poll SKIP the driver (`vtable[0x128] = FUN_710074b578`).  But
   the driver is what REFRESHES the registry slot `id_fb` reads — so once the
   gate engages on the first node, `id_fb` freezes there and *every* node
   reads as gated (v7: 1-1 and 1-2 both blocked, `id_fb` stuck at 33).
2. **`+0x30` clear is not the commit trigger, and per-frame scene reads fault
   on teardown.**  Letting the driver run then clearing the poll's `+0x30`
   "requested" port did NOT block entry (1-1 entered) AND crashed
   (0xC0000005) — the `+0x30` write + the per-frame `scene/reg` reads hit
   freed memory during the course-load teardown.

**What the next session must pin down statically:**
- The **real entry-commit trigger**: what does the poll/driver set that
  `SetCourseInFlag` (`FUN_710088cfdc`, the commit) consumes?  Trace how the
  driver's published `+0x20`(mode)/`+0x28`(index) reach the commit actor's
  `+0x20` courseInfo.  The lever must cancel entry WITHOUT skipping the
  driver (so `id_fb` stays fresh).  Candidates: reset the driver's
  `+0x20`/`+0x28` outputs to their cleared values (0 / -1); or find the
  course-in request flag on the courseInMgr / sequence controller.
- A **transition-safe guard** for any per-frame world-map read: the `scene`
  global (`*(mainBase+0x3625850)`) goes stale during the course-load
  teardown and `*(scene+0x40)` faults.  Need a "world-map active & stable"
  predicate (e.g. gate on the SceneTransition window, or a scene-state byte)
  before dereferencing, or move identity to a teardown-safe object.

Current build: **INERT** (gate disabled, Orig-only) — safe & playable.
The kill-switch lever and the `id_fb` identity are both proven; only the
*combination* (cancel-without-freeze + transition safety) remains.

### Per-course identity leads (for step 2 — not yet built)

- **Hovered-node gate id from a global:** the `GetPlayerOnCoursePointGateId`
  nerve body `FUN_7101793c7c` calls `FUN_7101b70f30()` (no args) which
  reads global state keyed by hash `0x6c259974`, gates on a scene-mode
  check (`FUN_71005a0e90() == 2`), and returns `*FUN_7101791fc4(obj)` =
  the current node's **gate id**. This is identity-from-a-global, callable
  from any stable hook. Caveat: it returns a *gate id*, not the 8-byte
  course key / `descriptor+8` hash — confirm the gate-id↔AP-course mapping
  (likely the bridge can key on gate id, or walk node→`+0x350`→`+0x108`
  for the true key as the GoToCenter gate does).
- **World-map player / registry globals:** world-map player is
  `*(DAT_7103625850 + 0x98)`; course-point registry is
  `*(DAT_7103625850 + 0x40)` (indexed by a course index via
  `FUN_71001d5aa0`).
