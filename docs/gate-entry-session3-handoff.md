# Gate-entry — session 3 handoff (2026-05-30 PM)

> ## 2026-06-03 — bridge-side death-gate shipped for two specific cases
> The Switch-side *pre-commit* and *bounce* gates remain shelved (below).
> But the two cases that actually matter for logic enforcement are now
> handled **entirely bridge-side** via the DeathLink `synthKill` route —
> no Switch RE needed:
>
> 1. Entering a **badge-granting course** (every stage in
>    `processor._STAGE_TO_BADGE_INTERNAL_ID`) without the AP-granted badge.
> 2. Entering the **final Bowser stage** (`0x6895BF00`) without all 6
>    AP Royal Seeds.
>
> On `course_in` the processor emits a `GateEntered` (`protocol.GateEntered`);
> `SMBWContext.handle_gate_entered` evaluates the requirement against AP item
> state and, if unmet, arms a loop that `send_kill`s the player ~10 s after
> entry (grace to pause+leave) and re-arms every ~10 s while they stay inside
> without the item. The `BridgeState.in_course` flag (set on `course_in`,
> cleared on `course_result`/`koopajr_result`) stops the loop the moment they
> leave, so it never fires on the world map. The gate arms even when NOT
> connected to AP (anti-cheat: you can't dodge it by playing offline).
> Toggle: `slot_data["level_entry_gating"]` (default on). This needs none of
> the persistent course-sequence RE the rest of this doc hunts for — that
> hunt is only required for a *non-lethal* bounce.
>
> Relatedly, the bridge **no longer overwrites Royal Seeds on the Switch at
> all** (the old `SetRoyalSeedsAbsolute` push on ReceivedItems / HelloMsg /
> 2 s tick is gone) — Royal Seed state is vanilla-owned, and the death-gate
> above is what enforces AP logic at Bowser. `send_set_royal_seeds_absolute`
> survives only as a manual `/send_royal_seeds [mask]` client command
> (defaults to forcing all 6) for when the death-gate needs a manual
> override.

Authoritative handoff for the next agent on the **AP-controlled course-entry
gating** feature (block entry to chosen courses/palaces under AP state).
Read this first, then [royal-seed-phase-a-findings.md](royal-seed-phase-a-findings.md)
(the "session 2" + "per-course gate" sections) for the deeper trail.

> **LATEST (read this): "## Session 6 update (2026-05-31)" at the BOTTOM**,
> esp. "Capture #1 RESULT" + "capture #2 (DEPLOYED)".  Capture #1 SETTLED: the
> grandparent has NO owner back-pointer (0 owner/fwd hits) BUT pinned the live
> PERSISTENT course-sequence controller — `g1 = DAT_3623670 = 0x20d2fd2db0`, vt
> `0x710349d300`, ctor `FUN_7101baeb20`, code cluster `0x7101bae…` (a
> multiply-inherited al NerveExecutor next to the course/ExitCourseMgr code).
> capture #2 SETTLED: g1's first 0x160 B is byte-identical play-vs-exit ⇒ the g1
> ROOT is not the direct holder.  capture #3 SETTLED: a wide BFS reachability
> search from scene/g1/areaMgr (512 objs) found 0 references to the exit tree ⇒
> the "find the holder by scanning" approach is EXHAUSTED (3 hypotheses
> eliminated).  **SHELVED: all gate-entry CODE was then reverted to `master` (both
> gate-entry hooks + every probe removed → back to the 13-hook baseline,
> redeployed); this handoff is now a DOCS-ONLY PR with zero code change.  RE
> primitives are preserved in this doc + git history.**  **NEXT SESSION (if
> resumed): the PauseResult data lever** (RE where the pause "Return to World Map" result is stored + read
> by the persistent course sequence; set that field during play) — see "NEXT
> SESSION" in the Session 6 update.  Read Session 5 first for the SETTLED
> transient-tree context.  ⚠️ Bounce = 6 sessions; bridge auto-resolve already
> fixes the user bug, so this is NOT urgent.  SETTLED empirically (session 5): the ENTIRE exit sequence
> tree (`ctx`+`parent`+`grandparent`) is TRANSIENT — mapped-but-empty during
> play, built only on course-out; NO persistent parent to latch/drive (latch
> AND parent-persistence hypotheses both dead).  Spawn mechanism fully RE'd
> (SceneFactory vtable `0x710349cf38` → `FUN_7100229fa4` builds the grandparent
> — decompiled session 6: writes NO owner back-pointer; parent via
> `FUN_7100062a0c`; ctx via state-key `0x99137dfe`/`FUN_7101be3934`).  Goal:
> drive the PERSISTENT al SceneController (`g1=DAT_3623670`, vtables
> `0x710349d…`) that BUILDS the exit scene.  Session 6 deployed the probe to
> find that owner at the course-out moment.  Build is SAFE/PLAYABLE (one
> read-only probe ON for the capture; revert `kGrandparentOwnerProbe` to false
> after).

The bridge-side Royal-Seed auto-resolve already fixes the immediate bug; this
is the *general* Switch-side gating capability.

---

## TL;DR

- **Two primitives are proven and reusable:** the course-in **kill-switch**
  (`*(courseInMgr+0x70)=1` blocks course-in, no scene load, no crash) and the
  hovered-node **identity** (`id_fb`, an int that names the node: 1-1=33,
  1-2=1362, 2545=next).
- **The pre-commit gate is a DEAD END.** Course-in is fused to world-map
  navigation: every Switch-side intercept either freezes identity, breaks
  movement, or crashes on teardown (9 live iterations, documented below).
- **Pivot to the BOUNCE** (let the gated course load, then immediately exit via
  the game's own course-out). It never touches world-map nav state, so it
  sidesteps the whole problem. The blocker: a clean callable trigger for a
  *complete* course-out (`requestCourseOut()` does teardown but not the scene
  change). The course-out is an al-Sequence/Nerve state machine across several
  actors; no static handle found yet.
- **Recommended next step:** a SAFE observability build that captures the
  controller + trigger when YOU manually pause → "Return to World Map", then
  replicate it. Details in "Next steps" below.
- **Current deployed build is SAFE/PLAYABLE** (gate hook inert).

---

## Session 3 update (2026-05-31) — step 1 (observability) BUILT + DEPLOYED

Next-steps #1 is implemented, built clean, and deployed. **Log-only, no
behavior change** — the build stays SAFE/PLAYABLE (gate probes confirmed
inert: `kBlockAllCourseEntry=false`, `kForceCourseInForbidden=false`,
`kTestGatedIdFb=-1`). Hook count 17 → **18**.

What was added to `switch-mod/src/main.cpp`:
1. **Controller capture** — inside the existing `nerveActivateOnceHook`
   (on `FUN_7100559f7c`), a new branch `if (vt_off ==
   kVtableOff_CourseExitByAreaTag /* 0x33fd690 */ && nerve)` logs the
   course-sequence controller and dumps `nerve+0x00..0x80` plus the
   NerveKeeper pointer at `nerve+0x68` (and `keeper+0x00..0x60`). Bounded
   to the first 8 captures. Log lines: `COURSE_EXIT_BY_AREATAG: controller=…`
   / `  ctrl +0xNN: …` / `  keeper@+0x68 = …` / `  keeper +0xNN: …`.
2. **Chain capture** — a new log-only trampoline `exitCourseMgrBodyHook`
   on the ExitCourseMgr body `FUN_7101be3a5c` (`+0x1be3a5c`) dumps its
   `ctx` (first arg, `ctx+0x00..0x80`) plus the two course-out globals
   `DAT_7103628398` (`+0x3628398`) and `DAT_7103623670` (`+0x3623670`)
   (each `+0x00..0x40`), and cross-checks `probe::courseManager()`. Calls
   Orig unconditionally. Log lines: `EXIT_COURSE_MGR_BODY: ctx=…` /
   `  ctx +0xNN: …` / `  DAT_3628398 = …` / `  g0 +0xNN: …` /
   `  DAT_3623670 = …` / `  g1 +0xNN: …` / `  courseManager() = …`.
   Helper `dumpWords(tag, addr, nwords)` does the 2-words-per-line dump.

Build/deploy verified: markers `ExitCourseMgrBody`, `COURSE_EXIT_BY_AREATAG`,
`EXIT_COURSE_MGR_BODY`, `18 hooks` all present in the uncompressed
`build/smbw_archipelago.nss` ELF; the packaged `build/exefs/subsdk9`
(LZ4-compressed) + `build/exefs/main.npdm` were deployed to
`%APPDATA%\Ryujinx\mods\contents\010015100b514000\smbwap\exefs\` (as
`subsdk9` + `main.npdm`) from THIS worktree, deployed copy hash-matched.
NOTE for future deploys: the hakkun build emits the subsdk under
`build/exefs/` (and `build/sd/atmosphere/contents/…/exefs/`), **not**
`build/subsdk9` as the old CLAUDE.md exlaunch snippet says.

### Capture procedure (the human step — then I continue)

1. Boot SMBW in Ryujinx; confirm the boot banner: a `[smbwap inf]` line
   containing `18 hooks … gate-entry session-3 course-out capture`, and
   `install ExitCourseMgrBody @ +0x1be3a5c OK`.
2. Enter ANY course (a normal level is fine — does not need to be gated).
3. **Pause → "Return to World Map"** (the natural course-out).
4. Both captures fire. Tail the log and grab every `[smbwap` line around
   the exit, especially `COURSE_EXIT_BY_AREATAG` / `EXIT_COURSE_MGR_BODY`
   and their `ctrl`/`keeper`/`ctx`/`g0`/`g1`/`courseManager()` dumps.
   (Strip NULs: ``Get-Content … | %{ $_ -replace "`0","" }``.)

### What the capture unblocks (the RE that follows)

From the dumped layout: (a) the controller's reachable-from-a-global path
(so `PlayerTickLatch` can fetch it), (b) the heap exit-Nerve object stored
on the controller (likely off the keeper at `+0x68`), (c) `al::setNerve`.
Then `setNerve(controller, exitNerve)` from `PlayerTickLatch` = the
complete bounce, wired to the bridge like badge-sync (bridge decodes
`course_in`, pushes `RequestCourseOutMsg` for gated courses).

### Capture #1 result (2026-05-31, pause→"Return to World Map" in 1-2)

Boot banner + all 18 installs confirmed live (incl. `ExitCourseMgrBody @
+0x1be3a5c OK`). Only ONE of the two captures fired:

- ❌ **`COURSE_EXIT_BY_AREATAG` did NOT fire.** Across the whole session only
  two vtables passed through `NerveActivateOnce`: `0x33fd9a8` (SceneTransition,
  fire #1, state `0x00ff003700000084`) and `0x33fd4c8` (fire #2). The
  `RequestEventCourseExitByAreaTag` vtable `0x33fd690` never appeared — so the
  pause→Return-to-World-Map path does **not** route that nerve's slot-8 through
  `FUN_7100559f7c`. (The handoff's assumption it would was wrong for this exit
  type; `0x33fd690` may be the pipe/area-tag warp-out only.) **Drop the
  RequestEventCourseExitByAreaTag controller-capture approach.**
- ✅ **`EXIT_COURSE_MGR_BODY` fired** — `FUN_7101be3a5c(ctx)` ran on the exit,
  and the dump is the controller we need:

```
EXIT_COURSE_MGR_BODY: ctx=0x20bf799330
  ctx +0x00: 000000000b9ac200 00000020bf798dc0   ; +0x00 vtable (main-mod), +0x08 parent=0x20bf798dc0
  ctx +0x10: 0000000000000000 00ff000800000084   ; +0x18 Nerve state-word 0x00ff0008_00000084
  ctx +0x20: 00000020bf799440 00000020bf799310
  ctx +0x30: 00000020bf7993e8 00ff000400000662   ; +0x38 state-word
  ctx +0x40: 0000000000000070 000000000b910d50
  ctx +0x50: 00000020bf798dc0 00000020bf799408   ; +0x50 parent again
  ctx +0x60: 00ff000900000025 00000000cd54e22d   ; +0x60 state-word, +0x68 hash 0xcd54e22d
  ctx +0x70: 000000000af43475 000000000af43475
  DAT_3628398 = 0x2092441fe8   (g0+0x20..0x30 = 0x20dab4d730/830/930)
  DAT_3623670 = 0x20d2fd2db0
  courseManager() = 0x20dab50848    ; ← chain VALID at exit time
```

Findings:
1. **`probe::courseManager()` resolves to a valid pointer at exit time**
   (`0x20dab50848`, in the same `0x20dab4xxx` cluster the `DAT_3628398`
   chain walks through). So the chain + `requestCourseOut()` plumbing is sound.
2. **`ctx` (0x20bf799330) is the course-sequence controller** — a C++ object
   (vtable `0x0b9ac200`, a main-module address; main module is loaded low,
   heap is `0x20xxxxxxxx`). It carries Nerve state-words at +0x18/+0x38/+0x60
   and a parent back-pointer at +0x08/+0x50 (`0x20bf798dc0`). This is the
   object to drive for the bounce.
3. **`ctx` is NOT in `DAT_3628398`/`DAT_3623670`'s first 0x40 bytes**, so its
   reachable-from-a-global path is still unknown — that's the next RE target.

### Refined next step (RE) — find ctx's global path, then bounce

1. Ghidra: decompile `FUN_7101be3a5c` (`+0x1be3a5c`). Determine (a) what `ctx`
   (`this`) is and its class via vtable off `0x0b9ac200 − mainBase`, (b) **who
   calls it** and from what global/anchor (so `PlayerTickLatch` can fetch the
   same `ctx`), and (c) whether `FUN_7101be3a5c(ctx)` performs the FULL exit
   (teardown + sets the controller nerve success → sequence advances → scene
   change) vs. just the `FUN_7101a612cc` teardown. Recall `requestCourseOut()`
   (= `FUN_7101a612cc(courseMgr)` only) did teardown WITHOUT a scene change, so
   the scene change lives in the extra work `FUN_7101be3a5c` does around it.
2. Bounce candidates, simplest first: **(B) call `FUN_7101be3a5c(ctx)` directly**
   from `PlayerTickLatch` (replicates the exact engine exit) once we can fetch
   `ctx` from a global; or **(A) `setNerve(ctx, exitNerve)`** per the original
   plan. Either needs ctx's anchor from step 1.
3. Wire to the bridge like badge-sync (decode `course_in` → push
   `RequestCourseOutMsg` for gated courses → fire the bounce from
   `PlayerTickLatch`, gated by `isSaveLoaded()` / not-in-transition).

---

## Session 4 update (2026-05-31) — RE complete; capture #2 (anchor-finder) deployed

The Ghidra RE in "Refined next step" #1 is **done** (decompiles below). The
exit mechanism is fully understood; the ONE remaining runtime unknown is
`ctx`'s global path, and a SAFE log-only **capture #2** is built + deployed to
resolve it in one boot+course+exit. Build stays SAFE/PLAYABLE (still 18 hooks,
gate probes inert; the only change is additive logging inside the existing
log-only `exitCourseMgrBodyHook`).

### What the decompiles say (answers to RE steps a/b/c)

**`FUN_7101be3a5c(ctx)` — the ExitCourseMgr nerve body (full decompile):**
```c
void FUN_7101be3a5c(long ctx) {
  if (DAT_7103628398 chain valid -> courseMgr = c (via +0x30->+8->**(+8)->+0x118)) {
    params.flag = 0;
    params.exit_type = clamp(*( *(ctx+0x20) & ~3 ), 0, 4);   // exit reason via ctx+0x20
    params.f8 = *(courseMgr+0x98);
    FUN_7101a612cc(courseMgr, &params);                      // == probe::requestCourseOut()
  }
  *(uint*)(ctx+0x18) = (*(uint*)(ctx+0x18) & 0x3fffffff) | 0x80000000;  // STEP-ADVANCE
}
```
- **(c) FULL vs teardown:** the body = teardown (`FUN_7101a612cc`, which is
  exactly `requestCourseOut()`) **plus** the single write
  `*(ctx+0x18) = ...&0x3fffffff | 0x80000000`. That write is the ONLY thing
  the body does beyond the teardown — and it's what the teardown-only smoke
  test was missing. **`ctx+0x18` is the controller's al-Nerve step word; the
  `&0x3fffffff | 0x80000000` idiom = "mark this nerve step complete → the
  NerveKeeper advances to the next nerve".** PROOF it's the generic advance
  idiom, not exit-specific: `CheckNextGoToWorldMap` (`FUN_710163e944`, the
  step AFTER ExitCourseMgr) runs on the **same `ctx`** and ends with the
  **identical** write:
  ```c
  void FUN_710163e944(long ctx) {       // CheckNextGoToWorldMap
    FUN_710163f144(ctx+0x98, ctx+0x20); // the actual world-map load request
    FUN_710163e990(ctx);
    *(uint*)(ctx+0x18) = (*(uint*)(ctx+0x18) & 0x3fffffff) | 0x80000000;
  }
  ```
  So the sequence is `… → ExitCourseMgr(teardown + advance) → keeper advances →
  CheckNextGoToWorldMap(load world map + advance) → …`. **courseMgr comes
  entirely from the global chain, NOT from `ctx`** — so the bounce only needs
  `ctx` (for the exit-reason read at +0x20 and the step write at +0x18).

- **(a) `ctx`'s class:** the **course-sequence controller**, class vtable
  Ghidra **`0x71034a8200`** (runtime `0x0b9ac200`). It's an al `NerveExecutor`-
  derived class (16 vtable slots; base methods at `0x7101449xxx`,
  `0x71000ac930` typeinfo slot). Its ExitCourseMgr **nerve** vtable
  (`0x71034a8290`, slot 0 returns the string `"ExitCourseMgr"`) sits packed
  immediately after the class vtable. `ctx+0x18/+0x38/+0x60` are nerve step
  words (tag `0x00ff00XX_........`); `ctx+0x08`==`ctx+0x50`==parent;
  `ctx+0x20` → exit-reason obj. Confirmed: `FUN_7101be3934` (an al nerve-
  factory) sets byte `+0x1e=0xff`, and live `ctx+0x1e` IS `0xff`.

- **(b) who calls it / `ctx`'s anchor:** `FUN_7101be3a5c` is dispatched by the
  NerveKeeper via the nerve vtable slot 8 — there is no named caller. `ctx`'s
  reachable-from-a-global path is **still the open item**; capture #2 (below)
  finds it at runtime.

**`FUN_7101a612cc(courseMgr, params)` — the teardown (`requestCourseOut`):**
sets `courseMgr+0x94=3`, tears down ~12 sub-objects, records a gmd stat
(`FUN_710049f648(gmd,_,0xb47087e6)`), pokes the scene global `DAT_7103625850`,
sets the course-in kill-switch bit (`DAT_7103630d28+0x68 |= 8`), walks the
`DAT_7103628398` chain to `+0x120`. It preps everything but does NOT itself
load the world map — confirming the scene change is the controller's nerve
advance, exactly as theorized.

### ★ Major unlock: runtime main base `B_main = 0x08504000` (dev Ryujinx)

`ghidra = 0x7100000000 + (runtime − 0x08504000)`. Derived from the area-mgr
singleton: its ctor `FUN_7100654ee0` stores Ghidra vtable `0x7103488658`
(offset `0x03488658`) at obj+0; capture #1 showed that object's `+0x00 =
0x0b98c658`, so `B_main = 0x0b98c658 − 0x03488658 = 0x08504000`. Double-checked
via `ctx+0x48 = 0x0b910d50 → ghidra 0x710340cd50` (a valid sub-object vtable)
and `ctx+0 = 0x0b9ac200 → 0x71034a8200` (slot 0 = `FUN_7101be3970`). This is
the mapping prior sessions lacked; it makes any future heap dump decodable.
Saved as memory `reference_smbw_runtime_main_base`. ⚠️ DEV-ONLY — real-HW ASLR
moves it; capture #2 logs `getMainModule()` to re-pin on any new env.

### The clean bounce = option A (`setNerve(ctx, ExitCourseMgrNerve)`)

Because `ctx+0x18` is the keeper's *current-nerve* step word, **option B**
(calling `FUN_7101be3a5c(ctx)` out-of-band while the controller is in its
"playing" nerve) would run the teardown AND mark the WRONG nerve's step
complete → undefined transition. **Option A** is the principled path: tell the
keeper to switch to the ExitCourseMgr nerve; next frame it runs the body +
advances naturally. Option A needs: `ctx` + the per-instance ExitCourseMgr
nerve object (stored on the controller; heap, no static singleton) + `al::
setNerve`. All three become reachable once `ctx` is in hand — capture #2 gets
`ctx`, and the controller dump (parent + keeper sub-objects at
`ctx+0x20/0x28/0x30/0x48/0x58`) lets us locate the nerve object + setNerve.

### Capture #2 — DEPLOYED NOW (the human step)

Log-only additions to `exitCourseMgrBodyHook` (`FUN_7101be3a5c`,
`+0x1be3a5c`): on the first natural course-out it logs `B_main`, dumps the
controller's `parent` (`ctx+0x08`, 32 words), and runs `anchorSearch` — a
bounded **2-hop pointer search** from the engine globals (`DAT_3628398`
areaMgr, `DAT_3623670`, `DAT_3625850` scene, `DAT_3630a88`, `DAT_3628d90`,
`DAT_3630d28` courseInMgr) **and** the live `courseManager()` — for the value
`ctx` and its `parent`. Reads only; the inner (2nd-hop) scan only follows
values that validate as live C++ objects (first qword = a main-module vtable
in `[B_main, B_main+modsize)`), so it won't chase a scalar-that-looks-like-a-
pointer into an unmapped page. Banner now says `gate-entry session-3 capture
#2 anchor-search … 18 hooks`.

**Procedure:** boot SMBW → confirm banner + `install ExitCourseMgrBody @
+0x1be3a5c OK` → enter ANY course → pause → "Return to World Map". Grab every
`[smbwap` line, especially `B_main(getMainModule) = …`, `parent(ctx+0x08) =
…`, the `parent +0xNN:` dump, and any `ANCHOR: … == ctx` / `== parent` /
`anchorSearch done: N hit(s)`. (Strip NULs.)

**Expected outcome + next step:**
- ≥1 `ANCHOR: <global> + 0xA [-> + 0xB] == ctx` → that IS the runtime path;
  add a `probe::courseSequenceController()` walker (mirrors
  `probe::courseManager()`), then implement option A from `PlayerTickLatch`.
- Only `== parent` hits → global→parent is known; the `parent` dump shows
  parent→ctx offset; combine for the full walk.
- 0 hits → `ctx` is >2 hops from every seed; widen `anchorSearch` (more seeds
  / depth 3) or seed from `g1`'s deeper nodes — the `parent` dump still gives a
  static lead (decode parent's vtable via `B_main`).

(Deferred candidate if the anchor proves unstable across courses: latch `ctx`
in `exitCourseMgrBodyHook` on the first natural exit and validate structurally
before reuse — but the controller may be re-created per course-load, so the
global walk is preferred.)

### Capture #2 RESULT (2026-05-31) + capture #2b deployed

Capture #2 ran. **`B_main = 0x8504000` confirmed live** (matches the static
derivation exactly). **`anchorSearch: 0 hits`** — neither `ctx` nor `parent`
is within 2 hops of any seed global, because the al scene/sequence tree that
owns `ctx` is rooted **deep inside the SCENE object** (`DAT_3625850` is huge —
`FUN_7101a612cc` reads `scene+0x2c40`), far past the 0x180 window. New facts:

- **`parent = 0x20bf798dc0` (== `ctx+0x08`), class vtable Ghidra `0x7103517068`**
  (runtime `0x0ba1b068`). It's an al sub-sequence with its own nerve step words
  (`parent+0x70 = 0x00ff0009…`) and a likely grandparent at `parent+0x08 =
  0x20bf796368`. `parent` does NOT hold `ctx` in its first 0x100.
- **The tree structure (static RE):** the parent (0xe0 bytes, vtable
  `0x7103517068`) is built by **`FUN_7100062a0c`** and stored into a
  grandparent's array (`*(grandparent+0x20) + idx*0x18`, count `grandparent+
  0x18`). Grandparent built by **`FUN_7100060b00`** (sead HeapMgr alloc),
  called from al builders `FUN_710005ceb0` / `FUN_7101f405e0` (one has an
  18960-byte stack frame). This is generic al sequence/scene framework — the
  rooting global is many hops up; tracing it statically is not worth the hops.

**capture #2b (DEPLOYED)** replaces `anchorSearch` with v2: (1) an **ancestor
climb** that follows `+0x08` up from `ctx`, logging each level's address +
vtable mapped to Ghidra (`L0 obj=… vt(ghidra)=…`), naming the whole chain and
the ROOT class; (2) a **wide scan** of the scene + its `+0x40` sub-object (and a
2-hop scan of the smaller globals) for `ctx`/`parent`/any climbed ancestor.
Banner now: `…capture #2b ancestor-climb … 18 hooks`. Same procedure (boot →
any course → pause → Return to World Map). The climb's top level whose
`vt(ghidra)` we can find a global for IS the anchor; from there, the downward
path to `ctx` is read off the wide-scan `ANCHOR:` hits + the al builder
decompiles (`FUN_7100062a0c` shows grandparent→array→parent).

### Capture #2c RESULT (the climb, 2026-05-31) + crash → capture #3 (QueryMemory-safe)

The climb fired and gave **3 levels** before crashing:

| L | object (runtime) | class vtable (Ghidra) | what |
|---|---|---|---|
| 0 | `0x20bf799330` | `0x71034a8200` | `ctx` — ExitCourseMgr course controller |
| 1 | `0x20bf798dc0` | `0x7103517068` | `parent` — al sub-sequence |
| 2 | `0x20bf796368` | `0x71033f9660` | `grandparent` — 0x208-byte al sequence container |

Then it **crashed reading `grandparent+0x08`** (PC in our subsdk). Meaning: the
`+0x08` chain TOPS OUT at the grandparent — `+0x08` is the al host pointer only
for actor-ish nodes (ctx, parent); the grandparent (class `0x71033f9660`) lays
out `+0x08` as something else, and that value was an in-heap-window but
**unmapped** address → the "arena is always mapped" assumption was wrong.

Static follow-up on the grandparent class:
- `0x71033f9660` is built by the **virtual factory `FUN_7100229fa4`** (alloc
  0x208, vtable, by a type selector `param2[0x509]==6`; sibling type 5 → vtable
  `0x71033f9610`).  The factory is invoked by **indirect dispatch** (its address
  sits in a table at `0x710349cf58`), so the caller chain to a global isn't
  cheaply traceable statically → resolve the anchor at runtime instead.
- **`ctx` is STABLE at `0x20bf799330` across ALL captures (incl. different
  sessions)** — the dev Ryujinx allocator is deterministic.  So once we know a
  global→grandparent offset, the whole walker is fixed for dev.

**capture #3 (DEPLOYED)** = `anchorSearch` v3.  Fixes the crash by making EVERY
read fault-safe via `hk::svc::QueryMemory` (checks the page is mapped+readable,
caches the last-good region so a sweep is a few SVCs), and re-enables a **wide
scan of the scene + its `+0x40` sub-object** (plus 2-hop of the small globals)
for `ctx`/`parent`/**grandparent**.  Banner: `…capture #3 anchor-scan
(QueryMemory-safe)…`.  Same procedure.  Expected: an `ANCHOR: scene[+…] ==
ancestor[L2]` (or `scene+0x40 + … == …`) hit naming the grandparent's offset in
the scene → that + `B_main` gives a `probe::courseSequenceController()` walker
(scene global → … → grandparent → array(+0x20) → parent → ctx), and then option
A (`setNerve`) / the bounce can be implemented.  If still 0 hits, the tree is
off a different global — widen seeds next, or pivot to a per-frame vtable latch
on `ctx` (class `0x71034a8200`), which the stable address makes trivial in dev.

### Capture #3 RESULT + BOUNCE PRIMITIVE IMPLEMENTED (2026-05-31)

capture #3 ran **clean (no crash — the QueryMemory fix worked)**: `B_main=
0x8504000`, climb `ctx(0x71034a8200) → parent(0x7103517068) → grandparent
(0x71033f9660)`, then stopped gracefully.  **`anchorSearch v3: 0 hit(s)`** — the
grandparent is NOT in the scene / `scene+0x40` / the small globals within the
scanned windows.  The al sequence tree roots off some other global, past a cheap
runtime sweep.  So the robust global→ctx walker is deferred; instead we use the
fact that **`ctx` is stable at `0x20bf799330` and is the live first arg of the
ExitCourseMgr-body hook**.

**Implemented (build "gate-entry session-3 bounce primitive … 18 hooks"):**
- `probe::latchCourseSeqController(ctx)` / `courseSequenceController()` /
  `bounceCourseOut()` in `switch-mod/src/probe/Gates.{hpp,cpp}`.  The latch is
  called unconditionally from `exitCourseMgrBodyHook` (every natural exit,
  validated by vtable == `mainBase()+0x34a8200`).  `bounceCourseOut()` re-checks
  the vtable + a valid `courseManager()`, then calls `FUN_7101be3a5c(ctx)` (NSO
  `+0x1be3a5c`) — the COMPLETE course-out (teardown + step advance).
- `kBounceSmokeTest` in `main.cpp` (default **false** → dead-code-eliminated,
  build SAFE).  When true: after the controller is latched (one natural exit)
  and the player is ~300 ticks into a *re-entered* course, it fires
  `bounceCourseOut()` once.  Test flow: course A → pause→Return-to-Map (latch) →
  course B → ~5 s later B bounces to the world map.

**⚠️ Known risk of the bounce (option B) still UNVALIDATED:** `FUN_7101be3a5c`
writes `*(ctx+0x18)` = the controller's *current* Nerve step word.  Firing it
mid-play (when ctx is in a "playing" nerve, not the ExitCourseMgr nerve) marks
the WRONG step done → the keeper may advance the playing nerve unexpectedly, and
the teardown has already broken the course, so a soft-lock/crash is possible.
The teardown call itself is proven safe (== `requestCourseOut()`), and the extra
work is just a u32 write, so a *hard* crash is not the most likely outcome — but
it's not ruled out.  The clean alternative is **option A (`al::setNerve(ctx,
ExitCourseMgrNerve)`)**, which transitions properly; it needs the per-instance
ExitCourseMgr nerve object (stored on ctx/its keeper) + `al::setNerve`, both
still to be RE'd (next session if the smoke test shows option B is unsafe).

### ❌ Latch+bounce TESTED — DEAD END (2026-05-31): the controller is TRANSIENT

Flipped `kBounceSmokeTest` on and ran the test (enter A → pause→Return-to-Map →
enter B → fire at tick 300).  Result: **`bounceCourseOut -> 0` (no crash)**, then
a diagnostic at ticks 1/150/300 in course B showed:
```
ctrl=0x20bf799330 readable=1 vt=0x0 expect=0xb9ac200 cmgr=0x20dab50848
```
i.e. the latched address is mapped but its **vtable is `0x0` the entire time
during course B**.  So the ExitCourseMgr controller is a **TRANSIENT object,
created when the course-out is requested and freed (zeroed) afterward — NOT a
persistent object that exists during play.**  Consequences:
- A pre-latched `ctx` is **never valid mid-play** → the latch approach is dead.
- `bounceCourseOut()`'s vtable re-check did its job (rejected the freed object →
  returned 0 instead of calling into garbage → **no crash**).  Keep that guard
  pattern.
- The "stable 0x20bf799330" was an artifact of all captures being taken at the
  pause→exit MOMENT; during play that slot is freed.

Static confirmation: the sequence factory `FUN_7100229fa4` lives in a vtable at
`0x710349cf38` whose ctor `FUN_7100942e70` allocates only an **8-byte** object
(vtable-only) — a **stateless al SceneFactory**.  It doesn't hold the sequence;
the al scene-init flow that calls it does, deep in framework code.  So neither a
runtime sweep (capture #3: 0 hits) nor a cheap static climb reaches the live
sequence.

**Current state:** `kBounceSmokeTest=false` → safe/playable build deployed
(banner "bounce primitive present but INERT").  `probe::{latchCourseSeqController,
courseSequenceController,bounceCourseOut}` are kept in Gates.{hpp,cpp} for when a
live `ctx` is obtainable; the latch still runs on natural exits (harmless).

### Next direction (the real path)

Drive the **PERSISTENT pause-menu "Return to World Map" request** — the thing
that CREATES the transient exit controller — rather than the controller itself.
Concretely, next session:
1. RE the pause flow: `game::sequence::PauseResult` (RTTI @ 0x71028b6339),
   `IsExecuteNormalCourseOutDemoForWorldMapPlayer` (0x71028d49b5),
   `SetResultFailureIfExitStage`.  Find what the "Return to World Map" selection
   sets/calls and whether the target (the persistent course sequence = parent
   class `0x7103517068`, or a pause/course-out request flag) is reachable from a
   global or the live `courseManager()` during play.
2. Open question to settle first: is the PARENT (course sequence, vtable
   `0x7103517068`) PERSISTENT during play (it runs the course, so likely yes —
   only `ctx` was shown transient)?  If so, a play-time scan for a live
   `0x7103517068` instance + driving its exit transition is viable.  A quick
   diagnostic = the ctx-vtable probe above but reading the PARENT slot during
   play instead of ctx.
3. Fallback already shipped: the bridge-side Royal-Seed auto-resolve fixes the
   immediate user-facing bug, so the general Switch-side gate is not urgent.

---

## Environment / gotchas (don't relearn)

- Ghidra MCP: `open_program("/main.nso")` first, then `run_script_inline`
  (drive `DecompInterface` at 90 s; the `decompile_function` wrapper times
  out). NSO base `0x7100000000`. Nerve resolution recipe (used repeatedly):
  a Nerve name string's xref points into a tiny `adrp;add;ret` name-getter at
  `(strRefAddr - 4)`; find the vtable via `findQword(getterAddr)`; slot 8
  (offset 0x40) = the execute body.
- **Build/deploy is booby-trapped — verify every deploy:**
  - The repo build auto-deploys to `build/sd/atmosphere/contents/…/exefs/subsdk9`.
    Real Ryujinx (`Desktop\ryujinx-1.3.3`, non-portable) loads from
    `%APPDATA%\Ryujinx\mods\contents\010015100b514000\smbwap\exefs`.
  - **Something else (a build of the MAIN repo, not this worktree) overwrote
    `%APPDATA%` with an ancient 13-hook `subsdk9` mid-session.** Always
    re-deploy from THIS worktree and confirm in-game banner.
  - **`subsdk9` is LZ4-compressed**, so plaintext string search on it gives
    false negatives. Verify against the linked **`build/smbw_archipelago.nss`**
    (ELF, uncompressed) instead — `.nss.Contains('<banner>')`.
  - ninja sometimes thinks `main.cpp.obj` is up-to-date after an edit (mtime
    race from the concurrent process). Force it: bump `main.cpp` mtime
    (`(Get-Item main.cpp).LastWriteTime = Get-Date`) before `cmake --build`.
- Tail logs: `Desktop\ryujinx-1.3.3\Logs\Ryujinx_*.log`, strip NULs
  (`-replace "`0",""`). Our lines: `[smbwap inf] PhaseB …`.

---

## Proven primitives (reuse these)

### 1. Course-in kill-switch (blocks, never crashes, but coarse)

`courseInMgr = *(mainBase + 0x3630d28)` (`DAT_7103630d28`) — a stable sead
singleton (0x98 bytes, allocated once in `FUN_7100655408`). The world-map
input poll `CheckCourseInUIKey` (`FUN_710022a964`, +0x22a964) starts course-in
only when `*(char*)(courseInMgr+0x70) == 0`. **Forcing that byte to 1 blocks
ALL course-in, pre-commit, no scene load** — confirmed clean over 3584 polls,
player could still walk freely. This is the block lever; it just can't be made
per-course (see dead-end below).

### 2. Hovered-node identity `id_fb`

`id_fb = *(int*)( *(*(mainBase+0x3625850) + 0x40) + 0x110 )`
= worldMapScene → registry (`scene+0x40`) → player-0 "reserved open course"
fallback slot (`registry+0x100`) → id (`+0x10`). Live values: **1-1 → 33,
1-2 → 1362, next → 2545** (stable while settled on a node, `cp_type=4`).
SAFE to read (one int off the range-validated registry) ONLY when not in a
scene transition (see crash note). The poll actor's vtable is NSO `0x34b8798`;
its course-in driver is `vtable[0x128] = FUN_710074b578`, which RESERVES the
hovered node into that slot (so it's the thing that updates `id_fb`).

### 3. Course-out teardown (incomplete on its own)

`probe::requestCourseOut()` / `probe::courseManager()` in
`switch-mod/src/probe/Gates.{hpp,cpp}`. `courseManager()` =
`DAT_7103628398 → +0x30 → +8 → *(+8) → +0x118` (verified in-course only; NULL
on the world map). `requestCourseOut()` calls the course-out executor
`FUN_7101a612cc(courseMgr,&params)` — does teardown (sets `courseMgr+0x94=3`,
tears down sub-objects) but **does NOT change scene** (smoke-tested). The scene
change needs the sequence controller's Nerve to advance after the
`ExitCourseMgr` Nerve body runs.

### 4. Transition guard

`probe::isInSceneTransitionWindow()` (declared `ap/ApFrameBridge.hpp`, already
used in main.cpp) — true for 3 s after a SceneTransition Nerve fires. REQUIRED
before any per-frame world-map pointer read: the scene global
`*(mainBase+0x3625850)` goes STALE during the course-load teardown and
dereferencing it faults (`0xC0000005`). `kLooksLikePtr` in main.cpp was
tightened to the Ryujinx heap window `[0x2000000000,0x2800000000)` + 8-byte
alignment — note this is DEV-only; real-HW ASLR will move the heap.

---

## Why the pre-commit gate is a DEAD END

Course-IN is woven into world-map navigation; the "identity" is the same state
navigation depends on. Every lever broke something (all live-tested):

| Attempt | Lever | Result |
|---|---|---|
| v1 | `+0x70=1` for ALL nodes | ✅ blocks, player walks — but coarse |
| v7 | `+0x70=1` only when `id_fb==33` | ❌ **freezes identity**: `+0x70` skips the driver, and the driver is what refreshes `id_fb`; once it gates one node `id_fb` sticks at 33 → every node mis-reads as gated |
| v8 | let driver run, clear poll port `+0x30` after Orig | ❌ didn't block (not the commit trigger) **and crashed** (per-frame scene reads on teardown) |
| v9 | let driver run, clear the reservation slot (`reg+0x110 = -1`) | ❌ **broke movement entirely** — the driver does NOT re-reserve; the reservation slot IS the player's current-node anchor, so the player froze in place |

**Conclusion:** there is no clean pre-commit chokepoint. Do not re-attempt the
pre-commit gate. The entry decision and the navigation cursor are the same
state machine.

(For reference, the commit itself is `SetCourseInFlag` `FUN_710088cfdc`
(+0x88cfdc): `courseInfo = **(actor+0x20)`; `FUN_71000d33e0(courseInfo)` →
descriptor; `descriptor+8` = the per-course hash; writes a container-B bool via
`FUN_710049ea24` — which flows through our existing `GmdBoolWriter` hook, so we
already SEE the entered course's hash at commit, just too late to pre-block.
Trampolining `SetCourseInFlag` crashes — do not.)

---

## The BOUNCE (recommended path) — architecture + what's missing

Let the gated course load, then trigger the game's own course-out (the exact
path pause→"Return to World Map", give-up, and death-with-no-lives all use).
It never touches world-map nav state.

**Sequence (mapped):** the course-sequence controller (an al actor with a
NerveKeeper; its code cluster is `0x7101bd…–0x7101bf…`, same neighborhood as
`ExitCourseMgr` `FUN_7101be3a5c`) drives:
`… → ExitCourseMgr (FUN_7101be3a5c, teardown + sets nerve success) →
CheckNextGoToWorldMap (FUN_710163e944, vtable 0x710338a698) → world-map load`.

The controller's Nerves include (all confirmed via the name-getter→vtable→slot8
recipe): `ExitCourseMgr` (vtable 0x71034a8290), `RequestStageStartEvent`
(0x71034af1c0), `SetResultFailureIfExitStage` (0x71034b3548), `DebugChangeStage`
(0x71034a5878, empty body), plus `RequestEventCourseExitByAreaTag`
(0x71033fd690 — an ACTIVE nerve whose slot 8 = `thunk_FUN_7100559f7c`, so our
existing `NerveActivateOnce` hook on `FUN_7100559f7c` ALREADY sees it fire on a
natural exit, with `nerve`==the controller).

**What's missing:** a callable trigger that drives the controller into the exit
path. Blockers found:
- The Nerve singleton objects are **heap-constructed per-controller** — no
  static `&ExitCourseMgrNerve`, and `al::setNerve` is not a named symbol
  (binary stripped of game symbols).
- The controller's dispatcher/update (the function that reads `PauseResult` and
  transitions to the exit) and the controller's runtime address were not
  pinned down statically. The pause flow is the al-Sequence "…Result" pattern
  (`game::sequence::PauseResult` @ 0x71028b6339, RTTI type-name); the parent
  course sequence reads the result enum and changes stage.

---

## Recommended NEXT STEPS (in order)

1. **Observability build to capture the natural course-out trigger (SAFE,
   log-only — the key unlock).** Our `NerveActivateOnce` hook
   (`nerveActivateOnceHook`, on `FUN_7100559f7c`, in main.cpp) already runs for
   every active nerve and computes `vt_off`. Add: when `vt_off ==
   0x33fd690` (`RequestEventCourseExitByAreaTag`, vtable 0x71033fd690 − base),
   log the controller pointer (`nerve`) and dump `nerve+0x00..0x80` + the
   keeper at `nerve+0x68`. Then YOU manually pause → "Return to World Map" in a
   course. From the captured controller pointer + structure, RE: (a) how the
   controller is reached from a global (so we can get it from PlayerTickLatch),
   (b) the Nerve object stored on it for the exit, (c) the `al::setNerve`
   function. With those three, `setNerve(controller, exitNerve)` from
   PlayerTickLatch = the bounce.
   - Even better, ALSO hook `ExitCourseMgr` body `FUN_7101be3a5c` log-only to
     confirm timing/ordering and capture its `ctx`.
2. **Find the high-level "return to world map" request the pause menu calls.**
   Trace the `PauseResult` consumer (course-sequence update). The course
   sequence reads the result enum; the "Return to World Map" branch calls into
   the exit. If that branch is a callable method on a globally-reachable
   controller, call it directly (cleanest — all engine guards intact). Strings
   to anchor: `game::sequence::PauseResult`, `SetResultFailureIfExitStage`,
   `CheckNextGoToWorldMap`, `IsExecuteNormalCourseOutDemoForWorldMapPlayer`
   (0x71028d49b5), `RequestStageStartEvent`.
3. **Wiring once a trigger works (mirror badge-sync):** bridge holds the
   AP-gated course set; it already decodes `course_in` (knows the entered
   course) — if gated, push a `RequestCourseOutMsg`; the Switch fires the
   bounce from PlayerTickLatch (after `Orig`, a proven-safe in-course context),
   gated by `probe::isSaveLoaded()` / not-in-transition. Course identity on the
   bridge side is already available from the `course_in` PlayReport.

---

## Code state

- `switch-mod/src/main.cpp`: the `checkCourseInUiKeyGateHook` on
  `FUN_710022a964` (+0x22a964) is **installed but INERT** (`kTestGatedIdFb=-1`,
  early-returns after Orig). Keep or remove — it's harmless. The kill-switch
  constants (`kCourseInMgrGlobalNsoOffset=0x3630d28`, `+0x70`), the world-map
  scene offset (`0x3625850`), and `kLooksLikePtr` (heap-tight) are all there
  and documented in-place. `kForceCourseInForbidden=false`.
- `switch-mod/src/probe/Gates.{hpp,cpp}`: `courseManager()`, `requestCourseOut()`
  kept for the bounce follow-up.
- Hook count is 17 (the inert gate hook included). Banner currently:
  "PhaseB per-course gate INERT (safe/playable; pre-commit gate abandoned,
  pivoting to bounce)".

---

## Session 5 update (2026-05-31) — spawn mechanism RE'd; PARENT-persistence diag DEPLOYED

Picks up from "Next direction (the real path)": drive the PERSISTENT host
sequence (not the transient `ctx`).  Did the cheap Ghidra RE of the course-out
*spawn* mechanism, then built + deployed a SAFE log-only **persistence
diagnostic** to settle the gating empirical question.

### What creates the transient ExitCourseMgr controller (RE — done)

The exit controller `ctx` (class vtable Ghidra `0x71034a8200`) is a **0x28-byte**
object **constructed by `FUN_7101be3934`** (allocs 0x28, writes vtable
`&PTR_FUN_71034a8200` at +0, sets byte `+0x1e=0xff` — matches the live capture).
`FUN_7101be3934` is referenced **only as DATA inside `FUN_710094b730`**, a
static registrar that binds it into an al **state-factory** registry:

```c
void FUN_710094b730(void) {            // static-init registrar
  DAT_7103625f20 = 0x99137dfe;         // <-- STATE KEY for ExitCourseMgr ctrl
  DAT_7103625f28 = FUN_7101be3934;     // <-- create-fn (the ctx ctor)
  ... links {key,createfn,..} into the global init chain DAT_71036358a0 ...
}
```

So the engine creates the controller on demand by **requesting state-key
`0x99137dfe`** from the al factory; the factory walks the registry, matches the
key, and calls `FUN_7101be3934`.  The controller is **hosted by the parent
sub-sequence** (`ctx+0x08`, vtable `0x7103517068`), which ticks it; the parent
hosts the keeper that runs `ExitCourseMgr → CheckNextGoToWorldMap → world-map
load`.  **The clean trigger is therefore: drive the persistent host sequence to
request state `0x99137dfe`** (i.e. enter the exit state) — that spawns + ticks
the controller through the framework, which is exactly what the latch+bounce of
the transient `ctx` could not do.

Open RE (deferred — pursue once persistence is settled): **who requests key
`0x99137dfe`.**  It's loaded as an immediate (movz `#0x7dfe`/movk `#0x9913`) by
the requester, NOT via the static node address (the node has only 2 self-refs).
Bounded scans of the ctx (`0x7101bd…`), CheckNextGoToWorldMap (`0x71016 3…`),
IsExecuteNormalCourseOutDemo (`0x71016d…`) and teardown (`0x7101a6…`) clusters
found **no** load of `0x99137dfe` → the requester is elsewhere / reached by
indirect al dispatch.  Other RE landmarks pinned this session:
- `PauseResult` is an **enum** (reflection registrar `FUN_7100581d38`,
  to-string `FUN_7101c0bd9c`), not a class — the pause menu sets a PauseResult
  value that the sequence reads.
- `IsExecuteNormalCourseOutDemoForWorldMapPlayer` is a **Nerve** (vtable
  `0x71033c4478`, slot-0 name-getter `0x71016d1100`); **execute body =
  `FUN_71016d1118`**.  It ends with the same `*(this+0x18)=…&0x3fffffff|0x80000000`
  step-advance idiom and gates on `DAT_7103628398+0x58 == 1` — a course-out-demo
  nerve on a *different* (world-map-player) controller; lower-priority lead.
- teardown `FUN_7101a612cc` is called **only** by the ExitCourseMgr body
  `FUN_7101be3a5c` (confirmed — single caller).

### The diagnostic — DEPLOYED NOW (the human step)

`switch-mod/src/main.cpp` gained **`seqPersistDiag(base, courseMgr, p1, p2)`**
(log-only, one-shot, every read gated by `svc::QueryMemory`), fired from
`playerTickLatchHook` **after Orig**, ~200 player-ticks after settling into a
loaded course (`isSaveLoaded() && courseManager()!=null &&
!isInSceneTransitionWindow()`; counter resets on the world map).  Gate constant
`kSeqPersistDiag=true` (read-only ⇒ safe to ship ON; **revert to false after the
capture is read**).  Also added `probe::latchCourseSeqParent()/courseSeqParent()`
to `Gates.{hpp,cpp}` (validates the parent vtable `+0x3517068`); the parent
(`ctx+0x08`) is latched on every natural exit in `exitCourseMgrBodyHook`.
**Still 18 hooks; bounce/gate probes remain INERT; build SAFE/PLAYABLE.**

`seqPersistDiag` runs three independent probes (targets: ctx `+0x34a8200`,
PARENT `+0x3517068`, GRANDPARENT `+0x33f9660`):
- **(A)** read the **latched parent**'s vtable during play — the cheapest,
  decisive-if-positive signal (the dead-end test only ever read the *ctx* slot).
- **(B)** climb the al host-chain (`+0x08`) up from live seeds (`courseMgr`, the
  two player-tick args, the latched parent), logging each level's Ghidra vtable
  and flagging any of the 3 target classes.
- **(C)** scan the scene + engine globals (2-hop) for the 3 target vtables AND
  **census** the distinct main-module vtables reachable (cap 48) with an example
  path each — so even a partial reach names which persistent al objects hang off
  the scene during play.

Build verified: markers `seqPersistDiag`, `PARENT-persistence diag`, `18 hooks`,
`(A) latched parent`, `(C) census vt`, `GRANDPARENT seq container`,
`latchCourseSeqParent` all present in the uncompressed
`build/smbw_archipelago.nss`; `build/exefs/{subsdk9,main.npdm}` deployed to
`%APPDATA%\Ryujinx\mods\contents\010015100b514000\smbwap\exefs\` from THIS
worktree, **deployed copies hash-matched**.

**Procedure:** boot SMBW → confirm banner `gate-entry session-5
PARENT-persistence diag … 18 hooks` → enter ANY course → just **play for ~5–10 s**
(no pause needed; the diag fires automatically ~200 ticks in).  Grab every
`[smbwap` line from `seqPersistDiag: firing …` through `seqPersistDiag done: …`
(strip NULs: ``Get-Content … | %{ $_ -replace "`0","" }``).

**Decision tree (next session):**
- **(A) shows the latched parent vt == PARENT (or any `<== PARENT/GRANDPARENT`
  flag fires in B/C)** → a host-sequence class object IS persistent + reachable
  during play.  Note its live address + the path (global/scene offset, or the
  climb level from a live seed).  Then add a `probe::courseSequenceHost()` walker
  (dev: the path from the diag; real-HW: re-derive via the same scan) and drive
  it into the exit state — request key `0x99137dfe` on it / `setNerve` to the
  exit nerve.  RE the "request state by key" dispatcher next (the open RE above).
- **(A) reads vt==0 / a different vtable AND no PARENT/GRANDPARENT flag in B/C**
  → the parent is *also* transient (part of the exit machinery, not the
  course-play sequence).  Use the **census (C)** to pick the actual persistent
  course-play sequence object (identify its class offline from its vtable), then
  repeat with that vtable as the target.
- **B's climb tops out below a target** → record the top-of-climb class (the
  persistent root reachable from live actors); the downward path to the host
  sequence is then read off the al builder decompiles (`FUN_7100062a0c`
  grandparent→array(+0x20)→parent).

### Code state delta (session 5)

- `switch-mod/src/probe/Gates.{hpp,cpp}`: added `latchCourseSeqParent()` /
  `courseSeqParent()` (parent vtable `+0x3517068` validated).  Existing
  controller latch / `bounceCourseOut()` unchanged (kept INERT).
- `switch-mod/src/main.cpp`: added `seqPersistDiag()`; `exitCourseMgrBodyHook`
  now also latches the parent; `playerTickLatchHook` fires `seqPersistDiag` once
  per play-session under `kSeqPersistDiag` (true this build — **revert to false**
  after reading the capture).  Banner updated.  Hook count unchanged (18).

### Session 5 capture #1 result + capture #2 (probe D) DEPLOYED

Capture #1 (boot → enter course → play, no exit) — `B_main=0x8504000` confirmed:
- **(A) latched parent = 0x0** — N/A: no prior natural course-out this run, so
  nothing was latched.  Needs the course-A → pause→Return-to-Map → course-B
  protocol (capture #2 below re-arms the diag per-course so course B fires it).
- **(C) 0 target hit(s), 48 distinct vtables** — within the 2-hop scan windows,
  **no** parent (`+0x3517068`) / grandparent (`+0x33f9660`) / ctx (`+0x34a8200`)
  class object is reachable during play.  Every reachable object is in the
  `0x20dab… / 0x2092… / 0x209fab… / 0x20d2fd…` heap clusters; the exit-time
  sequence tree lived in `0x20bf79…` — **a cluster that appears nowhere during
  play**.  Strong (not yet conclusive) signal that the exit parent/grandparent
  are **transient exit-machinery**, not the persistent course-play sequence.
- **Lead — `g1` (`DAT_3623670`) is the live sequence-controller root.** `g1+0x20`
  yields a cluster of objects with vtables `0x710349d300 / dfe0 / e700 / e4c0 /
  e4e0` + `0x71034f3ac0`, sitting right next to the al sequence/scene-factory
  dispatch table (`0x710349cf38/cf58`).  The persistent course-play sequence is
  almost certainly down this `g1` tree (deeper than the 2-hop window).
- RTTI is stripped (typeinfo @ vt−8 == 0 for every class vtable), and the names
  `"ExitCourseMgr"/"CheckNextGoToWorldMap"/"RequestStageStartEvent"` are each
  referenced ONLY by their nerve name-getter (no create-by-name call site) — so
  class/requester identification must come from decompiling, not RTTI/strings.

**capture #2 (DEPLOYED, still 18 hooks / SAFE):** `seqPersistDiag` now also runs
**(D) a DEV-ONLY transience check** — QueryMemory-reads the *known* exit-tree
addresses during play (`ctx 0x20bf799330`, `parent 0x20bf798dc0`, `grandparent
0x20bf796368`; stable across dev sessions) and logs each vtable.  The dead-end
test already showed `ctx` reads vt=0 mid-play; (D) adds the PARENT and
GRANDPARENT (separate objects) to settle *whole-tree-transient* vs
*container-persists*.  Firing changed to **once per course-entry** (re-arms on
the world map, cap 6) so the A→exit→B protocol exercises probe (A).

**Procedure (capture #2):** boot → confirm banner → enter course, **play ~10 s**
(fire #1: D reads the exit-tree slots) → **pause → "Return to World Map"** → enter
another course, play ~10 s (fire #2: A shows the latched parent + D again).  Grab
all `seqPersistDiag` lines (esp. `(D) … vt(ghidra)=…` and `(A) latched parent=…`).

**Decision (capture #2):**
- (D) parent/grandparent read **vt=0 / unmapped / a non-matching vtable** AND (A)
  doesn't flag PARENT → the exit tree is transient ⇒ stop chasing it; RE the
  **`g1` play-sequence** (identify the `0x710349d…` controller class, find how it
  requests course-out — i.e. who calls the al state-factory with key
  `0x99137dfe`) and drive that.
- (D) parent **or** grandparent reads its matching vtable (or (A) flags PARENT) ⇒
  the host/container persists at a known dev address ⇒ add a
  `probe::courseSeqHost()` returning it and drive it into the exit state.

### ✅ Capture #2 RESULT — SETTLED: the entire exit tree is TRANSIENT

Capture #2 ran (`B_main=0x8504000`).  Probe **(D)** during play vs. at the exit
moment, same three addresses:

| address | DURING PLAY (fire #1) | at the EXIT (pause→Return) |
|---|---|---|
| `ctx@0x20bf799330`         | `vt=0x0`         | `0x71034a8200` (ctx class) |
| `parent@0x20bf798dc0`      | `vt=0xc` (freed) | `0x7103517068` (parent class) |
| `grandparent@0x20bf796368` | `vt=0x0`         | `0x71033f9660` (grandparent class) |

The slots are **mapped-but-empty during play** and only populated at course-out.
Probe (C) again: **0 target hits / 48 vtables** (no ctx/parent/grandparent-class
object reachable during play).  **CONCLUSION: the entire exit sequence tree —
`ctx` + `parent` + `grandparent` — is TRANSIENT**, allocated into a fixed arena
on course-out and torn down after.  The "drive the persistent parent" hypothesis
is **disproven**; there is no persistent object up the `+0x08` chain to latch or
drive.  (The exit addresses are byte-identical to every prior capture, so the
arena placement is deterministic in dev — but empty during play.)

### Spawn-mechanism RE recap (session 5, what builds the exit tree)

On course-out the persistent layer builds, in order:
1. **grandparent** (al sequence container, vtable `0x71033f9660`, 0x208 B) via the
   al **SceneFactory** — vtable `0x710349cf38` (ctor `FUN_7100942e70`, an 8-byte
   stateless factory), create-slot **`+0x20` == `FUN_7100229fa4`** (type-6).
2. **parent** sub-sequence (`0x7103517068`) via `FUN_7100062a0c`, stored into
   `grandparent+0x20` array (count `grandparent+0x18`).
3. **ctx** ExitCourseMgr controller (`0x71034a8200`, 0x28 B) via `FUN_7101be3934`,
   registered in the al **state-factory** under key **`0x99137dfe`**
   (`FUN_710094b730` binds key→ctor).
4. ctx is ticked → `ExitCourseMgr` body (teardown `FUN_7101a612cc` + step-advance)
   → `CheckNextGoToWorldMap` → world-map load.

Negative results this session (don't re-walk): `SetResultFailureIfExitStage`
nerve execute = `FUN_7101bf5f88` is an **empty body** (no-op marker); the name
strings have no create-by-name call site; RTTI is stripped; the SceneFactory
ctor `FUN_7100942e70`'s caller chain climbs into the al framework init (the
"many hops to a global" path — not cheaply traced statically).

### NEXT SESSION — prioritized plan (drive the persistent SceneController)

The lever must act on a PERSISTENT object that exists during play.  Best leads,
in order:

1. **Find the persistent owner of the exit scene tree at exit time (cheapest
   next probe).** The transient grandparent must be linked into a PERSISTENT
   SceneController's scene-stack/array.  Add to `exitCourseMgrBodyHook` (already
   fires at the exit, QueryMemory-safe context) a dump of the **grandparent's**
   fields (`grandparent = QueryMemory-safe *(parent+0x08)`, 0x208 B) flagging
   each pointer field whose target's vtable is a main-module vtable (Ghidra-
   mapped).  One of those points UP to the persistent controller (likely a
   `g1`/`0x710349d…`-class object reachable during play).  Match it against the
   capture #1 census (`g1+0x20` cluster) to confirm it's persistent.
2. **Identify the persistent SceneController + its "change/push scene" method.**
   `g1 = DAT_3623670`; `g1+0x20` holds controller objects (vtables
   `0x710349d300…`).  Decompile that class to find the method that invokes the
   SceneFactory create-slot (`0x710349cf38 +0x20`) to push the exit scene.  Then
   drive it during play (a real persistent object + a real method = the clean
   trigger, all engine guards intact).  Gate as always on `isSaveLoaded()` /
   not-in-transition; smoke-test OFF by default.
3. **Behavioral capture (fallback / cross-check).** A focused observability build
   logging EVERY distinct nerve vtable + its host object that fires during a
   manual pause→"Return to World Map" window pinpoints the persistent
   pause/stage controller and the exact nerve/method that initiates the exit
   (mirrors how earlier milestones were cracked).  The existing `NERVE_NEW_VT`
   logging already records distinct nerve vtables — re-check a pause→exit log for
   the new vtables that appear at that instant.

### Code / build state after session 5

- **Build is SAFE/PLAYABLE and INERT again.**  `kSeqPersistDiag` reverted to
  `false` (diag dead-code-eliminated — `subsdk9` back to 92052 B); banner now
  `gate-entry session-5 SETTLED: exit tree … TRANSIENT … 18 hooks`.  Deployed to
  `%APPDATA%\…\exefs\` from THIS worktree, hash-matched.  Still **18 hooks**; all
  test constants OFF (`kBounceSmokeTest`, `kCourseOutSmokeTest`,
  `kBlockAllCourseEntry`, `kForceCourseInForbidden=false`, `kTestGatedIdFb=-1`).
- Kept for reference (gated off): `seqPersistDiag()` + probes A/B/C/D in
  `main.cpp`; `probe::latchCourseSeqParent()/courseSeqParent()` +
  `latchCourseSeqController()/bounceCourseOut()` in `Gates.{hpp,cpp}`.  The parent
  latch still runs harmlessly on every natural exit.

---

## Session 6 update (2026-05-31) — grandparent OWNER finder DEPLOYED

Picks up "NEXT SESSION — prioritized plan" #1 (drive the persistent al
SceneController that BUILDS the exit scene; cheapest first probe = find the
persistent owner of the exit tree at exit time).  Did the supporting static RE,
then built + deployed a SAFE log-only **owner finder**.  Build stays
SAFE/PLAYABLE except the one read-only probe is ON for this capture.

### Static RE this session (Ghidra MCP, all bounded — no broad scans)

- **`FUN_7100229fa4` (the SceneFactory create-slot that builds the grandparent)
  — decompiled.**  It is a generic al allocator/ctor: for type selector
  `param2[0x509]==6` it allocs **0x208 B** via the sead HeapMgr, writes vtable
  `&PTR_FUN_71033f9660`, and initializes **self-referential intrusive-list
  sentinels** at qword indices 0x39/0x3a (`= self+0x39`), 0x3c/0x3d
  (`= self+0x3c`), plus list-size words 0x3b=`0x1000000000`,
  0x3e=`0x800000000`, 0x3f=0x40=0.  Sibling type-5 → vtable `0x71033f9610`,
  0x200 B.  **CRITICAL: the factory writes NO owner/host back-pointer** — so the
  grandparent→owner link is established by the *caller*, and `grandparent+0x08`
  (which faulted in capture #2c) is NOT a clean host pointer (it is one of these
  list sentinels / uninitialized at that instant).  The owner must be found by
  dumping the grandparent's other fields, or by the controller holding it.
- **`FUN_7100229fa4`'s only xref is the DATA ref at `0x710349cf58`** (the
  SceneFactory vtable's create-slot `+0x20`).  It is invoked purely by indirect
  dispatch → no static caller.  Likewise the **SceneFactory vtable
  `0x710349cf38` has exactly ONE xref: its own ctor `FUN_7100942e70`** — the
  factory is constructed once and stored into an object by the ctor's caller,
  which climbs into al-framework init (the expensive path the handoff already
  flagged).  ⇒ the static route to the owner is not cheap; the runtime probe is.
- **`CheckNextGoToWorldMap` does NOT expose a callable scene-load.**
  `FUN_710163e944(ctx)` = `FUN_710163f144(ctx+0x98, ctx+0x20)` +
  `FUN_710163e990(ctx)` + the step-advance.  `FUN_710163f144(p1,p2)` decompiles
  to a **generic tagged-pointer setter** (writes `p2` into `*(*p1 & ~3)` behind a
  tag-bit guard + a TLS error stamp) — it stages a value for the al state
  machine, it is NOT "load the world map".  So there is no standalone load fn to
  call; the scene change is intrinsically driven by the al sequence machine —
  confirming the only clean lever is **driving the persistent controller**.
- **State-factory registry confirmed** (registrar `FUN_710094b730`): builds a
  node `{key=0x99137dfe, createfn=FUN_7101be3934, 0, 0}` and prepends it to the
  global init chain `DAT_71036358a0` (position-independent, slide
  `DAT_71036358b4`).  All xrefs to `DAT_71036358a0` are *other* such registrars
  (one per al state) — the generic runtime create-by-key lookup is al framework.
  `FUN_7101be3934` (ctx ctor) re-confirmed: alloc 0x28, vtable
  `&PTR_FUN_71034a8200`, byte `+0x1e=0xff`, rest zero.

### The probe — DEPLOYED NOW (the human step)

`switch-mod/src/main.cpp` gained **`grandparentOwnerProbe(base, ctx)`**
(log-only, one-shot, every read `svc::QueryMemory`-gated), fired once at the
natural course-out from inside `exitCourseMgrBodyHook` (where `ctx` is the live
ExitCourseMgr-body arg and `courseManager()` is valid — the proven-safe
context).  Gate constant `kGrandparentOwnerProbe=true` (read-only ⇒ safe to ship
ON; **revert to `false` after the capture** → the legacy `anchorSearch` runs in
its place, restoring the prior INERT behavior).  Still **18 hooks**; bounce/gate
probes remain INERT.  `subsdk9` grew 92052 → **92835 B** (probe compiled in).

It derives `parent = *(ctx+0x08)` and `grandparent = *(parent+0x08)` (the same
two steps capture #2c made successfully; it never reads `grandparent+0x08`,
which faulted), then runs three independent owner-finders at the SAME instant:
- **(1) g1 SceneController census** — `g1 = DAT_3623670`; logs each object
  reachable in `g1+0x00..0x200` and one hop deeper (`g1ctrl[i] obj=… vt(g)=… via
  g1[+k]`), so any owner link can be matched to a play-time-reachable persistent
  controller (the cluster the session-5 census found at vtables `0x710349d…`).
- **(2) grandparent dump** — every ptr field in `grandparent+0x00..0x208`:
  `GP +0xNNN: OBJ 0x… vt(g)=0x…` for fields whose target is a live main-module
  object (a back-pointer to an owner base — the handoff's primary signal),
  `[OWNER back-link]` for fields landing inside a censused controller (intrusive
  link to owner+nodeOffset), `==ctx/==parent` tags for intra-tree fields, raw
  for the rest.
- **(3) forward search** — does any censused controller hold `gp`/`parent`/`ctx`
  in its first 0x800 bytes (`FWD: g1ctrl[i](…)+0xNN == grandparent`)?

**Procedure:** boot SMBW → confirm banner `gate-entry session-6 grandparent
OWNER finder ACTIVE … 18 hooks` and `install ExitCourseMgrBody @ +0x1be3a5c OK`
→ enter ANY course → **pause → "Return to World Map"**.  Grab every `[smbwap`
line from `gpOwnerProbe: B_main=…` through `gpOwnerProbe done: …`, especially the
`g1ctrl[…]`, `GP +0x…`, and `FWD:` lines (strip NULs:
``Get-Content … | %{ $_ -replace "`0","" }``).

**Decision tree (next session):**
- **A `GP +0x…: OBJ … vt(g)=…` or `[OWNER back-link]` hit, or a `FWD:` hit**
  → that object is the persistent owner.  Map its `vt(g)` to Ghidra, confirm it
  appears in the (1) census (so it is reachable during play), then **decompile
  that class** and find the method that invokes the SceneFactory create-slot
  (`0x710349cf38 +0x20`) / changes to the type-6 exit scene / requests state-key
  `0x99137dfe`.  Add a `probe::courseSceneController()` walker (g1 → … → owner,
  path read from the (1) census offsets) and drive that method during play,
  gated on `isSaveLoaded()` / not-in-transition; smoke-test OFF by default.
- **Object fields exist but none match a censused controller** → widen the (1)
  census (more g1 depth) or the membership span (currently 0x2000); the owner is
  a g1 object deeper than the 2-hop census.  The raw `GP +0x…` addresses are
  logged regardless, so match them against the `g1ctrl[…]` addresses offline.
- **0 obj/owner/fwd hits** → the grandparent has no owner back-pointer and the
  owner is not in the censused g1 cluster.  Fall back to plan #3 (behavioral):
  a focused build logging every distinct nerve vtable + host object during a
  manual pause→"Return to World Map" window (re-check `NERVE_NEW_VT` in
  `nerveActivateOnceHook` for the new vtables at that instant) to pinpoint the
  persistent pause/stage controller directly.

### ✅ Capture #1 RESULT (2026-05-31) — owner has NO backlink; persistent controller PINNED

gpOwnerProbe ran clean at a pause→Return-to-World-Map.  `B_main=0x8504000`,
`ctx=0x20bf799330 (vt 0x71034a8200) parent=0x20bf798dc0 (vt 0x7103517068)
grandparent=0x20bf796368 (vt 0x71033f9660)` — all as before (deterministic dev
arena; `GP+0x1c8 = gp+0x1c8` confirms the factory's self-sentinels).

**`gpOwnerProbe done: 2 obj field(s), 0 owner hit(s), 0 fwd hit(s), 11 g1 ctrl(s)`.**
- The grandparent's only two object fields point back INTO the transient exit
  arena, NOT up to a persistent controller: `GP+0x30 → 0x20bf796740 (vt
  0x7103517088)` (a sibling sub-sequence) and `GP+0x1f8 → 0x20bf683030 (vt
  0x71034c2e68)`.  All other ptr fields are the factory list sentinels
  (`0x20bf796530/548`, in the arena).  ⇒ **the grandparent carries no
  back-pointer to its persistent owner**, and the shallow (2-hop, 0x800-deep) g1
  scan does not hold it.  (Owner link may be deeper / in a sead container / a
  tagged pointer — see capture #2.)
- **★ Major unlock — the live PERSISTENT course-sequence controller is now
  pinned.**  `g1 = DAT_3623670 = 0x20d2fd2db0`, primary vtable Ghidra
  **`0x710349d300`** (runtime `0x0bf6d300`).  Static follow-up: its ctor is
  `FUN_7101baeb20`, dtor `FUN_7101baf454`, code cluster **`0x7101bae…`** — right
  next to the course / ExitCourseMgr management code (`0x7101bd…/0x7101be…`).  It
  is a multiply-inherited `al::NerveExecutor` with EMBEDDED subobjects at `+0x08`
  (vt `0x71034f3ac0`), `+0xf0` (vt `0x710349d4d0`), `+0x118` (vt `0x710349d4f0`),
  all within `g1+0x00..0x140`.  The live g1-controller-cluster census (dev):

  | g1ctrl | live addr | vt (Ghidra) | via |
  |---|---|---|---|
  | g1 (root) | 0x20d2fd2db0 | 0x710349d300 | DAT_3623670 |
  | 0 | 0x20d2fd2db8 (=g1+0x8) | 0x71034f3ac0 | *(g1+0x20) |
  | 2 | 0x20d6292348 | 0x710349dfe0 | g1.sub+0x50 |
  | 3 | 0x20d6293ef0 | 0x710349e700 | g1.sub+0x58 |
  | 4 | 0x20d63a8200 | 0x710349e4c0 | g1.sub+0x60 |
  | 5 | 0x20d63ca170 | 0x710349e4e0 | g1.sub+0x68 |
  | 6 | 0x20d2fd2ea0 | 0x710349d4d0 | g1.sub+0x100 |
  | 7–10 | 0x20d2fd2cb0/f10/3010, 0x2092424920 | 0x71034f64e0 | sead containers |

- Negatives this session: `CheckNextGoToWorldMap`'s `FUN_710163f144` is a generic
  tagged-ptr setter (not a scene-loader) → no callable load shortcut.  Bounded
  movk-`#0x9913` scan of `[0x7101b00000,0x7101c00000)` = **0 hits** → the
  `0x99137dfe` state-key requester is NOT in the course-management range either
  (still unfound; ruled-out ranges now: ctx/checkNext/courseOutDemo/teardown +
  0x7101b…–0x7101c…).

### capture #2 — g1 controller state DIFF

`dumpG1CourseSeqCtrl(phase, base)` dumped `g1+0x00..0x160` (covers all embedded
subobjects) at PLAY (~200 in-course ticks, `playerTickLatchHook`) and at EXIT
(`exitCourseMgrBodyHook`), to find a field that flips on course-out.

**✅ RESULT (2026-05-31): the two dumps are BYTE-IDENTICAL.**  Nothing in
`g1+0x00..0x160` changes between play and the ExitCourseMgr-body moment ⇒ the g1
ROOT controller is **not** the direct holder/driver of the transient exit tree —
it stores no exit-scene pointer and flips no nerve word in that range at that
time.  (Note the EXIT dump fires at the ExitCourseMgr *body*, a few ticks BEFORE
the final world-map scene swap in `CheckNextGoToWorldMap`, so the swap may simply
not have happened yet — but the exit sequence is already running, ticked by
*something* persistent that g1's first 0x160 B does not reference.)  Notable g1
fields (stable): `+0x128 = 0x0a0b3284 → Ghidra 0x7101baf284` (a code/nerve ptr in
g1's own cluster); the 5 sibling controllers at `+0x58/+0x60/+0x68/+0x70/+0x108`.

### capture #3 — WIDE holder reachability search

Since neither the grandparent (no backlink) nor the g1 root (unchanged) reveals
the holder, capture #3 hunted *whoever ticks the exit tree* by brute
reachability: **`holderSearchWide(base, ctx)`** (log-only, QueryMemory-safe, ≤512
objs / ≤300k reads) BFS-walks the persistent object graph from scene
(`DAT_3625850`) + `scene+0x40` (the teardown's `*(scene+0x40)+0x2c40` suspect,
0x4000 deep) + areaMgr (`DAT_3628398`) + g1 (`DAT_3623670`), reporting any field
pointing to/INTO the exit tree (`==ctx/parent/gp` or `->gp+`/`->parent+`/`->ctx+`,
tagged via `&~3`).

**✅ RESULT (2026-05-31): `0 hit(s), 512 obj(s), 133376 read(s)`, queue drained.**
The BFS exhausted the persistent object graph reachable from those four roots
(512 distinct objects) and found NOTHING pointing to or into the exit tree.  ⇒
the transient exit sequence is **not referenced by any plain object-pointer chain
from scene / g1 / areaMgr** within reach.  It is ticked via an indirect path the
scan can't cheaply follow: most likely an al **update-list iterator** (the
sequence is linked into a per-frame update list via an EMBEDDED ListNode, whose
head lives in an object not reachable from the seeds, or the list element pointer
targets a tree sub-allocation outside the matched node ranges — e.g. the
grandparent's children array `gp+0x20 = 0x20bf796590`, a non-object the BFS
doesn't enqueue), or held off a root not seeded.

**This closes the "find the holder by scanning" approach** (3 hypotheses
eliminated this session: grandparent backlink, g1 root state-diff, broad
reachability).  Reverted to INERT/SAFE.

### NEXT SESSION — the PauseResult data lever (recommended) + alternatives

The scan approaches are exhausted; switch to RE of the *cause*, not the
machinery.  All three sidestep "who holds the transient tree":

1. **PauseResult data lever (recommended — cleanest, no transient tree
   involved).**  The pause menu's "Return to World Map" sets a `PauseResult`
   enum value that the persistent course sequence reads each frame and acts on
   (RE'd as an enum in session 5: reflection registrar `FUN_7100581d38`,
   to-string `FUN_7101c0bd9c`).  Plan: (a) decompile `FUN_7101c0bd9c` to
   enumerate the enum values (find the "ReturnToWorldMap"/"ExitStage" ordinal);
   (b) find the CONSUMER — who reads the PauseResult and branches to course-out
   (xref the enum's storage; the consumer is the persistent pause/course
   sequence's calc); (c) find the persistent FIELD the result is stored in; (d)
   set that field during play (gated, from PlayerTickLatch) and let the engine
   initiate the exit through its own guards.  This is a DATA write to a
   persistent object — the kind of lever the whole bounce needs.
2. **areaMgr course-out-demo gate (quick check).**  `IsExecuteNormalCourse
   OutDemoForWorldMapPlayer` nerve (`FUN_71016d1118`, vt `0x71033c4478`) gates on
   `DAT_3628398+0x58 == 1` (areaMgr is persistent + IS reachable).  Decompile it
   + find the writer of `areaMgr+0x58`; if that flag (or a sibling areaMgr field)
   is what the pause "Return to World Map" sets to request course-out, it is a
   one-write lever.  (Caveat: this is the WORLD-MAP-player side / "demo" — may be
   downstream of the actual exit, not the trigger.  Cheap to rule in/out.)
3. **Behavioral nerve capture (fallback).**  Log every distinct nerve vtable +
   host during a manual pause→"Return to World Map" (the existing `NERVE_NEW_VT`
   in `nerveActivateOnceHook`).  Caveat: session-3 capture #1 saw only 2 active
   nerves route through `FUN_7100559f7c` on this path (the exit nerves are
   one-shot dispatch, not active) — limited signal, hence lowest priority.

⚠️ **Reality check:** the bounce has now consumed 6 sessions and the exit
machinery is proving to be deeply-isolated al framework.  The bridge-side
Royal-Seed auto-resolve already fixes the actual user-facing bug, so the general
Switch-side gate remains **not urgent** — weigh further investment accordingly.

### ✅ Code REMOVED / shelved (2026-05-31) — this is now a DOCS-ONLY PR

Per the shelve decision, **all gate-entry CODE was backed out** of
`switch-mod/src/{main.cpp, probe/Gates.{cpp,hpp}}` — those three files were
reverted to `origin/master` (verified byte-identical to master), so the PR
carries **only this documentation** (`docs/gate-entry-session3-handoff.md` +
`docs/royal-seed-phase-a-findings.md`) and **zero code change**.

- **Removed:** the two gate-entry hooks (`exitCourseMgrBodyHook` on
  `FUN_7101be3a5c`, `checkCourseInUiKeyGateHook` on `FUN_710022a964`) and the
  three other Phase-A observability hooks added on this branch, plus every
  gated-off probe (`grandparentOwnerProbe`, `dumpG1CourseSeqCtrl`,
  `holderSearchWide`, `seqPersistDiag`, `anchorSearch`, the latch/bounce
  primitives in `Gates.{cpp,hpp}`, and all `k*` test constants).
- **Result:** the subsdk is back to the **13-hook `master` baseline** (banner
  `Phase 2g: 13 hooks`).  Rebuilt + redeployed to
  `%APPDATA%\…\010015100b514000\smbwap\exefs\`, **hash-matched** (`subsdk9`
  87230 B, `main.npdm` 1608 B).  Verified: the binary contains none of
  `EXIT_COURSE_MGR_BODY` / `PhaseB coursein_gate` / `session-6` / `gpOwnerProbe`.
- **All the RE primitives + probe implementations are preserved here in the
  docs and in this branch's git history** (commits `c3c26e1` + the session-5/6
  commit, before the revert) — re-derive or cherry-pick from there if the work
  is ever resumed via the PauseResult data lever above.
