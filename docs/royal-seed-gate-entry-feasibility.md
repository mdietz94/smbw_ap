# Gate-entry approach feasibility — can we lock palace entry from AP?

Follow-up to [royal-seed-check-loss-re-findings.md](royal-seed-check-loss-re-findings.md).
The original RE confirmed that the seed bool unifies "palace cleared" and
"player has Royal Seed" — there's no clean way to grant the seed without
also marking the palace cleared. This doc asks the inverse question:
**can we lock palace entry (or arbitrary course entry) via an
AP-controlled mechanism**, then let the player play naturally to fire
the PlayReport?

**Short answer: yes, the architecture supports this — Mario Wonder
already has a per-course "needs item to enter" mechanism, and the
world-map gate function is a single hookable choke point. Building it
out is non-trivial work (new wire message, Switch-side bitfield, new
hook, course-identity decoding) but it's clean and matches the existing
badge-sync precedent.**

## Static evidence supporting feasibility

### 1. The game already has per-course entry conditions

Strings found at:

- `NeedBadgeIdEnterCourse` (0x71028dc9da)
- `GetNeedBadgeIdEnterCourseForDirectCourseIn` (0x71028a310f)

The second is a Nerve event name. Its vtable lives at `0x71034b9300`
with slot 8 (execute) at `FUN_7101bffe94`. The Nerve returns the badge
ID needed to enter a given course (or -1 if none). This is the
machinery behind "this badge challenge course needs Spring Feet" gating
in vanilla. **It proves the engine already supports the abstraction
"entry to course X requires owning Y".**

### 2. World-map node lock state has a Nerve

- `WorldMapDemoCoursePointLockSelector` at 0x71028c1cf5
- Vtable at 0x71033f8ed0, execute (slot 8) at `FUN_71008840f0`
- Execute reads `[this+0x32]` as a sentinel byte — if `0xFF`, no lock;
  otherwise dispatches to lock-display logic
- `[this+0x19]` bit 5 controls additional lock behavior

This Nerve drives the visual "locked" state on world-map nodes
(presumably the grey/chained appearance over a node the player can't
yet enter).

### 3. The gate-check function is a single shared choke point

`FUN_7100383418` is called by two functions, both of which are Nerve
slot-8 execute wrappers with identical shape:

- `FUN_71003833dc` (right before the gate function in memory)
- `FUN_710085134c` (separate Nerve, same wrapper pattern)

Each wrapper:
1. Calls `FUN_7100383418(this)`
2. Tests bit 0 of the return value
3. Sets Nerve state bits 30/31 in `[this+0x18]` according to the result

`FUN_7100383418`'s body:

- Walks the Nerve `this`: `[this+8] → [+8] → [+0x1f8]` to reach a deep
  context object (likely the course-point-info manager)
- Reads bool with hash `0x46d90c82` (unknown — likely a per-node
  "interactive" flag, NOT a seed)
- Reads a counter via `FUN_710012ae94`
- Calls `FUN_71016cbf58` (another bool reader)
- Logs `WorldMap_OpenCoursePointGate_Failure` on failure paths
- Returns 1 = gate open, 0 = gate closed

**Hooking `FUN_7100383418` with an early-out is feasible.** The hook
could be: "if AP-locked-courses bitfield says no, return 0 immediately;
else fall through to the real gate logic."

### 4. Existing precedent: the badge bitfield sync

The current badge sync (M4 follow-up #2) demonstrates the exact
architectural pattern needed:

- Bridge holds a canonical bitfield derived from AP `items_received`
- New wire message: `SetBadgesAbsoluteMsg { bits: u64 }`
- Pushed to Switch on ReceivedItems, HelloMsg, and a ~2 s periodic tick
- Switch-side primitive: `probe::setBadgeBitfieldAbsolute(bits)`
  overwrites the live bitfield (idempotent, no merge)
- Same input always produces the same final state

We'd build the AP-locked-courses sync the same way:

- Bridge derives "AP-locked courses" from AP item state
- Wire message: `SetApLockedCoursesAbsoluteMsg { bits: u128 }` (Mario
  Wonder has ~85 courses, plus palaces — comfortably fits in 128 bits)
- Pushed on ReceivedItems / HelloMsg / periodic tick
- Switch-side: `probe::setApLockedCoursesAbsolute(bits)` writes a
  global bitfield
- Hook on `FUN_7100383418` consults the bitfield

## What this would unlock

The user's specific Royal Seed problem becomes a special case:

- AP holds the player's Royal Seed (doesn't grant the bool to the
  Switch yet)
- AP-locked-courses bitfield says "palace X is enterable" (because the
  player needs to play it to clear the AP location)
