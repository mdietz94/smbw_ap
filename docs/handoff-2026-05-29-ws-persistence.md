# Wonder Seed persistence handoff — 2026-05-29 EOD

This doc captures all findings from the 2026-05-29 session re-opening
the Wonder Seed RE work.  Branch: the worktree at
`C:\Users\maxwe\Documents\smwonder_archipelago\.claude\worktrees\bridge-cse_01KtYB9wuC3NNZhW48vZ89Zd`.
Read this first before continuing.  Other entry-points:

- [docs/handoff.md](handoff.md) — broader project handoff (older)
- [docs/wonder-seed-re-reopen-2026-05-28.md](wonder-seed-re-reopen-2026-05-28.md) — yesterday's planning doc (now mostly superseded by the work shipped today)
- [docs/static-analysis-findings.md](static-analysis-findings.md) — long-form RE notes
- [CLAUDE.md](../CLAUDE.md) — repo orientation (still authoritative for daily dev loop, hooks, gotchas)

## TL;DR — what shipped + what's open

**Working end-to-end:**

1. **AP-authoritative per-course Wonder Seed bitfield** in container-C
   at hash `0x60458608`.  Bridge derives a 128-bit mask from
   `items_received`, Switch overwrites the live state via
   `probe::setWonderSeedBitfieldAbsolute(bits_lo, bits_hi)` on every
   `ReceivedItems` / `HelloMsg` / 2 s tick.
2. **AP-authoritative per-world Wonder Seed counter** at container-A
   hash `0x390eb960` (+ 4 mirror hashes).  Re-enabled
   `probe::pushWonderSeedOverrideCurrentWorld()` with safety gates
   after user hypothesized PR #40's crash was bitfield-counter
   inconsistency, not the writer itself.  **No crash.**  Live-validated
   with 12 → 14 W1 seeds: counter writes value=12 then 14 in lockstep
   with bridge bit-mask.
3. **Disk persistence** of the LOW 64 bits (data[0..1]) confirmed:
   set bit 0 cleared via push, save + quit + Ryujinx restart →
   `D_read fire=0 hash=0x60458608 bit=0 value=0` on next boot.
4. **Bridge replay** (HelloMsg push on Switch reconnect) covers the
   HIGH 64 bits (data[2..3]) which don't persist to disk for hash
   `0x60458608`.  Functionally end-to-end works because the bridge
   re-pushes within ~1 s of connect.

**Open questions** (left for the next session — these are the
followups the user asked about at end-of-session):

1. **Why does the bitfield only cover 80 entries when the game has 131
   levels?**  The Murmur3 course lookup table at NSO `+0x34dec90` is
   exactly 81 qwords (entries `Course1` ... `Course80` + `Invalid`).
   But online sources say SMBW has 131 levels (main + side).  So hash
   `0x60458608` only tracks ~80 "main" courses?  What tracks the
   side courses?  Is there a sibling bitfield for them?
2. **Where does the per-seed bitmask live?**  The in-game UI shows
   WHICH specific seeds you've collected per course (top-of-flag,
   secret-exit, wonder-phase, badge challenges).  Hash `0x60458608`
   is binary per-course (1 bit each).  The granular per-seed state
   must be stored elsewhere.  STRONG candidate:
   `probe::setPerCourseBitfieldAbsolute(hash, course_index, bitmask)`
   already exists and writes to a (hash, course_index) → u32 bitmask
   container at `gmd+0x788` (see "container D" section below).  We
   know GoalSeed/CourseClear hashes are in there.  **The Wonder Seed
   variant hash is unknown** and the live observability already
   captured candidate hashes (see "Captured per-course hashes"
   section).  Cross-reference against game actions to identify
   which is WS.
3. **Why does data[2..3] of hash `0x60458608` not persist?**  Both
   badges (hash `0x105df820`) and Wonder Seeds use the same
   container-C uint32_t[4] storage.  For badges, M3.2 found
   data[2..3] = mirror of data[0..1].  For WS, we saw the same
   natural pattern (data[0] == data[2] = 0x0f on load).  When we set
   bit 80 (data[2] bit 16), it didn't persist — but bit 80 is past
   the table's last valid index (table has Invalid at index 80).
   **Hypothesis worth testing**: set a bit in the 64..79 range
   (which IS valid per the 80-entry table); see if it persists.
   If yes, then data[2..3] DOES persist for valid course indices
   and only "out-of-range" bits get stripped by the save serializer.
   If no, then data[2..3] is truly a non-persistent mirror and the
   80-bit table is effectively 64 addressable bits.
