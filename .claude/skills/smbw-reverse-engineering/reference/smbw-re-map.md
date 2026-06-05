# SMBW RE map — current-state reference

**This is the canonical "what is true now" ledger for SMBW v1.0.0 RE.** It is
*present-tense only*: every fact here is the current best understanding, with a
status flag. The append-only research journals
([static-analysis-findings.md](static-analysis-findings.md),
[../../smbw-save-data/reference/save-diff-findings.md](../../smbw-save-data/reference/save-diff-findings.md))
remain the "why/how we found it" archive — read them for derivation, not for
current truth (they contain superseded claims, layered corrections, and dead-ends).

**Status legend** — `CONFIRMED` = live-validated on hardware/Ryujinx ·
`HIGH-CONF` = static-only / cross-verified but untested · `ACTIVE` = RE in
progress (see the linked spike) · `CHEAT` = sourced from the HamletDuFromage cheat
DB, not independently re-derived.

When this map and a journal disagree, **this map wins** (the journal is older).
When this map and the live source (`switch-mod/src/`) disagree, the source wins —
update this map.

---

## 1. Target & address space

| Fact | Value |
|---|---|
| Game / version | Super Mario Bros. Wonder **v1.0.0** (never apply v1.0.1 — every offset is pinned) |
| Build ID | `CD6E42AEE7934F4D`, codename `Secred.nss` |
| NSO base (Ghidra + loaded) | `0x7100000000` |
| `.text` | ≈ `0x7100000000` – `0x7102800000` |
| `.rodata` strings | `0x71028XXXXX` – `0x71029XXXXX` |
| Nerve vtable regions | `0x710334XXXX`, `0x71033fXXXX`, `0x71034BXXXX` |
| Generic Nerve dtor/dispatch (every vtable `-8` slot) | `0x71000ac930` |
| **`gmd::GameDataMgr::sInstance`** (the master anchor) | NSO **`+0x0363F0F0`** — deref qword → live `GameDataMgr*`; Switch wraps as `probe::gmdSingleton()` |
| Dev-Ryujinx live↔Ghidra map | `ghidra = 0x7100000000 + (runtime − 0x08504000)` (`B_main = 0x08504000`) |

NSO and the Nintendo SDK have **independent bases**: use `installAtMainOffset` for
game code, `installAtSym<>` for SDK + game symbols. SDK symbol table:
[switch-mod/syms/100/sdk.sym](../../../../switch-mod/syms/100/sdk.sym).

---

## 2. GameDataMgr API surface

NSO-relative. The container-A **writer** signature is `(gmd, value, hash)` —
value first (an early CLAUDE.md note had it backwards).

| NSO offset | Role | Signature | Status |
|---|---|---|---|
| `+0x0049F648` | **Container-A counter WRITER** ★ | `(GameDataMgr*, u32 value, u32 hash)` | CONFIRMED |
| `+0x0012AE94` | Container-A counter READER | `(GameDataMgr*, u32* out, u32 hash)` | CONFIRMED |
| `+0x0049EA24` | **Container-B bool WRITER** (wrapper) | `(GameDataMgr*, u32 value, u32 hash)` → checks `gmd+0x68` init/lock, delegates to `+0x1F263FC` | CONFIRMED |
| `+0x1F263FC` | Container-B bool deferred-write delegate | `(gmd+8 substruct, value & 1, hash)` | CONFIRMED |
| `+0x03838AC` | Sub-bool READER (INTRO / COMPLETE_GAME reads) | `(sub_obj, u8* out, u32 hash)` | CONFIRMED |
| `+0x03D3FB0` | Stage-info hash → course-index **TRANSLATOR** (NOT a writer) | `(top_hash, u32* out_index)` | CONFIRMED |
| `+0x03D4110` | **Murmur3-32** course-name hash (81 names, seed 0) | — | CONFIRMED |
| `+0x01F27B78` | Object-pointer accessor (badge/typed-obj path) | `(GameDataMgr*, void** out_obj, u32 hash)` | HIGH-CONF |

Switch primitives that drive these live in `switch-mod/src/probe/*.cpp`
(`ContainerA.cpp`, `ContainerB.cpp`, `ContainerC.cpp`, `PerCourse.cpp`,
`SeedTrace.cpp`); hooks are installed in `switch-mod/src/main.cpp`.

