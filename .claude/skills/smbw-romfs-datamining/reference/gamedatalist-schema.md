# GameDataList.Product.100 schema + the Bowser-approach case study

Reference for the **smbw-romfs-datamining** skill. The on-disk shape of the GameData
schema, the murmur3 name→hash rule, and the worked example that recovered the
open-world Bowser approach (cloud piranhas + castle fly-in).

## The file

`GameData/GameDataList.Product.100.byml.zs` in the RomFS — a bare **zstd**-compressed
**BYML v7**. Decompress + parse with `scripts/romfs/{sarc_extract,byml_parse}.py` into
`GameDataList.json`. Top level:

```
{ "Data": { <category>: [ <entry>, ... ], ... }, "MetaData": {...} }
```

`Data` keys are the **container categories** (counts from v1.0.0):

| category | n | category | n | category | n |
|---|---|---|---|---|---|
| `Bool` | 255 | `Int` | 70 | `Struct` | 49 |
| `BoolArray` | 156 | `IntArray` | 10 | `UIntArray` | 49 |
| `Enum` | 108 | `UInt` | 1 | `EnumArray` | 22 |
| `Float` | 2 | `UInt64` | 4 | `UInt64Array` | 11 |
| `Vector3` | 7 | `String64` | 1 | `WString64Array` | 1 |

### Entry shape

A scalar entry (Bool/Int/Enum/...):

```json
{ "DefaultValue": false, "Hash": 376959032, "ResetTypeValue": 32, "SaveFileIndex": 0 }
```

- **`Hash`** = murmur3 x86_32 (seed 0) of the flag's name (see below). The save key.
- **`SaveFileIndex`** = index into `game_data.sav`'s per-category table when **≥ 0**;
  **`-1` = transient** (live-only, reset every load — never try to persist it).
- **`ResetTypeValue`** = when the engine auto-resets it (per-course, per-world, never).
- **`DefaultValue`** = value on a fresh save.

A **Struct** entry has no direct save value; it's a *grouping* whose `DefaultValue`
is a member list:

```json
{ "Hash": <struct-name-hash>,
  "DefaultValue": [ { "Hash": <member-name-hash>, "Value": <full-dotted-name-hash> }, ... ] }
```

Each member's **`Value`** is the hash of the **dotted full name**
`StructName.MemberName` — and *that* is the hash the save file / pair-region uses.

## The hash rule (THE unlock)

**MurmurHash3 x86_32, seed 0, over the name as UTF-8 bytes at strlen length** (the
NUL is not hashed). Implementation: `scripts/romfs/hash_lookup.py:murmur3_32`. This is
the same murmur3 as the NSO course-name hash `FUN_71003D4110` — the field-name hash
was never "internal/unknown", it's just this applied to the **dotted** name.

