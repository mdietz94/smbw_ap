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

All grant-relevant field hashes. **The field-name hash is now SOLVED** (§10): it's
murmur3-32 seed 0 over the flag's name — for a Struct member, the **dotted full name**
`StructName.MemberName`. So any hash below can be re-derived from its name, and any
new flag resolved offline from the `GameDataList.Product.100` RomFS schema (name →
hash → **category** → SaveFileIndex) via the **smbw-romfs-datamining** skill. The
world-map / Bowser-approach hashes live in §11.

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
| `ItemGetMaskBuild` (power-up negation) | `+0x3c4050` | direct trampoline; post-orig AND-clears AP-denied bits from the can-get mask at `component+0xB0` (see §14) | every ItemGet can-mask rebuild (per-frame player tick `+0x275400` + setting changes) | HIGH-CONF (static; built, not yet live-validated) |
| `BadgeShopComputeStates` (AP shop ownership) | `+0x1c3f6a4` | direct trampoline; post-orig overrides AP-managed badge rows' display state (see §15) | every Poplin badge-shop state recompute | HIGH-CONF (static; built, not yet live-validated) |
| `BadgeShopPurchaseCommit` (shop check) | `+0x1c4072c` | direct trampoline; edge-detects a confirmed badge buy (kind@`+0x6a8`==0, done byte@`+0x6f8` 0→1) → `enqueueBadgeAcquired(id@+0x6ac)` (see §15) | confirmed badge purchase | HIGH-CONF (static; built, not yet live-validated) |
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
| `CheckOverworldBridgeGate` | `+0x935ce0` | overworld inter-world bridge/route gate eval `(world_val)`; companion list-builder `+0x480f20` | MED-CONF (memory-sourced, not re-verified) |

### Annotation caveats — what to re-evaluate (for future agents)

The Ghidra names/prototypes/comments (exported to `re_discovered.sym` /
`re_structs.json`) carry known limitations. Re-verify before trusting these:

- **`void*` pointer types** — every GameDataMgr/substruct param is typed `void*`
  (no `GameDataMgr` struct is defined in the project; the layout is §4 here only).
- **Decompile-verified prototypes** (trustworthy arg structure): the GameDataMgr
  API set — `ContainerA*`/`ContainerB*` readers+writers, `SetContainerBBoolDeferred`,
  `StageInfoHashToCourseIndex`, `GameDataMgrObjectAccessor`.
- **Convention-only prototypes** (NOT decompile-verified): the nerve/tick set —
  `NerveActivateOnceShared`, `SetCourseClearedFlagToGameData`,
  `HandleGameCompleteGoalNerve`, `PlayerTickLatchTarget` — all use the single-arg
  `void(void*)` execute convention. `PlayerTickLatchTarget` models `param_1` only
  (likely more args) and was **created via `create_function`** (no prior
  auto-analysis — verify its body bounds).
- **Inferred prototype**: `HandleNerveAtomicStateBump` (from its `(this+0x68)` call site).
- **No prototype set** (signature unverified): `Murmur3_32_CourseName`,
  `GetGameDataAccessor`, `CheckGameDataAccessorResult`, the per-course candidates,
  and the **6 original FlowerLock/castle functions** (2026-06-03) — those describe
  signatures in prose but decompile still shows `undefined f(void)`.
- **Identity, not signature, caveats**: `HandleGameCompleteGoalNerve` is static-only
  (not live-validated); `CheckOverworldBridgeGate` is MED-CONF (memory-sourced).
- **No Ghidra struct types** exist (`re_structs.json` `structs[]` is empty) — all
  struct/offset knowledge is §4 above.

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

`FUN_71003D4110` is **Murmur3-32, seed 0**, over the name as UTF-8 bytes (strlen
length — NUL not hashed); constant signature `0xcc9e2d51 / 0x1b873593 / 0xe6546b64 /
0x85ebca6b / 0xc2b2ae35`.