---

## 3. Container model (A / B / C / D)

- **A — hash-keyed numeric counters** (coins, Wonder-Seed counts + mirrors).
  Writer `+0x0049F648`, reader `+0x0012AE94`. Lock-free (ARM exclusive-monitor
  atomics). **Deferred-write**: queues to the dirty ring at `gmd+0xf8`, drains at
  next save. Switch: `grantContainerACounter(hash,value)`,
  `incrementContainerACounter(hash,delta)`.
- **B — hash-keyed bools** (Royal Seeds, COMPLETE_GAME, INTRO). Writer
  `+0x0049EA24` → delegate `+0x1F263FC` on the `gmd+8` substruct. Same
  deferred-write/dirty-queue behavior as A. Switch: `grantContainerBBool(hash,value)`.
- **C — bitfields** at `gmd+0x70..0x8c` (badges; Wonder-Seed bitfield). Access by
  walking bucket → typed sub-object → `uint32_t[]`. Switch:
  `setBadgeBitfieldAbsolute(bits)`, `setContainerCBit(hash,bit,val)`,
  `setWonderSeedBitfieldAbsolute(lo,hi)`.
- **D — per-course / per-seed arrays** (Wonder-Seed persistence). Live writer is
  **ACTIVE** RE (see §11). Switch: `setPerCourseBitfieldAbsolute(...)`,
  `pushWonderSeedContainerDCounts()`.

**Deferred-write trap:** a save-before-flush silently drops an A/B grant. The fix
is **AP-authoritative replay** — the bridge re-pushes the canonical set on every
`ReceivedItems`, every `HelloMsg`, and a ~2 s tick (badges & Wonder-Seed counts
absolute-overwrite; Royal Seeds re-emit per seed). For instant in-game UI refresh,
**dual-write** the live-state struct too (see §9).

---

## 4. GameDataMgr struct layout (partial)

| Offset | Contents |
|---|---|
| `+0x08` | Container-B bool substruct (delegate target of `+0x1F263FC`) |
| `+0x68` | Container-B init/lock flag (checked by the `+0x0049EA24` wrapper) |
| `+0x70..0x8c` | Container-C bitfields (badges, Wonder-Seed bitfield) |
| `+0xe0` | Container-A bucket array |
| `+0xec` | Container-A bucket count |
| `+0xf0` | Container-A dirty-queue capacity |
| `+0xf8` | Container-A dirty-queue ring buffer ptr (slot stride `0xc`) |
| `+0x100` | Container-A dirty-queue head/state word (atomic) |
| `+0x128` | Container-A secondary container ("insert new" path) |
| `+0x250..+0x26c` | Container-B-1 (simple, struct stride `0x38`) |
| `+0x2b0..+0x2cc` | Container-B-2 (typed-virtual, struct stride `0x50`) |

---

## 5. Hash keys

All grant-relevant field hashes. The field-name hash *function* is still unknown
(Murmur3 of the obvious English names doesn't reproduce these; likely internal /
Japanese / precomputed) — but the values below are verified, so it isn't blocking.

| Hash | Field | Container | Width | Status |
|---|---|---|---|---|
| `0xf4ee6827` | flower_coin (purple coins) | A | u16 | CONFIRMED (6→99 live) |
| `0x17f0bb21` | regular_coin | A | u8 | CONFIRMED |
| `0x55815859` | Royal Seed W1 (`GRAND_SEED_WORLD1`) | B | bool | CONFIRMED |
| `0x49abba86` | Royal Seed W2 | B | bool | CONFIRMED |
| `0xb550d8d6` | Royal Seed W3 | B | bool | CONFIRMED |
| `0x1dcf7f6e` | Royal Seed W4 | B | bool | CONFIRMED |
| `0x0d5a3e00` | Royal Seed W5 | B | bool | CONFIRMED |
| `0xd4660d2b` | Royal Seed W6 | B | bool | CONFIRMED |
| `0x5d3ec9b4` | COMPLETE_GAME | B | bool | CONFIRMED |
| `0x89f1cc52` | INTRO_CUTSCENE_COMPLETED | B | bool | CONFIRMED |
| `0x105df820` | badge owned bitfield (bit == internal_id) | C | u64 | CONFIRMED |
| `0x6d1b5c25` | badge **UI-slot** bitmap (NOT ownership — file `0x1204`) | C | — | CONFIRMED |
| `0x390eb960` | per-current-world Wonder-Seed count (the one the gate predicate `+0x1787b40` reads) | A | u32 | CONFIRMED |
| `0x21f89ab1`, `0x8c20ccb7`, `0xeeff353b`, `0xa0e5f253` | mirrors of `0x390eb960` (update in lockstep) | A | u32 | CONFIRMED |
| `0x9f5ead3c` | live current-world index (1..9) | A | u32 | CONFIRMED |
| `0xdf82e9ab` | "current course" / is-clear lookup key (read in the course-clear nerve) | — | — | HIGH-CONF |
| `0x60458608` | per-course Wonder-Seed bitfield (Container D) | D | — | ACTIVE (§11) |

