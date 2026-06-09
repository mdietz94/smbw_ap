# RE: the "Grand Propeller Flower" world-reveal demos (W3-end W4/5/6 unlock) — 2026-06-08

Static-only Ghidra pass on `main.nso` (image base `0x7100000000`). This identifies the
**actual** story-beat the user wants ("the demo at the end of W3 that unlocks W4/5/6"),
which prior docs missed — they only RE'd the per-world `FirstVisitDemo`. See plan
`logical-beaming-hartmanis.md`. **This is distinct from `FirstVisitDemo`.**

## The mechanism (confirmed)

The world-map "ride the propeller flower → new island cluster rises" cutscenes are a
**course-point demo**, data-driven from RomFS BYML, dispatched by NSO code.

- **Trigger condition enum** — `FUN_71009281bc` (NSO `+0x9281bc`) parses a
  CoursePointInfo `ConditionType` string → enum. Full table:
  `0 (empty) · 1 ClearAppointedCourse · 2 PowerJewel · 3 ClearLinkedCourse ·
   4 OpenAppointedCourse · 5 GrandPropellerFlowerDemo · 6 ComingOutNextGoTo ·
   7 ClearAppointedCourseMulti · 8 GoldenPropellerFlower · 9 GetMedalFrom1To4 ·
   10 FullCompAppointedCourse · 11 FirstVisitDemo`.
  The reveal we want is **`GrandPropellerFlowerDemo` (5)**.

- **CoursePointInfo reflection/BYML field table** — `FUN_7101b21f54` (NSO `+0x1b21f54`)
  enumerates every CoursePointInfo field. Reveal-relevant ones:
  `ConditionType, WorldKind, WorldNo_, GoalID, GateId, PropellerFlowerId,
   NpcId, NextGoToDokanId, DummyPointId, GrandPropellerFlowerDemoType,
   IsSameTimingOpen, IsRailDrawDoneImmediately, SameTimingOpenCoursePriority,
   LockedCourseDisplayType`.
  ⟹ **The reveal node and its parameters live in RomFS BYML, not NSO.** NSO holds only
  the parser, the field accessors, and the demo executor.

- **`GrandPropellerFlowerDemoType` enum** — string→value parser `FUN_7100581b0c`
  (NSO `+0x581b0c`); field accessor `FUN_71017958a8` (NSO `+0x17958a8`, walks an
  inheritance chain, storage at `+0x70` of the resolved CoursePointInfo). Values are
  the per-world-cluster reveals, named by internal world codename:

  | value | name | world (inferred) |
  |---|---|---|
  | -1 | Invalid | — |
  | 0 | AfterSavanna | W1 Pipe-Rock Plateau |
  | 1 | AfterYama | W2 Fluff-Puff Peaks (Yama = mountain ✓) |
  | **2** | **AfterWa** | **W3 Shining Falls → THE W3-END REVEAL** |
  | 3 | AfterSabaku | W4 Sunbaked Desert (Sabaku = desert ✓) |
  | 4 | AfterKin | W5 Fungi Mines (Kin = Kinoko/mushroom) |
  | 5 | AfterNettai | W6 Deep Magma Bog (Nettai = tropical) |
  | **6** | **ToCastle** | **final reveal → opens Bowser's Castle path** |

  Codename→world for `Sabaku`(W4) and `Yama`(W2) are certain; `Savanna/Wa/Kin/Nettai`
  are inferred from theme + enum ordering. **Confirm `AfterWa = W3` via Track A's
  save-diff** (or the WorldKind enum codec `PTR_FUN_7103490e48`).

- **`ToCastle` (6) is independently useful**: it's the reveal that opens the Bowser
  path — relevant to the open_world `palaces_required` → Bowser gate
  (`context.py` `_bowser_opened` latch + `kCastleMaskBit`).

## World-map reveal sequences (the visible effect)

