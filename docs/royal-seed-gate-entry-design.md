# AP-controlled course-entry gating — detailed design

Follow-up to:
- [royal-seed-check-loss-re-findings.md](royal-seed-check-loss-re-findings.md)
  (why the seed-set-from-AP path can't be rescued by data tweaks alone)
- [royal-seed-gate-entry-feasibility.md](royal-seed-gate-entry-feasibility.md)
  (initial feasibility — yes, hookable)

This doc nails down the concrete hook target, signature, course-identity
extraction, and rollout plan, after a deeper RE pass.

**Headline change from the feasibility doc**: the gate function
`FUN_7100383418` never returns "locked" via its return value — it
always returns 1 in normal operation. The lock decision lives in a
**side-effect byte** at `*((this+0x20) & ~3)`. That changes the hook
design (intercept the side effect, not the return) but it's actually
cleaner: we don't need to fight the Nerve state machine.

## The mechanism — `IsDisplayFlowerLockUI`

The gate is owned by a single object whose vtable lives at
`0x71033c1ad8`. Its slot-0 name getter returns the string
`IsDisplayFlowerLockUI` at `0x7102936065`.

"Flower lock" in Mario Wonder is the vanilla world-map seed-bar gate —
the "you need N Wonder Seeds to unlock this path" lock that already
exists in the game. Hooking it composes with the vanilla mechanism
rather than fighting it.

The vtable's relevant slots:

| Slot | Address | Role |
|---|---|---|
| -1 (typeinfo) | `0x71033c1ad8` | `0x71000ac930` — shared Nerve typeinfo |
| 0 | `0x71033c1ae0` | `0x71016cbf38` — name getter → "IsDisplayFlowerLockUI" |
| 1 | `0x71033c1ae8` | `0x71016cbf44` — name getter → "AI" |
| 7 | `0x71033c1b18` | `0x710085131c` — init/setup (writes the lock byte pointer to `[this+0x20]`) |
| **8** | `0x71033c1b20` | **`0x710085134c`** — primary execute (default-success wrapper) |
| **17** | `0x71033c1b68` | **`0x71003833dc`** — secondary execute (default-fail wrapper) |

Both execute slots call the same body, `FUN_7100383418`. They differ
only in how they translate its return value into the Nerve's state
bits at `[this+0x18]`:

```c
// Slot 8 (default-success):
uVar1 = state | 0x80000000;          // bit 31 set
if ((return_val & 1) == 0) uVar1 = state | 0xc0000000;  // bits 30+31 if return = 0

// Slot 17 (default-fail):
uVar1 = state | 0xc0000000;          // bits 30+31 set
if ((return_val & 1) != 0) uVar1 = state | 0x80000000;  // bit 31 only if return = 1
```

Two execute slots with inverted defaults is the state-machine for the
lock animation (one slot handles "currently locked → transition to
open", the other "currently open → transition to lock"). Both
ultimately reduce to the same gate-body decision.

## The gate body — `FUN_7100383418`

Full decompile in
[royal-seed-gate-entry-feasibility.md](royal-seed-gate-entry-feasibility.md)
addendum. Key observations from the decompile:

1. **Returns 0 only on null-pointer fast-fail** — five different
   global pointers are checked at function entry. In normal operation
   none are null. So the return value is effectively always 1.
2. **Reads `this+0x20` as a packed pointer**: low 2 bits are flags;
   `*((this+0x20) & ~3)` is the writeable **lock byte** that the
   function clears to 0 at entry, then conditionally sets to 1.
3. **Walks the inner context**:
   `this+8 → [+8] → [+0x1f8] → [+0x350] → [+0x108]` is `uVar19`, the
   8-byte **course key**.
4. **Binary-searches a sorted course descriptor table** at some
   `mgr+0x40` (where `mgr` is the inner course-mgr) using `uVar19` as
   key. After match, reads byte at `descriptor+0x89` as `bVar5` (likely
   a per-course locked bit).
5. **Reads three additional game state queries**:
   - bool `0x46d90c82` via `FUN_71003838ac` → `local_54` (per-node
     interactivity flag?)
   - bool `0x925d4260` via virtual call on `(gmd+8)->vtable[+0x20]` →
     `local_58` (custom-container query, primary decision)
   - counter `0x90d4d0f2` via `FUN_710012ae94` → `local_5c`, then
     checked as `(local_5c | 2) != 2` (likely a Wonder Seed count
     compared to threshold)
6. **Sets the lock byte to 1 (open) when**:
   `local_58[0] == '\0' && !bVar4 && !(((bVar5|bVar17)^1) || local_54[0] != '\0')`
   else either calls `FUN_710032aea4(puVar16, 1)` (open via setter) or
   leaves byte as 0 (locked).

## Course identity — `uVar19`

The 64-bit key at `[mgr+0x108]` is the natural course identity. The
binary search compares it as an 8-byte equality — likely it's a
**pointer to a course-info struct** (stable address per course),
though it could also be a 64-bit composite key (e.g., `(world_no <<
32) | course_no`). Either way, an opaque comparison works.

For a hook:

- At entry, walk the pointer chain to `uVar19`
- Build a Switch-side lookup table at boot mapping
  `uVar19_value → ap_course_index` for every AP-managed course (~6
  palaces minimum for the seed problem; potentially up to 81 courses
  if we extend to course-level rando)
- Use the index to look up our AP-locked-courses bitfield

Bootstrapping that map needs **one of**:

- **Option A (preferred)** — walk the course descriptor table at boot
  time, read `descriptor+0` (the key) and `descriptor+?` (the course's
  name string), match the name against a known whitelist. The Murmur3
  course-name hash table at `+0x71003D4110` (per CLAUDE.md) gives us
  the index → name mapping; we already have the inverse from the AP
  world-data.
- **Option B** — at first frame after a course's first `Get...` Nerve
  fires, the key is published into `mgr+0x108`; observe and record.
  Cheaper but unreliable for courses the player hasn't visited yet.

Option A is the right move because we want to lock courses **before**
the player attempts to enter them.

## Alternative gate hook — `GetNeedBadgeIdEnterCourse[ForDirectCourseIn]` Nerve

Considered and rejected. The Nerve at vtable `0x71034b9300` (slot 8 =
`FUN_7101bffe94`) does:

```c
FUN_71005949e0(this + 0x20, 0xffffffff);  // write -1 = "no badge needed"
state = ... | 0x80000000;                  // mark complete
```

Slot 7 override (`FUN_7101bffe64`) binds a property named `"BadgeId"`
to `[this+0x20]` — the Nerve publishes the badge-id-needed value via
that named property, and other code reads it.

We could in principle hook this Nerve to write a phantom badge ID for
AP-locked courses. The vanilla badge-challenge UI would then display
"you need badge X" and block entry. But:

1. **No course identity at the Nerve level** — the Nerve fires for the
   "current target course" only, established by external context. We'd
   have to read the "current course" global (the `0xdf82e9ab` hash
   path) to know which course we're vetoing.
2. **UI lies** — the vanilla badge-needed UI would show an arbitrary
   badge icon. Confusing for players.
3. **The Nerve might not fire for palaces** — `NeedBadgeId` is for
   badge-challenge courses, which palaces aren't. Need to verify
   activation conditions.

`IsDisplayFlowerLockUI` is the better target.

## The hook design

### Switch-side

```cpp
// in switch-mod/src/probe/, alongside grantContainerBBool

namespace probe {

// One u128 (16 courses × 8 byte-aligned bits is plenty for 6 palaces;
// scale up to u256 or array<u128, 2> if we extend to all 81 courses).
// Updated absolutely by SetApLockedCoursesAbsoluteMsg — no merge.
inline std::atomic<uint64_t> sApLockedCoursesLo{0};
inline std::atomic<uint64_t> sApLockedCoursesHi{0};

// Maps the 8-byte course key from [mgr+0x108] to an AP-side index.
// Populated at boot by walking the course descriptor table.
// (See identifyCourseDescriptors below.)
inline std::unordered_map<uint64_t, uint8_t> sCourseKeyToApIndex;

void setApLockedCoursesAbsolute(uint64_t lo, uint64_t hi) {
    sApLockedCoursesLo.store(lo, std::memory_order_relaxed);
    sApLockedCoursesHi.store(hi, std::memory_order_relaxed);
}

bool isCourseLockedByAp(uint64_t courseKey) {
    auto it = sCourseKeyToApIndex.find(courseKey);
    if (it == sCourseKeyToApIndex.end()) return false;  // unknown courses pass through
    uint8_t idx = it->second;
    uint64_t bit = 1ULL << (idx & 63);
    uint64_t mask = idx < 64 ? sApLockedCoursesLo.load() : sApLockedCoursesHi.load();
    return (mask & bit) != 0;
}

}  // namespace probe

// Hook on FUN_7100383418
HOOK_DEFINE_TRAMPOLINE(FlowerLockGateBody) {
    static u64 Callback(void* self) {  // self = Nerve `this`
        // Walk pointer chain to course key
        long innerCtx = *(long*)((char*)self + 8);
        if (!innerCtx) return Orig(self);
        innerCtx = *(long*)((char*)innerCtx + 8);
        if (!innerCtx) return Orig(self);
        long coursePointInfo = *(long*)((char*)innerCtx + 0x1f8);
        if (!coursePointInfo) return Orig(self);
        long mgr = *(long*)((char*)coursePointInfo + 0x350);
        if (!mgr) return Orig(self);
        uint64_t courseKey = *(uint64_t*)((char*)mgr + 0x108);
        if (!courseKey) return Orig(self);

        if (probe::isCourseLockedByAp(courseKey)) {
            // Reset the lock byte to 0 (= closed) and return 1 (= Nerve done).
            // The two callers (slot 8 and slot 17 wrappers) interpret return=1
            // as "processing finished"; the actual gate decision is in the
            // lock byte, which we leave at 0 = LOCKED.
            uint64_t packedPtr = *(uint64_t*)((char*)self + 0x20);
            // Low 2 bits of the packed pointer are flags; the actual byte
            // pointer is (packedPtr & ~3).
            if ((packedPtr >> 1) & 1) {
                char* lockByte = (char*)(packedPtr & ~3ULL);
                if (lockByte) *lockByte = 0;
            }
            return 1;
        }
        return Orig(self);
    }
};

// Install at NSO+0x383418
FlowerLockGateBody::InstallAtOffset(0x383418);
```

### Boot-time descriptor walk (Option A from above)

```cpp
// Walk the course descriptor table to build sCourseKeyToApIndex.
// This needs to run after the game's stage manager has populated the
// table — likely after a particular Nerve fires the first time. The
// "current course" Nerves all walk through the same table.
//
// Simplest place to hook this: piggyback off the first call to
// FUN_71003D3FB0 or FUN_71003D4110 (the stage-info / Murmur3 lookup
// utilities from CLAUDE.md) — by then the descriptor table is live.

void identifyCourseDescriptors() {
    // Read the manager pointer that lives behind the gate function:
    // [gmd->something] OR [GameStateMgr+0x...]. Discover via Ghidra or
    // by trapping the first FUN_7100383418 call and capturing the chain.
    //
    // For each descriptor in the sorted table:
    //   uint64_t key = *(uint64_t*)descriptor;
    //   const char* name = readCourseName(descriptor);  // need offset RE
    //   if (isApManagedCourse(name)) {
    //       sCourseKeyToApIndex[key] = apIndexFor(name);
    //   }
}
```

This is the one remaining RE gap: extracting the course name from a
descriptor. Two options:

- Read the course's Murmur3 hash by inverting the lookup at
  `+0x71003D4110` (we know the 81 names; compute their Murmur3s; match
  against descriptor data).
- Find where the course name string is stored in the descriptor and
  read it directly.

The second is cleaner — needs a short Ghidra session on one descriptor.

### Bridge-side

```python
# apworld/smbw_archipelago/client/wire.py
@dataclass
class SetApLockedCoursesAbsoluteMsg:
    lo: int  # u64 bitfield, indices 0..63
    hi: int  # u64 bitfield, indices 64..127

# In SMBWContext._handle_received_items / on_hello / periodic tick:
def _ap_locked_courses_mask(self) -> tuple[int, int]:
    """Derive AP-locked-courses bitfield from current AP state.

    For the Royal Seed problem: a course is locked iff its AP location
    has NOT been checked yet. Player must play it to clear it; once
    the natural PALACE_CLEAR fires, the location flips, we drop the
    lock, and the world is back in vanilla shape.
    """
    lo = hi = 0
    for course_name, ap_idx in COURSE_NAME_TO_AP_INDEX.items():
        location_name = LOCATION_FOR_COURSE[course_name]
        if location_name in self.checked_locations:
            continue
        if ap_idx < 64: lo |= 1 << ap_idx
        else: hi |= 1 << (ap_idx - 64)
    return lo, hi

# Push triggers (mirror badge sync):
#   - on every AP RoomUpdate / ReceivedItems
#   - on every Switch HelloMsg (subsumes M4.5-style replay)
#   - on a ~2 s periodic tick (catches in-game state drift)
```

## Failure modes / edge cases to verify before merging

1. **Lock byte semantics**: confirm that setting the lock byte to 0
   actually prevents course entry (not just visual). The decompile
   shows it gets cleared by another path (`FUN_710032aea4(puVar16,
   uVar6)`) — we need to verify the consumer reads the byte we control.
2. **Animation desync**: the two execute slots model "transition to
   locked" vs "transition to open". Forcing the lock byte to 0
   mid-animation could glitch the visual. Likely benign (the next
   frame's Nerve tick will resolve correctly) but worth checking.
3. **Cutscenes**: world-map demos that move the player past a course
   point may rely on the gate being open. Need to test that lock state
   doesn't break inter-world transitions, palace-clear demos, etc.
4. **Save-data interplay**: if the player force-quits while a course
   is AP-locked, the save shouldn't be corrupted. The lock byte is in
   gmd-adjacent state but it's not what gets persisted (the persisted
   state is the seed bool / per-course flag in container B). Locking
   only affects runtime UI; restart preserves AP state.
5. **First-visit demo**:
   `GetWorldMapFirstVisitDemoWorldNoFromCoursePointInfo` is a Nerve
   that fires on first arrival at a world. Need to verify it doesn't
   pre-clear the lock state for "newly visited" courses.
6. **Palaces specifically**: palaces are course points but their gate
   semantics might differ. The IsDisplayFlowerLockUI Nerve handles
   "seed-bar gates" on world-map PATHS more than "course nodes". Need
   to verify the function fires for palace nodes specifically — likely
   yes since the gate body walks `CoursePointInfo` (palaces ARE course
   points) but worth confirming.

## Phased rollout plan

### Phase A — Confirm the hook works (1–2 days)

- Install the hook on a fresh build, returning 1 (= "locked") for ALL
  course-points unconditionally.
- Boot to world map, confirm all course points show locked / refuse
  entry.
- If yes, the architectural approach is proven. If no, lock byte isn't
  the gate (rethink).

### Phase B — Course-key extraction (1 day)

- Add observability hook on FUN_7100383418 to log `(courseKey,
  descriptor name)` for every fire.
- Walk through every course in the game (or just visit each world),
  collect the course-key → name mapping.
- Hard-code the mapping in the subsdk for the 6 palaces (smallest
  useful set for the seed problem).

### Phase C — Wire up AP control (2 days)

- Bridge: `SetApLockedCoursesAbsoluteMsg` + derivation logic + sync
  triggers (mirror badge-bitfield code).
- Switch: bitfield storage, `setApLockedCoursesAbsolute()`, hook
  consults bitfield.
- Test: lock W1 palace from AP, attempt entry, confirm refused. Clear
  AP-side (mark location checked), confirm gate reopens.

### Phase D — Remove the seed auto-resolve workaround (1 day, optional)

- The Royal Seed check-loss problem is now solvable naturally: keep
  the palace AP-locked until checked, player enters and clears the
  palace naturally, natural PALACE_CLEAR PlayReport fires, AP marks
  the location.
- Bridge stops setting the seed bool from AP (the natural clear path
  already sets it). The container-B grant for seed bools becomes
  unnecessary for AP-managed slots.
- Keep the bridge-side auto-resolve as belt-and-suspenders for at
  least one release; remove after dogfooding.

### Phase E (future) — Extend to course-level rando (open-ended)

- Lock any course based on AP rules (e.g., "Spring Feet badge required
  for X courses", "wave 2 unlocks after 5 wave 1 seeds").
- Same infrastructure, just a richer derivation function on the bridge.

**Total Phase A–D: ~5 days, plus Phase E ad-hoc.** Comparable to M4
badge sync.

## Open questions before starting Phase A

1. **Does setting `*((this+0x20) & ~3) = 0` actually prevent entry?**
   The decompile suggests yes, but verification needs a one-shot test
   hook. Cheapest possible Phase A: install a hook that forces lock=0
   on every gate call and observe.
2. **Are there OTHER gates besides `FUN_7100383418`?** A "press A to
   confirm entry" check might be on a separate code path (the
   `GetNeedBadgeIdEnterCourseForDirectCourseIn` Nerve hints at a
   "DirectCourseIn" alternate path that may bypass the flower-lock
   gate). Need to grep for confirm-handlers.
3. **What's the `FUN_71016cbf58` sub-call inside FUN_7100383418?** It
   reads hash `0x30bdd45c` (per our earlier `get_assembly_context`
   pass). If that's a "is course already unlocked permanently" flag,
   we may need to also intercept it.

## Loose ends from prior RE worth resolving here

- **The hash `0x46d90c82`** (read by the gate): still unidentified.
  Likely a per-course interactivity flag. If it's a container-B bool
  we already write via `grantContainerBBool`, we could in principle
  lock courses by clearing it — but we'd risk fighting whatever else
  reads/writes it. The hook approach is safer.
- **The hash `0x925d4260`** (read via custom virtual call on
  `gmd+8 vtable[+0x20]`): unknown. The custom call signature suggests
  it's a container variant we haven't catalogued. Worth a follow-up
  decompile of the virtual call target.
- **The hash `0x90d4d0f2`** (counter, with `(val | 2) != 2` check):
  unknown. The check pattern suggests this counter is required to
  have bits 2+ set (i.e., value >= 4) for "open". Could be the Wonder
  Seed count threshold for the path. Cross-check with the seed
  manifest in the apworld data.