**Wonder-Seed bucket convention** (Switch `kWorldValToBucket` in `main.cpp`):
in-game world index `1=W1, 2=Petal Isles, 3=W2, 4=W3, 5=W4, 6=W5, 7=W6,
8=Castle (no AP bucket), 9=Special`. AP bucket layout: `W1..W6 = 0..5,
Petal Isles = 6, Special = 7`. `0x8c20ccb7` is **per-current-world** (resets on
world transition) — *not* a lifetime total.

---

## 6. Confirmed hooks (live)

All declared as file-scope `HkTrampoline` and installed in `hkMain()`
([switch-mod/src/main.cpp](../../../../switch-mod/src/main.cpp)).

| Hook | NSO offset | Mechanism | Fires on | Status |
|---|---|---|---|---|
| `NerveActivateOnce` | `+0x559f7c` | trampoline on shared helper `FUN_7100559f7c` (19 xrefs); filter by `nerve[0]` vtable, `vt_off = vtable − GetTargetStart()` | many Nerves (see filter table) | CONFIRMED |
| `SetCourseClearFlagExecute` | `+0x1bf28cc` | direct trampoline; slot 8 of `SetCourseClearFlagToGameData` (vtable `+0x34b14e8`) | every valid course clear → `COURSE_CLEARED` | CONFIRMED |
| DeathLink (SceneTransition) | via `NerveActivateOnce`, `vt_off = 0x33fd9a8` | discriminator at `nerve+0x18`: `0x04` = Mario death vs `0x84` = controlled exit | Mario death → AP Bounce | CONFIRMED |
| Game-completion goal | `+0x15b77a8` | one-shot Nerve `SetFlagEndDispMsgFirstVisitedWorldAfterClearedLastBoss` (vtable `+0x3363330`, slot 8) | final Bowser cleared → `GoalCompleted` | CONFIRMED |
| `PlayerTickLatch` | `+0x273868` | trampoline on `FUN_7100273868`; walks `p1→+0x10→+0x208→(+0 or +0x118)→HP` to latch `live_base` | per-tick (latches once) | CONFIRMED |
| PlayReport `SetEventId` | sym `_ZN2nn5prepo10PlayReport10SetEventIdEPKc` | `installAtSym` | captures room/event id | CONFIRMED |
| PlayReport IPC `SaveReport{,WithUser}` | sym `CmifProxyImpl<IPrepoService>::_nn_sf_sync_SaveReport*` | `installAtSym` | captures serialized payload | CONFIRMED |

**`NerveActivateOnce` vtable filter** (the `vt_off` values it acts on):

| `vt_off` | Event | Status |
|---|---|---|
| `0x3345728` | Wonder Seed pickup → `WONDER_SEED_AWARDED` | active |
| `0x33fd9a8` | SceneTransition (death / map travel / ~50 ms post-seed) — DeathLink source | active (discriminated) |
| `0x33fd690` | RequestEventCourseExitByAreaTag (never fires on flag touch) | inactive |
| `0x3345bc0` / `0x3345cf8` / `0x3345e30` | RequestEventGoal{Base,GateFinish,TreasureChest} (passive) | inactive |
| `0x3346330` | Wonder Flower touched (Wonder phase start) | observed (opt-in candidate) |
| `0x33fd870` / `0x33fd4c8` / `0x33fd738` | player-state anim / menu-exit / world-map travel | observed (reference) |