- Player plays palace naturally, clears it
- Natural PALACE_CLEAR PlayReport fires → AP location satisfied
- The natural in-game seed-grant code (one of the 18 dynamic-hash
  writer callers — possibly via the `RequestEventGetGrandSeed` Nerve at
  vtable `0x7103345920`) sets the seed bool as a side effect of clearing
- AP records the player's Royal Seed
- (Optional) AP could then issue the player a "Royal Seed" item from
  someone else's world, which now safely flips an already-set bool

Beyond the seed problem, this enables real progression-locked
randomizer rules:

- "Player needs item X to play course Y"
- "Player needs N Wonder Seeds to unlock world Z"
- Out-of-order course unlocking (currently the game enforces strict
  vanilla progression)

## What it would cost — scope estimate

This is **not** a small PR. Rough breakdown:

1. **Deeper RE of `FUN_7100383418` (~ a day)**: identify how it locates
   the current course-point identity from the Nerve `this` struct.
   Need to know what hash, ID, or pointer represents "this is W1 palace
   node". The `[this+8] → [+8] → [+0x1f8]` walk needs to be decoded
   end-to-end; the actual course key likely lives at some offset off
   `x24` (the value at `+0x1f8`).
2. **Course → AP index mapping (~half a day)**: build a static table
   mapping each Mario Wonder internal course key to an AP item slot.
   We already have parts of this in `apworld/smbw_archipelago/client/
   location_table.py` and `royal_seed_table.py`; needs unification with
   whatever key the gate sees.
3. **Switch-side primitive + bitfield (~a day)**: write
   `probe::setApLockedCoursesAbsolute(bits)`, a global bitfield in
   gmd-adjacent state (or just module-static — doesn't need to
   persist), and the hook on `FUN_7100383418` that consults it. Mirror
   the badge-sync pattern.
4. **Wire protocol + bridge logic (~a day)**:
   `SetApLockedCoursesAbsoluteMsg` plumbing in `wire.py` /
   `ApProtocol.hpp`, derivation logic in `SMBWContext`, periodic
   push, replay on HelloMsg.
5. **Dynamic verification (~half a day)**: in Ryujinx, confirm the
   hook fires for palace entry attempts, that locking a course blocks
   entry visibly, that unlocking a course restores entry, and that no
   regressions occur on non-AP-managed courses.
6. **Edge cases**: cutscene routing, demo skips, palette UI state,
   inter-world transitions when a target course is locked, behavior of
   the "Direct Course In" variant (different Nerve, may need its own
   hook).

**Total: ~4–5 days of focused work.** Comparable to the M4 badge
sync. Lower-bound assumes the course identity decoding is
straightforward; could grow if the `+0x1f8` context turns out to be
shared across nodes and requires further disambiguation.

## What's NOT needed for the simpler Royal Seed unblock

For the immediate Royal Seed check-loss bug only, the bridge-side
auto-resolve (current PR) is still the right ship-now answer. The
gate-entry approach is **strictly larger** scope, but offers value
beyond just the seed fix.

## Recommendation

**Two-phase plan:**

- **Now**: ship the bridge-side auto-resolve PR. Unblocks gameplay.
- **Later (when randomizer progression rules matter)**: build the
  gate-entry sync as a milestone. Use it to both replace the seed
  auto-resolve workaround AND enable progression-locked rando rules.
  The work investment unlocks substantially more design space than the
  seed fix alone.

The user's instinct that "preventing entry until you have the seed" is
the cleaner mental model is correct — it just doesn't fit in the
current PR's scope.

## Loose ends to resolve before building the gate-entry sync

1. **Confirm hash `0x46d90c82`'s role** in `FUN_7100383418` — if it's a
   per-node interactivity flag we can already influence via the
   container-B writer, we might not need a new bitfield at all; we
   could just set/clear this flag per-course from the bridge. Saves a
   wire message but couples us to whatever this flag's other consumers
   are.
2. **Confirm `FUN_710085134c` and `FUN_71003833dc` are the only two
   gate Nerves** — if there's a "Direct Course In" variant (the string
   `GetNeedBadgeIdEnterCourseForDirectCourseIn` hints at this) on a
   separate path, we need to hook it too.
3. **Locate the course-key inside the Nerve `this`** — walk the
   `[+8] → [+8] → [+0x1f8]` chain to a named field. A focused decompile
   pass would answer this; the current bridge timeouts on
   `decompile_function` for the gate function make this slow but
   feasible via piecewise `read_memory` + manual decode if scripts
   stay disabled.
4. **Verify the bitfield doesn't conflict with badge-challenge gating**
   — Mario Wonder's badge challenges already lock courses; our AP lock
   needs to compose with that, not collide.
