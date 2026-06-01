---
name: smbw-save-data
description: >-
  Grant items and read/write SMBW save data via the GameDataMgr (gmd::) runtime
  API. Use when wiring a new AP item to an in-game grant, picking which probe::
  primitive an item needs (counter / bool / badge bitfield / per-course / wonder
  seed), working with hash keys, the container A/B/C/D layout, the save-file
  byte format, or the save-survival/replay strategy. Bundles the save-file format
  reference and the save-OUT-vs-live-state warning. Triggers: "grant an item",
  "GameDataMgr", "hash key", "container A/B/C", "badge bitfield", "royal seed",
  "wonder seed count", "save file offset", "why doesn't the grant survive reload".
---

# SMBW save data & item grants (GameDataMgr)

The live-grant path is the **GameDataMgr** runtime API — the *only* mechanism
that changes actual gameplay state. To find/RE these functions, see the
**smbw-reverse-engineering** skill. The full decompile journal is at
[`../smbw-reverse-engineering/reference/static-analysis-findings.md`](../smbw-reverse-engineering/reference/static-analysis-findings.md).

> ⚠️ **The single biggest trap: the save-OUT staging buffer is NOT a live-grant
> path.** Writers anchored on file offsets (the `savedata_id` UUID scan, badge
> offset `0x0EA0`, etc.) modify only the transient save-serialization buffer; the
> game repopulates it FROM live state on every save and discards your writes.
> That work produced a **save-file *editor*** + a **verification target** (predict
> the bytes a real grant will write), not a grant. Background:
> [`reference/runtime-address-backtrace-plan.md`](reference/runtime-address-backtrace-plan.md),
> [`reference/save-diff-grants.md`](reference/save-diff-grants.md).

## The singleton anchor

`gmd::GameDataMgr::sInstance` @ NSO **`+0x0363F0F0`**. Deref this qword at runtime
→ the live `GameDataMgr*`. The Switch side wraps this as `probe::gmdSingleton()`.
This replaces every Cheat-Engine pointer-scan workflow.

## Containers & their writers

| Container | Holds | Live writer (NSO) | Switch primitive (`probe::`, file) |
|---|---|---|---|
| **A** | hash-keyed u32/u16/u8 **counters** (coins, wonder-seed counts + mirrors) | `+0x0049F648` `(gmd, value, hash)` | `grantContainerACounter(hash,value)`, `incrementContainerACounter(hash,delta)` — `ContainerA.cpp` |
| **B** | hash-keyed **bools** (Royal Seeds, COMPLETE_GAME, INTRO) | `+0x0049EA24` wrapper → `+0x1F263FC` delegate on `gmd+8` substruct | `grantContainerBBool(hash,value)` — `ContainerB.cpp` |
| **C** | **bitfields** at `gmd+0x70..0x8c` (badges; wonder-seed bitfield) | walk bucket → typed sub-obj → `uint32_t[]` | `setBadgeBitfieldAbsolute(bits)`, `setContainerCBit(hash,bit,val)`, `setWonderSeedBitfieldAbsolute(lo,hi)` — `ContainerC.cpp` |
| **D** | per-course / per-seed arrays (wonder-seed persistence) | ⏳ active RE | `setPerCourseBitfieldAbsolute(...)`, `pushWonderSeedContainerDCounts()` — `PerCourse.cpp`, `SeedTrace.cpp` |

Other GameDataMgr accessors: `+0x0012AE94` container-A **reader**
`(gmd, u32* out, hash)`; `+0x03838AC` sub-bool **reader**; `+0x03D3FB0`
stage-info hash → course-index **translator** (NOT a writer — old CLAUDE.md
guess was wrong); `+0x03D4110` Murmur3-32 course-name lookup.

Grant primitives live in `switch-mod/src/probe/*.cpp`; hooks that drive them are
installed in `switch-mod/src/main.cpp`. (CLAUDE.md's old `src/program/main.cpp`
paths are the retired exlaunch tree.)

## Verified hash keys

| Hash | Field | Container | Width |
|---|---|---|---|
| `0xf4ee6827` | flower_coin (purple coins) | A | u16 |
| `0x17f0bb21` | regular_coin | A | u8 |
| `0x55815859` | Royal Seed W1 (`GRAND_SEED_WORLD1`) | B | bool |
| `0x49abba86` | Royal Seed W2 | B | bool |
| `0xb550d8d6` | Royal Seed W3 | B | bool |
| `0x1dcf7f6e` | Royal Seed W4 | B | bool |
| `0x0d5a3e00` | Royal Seed W5 | B | bool |
| `0xd4660d2b` | Royal Seed W6 | B | bool |
| `0x5d3ec9b4` | COMPLETE_GAME | B | bool |
| `0x89f1cc52` | INTRO_CUTSCENE_COMPLETED | B | bool |
| `0x105df820` | badge owned bitfield (bit == internal_id) | C | u64 |
| `0xdf82e9ab` | "current course" / is-clear lookup key | — | — |
| `0x390eb960` (+ `0x21f89ab1`,`0x8c20ccb7`,`0xeeff353b`,`0xa0e5f253`) | per-current-world Wonder Seed count (gate predicate `+0x1787b40` reads `0x390eb960`) | A | u32 |