4. **The 80-bit-per-world reordering proposal was deferred** — we'd
   produced a layout for using GAME-order packing (W1, Petal Isles,
   W2, ..., Special with main-course counts) but the user paused to
   ask the questions above before we shipped it.  The current
   bridge still uses naive 16-per-world AP-order packing.

## Architecture summary

### Switch side

- `switch-mod/src/probe/ContainerC.cpp`
  - `findContainerCData(hash)` — walks `gmd+0x80` buckets,
    follows to `gmd+0x78 + idx*0x40 + 0x28` for the data pointer.
  - `setBadgeBitfieldAbsolute(uint64_t bits)` — M3.2 badge writer.
  - `setContainerCBit(hash, bit_index, value)` — single-bit toggle.
  - **NEW (2026-05-29):** `setWonderSeedBitfieldAbsolute(uint64_t
    bits_lo, uint64_t bits_hi)` — 128-bit overwrite at hash
    `0x60458608`.  Writes all 4 u32s as distinct bits (not mirrors).
- `switch-mod/src/probe/PerCourse.cpp`
  - `setPerCourseBitfieldAbsolute(hash, course_index, bitmask)` —
    writes via `FUN_7101F2B354` at gmd+0x788 substruct (container D).
  - **MODIFIED (2026-05-29):** `pushWonderSeedOverrideCurrentWorld()`
    — added safety gates (isSaveLoaded + !isInSceneTransitionWindow
    + checkContainerA backpressure refusal).
- `switch-mod/src/probe/SeedTrace.cpp` / `.hpp`
  - 3 observability trampolines (still installed, all working):
    - `ContainerBWrapper4Arg` @ NSO+0x49EA24 — `B_wrap fire=N hash=X val=Y w3=Z`
    - `PerCourseBitReader` @ NSO+0x124134 — `D_read fire=N hash=X bit=Y value=Z`
    - `PerCourseBitfieldWriter` @ NSO+0x1F2B354 — `D_write fire=N hash=X course=Y value=Z`
  - `setPerCourseWonderSeedBit(course, value)` — dev helper, calls
    setContainerCBit(0x60458608, course, value).  Used by
    `triggerWonderSeedSmokeTest` (only fires when manually invoked).
  - `dumpGmdSubstructsOnce` — fires once on first B_wrap fire,
    prints gmd substruct base pointers + queue head/cap words.
- `switch-mod/src/ap/ApProtocol.hpp` / `.cpp`
  - **NEW (2026-05-29):** `WireSetWonderSeedsAbsolute { bits_lo, bits_hi }`
    + parser + `InboundKind::SetWonderSeedsAbsolute = 13` + dispatch.
- `switch-mod/src/ap/ApFrameBridge.cpp`
  - **NEW (2026-05-29):** drainInbound dispatch case for
    SetWonderSeedsAbsolute (dedup last-write-wins like badges) →
    apply via `probe::setWonderSeedBitfieldAbsolute`.
  - **MODIFIED:** SetWonderSeedCounts apply block now also calls
    `probe::pushWonderSeedOverrideCurrentWorld()` after caching the
    counts.
- `switch-mod/src/ap/ApClient.cpp`
  - **NEW (2026-05-29):** switch case for
    `InboundKind::SetWonderSeedsAbsolute` in the per-kind dispatcher
    (was MISSING which caused the parse-but-drop bug we fixed).
- `switch-mod/src/main.cpp`
  - Removed the one-shot Wonder Seed smoke test from
    SetCourseClearFlagExecute (AP-driven push supersedes it).

### Bridge side (`apworld/smbw_archipelago/client/`)

- `wire.py`
  - **NEW:** `SetWonderSeedsAbsoluteMsg { bits_lo, bits_hi }`
  - Added to `WireMsg` union and `_FROM_WIRE` registry.
- `lan_server.py`
  - **NEW:** `WonderSeedBitsProvider` callable type alias.
  - **NEW:** `send_set_wonder_seeds_absolute(bits_lo, bits_hi)`.
  - **NEW:** `_push_wonder_seed_bits_now()` (called from HelloMsg
    handler + `_idempotent_sync_loop` every 2 s).
  - **NEW:** writer-loop log branch for the new message + `_last_logged_wonder_seed_bits` dedup tracker.