The demo plays named World-Map SEL/animation sequences. Examples for the desert cluster
(`Sabaku`): `WM_SabakuCoursePointEmerge` (course points rise), `WM_WhileSabakuDoor` /
`WM_SabakuDoorFin` (door/gate opens), `WM_PLStepSabakuSwitch`. These live in a sequence
table at data `0x71034754e0`. Related actors/params: gparam
`game__actor__gparam__WorldMapPropellerFlower` (`0x71029104a0`), enum
`WorldMapPropellerFlowerId` (`0x71028f6dc1`), `WorldMapPropellerFlowerTable`
(`0x71028cfd2e`), `CheckWorldMapPropellerFlowerContactRumble` (`0x7102928ff8` — player
contacts the flower → triggers).

## ★ THE GATE — SOLVED (the route is predicate-derived, not a one-time animation)

`ProcessWorldMapRouteGate` (NSO `+0x377280`, named) re-evaluates every world-map route
**on each world-map (re)load**, switching on the route's `ConditionType`. For
`ConditionType == 5` (GrandPropellerFlowerDemo):

```c
case 5:
  demoType = *FUN_71017958a8(coursePointInfo);   // GrandPropellerFlowerDemoType (0..6)
  routeOpen = FUN_7101b70610(demoType);
```

And the gate predicate is trivial:

```c
bool FUN_7101b70610(int demoType) {            // NSO +0x1b70610
  if (demoType == -1) return false;
  char out = 0;
  PerCourseBitReader(gmd, &out, 0x35bf61af, demoType);   // container-C bitfield read
  return out != 0;
}
```

`PerCourseBitReader` (NSO `+0x124134`) is the **container-C bitfield** reader (same
bucket layout as badges `0x105df820` / wonder-seeds `0x60458608`: cap `gmd+0x70`,
objects `gmd+0x78`, buckets `gmd+0x80`, count `gmd+0x8c`, data `obj+0x28`; bit read =
`data[idx>>5] >> (idx&31) & 1`).

**⟹ The W4/5/6 roads are gated on one container-C bitfield: hash `0x35bf61af`, bit index
= GrandPropellerFlowerDemoType.** Because `ProcessWorldMapRouteGate` re-runs on every
map load, **setting the bit both marks the reveal done AND rebuilds the roads** — no
need to play the demo animation. This resolves the "flag just marks it done" worry.

| set bit | opens |
|---|---|
| **2 (AfterWa)** | **post-W3 routes → W4/5/6 cluster** (the target) |
| 6 (ToCastle) | Bowser's Castle path (also serves open_world palaces→Bowser) |
| 0,1,3,4,5 | the other per-world reveals |

**Implementation lever:** `setContainerCBit(0x35bf61af, bit, 1)` (ContainerC.cpp) —
container-C persists to disk, bits 0–6 are well within the persistent low range, and the
set is idempotent → drop it straight into the `ApplyWorldUnlock`/replay channel, gated on
open_world. Confirm `bit 2 = W3` empirically via Track A's S0→S1 diff (this bitfield's
bit 2 should flip 0→1). Caveat: the per-world FirstVisitDemo NPC-obstacle (hash
`0xb003b5f0` byte, handled by existing `courseVisibleHook`) is a *separate* concern from
this route-cluster gate.

Note: there are two route-gate sites — `ProcessWorldMapRouteGate` (`+0x377280`) and
`FUN_71005840f0`; both go through the same `ConditionType` switch and both reach
`FUN_71017958a8`/`RouteFlowerLockChecker`, so the bit governs both.

## ★ LIVE TEST RESULT (2026-06-08): node appears, road does NOT

Setting `setContainerCBit(0x35bf61af, 2, true)` on entry to Petal Isles made the
**W4 destination node emerge** (a small scene played) — confirming the bit drives the
`GrandPropellerFlowerDemo` route element (case 5) — **but the connecting road did not
draw.** So the road is a *separate* route element with its own gate; the node-bit is
necessary but not sufficient.

**The road gate is a global story-phase pointer, not a bit.** `ProcessWorldMapRouteGate`
case 6 (`ComingOutNextGoTo`) → `FUN_7101b71210`:

