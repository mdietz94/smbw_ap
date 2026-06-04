# Handoff: open-world in-game world/course unlock (shippable mechanism)

**Branch:** `claude/sleepy-mccarthy-a25a07` (open-world WIP — NOT yet a PR).
**Sibling PR already up:** [#105](https://github.com/mdietz94/smbw_ap/pull/105)
(imgui-overlay cmdbuf crash fix — independent, already merged-ready).

## Where open-world stands

The open-world feature is built and deployed end-to-end:

- **apworld** (`hooks/Options.py`, `__init__.py:generate_early`, `open_world.py`,
  `hooks/World.py`): `open_world` / `open_world_count` / `palaces_required`
  options; random N-world selection; in-memory region restructure (active worlds
  hang off `Manual`, Bowser gated on active Royal Seeds); content exclusion;
  slot_data export. 685 Python tests pass; `tests/test_open_world_gen.py` covers it.
- **client** (`client/context.py`, `lan_server.py`, `wire.py`): reads
  `open_world_active` + `palaces_required`, sends `SetRoutableWorldsAbsolute`
  (routable-world mask) at connect + on every ReceivedItems/HelloMsg/tick; at the
  palace threshold sends all Royal Seeds + the Castle bit.
- **switch-mod** (`src/main.cpp` `worldRoutableHook`, `ap/ApProtocol*`,
  `ap/ApFrameBridge*`, `ap/ApClient.cpp`): `InboundKind::SetRoutableWorldsAbsolute=14`;
  caches `g_routable_world_mask`; trampoline on `FUN_7100935ce0` (+0x935ce0) forces
  the per-world routability predicate true for masked worlds.

**Confirmed working in Ryujinx (log 2026-06-03_14-23-51):** hook installs, client
sends `mask=0x002` (W2), drainInbound caches it, and the hook fires
(`WorldRoutable force-true world_val=3 bit=1 orig=0`). Tabbing to W2 makes the W2
**tab appear**.

## The problem this spike solves

Forcing `FUN_7100935ce0` true only adds the world to the travel **list** — its
**courses don't appear** and you can't teleport, because the predicate merely
*reads* a per-world "unlocked" flag; the world-map course-node population reads the
**real unlock/progression state**, which a fresh save doesn't have.

Decompile of `FUN_7100935ce0` (image base 0x7100000000): it hash-looks-up the
world's record in `GameDataMgr::sInstance` (`DAT_710363f0f0`) via the table at
`+0x80/+0x8c`, requires `record.vtable[+0x20]() >= 1`, and returns
`*(*(record+0x28)) & 1`. We override the return; we do **not** set the underlying
state, and the course system needs the underlying state.

## Chosen shippable approach — runtime unlock via gmd writes (NO save shipped)

A planted 100% save (manual test) proves worlds/courses become fully accessible.
But we can't distribute Nintendo save data. Instead: **diff a 100% save against a
fresh save to enumerate the unlock state as a table of `(hash → value)` writes,
then replay the relevant subset at runtime via the mod's existing gmd container
writers** — exactly the pattern already used for badges/seeds. The table is just
schema hashes + small values (mostly 1), not copyrighted content, so it's
shippable (bake it into the mod or the apworld and send/apply it).

### Tractability — already validated
`scripts/savediff.py <fresh> <100pct>` prints per-`(hash,value)` pair changes +
trailing-region byte changes. Fresh→100% is ~hundreds of `0 → 1` bool pairs
(container-B), incl. recognizable Royal-Seed hashes (`0x49abba86` W2, `0x1dcf7f6e`
W4, `0xd4660d2b` W6 …), plus trailing per-course/badge byte changes (container-C/D).
So the unlock state is enumerable and writable with primitives that already exist:
`probe::grantContainerACounter / grantContainerBBool / setContainerCBit /
setPerCourseBitfieldAbsolute`.

### Reference data on disk (user's machine)
- 100% save: `C:\Users\maxwe\Desktop\100%SMW\game_data.sav` (+ the 3 sibling .sav).
- A near-fresh save: `C:\Users\maxwe\Desktop\smbw-save-backup-20260603-144511\0\game_data.sav`
  (just-started open-world game). **Better:** start a brand-new file (only W1
  unlocked) and capture it for the cleanest diff.
- Other captures: `Desktop\{pre-badge,post-badge,pre_grant}\…\game_data.sav`.
- Ryujinx live save dir: `%APPDATA%\Ryujinx\bis\user\save\0000000000000002\{0,1}\`.
  (The 100% save is currently planted there for manual testing; backup as above.)

## Tasks

1. **Capture the canonical delta.** Start a brand-new SMBW save (only W1), capture
   `game_data.sav`. `savediff.py <fresh> <100pct>` → full `(hash, before, after)`
   pair list + trailing byte diffs. Persist as a data table (e.g.
   `apworld/.../client/world_unlock_table.py` or a JSON the mod bakes in).

2. **Categorize the hashes.** Bucket the changed hashes: world-unlock / per-course
   "exists/discovered" / per-course clear / Royal Seeds / badges / Wonder-Seed
   counts / stats+playtime / `COMPLETE_GAME` / game-goal. Cross-reference known
   hashes: `royal_seed_table.ROYAL_SEED_HASHES`, the container-B bool list in
   `ApFrameBridge.hpp` (`isBoolHash`), the Wonder-Seed mirror hashes, the
   `FUN_7100935ce0` record. Use the **smbw-save-data** skill + `savediff.py` field
   notes. Goal: find the **minimal subset that makes worlds + course nodes appear
   and be travelable**.

3. **Decide the exclude-list** (state AP must own, not the unlock): Royal Seeds
   (AP-gated → Bowser), `COMPLETE_GAME`/game-goal, and likely per-course **clear**
   flags (so PlayReport checks still fire on first clear). Keep world-unlock +
   course-exists/discovered. This is the crux experiment — iterate on hardware.

4. **Implement the runtime writer.** New `probe::applyWorldUnlock(...)` (or a
   batch driver) that writes the unlock subset via the existing container writers,
   **spread across NerveActivateOnce ticks** to respect backpressure
   (`Gmd.hpp` thresholds) and the scene-transition gate — do NOT dump hundreds of
   writes in one tick. Apply once after `isSaveLoaded()`, idempotent. Gate to
   open-world (only when `g_routable_world_mask != 0`), and scope to active worlds
   if per-world hashes are separable (else unlock all + rely on AP logic + the
   level-entry death-gate to restrict).

5. **Wire it.** Either a new `InboundKind::ApplyWorldUnlock` the client sends at
   connect when `open_world` (preferred — keeps the table apworld-side and
   shippable in `smbwonder.apworld`), or auto-apply on the Switch from a baked-in
   table when the routable mask arrives. Reconcile with: the `worldRoutableHook`
   (may become redundant — keep or drop), the Wonder-Seed override (will gate
   course *entry* by AP count — intended), badge authority (clobbers to AP mask),
   and the level-entry death-gate (#103/#104, enforces "no Bowser before palaces"
   even though unlock state grants physical access).

6. **Verify (Ryujinx, smbw-build-deploy skill).** Build overlay-on (lib/imgui must
   be checked out — overlay is now mandatory, see [#105]). Fresh save + open-world
   seed: active world's courses appear + are enterable; clears fire PlayReport
   checks; death-gate still blocks premature Bowser; goal works after palaces.
   Tail `[smbwap` log for the new `[unlock]` lines + backpressure warnings.

## ⚑ STATIC-RE FINDING (2026-06-03, confirmed) — read this first

The container-B bool replay (85 hashes) does NOT make courses appear, and Ghidra
RE explains why:

- `FUN_7100935ce0` gates a world on its per-world record's `vtable[+0x20]() >= 1`
  — a **count** of the world's content — AND `*(*(record+0x28)) & 1`. The
  world-map course-node UI reads the same per-world record. A fresh world's count
  is 0, so **no course nodes are drawn** regardless of any bool.
- `savediff.py fresh→100%` breakdown: **~85 pair-region bool flips + ~10 stat
  counters, but 736 trailing-region runs (file offset 0xbf0+).** The trailing
  region is the bulk and holds the **per-course records** (per-course structs with
  embedded course-name strings) that populate the count and the map nodes.
- => The unlock state lives in the **trailing per-course region**, NOT the
  pair-region containers A/B. The agent replayed the wrong (small) layer.

**Refined direction:** write the per-course records, not bools. The mod ALREADY
has the container-D per-course writer (`probe::setPerCourseBitfieldAbsolute` →
`FUN_7101F2B354`, gmd `+0x788` deferred / `+0x800` live; `PerCourse.cpp` +
`SeedTrace.cpp::pushWonderSeedContainerDCounts` already encode per-world counts as
container-D bitmasks). The gap is the **course-name → course-index mapping**
(Murmur3 `FUN_71003D4110`) so we know which `(hash, course_index, bitmask)` writes
correspond to "this world's courses exist/unlocked". Use the 100% save's
trailing region as ground truth: map its changed per-course runs to container-D
writes, replay them for the active world(s), verify course nodes appear.

**Alternative if trailing-region writes prove too gnarly:** pivot the "teleport"
to a **direct stage-warp** — find SMBW's load-course-by-stage-key routine and warp
the player straight into a course, bypassing the world-map unlock entirely. This
sidesteps the per-course-record problem but is its own RE (find + safely call the
warp). May be the more pragmatic shippable path.

## ⚑ PATH A — concrete recipe + RE findings (2026-06-03, second Ghidra pass)

Decided direction: write the per-course / route records via the existing
container writer; do NOT ship a save. Key functions nailed down:

- **Course-index resolver `FUN_71003D4110(murmur3_name_hash, &out_index)`**
  (NSO +0x3D4110): there are exactly **81 courses** (loop 0..0x50). A static array
  of course-name string pointers lives at **`PTR_s_Course1_71034dec90`** (81 ptrs).
  `course_index` = the array slot **0..80**; the key is the **Murmur3 hash of the
  course's internal name**. So container writes are keyed by
  `(course_type_hash, course_index 0..80)`.
- **Write target is the FlowerLock container @ gmd+0x800** (world-map ROUTES). The
  reader the mod calls "container-D per-course" — `FUN_71000E258C` — is now
  labeled `FlowerLockBitfieldReader_gmd800` in the shared Ghidra DB (the
  `claude/elastic-hopper-94525d` agent is RE'ing this same container — COORDINATE).
  It hash-probes gmd+0x800 (cap gmd+0x80c), data array gmd+0x7f8 (slot*0x40+0x28),
  falls back to gmd+0x788 (`FUN_71000e26a8`). `probe::setPerCourseBitfieldAbsolute`
  (writer `FUN_7101F2B354` @ +0x1F2B354 — note: not a *defined* function in the
  DB, but the live mod calls it fine) writes into this container.
- The teleport UI lists, per world, the **courses you've reached**; a fresh world's
  list is empty (→ "no courses, can't teleport"). The 100% save has them all.

**Recipe to implement & test:**
1. Enumerate the 81 course names from `PTR_s_Course1_71034dec90` → map
   `course_index 0..80` → world (which indices belong to W1..W6/PI/Special).
2. Diff the 100% save's trailing region vs fresh, per course, to get the target
   `(course_type_hash, course_index, bitmask)` writes that mark a course
   reached/route-open (the per-course-type hashes incl. CourseClear/Normal @ file
   0x43F0, GoalSeed @ 0x3348, CourseClear/Badge @ 0x4438, and the FlowerLock
   route hash(es) @ gmd+0x800 — confirm which one drives the teleport list / node
   visibility, likely FlowerLock).
3. Replay those via `setPerCourseBitfieldAbsolute` for the **active** world's
   course indices only (batch across ticks; respect `checkContainerD()`
   backpressure — already built into the writer).
4. Verify in Ryujinx (FRESH save + open-world seed): active world's course nodes
   appear + teleportable; clears fire AP checks; death-gate still gates Bowser.

Open sub-question to pin first: WHICH bit/hash makes a course show in the teleport
list — the FlowerLock route bit (gmd+0x800) or a CourseClear/visited flag. Bisect
with one course: write only the FlowerLock bit for one W-active course, see if it
appears; if not, add the CourseClear/GoalSeed bits.

## Risks / open questions
- **Course-node population needs the trailing per-course region** (CONFIRMED above),
  which is only partly RE'd — see `docs/handoff-2026-05-29-ws-persistence.md` and
  the container-D code in `switch-mod/src/probe/PerCourse.cpp` / `SeedTrace.cpp`.
- **Batch-write safety**: hundreds of deferred-queue writes can hit backpressure /
  race scene transitions → game Abort. Spread + gate.
- **100% save has Bowser/everything cleared** — exclude those flags or Bowser
  goal/world-map state will look done.
- A truly clean diff needs a brand-new file as the "before" (the current backup may
  have a little progress).

## Key files
- `scripts/savediff.py` (+ `.claude/skills/smbw-save-data/` reference) — the diff/format.
- `switch-mod/src/probe/{ContainerA,ContainerB,ContainerC,PerCourse}.cpp` — writers.
- `switch-mod/src/ap/{ApProtocol.hpp/.cpp,ApFrameBridge.cpp,ApClient.cpp}` — wire opcode.
- `switch-mod/src/main.cpp` — `worldRoutableHook`, drain dispatch, tick.
- `apworld/smbw_archipelago/client/{context.py,lan_server.py,wire.py}` + new table module.
- `FUN_7100935ce0` @ NSO +0x935ce0 (record flag `*(*(record+0x28)) & 1`).
- Memories: `[[smbwap-open-world-mode]]`, `[[smbwap-flowerlock-route-gates]]`
  (inter-world routes may be RomFS/BYML — watch for this wall),
  `[[smbwap-wonder-seed-gate-solved]]`, `[[smbwap-debug-overlay-mandatory]]`.
