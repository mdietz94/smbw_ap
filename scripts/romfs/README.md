# RomFS GameData datamining toolchain

Offline tooling to resolve **any named SMBW GameData flag → its hash + container
category** straight from the game's RomFS — **no Ghidra, no runtime**. This is the
data side of SMBW RE; the Ghidra/NSO-static side lives in `scripts/ghidra/`.

Full methodology, the GameDataList schema, and worked examples are in the
**smbw-romfs-datamining** skill (`.claude/skills/smbw-romfs-datamining/`). This
README is the script-level quickref.

## Why this exists

Every SMBW save flag is keyed by a **murmur3 x86_32 (seed 0) hash of its name**.
`GameDataList.Product.100.byml.zs` in the RomFS is the *complete* schema: for every
flag it records the hash, category (Bool/Int/Enum/Struct/Float/...), default, and
`SaveFileIndex` (≥0 = persisted to `game_data.sav`, −1 = transient). So once you
have a flag's name you can compute its hash and read its category — which tells you
**which `probe::` writer to use** (Bool→`grantContainerBBool`, Int→
`grantContainerACounter`; mismatching them is a silent no-op or a crash — see the
**smbw-save-data** skill's Bool-vs-Int footgun).

## One-time RomFS extract

1. Dump the base **NSP** (or pull RomFS via Ryujinx), get the **title key** from the
   `.tik`, and run `hactool` to extract the RomFS to a directory (`romfs/`).
2. Decode the schema:
   ```
   python sarc_extract.py <...>/GameData/GameDataList.Product.100.byml.zs gd/   # if packed
   python byml_parse.py GameDataList.Product.100.byml GameDataList.json
   ```
   (`GameDataList.Product.100.byml.zs` is a bare zstd-compressed BYML v7 — decompress
   with `zstandard`, then `byml_parse.py`.) Point tools at it via `$SMBW_GDL`.

## Scripts

| Script | Does |
|---|---|
| `hash_lookup.py names.txt [GameDataList.json]` | murmur3 each name → look up category/index in GameDataList. **The everyday tool.** Exposes `murmur3_32()` + `load_index()` for import. |
| `byml_parse.py in.byml out.json` | minimal BYML v2–v7 → JSON (GameDataList, WorldMapInfo, BancMapUnit, gparam, ...). |
| `sarc_extract.py pack.zs out/` | decompress `.pack.zs` (zstd) + unpack the SARC (actor packs: gparam `.bgyml`, AI `.ainb`, ...). |
| `build_corpus.py` | harvest identifier strings from the RomFS (+ NSO) → `corpus.txt` for the namer. `$SMBW_ROMFS`, writes `./corpus.txt`. |
| `name_hashes.py hashes.txt corpus.txt [main_dec.bin ...]` | reverse direction: recover NAMES for a list of hashes by brute-matching the corpus (composes `Struct.Member` dotted names + verifies). |

## Resolve a flag (the common case)

```
echo WorldMapKoopaCastleEntranceDemoInfo.IsAppear > names.txt
SMBW_GDL=GameDataList.json python hash_lookup.py names.txt
# -> ...IsAppear  0xc06bd61e  -> Bool[245]  {'SaveFileIndex': 245, ...}
```

Struct members hash by their **dotted full name** (`Struct.Member`), not the bare
member. The murmur3 is over the UTF-8 bytes at **strlen length** (the NUL is not
hashed). Validated end-to-end against `IsChangeEnvEnterKoopaCastle → 0xe02a5e43`
(also recovered from a W6-cutscene save diff).

> The extracted RomFS, `GameDataList.json`, and `corpus.txt` are large scratch
> artifacts and live outside the repo (e.g. `C:\Users\maxwe\Documents\smbw_re_tmp\`).
> Only these scripts are version-controlled.