PlayReport SceneTransition state words seen: `0x00ff003700000084` (player course
exit), `0x00ff000600000004` (death), `0x00ff000800000084` (other transition).

### World-map route / FlowerLock gate functions (named in Ghidra, 2026-06-03)

Not hooks — RE'd functions behind the castle/Bowser route gate. Exported to
`switch-mod/syms/100/re_discovered.sym` (+ plate comments in `re_structs.json`).

| Function | NSO offset | Role | Status |
|---|---|---|---|
| `WonderSeedRouteGatePredicate` | `+0x1787b40` | Wonder-Seed route gate predicate (reads `0x390eb960`, §5) | CONFIRMED |
| `RouteFlowerLockChecker` | `+0x1b70670` | per-route FlowerLock evaluator; case 7 of `FUN_7100377280` (WorldMapRouteGateUpdate) + `FUN_71005840f0`; castle path opens when all 6 Royal-Seed FlowerLock bits set | HIGH-CONF |
| `CheckFlowerLockBit` | `+0x48c1a0` | tests one FlowerLock bit in the `gmd+0x800` container; `param_2 = bit index` (`-1` = "any data?") | HIGH-CONF |
| `FlowerLockBitfieldReader_gmd800` | `+0xe258c` | `(gmd, u32* out, hash, bitIdx)->bool`; hash table @ `gmd+0x800`, capacity @ `+0x80c`, bitfield array @ `gmd+0x7f8` (slot*0x40 + 0x28) | HIGH-CONF |
| `FlowerLockBitfield_PopCount_gmd800` | `+0x483574` | popcount over the `gmd+0x800` FlowerLock bitfield for a route entry | HIGH-CONF |
| `FlowerLock_SyncGmd80_to_ContainerB` | `+0x6b5d6c` | load-step-1 of `FUN_7101bd7c04` (save-load); syncs `gmd+0x80` bits → container-B bools via `+0x1F263FC` | HIGH-CONF |

There are **two distinct FlowerLock containers**: `gmd+0x80` (synced to container-B
at load) and `gmd+0x800` (the route-gate bitfield read by `CheckFlowerLockBit`).
FlowerLock container-B gate-bool hashes: `0x1faf41e5` and `0xb9bd745d` (HIGH-CONF,
static). ⚠️ `0xb9bd745d` conflict: an older note (`[[smbwap-wonder-seed-counter-candidate]]`)
calls `0xb9bd745d` an *AP-grant queue*; the 2026-06-03 plate comment calls it a
*container-B FlowerLock gate bool*. **Unresolved** — verify before relying on either.

### Supporting functions (named in Ghidra 2026-06-05)

Lower-level helpers RE'd alongside the above; all named + commented in the Ghidra
project and in `re_discovered.sym`.

| Function | NSO offset | Role | Status |
|---|---|---|---|
| `GetGameDataAccessor` | `+0x59f894` | opens a GameData accessor (top of the course-clear body) | HIGH-CONF |
| `CheckGameDataAccessorResult` | `+0x5e93fc` | success/fail check after a GameData write (`tbz w0`) | HIGH-CONF |
| `HandleNerveAtomicStateBump` | `+0x5390` | atomic Nerve state-bump primitive; `(this+0x68)` in the active-Nerve execute shape | HIGH-CONF |
| `ProcessWorldMapRouteGate` | `+0x377280` | world-map route-gate dispatcher; case 7 → `RouteFlowerLockChecker` (other site `+0x5840f0`) | HIGH-CONF |
| `GetFlowerLockBitfieldFallback_gmd788` | `+0xe26a8` | fallback FlowerLock reader on `gmd+0x788` when the `gmd+0x800` lookup misses | HIGH-CONF |
| `ProcessGameDataRestorePipeline` | `+0x1bd7c04` | save-load (restore) pipeline; load step 1 calls `FlowerLock_SyncGmd80_to_ContainerB` | HIGH-CONF |
| `ProcessExitCourseMgrBody` | `+0x1be3a5c` | ExitCourseMgr complete course-out (teardown + step advance); gate-entry hook candidate | ACTIVE |
| `CheckOverworldBridgeGate` | `+0x935ce0` | overworld inter-world bridge/route gate eval `(world_val)`; companion list-builder `+0x480f20` | HIGH-CONF |

