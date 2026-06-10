---
name: smbw-romfs-datamining
description: >-
  Resolve any named SMBW GameData flag to its hash + container category OFFLINE
  from the game's RomFS — no Ghidra, no runtime. Use when you need a flag's
  murmur3 hash, want to know whether a flag is Bool/Int/Enum/Struct (which picks
  the probe:: writer), are tracing a world-map obstacle/actor (cloud piranhas,
  castle fly-in, route gates) to the saved bool that gates it, parsing BYML/SARC
  RomFS data (GameDataList, WorldMapInfo, actor packs, AI graphs, gparam), or
  brute-recovering a name from a known hash. Bundles the GameDataList schema and
  the Bowser-approach worked example. Triggers: "resolve a flag hash", "what
  container category", "murmur3 a name", "GameDataList", "extract the romfs",
  "BYML", "SARC", "actor pack", "which world-map flag", "name this hash".
---

# SMBW RomFS GameData datamining (offline flag resolution)

The **third pillar** of SMBW RE, alongside Ghidra/NSO-static
(**smbw-reverse-engineering**) and runtime grants (**smbw-save-data**). Where those
ask "what function writes this?" and "how do I write it live?", this skill answers
**"what *is* this flag — its hash, its category, the actor it gates"** — entirely
from the game's shipped data, no Ghidra session and no running game.

This is what cracked the open-world **Bowser approach** (cloud piranhas + castle
fly-in) after two wrong Ghidra-only hypotheses (Royal Seeds, then
`EndFirstVisitWorldDemo`). The RomFS gave the ground truth in minutes.

## The core fact

Every SMBW GameData flag is keyed by **MurmurHash3 x86_32, seed 0, over its name**
(UTF-8, strlen length — the trailing NUL is **not** hashed). This is the *same*
murmur3 as the course-name hash `FUN_71003D4110`
([smbw-reverse-engineering](../smbw-reverse-engineering/SKILL.md) §hash) — it was
long thought the *field*-name hash was "unknown / internal", but it is just this
murmur3 applied to the **full dotted name**. That resolution is the key unlock.

`GameData/GameDataList.Product.100.byml.zs` is the **complete schema**: every flag's
`Hash`, `category` (the top-level `Data` keys: `Bool`, `Int`, `Enum`, `Float`,
`Struct`, ...), default value, and **`SaveFileIndex`** (≥0 → persisted in
`game_data.sav`; −1 → transient/live-only). See
[`reference/gamedatalist-schema.md`](reference/gamedatalist-schema.md) for the
on-disk shape and the Struct dotted-name composition.

## Why category matters (the payoff)

Knowing a flag's category tells you **which `probe::` writer to use** — and
mismatching them is the
[Bool-vs-Int writer footgun](../smbw-save-data/SKILL.md) that silently sank an
entire 86-hash unlock table:

| GameDataList category | `probe::` writer | mismatch result |
|---|---|---|
| `Bool` | `grantContainerBBool(hash, 1)` | `grantContainerACounter` → **silent no-op** |
| `Int` | `grantContainerACounter(hash, n)` | `grantContainerBBool` → **null-deref crash** |

So "resolve the hash" is incomplete; **always read the category** and route the
write accordingly.

## Toolchain — `scripts/romfs/`

