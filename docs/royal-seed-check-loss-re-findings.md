# Royal Seed check-loss — Phase 1 static RE findings (2026-05-30)

Static-analysis Phase 1 of the Royal Seed check-loss investigation. Done
via the ghidra-mcp-http bridge against the Wonder project (main.nso
v1.0.0). Investigates four hypotheses for why granting a Royal Seed
container-B bool from AP suppresses the natural PALACE_CLEAR PlayReport.

See [docs/handoff.md](handoff.md) and the user's plan in the originating
prompt for context.

## Bottom line

**Hypothesis 1 (world-map UI gate) is architecturally confirmed.** The
seed bool IS the canonical "palace cleared" flag — there is no separate
"world unlocked" vs "palace cleared" distinction in the data model. The
world-map node's "is cleared" check resolves the node's flag hash via
the `GetCourseFlagFromCoursePointInfo` Nerve, and for a palace node that
hash IS the Royal Seed hash. Granting the seed bool therefore makes the
world-map render the palace as cleared, which is what removes the
player's incentive to re-enter.

**Hypothesis 4 (SetCourseClearFlag Nerve early-exits on already-cleared)
is ruled out.** The Nerve at NSO+0x1bf28cc reads per-course gameplay
flags (touch-goal-top, goal_id, plus three previously-unknown hashes),
NOT the Royal Seed bool. Setting the seed bool from AP does not gate
this Nerve.

**Hypotheses 2 (PlayReport gated on first-clear) and 3 (palace
short-circuits boss fight) cannot be conclusively confirmed or ruled
out from static analysis alone**, but become moot if the player never
re-enters the palace (which is what hypothesis 1 implies).

**Recommended near-term action**: ship the bridge-side auto-resolve
workaround (the current PR). A natural fix that lets the player
actually play the palace requires non-trivial Switch-side hooks; the
data model does not support a clean field-level fix.

## What was investigated

### 1. The 29 call sites of the container-B bool writer (`FUN_710049EA24`)

`get_function_callers` + `get_assembly_context` over all writer call
sites. The hash argument (w2) at each call site:

| Class | Count | Notes |
|---|---|---|
| Constant hash (movz + movk pair) | 11 | None of them load `0x55815859` (W1 seed) or any other Royal Seed hash. |
| Dynamic hash (loaded from struct field or local var) | 18 | The natural seed-grant path lives here — code paths driven by tables/Nerves that supply the hash at runtime. |

Hashes that DO appear as constants in writer call sites (none are seed
bools):

```
0xfd393625 (FUN_710049e5c4, FUN_7101a6b2c0 — appears at writer-wrapper)
0xe237fbc6 (FUN_7101455f40)
0xc5e1cbe9 (FUN_7101b5ade0)
0xb9bd745d (FUN_7101a69558)
0x1faf41e5 (FUN_7101a695cc)
0x9fd4fe00 (FUN_71015d6be8)
0xf760d763 (FUN_710064fed0)
0x45de6332 (FUN_7101a6b5f0)
0x972d769d (FUN_7101a6b780)
0x0d5de3d5 (FUN_7101c4f780)
```

**Conclusion**: the natural seed-grant code is data-driven — the hash is
looked up from a table or struct, not hardcoded per-world. Probably a
"course completion → grant flag" dispatch table keyed by the course's
identity.

### 2. The 6 Royal Seed hashes form a structured table in .rodata

Located at `0x71029f0bf4` (W1) through `0x71029f0e94` (W6), strides:

| World | Address | Delta from previous |
|---|---|---|
| W1 | 0x71029f0bf4 | — |
| W2 | 0x71029f0cd4 | 0xE0 (56 entries) |
| W3 | 0x71029f0d44 | 0x70 (28 entries) |
| W4 | 0x71029f0db4 | 0x70 (28 entries) |
| W5 | 0x71029f0e24 | 0x70 (28 entries) |
| W6 | 0x71029f0e94 | 0x70 (28 entries) |

W1 has a larger section (56 entries) — likely tutorial/intro flags
specific to W1 progression. Each subsequent world has 28 hashes,
consistent with a per-world flag manifest. The seed bool is one entry
of many in each world's section.

The table is referenced via `adrp` from somewhere; the specific xref
chain wasn't fully resolved (only one direct page-base xref found at
`0x7100747790`, which is data, not a function).

### 3. The 42 call sites of the container-B bool reader (`FUN_71003838AC`)

`get_function_callers` returned 32 functions, expanded to 42 individual
call sites. Constant-hash readers in the binary (`movz #LO; movk #HI,
lsl #16` pattern):