---

## 7. Save-file byte offsets (verification / offline editing — NOT live-writable)

> ⚠️ These are **save-OUT staging-buffer** offsets in `game_data.sav`. The game
> repopulates the buffer FROM live state on every save and discards writes into
> it. Use them to *predict & confirm* the bytes a real (GameDataMgr-API) grant
> produces, and for offline save editing — **never as a live grant target.**

File: `%APPDATA%\Ryujinx\bis\user\save\0000000000000002\<user>\game_data.sav` —
~21,876 bytes, plaintext, magic `04 03 02 01`, ~88% zero. The first `0x400` after
the header is **128 × (u32 hash_key, u32 value)** — the container-A counter table
(same hashes as §5).

| Offset | Field | Width | Notes |
|---|---|---|---|
| `0x0890` / `0x0894` | flower_coin (key / value) | u32 / u32 | key `0xf4ee6827`; value 6→99 & 148→118 validated |
| `0x08A8` | regular_coin value | u32 | key `0x17f0bb21`; 26→33 validated |
| `0x0EA0` | **badges owned** bitfield | u64 | bit position == internal_id (same bitfield as hash `0x105df820`, see §8) |
| `0x0C58` (`0x0C5C` byte) | "badges ever equipped" bitfield | u96-ish | bit == internal_id of an ever-equipped badge |
| `0x16B8` | currently-equipped badge identity hash | u32 LE | e.g. `0xe41b1aba` Wall-Climb → `0xb77086e2` Auto Super Mushroom |
| `0x1204` | badge UI-slot bitmap | — | hash `0x6d1b5c25` (not ownership) |
| `0x167C` | lives | — | |
| `0x3480` | shop Wonder-Seed flag | u32 | bit 0 = first Poplin-shop WS purchase |
| `0x1718` | per-course PurpleCoin records | array, stride 4 | |
| `0x3360` | per-course GoalSeed | array, stride 4 | |
| `0x3AF8` | per-course WonderSeed bitfield (`0x3AF8 + 4*course_idx`) | array, stride 4 | the Container-D persistence target (§11) |
| `0x4408` | per-course CourseClear | array, stride 4 | |
| `0x53E8` | BC active-hash slot | — | |

Diff tool: `python scripts/savediff.py <pre>.sav <post>.sav` (classifies
first-acquire / +1 / bit-flip / change).

---

## 8. Badge map (apworld # → SMBW internal_id)

internal_id == bit position in the `0x0EA0` ownership bitfield (== hash
`0x105df820`). IDs are sparse and non-monotonic — each needs an empirical capture.
**4 of 24 known.** The save offset `0x0EA0` and the API hash `0x105df820` are the
*same* bitfield via two access paths (save file vs live container-C) — not a
conflict.

| apworld # | Badge | internal_id | Equip name hash | Status |
|---|---|---|---|---|
| #0 | Parachute Cap | 34 or 35 | — | HIGH-CONF (ambiguous w/ #1) |
| #1 | Wall-Climb Jump | 34 or 35 | `0xe41b1aba` | HIGH-CONF |
| #2 | Coin Reward | **9** | — | CONFIRMED |
| #3 | Auto Super Mushroom | **46** | `0xb77086e2` | CONFIRMED |
| #4..#23 | (the other 20) | TODO | TODO | needs capture |

The serializer strips bit positions that aren't real badges — only set valid
internal_ids. `setBadgeBitfieldAbsolute(bits)` overwrites the whole bitfield and
shows live (no save/reload). Bridge name→hash tables:
`apworld/smbw_archipelago/client/{badge_table,royal_seed_table,wonder_seed_table,coin_table}.py`.

---

## 9. Live-state struct offsets (dual-write / DeathLink)

`live_base` is latched by `PlayerTickLatch` (§6). Offsets below are **CHEAT**-DB
sourced (HamletDuFromage) except the DeathLink HP write, which is CONFIRMED live.