- `context.py`
  - **NEW:** `_recompute_wonder_seed_bits() -> (bits_lo, bits_hi)`.
    Currently uses naive 16-per-world AP-order packing.
    **This is what needs to change for proper persistence.**
  - `_handle_received_items` now also calls
    `send_set_wonder_seeds_absolute`.
- `main.py`
  - Passes `wonder_seed_bits_provider=ctx._recompute_wonder_seed_bits`
    to `LanServer` ctor.

### Deployed locations

- Switch subsdk: built into `switch-mod/build/exefs/subsdk9` and
  copied to `$APPDATA/Ryujinx/mods/contents/010015100b514000/smbwap/exefs/subsdk9`.
- Apworld: zipped from worktree, dropped at
  `C:\ProgramData\Archipelago\custom_worlds\smbwonder.apworld`.
- Build/deploy commands: see CLAUDE.md "Daily dev loop".  The
  apworld zip is built via:

  ```pwsh
  python -c "
  import zipfile
  from pathlib import Path
  SRC = Path('apworld/smbw_archipelago').resolve()
  DST = Path('C:/ProgramData/Archipelago/custom_worlds/smbwonder.apworld')
  SKIP = {'__pycache__', '.mypy_cache', '.ruff_cache', '.pytest_cache', 'tests'}
  files = [p for p in SRC.rglob('*') if p.is_file() and not any(part in SKIP for part in p.parts)]
  with zipfile.ZipFile(DST, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
      for p in files: zf.write(p, (Path('smbwonder') / p.relative_to(SRC)).as_posix())
  print(f'OK: {len(files)} files, {DST.stat().st_size/1024:.1f} KiB')
  "
  ```

  **Caveat for restart:** Closing the SMBW Client window does NOT
  reload the apworld code — the Archipelago Launcher process caches
  imports.  Must kill ALL `python.exe` processes via Task Manager
  before re-launching, then click SMBW Client again.

## Game state map

### GameDataMgr (gmd) singleton

Anchor: NSO `+0x363F0F0` (qword); deref gives live `GameDataMgr*`.
At runtime in Ryujinx: address `0x20d29a07a8` (deterministic, repeats
across boots).

### Container layout in gmd struct

```
gmd+0x008  container B substruct (bool slots like Royal Seeds, INTRO, COMPLETE_GAME)
gmd+0x068  container B init/lock
gmd+0x070  container C count limit
gmd+0x078  container C typed sub-obj array (each 0x40 bytes)
gmd+0x080  container C bucket array (8-byte entries: u32 hash + u32 idx)
gmd+0x08C  container C bucket count
gmd+0x0C8  container A substruct base
gmd+0x0F0  container A dirty queue cap
gmd+0x0F8  container A dirty queue ring
gmd+0x100  container A dirty queue head
gmd+0x128  container A secondary substruct
gmd+0x150  container A secondary cap
gmd+0x160  container A secondary head
gmd+0x788  **PER-COURSE BITMASK substruct ("container D")** ← key for question #2
gmd+0x7E8  container D lock
```

### Container C bitfield reader (FUN_7100124134)

```c
*out = (data[bit_index >> 5] >> (bit_index & 0x1F)) & 1
```

Where `data` is the typed sub-obj's u32[4] data pointer at
`*(gmd+0x78 + bucket_idx*0x40 + 0x28)`.  So bit 0 = data[0]&1,
bit 63 = data[1]>>31, bit 64 = data[2]&1, bit 127 = data[3]>>31.

### Container C known hashes

| Hash | Purpose | Storage shape | Save persistence |
|---|---|---|---|
| `0x105df820` | Badge owned bitfield | u64 (u32[0..1] real, u32[2..3] mirror) | ✅ persists; file offset 0x0EA0 |
| `0x60458608` | Per-course Wonder Seed (binary) | u32[4] | ✅ data[0..1] persists, data[2..3] empirically does not (see Q3) |
| `0x6d1b5c25` | Badge UI slot bitmap (aux, NOT canonical) | u32[4] | ✅ persists; file offset 0x1204 |