**The field/flag-name hash is the SAME function — CONFIRMED 2026-06-09** (supersedes
the old "field-name hash unknown"). A GameData flag hashes as `murmur3(name)`, and a
Struct **member** as the **dotted full name** `murmur3("StructName.MemberName")`.
Validated: `murmur3("IsChangeEnvEnterKoopaCastle") == 0xe02a5e43` (independently from
a save diff) + every `WorldMapCloudPackunVanishInfo` member reproduces. ⟹ any flag's
hash is now computable offline from its name. The `GameDataList.Product.100` RomFS
schema maps every flag's name → hash → category → `SaveFileIndex`; resolve flags with
the **smbw-romfs-datamining** skill + `scripts/romfs/hash_lookup.py` (no Ghidra).

**ARM64 gotcha:** 32-bit hash constants are materialized via `mov`/`movk` pairs,
not stored as literals — byte-searching for a hash returns near-zero hits. Walk
the `mov`/`movk` pair to reconstruct the immediate (or just murmur3 the name).

---

## 11. World map, open-world routing & route gates

Drives open-world mode (walk-in from Petal Isles). Derivation /
remaining-spike (the `FirstVisitDemo` flag hash is still unpinned):
[docs/re-world-intro-demo-2026-06-05.md](../../../../docs/re-world-intro-demo-2026-06-05.md).
Switch impl: `probe::applyOpenWorldEntry` (`SeedTrace.cpp`) + the
`worldRoutable` / `worldTravel` / `courseVisible` / `routeGateForceOpen` hooks
(`main.cpp`).

**Per-world descriptor table** — NSO **`+0x29f0ba4`**, stride **`0x70`** (28 u32
fields/record). Record order `rec0..8` = W1, Petal, W2, W3, W4, W5, W6, Special,
**Castle**. Each u32 field is a runtime-hashed gmd container key (no static xref).
Mask-bit→record: `kBitToRec[9] = {0,2,3,4,5,6,1,7,8}`. CONFIRMED.

| record field | Role | Container |
|---|---|---|
| `+0x04`, `+0x08` | per-course "reached/visible" arrays = Course-Map nodes | gmd+0x80 |
| `+0x3c` | "world discovered" byte (world-level travel) | gmd+0x20 |
| `+0x48` / `+0x6c` | badge-house / Master-Poplin's-house shop nodes | gmd+0x20 |
| `+0x10` / `+0x14` | Wonder-Seed reg/spec hashes (anchors to ID a record live) | — |

**gmd+0x80** (Course-Map node source): `bucket=*(gmd+0x80) cap=*(+0x8c)
limit=*(+0x70) objs=*(+0x78)`, obj stride `0x40`, `count@obj+0x20`,
`data(u32*)@obj+0x28`. Filling a record's `+0x04`/`+0x08` first-course block
(`{20,0,0,0}` / `{28,1<<28,0,0}`) surfaces that world's course node(s); all-zero =
no nodes. **gmd+0x20** (discovered/shop bytes): `bucket=*(+0x20) cap=*(+0x2c)
limit=*(+0x10) objs=*(+0x18)`, obj stride `0x18`, **byte@obj+0x16**. A shop byte
surfaces its node only *with* the gmd+0x80 fill present. CONFIRMED (live).

**World-map predicates:**

| NSO offset | Role | Reads | Status |
|---|---|---|---|
| `+0x0935c80` | `CourseVisible` — per-node visible (overworld walk) | `actor+0x8d` visited byte + gmd bool `0xb003b5f0` | CONFIRMED (hooked: force-true) |
| `+0x0935ce0` | `WorldRoutable` — world shows in travel TAB | gmd+0x80 `vtable[+0x20]()>=1 && data[0]&1` | CONFIRMED (hooked) |
| `+0x0055ed80` | `WorldTravel` — travel-confirm for a world | → `FUN_710055f330` | CONFIRMED (hooked) |
| `+0x0055f330` | world travel-availability predicate | gmd+0x20 byte OR gmd+0x80 `data[0]&1` | CONFIRMED |
| `+0x01c235c4` | Course-Map selection handler (world-select → `"L_Btn-T_World_NoOpen"`) | — | CONFIRMED |