| Field | Offset from `live_base` | Status |
|---|---|---|
| HP (DeathLink writes int16 `0`) | `+0x38` | CONFIRMED |
| flower_coin (dual-write for instant UI) | `+0xC8` | CHEAT |
| lives | `+0x60` | CHEAT |
| death byte | `+0x1C` | CHEAT |
| vertical velocity | `+0xF8` | CHEAT |
| swim state | `+0x150` | CHEAT |

---

## 10. Hash function

`FUN_71003D4110` is **Murmur3-32, seed 0**, over 81 hardcoded course-name strings
— identified by the constant signature `0xcc9e2d51 / 0x1b873593 / 0xe6546b64 /
0x85ebca6b / 0xc2b2ae35`. The **field-name** hash (flower_coin etc.) is a
different, still-unknown function.

**ARM64 gotcha:** 32-bit hash constants are materialized via `mov`/`movk` pairs,
not stored as literals — byte-searching for a hash returns near-zero hits. Walk
the `mov`/`movk` pair to reconstruct the immediate.

---

## 11. Active RE (not yet confirmed — do not treat as settled)

- **Container-D per-course Wonder-Seed persistence** — hash `0x60458608`,
  hypothesized 4-arg writer overload, save offset `0x3AF8 + 4*course_idx`. The
  shipped `pushWonderSeedOverride` is **gate-override only** (makes the current
  world's gates pass from AP's count) and does NOT write per-course persistent
  storage. Spikes:
  [docs/handoff-2026-05-29-ws-persistence.md](../../../../docs/handoff-2026-05-29-ws-persistence.md),
  [docs/wonder-seed-re-reopen-2026-05-28.md](../../../../docs/wonder-seed-re-reopen-2026-05-28.md).
  Observability hooks shipped in `switch-mod/src/probe/SeedTrace.cpp`.
- **Royal-Seed gate-entry / check-loss** — Phase A found **no single hookable
  course-entry gate** (negative result). The course-out controller chain is mapped
  (`ctx` class vtable `0x71034a8200` → parent `0x7103517068` → grandparent
  `0x71033f9660`; complete course-out body `FUN_7101be3a5c` @ `+0x1be3a5c`) but a
  stable per-world-map hook is not yet established. Spikes:
  [docs/gate-entry-session3-handoff.md](../../../../docs/gate-entry-session3-handoff.md),
  [docs/royal-seed-gate-entry-design.md](../../../../docs/royal-seed-gate-entry-design.md),
  [docs/royal-seed-phase-a-findings.md](../../../../docs/royal-seed-phase-a-findings.md),
  [docs/royal-seed-check-loss-re-findings.md](../../../../docs/royal-seed-check-loss-re-findings.md).

---

## 12. Ruled out — do NOT re-derive these

| Claim seen in old notes | Reality |
|---|---|
| `FUN_71003D3FB0` writes the course-clear field | It's a stage-info → course-index **translator**, not a writer |
| `FUN_71003838AC` is a unified bool get/set | It's a **reader** only |
| `FUN_710049F648` signature is `(this, hash, value)` | Actual: `(this, value, hash)` — **value first** |
| SMBW's hash function is none of CRC32/FNV/DJB2/SDBM/Murmur3 | Murmur3-32 **is** used (for course names); field-name hash still unknown |
| `0x8c20ccb7` is the lifetime Wonder-Seed counter | It's one of 5 **per-current-world** mirrors (resets per world) |
| `0x17f0bb21` is `play_time_sec` | It's `regular_coin_count` |
| The save-OUT staging buffer (UUID `savedata_id` scan) is a grant target | It is **not** live — repopulated from live state every save |
| `dumpPerWorldTables` yields a per-world hash record list | Those tables are schema/registry, not records (negative result) |
| `0x0f3c` is the master owned-badges bitfield | No — `0x0EA0` is; `0x0f3c` was a mis-read |

---

*Maintenance: when a §11 ACTIVE item resolves, promote it into the relevant table
above with a CONFIRMED/HIGH-CONF flag and delete the spike pointer. When you
finish a Ghidra session, run `scripts/ghidra/export_re_annotations.py` to dump
named functions/structs into `switch-mod/syms/100/re_discovered.sym` +
`re_structs.json` so the analysis survives the local project file.*