```c
read stage = container[0xecea4196];      // FUN_7100221128, container @ gmd+0x260/+0x2c0
phase = mapStageToPhase(stage);          // FUN_7101b71284 (binary-search DAT_710361fb60)
return route.expectedPhase != 0 && phase != 0 && route.expectedPhase == phase;  // EXACT
```

Key consequences:
- `0xecea4196` is a **single global story-progression stage** (enum-typed container at
  `gmd+0x260`, distinct from containers A/B/C/D). 0x15 (21) is a "none/final" sentinel.
- The gate is **exact-match on phase**, so `ComingOutNextGoTo` roads are *transient*
  "current-objective" highlights — the permanent W3→W4 road may be yet another element.
- ⟹ **Jamming the stage "as late as possible" is risky**: one global exact-match pointer;
  a late value can skip the phase a given road wants and ripple into dialogue/other
  worlds. The correct value must come from a real save.
- The hash `0xecea4196` is loaded via instruction immediates (no rodata pool entry), so
  the writer isn't cheaply found statically → use the save-diff.

**Decisive next step = save-diff (Track A).** It captures the *complete* footprint the
demo writes (the `0x35bf61af` bit, the `0xecea4196` stage value, and any road/rail
flags) safely and at once. Two useful diffs:
- **fresh → existing 100% save**: reads the *terminal* `0xecea4196` value (= "as late as
  possible") + confirms all `0x35bf61af` bits — cheapest, no new playthrough.
- **pre-W3 → post-W3 boundary**: the *minimal* correct stage value + exactly which bits
  flip for this one reveal.

## ★ ROAD vs NODE — parallel-agent findings (2026-06-08, later)

The node bit is necessary but not sufficient; the road is a separate route component.
Established (Agent run, read-only):
- **`0x35bf61af` and `0xecea4196` each have exactly ONE materialization in the whole
  8.3M-instruction NSO — their READERS** (`FUN_7101b70610` and `FUN_7101b71210`). The
  hash is built `mov w2,#0x61af` + `movk w2,#0x35bf,lsl#16`. Neither appears as raw data
  (no descriptor table). ⟹ the demo's WRITERS obtain the hash by **hashing the name
  string at runtime** (`GrandPropellerFlowerDemoType` @0x71028e98ba etc.), so the writer
  is **not reachable by constant search** — it lives behind the world-map Nerve state
  machine + the BYML route loader.
- **The road/rail is almost certainly route case 6 (`ComingOutNextGoTo`)**, gated on the
  **global story-stage counter `0xecea4196`** (not another bit). To draw the W3→W4 road
  we must advance that stage to the post-W3 value.
- **The post-W3 stage value is NOT a static literal.** `FUN_7101b71210` maps the raw
  stage through a binary search over a load-time descriptor table (`DAT_710361fb60`,
  count `DAT_710361fb58`, returns `desc+4`; sentinel 0x15 = invalid). Table + saved
  value are populated at load ⟹ only a **live save (diff or live read) gives the number.**
- Each course-point node carries up to 4 route-link slots (`node+0xa0/+0xb0/+0xc0/+0xd0`).
- **Demo-type accessor `FUN_71017958a8`** returns a POINTER to the demoType storage at
  `CoursePointInfo+0x70` (gated by `+0xac` bit0 "resolved" / parent link `+0x10`). A
  **runtime write of `2` there retargets a flower's reveal to AfterWa** — but the record
  is RomFS-BYML-parsed, so it must be a runtime write after parse (NSO patch won't do it).
  The demo EXECUTOR (reads this pointer, branches on type, kicks the SEL sequence
  `WM_*CoursePointEmerge` @table 0x71034754e0) is reachable but was not fully mapped
  before the Ghidra MCP server hung under concurrent load.

**CAVEAT (unconfirmed):** case-6 is *exact-match* on phase, which is odd for *permanent*
roads (a 100% save shows all roads, yet has one final stage value). So either the
permanent road is NOT case-6 (the route-element struct mapping was blocked by the server
hang), or there's a separate persistent "rail drawn" flag. Resolve via the save-diff.

### Two viable unblock paths (pick one)
- **A — trigger the real demo (no save needed).** Restart the Ghidra MCP server; finish
  mapping the executor (xref `0x7102928ff8` contact-name + readers of `FUN_71017958a8`,
  find the one referencing seq table `0x71034754e0`); call it from the subsdk with
  demoType=2 (or runtime-write `2` to a live flower's `+0x70` then trigger). Plays the
  real cutscene → draws road + advances stage + sets node, all correctly.
- **B — save-derived (needs a post-W3 save).** Capture/read a post-W3 save; diff to read
  the exact `0xecea4196` value + confirm the road state; set node bit + story stage.

## ★ Executor hunt (2026-06-08, Ghidra restored) — leads + why it's blocked

Explored the trigger/executor path thoroughly with Ghidra back (single-threaded). Found
a **third enum — the world-map story-event/cutscene type** (string→enum parser
`FUN_7101c11c14`, codec wrapper `FUN_7101c0b66c`):
`0 Invalid · 1 GrandSeedDemoBefore · 2 GrandSeedDemoAfter · 3 GrandSeedDemoKoopaPowerUp ·
 4 KoopaSenkanDemoWorld002 · 5 KoopaSenkanDemoWorld007 · 6 KoopaMoveDemo ·
 7 OpenHatenaFenceDemo · 8 GrandSeedDemoBeforeKinkin · 9 EmotionConversation`.
These are the actual **Grand/Royal-Seed story cutscenes** — `GrandSeedDemoAfter` (2) is
the strongest W3-reveal candidate; `OpenHatenaFenceDemo` (7) opens a "fence". Related:
event request `RequestEventOpenSabakuFence` (name @0x710290eb98), enum-value strings
@0x71034c0d30…0x71034c0dee.

**Why a clean static trigger doesn't fall out:** every parser/codec/accessor in this
subsystem (`FUN_7101c11c14`, `FUN_7101c0b66c`, the GrandPropellerFlowerDemoType accessor
`FUN_71017958a8`, the contact-rumble stub, the WM sequence table 0x71034754e0) is
registered through the reflection/vtable framework and triggered by BYML course-point
data — `get_function_callers` returns "no callers" because dispatch is indirect. The
demo's persistent state-writers hash their keys at runtime (no constant trail), and the
road's story-stage value is load-time data. So neither "call an executor with demoType=2"
nor "set the exact persistent state" is cleanly achievable from NSO static RE alone, and
synthesizing the actor/Nerve context to fire it from a subsdk hook would be crash-prone.

**⟹ Decisive resolution = the save-diff** (captures exactly what the reveal writes,
low-risk). The event-demo enum above is the entry point if we later pursue a live trigger
(find the accessor for this field + its switch dispatcher via runtime breakpoint).

## ★ Story-stage container ("container-E") layout + direct-write recipe (ready primitive)

The road gate reads the story stage via `FUN_7100221128(gmd, &out, 0xecea4196)` (scalar)
/ `FUN_7100221278(gmd, &out, hash, index)` (per-index). These readers fully expose the
container, so we can write it directly (mirroring how `setContainerCBit` walks
container-C) — **no need to find the game's reflection writer**.

**Container-E (enum/typed GameData), two parallel buckets in the gmd singleton:**
- *Scalar bucket* (where `0xecea4196` lives):
  - `gmd+0x260` = bucket array base; `gmd+0x26c` = bucket count (open-addressing).
    Each entry = `{u32 key, u32 objIndex}` (8 bytes); probe slot = `key % count`,
    linear walk, terminate on `key==target` or `key==0`.
  - `gmd+0x258` = objects array base; stride **0x38**; **scalar value at obj+0x1c**.
  - `gmd+0x250` = object-count bound (clamp objIndex to 0 if ≥).
- *Array bucket* (per-index variant): `gmd+0x2c0` base / `gmd+0x2cc` count;
  objects `gmd+0x2b8`, stride **0x50**, element array at obj+0x28, per-object element
  count via vtable slot 0x20. (Not needed for the scalar story stage.)

**Direct-write recipe for the story stage** (implement as `probe::setStoryStage(value)`):
1. `gmd = gmdSingleton()`.
2. `bucket = *(gmd+0x260)`; `count = *(u32*)(gmd+0x26c)`; if either 0 → bail.
3. open-address probe `bucket` for entry `key==0xecea4196`; read `objIdx = *(u32*)(entry+4)`.
4. clamp: if `objIdx >= *(u32*)(gmd+0x250)` → 0.
5. `valuePtr = *(gmd+0x258) + objIdx*0x38 + 0x1c`; `*valuePtr = <post-W3 value>`.

`<post-W3 value>` is the one number we still need — the **raw stored stage value** (the
enum-hash that `FUN_7101b71284` maps to a phase). The save-diff supplies it: compare
`0xecea4196`'s value in S0 (pre-reveal) vs S1 (post-reveal). Then `setStoryStage(thatValue)`
+ the already-deployed `setContainerCBit(0x35bf61af, 2)` should give node **and** road on
the next world-map load.

⚠️ The story stage is a single **global** pointer — writing it advances world-map story
state broadly. Validate side effects (other worlds' paths, NPC dialogue) after setting;
if the road turns out to need a narrower per-route flag instead, the diff will show that
(a different hash/region changing) and we use that instead.

## ★★ SAVE-DIFF RESULTS (2026-06-08) — the definitive footprint

Nine boundary saves diffed (`scripts/savediff.py`). The W4/5/6-unlock cutscene
(`"w3 end"` → `"w3-post cutscene"`) writes EXACTLY:
- **7 container-A pair-region booleans 0→1**: `0x336d9b3d 0xa6e6ce4b 0x466dae6b
  0xa6ce080b 0x6a7aa43e 0x048bc39c` (these 6 are ALREADY in `WORLD_UNLOCK_HASHES`) +
  **`0x20fced8b`** (pair 270) — the ONE that was missing.
- **trailing bytes**: `0x0ced 09→0b`, `0x0d14 00→02`, `0x0e89 08→18`, and
  **`0x10a8 03→07`** = container-C `0x35bf61af` **bit 2** (the node bit).
- noise: rotating triplet `0xd32edb2d/0xa530167a/0xcde23083` + `0x50c8` (savedata UUID).

**Node bit CONFIRMED** via the analogous W2→W3 cutscene (`"pre/post w2-cloud piranha"`):
`0x10a8` there is `01→03` (bit 1 = AfterYama/W2). So `0x35bf61af` bit index = W3 demoType 2. ✓

**Story-stage hypothesis was WRONG**: `0xecea4196` did NOT change across the cutscene.
The road is not story-stage gated.

**ROOT CAUSE of "node but no road" in open-world**: `WORLD_UNLOCK_HASHES` is derived from
a fresh→100% diff, which **structurally cannot capture transient flags that are 0 at both
endpoints**. `0x20fced8b` is exactly such a reveal-phase flag (set by the cutscene, evidently
cleared by 100%), so open-world (built only from the 100% table) set the node bit + the
other 6 booleans but never `0x20fced8b` → road never drew.

**FIX deployed (2026-06-08)**: added `0x20fced8b` to `WORLD_UNLOCK_HASHES`
([client/world_unlock_table.py](apworld/smbw_archipelago/client/world_unlock_table.py)),
rebuilt + pushed the apworld. Client-only change (pushed via the existing ApplyWorldUnlock
→ `grantContainerACounter` channel; no switch-mod rebuild). **Awaiting in-game test**: node
bit (deployed) + `0x20fced8b` → does the W3→W4/5/6 road now draw on map (re)load?

**If the road still doesn't draw**, the remaining suspects are the 3 trailing bytes
(`0x0ced/0x0d14/0x0e89`) — world-map-graph records in the 0x0c00–0x0f00 save region
(likely derived from the reveal flags on load, but if not, set them via a gmd-field map or
a one-shot save patch replicating the exact post-cutscene bytes).

**Other diffs (context):** the W2/W3 *entry gates* write a separate bitfield at save
`0x1118` (W2→bit1, W3→bit2), no pair changes — unrelated to the PI reveal road. Beating the
final W3 level grants the **W3 Royal Seed `0xb550d8d6`** (pre-cutscene state; AP grants
Royal Seeds in open-world, so it's covered).

## ★★★ ROAD SOLVED + DEPLOYED (2026-06-08) — three more container-C bits

Save-patch tests settled the two unknowns:
- **Forward test** (`"w3 end"` + road bytes): the cutscene plays on load anyway (it's
  position/progress-triggered — a "pending demo" queued by beating W3's final level, which
  open-world never does → why the road never appears there).
- **Inverse test** (`"w3-post cutscene"` with road bytes REVERTED): **the road disappeared**
  ⟹ the road is **live-state-driven** — `ProcessWorldMapRouteGate` re-reads these bytes on
  every world-map load. No cutscene needed; setting the state draws the road.

The pair-region flags are NOT the road (live test: all 7 booleans incl. `0x20fced8b` +
node bit set, road absent). The road is **three world-map-graph container-C bitfields**,
recovered by mapping the changed save offsets to owning container hashes via the save's
`(hash, blob-offset)` index (records serialize as `count@+0`, bitfield-data `@+4`):

| save off | container hash | bit | meaning |
|---|---|---|---|
| `0x0ced` | `0xbcc1ef0e` | 9 | road-graph bit |
| `0x0d14` | `0x57df969b` | 1 | road-graph bit |
| `0x0e89` | `0x2309a645` | 12 | road-graph bit |
| `0x10a8` | `0x35bf61af` | 2 | node bit (already deployed) |

**DEPLOYED**: `applyOpenWorldEntry` ([switch-mod/src/probe/SeedTrace.cpp](switch-mod/src/probe/SeedTrace.cpp))
now also calls `setContainerCBit(0xbcc1ef0e,9) / (0x57df969b,1) / (0x2309a645,12)` next to the
node bits, latched once all succeed. Built + deployed to Ryujinx. **Awaiting test.**

These bits are W3-reveal-specific (open W4/5/6). GENERALIZATION for arbitrary open-world
configs: derive each world-reveal's bit-set from its cutscene diff — the W2→W3 cutscene
(`"pre/post w2-cloud piranha"`) sets DIFFERENT bits in the same hashes (e.g. `0xbcc1ef0e`
accumulates per-world), so the per-world bit map must be tabulated (have W2/W3 diffs; need
W4/5/6/Special diffs for full coverage). For now W3→W4/5/6 is wired unconditionally in
open-world.

## ★ PER-WORLD REVEAL BIT MAP (2026-06-08, W2–W6 captured)

Hash-mapped diffs of each world's wonder-seed reveal cutscene (`"wN-pre cutscene"` →
`"wN-post cutscene"`), trailing bytes → `(container-C hash : bit)` via the save's
`(hash, blob-offset)` index. **node bit = `0x35bf61af`** increments per world (the
GrandPropellerFlowerDemoType enum); **road bits** are per-reveal:

| reveal | node `0x35bf61af` | road bits (hash:bit) |
|---|---|---|
| W2 (AfterYama) | bit 1 | `0xbcc1ef0e`:2, `0xbcc1ef0e`:8, `0x09bfe967`:1 |
| **W3 (AfterWa)** ✅ deployed | bit 2 | `0xbcc1ef0e`:9, `0x57df969b`:1, `0x2309a645`:12 |
| W4 (AfterSabaku) | bit 3 | `0x9d25ce3b`:1 |
| W5 (AfterKin) | bit 4 | `0x40c00dd7`:2, `0xf5411212`:3 |
| W6 (AfterNettai) | bit 5 | `0x52781dfd`:1 |
| ToCastle (Bowser) | bit 6 | (W6 cutscene "also opens Bowser road" — likely `0x52781dfd`:1 above; bit 6 may be the node only) |

Notes:
- `0xbcc1ef0e` is a **shared accumulating** world-map-graph bitfield (W2 adds bits 2/8,
  W3 adds bit 9, the W5 battleship adds bit 6) — must OR per-world, never overwrite.
- **W4 PI wonder-seed gate** (`"w4-pre/post-castle-gate"`) sets `0x05983371`:4 — that's
  the gate-cleared flag (separate from the reveal).
- **Transient pair-region flags** (set by reveal, missed by fresh→100% diff, like W3's
  `0x20fced8b`): W6 sets `0xa23922fa` + `0xe02a5e43` — these **clear the cloud-piranha
  barrier around Bowser's Kingdom** (live-diagnosed 2026-06-08: with node+road set but
  these omitted, the barrier stayed up). Added to `WORLD_UNLOCK_HASHES`. So the cloud
  piranhas (overworld route barriers) are cleared by the reveal's transient pair flags,
  while the container-C bits draw the roads.
- `0x3d17a42a @ save 0x50c8` changes in every diff = savedata-UUID noise.

GENERALIZATION PLAN: a per-world reveal table `{world: (node_bit, [(hash,bit)...])}`; set
the node+road bits for the worlds open in the seed (or all, for full PI→W4→W5→W6
reachability). Open design Qs: full-network vs per-open-world; gate ToCastle (bit 6) +
W6 road behind the palace threshold (currently bit 6 is unconditional).

## Still open (next RE steps)

1. **Confirm `bit 2 = W3` and that the W4/5/6 PI routes use ConditionType=5.** Track A's
   S0→S1 diff should show container-C `0x35bf61af` bit 2 flip 0→1 (and nothing else
   load-bearing). If a different bit flips, remap. Also confirms the codename inference.
2. **What sets the bit during normal play** (for completeness / to be sure no extra
   sibling state is needed). The demo-completion handler writes `setContainerCBit`-style
   to `0x35bf61af`. Find it via writers of hash `0x35bf61af` (search the container-C
   writer `FUN_710...` call sites for that constant) — but this is now optional, since
   we can set the bit ourselves.
3. **Live test the pure-gmd fix.** Set `setContainerCBit(0x35bf61af, 2, 1)` on a modded
   save and reload the world map; verify the W4/5/6 routes open and are walkable. If
   anything is still blocked, check the FirstVisitDemo NPC-obstacle (`0xb003b5f0`) and
   the existing routability hooks (`FUN_7100935ce0`/`FUN_7100935c80`).
4. **Fallback (only if (3) fails): trigger the real demo.** Hijack a course-point /
   propeller-flower to invoke the AfterWa demo executor once (the user's "redirect the
   W1 flower" idea). Entry points retained for this: contact check
   `CheckWorldMapPropellerFlowerContactRumble`, sequence table `0x71034754e0`, accessor
   `GetGrandPropellerFlowerDemoTypeFromCoursePointInfo` (`0x71028a7c74`).

## Anchors

- Condition parser `FUN_71009281bc` (`+0x9281bc`); enum→string table `0x7103485c40`.
- CoursePointInfo field table `FUN_7101b21f54` (`+0x1b21f54`).
- `GrandPropellerFlowerDemoType` parser `FUN_7100581b0c` (`+0x581b0c`); accessor
  `FUN_71017958a8` (`+0x17958a8`, storage `+0x70`); value strings `0x71034c1dc8`
  (`Invalid`) … `0x71034c1e17` (`ToCastle`).
- Reflection name `GetGrandPropellerFlowerDemoTypeFromCoursePointInfo` `0x71028a7c74`.
- WorldKind enum codec vtable `PTR_FUN_7103490e48`; demo-type codec vtable `0x7103488228`.
- WM reveal sequence table `0x71034754e0`; propeller-flower gparam/enum
  `0x71029104a0 / 0x71028f6dc1 / 0x71028cfd2e`.