**Route-gate / FlowerLock** (world-map route-segment "can pass"):

| NSO offset | Role | Status |
|---|---|---|
| `+0x0383418` | route-gate body — writes lock byte at `*(actor+0x20)&~3` (0=locked, 1=open); returns 1, the byte IS the output | CONFIRMED (hooked: force-open for PI/Castle) |
| `+0x016cbf58` | `IsDisplayFlowerLockUI` — per-route FlowerLock predicate | CONFIRMED |

`FUN_7100383418` gmd inputs: master FlowerLock bool **`0x30bdd45c`** (true→locked),
**`0x925d4260`** (container-B path), Wonder-Seed count **`0x90d4d0f2`** (≥ threshold
→ open). The per-route lock list comes from a BYML/RomFS **`"FlowerLock"`** param on
the route actor — **not** an addressable gmd hash. PI→world routes gate on a
cumulative Petal-Isles Wonder-Seed count (vanilla `W4 Start` needs 10); the gate
**recomputes from the count**, so the byte-force does not hold for seed gates — the
AP-authoritative count is the lever (grant all PI Wonder Seeds).

**World-intro `FirstVisitDemo`** (BYML): the one-time demo that moves the Poplin
world-map NPCs and removes the route obstacle on a world's first **on-foot** entry.
Trigger is a course-point demo condition (`"FirstVisitDemo"` → enum `0xb`, parser
**`FUN_71009281bc`**); content is BYML/RomFS. NPCs are `WorldMapNpc` actors moved
via `WorldMapNpcEventRailMoveParam`; the blocker is a `WorldMapNpcObstacleEventParam`
actor. Completion flag `EndFirstVisitWorldDemo` (gmd; hash not pinned —
runtime-hashed). **Fast-travel straight into a world skips it (obstacle stays);
walking in from PI plays it.** HIGH-CONF.

**World-reveal demos + the Bowser approach (open-world)** — CONFIRMED 2026-06-09,
mostly via the **smbw-romfs-datamining** offline path. Open-world walks in from Petal
Isles and never plays the position-triggered reveal cutscenes, so the roads to other
worlds and the Bowser path stay drawn-but-blocked. `ProcessWorldMapRouteGate`
(`+0x377280`) re-derives every route from gmd state on each map load, so **setting the
persistent bits draws the roads without the cutscene** (live-confirmed by save-diff +
inverse-revert test). Three layers:

1. **Reveal node + roads** (container-C bitfields). Node `0x35bf61af` bit =
   `GrandPropellerFlowerDemoType` (1=W2 … 5=W6, 6=ToCastle). Road bits per world:
   W2 `0xbcc1ef0e`:2/8 + `0x09bfe967`:1; W3 `0xbcc1ef0e`:9 + `0x57df969b`:1 +
   `0x2309a645`:12; W4 `0x9d25ce3b`:1; W5 `0x40c00dd7`:2 + `0xf5411212`:3; W6
   `0x52781dfd`:1. `0xbcc1ef0e` is a **shared accumulating** graph bitfield — OR
   individual bits, never overwrite. Set via `setContainerCBit` in
   `applyOpenWorldEntry`.
2. **Cloud-piranha barrier** around Bowser's Kingdom = six per-world actors
   `WObjCommonPackunCloud<World>`, each gated on a **saved Bool**
   `WorldMapCloudPackunVanishInfo.IsVanish<World>` (RomFS AI-graph ground truth):

   | flag | hash | cat |
   |---|---|---|
   | IsVanishSavanna | `0xc687fb5f` | Bool[234] |
   | IsVanishYama | `0xcff5f3d2` | Bool[236] |
   | IsVanishWa | `0x048bc39c` | Bool[235] |
   | IsVanishSabaku | `0x1677f038` | Bool[233] |
   | IsVanishKin | `0x95539ec5` | Bool[231] |
   | IsVanishNettai | `0x7f6e8a47` | Bool[232] |