### Container D writer (FUN_7101F2B354)

Signature: `(gmd, u32 value, u32 hash, u32 course_index)`.
Routes through gmd+0x788 substruct.  Each (hash, course_index) →
u32 bitmask entry.  Each bit in the u32 = one "exit type" for that
course (bit 0 = normal pole, bit 1 = secret exit, ...).

**Known container D hash usage** (from `PerCourse.cpp` comments,
discovered via 2026-05-25 forward-trace + save-diff cross-check):

| Field | File offset | Course range | Notes |
|---|---|---|---|
| GoalSeed (normal) | 0x3348 | per-course u32 | hash unknown |
| GoalSeed (badge challenges) | 0x3390 | per-course u32 | hash unknown |
| CourseClear (normal) | 0x43F0 | per-course u32 | hash unknown |
| CourseClear (badge challenges) | 0x4438 | per-course u32 | hash unknown |
| WonderSeed (mid-course phase) | 0x3AF8 (hypothesized) | per-course u32 | hash unknown — **this is what question #2 asks for** |

### Captured per-course hashes from observability (D_write events)

Observed via `PerCourseBitfieldWriter` trampoline in
`Ryujinx_1.3.3_2026-05-29_11-19-52.log` and similar.  These are
container-D writes the GAME ITSELF performed during gameplay:

```
D_write hash=0xa6140d7c course=5  value=0x00000001
D_write hash=0x6f9dfc59 course=5  value=0x00000000
D_write hash=0x0235d948 course=5  value=0x00000001
D_write hash=0xa4dbcac9 course=5  value=0x00000000
D_write hash=0x46721422 course=11 value=0x00000001
D_write hash=0xb161b8ab course=11 value=0x00000000
D_write hash=0x03722209 course=11 value=0x00000001
D_write hash=0x4ebb08c0 course=11 value=0x00000000
```

These 8 hashes were observed paired (one writes value=1, the
"matching" one writes value=0 immediately after) during specific
gameplay moments.  Cross-referencing each pair to a known game
action (course clear vs. seed grab vs. specific exit type) would
let us identify the WonderSeed hash.

### Course name table (FUN_71003D4110 Murmur3 lookup)

- Table base: NSO `+0x34dec90` (runtime `0x71034dec90`)
- 81 qword entries, each → const char* course name
- Names: `Course1`, `Course2`, ..., `Course80`, `Invalid` (at index 80)
- **No world info embedded in names — purely sequential numbering**
- Confirmed by `scripts/ghidra/dump_course_table_direct.py`
- Position in table == `course_index` == bit position in container-C
  bitfield at hash `0x60458608`

### Game's internal world enum (from FUN_7100743D10 decompile)

| Game world_val | Meaning | Maps to AP bucket |
|---|---|---|
| 1 | W1 (codename Savanna) | 0 |
| 2 | Petal Isles | 6 |
| 3 | W2 | 1 |
| 4 | W3 | 2 |
| 5 | W4 | 3 |
| 6 | W5 | 4 |
| 7 | W6 (codename Nettai) | 5 |
| 8 | Castle (aliased to Petal Isles?) | -1 |
| 9 | Special (codename Himitu) | 7 |

**Game order through the course table is LIKELY**: W1, Petal Isles,
W2, W3, W4, W5, W6, Special.

### Real game course counts per world (per online research)

| World | Main | Side | Total |
|---|---|---|---|
| W1 Pipe-Rock Plateau | 12 | 8 | 20 |
| Petal Isles | 11 | 6+5 | 22 |
| W2 Fluff-Puff Peaks | 10 | 6 | 16-18 |
| W3 Shining Falls | 7 | 6 | 13 |
| W4 Sunbaked Desert | 10 | 11 | 21 |
| W5 Fungi Mines | 8 | 5 | 13 |
| W6 Deep Magma Bog | 7 | 9 | 16 |
| Special World | — | — | 10 |
| **Total** | **65** | **51** | **131** |

But the course table only has 80 entries.  So `0x60458608` only
tracks main courses + maybe palace bosses (≈ 75 main + 5 extra = 80?).
**Side courses must be tracked elsewhere — see question #1.**

### AP item counts per world (from `apworld/.../data/items.json`)