- Scalar flag → `murmur3("FlagName")`.
- Struct member → `murmur3("StructName.MemberName")` (the bare member won't match).

**Proof:** `murmur3("IsChangeEnvEnterKoopaCastle") == 0xe02a5e43`, a hash we had
*independently* recovered from a W6-cutscene save diff; and all 9
`WorldMapCloudPackunVanishInfo` members reproduce their recorded `Value` hashes.

## Case study — the open-world Bowser approach (2026-06-09)

Open-world drops the player into Petal Isles on foot; the route to Bowser's Castle
stayed blocked two ways. Two **Ghidra-only hypotheses were wrong** (live-tested):
"the piranhas are gated on Royal Seeds" and "on `EndFirstVisitWorldDemo`" — the
latter is the world's *opening* cutscene flag, not the post-castle barrier. The RomFS
gave ground truth.

### The six cloud piranhas

Per-world obstacle actors `Pack/Actor/WObjCommonPackunCloud{Savanna,Yama,Wa,Sabaku,
Kin,Nettai}.pack.zs` (gparam just `{WorldNo: "<world>"}`). Their AI graph
`AI/WObjCommonPackunCloud*.root.ainb` binds the GameData property
`WorldMapCloudPackunVanishInfo.IsVanish<World>` via `ActorPropertyBinder`. The six
**saved** despawn bools:

| flag (dotted) | hash | GameDataList |
|---|---|---|
| `WorldMapCloudPackunVanishInfo.IsVanishSavanna` | `0xc687fb5f` | Bool[234] save=0 |
| `…IsVanishYama` | `0xcff5f3d2` | Bool[236] save=0 |
| `…IsVanishWa` | `0x048bc39c` | Bool[235] save=0 |
| `…IsVanishSabaku` | `0x1677f038` | Bool[233] save=0 |
| `…IsVanishKin` | `0x95539ec5` | Bool[231] save=0 |
| `…IsVanishNettai` | `0x7f6e8a47` | Bool[232] save=0 |

The struct's other 3 members (`IsRequestVanish`, `IsEndVanishAnim`, `IsAnimeReset`)
are `SaveFileIndex -1` — transient animation state, leave alone.

### The castle fly-in node

With piranhas cleared, the Castle Bowser entry still didn't spawn. RomFS trace:
`WObjCommonMiniKoopaTeleportFlowerA` is placed on the **World002 (Petal Isles)** map
(`BancMapUnit/World002.bcett.byml`, obj2000, `WorldMapId 12 → World008 "Castle"`). It
is **Create-linked** (via a `LogicalSignalORTag`) from
`WorldMapObjKoopaCastleEntranceGround` (obj2176), whose AI reads the saved bool:

| flag (dotted) | hash | GameDataList |
|---|---|---|
| `WorldMapKoopaCastleEntranceDemoInfo.IsAppear` | `0xc06bd61e` | Bool[245] save=0 |

(`.IsRequestAppear` `0x1313dba6` is the transient sibling.) Bonus from the same map:
`World002.GateTable` shows the five inter-area gates need 5/8/10/12/15
`NeedNumOfWonderSeed`.

### Why every prior attempt silently failed — the Bool-vs-Int footgun

All seven of these hashes were **already** in the client's `WORLD_UNLOCK_HASHES`
(from a fresh→100% save diff) — but that channel's Switch handler wrote them via
`grantContainerACounter` (the **Int**-container writer), a **silent no-op for
Bool-category** entries. A GameDataList audit showed **84 of the 86** table hashes
are Bool — so almost the entire table never landed. (Worlds still opened because
`applyOpenWorldEntry`'s `gmd+0x80` record-fill did that independently.) The historical
"the Bool writer crashed the drain worker" note was the inverse: `grantContainerBBool`
**null-derefs on the 1–2 *Int*-category hashes** in the list (`0x5ac1e406`,
`0x20fced8b`).

**Fix:** split the table by GameDataList category — `WORLD_UNLOCK_INT_HASHES` (2,
via `grantContainerACounter`) and `WORLD_UNLOCK_BOOL_HASHES` (84, via
`grantContainerBBool`); the six `IsVanish*` + the castle `IsAppear` are also
force-granted in `probe::applyOpenWorldEntry`. See the
[smbw-save-data](../../smbw-save-data/SKILL.md) Bool-vs-Int section.

### Curating the bool list (don't reveal closed-world content)

When the 84 bools finally landed, **closed** worlds began showing unlocked courses.
Naming the hashes (`name_hashes.py`, murmur3 brute-match of NSO+RomFS strings) showed
the fresh→100% diff mixes three classes:

1. world-map obstacle/unlock state we *want* (the Bowser approach);
2. per-world `W_{Savanna…Castle,Himitu}` progress-struct members
   (`IsEndFirstVisitDemo`, `IsOpenWorldName`, `IsGetMotherSeed`) — these reveal a
   world's courses even when the seed has it **closed**;
3. completion records (`*_FINISH_A`, `Motherseed0xFinish`, `CaptainKinopio_*_Finish`,
   `IsDoneStaffCredits`) — these hide live events/rewards in a randomizer.

Keep only class 1 (+ harmless one-time-dialog suppressors); the rest move to
`WORLD_UNLOCK_DROPPED_HASHES`, each named for traceability. Open worlds still get
their course nodes from the `gmd+0x80` record fill, so dropping class 2 costs no
reachability. This is the general lesson: **a fresh→100% save diff over-captures** —
name the hashes and keep only the class you intend.

## Pointers

- Toolchain quickref: [`scripts/romfs/README.md`](../../../../scripts/romfs/README.md).
- Session journal (full derivation, dead-ends): `docs/grand-propeller-flower-reveal-re-2026-06-08.md`.
- Runtime writers + the Bool-vs-Int footgun: [smbw-save-data](../../smbw-save-data/SKILL.md).
- NSO-static side (Nerves, hooks, the route-gate functions): [smbw-reverse-engineering](../../smbw-reverse-engineering/SKILL.md).