3. **Castle fly-in node** = `WObjCommonMiniKoopaTeleportFlowerA` (World002/Petal Isles,
   WorldMapId 12→World008), Create-linked from `WorldMapObjKoopaCastleEntranceGround`,
   which reads saved Bool `WorldMapKoopaCastleEntranceDemoInfo.IsAppear` =
   **`0xc06bd61e`** (Bool[245]).

⚠️ **All seven layer-2/3 bools are Bool-category** → grant via `grantContainerBBool`,
NOT `grantContainerACounter` (the Int writer **silently no-ops** on Bool hashes — see
§13 and the smbw-save-data Bool-vs-Int footgun). They were all already in the client's
`WORLD_UNLOCK_HASHES` but dead because the table was dispatched single-list through the
Int writer; the fix split it by GameDataList category. Full case study:
[`../../smbw-romfs-datamining/reference/gamedatalist-schema.md`](../../smbw-romfs-datamining/reference/gamedatalist-schema.md);
session journal: `docs/grand-propeller-flower-reveal-re-2026-06-08.md`.

**Current-world index:** container-A hash **`0x9f5ead3c`** = in-game world index
(`1=W1, 2=Petal, 3=W2, 4=W3, 5=W4, 6=W5, 7=W6, 8=Castle, 9=Special`). CONFIRMED.

---

## 12. Active RE (not yet confirmed — do not treat as settled)

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

## 13. Ruled out — do NOT re-derive these

| Claim seen in old notes | Reality |
|---|---|
| `FUN_71003D3FB0` writes the course-clear field | It's a stage-info → course-index **translator**, not a writer |
| `FUN_71003838AC` is a unified bool get/set | It's a **reader** only |
| `FUN_710049F648` signature is `(this, hash, value)` | Actual: `(this, value, hash)` — **value first** |
| SMBW's hash function is none of CRC32/FNV/DJB2/SDBM/Murmur3 | Murmur3-32 **is** used — for course names AND field/flag names (the latter over the **dotted** `Struct.Member` name); the "field-name hash unknown" claim is **resolved** (§10) |
| The Bowser's-Kingdom cloud piranhas are gated on Royal Seeds | **No** (live-tested). They're six `WorldMapCloudPackunVanishInfo.IsVanish*` saved Bools (§11) — RomFS AI-graph ground truth |
| The cloud piranhas clear when `EndFirstVisitWorldDemo` is set | **No** — that's the world's *opening*-cutscene flag, not the post-castle barrier. The hook that forced its reader (`FUN_7101b5c600`) was reverted |
| The `WORLD_UNLOCK_HASHES` table lands through `grantContainerACounter` | **No for 84/86 of them** — they're Bool-category and the Int writer **silently no-ops**; must route Bool hashes through `grantContainerBBool`. (`grantContainerBBool` conversely null-derefs on the ~2 Int hashes — the real cause of the old "bool writer crashed the drain" note.) |
| `0x8c20ccb7` is the lifetime Wonder-Seed counter | It's one of 5 **per-current-world** mirrors (resets per world) |
| `0x17f0bb21` is `play_time_sec` | It's `regular_coin_count` |
| The save-OUT staging buffer (UUID `savedata_id` scan) is a grant target | It is **not** live — repopulated from live state every save |
| `dumpPerWorldTables` yields a per-world hash record list | Those tables are schema/registry, not records (negative result) |
| `0x0f3c` is the master owned-badges bitfield | No — `0x0EA0` is; `0x0f3c` was a mis-read |
| Container-D (`gmd+0x800`) controls Course-Map node visibility | No — it holds Wonder-Seed / the 4-elem castle-gate arrays; filling it surfaces no nodes (the source is the **gmd+0x80** per-world record) |
| The 85 container-A `world_unlock_table` bools open world routes | No — they write a different offset than the travel code reads (`byte@+0x16` stays 0) |
| A `gmd+0x20` "discovered" byte alone surfaces a course node | No — the `gmd+0x80` first-course fill is required; the byte alone (or a shop byte alone) does nothing |
| Forcing the route-gate lock byte (`FUN_7100383418`) open clears the path | Only the gate *state* — the `FirstVisitDemo` obstacle actor (BYML) still physically blocks; only the demo (normal on-foot entry) removes it |