The bridge maps AP item names → hashes in
`apworld/smbw_archipelago/client/{royal_seed_table,badge_table,wonder_seed_table,coin_table}.py`.
Cross-verified against MemetendoYT's save editor + the HamletDuFromage cheat DB.

## Badges (Container C)

Owned bitfield: hash `0x105df820`, **bit position == internal_id == badge ID**
(bit 4 = Spring Feet). `setBadgeBitfieldAbsolute(bits)` overwrites the whole
bitfield (appears live, no save/reload). Gotchas: hash `0x6d1b5c25` is the
auxiliary UI-slot bitmap (file offset `0x1204`), **not** ownership; the serializer
strips bit positions that aren't real badges — only use valid internal_ids.

## Deferred-write & save-survival

Both container-A (`+0x49F648`) and container-B (`+0x49EA24`) **queue to a dirty
buffer at `gmd->[+0xf8]`** and apply at next save. A load-before-flush silently
drops the grant. The fix is **AP-authoritative replay** — the bridge re-pushes
the canonical set on triggers so any drop/in-game-pickup is reverted:

- **Badges** — `SetBadgesAbsoluteMsg` absolute overwrite on every `ReceivedItems`,
  every `HelloMsg`, and a ~2 s tick. Idempotent.
- **Royal Seeds** — `_collect_royal_seed_grants` → re-emit one `GrantHashKeyedMsg`
  per received seed on every `HelloMsg`.
- **Wonder Seed counts** — `SetWonderSeedCountsMsg{counts:[u32;8]}` (AP buckets
  W1..W6=0..5, Petal Isles=6, Special=7) overwritten on the same 3 triggers; the
  Switch maps current-world index → bucket and writes the 5 mirror hashes via
  `pushWonderSeedOverride`.
- **Not replayed**: container-A coins + the two completion bools (not AP items
  today). Extend `_collect_royal_seed_grants` into a generic
  `_collect_hash_keyed_grants` when they become items.

⚠️ **Gate-override only, not per-course persistence**: `pushWonderSeedOverride`
makes the *current world's* gates pass from AP's count but does NOT write the
per-course Wonder Seed bitfield (Container D, file offset `0x3AF8 + 4*course_idx`)
— that writer is the **active** RE in
[docs/handoff-2026-05-29-ws-persistence.md](../../../docs/handoff-2026-05-29-ws-persistence.md)
and [docs/wonder-seed-re-reopen-2026-05-28.md](../../../docs/wonder-seed-re-reopen-2026-05-28.md).
The Royal-Seed check-loss / gate-entry follow-up is also active in
[docs/royal-seed-gate-entry-design.md](../../../docs/royal-seed-gate-entry-design.md)
and [docs/gate-entry-session3-handoff.md](../../../docs/gate-entry-session3-handoff.md).

## Dual-write for instant UI refresh

A deferred write applies at next save; for the in-game counter to refresh
immediately, also write the live-state struct field directly (HamletDuFromage
cheat anchors give offsets: flower_coin `live_base + 0xC8`, lives `+0x60`).

## Save-file format (verification + offline editing)

`%APPDATA%\Ryujinx\bis\user\save\0000000000000002\<user>\game_data.sav` —
~21,876 bytes, **plaintext**, magic `04 03 02 01`, ~88% zero. First 0x400 after
the header is **128 × (u32 hash_key, u32 value)** — the same container-A counter
table. Full byte map (header, tables, string blob, trailing per-course arrays:
CourseClear/GoalSeed/WonderSeed/PurpleCoin/ClapperGate stride 4, badges `0x0EA0`,
lives `0x167C`) is in [`reference/save-diff-findings.md`](reference/save-diff-findings.md).

Diff a save before/after an action with `scripts/savediff.py`:
```pwsh
python scripts/savediff.py game_data.sav.pre game_data.sav        # classifies first-acquire / +1 / bit-flip / change
python scripts/savediff.py --summary game_data.sav                # single-save inspection
```
This is how every grant primitive was validated (e.g. flower_coin `6 → 99` at
file offset `0x0894` after `grantContainerACounter(0xf4ee6827, 99)`).