| Hash | Reader location | Identified? |
|---|---|---|
| `0x000bcbd7` | `FUN_7100935c30`, `FUN_7101b6fc8c` | unknown |
| `0x221bb0e6` | `FUN_71015f5bcc` | unknown |
| `0x2feb2c85` | `FUN_7101c5f50c`, `FUN_7101c4a08c` | unknown |
| `0x30bdd45c` | `FUN_71016cbf58` | unknown |
| `0x32ff5534` | `FUN_71004591ec` | unknown |
| `0x46d90c82` | `FUN_7100383418` | unknown |
| `0x4278994a` | `FUN_7101be011c` | unknown |
| **`0x5d3ec9b4`** | `FUN_7100689880` | **COMPLETE_GAME** ✓ |
| `0x6eabbad1` | `FUN_71007b5494` | unknown |
| `0x84912cd0` | `FUN_7101bf28cc` (SetCourseClearFlag) | new — per-course flag |
| `0x859d1259` | `FUN_71007b5494` | unknown |
| **`0x89f1cc52`** | `FUN_7101c6413c`, `FUN_7101c6347c` | **INTRO_CUTSCENE_COMPLETED** ✓ |
| `0x96da16c3` | `FUN_7101b5bcd0` | unknown |
| `0x9881f8d9` | `FUN_7101bf28cc` (SetCourseClearFlag) | new — per-course flag |
| `0x9a1e0d84` | `FUN_7101aaebe0`, `FUN_7101aaf73c` | unknown |
| `0xacd56da2` | `FUN_7101aabe00` | unknown |
| `0xb003b5f0` | `FUN_7100377280`, `FUN_7100935c80` | unknown |
| `0xb569d6fd` | `FUN_7101bf28cc` (SetCourseClearFlag) | new — per-course flag |
| `0xe05b4f08` | `FUN_7101bf5f8c` | unknown |
| `0xe06de571` | `FUN_7100643660`, `FUN_710095b138` | unknown |
| **`0xed817774`** | `FUN_7101bf28cc`, `FUN_7101a5d9a0` | touch_goal_top_result (per-course) ✓ |
| `0xf9e08f2d` | `FUN_71006a8980` | unknown |
| **`0xf79bcbb0`** | (used in `FUN_7101bf28cc` via counter reader at `0x710012ae94`) | goal_id (per-course) ✓ |

**Important: not a single constant-hash reader queries `0x55815859`** or
any other Royal Seed hash. Like the writer, the seed-bool read path is
data-driven (caller passes the hash, hash comes from struct field or
table).

Dynamic-hash readers (hash supplied by caller, candidates for the
world-map gate code path):

- `FUN_7100597c90`, `FUN_7100606f80` — wrapper-style "given a hash,
  return the bool"
- `FUN_7100831c98` — `mov w2, [...]` from earlier code
- `FUN_7101b5c600`, `FUN_7101b5c628`
- **`FUN_7101c24140`** — `ldr w2, [x0, #0x3c]` — reads the hash from
  field +0x3c of an input struct. **This pattern matches "look up the
  hash for a course-point info struct, then query its bool".**
- `FUN_7101c5f50c`, `FUN_7101c4a08c` — both load same hash literal
  `0x2feb2c85` into w23 then call reader; not seed.
- `FUN_7101f2c5e4` — hash from `[x8, #0x4]`
- Standalone caller at `71002ee6c8`

### 4. The world-map gate: `GetCourseFlagFromCoursePointInfo` Nerve

Discovered through the string `GetCourseFlagFromCoursePointInfo` at
`0x71028ae742`:

- Name-getter function: `FUN_710178d988` (returns the string).
- Nerve vtable: `0x710340add0`.
- Vtable slot 8 (execute): `FUN_710178d9a0` (a thin wrapper that calls
  the body and advances the Nerve state at this+0x18).
- Nerve body: `FUN_710178d9dc` — walks a course-point-info struct and
  returns the appropriate flag info. Callees include
  `FUN_7100934c70` (which calls the container-A reader
  `FUN_710012ae94`) and `FUN_7101b5c5b0`.

Also found these correlated strings indicating this is the world-map
gate machinery:

- `WorldMap_OpenCoursePointGate_Failure` at `0x71028b480b` — log message
  from the gate's failure path (referenced in `FUN_7100383418` which is
  itself a bool-reader caller — likely the gate's helper).
- `CoursePoint_OpenYellow`, `CoursePoint_WhiteToYellow` — visual state
  transitions for course-point nodes (yellow = cleared).
- `WorldMapDemoCoursePointLockSelector` — lock-state UI logic.
- `IsExistCoursePointByCourseId`, `GetCoursePointInfoByCourseId` — node
  lookup APIs.

**Confirms**: the world-map UI uses the Nerve dispatch to resolve a
course-point's flag hash, then queries the bool reader to determine
"cleared or not". For palace nodes, the resolved flag IS the Royal Seed
bool. Granting the seed therefore flips the world map to "palace
cleared" state.

### 5. SetCourseClearFlagToGameData Nerve at `FUN_7101bf28cc`

This Nerve (hooked by our `SetCourseClearFlagExecute::InstallAtOffset(0x1bf28cc)`
trampoline in main.cpp) reads 5 hashes via the bool reader plus one via
the counter reader:

| Hash | Reader | Note |
|---|---|---|
| `0xdf82e9ab` | (constant load early) | "current course" hash (already in CLAUDE.md) |
| `0xed817774` | bool reader | touch_goal_top_result (known) |
| `0xf79bcbb0` | counter reader | goal_id (known) |
| `0x84912cd0` | bool reader | **new** — per-course bool |
| `0x9881f8d9` | bool reader | **new** — per-course bool |
| `0xb569d6fd` | bool reader | **new** — per-course bool |

None of these are seed bools. **AP-granted seed bool would NOT cause an
early-exit in this Nerve.** If the player did somehow re-enter and
clear a palace, the Nerve would fire normally and the PALACE_CLEAR
PlayReport would emit.

This rules out hypothesis 4.

## What this means for the fix

### The data model

```
World map node (e.g. W1 palace)
  └── CoursePointInfo struct
        └── flag hash field (+0x3c?) = 0x55815859 (Royal Seed W1)
              └── boolean state in container-B (gmd+0x8 substruct)
```

A single bool serves two semantic purposes:
1. **"Player has the Royal Seed"** (used by AP-grant code, save data,
   progression counters)
2. **"Palace is cleared"** (used by world-map UI to gate node visuals
   and interactivity)

There is no observable separation in the binary between these two
roles. Setting either one sets both.

### Implications for the three fix options the user listed

- **(a) grant via a different "world unlocked" field**: no evidence
  such a field exists. The seed bool IS the world-cleared flag.
- **(b) blank the palace clear flag**: the palace clear flag is the
  seed bool. Can't blank without losing AP state.
- **(c) hook the world-map gate**: technically viable but invasive —
  would need to hook `FUN_710178d9dc` (or one of its callees) to
  return a "pseudo" hash for palace nodes whenever AP has granted but
  the player hasn't yet physically cleared, then track the
  pseudo-bool's lifecycle separately. Adds Switch-side state and a
  long-lived hook on a hot path.

### Additional option not in the user's list

- **(f) deferred grant**: hold AP-granted Royal Seed items on the
  bridge, only commit them to the Switch save after the natural
  PALACE_CLEAR fires. But this is essentially "don't give the player
  the seed until they clear the palace", which contradicts the AP
  invariant that "received item == have it now".

### Recommendation

**The current PR's bridge-side auto-resolve is the correct architectural
choice.** The unfortunate gameplay consequence (player never plays the
palace) is unavoidable given the data model. Removing this consequence
requires either:

- A long-lived Switch-side hook on the course-flag Nerve that maintains
  a separate "AP-pending" mask of palace-node states, with care taken
  to not desync with save serialization and the badge/coin grant paths.
- A pre-emptive design conversation about whether to make Royal Seeds a
  "deferred reward" item that surfaces only after natural clear, which
  would be an AP world-data change.

Neither belongs in the current PR. The bridge-side auto-resolve
unblocks gameplay; this RE work documents why the natural path is
blocked and what it would cost to fix it.

## Loose ends / follow-up RE

1. **Identify what `0x46d90c82` reads in `FUN_7100383418`** — that
   function references `WorldMap_OpenCoursePointGate_Failure` and is
   probably the actual gate check. The hash it reads might be a
   per-node interactivity flag distinct from the seed bool (which would
   reopen option (a)). Worth a dedicated decompile pass.
2. **Decompile `FUN_710178d9dc` end-to-end** to confirm the precise
   dispatch from CoursePointInfo to flag hash. The current evidence is
   architectural; a clean decompile would let us name the struct fields
   and identify per-node-type vs shared dispatch.
3. **Identify the natural seed-grant code path** (one of the 18
   dynamic-hash writer callers). Probably tied to the RequestEventGetGrandSeed
   Nerve at vtable `0x7103345920` (slot 7 override `FUN_7101563800`,
   slot 8 execute `FUN_7100559f7c` — confirms this is an active Nerve
   using the shared one-shot helper). Worth tracing if we ever want to
   intercept "got Royal Seed" events.
4. **Dynamic test (Phase 0 in the user's plan)**: grant W1 seed via AP
   on a fresh save, try to enter the palace, observe whether the node
   refuses input or whether the palace short-circuits its boss fight.
   Static analysis strongly suggests the former.

## Methodology notes for next RE session

- The ghidra-mcp-http bridge worked well for `get_function_callers`,
  `get_xrefs_to`, `get_assembly_context`, and `read_memory`.
  `decompile_function` and `search_instructions` both time out reliably
  on this binary — avoid them.
- `run_script_inline` is gated behind `GHIDRA_MCP_ALLOW_SCRIPTS=1`,
  which is not currently set. Setting it would let us bulk-scan for
  hash-load patterns across the whole binary in one shot.
- ARM64 hash literals load as `movz wN, #LO; movk wN, #HI, lsl #16` —
  byte search for `LO HI` won't find them because they're split across
  two instructions with the destination register in the low 5 bits.
  Either set GHIDRA_MCP_ALLOW_SCRIPTS=1 or use bulk
  `get_assembly_context` over xref source addresses (much cheaper than
  decompiling each caller).