| Item | Count |
|---|---|
| W1 Wonder Seed | 35 |
| W2 Wonder Seed | 30 |
| W3 Wonder Seed | 20 |
| W4 Wonder Seed | 36 |
| W5 Wonder Seed | 21 |
| W6 Wonder Seed | 30 |
| Petal Isles Wonder Seed | 34 |
| Special World Wonder Seed | 19 |
| **Total seeds** | **225** |

225 seeds across ~75-80 courses = avg 2.8 seeds per course, which
matches what we'd expect (each course has 1-4 seeds: top-of-flag,
secret exit, wonder phase, badge challenge in some).

## Empirical persistence behavior

### What persists to disk for container-C hash 0x60458608

- **data[0..1] (bits 0-63):** ✅ PERSISTS.  Cleared bit 0 in one
  session, save+restart Ryujinx, next session's first D_read on bit
  0 returns value=0.
- **data[2..3] (bits 64-127):** Empirically did NOT persist in our
  test: set bit 80 (data[2] bit 16) via absolute push, save+restart,
  next session's `before=[0,0,0,0]`.  BUT: bit 80 is index 80 =
  `Invalid` (per the table dump).  Save serializer may strip
  invalid bits (matches CLAUDE.md badge comment: "save serializer
  filters out bit positions that don't correspond to real badges").
  **Untested: set a bit in 64-79 range and see if it persists.**

### Counter (container-A hash 0x390eb960)

- Counter is independently persisted (does NOT derive from bitfield
  popcount on load).
- Save deserializer writes the natural saved counter (we observed
  value=16 from natural play loaded back even when our previous
  push cleared the bitfield).
- Our `pushWonderSeedOverrideCurrentWorld()` overwrites the counter
  to AP's value AFTER deserialization completes (gated on
  isSaveLoaded + !isInSceneTransitionWindow + backpressure).

### Reader-side override (still installed)

`containerAReaderHook` at NSO+0x12AE94 substitutes 3 safe mirror
hashes (0x390eb960 gate-predicate, 0x21f89ab1 in-course HUD,
0x8c20ccb7 wildcard) to AP's count on every read.  This is
belt-and-suspenders — usually a no-op now that `pushWonderSeedOverride`
keeps the underlying counter consistent with AP, but covers the
brief window between save load and our first counter write.

## Current bridge bit-packing (the part the user paused us on)

`apworld/smbw_archipelago/client/context.py::_recompute_wonder_seed_bits`:

```python
BITS_PER_WORLD = 16
bits = 0
for w, count in enumerate(counts):  # counts is AP-bucket-ordered
    capped = min(count, BITS_PER_WORLD)
    if capped <= 0: continue
    bucket_mask = (1 << capped) - 1
    bits |= bucket_mask << (w * BITS_PER_WORLD)
```

Counts indexed by AP bucket: `[W1, W2, W3, W4, W5, W6, Petal, Special]`.
So current layout:

| AP bucket | Bits | Lands in u32 |
|---|---|---|
| 0 W1 | 0-15 | data[0] ✅ persists |
| 1 W2 | 16-31 | data[0] ✅ persists |
| 2 W3 | 32-47 | data[1] ✅ persists |
| 3 W4 | 48-63 | data[1] ✅ persists |
| 4 W5 | 64-79 | data[2] ❌ (this is the "doesn't persist" half) |
| 5 W6 | 80-95 | data[2..3] ❌ AND past-table-end |
| 6 Petal Isles | 96-111 | data[3] ❌ AND past-table-end |
| 7 Special | 112-127 | data[3] ❌ AND past-table-end |

Pulls double-duty in the wrong direction:
- AP-order != game-order, so AP's W5 bits go where the game's
  Petal Isles bits "should" be.
- 16 per world overflows the 80-bit table for any world that has
  fewer than 16 actual courses.

## Proposed (but not yet shipped) fix to bit-packing

Use the game's internal world order with main-course counts (75
slots used + 5 padding):

```python
# Position in game's course table => (AP bucket, n_slots)
GAME_ORDER_LAYOUT = [
    (0, 12),  # W1
    (6, 11),  # Petal Isles
    (1, 10),  # W2
    (2, 7),   # W3
    (3, 10),  # W4
    (4, 8),   # W5
    (5, 7),   # W6
    (7, 10),  # Special
]

def _recompute_wonder_seed_bits(self):
    counts = self._recompute_wonder_seed_counts()
    bits = 0
    offset = 0
    for ap_bucket, n_slots in GAME_ORDER_LAYOUT:
        capped = min(counts[ap_bucket], n_slots)
        if capped > 0:
            bits |= ((1 << capped) - 1) << offset
        offset += n_slots
    bits_lo = bits & ((1 << 64) - 1)
    bits_hi = (bits >> 64) & ((1 << 64) - 1)
    return (bits_lo, bits_hi)
```

