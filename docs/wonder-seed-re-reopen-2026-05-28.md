# Wonder Seed RE re-open — 2026-05-28

This doc tracks the second pass at reverse-engineering Wonder Seed
persistence + the natural in-game write path. Companion to
[docs/handoff.md](handoff.md) "Wonder Seed gate override" and
[docs/static-analysis-findings.md](static-analysis-findings.md)
"2026-05-26 — Wonder Seed gate-check RE".

## Why a second pass

Current state (shipped 2026-05-26):

- Gates pass via a **read-side override** at NSO `+0x12AE94`
  (`ContainerAReader` trampoline). When the gate predicate at NSO
  `+0x1787B40` reads container-A hash `0x390eb960`, we substitute the
  AP-authoritative count. Two other safe hashes (`0x21f89ab1`,
  `0x8c20ccb7`) also get substituted; two unsafe hashes
  (`0xeeff353b`, `0xa0e5f253`) are NOT substituted because they feed
  seed-gain animation buffers (substitution OOB's an array).
- Counts live in `g_wonder_seed_counts[8]` (atomic, in-memory only).
- Bridge replays the 8-tuple every ~2 s as a `SetWonderSeedCounts`
  message.

Two open problems the override does NOT solve:

1. **Persistence.** Save + quit + reload → the game popcounts the
   per-course Wonder Seed bitfield (hypothesized hash `0x60458608`)
   to recompute the per-world counter. That bitfield is zero, so the
   recomputed count is zero, and the player loses their AP-granted
   gate progress. Worked around at runtime (override re-substitutes
   on next read) but breaks any system that snapshots counts
   pre-substitute.

2. **UI consistency.** Sometimes the in-game UI lags the override
   (game caches the value mid-frame; substitute hits a later read).
   Players have reported needing to re-enter a level for a check to
   pass. The mirror group also resets on world transitions, so an
   override window can lapse during a transition.

3. **Crash-avoidance.** The earlier writer-side approach
   (`pushWonderSeedOverride`, retired in PR #40) crashed inside
   `FUN_710049F750` (container-A secondary-insert during scene
   transitions). We don't have a complete picture of WHY — only that
   write contention with the game's natural mutations is dangerous.
   More observability on the natural pipeline narrows the unknowns.

## The hypothesis we want to test

From [docs/static-analysis-findings.md](static-analysis-findings.md)
"2026-05-26 — Wonder Seed gate-check RE (iteration 7)":

> `0x60458608` is the **per-course Wonder Seed bitfield** (container-D
> shape; one bit per course). Read via
> `FUN_7100124134(gmd, &out, 0x60458608, bit_index)`. **Written via**
> `FUN_710049ea24(gmd, value, 0x60458608, bit_index)` — a 4-arg
> overload of the documented 3-arg Royal-Seed writer.

If true: writing per-course bits via this 4-arg call gives us full
persistence. AP grants a Wonder Seed → bridge maps the AP item to a
course_index → Switch writes `FUN_710049EA24(gmd, 1, 0x60458608,
course_idx)` → save preserves it → on reload the game's popcount sees
the bit set and the per-world counter is correct without ever needing
the read-side override (which would remain as a belt-and-suspenders
in-memory fallback).

The claim was never tested in 2026-05-26 because the session ran out
of time after the static analysis. It's the load-bearing hypothesis
for this re-open.

## Artifacts shipped this pass

### 1. Observability — new switch-mod trampolines

[`switch-mod/src/probe/SeedTrace.cpp`](../switch-mod/src/probe/SeedTrace.cpp)
defines three new trampolines wired in via
`probe::installSeedTraceHooks()`, called once from `hkMain` after the
existing Phase 2g hook installs.

| Hook | NSO offset | Purpose |
|---|---|---|
| `ContainerBWrapper4Arg` | `+0x0049EA24` | Trampolines `FUN_710049EA24` with a 4-arg signature `(gmd, value, hash, w3)`. Logs every call's 4 register slots. **The decisive test for the 4-arg overload claim**: if hash `0x60458608` ever flows through this wrapper from the game itself AND `w3` ranges 0..160 (course_index-shaped), the overload is real. For documented 3-arg Royal-Seed hashes, `w3` should be uncorrelated (whatever was last in `w3` before the call). |
| `PerCourseBitReader` | `+0x00124134` | Filtered to hash `== 0x60458608` and `== 0x580b7eb4`. Logs (hash, bit_index, *out, rc). Surfaces the game's natural read pattern — most likely fires during the per-world popcount loop on world-map state recompute. |
| `PerCourseBitfieldWriter` | `+0x01F2B354` | Trampolines the already-used 4-arg per-course u32 bitmask writer. Logs every (hash, value, course_index) write from game code AND from our own `setPerCourseBitfieldAbsolute` calls. Useful for spotting any game-internal write to hash `0x60458608` (which would mean per-course Wonder Seed storage actually uses this path, NOT the FUN_710049EA24 overload). |

All three are budgeted + deduped per `(hash, course_index_or_bit)`
tuple so the log ring isn't flooded during save deserialization.

Two helpers also exposed:

- `probe::setPerCourseWonderSeedBit(course_index, value)` — the
  hypothesis-driven test primitive. Calls
  `FUN_710049EA24(gmd, value, 0x60458608, course_index)`. **Not
  wired into the bridge** — purely a smoke-test entry point. The
  next session can add a boot-time call (e.g., `course_index = 0`,
  `value = 1`) and save-diff the result.
- `probe::dumpGmdSubstructsOnce(via)` — one-shot dump of gmd
  substruct base pointers + queue head/cap words at known + a
  speculative grid of `gmd+0x140..0x2E0` slots. Idempotent (atomic
  guard). Fires on the first `ContainerBWrapper4Arg` call, which is
  guaranteed post-save-deserialization. Reveals any unmapped
  substructs.

### 2. Static analysis — new Ghidra scripts

Five new scripts under [`scripts/ghidra/`](../scripts/ghidra/), all
documented in [`scripts/ghidra/README.md`](../scripts/ghidra/README.md)
under "Wonder Seed RE re-open (2026-05-28)".

| Script | What it discovers |
|---|---|
| [`find_hash_immediate_loads.py`](../scripts/ghidra/find_hash_immediate_loads.py) | Every function that materializes the 13 seeded 32-bit literals as a `mov`/`movz`/`movk` immediate. Inverts `walk_hash_writer_xrefs.py`: that script harvests CALLERS of known accessors and back-walks the hash; this finds every LOADER of a known hash regardless of accessor. Correlation table identifies aggregators (functions touching ≥2 seeded literals — likely popcount + mirror-write loops). |
| [`walk_gmd_field_access.py`](../scripts/ghidra/walk_gmd_field_access.py) | Histogram of `gmd+0xXX` access per offset, classified as DOCUMENTED vs NEW. Reveals unmapped substructs. The hypothesized container D for `0x60458608` must show up somewhere — this script pins which offset. |
| [`decompile_container_chain.py`](../scripts/ghidra/decompile_container_chain.py) | Decompile + plate-comment the chain `FUN_710049EA24 → FUN_71005E93FC → FUN_7101F263FC`, plus `FUN_7101F2B354`, `FUN_7100124134`, `FUN_71001787B40`, `FUN_7100935CE0`. Verifies the 4-arg overload claim against the decompile body, and surfaces any internal dispatch logic that switches on hash. |
| [`find_wonder_seed_acquisition_chain.py`](../scripts/ghidra/find_wonder_seed_acquisition_chain.py) | BFS from the WonderSeedAwarded Nerve vtable's execute slot (NSO+0x3345728+0x40), depth-limited to 6. Flags hits on every known writer offset. Output is an indented call tree showing exactly which game code path leads to a container write when Mario grabs a Wonder Seed. |
| [`brute_seed_field_hashes.py`](../scripts/ghidra/brute_seed_field_hashes.py) | Host-side. Murmur3-32 (multiple seeds), FNV-1a, CRC-32 brute force over Wonder Seed / per-course / world / Japanese-romaji candidate names against the 10 unknown seed hashes. Mirror of `brute_badge_field_hashes.py`. A hit names the field; zero hits is itself useful (rules out Murmur3 + dictionary, narrows hypothesis space). |

## Test protocol (next session)

Phase A — verify the observability shipped this pass

1. Build switch-mod per [CLAUDE.md](../CLAUDE.md) "Daily dev loop".
   Confirm the new SeedTrace hooks install (`[smbwap inf] [seed-trace]
   install ContainerBWrapper4Arg @ +0x49ea24 OK` etc. in the log).
2. Load a save with some seeds owned. Confirm the
   `dumpGmdSubstructs` one-shot fires once with a non-null gmd and a
   sensible spread of substruct pointers.
3. Confirm `PerCourseBitReader` fires on world-map transition with
   hash `0x60458608` and bit_index values 0..N. If yes: the reader
   IS reading that hash and our 0x60458608 hypothesis is supported.
4. Confirm `ContainerBWrapper4Arg` fires for the 9 documented
   bool/counter hashes during save deserialization. Note `w3` values
   for each. For Royal-Seed hashes, `w3` should be garbage (any value).
   If it's consistently 0 or a specific small constant, that's
   suggestive that the wrapper IS reading w3 and the 4-arg overload
   claim holds (but on a different dispatch — w3 might be a different
   role for those hashes).

Phase B — Mario grabs a Wonder Seed

5. Enter a course with an uncollected Wonder Seed. Grab it.
6. Watch the log for:
   - `ContainerBWrapper4Arg fire ... hash=0x60458608 val=1 w3=<bit_index>`
     — confirms the game uses the 4-arg overload on Wonder Seed grab,
     names the bit_index.
   - OR `PerCourseBitfieldWriter fire ... hash=0x60458608 course=N
     value=0x...` — confirms the game uses the u32-bitmask writer
     instead (different storage scheme).
   - OR neither — game uses a third path we haven't hooked, hint
     in `dumpGmdSubstructs` output points to the substruct.

Phase C — test the write primitive (after Phase B narrows shape)

7. If Phase B confirmed the 4-arg overload + bit-index: add a
   boot-time call to `probe::setPerCourseWonderSeedBit(course_idx=0,
   value=1)`. Save + quit. Run save-diff against the per-course
   Wonder Seed flag array at file offset `0x3AF8` (per
   [docs/save-diff-findings.md](save-diff-findings.md)). If the right
   u32 flipped, the write primitive works.

Phase D — wire into the bridge (only after C succeeds)

8. Add `SetPerCourseWonderSeedsMsg { course_indices: list[u32] }` to
   `apworld/smbw_archipelago/client/wire.py`. Bridge derives the
   list from `items_received` via a new course_index ↔ Wonder Seed
   item ID table.
9. Switch dispatch in `ApFrameBridge::drainInbound` iterates the
   list and calls `probe::setPerCourseWonderSeedBit` for each.
10. Idempotent absolute-set on `ReceivedItems` + `HelloMsg` +
    periodic tick — same pattern as `SetBadgesAbsoluteMsg`.

Phase E — fallback if 0x60458608 turns out NOT to be the storage hash

The Ghidra scripts ship orthogonal lines of evidence:

- `find_hash_immediate_loads.py` → tells us every function that loads
  0x60458608. If only `FUN_7100124134` does (and zero writers), the
  hash is read-only from this storage layout, and Wonder Seed bits
  live elsewhere.
- `walk_gmd_field_access.py` → unmapped substructs surface here.
- `find_wonder_seed_acquisition_chain.py` → the natural write path
  from WonderSeedAwarded Nerve execute is in the BFS output even if
  it doesn't hit a known primitive.
- `decompile_container_chain.py` → the actual code is the deciding
  evidence; if the 4-arg overload claim is wrong, the decompile
  shows it directly.

## Hard constraints (re-read before coding)

- **No new thread_local.** Use `std::atomic` + boot-init pattern, as
  documented in [CLAUDE.md](../CLAUDE.md) "Critical gotchas" #1.
- **Hook entry safety.** Hakkun's trampolines are more robust than
  exlaunch's And64InlineHook, but PC-relative ops in the first 5
  insns of a hook target can still corrupt the trampoline. The
  three new hooks (`+0x49EA24`, `+0x124134`, `+0x1F2B354`) are all
  functions we've called extensively from `probe::`; calling is safe.
  Trampolining is one notch riskier. If a build deploys and one of
  the three never logs (the others do), suspect prologue corruption
  and pivot to hooking a caller instead.
- **No live grant calls without backpressure check.**
  `setPerCourseWonderSeedBit` already gates on `checkContainerB()`.
- **No bridge changes in this RE phase.** Phase A-C is observability
  + smoke test only. The bridge integration in Phase D is a
  separate, gated change.

## Backstop: even if 0x60458608 doesn't pan out

Independent of whether the per-course bitfield write works, the
artifacts ship value:

1. **`dumpGmdSubstructs`** gives us the first complete view of the
   live gmd struct layout — useful for every future container RE.
2. **`PerCourseBitReader` logs** characterize when the game popcounts.
   Even if we never write the bitfield, knowing the popcount cadence
   (every world transition? every map-load?) tells us when the
   override needs to fire.
3. **`ContainerBWrapper4Arg` logs** for the documented Royal-Seed hashes
   tell us whether `w3` is consistently garbage or actually consumed
   — if consumed, we have a previously unmapped semantics for the
   container-B writer.
4. **`walk_gmd_field_access.py` histogram** narrows where any
   unmapped container substruct lives, even if 0x60458608 doesn't
   route through it.
5. **Crash-avoidance**: the 2026-05-26 race in `FUN_710049F750` is
   better characterized by the natural-write logs from
   `PerCourseBitfieldWriter` and `ContainerBWrapper4Arg`. Knowing
   exactly when the game writes vs. when we can safely interject
   reduces the risk of repeating PR #40.
