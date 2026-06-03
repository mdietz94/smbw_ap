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

## Risks / open questions
- **Course-node population may need more than container bools** — if setting the
  bool/bitfield hashes doesn't make course nodes appear, the world-map nodes likely
  live in the trailing per-course records (container-D / the 0xbf0+ region, partly
  un-RE'd — see `docs/handoff-2026-05-29-ws-persistence.md`). RE that region next.
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