---

## 14. ItemGet pipeline & power-up negation (2026-06-10 static session)

How an in-level item pickup is permitted, and the choke point the
`ItemGetMaskBuild` hook (§6) uses to negate power-ups.  Derivation:
static-analysis-findings.md "2026-06-10 — power-up negation" + the RomFS
`pack_PlayerBase` extraction.  All offsets HIGH-CONF static (capstone over the
uncompressed NSO with relocations applied) unless noted.

**The mechanism is the engine's own per-item-type permission system** — the
same one that makes power-ups untouchable while drill-digging.  The player's
`game::actor::component::ItemGetParam` (RomFS
`pack_PlayerBase/ItemGetParam/Player...bgyml`) declares named
`ItemGetSetting` profiles: `DefaultGetItemSetting` (everything true),
`DrillDig` (coins + PlusWatch only), `OnlyCoin`, `DisableItemGet` (empty =
nothing).  The active profile is converted to a **per-player can-get bitmask**
and the pickup sensor refuses any touch whose type bit is clear: item stays in
the level, no pickup animation, no transform, no damage.

| NSO offset | Role | Status |
|---|---|---|
| `+0x3c4050` | **can-get mask BUILDER**: `(component, x1)` → clears `component+0xB0` (u32) + `+0xB4` (u16 coin mask), reads active `ItemGetSetting*` from `component+0x68`, ORs one bit-group per true field.  Single ret, no return value.  Clean prologue (sub/stp/stp/stp/add).  Callers: `+0x275400` (per-frame player tick), `+0x3c3e94`, `+0x447894`, tail `+0x8c27bc` | CONFIRMED static |
| `+0x182c8bc` | **canGetItemType reader**: `mask >> runtime_type & 1` (type ptr via `+0x182ca58`); inlined copies at `+0x2d6324`, `+0x70a528`, `+0x182d1ec` | CONFIRMED static |
| `+0x93a204` | ItemGetActorType **string→enum parser** (table at data `+0x3497831`, double-NUL-separated) | CONFIRMED static |
| `+0x17d7100` | `ItemGetSetting` gparam class visitor (class hash `0xa0d58fa9`, object size `0x50`); per-field accessor helpers `+0x17d7540..+0x17d7c14` | CONFIRMED static |
| `+0x7890fc` | second field visitor (alphabetical field index 0..15) | CONFIRMED static |

**`ItemGetSetting` object layout** — bool values at `+0x30..+0x3F`
(alphabetical), per-field presence flags at `+0x40..+0x4F`, parent-fallback
chain at `+0x10`/`+0x18` (gyml inheritance):
`+0x30` AwaFlower · `+0x31` CoinDefault · `+0x32` DrillSuit · `+0x33`
ElephantSuit · `+0x34` FireFlower · `+0x35` ItemBalloon · `+0x36` ItemBubble ·
`+0x37` ItemKey · `+0x38` Kinoko · `+0x39` Kinoko1Up · `+0x3a` PlusWatch ·
`+0x3b` Star · `+0x3c` WonderChip · `+0x3d` WonderHole · `+0x3e` WonderSeed ·
`+0x3f` WonderStar.

**Runtime item-type bit positions in the `component+0xB0` mask**
(= RomFS ItemGetActorType enum + 1; cross-checked: the WonderSeed field ORs
`0x2840` = bits 6/11/13 = Offering/WonderFlower/GroundSead, and the
`+0x182c8f0` seed-special-case tests exactly `0x2840`):