This puts everything except Special and W6's last bit in the
persistent half (bits 0-63 = data[0..1]).

But the user paused us before shipping this because:

> Doesn't it seem weird this would only cover some of the worlds?
> And if this just tracks if a level has been completed, somewhere
> must track which seeds have been collected in a world, because
> that also displays in the UI, can you think of how we would
> discover that bitmask?

The question is whether the 80-bit hash `0x60458608` storage is the
right model at all, or if we're missing a richer per-seed bitmask
elsewhere.

## Open questions to investigate next session

### Q1: Where are the 50+ side courses tracked?

The Murmur3 course table has 80 valid entries.  Vanilla SMBW has
131 levels.  So `0x60458608` only covers ~60% of levels — main
courses, presumably.  The remaining ~50 side courses (KK balls,
badge challenges, mid-course warps, hidden areas, palace bosses,
the 8 W5 mushroom houses) are tracked somewhere ELSE.

Hypotheses:
- Multiple container-C bitfields, one per "category" of course.
  We'd see them as different hashes in observability.
- A container-D u32 array indexed by course-category, with each u32
  being a bitmask of N side-course slots.
- A separate non-`0x60458608` bitfield with different hash.

Discovery paths:
- Look at the captured `D_write` events (above) for hashes paired
  with course_index values >= 80.  If any exist, those are the
  side-course storage.
- Decompile FUN_71001E4AE0 (called at start of FUN_710066E548) —
  per the decompile, it returns the iteration count.  If it returns
  ~12 for W1's map but the game has 20 W1 levels, the populator is
  only walking main courses.  Sibling code paths probably handle
  side courses.
- Scan `.rodata` for OTHER 81-string-table-shaped structures near
  `+0x34dec90`.  There may be 1 table per course category.

### Q2: Where is the per-seed (which seeds in this course) bitmask?