Version-controlled in the repo (mirrors `scripts/ghidra/`); full quickref in
[`scripts/romfs/README.md`](../../../scripts/romfs/README.md). The large scratch
artifacts (extracted `romfs/`, `GameDataList.json`, `corpus.txt`, decompressed NSO)
live **outside** the repo, e.g. `C:\Users\maxwe\Documents\smbw_re_tmp\`.

| Script | Use |
|---|---|
| `hash_lookup.py names.txt` | **the everyday tool** — murmur3 each name → GameDataList category/index. Imports `murmur3_32()` + `load_index()`. |
| `byml_parse.py in.byml out.json` | BYML v2–v7 → JSON (GameDataList, WorldMapInfo, BancMapUnit, gparam). |
| `sarc_extract.py pack.zs out/` | decompress `.pack.zs` + unpack the SARC (actor gparam `.bgyml`, AI `.ainb`). |
| `build_corpus.py` → `name_hashes.py` | reverse direction — recover a NAME from a known hash by brute-matching a RomFS+NSO string corpus (composes `Struct.Member`). |

## Workflow A — name → hash + category (the common case)

1. Get the RomFS once (`hactool` on the base NSP + the `.tik` title key) and decode
   the schema: `sarc_extract.py`/zstd → `byml_parse.py GameDataList...byml
   GameDataList.json`. Point tools at it with `$SMBW_GDL`.
2. Guess the flag name from in-game behavior + RomFS actor/AI strings. World-map
   state is overwhelmingly `Struct` members, so the real key is the **dotted full
   name** `WorldMapCloudPackunVanishInfo.IsVanishWa`.
3. `echo <DottedName> > names.txt; python hash_lookup.py names.txt` → hash + category
   + `SaveFileIndex`. Done — now pick the writer by category.

## Workflow B — gate an obstacle/actor I can see on the world map

This is the Bowser-approach recipe (cloud piranhas, castle fly-in node):

1. **Find the actor.** RomFS placement is in `BancMapUnit/World00N.bcett.byml`
   (parse with `byml_parse.py`); the actor's pack is `Pack/Actor/<Name>.pack.zs`.
2. **Extract the pack** (`sarc_extract.py`) and read its **AI graph**
   `AI/<Name>.root.ainb` — `ActorPropertyBinder` / `GetEnumFromGameData` nodes name
   the **GameData property** that gates the actor (e.g.
   `WorldMapCloudPackunVanishInfo.IsVanish<World>`).
3. **Resolve** that dotted name with `hash_lookup.py` → hash + category +
   SaveFileIndex.
4. **Trace Create-links** (`LogicalSignalORTag`, obj refs in the `.bcett.byml`) when
   the visible node is spawned by a *different* controller actor — e.g. the castle
   teleport flower `WObjCommonMiniKoopaTeleportFlowerA` is Create-linked from
   `WorldMapObjKoopaCastleEntranceGround`, whose AI reads
   `WorldMapKoopaCastleEntranceDemoInfo.IsAppear = 0xc06bd61e`.
5. **Write** by category from the Switch side (`probe::applyOpenWorldEntry` in
   `SeedTrace.cpp`), then live-confirm.

## Workflow C — hash → name (reverse)

When a save diff or Ghidra immediate gives a hash you can't place:
`build_corpus.py` (harvest RomFS+NSO strings) then
`name_hashes.py hashes.txt corpus.txt main_dec.bin`. It composes every
`Struct.Member` dotted name and verifies against GameDataList's recorded
member-value hash, so it names struct fields the bare corpus can't.

## Validation & caveats

- **Algorithm is proven**: `IsChangeEnvEnterKoopaCastle → 0xe02a5e43` matches a hash
  independently recovered from a W6-cutscene save diff; all 9
  `WorldMapCloudPackunVanishInfo` members verify against their full-name hashes.
- **Dotted names only for struct members.** A bare `IsVanishWa` will not match;
  prefix the owning struct.
- **`SaveFileIndex == -1` flags are transient** — do not try to persist them; they
  reset every load (the `IsRequestVanish`/`IsEndVanishAnim` animation members).
- **RomFS gives identity, not the live writer.** It tells you *what* to write and
  *which container*; the in-game writer/Nerve (if you need to hook the moment it
  changes) is still a Ghidra job (**smbw-reverse-engineering**).
- **Hashes can collide with non-flags.** `hash_lookup.py` only reports a hit if the
  hash is actually in GameDataList, so a stray match is filtered out.

## What this superseded

Two **wrong** Ghidra-only hypotheses for the Bowser cloud-piranha barrier — "gated
on Royal Seeds" and "gated on `EndFirstVisitWorldDemo`" (the *opening*-cutscene
flag) — both ruled out by live tests. The RomFS ground truth (six per-world
`WorldMapCloudPackunVanishInfo.IsVanish*` saved bools) settled it. See
[`reference/gamedatalist-schema.md`](reference/gamedatalist-schema.md) for the full
flag table and the case study, and `docs/grand-propeller-flower-reveal-re-2026-06-08.md`
for the original session journal.