| bit | item | | bit | item |
|---|---|---|---|---|
| 1 | Kinoko | | 10 | Key |
| 2 | **FireFlower** | | 11 | WonderFlower |
| 3 | Star | | 12 | **DrillSuit** |
| 4 | OneUpKinoko | | 13 | GroundSead |
| 5 | **ElephantSuit** | | 14 | ItemBalloon |
| 6 | Offering | | 15 | WonderChip |
| 7 | ItemType00 | | 16 | ItemBubble |
| 8 | ItemType01 | | 17 | PlusWatch |
| 9 | WonderKinoko | | 18 | **AwaFlower** |
| | | | 19 | WonderStar |

CoinDefault drives the separate u16 coin-category mask at `+0xB4`
(bits `0x67`).  Switch impl: `probe/ItemGetGate.{hpp,cpp}` + the
`itemGetMaskBuildHook` trampoline in `main.cpp`; bridge message
`set_itemget_deny` (`WireSetItemGetDenyMask`, applied on the network thread —
single atomic store); client debug command `/deny_powerups`.

**Related anchors mapped en route** (for future power-up work):

- The HamletDuFromage "Mario form swap" cheat decodes as: patch the `ldr w9,
  [x8]` at `+0x198b50` into `b +0x20128e8` (a code cave returning the constant
  at `+0x20128f4`).  The site is a **per-player current-form getter block**
  inside a big hash-keyed property dispatcher (block loads
  `*(mgr+0xB0 array)[player]`); form values 2=Fire 3=Elephant 5=Small 6=Drill
  8=Tall 9=Bubble.  The form mirror array is WRITTEN at `+0x274830` (inside
  the per-frame tick fn starting `+0x274244`) from **live player struct
  `+0xB8`** — the live current-form field.
- `PlayerRequestItemGet` AI request: vtable at data `+0x33388e8` (slot0 name
  getter `+0x1529fc0`, slot8 execute `+0x1529fd8` — ORs `1 << *(req+0x20)`
  into a pending byte at `player+0x86b`; param slot bound by name
  `"RequestItemGetType"` via slot7 `+0x7daf84`).  Fallback hook candidate if
  the mask approach ever needs replacing.
- Metamorphosis AS selector object (vtable `+0x33f2870`, ctor `+0x87c2f4`
  area): binds conditions `IsNoChangeGetItem` (slot `+0x68`!),
  `IsForceItemGet` (`+0x70`), `IsAutoSuperKinoko` (`+0x78`) and queries AS
  events `None/ChangeSuper/ChangeFire/ChangeElephant/ChangeDrill/ChangeBubble`
  → availability bytes `+0x80..+0x85` (PlayerModeType order).  The engine has
  native per-change veto conditions here if finer-grained suppression is ever
  needed (RomFS: `pack_PlayerBase/AI/PlayerPowerUp.module.ainb`).

## 15. Poplin badge-shop & AP-authoritative shop ownership (2026-06-10 static session)

How the Poplin BADGE shop builds its per-row "owned / sold-out / buyable"
display and where the AP shop hooks (§6) attach.  All offsets HIGH-CONF static
(decompile + disassemble over the uncompressed NSO), built but not yet
live-validated.

**The coupling AP breaks:** the shop computes each row's state from the badge
**owned** (`0x105df820`) / **ever-purchased** (`0xe48a1168`) BoolArray bits —
both saved BoolArray[100], bit == badge internal_id (same index as §8).  So an
AP-granted badge (owned bit set so Mario can equip it) showed SOLD OUT and an
AP-checked badge whose bit the game never set still showed buyable.  Readers:
`isBadgeOwned` `+0x1b5b870` (reads `0x105df820`), `isBadgePurchased` `+0x1b5b810`
(reads `0xe48a1168`), combined `IsBadgeOwnedOrPurchased` `+0x689b20`(arg2=1) and
`+0x6b0140` (direct bucket walk).  AI EventQuery `IsGetBadge` evaluator
`+0x179e984` (out +0x28 owned||purchased, +0x30 purchased, +0x38 owned);
`CheckEnableBuy` evaluator `+0x1638324`.