In-game UI shows individual seed icons per course slot (e.g.,
"top-of-flag seed collected", "secret-exit seed collected", "wonder
phase seed collected").  This per-seed granular state is stored
SOMEWHERE — and AP's 225 Wonder Seed items would ideally map to it
1:1 (each AP item == one specific seed in one specific course).

**STRONG candidate**: container D at `gmd+0x788` — `(hash,
course_index) -> u32 bitmask`.  Each u32 has 32 bits, with each
bit representing one exit type for that course.  Already-used
hashes (in `PerCourse.cpp`) include GoalSeed/CourseClear for both
normal courses and badge challenges, all at known file offsets but
unknown hashes.  **The Wonder Seed variant hash is what we need.**

Discovery paths:
- The 8 captured `D_write` hashes (above) are STRONG candidates.
  Run the game, trigger a single specific event (e.g., grab the
  wonder-phase seed in one course), capture the immediate `D_write`
  events, then we know which hash is "WonderSeed write".
- Decompile `FUN_7101F2B354` (the per-course bitmask writer) and
  trace its callers.  Look for callers that fire at Wonder Seed
  acquisition moments.
- Look at the save file at offset 0x3AF8 (the hypothesized
  WonderSeed mid-course bitmask region).  Hex-diff a save with no
  WS vs. a save with 1 WS — the changed byte is in this region.
- Decompile FUN_71000E258C (the per-course bitmask READER) and
  cross-reference its hash-argument call sites against the game's
  Wonder Seed UI overlay code.

### Q3: Does data[2..3] for 0x60458608 actually persist?

We saw bit 80 not persist, but bit 80 is `Invalid` per the table.
Bits 64-79 ARE valid courses in the table.  **Untested**: does
setting one of those bits (e.g., bit 70) persist?

Quickest test: temporarily revert the `_recompute_wonder_seed_bits`
to set bit 70 unconditionally (force `bits_hi |= 1 << 6`); save,
restart Ryujinx, check next session's BEFORE state.  If
`data[2] == 0x40` (bit 6 of data[2] = bit 70), persists.  If
`data[2] == 0`, doesn't.

This is the SIMPLE persistence test we need before deciding what
allocation to use.

### Q4: Reorder + ship the GAME_ORDER_LAYOUT proposal

Conditional on Q3's answer.  If data[2..3] persists, full
128-bit GAME_ORDER_LAYOUT can fit everything.  If not, restrict
to the persistent 64-bit half.

## Useful files for the next session

### Already-built Ghidra scripts

- `scripts/ghidra/dump_course_table_direct.py` — re-run for the
  81-entry course name list.  Output: `Course1`..`Course80` +
  `Invalid`.  **No world info in names** — they're literally just
  sequential numbers.
- `scripts/ghidra/dump_course_name_table.py` — broader scan; tried
  to find string tables but mostly found unrelated rodata.  The
  decompile of FUN_71003D4110 it produced revealed the actual table
  address.
- `scripts/ghidra/find_hash_immediate_loads.py` — find functions
  that load specific 32-bit literals.  Seed with the 8 captured
  D_write hashes above to find their loaders and callers.
- `scripts/ghidra/decompile_container_chain.py` — decompiles
  documented WS-related functions; add the 8 D_write hash users to
  the TARGETS list to investigate.

### Logs of interest

- `Ryujinx_1.3.3_2026-05-29_11-19-52.log` — captured the 8 D_write
  hashes (search for `D_write fire=`).
- `Ryujinx_1.3.3_2026-05-29_12-06-43.log` — confirmed counter write
  works (search for `pushWonderSeedOverride: wrote value=`).
- `Ryujinx_1.3.3_2026-05-29_12-01-30.log` — confirmed bit 0 clear
  persisted to disk (D_read fire=0 hash=0x60458608 bit=0 value=0).

### Key source files

- `switch-mod/src/probe/ContainerC.cpp` — primitives + setWonderSeedBitfieldAbsolute
- `switch-mod/src/probe/PerCourse.cpp` — pushWonderSeedOverrideCurrentWorld + setPerCourseBitfieldAbsolute
- `switch-mod/src/probe/SeedTrace.cpp` / `.hpp` — observability hooks
- `switch-mod/src/ap/ApFrameBridge.cpp` (drainInbound) — dispatch + apply
- `switch-mod/src/ap/ApClient.cpp` (handleInboundMsg) — per-kind switch
- `apworld/smbw_archipelago/client/context.py::_recompute_wonder_seed_bits` — bridge bit derivation
- `apworld/smbw_archipelago/client/wire.py` — SetWonderSeedsAbsoluteMsg
- `apworld/smbw_archipelago/client/lan_server.py` — send method + periodic tick

## User preferences for next session

- **Delegate noisy ops** to subagents — submodule init, full builds,
  large log dumps.  Keep main context clean.  See
  `feedback_delegate_noisy_ops` memory.
- Avoid asking the user to play the game when possible — prefer
  RE-derived facts to live observability.

## Concrete next actions in priority order

1. **Test Q3** — change bridge bit-packing to set ONE specific bit
   in the 64-79 range (e.g., bit 70), save+restart, observe.  Tells
   us whether to keep 128-bit or restrict to 64-bit.
2. **Identify the per-seed hash** for container D writing on Wonder
   Seed grabs.  Use one of the discovery paths under Q2.  Once
   known, container-D `setPerCourseBitfieldAbsolute` already exists
   to write it (just needs to know the hash).
3. **Find the side-course bitfield** for Q1 — probably a different
   container-C hash, or a separate container-D layout.  The captured
   8 D_write hashes are the seed for this investigation.
4. **Ship GAME_ORDER_LAYOUT** (Q4) once Q3 answers what allocation
   makes sense.
5. Once Q2 is answered, AP can become PER-SEED authoritative (not
   just per-course), and the AP UI markers can show which specific
   seeds (top-of-flag, secret exit, etc.) AP has granted.

## Memory files to read first

In `C:\Users\maxwe\.claude\projects\C--Users-maxwe-Documents-smwonder-archipelago--claude-worktrees-bridge-cse-01KtYB9wuC3NNZhW48vZ89Zd\memory\`:

- `MEMORY.md` — index
- `feedback_delegate_noisy_ops.md` — delegate-noisy-ops preference