**Class:** `UIBadgeShopScreen` (factory `+0x6b62e4` allocs 0x978, vtable
`+0x34cfc08`; getName `+0x1c3ddcc`).  The shop lineup is **per world-map NPC**
(`+0x5233c0(0x6c259974)`); built by `+0x1c3f494` → `+0x1b71400` walking the NPC's
gparam `game__stage__ShopItemInfo` list, appending badge rows via `+0x1b71760`
(challenge badges hidden unless BoolArray `0x9c3b0d85`[id]).

| field | offset | role |
|---|---|---|
| item count | `screen+0x7d0` | u32 |
| item array | `screen+0x7d8` | ptr → array of item* (stride 8) |
| coins held | `screen+0x6e0` | u32 (the currency this shop charges; badges = flower coins) |
| item type | `item+0x00` | u32: 0 badge / 1 WonderSeed / 2 1-Up / 3 Kakashi |
| item badge id | `item+0x04` | s32 internal_id (== owned-bit index) |
| item price | `item+0x18` | s32 |
| item **state** | `item+0x20` | u32 display state — **0 buyable / 1 unaffordable / 2 sold-out / 3 maxed** (written LAST per item) |

| NSO offset | Role | Status |
|---|---|---|
| `+0x1c3f6a4` | **`computeItemStates(screen)`** — sets each `item+0x20`; badge sold-out test = `+0x689b20`(id,1). **Hooked** (`BadgeShopComputeStates`): post-orig, for AP-managed badge rows overwrite state — SOLD OUT (2) if checked, else affordability (0/1) ignoring the owned/purchased bits.  Prologue sub/stp×5 SAFE | CONFIRMED static |
| `+0x1c4072c` | **`purchaseCommit(screen)`** — UIBadgeShopScreen `cPurchaseConfirmation` state execute.  `switch(screen+0x6a8)` kind (0=badge); case 0 grants once via `+0x1b5ade0`, sets write-once done byte `screen+0x6f8`=1, deducts coins `+0x6e0`, sets state=2.  Buy badge id = `screen+0x6ac` (live after grant).  **Hooked** (`BadgeShopPurchaseCommit`): edge kind==0 ∧ done 0→1 across orig → `enqueueBadgeAcquired(*(int*)(screen+0x6ac))`.  Prologue stp-pre/str/stp×3 SAFE | CONFIRMED static |
| `+0x1b5ade0` | `GrantBadgeWorldMapDemoFlags(&id)` — the actual badge grant; 3 callers (shop commit, AI gift, medley dispatch) so NOT shop-specific — hook the commit fn, not this | CONFIRMED static |
| `+0x1c3e560` | `resolvePaneContent` — selects each row's **msbt label** (badge name = BadgeInfo gparam member `+0x40`, desc `+0x48`; prefixes "GameMsg/Name_Badge" / "GameMsg/BadgeInfo").  Hook point for the deferred "shop text reflects the AP check" bonus; substituting custom UTF-16 needs the downstream eui `Message::getText` (resolver ~`+0x4eb000`, not yet a clean function) — left for a follow-up session | CONFIRMED static |

There is **no code writer** for `0x105df820`/`0xe48a1168` — the bits are set
data-driven through the GameData trigger system (`+0x55534e0` → `+0x3877c4`
ring at gmd+0x278), so the owned/purchased bits can't be hooked per-hash; that
is why purchase detection hooks the commit fn instead.  Switch impl:
`probe/BadgeShop.{hpp,cpp}` + the two trampolines in `main.cpp`; bridge message
`set_badge_shop_state` (`WireSetBadgeShopState{managed,sold}`, applied on the
network thread); client `SMBWContext._recompute_badge_shop_state`.

---

*Maintenance: when a §12 ACTIVE item resolves, promote it into the relevant table
above with a CONFIRMED/HIGH-CONF flag and delete the spike pointer. When you
finish a Ghidra session, run `scripts/ghidra/export_re_annotations.py` to dump
named functions/structs into `switch-mod/syms/100/re_discovered.sym` +
`re_structs.json` so the analysis survives the local project file.*
