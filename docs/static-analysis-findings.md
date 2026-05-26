# Static-analysis findings — sprint 2

Append-only log of what each Ghidra/decompiler pass reveals. Mirrors
the style of [save-diff-findings.md](save-diff-findings.md).

Plan + strategy: `~/.claude/plans/i-would-like-to-resilient-pancake.md`.

---

## ★ Summary as of 2026-05-24 (read this first)

The previous M3-grant Ghidra sprint (2026-05-20→21) was declared a
dead-end after 11 scripts. **This sprint succeeded** — the M3.3 grant
API is fully decompiled and ready to wire.

⚠️ **First — what the save-diff sprint (2026-05-22→23) actually
produced.** The save-OUT staging buffer found via the `savedata_id`
UUID heap scan (per [docs/runtime-address-backtrace-plan.md](runtime-address-backtrace-plan.md))
is **NOT a viable target for live grants** — it's the buffer the
game writes TO during save serialization, populated FROM the live
state. Writes into it are overwritten on every subsequent save.
What that work gave us is:

- A **save-file editor** (offline modification of `game_data.sav`).
- A **byte-level verification target** (after a live grant via the
  GameDataMgr API, we can predict and confirm the bytes that will
  appear in the saved file).
- **Mapped save-file offsets** for badges (`0x0EA0`), per-course
  CourseClear / GoalSeed / WonderSeed / PurpleCoin arrays
  (`0x4408` / `0x3360` / `0x3AF8` / `0x1718` etc.), lives (`0x167C`),
  shop wonder-seed flag (`0x3480`), and the BC active-hash slot
  (`0x53E8`).
- **MemetendoYT cross-verification** of the 8 pair-region hash keys
  (flower_coin, regular_coin, 6 Royal Seeds, COMPLETE_GAME, INTRO).

**The save-diff offsets are NOT addresses we can write to at runtime.**
For live grants, the only path is the GameDataMgr API below.

### The grant primitive

`gmd::GameDataMgr::sInstance` lives at NSO `+0x0363F0F0`. The
container-A counter writer is at NSO `+0x0049F648`:

```c
// Lock-free (ARM exclusive-monitor atomics).  Thread-safe.
// Deferred-write: queues to ring buffer at gmd->[+0xf8], drains on next save.
void FUN_710049F648(GameDataMgr* gmd, uint32_t value, uint32_t hash);
```

### Verified hash keys (all in container A; all grantable via the writer above)

| Key | Field | Width in save |
|---|---|---|
| `0xf4ee6827` | flower_coin (purple coins) | u16 @ 0x0894 |
| `0x17f0bb21` | regular_coin | u8 @ 0x08AC |
| `0x55815859` | GRAND_SEED_WORLD1 (Royal Seed W1) | u8 bool (pair region) |
| `0x49abba86` | GRAND_SEED_WORLD2 | u8 bool |
| `0xb550d8d6` | GRAND_SEED_WORLD3 | u8 bool |
| `0x1dcf7f6e` | GRAND_SEED_WORLD4 | u8 bool |
| `0x0d5a3e00` | GRAND_SEED_WORLD5 | u8 bool |
| `0xd4660d2b` | GRAND_SEED_WORLD6 | u8 bool |
| `0x5d3ec9b4` | COMPLETE_GAME | u8 bool |
| `0x89f1cc52` | INTRO_CUTSCENE_COMPLETED | u8 bool |

Flower_coin and regular_coin are confirmed via 3 + 1 call sites in
the binary. The 6 Royal Seeds, COMPLETE_GAME, and INTRO are
high-confidence-but-untested grantable: they live in the same
pair-region container as flower_coin (per save-diff cross-verification
with MemetendoYT save editor) and should be grantable by the same writer.

### The full GameDataMgr API surface

| NSO offset | Role | Signature |
|---|---|---|
| `+0x710012AE94` | Container A counter **READER** | `(gmd, hash, &out)` |
| **`+0x710049F648`** | Container A counter **WRITER** ★ | `(gmd, value, hash)` |
| `+0x71003838AC` | Sub-bool **READER** (INTRO, COMPLETE_GAME reads) | `(sub_obj, &out_byte, hash)` |
| `+0x71003D3FB0` | Stage-info hash → course-index **TRANSLATOR** | `(top_hash, &out_index)` |
| `+0x71003D4110` | **Murmur3-32 hash function** over 81 course names | `(target_hash, &out_index)` |
| Container-B WRITER | **Unknown** — search continues in `FUN_71005E93FC` / one of the other accessors | TBD |

### GameDataMgr struct layout (partially mapped)

| Offset | Contents |
|---|---|
| `+0xe0` | Container A bucket array |
| `+0xec` | Container A bucket count |
| `+0xf0` | Container A dirty-queue capacity |
| `+0xf8` | Container A dirty-queue ring buffer ptr (slot stride 0xc) |
| `+0x100` | Container A dirty-queue head/state word (atomic) |
| `+0x128` | Container A secondary container ("insert new" path) |
| `+0x250..+0x26c` | Container B-1 (simple, struct stride 0x38) |
| `+0x2b0..+0x2cc` | Container B-2 (typed-virtual, struct stride 0x50) |

### Sprint-2 Ghidra scripts (in [scripts/ghidra/](../scripts/ghidra/))

| Script | Purpose |
|---|---|
| `import_sdk_symbols.py` | One-time setup — imports all of `switch-mod/syms/100/*.sym` into Ghidra as labels (the previous sprint's miss) |
| `find_gamedatamgr_xrefs.py` | Walks every xref to `sInstance` to enumerate the GameDataMgr API surface |
| `walk_hash_writer_xrefs.py` | For each accessor, harvests the hash constants used at every call site (cross-checks vs MemetendoYT's 8 keys) |
| `find_offset_constant_xrefs.py` | Scans `.text` for known trailing-region save offsets (badge `0x0EA0`, per-course arrays `0x4408`/`0x3360`/etc.) — written but **not yet run** |
| `playreport_field_backtrace.py` | For each PlayReport field name, traces backward from the `Add` call to find the value-load offset — reveals live-struct layout. Written but not yet run. |

### What's still unknown

1. **Container-B writer** (for non-counter fields). The course-clear
   hash `0xdf82e9ab` and bool fields like INTRO are read via
   container B but the writer hasn't been identified. Candidates:
   `FUN_71005E93FC` (next call in M1 hook chain), or one of the 7
   unprofiled accessors (`0x7100124134`, `0x7100472BE4`,
   `0x7100221128`, `0x7100370264`, `0x71000E258C`, `0x710049EA24`,
   `0x7100533FE4`, `0x71003877C4`).
2. **Field-name hash function**. Murmur3-32 of obvious English
   strings doesn't reproduce MemetendoYT's 8 keys. The field
   names may be in Japanese/internal codenames, or precomputed
   offline. **Not blocking** — we have the hashes.
3. **Whether the deferred-write design causes UI lag**. The dirty
   queue drains at next save; the on-screen counter reads from a
   separate live-state struct (`live_base + 0xC8` for flower_coin
   per the cheat DB). For immediate UI refresh, dual-write may be
   needed. Smoke test will reveal whether this matters in practice.

### Next session's smoke test

```cpp
// In switch-mod/src/program/main.cpp
namespace gmd {
constexpr uintptr_t kSInstance        = 0x0363F0F0;
constexpr uintptr_t kContainerAWriter = 0x0049F648;
constexpr uint32_t kHashFlowerCoin    = 0xf4ee6827;

using SetCounterFn = void (*)(void* gmd, uint32_t value, uint32_t hash);

inline void* Singleton() {
    return *reinterpret_cast<void**>(
        exl::util::modules::GetTargetStart() + kSInstance);
}

inline void GrantContainerA(uint32_t value, uint32_t hash) {
    auto fn = reinterpret_cast<SetCounterFn>(
        exl::util::modules::GetTargetStart() + kContainerAWriter);
    void* s = Singleton();
    if (s) fn(s, value, hash);
}

inline void GrantFlowerCoin(uint16_t total)  { GrantContainerA(total, kHashFlowerCoin); }
}  // namespace gmd
```

Then: call `gmd::GrantFlowerCoin(99)` at boot, save in-game, quit,
diff `game_data.sav`. Expected: `0x0894 = 63 00` (u16 LE = 99).

### Corrections to earlier sprint assumptions

These were guesses by past sessions that turned out wrong; record
here so future me doesn't follow the same dead-ends:

- ❌ "`FUN_71003D3FB0` writes the course-clear field" (per CLAUDE.md note) — wrong; it's a stage-info → course-index translator.
- ❌ "`FUN_71003838AC` is a unified bool get/set" — wrong; it's a reader only.
- ❌ "`FUN_710049F648` takes `(this, hash, value)`" (initial draft after Phase 1.2 harvest) — wrong; the decompile shows `(this, value, hash)`.
- ❌ "SMBW's hash function is none of CRC32/FNV/DJB2/SDBM/Murmur3" (2026-05-21 conclusion) — partially wrong; **Murmur3-32 with seed 0 IS used** (for course names), and the field-name hashes may also be Murmur3 with different strings we haven't found. The original brute-force attempt's Murmur3 implementation may have had bugs or wrong seed.

---

## 2026-05-24 — Pre-Ghidra setup: external anchor harvest

Before running a single Ghidra script, three background research passes
landed and produced concrete anchors the static analysis can feed on.

### A. The big lead: `gmd::GameDataMgr::sInstance`

Hiding in plain sight in [switch-mod/syms/100/gmd/GameDataMgr.sym](../switch-mod/syms/100/gmd/GameDataMgr.sym):

```
_ZN3gmd11GameDataMgr9sInstanceE = __main_start + 0x0363f0f0;
```

This is the **singleton pointer to the master GameDataMgr** — the
class that holds (or points to) the entire live save-data state.

**Why this matters massively:**

- The runtime-address-backtrace plan ([docs/runtime-address-backtrace-plan.md](runtime-address-backtrace-plan.md))
  proposes a multi-step Cheat Engine workflow to find the live state
  base via "find what writes" + multi-boot pointer-scan rescans.
- **That entire workflow is replaced by**:
  ```cpp
  uintptr_t live_state_base = *(uintptr_t*)(GetTargetStart() + 0x0363f0f0);
  ```
  One dereference from the NSO base. Stable per session, anchored to
  the loaded NSO so no pointer-scan required.

- The previous Ghidra sprint never grep'd the sym files for
  `GameData`/`gmd::`/`sInstance` — they only used [sdk.sym](../switch-mod/syms/100/sdk.sym)
  at hook-install time via `nn::ro::LookupSymbol`. The
  `GameDataMgr.sym` file (62 bytes, one line) sat unread.

**Next concrete step:** run [scripts/ghidra/import_sdk_symbols.py](../scripts/ghidra/import_sdk_symbols.py)
to bring all .sym files into Ghidra, then [scripts/ghidra/find_gamedatamgr_xrefs.py](../scripts/ghidra/find_gamedatamgr_xrefs.py)
to enumerate every consumer of the singleton. Each xref's enclosing
function is a candidate hook (especially functions with many xrefs —
those are the save serializer / deserializer / per-field accessors).

Bonus: there's also a [main.sym](../switch-mod/syms/100/main.sym) with
88 NSO-relative symbols (mostly stdlib/RTTI but including `nnMain` at
+0x5a48d0 and `nninitStartup` at +0x613120) and several `sead/` /
`gmd/` sym files (heap, threads, containers). All previously unused.

### B. HamletDuFromage cheat DB — 30+ new NSO code-site anchors

The previous sprint used **one** cheat anchor (+0x12AF6C, the Wonder
Seed counter read site). The full SMBW cheat catalog harvested from
4 community repos contains 30+ load-bearing NSO code-site anchors:

| NSO offset | Cheat name | Pattern | Semantic |
|---|---|---|---|
| **`0x0045AA34`** | Life 99 | `STR W7,[X22,#0x60]` | **Lives writer** — `live_base + 0x60` |
| **`0x00467338`** | 999 coins after Poplin Shop | `STR W2,[X1]` | Coin-balance writer |
| **`0x0049253C`** | Coins (Purple) | `STR W10,[X22,#0xC8]` | **flower_coin writer — `live_base + 0xC8`** |
| `0x00880580` | Star Power | `MOV W21,#1` | Powerup-state writer |
| `0x00198B50` | Mario form swap | `STR W9,[X8,#…]` | Powerup form-byte writer |
| **`0x0048A818`** | Got All Top-of-Flag | `MOV W8,#1` | **Top-of-flag save flag write** |
| `0x0048A528`, `0x005D9F58`, `0x00935E10` | Fast-Travel | `MOV W8,#1` etc. | Course/World unlock save flags |
| `0x00306AEC`, `0x00306B90` | Magnet Coin badge | `MOV W20/21,#1` | Badge enable flags |
| `0x01751DB8` | Wall-Jump badge | `MOV W1,#7` | Wall-jump badge flag |
| `0x0033186C` | Squat-Jump High badge | (mod) | Badge branch |
| `0x002743C0..0x002743C8` | Disable Death | `MOV W11,#1; STRB W11,[X9,#0x1C]` | Death-handler entry — **`live_base + 0x1C` is HP/death byte** |
| `0x000B4B10`, `0x000ED250` | Swimming toggle | `MOV W8,#0` or restore | **`live_base + 0x150` is swim state** |
| `0x001E5D024` | MoonJump core | `LDR D9,[X8,#0xF8]` | **`live_base + 0xF8` is vertical velocity** |

(Full table available in agent output; copy-paste into Ghidra
bookmarks as you investigate each.)

**Key inference: the live game-state struct has at least these
fields at known offsets from a common base register (X22 in the
flower_coin / lives writers; X8/X9 in others)**:

| Live-state offset | Field | Source |
|---|---|---|
| `+0x1C` | HP / death byte | Disable Death cheat |
| `+0x60` | Lives (u32 written) | Life 99 cheat |
| `+0xC8` | flower_coin (u32 written, u16 saved) | Purple Coin cheat |
| `+0xF8` | Vertical velocity (D9 / double) | MoonJump cheat |
| `+0x150` | Swim-state byte | Swimming toggle cheat |

Cross-check candidate: `world_wonder_flower` in PlayReport corpus
(from M2.4) likely maps to `live_base + 0xC8` since flower_coin and
"world_wonder_flower" both equal the purple-coin count.

⚠️ Note: the cheat-DB "live base" may not be the same as the
GameDataMgr singleton — it could be a player-state struct (PlayerInfo)
holding gameplay counters, while GameDataMgr holds the persistent
save-data struct. They may be siblings under a parent or chained
through a pointer. To be verified by running the Ghidra scripts.

### C. MemetendoYT save editor (cross-verified)

Source review of [github.com/MemetendoYT/SMBW-SaveGame-Editor](https://github.com/MemetendoYT/SMBW-SaveGame-Editor)
confirmed:

- **No additional hash keys** beyond the 8 already in
  [docs/save-diff-findings.md](save-diff-findings.md).
- All per-course array bases are correct; stride is 4 bytes.
- **PURPLE_COINS is u16 LE**, not u32 (matters for grant width).
- The W1 course iteration order in the in-file array is
  `001, 002, 004, 005, 003, 200, 013, 007, 008, 009, 006, 010, ...`
  (insertion order in MemetendoYT's hardcoded Dictionary). Use this
  ordering when mapping (file-offset slot index) → (course ID).
- No W2-W6 mapping exists; those still need empirical capture.

### D. SMO sister project — patterns to port

`C:\Users\maxwe\Documents\smo_archipelago\` mirrors our architecture
and has a fully-developed hook framework. Transferable patterns:

| SMO file | What's there | SMBW use |
|---|---|---|
| `switch-mod/src/hooks/HookSymbols.hpp` (595 LoC) | Mangled symbol catalog with Itanium ABI, verified against real `main.nso` | Template for our [switch-mod/src/program/main.cpp](../switch-mod/src/program/main.cpp) hook list once we have more named symbols |
| `switch-mod/src/game/MoonApply.cpp` | `GameDataHolder::mGameDataFile @ +0x20` pattern | Equivalent for SMBW: `GameDataMgr->mSaveData @ +0x??` (TBD via Ghidra) |
| `switch-mod/src/ap/ApState.hpp` | `FlatHashSet<N>` + `SpscRing<T, N>` allocation-free containers | Direct port for M4 bridge state |
| `scripts/check_nso_symbols.py` | Verifies sym files match the NSO via mangled-name lookup | Should be ported / rerun for SMBW to confirm `gmd::GameDataMgr::sInstance` actually resolves at NSO +0x0363f0f0 |

---

## 2026-05-24 — First Ghidra run: GameDataMgr xref enumeration

User ran [import_sdk_symbols.py](../scripts/ghidra/import_sdk_symbols.py)
then [find_gamedatamgr_xrefs.py](../scripts/ghidra/find_gamedatamgr_xrefs.py).
The latter enumerated every consumer of `sInstance` and classified each
xref by what it does immediately after the load.

### Symbol import results

- `gmd/GameDataMgr.sym`: 1 applied (the singleton anchor) ✓
- `main.sym`: 86 applied (33 promoted to functions)
- `sdk.sym`: 0 applied (correctly — those are __sdk_start-relative,
  resolved dynamically at boot, not present in main.nso)
- `sead/*.sym`: 10 symbols across heap/container/thread namespaces

### GameDataMgr API surface (KEY FINDING)

`gmd::GameDataMgr::sInstance` has hundreds of xrefs. The
**dispatch-kind tally** reveals 10+ distinct accessor functions —
this IS the GameDataMgr API. Sorted by call frequency:

| Accessor NSO | Xrefs | Confirmed role |
|---|--:|---|
| `0x710012AE94` | 66 | **Hash-keyed Reader** (M3.3 probe identity). Handles `0xf4ee6827` (flower_coin) ✓, `0x9f5ead3c` (×3 sites), `0xed18dcfe` (×2), and others |
| `0x710049F648` | 46 | TBD — high-traffic, untouched until now |
| `0x7100124134` | 42 | TBD |
| `0x7100472BE4` | 41 | TBD |
| `0x71003838AC` | 38 | **Bool Get** (handles INTRO_CUTSCENE_COMPLETED `0x89f1cc52` ✓) |
| `0x7100221128` | 18 | TBD |
| `0x7100370264` | 16 | TBD |
| `0x71000E258C` | 14 | TBD |
| `0x710049EA24` | 14 | TBD |
| `0x7100533FE4` | 13 | TBD |
| `0x71003877C4` | 10 | TBD |
| `0x710059F894` | (called from M1 hook) | "GameData accessor opener" — confirmed in `SetCourseClearFlagToGameData` body |
| `0x71003D3FB0` | (sibling) | Container B accessor — uses `[+0x260]` displacement, distinct from `0x710012AE94`'s `[+0xe0]` |
| `vtable[0x20]` | 2 | One virtual call — likely a destructor or init hook |
| `no-call` | 257 | sInstance loaded for pointer comparison / NULL check — uninteresting |

⚠️ **Plan correction:** the original [plan](file:~/.claude/plans/i-would-like-to-resilient-pancake.md)
hypothesized `FUN_71003D3FB0` was the universal writer. **It's not.**
It uses `[x20, #0x260]` for its bucket array (vs. `0x710012ae94`'s
`[x20, #0xe0]`) — meaning it's a sibling container accessor, not the
counterpart writer. The actual writer is most likely one of the
top-5 unknown accessors above.

### MemetendoYT 8-key direct sighting

The Ghidra output shows **two of the 8 verified keys appear as
`mov w2, #...` immediates at GameDataMgr xref sites**, locked to
specific accessors:

| Hash | Field | Visible at xrefs calling | Confirmed via |
|---|---|---|---|
| `0xf4ee6827` | flower_coin | **`0x710012AE94`** (the reader) | `FUN_7101c2155c`, `FUN_7101c3f244` |
| `0x89f1cc52` | INTRO_CUTSCENE_COMPLETED | **`0x71003838AC`** (bool getter) | `FUN_7101c63654`, `FUN_7101c6413c` |

This confirms the data-flow: each known key is consumed via a
specific accessor. **To find the Setter** for a known field:
- Find the accessor that READS the field (visible in this list)
- Look for a sibling accessor in the top-10 with the same hash key
  appearing in its xrefs — that's the Setter
- OR: the Getter accessor's call-site contains a stored value (`str
  <reg>, [stack_addr]; ... add x1, sp, #stack_addr; bl getter`) — the
  store-before pattern indicates a Setter overload sharing the function

### Additional hash keys discovered (unknown semantics)

These appear as `mov w2, #imm` immediates at GameDataMgr xrefs but
aren't in MemetendoYT's 8. Candidates for future investigation:

| Hash | Accessor used | Notes |
|---|---|---|
| `0x9f5ead3c` | `0x710012AE94` | appears 3 times — likely a heavily-read field (could be `current_world` or `current_course`) |
| `0xed18dcfe` | `0x710012AE94` | appears 2 times |
| `0x390eb960` | `0x710012AE94` | |
| `0x85da9000` | `0x710012AE94` | |
| `0xd6f631af` | `0x710012AE94` (twice) | |
| `0xd1f27fc9` | `0x710012AE94` | |
| `0xc1a2a9a6` | `0x710012AE94` | |
| `0xdf82e9ab` | (M1 hook context) | **course-clear hash — visible at `FUN_71001bff230` after a read of `0x9f5ead3c`** — possible "if course X cleared then set flag Y" idiom |
| `0xecd38c6a` | `0x7100472BE4` | |
| `0xaae9c08e` | `0x7100472BE4` | |
| `0x42ffdf00` | `0x7100472BE4` | |
| `0xa5301670a` (wait — 0xa530167a) | `0x7100221128` | |
| `0xecea4196` | `0x7100221128` | |
| `0x60e24c90` | `0x7100221128` | |
| `0xcd0b87d1` | `0x71009B4400` (sub-call) | |
| `0xf9f53617` | `0x71009B4400` (sub-call) | |
| `0xe05b4f08` | `0x71003838AC` (bool getter) | another bool flag |
| `0xacd56da2` | `0x71003838AC` (bool getter) | |
| `0x9a1e0d84` | `0x71003838AC` (bool getter) | appears twice — bool flag |
| `0xb472b9bd` / `0x41e51faf` | `0x7100124134` | bytes-style accessor (CSEL pattern) |
| `0x4f1c1277` | (tail-call) | |

### Practical next action

Run the updated [walk_hash_writer_xrefs.py](../scripts/ghidra/walk_hash_writer_xrefs.py)
(now iterates over all 13 known accessors, not just FUN_71003D3FB0).
The output will produce:

1. **Per-accessor xref tables** — each accessor's call sites with
   reconstructed hash constants
2. **MemetendoYT 8-key coverage** — how many of the 8 keys appear
   somewhere in the unified harvest
3. **Distinct-hashes summary** — every hash constant ever observed,
   sorted by callsite count desc

From there: identify which accessor is the SETTER for each (hash,
getter) pair already established. Write a `GrantHashKeyed(hash,
value)` wrapper in [switch-mod/src/program/main.cpp](../switch-mod/src/program/main.cpp)
that calls the appropriate setter via `InstallAtOffset` of the
function pointer.

## 2026-05-24 — Second Ghidra run: hash-writer-xref harvest (BUG, but signal visible)

User ran the multi-accessor [walk_hash_writer_xrefs.py](../scripts/ghidra/walk_hash_writer_xrefs.py).

### Bug in run 1

Output showed all hashes truncated to 16 bits (`0x00006827` instead of
`0xf4ee6827`). Root cause: the AArch64 idiom builds 32-bit constants as

```
mov  w2, #0x6827           ; LOW 16 bits (encoded as MOVZ alias)
movk w2, #0xf4ee, LSL #16  ; HIGH 16 bits
```

The script's `mov` handler returned the low half WITHOUT OR'ing with
the `pending_value` accumulated from a previous `movk` walked-back.
Fixed in walk_hash_writer_xrefs.py — the `mov` branch now ORs with
`pending_value` if present, and the candidate-picking heuristic
prefers values with high bits set.

### Deductions visible despite the bug (16-bit halves still informative)

Each of the 8 MemetendoYT keys has a unique low half. Searching the
output for those low halves cross-references accessors → keys:

| Field | Full hash | Low 16b | Reader accessor | Writer accessor (inferred) |
|---|---|---|---|---|
| flower_coin (u16) | `0xf4ee6827` | `0x6827` | **`0x710012AE94`** (3 callsites) | **`0x710049F648`** (1+ callsite) |
| course-clear | `0xdf82e9ab` | `0xe9ab` | (TBD) | **`0x71003D3FB0`** (3 callsites — confirmed via SetCourseClearFlag M1 hook) |
| INTRO_CUTSCENE | `0x89f1cc52` | `0xcc52` | **`0x71003838AC`** (2 callsites — unified get/set) | **`0x71003838AC`** (same) |

**Strong inference:** GameDataMgr maintains **two parallel hash-keyed
containers** with distinct accessor pairs:

| Container | GameDataMgr offset | Reader | Writer | Purpose |
|---|---|---|---|---|
| **A** (counters) | `[+0xe0]` | `0x710012AE94` (66 xrefs) | `0x710049F648` (46 xrefs) | Holds u16/u32 counters: flower_coin, regular_coin, play_time_sec, etc. |
| **B** (flags) | `[+0x260]` | `0x710049f...` or `0x71005233C0` (sibling) | `0x71003D3FB0` (10 xrefs) | Holds boolean clear-flags: course-clear, Royal-Seed-acquired, INTRO completed, etc. |

(Container B's reader is one of the other top-10 — likely
`0x71005233C0` based on the first scan's internal-structure match
of `[x20, #0x260]`. Need confirmation in next run.)

The bool unified accessor `0x71003838AC` is probably **container B's
get/set entry point** since INTRO_CUTSCENE_COMPLETED (a boolean) is
written and read through it. Container B has TWO known accessors
(`0x71003D3FB0` writer + `0x71003838AC` get/set) — they may handle
different value widths (u8/u32 booleans vs. typed flags).

### Container-B-WRITER (FUN_71003D3FB0) hash dump

| Hash low 16b | Caller | Likely meaning |
|---|---|---|
| `0xad3c` (= low half of `0x9f5ead3c`) | FUN_71003D3EC0, FUN_7101BE2424, FUN_7101BFF230 | (TBD — appears at 3 distinct call sites; high-traffic flag) |
| `0xe9ab` (= low half of `0xdf82e9ab`) | FUN_710157e778, FUN_7101816fec, FUN_7101BF28CC | **course-clear writer call** (FUN_7101BF28CC is `SetCourseClearFlagExecute`, our M1 hook ✓) |
| `0x2c2f` | FUN_710064F0AC, FUN_7101B5C140 | (TBD) |
| `0x7fa9` | (no fn) | (TBD) |
| `0x88d4` | FUN_7101C4B5B4 | (TBD) |

### Container-A-WRITER (FUN_71003D3FB0 NO — that's container B) — actually `0x710049F648`

Top shared keys (16-bit halves) between READER `0x710012AE94` and WRITER `0x710049F648`:

| Hash low 16b | Reader callsites | Writer callsites | Strongest cross-check |
|---|---|---|---|
| `0x6827` (`0xf4ee6827` flower_coin ✓) | 3 | 1+ | **flower_coin GET/SET pair confirmed** |
| `0xb960` | 5 | (no direct setter visible) | TBD |
| `0xad3c` | 5 | (none) | high-traffic counter |
| `0xed18` | 3 | 1 (FUN_710177B7F4) | |
| `0x353b` | 2 | 2 (FUN_71006574C8, FUN_7101B6FB54) | |
| `0xce86` | 2 | 1 (FUN_7100955368) | |
| `0x9000` | 2 | 1 (FUN_71005839C0) | |
| `0xa9a6` | 1 | 1 (FUN_7101C47E80) | |
| `0x9ab1` | 2 | 1 (FUN_71006B5D6C) | |
| `0x47cf` | 1 | 1 | |
| `0x6906` | 1 | 1 (FUN_71014A7FE8) | |
| `0x5add` | 1 | 1 (FUN_7101A5E7A0) | |
| `0xf925` | 1 | 1 | |
| `0x6a36` | 1 | 1 (FUN_71007350F8) | |
| `0x307b` | 1 | 1 (FUN_7101A59D24) | |
| `0x2ea9` | 1 | 1 (FUN_71014A7FE8) | |
| `0x87e6` | 2 | 1 (FUN_7101A612CC) | |
| `0x0af7` | 1 | 1 (FUN_7101A5E7A0) | |

The 18+ shared keys between R and W is strong evidence the pairing is
correct.

### Next concrete actions

1. **Rerun** [walk_hash_writer_xrefs.py](../scripts/ghidra/walk_hash_writer_xrefs.py)
   after the bit-reconstruction fix. Output should show full 32-bit
   hashes, and MemetendoYT 8-key coverage should jump from 0/8 to
   probably 4-6/8 (Royal Seeds may not appear because they're set
   only at first palace boss clear — boot-time + rare events).
2. **Decompile `0x710049F648` in Ghidra/Hex-Rays.** Confirm it's a
   writer (takes a value arg, calls some `store` operation on
   `GameDataMgr.container1`). If yes, **wire `GrantFlowerCoin(N)`**
   in the subsdk by calling it directly: `((void(*)(GameDataMgr*,
   u32, u32))GetTargetStart() + 0x49f648)(GameDataMgr::sInstance,
   0xf4ee6827, new_value)`.
3. **Decompile `0x71003838AC`** to confirm get/set unification and
   bool semantics. INTRO_CUTSCENE is the test case.
4. **Decompile `0x71003D3FB0`** to confirm container-B-writer
   semantics. course-clear hash `0xdf82e9ab` is the test case.
5. **For Royal Seeds**: their hashes (`0x55815859` etc.) probably
   only appear in code that runs after a palace clear — a single
   function that calls writer-B with the world-index-specific hash.
   That function is the Royal-Seed grant target.

### Open: regular_coin (0x17f0bb21) and Royal Seeds — why missing?

MemetendoYT confirmed `regular_coin` exists at file offset `0x08AC`.
But its low 16b `0xbb21` doesn't appear in our scan. Possibilities:
- The setter is called from a code path Ghidra didn't analyze (e.g.,
  inside a switch table). After the fix, increase BACKWALK_INSNS or
  scan for the `movk #0x17f0` pattern directly.
- regular_coin is incremented via a different API (not GameDataMgr —
  maybe through a Player struct field at `+0x60` like Lives, see
  HamletDuFromage anchors).
- The cheat at NSO `+0x467338` (`STR W2,[X1]` for "999 coins after
  Poplin Shop") suggests coins are written to a non-hash-keyed
  location at the live-state struct — confirming the "two paths"
  theory (hash table for save persistence; direct struct field for
  in-game UI).

For Royal Seeds (`0x55815859`..`0xd4660d2b`): their 16-bit lows
(`0x5859`, `0xba86`, `0xd8d6`, `0x7f6e`, `0x3e00`, `0x0d2b`) also
don't appear in the scan. These are written only on palace boss
clears — relatively rare in the game's code paths. Try:
- Increase BACKWALK_INSNS to 32 (for complex prologues with many
  spilled regs).
- Look for `mov w?, #0xX; movk w?, #Y, LSL #16` where Y matches
  the high half of any Royal Seed hash (`0x5581`, `0x49ab`,
  `0xb550`, `0x1dcf`, `0x0d5a`, `0xd466`) — direct binary pattern
  search in Ghidra.

## 2026-05-24 — Third Ghidra run: full 32-bit hashes resolved ★

After the LSL-shift parser fix, the third run resolved ~190 32-bit
hashes across all 13 accessors. **5 of 8 MemetendoYT keys are now
located**, and we have **confirmed reader/writer pairs** for the M3
grant primitives.

### Locked-in accessor roles

| Accessor NSO | Role | Confirmed via |
|---|---|---|
| **`0x710012AE94`** | **Container-A READER** (`Get(gmd, hash, &out_buf)` — hash in w2) | 5× `0xf4ee6827` (flower_coin) + 1× `0x17f0bb21` (regular_coin) |
| **`0x710049F648`** | **Container-A WRITER** (`Set(gmd, hash, new_value)`) | 3× `0xf4ee6827` (flower_coin) — shared 18+ keys with the reader |
| **`0x71003D3FB0`** | **Container-B WRITER** (clear flags, `[+0x260]`) | 3× `0xdf82e9ab` (course-clear, called from our M1 hook FUN_7101bf28cc) |
| **`0x71003838AC`** | **Unified bool GET/SET** (one function handles both read and write per overload) | 2× `0x89f1cc52` (INTRO) + 1× `0x5d3ec9b4` (COMPLETE_GAME) |
| `0x7100124134` | TBD (no MemetendoYT match — handles `0xe237fbc6`, `0x105df820`, `0xe48a1168` × 3 each — likely course/world enums) | – |
| `0x7100472BE4` | TBD (handles `0x638a4ca3` × 5, `0x638a4ca3`/`0x920934d0` paired) | – |
| `0x7100221128` | TBD (handles `0xd32edb2d` × 3, struct-field array writer family) | – |
| `0x7100370264` | TBD (handles `0xdcf45353` × 3, `0x7940dc77` × 3 — looks like a 3-callsite distributed write per key) | – |
| `0x71000E258C` | TBD (handles `0x948e540d` × 3) | – |
| `0x710049EA24` | TBD (handles `0xfd393625` × 2, plus enum-int writes) | – |
| `0x7100533FE4` | TBD (handles **`0xab5acd0d` × 10!** in many fns — extremely hot single key, likely a state-machine event) | – |
| `0x71003877C4` | TBD (handles small int constants, probably an enum bool getter) | – |
| `0x710059F894` | "GameData accessor opener" (called by SetCourseClearFlag at +0x1bf28cc) — likely just opens a transaction / acquires a lock; the actual work goes via 0x71003D3FB0 | – |

### Drafting the M3 grant primitives

We have **enough information to write working grant code RIGHT NOW**
for the following items, all called from the subsdk via `InstallAtOffset`-
resolved function pointers:

```cpp
// In switch-mod/src/program/main.cpp:

#include "lib/util/sys/mem_layout.hpp"  // for GetTargetStart()

namespace gmd_grants {

// Resolved at first use; cached.
using GmdReadFn  = bool (*)(void* gmd, uint32_t hash, uint32_t* out);
using GmdWriteFn = bool (*)(void* gmd, uint32_t hash, uint32_t value);

// NSO offsets — confirmed via Ghidra walk_hash_writer_xrefs.py 2026-05-24.
static constexpr uintptr_t OFF_SINSTANCE     = 0x0363f0f0;
static constexpr uintptr_t OFF_CONTAINER_A_R = 0x0012ae94;  // reader
static constexpr uintptr_t OFF_CONTAINER_A_W = 0x0049f648;  // writer ★
static constexpr uintptr_t OFF_CONTAINER_B_W = 0x003d3fb0;  // course-clear etc.
static constexpr uintptr_t OFF_BOOL_GETSET   = 0x003838ac;  // unified bool

// MemetendoYT keys (verified via save-diff + Ghidra cross-ref):
static constexpr uint32_t HASH_FLOWER_COIN    = 0xf4ee6827;  // u16 in save
static constexpr uint32_t HASH_REGULAR_COIN   = 0x17f0bb21;  // u8 in save
static constexpr uint32_t HASH_COURSE_CLEAR   = 0xdf82e9ab;  // global bool
static constexpr uint32_t HASH_INTRO_DONE     = 0x89f1cc52;
static constexpr uint32_t HASH_COMPLETE_GAME  = 0x5d3ec9b4;

static void* GetGameDataMgr() {
    auto sInstancePtr = reinterpret_cast<void**>(GetTargetStart() + OFF_SINSTANCE);
    return *sInstancePtr;
}

bool GrantFlowerCoin(uint32_t new_total) {
    auto write = reinterpret_cast<GmdWriteFn>(GetTargetStart() + OFF_CONTAINER_A_W);
    void* gmd = GetGameDataMgr();
    return write(gmd, HASH_FLOWER_COIN, new_total);
}

bool GrantCourseClearGlobalFlag() {
    auto write = reinterpret_cast<GmdWriteFn>(GetTargetStart() + OFF_CONTAINER_B_W);
    void* gmd = GetGameDataMgr();
    return write(gmd, HASH_COURSE_CLEAR, 1);
}

// COMPLETE_GAME and INTRO_DONE go through the bool accessor — but its
// arg order may differ (likely (gmd, hash, value_byte)).  Decompile
// FUN_71003838AC in Ghidra to confirm before wiring.

}  // namespace gmd_grants
```

**Verification protocol** (before committing):
1. Decompile `FUN_710049F648` in Ghidra. Confirm signature is
   `bool Set(gmd*, u32 hash, u32 value)` (or whatever — note the
   actual signature here).
2. Add a debug hook at boot: call `GrantFlowerCoin(99)`. Save the
   game, quit, diff `game_data.sav` — `0x0894` should now be 99 (u16
   little-endian: bytes `63 00`).
3. If successful, the same pattern wires regular_coin, INTRO,
   COMPLETE_GAME, and the global course-clear flag.

### Royal Seeds — still missing (but explained)

None of the 6 Royal Seed hashes (`0x55815859`..`0xd4660d2b`) appeared
in the scan. Three possible reasons:

1. **They're stored in a `.rodata` lookup table** and loaded via
   `adrp x?, .rodata_hashes; ldr w?, [x?, #N]`. The hash key is
   never built via `mov+movk`. Our walker only handles immediate
   builds.
2. **They're computed at runtime** from string names like
   `"GRAND_SEED_WORLD1"` via the hash function we haven't recovered
   (Phase 3 of the original plan).
3. **Their writer is called from an extremely rare code path** that
   Ghidra's analysis missed (e.g., a switch-table case for palace
   index).

**Next action for Royal Seeds:** binary-search `.rodata` for the
exact 4-byte LE patterns:
- `59 58 81 55` (W1)
- `86 ba ab 49` (W2)
- `d6 d8 50 b5` (W3)
- `6e 7f cf 1d` (W4)
- `00 3e 5a 0d` (W5)
- `2b 0d 66 d4` (W6)

Whichever appear in `.rodata` give us xrefs to the loader. Add a
new script `find_rodata_hash_table.py` for this — Phase 3 promoted
to "now".

Pragmatic fallback: since we already have the file-offset writers
(per `docs/save-diff-findings.md`: writing `1` to `GRAND_SEED_WORLD{N}`
file offsets via the runtime address anchor), Royal Seeds are
already grantable via direct memory write. Hash-table-API grant
would be cleaner but isn't blocking.

### Key high-value side findings

- **`0x9f5ead3c`** appears 11 times (top of unknown-keys list) — read
  by Container-A reader (`0x710012AE94`) **AND** written by
  Container-B writer (`0x71003D3FB0`). Same hash key mirrored across
  both containers? Or two different fields with the same hash low
  bits? (Unlikely — a 32-bit collision is statistically rare.) Most
  likely: a single field that's mirrored across both containers, OR a
  shared "last_event" / "last_world" telemetry field. **Worth checking
  what 0x9f5ead3c semantically is** — it could be `current_course` or
  `current_world` based on PlayReport `stage_info.stage_key`.

- **`0xab5acd0d`** appears 10 times under `0x7100533FE4` only — a
  single key dominating one accessor. Likely a state-machine event ID
  (e.g., `last_event_id`).

- **`0x000bcbd7`** appears in both bool getter and accessor C — a bool
  that's also read in a non-bool path? Worth tracing.

### Refined `GrantHashKeyed` API

With Containers A and B both writable:

```cpp
enum class GmdContainer { A, B };

bool GmdSet(GmdContainer c, uint32_t hash, uint32_t value) {
    void* gmd = GetGameDataMgr();
    if (!gmd) return false;
    uintptr_t fn_off = (c == GmdContainer::A) ? OFF_CONTAINER_A_W
                                              : OFF_CONTAINER_B_W;
    auto fn = reinterpret_cast<GmdWriteFn>(GetTargetStart() + fn_off);
    return fn(gmd, hash, value);
}
```

This is a complete generic-grant primitive for all hash-keyed
save-data fields, modulo type-width differences (the writer
probably takes u32 — for u16 values like flower_coin or u8 like
regular_coin, the value is truncated by the writer's internal logic
when it stores to the typed slot).

### Status: M3.3b grant code becomes write-and-test

After 2 Ghidra script iterations and 3 runs, **we have grant
primitives ready for badging into [switch-mod/src/program/main.cpp](../switch-mod/src/program/main.cpp).**
The pivotal step now is decompiling `0x710049F648` and `0x71003D3FB0`
to lock in the exact signatures, then writing the C++ wrapper and
testing end-to-end with `GrantFlowerCoin(99)`.

## 2026-05-24 — `FUN_710049F648` decompiled — signature LOCKED IN ★★

User pasted Ghidra's decompilation of `FUN_710049F648`. Three big
findings:

### Signature: `(this, value, hash)` — VALUE FIRST

```c
void FUN_710049F648(GameDataMgr* this,   // param_1 = x0
                    uint32_t value,       // param_2 = w1
                    uint32_t hash);       // param_3 = w2
```

★ **My earlier draft had `(this, hash, value)` — wrong.** The
xref-walker correctly identified `w2` as the register holding the
hash constant, but I assumed standard `(this, key, value)` ordering
without confirming. The decompilation shows `param_2` (w1) is the
value and `param_3` (w2) is the hash.

This MATTERS for the grant code. The corrected primitive:

```cpp
typedef void (*GmdContainerASet_t)(void* gmd, uint32_t value, uint32_t hash);

bool GrantFlowerCoin(uint16_t new_total) {
    auto set = reinterpret_cast<GmdContainerASet_t>(
        GetTargetStart() + 0x0049F648);
    void* gmd = *reinterpret_cast<void**>(
        GetTargetStart() + 0x0363F0F0);
    if (!gmd) return false;
    set(gmd, new_total, 0xf4ee6827);
    return true;
}
```

### Function is lock-free + deferred-write

The body uses ARM exclusive-monitor instructions
(`ExclusiveMonitorPass` / `ExclusiveMonitorsStatus` = `LDXR` / `STXR`
pair). It does NOT write to the live state directly. Instead it:

1. Hashes into the bucket array at `this->[+0xe0]`
   (bucket count at `this->[+0xec]`).
2. Linear-probes 8-byte buckets to find the matching hash.
3. **Atomically reserves a slot** in the dirty-queue ring buffer at
   `this->[+0xf8]` (head pointer at `this->[+0x100]`, capacity at
   `this->[+0xf0]`, slot stride 0xc = 12 bytes = `(u32 value, u32 hash, u32 ?flags)`).
4. Writes `(value, hash)` into the reserved slot.

If the hash isn't in the main bucket array, falls through to
`FUN_710049F750(this + 0x128, value, hash, 0)` — the "insert new
entry" path. `this + 0x128` is a secondary container (probably a
linked list or smaller hash set for entries not yet rehashed into
the main array).

**Implications for grant code:**
- ★ **Thread-safe.** Can be called from any subsdk thread (no
  game-side mutex required).
- The save WILL persist correctly on next game save (the dirty queue
  is drained at save-time).
- The **on-screen UI counter may not refresh immediately** because the
  live game-state struct (e.g., `live_base + 0xC8` for flower_coin,
  per the cheat DB) is a separate copy that the game reads each frame
  to draw the HUD.

### Dual-write strategy for live UI updates

For items where the UI needs to refresh immediately (purple coin
counter, lives, etc.), we need to write BOTH:

1. **The hash-table** via `FUN_710049F648(gmd, value, hash)` — persists to save.
2. **The live-state field** at `live_base + offset` — updates UI.

Where `live_base` and the offsets come from the HamletDuFromage
cheat DB anchors:

| Field | Hash (container-A) | Live offset (from cheat DB) | Cheat anchor proving it |
|---|---|---|---|
| flower_coin | `0xf4ee6827` | `live_base + 0xC8` | NSO `+0x0049253C` `STR W10,[X22,#0xC8]` |
| lives | (not in container-A — separate file offset `0x167C`) | `live_base + 0x60` | NSO `+0x0045AA34` `STR W7,[X22,#0x60]` |
| HP / death byte | (game-only, not in save) | `live_base + 0x1C` | NSO `+0x002743C0..C8` |

`live_base` here is `*(void**)(GameDataMgr.sInstance + ???)` —
the per-player state struct. We need to find which member of
GameDataMgr points to it. **Likely candidate based on the new
decompile: container-A IS the persistent backing for the live state,
but the live state has its OWN struct with flat fields for fast
gameplay access.** They're synchronized at save/load time.

To find `live_base` derivation: search the decompiled
`FUN_710049253C` (the flower_coin write cheat anchor — search the
function containing that store) — x22 will have been loaded from
somewhere accessible via the gmd singleton. That trace gives us the
offset.

### Refined draft for [switch-mod/src/program/main.cpp](../switch-mod/src/program/main.cpp)

```cpp
// ===== GameDataMgr grant primitives (M3.3 / M3.3b) =====
// Static-analysis sprint 2 result — 2026-05-24.

namespace gmd {

// NSO-relative offsets (v1.0.0, BID CD6E42AEE7934F4D).
constexpr uintptr_t kSInstance        = 0x0363F0F0;
constexpr uintptr_t kContainerAReader = 0x0012AE94;  // unused for grants
constexpr uintptr_t kContainerAWriter = 0x0049F648;  // ★ counter writes
constexpr uintptr_t kContainerBWriter = 0x003D3FB0;  // ★ flag writes
constexpr uintptr_t kBoolGetSet       = 0x003838AC;  // ★ bool overload

// Hash keys (verified via Ghidra + MemetendoYT cross-ref).
constexpr uint32_t kHashFlowerCoin   = 0xf4ee6827;  // u16 in save
constexpr uint32_t kHashRegularCoin  = 0x17f0bb21;  // u8 in save
constexpr uint32_t kHashCourseClear  = 0xdf82e9ab;  // shared bool
constexpr uint32_t kHashIntroDone    = 0x89f1cc52;
constexpr uint32_t kHashCompleteGame = 0x5d3ec9b4;

using SetCounterFn = void (*)(void* gmd, uint32_t value, uint32_t hash);
using SetFlagFn    = void (*)(void* gmd, uint32_t value, uint32_t hash);

inline void* GetSingleton() {
    return *reinterpret_cast<void**>(
        exl::util::modules::GetTargetStart() + kSInstance);
}

void SetCounter(uint32_t value, uint32_t hash) {
    auto fn = reinterpret_cast<SetCounterFn>(
        exl::util::modules::GetTargetStart() + kContainerAWriter);
    void* gmd = GetSingleton();
    if (gmd) fn(gmd, value, hash);
}

void SetFlag(uint32_t value, uint32_t hash) {
    auto fn = reinterpret_cast<SetFlagFn>(
        exl::util::modules::GetTargetStart() + kContainerBWriter);
    void* gmd = GetSingleton();
    if (gmd) fn(gmd, value, hash);
}

// Convenience wrappers — type-cast value to its save-format width.
inline void GrantFlowerCoin(uint16_t total)  { SetCounter(total, kHashFlowerCoin); }
inline void GrantRegularCoin(uint8_t total)  { SetCounter(total, kHashRegularCoin); }
inline void SetCourseClearedGlobal()         { SetFlag(1, kHashCourseClear); }
inline void SetIntroCutsceneDone()           { SetFlag(1, kHashIntroDone); }
inline void SetCompleteGame()                { SetFlag(1, kHashCompleteGame); }

}  // namespace gmd
```

### Verification protocol

1. **Decompile `FUN_71003D3FB0` next** to confirm container-B writer
   has the same `(this, value, hash)` signature.
2. **Build and deploy** with a one-shot test: at first frame, call
   `gmd::GrantFlowerCoin(99)`. Save the game, quit, diff
   `game_data.sav`:
   - Expected: file offset `0x0894` becomes `63 00 00 00`
     (u16 LE for 99).
3. If the save shows 99 but the in-game counter still shows the old
   value, the **dual-write** path is needed (also patch
   `live_base + 0xC8`). For the MVP, save-only is fine — UI will
   refresh on next overworld load or course entry.

## To-fill sections (filled in as the Ghidra scripts run)

### Phase 1.1b — `FUN_71003D3FB0` decompiled: it's a DISPATCHER, not a simple writer ★

User pasted Ghidra's decompile of `FUN_71003D3FB0`. **The signature
is completely different from `FUN_710049F648`:**

```c
undefined8 FUN_71003D3FB0(uint   hash,          // param_1 = w0
                          undefined8 unknown);  // param_2 = x1
```

- **Only 2 params** (not 3) — the GameDataMgr `sInstance` is accessed
  via the global `_ZN3gmd11GameDataMgr9sInstanceE` directly, not
  passed as `this`.
- **Hash is in w0**, not w2 (different convention from container-A
  writer). Our xref walker's multi-register fallback correctly
  handled this; that's why we got `0xdf82e9ab` despite the different
  signature.
- **`param_2` is undefined8** — could be a pointer (likely) or a
  value. Without decompiling `FUN_71003D4110` (the inner call), we
  can't determine the exact semantics.

**What the function actually does** (it's a 2-tier router):

```text
1. Look up `hash` in container at sInstance + 0x260
   - bucket array at +0x258 (sInstance + 600)
   - bucket count at +0x26c
   - struct array stride 0x38, element field at +0x1c
   - On match: tail-call FUN_71003D4110(*field_at_+0x1c, param_2)
   - This is the "simple direct" path.

2. If not found, look up `hash` in container at sInstance + 0x2c0
   - bucket array at +0x2b8
   - bucket count at +0x2cc
   - struct array stride 0x50
   - On match:
       - Read index, look up struct
       - VIRTUAL DISPATCH: call **(struct + 0x20)()
       - If returns < 1, abort with 0
       - Otherwise tail-call FUN_71003D4110(*field_at_+0x28, param_2)
   - This is the "typed virtual" path — used for fields that need
     type-aware handling (probably bools that come through specific
     read/write methods).
```

So `FUN_71003D3FB0` is a **dispatcher** that routes hash-keyed
operations to per-field handlers. The actual write/read is done by
`FUN_71003D4110` (or virtual methods on container-2 entries).

### What this means for grant code

★ **We cannot easily wire `SetCourseClearedGlobal()` via `FUN_71003D3FB0`**
without first understanding `FUN_71003D4110`'s signature and what
`param_2` represents. The dispatcher's `param_2` is forwarded
unchanged — so the answer depends on the inner function.

Two next steps in priority order:

1. **Decompile `FUN_71003D4110`** to see its arg list. If it's
   `(u32 dest_field, u32 value)` or `(u32 dest_field, u8 value)`,
   then param_2 (in `FUN_71003D3FB0`) is the new value — meaning
   the call is `FUN_71003D3FB0(0xdf82e9ab, new_value)`. Straightforward.
2. **Decompile `FUN_71003838AC`** (the unified bool get/set) —
   this handled INTRO_CUTSCENE and COMPLETE_GAME and may turn out
   to be a much simpler API for boolean fields.

### Updated grant code: use container-A writer ONLY for now

The MVP grant primitives are SOLID for container-A counters
(flower_coin, regular_coin, etc.) via `FUN_710049F648`. Container-B
fields (course-clear, INTRO, COMPLETE_GAME) need one more decompile
pass before we can wire them safely.

```cpp
namespace gmd {

// Confirmed signature (decompile 2026-05-24):
//   void FUN_710049F648(GameDataMgr* this, u32 value, u32 hash)
using ContainerASetFn = void (*)(void* gmd, uint32_t value, uint32_t hash);

// FUN_71003D3FB0 has signature (hash, param_2) with sInstance accessed
// globally — different from container-A.  Defer wiring until
// FUN_71003D4110 is decompiled.

void GrantContainerA(uint32_t value, uint32_t hash) {
    auto fn = reinterpret_cast<ContainerASetFn>(
        exl::util::modules::GetTargetStart() + 0x0049F648);
    void* gmd = *reinterpret_cast<void**>(
        exl::util::modules::GetTargetStart() + 0x0363F0F0);
    if (gmd) fn(gmd, value, hash);
}

inline void GrantFlowerCoin(uint16_t total)  { GrantContainerA(total, 0xf4ee6827); }
inline void GrantRegularCoin(uint8_t total)  { GrantContainerA(total, 0x17f0bb21); }

}  // namespace gmd
```

### Side-finding: GameDataMgr struct layout (from container-B decompile)

The decompile reveals more of the singleton's struct shape:

| Offset | Meaning |
|---|---|
| `+0xe0` | Container A bucket array ptr (counters) — confirmed |
| `+0xe8` | Container A ??? |
| `+0xec` | Container A bucket count — confirmed |
| `+0xf0` | Container A dirty-queue capacity |
| `+0xf8` | Container A dirty-queue base ptr (slot stride 0xc) |
| `+0x100` | Container A dirty-queue head/state word (atomic) |
| `+0x128` | Container A secondary container ("insert new") |
| `+0x250` | Container B-1 struct-array count |
| `+0x258` | Container B-1 struct-array ptr (stride 0x38, field at +0x1c) |
| `+0x260` | Container B-1 bucket array ptr |
| `+0x26c` | Container B-1 bucket count |
| `+0x2b0` | Container B-2 struct-array count |
| `+0x2b8` | Container B-2 struct-array ptr (stride 0x50, field at +0x28, vtable at +0x20) |
| `+0x2c0` | Container B-2 bucket array ptr |
| `+0x2cc` | Container B-2 bucket count |

**Three containers total** inside GameDataMgr:
- **A** (counters, lock-free deferred-write)
- **B-1** (simple direct: hash → u32 field → FUN_71003D4110)
- **B-2** (typed virtual: hash → vtable → conditional → field → FUN_71003D4110)

The course-clear hash `0xdf82e9ab` is in either B-1 or B-2 (need runtime instrumentation or string-grep to determine).

## 2026-05-24 — FUN_71003D4110 and FUN_71003838AC decompiled

Two more decompilations landed. Both reveal **they're READERS, not
writers** — significantly reframing the API picture.

### `FUN_71003D4110` is the **Murmur3-32 hash function** ★★

The constants are textbook Murmur3-32:
- `0xcc9e2d51` (c1 mix) — shown as `-0x3361d2af`
- `0x1b873593` (c2 mix)
- `0xe6546b64` (block-mix add)
- `0x85ebca6b` (finalization 1) — shown as `-0x7a143595`
- `0xc2b2ae35` (finalization 2) — shown as `-0x3d4d51cb`
- Rotations: 15 (for ROL via shift-and-OR with shift-17), 13, 16

The function iterates **81 hardcoded course-name strings** at
`PTR_s_Course1_71034dec90` (Course1 ... Course81). For each, it
computes `Murmur3(name)` with seed 0, compares against `param_1`,
and on match writes the array index to `*param_2`.

**Implication:** `FUN_71003D4110` is a **course-name → course-index
translator** keyed on Murmur3 of course-name strings. It is NOT a
generic field-name hash function and NOT the writer.

Verified my Python Murmur3-32 against canonical reference vectors
(empty → 0, `"hello"` → `0x248bfa47`, `"a"` → `0x3c2569b2`,
`"Hello, world!"` → `0xc0363e43`). Algorithm matches.

### The field-name hash space is NOT Murmur3-of-obvious-name

Computing `Murmur3` of obvious field-name strings does NOT produce
MemetendoYT's keys:
- `Murmur3("flower_coin")` ≠ `0xf4ee6827`
- `Murmur3("PURPLE_COINS")` ≠ `0xf4ee6827`
- `Murmur3("GRAND_SEED_WORLD1")` ≠ `0x55815859`

So the field-name hashes use either (a) the same Murmur3 with
unknown internal strings (possibly Japanese romaji or codenames),
(b) precomputed constants generated offline and stored as static
data, or (c) a different hash function altogether.

★ **This is NOT blocking.** We already have the 8 verified hashes
from MemetendoYT. The grant primitive `FUN_710049F648(gmd, value,
hash)` works for any hash we already know — no string-reverse-
engineering needed.

### `FUN_71003838AC` is the bool READER, not a setter ★

```c
undefined8 FUN_71003838AC(long sub_obj,          // param_1 = x0 (sub-object)
                          byte* out_byte,         // param_2 = x1 (output)
                          uint32_t hash);         // param_3 = w2
```

Same 2-tier dispatcher structure as `FUN_71003D3FB0`:

1. **Sub-container 1** at `sub_obj + 0x20` (count `+0x2c`, struct
   array `+0x18` stride 0x18, byte field at struct offset `+0x16`):
   linear-probe → read byte → write to `*out_byte` → return 1.
2. **Sub-container 2** at `sub_obj + 0x80` (count `+0x8c`, struct
   array `+0x78` stride 0x40, vtable at `+0x20`, value at `+0x28`):
   linear-probe → virtual dispatch on `[struct+0x20]()` (must return
   > 0) → read `*(byte*)(struct+0x28) & 1` → write to `*out_byte` →
   return 1.

★ Signature reveals this is the bool **GETTER**, not setter. INTRO
and COMPLETE_GAME callers were READING those flags (probably as
gates: "is the intro done?", "is the game complete?"), not writing.

**`param_1` is a sub-object**, not GameDataMgr itself — the offsets
(`+0x20`, `+0x80`, `+0x18`, `+0x78`) don't match any GameDataMgr
field we've mapped. It's a "bool table" object that GameDataMgr
holds at some inner offset, accessed by callers via a getter
on GameDataMgr that returns this sub-object pointer.

### Re-interpretation of `FUN_71003D3FB0` (it's NOT a writer either)

Now that we know `FUN_71003D4110` is the Murmur3 hash function:

```text
FUN_71003D3FB0(top_level_hash, output_ptr):
  Look up `top_level_hash` in container B-1 (sInstance + 0x260).
    On match: read SECONDARY hash from struct[+0x1c].
              Call FUN_71003D4110(secondary_hash, output_ptr).
              FUN_71003D4110 writes course-INDEX to *output_ptr.
              Return its result (1 if found, 0 if not).
  Else look up in container B-2 (sInstance + 0x2c0).
    On match: virtual dispatch, then read SECONDARY hash from
              struct[+0x28].  Call FUN_71003D4110 same way.
```

★ **So `FUN_71003D3FB0` is "lookup a course index by stage-info
hash"**, NOT a writer. The M1 hook chain at NSO `+0x1bf28cc` calls
it to figure out "which course is currently active" before writing
the clear flag elsewhere. The actual write happens in
`FUN_71005E93FC` (the next `bl` in the chain) or in another
function entirely.

### Updated API map (post-decompile)

| Function | Role | Status |
|---|---|---|
| `0x710012AE94` | Container A **counter READER** `(gmd, hash, out_ptr)` | ★ in use |
| **`0x710049F648`** | Container A **counter WRITER** `(gmd, value, hash)` | ★★ **CONFIRMED — only confirmed grant primitive** |
| `0x71003838AC` | Sub-bool **READER** `(sub_obj, out_byte, hash)` | reader, not writer |
| `0x71003D3FB0` | Stage-info → course-index **TRANSLATOR** | reader, not writer |
| `0x71003D4110` | **Murmur3-32(course_name) → course_index** | hash function, not writer |
| `0x71005E93FC` | (TBD) — likely the **actual flag WRITER** in M1 hook chain | next decompile target |
| `0x710059F894` | (TBD) — "open GameData accessor" | next decompile target |
| Container-B WRITER | NOT YET IDENTIFIED — must be one of the 7 unprofiled accessors or `FUN_71005E93FC` | — |

### Practical recommendation: ship the M3.3 counter grants now

We have enough to wire and TEST these primitives in `main.cpp`:

```cpp
// M3.3 counter grants (CONFIRMED via Ghidra decompile 2026-05-24)
gmd::GrantFlowerCoin(99);   // → save file 0x0894 = 99 (u16 LE)
gmd::GrantRegularCoin(255); // → save file 0x08AC = 255 (u8)

// All 6 GRAND_SEED_WORLD{N} keys probably grantable via the same
// primitive (same container A, same writer), since MemetendoYT
// confirmed they live in the pair region with the same shape as
// flower_coin / regular_coin.  Try:
gmd::GrantContainerA(1, 0x55815859);  // Royal Seed W1
```

The Royal Seed test will validate the theory that container A also
holds the seed bools (with the writer truncating u32 → u8 internally
when the typed slot is u8). If it works, **M3.3 + M3.3b are
simultaneously unlocked** without needing to identify a container-B
writer at all.

### Phase 1.1e (TODO) — Decompile `FUN_71005E93FC`

The M1 hook body (CLAUDE.md) calls it AFTER `FUN_71003D3FB0`:

```
bl FUN_710059F894            ; open GameData accessor
mov w0, #0xdf82e9ab          ; ?
bl FUN_71003D3FB0            ; lookup current course index (we now know this)
bl FUN_71005E93FC            ; ← likely WHERE THE WRITE HAPPENS
tbz w0, #0, fail_path        ; check return
```

If this turns out to be the "write course-clear flag for the
current course" function with signature `(course_index)` or
`(course_index, value)`, it would unlock per-course flag writes —
a useful complement to the trailing-region u32 array writes
identified by `find_offset_constant_xrefs.py`.

### Phase 1.1f (TODO) — Decompile `FUN_710059F894`

The "GameData accessor opener" — likely acquires a write-context or
mutex. Understanding it tells us whether grant code needs to bracket
writes with a paired open/close, or whether `FUN_710049F648`'s
self-contained lock-free design makes that unnecessary.

### Phase 1.3 — Wire `GrantFlowerCoin(99)` smoke test (next session)

The end-to-end test that proves the entire static-analysis sprint:

1. Add `gmd::` namespace to [switch-mod/src/program/main.cpp](../switch-mod/src/program/main.cpp)
   per the draft code in this doc.
2. Add a one-shot hook at boot or at first WONDER_SEED_AWARDED that
   calls `gmd::GrantFlowerCoin(99)`.
3. Build, deploy, run in Ryujinx. Play any course briefly to trigger
   a save.
4. Quit and diff `game_data.sav` against a known baseline.
   - Expected: `0x0894` reads `63 00 00 00` (u16 LE = 99).
   - Expected: in-game purple coin counter shows 99 next overworld
     load (UI may have one-frame lag if the live struct isn't
     dual-written).
5. If success → repeat for Royal Seed W1: `GrantContainerA(1, 0x55815859)`,
   then check pair-region offset `0x0350` (per save-diff "Pair-key
   sanity check") flips from 0 → 1.

### Phase 1.3 — Generic-grant subsdk hook

(TODO — once Phase 1.2 confirms calling convention, draft `GrantHashKeyed`
as a new helper in [switch-mod/src/program/main.cpp](../switch-mod/src/program/main.cpp).)

### Phase 2 — Per-course u32-array writers

(TODO — run [scripts/ghidra/find_offset_constant_xrefs.py](../scripts/ghidra/find_offset_constant_xrefs.py)
and identify serializer (cluster of offsets in one fn) vs gameplay
writers (isolated offsets).)

### Phase 4.2 — Diaphora SMBW ↔ SMO

(TODO — set up BinExport on both NSOs, import into Diaphora, harvest
function correspondences for the `gmd::` / `nn::fs::SaveData` /
hash-counter family.)

### Phase 5 — PlayReport backwards walk

(TODO — run [scripts/ghidra/playreport_field_backtrace.py](../scripts/ghidra/playreport_field_backtrace.py).
Per the cheat-DB anchor at +0x49253C: expect `world_wonder_flower` to
load from `[x22, #0xC8]` — if confirmed, x22 == flower_coin live-state
base (likely == GameDataMgr->mSaveData->mPlayerState or similar nested
struct).)

---

## 2026-05-24 — M3.2 badge-grant sprint kickoff: scoping + strategy

The sprint-2 GameDataMgr API decompile (above) unlocked **counter
grants** via `FUN_710049F648(gmd, value, hash)` for container-A fields.
**Badges are NOT in container A** — see scoping below. This section
plans the static analysis for the badge grant path.

### What we know about badges (from save-diff + PlayReport)

- Badge ownership is a **u64 bitfield** at save file offset `0x0EA0`,
  in the **trailing region** (file `>= 0xBF0`).
- Bit positions are sparse SMBW internal badge IDs. Confirmed mappings:
  Coin Reward = 9, Auto Super Mushroom = 46; Wall-Climb + Parachute Cap
  share {34, 35} (specific pairing TBD). Pre-existing bits in user's
  save = 9, 34, 35, 45, 46.
- A separate **"badges ever equipped" bitfield** at `0x0C58..0x0C63`
  (cleared bits = badges previously equipped).
- A separate **"currently equipped" name-hash slot** at `0x16B8` (a
  single u32 hash, not the internal ID).
- PlayReport `course_in` emits `equip_badge_id=[N]` (the equipped
  badge's internal ID).
- PlayReport `course_result` emits `badge_id_array=[ids...]` (the
  owned badges at clear time).
- The HamletDuFromage cheat DB has **no badge OWNERSHIP cheats** —
  only badge-EFFECT cheats (force Magnet Coin behavior, Wall-Jump
  behavior, etc.) at `0x00306AEC`, `0x00306B90`, `0x01751DB8`,
  `0x0033186C`. These don't grant ownership; they bypass the
  ownership check.

### Why container-A grant doesn't cover badges

The pair region of `game_data.sav` (file `0x028..0xBF0`) is the
serialized form of GameDataMgr's container A + B. Per save-diff
findings:

- All 8 MemetendoYT keys (flower_coin, regular_coin, 6 Royal Seeds,
  COMPLETE_GAME, INTRO_CUTSCENE_COMPLETED) live in this pair region.
- The badge bitfield at `0x0EA0` is in the **trailing region** (file
  `>= 0xBF0`), which serializes a DIFFERENT struct family — fields
  with fixed file offsets, not hash-keyed lookups.

So the badge bitfield is held in some `gmd::` sub-object (or sibling
struct GameDataMgr points to) that gets serialized to the trailing
region at save time. **It's not container A or B.** The M3.3 grant
primitive `FUN_710049F648(gmd, value, hash)` cannot grant badges.

### Negative confirmation: Murmur3 brute-force (run 2026-05-24)

Ran [scripts/brute_badge_field_hashes.py](../scripts/brute_badge_field_hashes.py)
to rule out the cheap hypothesis "maybe badges ARE in container A under
a hash we haven't named yet." The script computes Murmur3-32 (seed 0,
the algorithm confirmed in `FUN_71003D4110`) of **710 candidate badge
field name strings** (English variants + Japanese loanwords + numbered
slots like `BADGE_0..63` matching the `GRAND_SEED_WORLD%d` pattern)
and cross-checks against the **22 unknown 32-bit hash keys** harvested
from `walk_hash_writer_xrefs.py`.

**Result: zero hits.** None of the candidate names reproduce any of the
22 unknown Ghidra-observed hashes. Possible interpretations:

1. **Badge field name is Japanese / internal codename** not in our
   English candidate list. (Extending with hiragana/katakana romaji is
   possible but low-yield.)
2. **Field-name hash function differs from Murmur3.** Murmur3 IS
   confirmed for course names — but field-name hashes may use a
   different algorithm. The fact that obvious strings like
   `Murmur3("flower_coin") ≠ 0xf4ee6827` already hinted at this.
3. **Badges genuinely aren't in container A.** Most consistent with
   the save-format evidence (trailing region ≠ pair region).

Either way, **the grant must go through a direct write to the live
badge state struct**, not through the hash-keyed writer. The Ghidra
work below targets that struct.

### Why sprint 1's badge attempts dead-ended (lessons)

Re-reading the 7 sprint-1 scripts ([scripts/ghidra/find_badge_*.py](../scripts/ghidra/)
and [scripts/ghidra/inspect_badge_*.py](../scripts/ghidra/)) with
sprint-2 hindsight:

- All 7 were **string-grep only** — no dataflow, no symbol-import,
  no use of imported sym files.
- The strings searched (`GiveBadgeIdOnCourseClear`, `BadgeShop`,
  `BadgeChallenge`, `BadgeHouse`, `BadgeMedley`, `BadgeFlower`) were
  all referenced only from **test harnesses, registration tables, or
  log format strings** — no shared grant helper surfaced.
- The conclusion "no exposed grant API" was correct AT THE TIME for
  string-anchored search, but premature: dataflow from known live-
  state anchors wasn't tried.

**Lessons applied to this sprint:**

1. Don't start with strings. Start with known live-state anchors
   (file offset `0x0EA0` from save-diff; cheat DB anchors).
2. Import sym files first (already done for GameDataMgr — they have
   no badge symbols, confirmed via grep of all `switch-mod/syms/100/`).
3. Use multiple independent angles and cross-reference. The
   functions appearing in 2+ analyses are the chokepoints.

### The new strategy: 4-phase Ghidra script

[scripts/ghidra/find_badge_writer_path.py](../scripts/ghidra/find_badge_writer_path.py)
combines four independent dataflow angles:

| Phase | Anchor | What it surfaces |
|---|---|---|
| **A** | `mov/movz/ldr/str` immediate `0x0EA0` | Save serializer + deserializer + direct badge bitfield accessors |
| **B** | PlayReport `equip_badge_id` string xrefs | Live-state load `ldr w_, [x_, #M]` for the equipped badge — adjacent fields likely include ownership |
| **C** | PlayReport `badge_id_array` xrefs + nearby `clz`/`rbit`/`ubfx` bit-iteration | The bitfield iterator — its source register IS the badge ownership u64 |
| **D** | Cheat DB anchors (`0x00306AEC`, `0x00306B90`, `0x01751DB8`, `0x0033186C`) | The badge-EFFECT gating function reads from the ownership bitfield; backward-walk its pre-cheat load |

The cross-reference logic at the end of the script identifies:
- Functions appearing in Phase A + B → save serializer
- Functions appearing in Phase B + D → badge-effect gating
- The grant writer is one xref back along the dataflow from any of
  the above

### Expected outcomes (3 scenarios)

**Scenario 1 (likely): badge bitfield lives at `(GameDataMgr*) + N`
for some struct offset N.** Phase A shows the serializer doing
`ldr xN, [sInstance_ptr]; ldr xM, [xN, #N]; ...; str xM, [xOUT, #0xEA0]`.
Then the grant code is:

```cpp
namespace gmd {
constexpr uintptr_t kBadgeBitfieldOffset = /* TBD from Phase A */;

void GrantBadge(uint8_t internal_id) {
    auto* gmd = GetSingleton();
    if (!gmd) return;
    uint64_t* badges = reinterpret_cast<uint64_t*>(
        reinterpret_cast<uintptr_t>(gmd) + kBadgeBitfieldOffset);
    __atomic_or_fetch(badges, (1ULL << internal_id), __ATOMIC_SEQ_CST);
}
}  // namespace gmd
```

**Scenario 2: badge bitfield lives in a separate struct
`*(GameDataMgr* + N)`, requiring a pointer indirection.** Phase A
shows `ldr xN, [sInstance_ptr]; ldr xM, [xN, #N]; ldr xK, [xM, #...]`.
The grant code adds one dereference:

```cpp
auto* gmd = GetSingleton();
auto* badge_struct = *reinterpret_cast<void**>(
    reinterpret_cast<uintptr_t>(gmd) + kPtrOffset);
uint64_t* badges = reinterpret_cast<uint64_t*>(
    reinterpret_cast<uintptr_t>(badge_struct) + kInnerOffset);
```

**Scenario 3: badge struct is allocated lazily (e.g., first equip)
and `0x0EA0` doesn't appear in `.text` as a literal.** Phase A
returns zero hits. In that case fall back to Phase C — the bit-iter
code MUST read from the bitfield, and its load source register chain
points to the live struct even when the offset isn't a literal.

### What the script CANNOT do (caveats)

- It cannot detect cases where `0x0EA0` is built via `add x_, x_, #0xE, lsl #8;
  add x_, x_, #0xA0` (split-immediate ADD), since the matcher only
  catches single-immediate forms. If Phase A returns zero hits, extend
  the matcher to look for the ADD-pair pattern. Sprint 2's
  `find_offset_constant_xrefs.py` has the same limitation.
- It does not run the decompiler — the output is raw disasm. After
  identifying a candidate function, open it in Ghidra's decompile view
  manually to read the C-like form.

### Verification protocol (once the script returns results)

For each candidate grant-writer found:

1. **Open the function in Ghidra's decompiler.** Confirm signature:
   should be either `(GameDataMgr*, uint8_t badge_id)` or
   `(GameDataMgr*, uint64_t bitmask)` or `(void)` (single-badge
   no-arg grant per shop slot).
2. **Trace the destination register** to confirm it writes to
   `(gmd*) + offset` and not just a stack scratch. Stack writes are
   serializer intermediates, not real grants.
3. **Wire it as a one-shot subsdk hook** mirroring the M3.3 pattern:

   ```cpp
   namespace gmd {
   using GrantBadgeFn = void (*)(void* gmd, uint8_t badge_id);
   constexpr uintptr_t kGrantBadge = /* TBD NSO offset */;

   void GrantBadge(uint8_t internal_id) {
       auto fn = reinterpret_cast<GrantBadgeFn>(
           exl::util::modules::GetTargetStart() + kGrantBadge);
       void* gmd = GetSingleton();
       if (gmd) fn(gmd, internal_id);
   }
   }
   ```

4. **Verify via save-diff.** Call `GrantBadge(9)` (Coin Reward) at
   boot, play any course briefly to trigger save, quit, diff
   `game_data.sav`: bit 9 of the u64 at file offset `0x0EA0` should
   be set. Cross-check with `0x0F3C` "shop purchase" bitfield — if
   it ALSO flips, our grant goes through the shop code path (might
   spend flower coins if not guarded). If only `0x0EA0` flips, it's
   a pure ownership grant.

### Run order for next session

1. **In Ghidra (M3.2 lane):**
   1. Run [scripts/ghidra/find_badge_writer_path.py](../scripts/ghidra/find_badge_writer_path.py).
      Paste full output into a follow-up entry below.
   2. If Phase A returns zero `0x0EA0` hits, also run
      [scripts/ghidra/find_offset_constant_xrefs.py](../scripts/ghidra/find_offset_constant_xrefs.py)
      (the broader sprint-2 script) — it scans 24 trailing-region
      offsets at once. The serializer should appear as a HIGH-count
      function across many offsets.
   3. If Phase B returns hits but Phase A doesn't, decompile the
      Phase B caller — its base register's source IS the live-state
      pointer; the badge bitfield is at some sibling offset.

2. **In parallel (M3.3 lane, P1 from handoff):** Wire `GrantFlowerCoin(99)`
   smoke test in `main.cpp` per the M3.3 priority-1 plan. This is
   independent of badges and unblocks the entire counter-grant family.

### Open questions (worth tracking)

1. **Is there a Per-World badge unlock state separate from the
   global bitfield?** PlayReport's `equip_badge_id` is per-player;
   the equip slot at `0x16B8` is global. Save-diff hasn't captured a
   2-player session — possibly different per-player bitfields exist.
2. **Does the game support badge LOSS?** (Probably not — Mario games
   are accretive.) If so, `GrantBadge(N)` can stay write-only via OR.
3. **Are "shop-available" and "owned" separate?** The save-diff bit
   at `0x0F3C` flipped on shop purchase but not on equip-swap — likely
   "ever purchased from shop" tracking. AP grants probably want to
   write `0x0EA0` (owned) WITHOUT writing `0x0F3C` (shop history), so
   the live grant code should be the path that bypasses the shop.

## 2026-05-24 — M3.2 Ghidra run #1: Phase A complete (63 hits) ★

User ran [find_badge_writer_path.py](../scripts/ghidra/find_badge_writer_path.py).
**Phase A returned excellent signal**; Phase B/C/D crashed on a Jython
bug in `_fn_label` (fixed — `hasattr("getAddress")` matches both
Instruction and Address; switched to `hasattr("getMnemonicString")`).

### Filtering the Phase A noise

Badges are a **u64** bitfield, so the relevant operations are 64-bit
(`str x_`, `ldr x_`, `str xzr`) or 128-bit (`ldr q_`, `str q_` —
NEON copies that include the bitfield + adjacent fields).

Drop:
- **`[sp, #0xea0]`** (11 hits in FUN_710060c62c, plus singletons) —
  stack-relative; the 0xEA0 coincidence is a local var at a large
  stack offset. Pure noise.
- **`strb`/`ldrb`** (5 hits) — byte ops, wrong width for u64. The
  field at offset 0xEA0 in those structs is something else.
- **`str w_`/`ldr w_`/`ldr s0`** — 4-byte ops, also wrong width.

After filtering, **~25 hits remain** in true 64-bit-or-wider accesses
to `[Xreg, #0xea0]`. Grouped by likely role:

### Top candidate functions (sorted by interpretive value)

| Priority | Function | Pattern | Hypothesis |
|---|---|---|---|
| **★★★★** | **`FUN_7100b97330 (+0xa4)` + `FUN_7100b97510 (+0x6c)`** | sibling pair: `ldr q3,[x21,#0xea0]` ; `str q3,[x20,#0xea0]` | **128-bit NEON copy of badge struct — almost certainly `operator=` and copy-ctor**. q-register = 16 bytes = u64 badge bitfield + 8 adjacent bytes. Function size ~0xC0 bytes makes them simple sibling copy methods. Decompile → reveals struct's class name + size. |
| **★★★★** | **`FUN_7100b1f580 (+0x64, +0x7c)`** | `ldr x0,[x0,#0xea0]` then `ldr x3,[x3,#0xea0]` — SAME offset, DIFFERENT base regs in one small function | **Likely a comparator** (`operator==(a, b)`: load a's bitfield, load b's bitfield, compare). Tells us the class. |
| **★★★** | **`FUN_7100345238`** (10KB function, 2 hits 0x1C18 apart) | `str x22,[x27,#0xea0]` at +0xbbc, `ldr x24,[x8,#0xea0]` at +0x27d4 | **Save serializer or deserializer.** The huge function size + multi-field access pattern fits a trailing-region copier. The decompiled function should show adjacent file-offset accesses (0xE98, 0xEA8, 0xEB0...) to confirm. |
| **★★★** | `FUN_71003e268c (+0x14)` | `mov w1, #0xea0` (the only immediate-build hit) | Small function passing 0xEA0 as an arg — could be a "memcpy(dst, src, 0xea0)" wrapper, or "set field-N at offset 0xea0" trampoline. Decompile is ~30 lines max. |
| **★★** | `FUN_71006f75b4` (3 hits at +0x298, +0x2c8, +0x2f8) + `FUN_71006f78b8 (+0x3c)` | 4 `str x9,[x8,#0xea0]` writes, 0x30 stride between first 3 | Either (a) writes to 4 sibling sub-objects with same layout (multi-profile save?), or (b) unrolled loop over an array of 4 BadgeStruct instances. |
| **★★** | Zero-init cluster | `str xzr,[x_,#0xea0]` in FUN_71003fff14 (+0xce0), FUN_71005b9294 (+0xac), FUN_71006e9dac (+0x1b8), FUN_71008ff074 (+0x1854), FUN_7100b6c774 (+0x50c) | Constructors / reset paths. Confirms the BadgeStruct exists and is instantiated in multiple contexts (default-init at boot, profile-load, new-game, etc.). |

### Strongest single lead: the q3 copy pair

`FUN_7100b97330` + `FUN_7100b97510` are adjacent ~0xC0-byte functions
that respectively LOAD `q3` from `[x21, #0xEA0]` and STORE `q3` to
`[x20, #0xEA0]`. The `q3` register is 128 bits = 16 bytes. This means
the badge struct field at offset 0xEA0 is **at least 16 bytes wide**
(or is followed by an 8-byte adjacent field copied atomically).

The save file shows a u64 at 0x0EA0 followed by a u32 at 0x0EA4
(value `0x400c` per the 2026-05-22 capture #1 notes). 0xEA0 + 16 =
0xEB0; we don't have annotations for the bytes between 0xEA8..0xEB0
but the q3 copy implies they're part of the same struct field family.

**Why these two functions matter most:** they're simple, separate,
and tell us:
1. **The this-pointer class** (visible in Ghidra's "Function Signature"
   panel once param types propagate).
2. **The exact size of the badge field group** (look at all `[x21, #N]`
   accesses near +0xEA0).
3. **Their callers** — every function that calls
   `BadgeStruct::operator=` is a path that REPLACES the badge state.
   The grant path is the caller most strongly associated with player-
   level events (course clear, shop purchase, BC win).

### Strongest single lead: the comparator

`FUN_7100b1f580` is also small (the two badge-related ops are 0x18
bytes apart, suggesting a function under 0x200 bytes). Comparator
functions are GOLD because they:
1. Identify the class by being named like `==` operators.
2. Reveal the struct's field layout via the field-by-field comparison
   chain (typical pattern: `if (a->f1 != b->f1) return false; if (a->f2
   != b->f2) return false; ...`).
3. Their xref list is short — only called from delta-detection paths
   (save dirty checks, etc.), which are themselves chokepoints.

### Strongest single lead: the giant serializer

`FUN_7100345238` is 10KB+ and has two well-separated 0xEA0 accesses.
This shape matches a save **trailing-region serializer or
deserializer** — copying many fields from live state to/from the
buffer (or vice versa). Decompiling it will reveal:
1. Whether `x22` (the write-source) is a register containing the live
   badge state or a register containing the save-buffer pointer.
2. Adjacent code accessing other known trailing-region offsets
   (0x167C lives, 0x16B8 equipped badge, etc.) — confirms it as the
   serializer.
3. The base register's source — backwalk from there to find the
   GameDataMgr member offset of the badge struct.

### Recommended next-session actions (in order)

1. **Decompile `FUN_7100b97510` first** (the q3-store sibling). Tiny
   function, highest info density. Report:
   - Function signature (Ghidra-inferred or from PDB-like info if any)
   - Full decompile body (~20 lines)
   - The class of `x20` / `x21` if Ghidra propagated it
2. **Decompile `FUN_7100b1f580`** (the comparator). Look at the
   chain of field-by-field compares — each `[reg, #N]` access maps
   to a struct field.
3. **Decompile `FUN_7100345238` around +0xbbc and +0x27d4**. Confirm
   serializer hypothesis; identify the live-state base register's
   source.
4. **Re-run the script** (Phase B/C/D now fixed) for the PlayReport
   backwalk + cheat-DB anchors output. Cross-reference the function
   names against the Phase A candidates above.

### What this tells us about the grant strategy

Even before the next decompile, we already know:

- **The badge struct is a real C++ class** (has operator=, comparator,
  is constructed in multiple places). Not a loose u64 sitting in
  GameDataMgr.
- **It's at least 16 bytes wide** (q-register copy), so it's a
  badge MANAGER struct with the bitfield as one field, not just the
  bitfield alone. Likely fields: `u64 owned_bitfield;
  u32 some_state; u32 some_other;`.
- **The grant API, if it exists, lives on this class** as a method
  like `void GrantBadge(u8 id)` or `void SetOwned(u64 bitfield)`.
  Once the class is named, we can grep `gmd::BadgeXxx::Grant` /
  similar in any future sym files or via Ghidra's symbol tree.

### Worth pursuing in parallel: badge struct discovery via FUN_71008ff074

The `str xzr,[x8,#0xea0]` at FUN_71008ff074 (+0x1854) is at a HUGE
offset into a function — characteristic of a master constructor or
init routine for a large data structure (possibly the **entire
GameDataMgr struct**, where +0x1854 puts us deep into a sub-struct
init sequence). Decompiling this would reveal the badge struct's
position within GameDataMgr.

Specifically: walk backward from +0x1854 to find the most recent
`stp x?, x?, [...]` that establishes x8 — that tells us how the
badge struct relates to the containing object.

## 2026-05-24 — M3.2 Ghidra run #2: Phase B/C/D complete ★★

User reran the bug-fixed script. Phase A unchanged (63 hits, same
analysis above). Phase B/C/D now produce output. **The Magnet Coin
cheat anchor in Phase D yielded the single highest-value finding of
the sprint.**

### ★★ Phase D headline: `FUN_7100348330` is the badge-ownership query

The Magnet Coin cheat at NSO `+0x306AEC` replaces `mov w20, w0` with
`mov w20, #1`. The original `mov w20, w0` transfers the return value
of a `bl 0x7100348330` call into w20. The very next instructions
USE w20 to write a single-bit "this badge's effect is active" byte:

```
  bl  FUN_7100348330               ; ★ returns w0 = 1 if Magnet Coin owned
  ldr x8, [x19]                    ; this->vtable
  mov w20, w0                      ; ← cheat replaces with mov w20,#1
  tbnz w8, #0x1, ...               ; check some flag (paused? disabled?)
  ldr x9, [x19]                    ;
  and w8, w20, #0x1                ; isolate the ownership bit
  and x9, x9, #-0x4                ; mask
  strb w8, [x9]                    ; activate-or-deactivate the effect
```

**This means `FUN_7100348330` is the centralized "is badge N owned?"
query.** It MUST internally read the badge ownership bitfield. Once
decompiled, we'll see exactly:
1. The bitfield's memory address (literal or via GameDataMgr indirection)
2. The badge-ID argument format (raw bit index? badge type enum?
   namespace-prefixed key?)
3. The struct chain — `this == BadgeMgr*`, or `this == GameDataMgr*`,
   etc.

The grant writer is the **inverse** of this function: OR the badge's
bit into the same memory location the read came from.

### Phase D secondary signal: Wall-Jump cheat path (FUN_7101751b8c)

```
  bl  FUN_71017520c0               ; ownership check (returns numeric variant?)
  cmp w0, #0x2
  b.eq ...                         ; if returned 2 (special wall-jump variant?)
  mov x0, x19
  mov w1, #0x6                     ; ← cheat replaces with mov w1, #7
  ...
  tbnz w21, #0x6, ...              ; checks bit 6 of w21
  tbnz w21, #0x2, ...              ; checks bit 2 of w21
```

w21 is a derived **badge-effect-enable bitmask** (NOT the raw
ownership bitfield — bits checked are small enum-style positions,
not the sparse internal-badge-IDs). FUN_71017520c0 is another
ownership-query candidate; probably the same family as FUN_7100348330
but specific to Wall-Jump's variant-selection.

### Phase B/C: PlayReport struct layout (NOT the GameDataMgr struct)

**Important caveat re-read:** PlayReport's `equip_badge_id` and
`badge_id_array` are read from a **per-PlayReport session struct**
(register x19 in all three xref sites), NOT from GameDataMgr directly:

| Field | Site | Live-state offset |
|---|---|---|
| `equip_badge_id` | FUN_7101a5d3e8 (+0x248): `add x1, x19, #0x434` | `x19 + 0x434` |
| `badge_id_array` | FUN_7101a5de58 (+0x6ac): `add x1, x19, #0x674` | `x19 + 0x674` |
| `badge_id_array` | FUN_7101a5de58 (+0x8d0): `add x1, x19, #0x674` | `x19 + 0x674` |
| `badge_id_array` | FUN_7101a5ea50 (+0xa60): `stur w20, [x29, #-0x80]` | stack-buffer per-iteration |

The `stur w20, [x29, #-0x80]` pattern in FUN_7101a5ea50 confirms that
in some call sites the array is BUILT from individual entries (loop
over set bits in the raw bitfield, push each into a stack buffer).
But in the other two sites (FUN_7101a5de58), `add x1, x19, #0x674`
indicates the array is already pre-computed and `x19 + 0x674` is the
array's address — meaning **the bit→array conversion was done
upstream of these PlayReport builders.**

So the layout chain is:
```
GameDataMgr* (sInstance)
  → ...some indirection...
    → BadgeStruct.owned_bitfield (u64 at offset 0xEA0 in some object)
      ↓ (bit-iteration somewhere)
  PlayReport session struct
    → +0x434: equipped badge ID (cached)
    → +0x674: badge ID array (cached, expanded from bitfield)
```

**This makes the bit-iteration code our Phase C jackpot target.**
There's a function that loops through the badge bitfield's set bits
and populates the per-session badge_id_array. Finding it gives us
both the bitfield's source address AND the badge-ID encoding.

### Cross-reference: any overlap between Phase A and Phase D?

The Magnet Coin path lives in:
- `FUN_7100306a7c` (cheat at +0x306AEC = function +0x70 / +0x114)
- `FUN_7100348330` (the ownership query — called from above)

Neither appears in Phase A's 0xEA0 candidate list. **This is
significant**: it means the ownership query reads the badge bitfield
through an indirection chain that does NOT contain `[reg, #0xEA0]`
as a literal offset.

Most likely interpretation: the badge struct's BASE address is what
flows around in the ownership query, not its enclosing object's
base + 0xEA0. I.e., `FUN_7100348330` operates on a `BadgeMgr*`
directly (not on `GameDataMgr* + 0xEA0`).

This is **good news** for the grant API: it implies there's a
named, addressable BadgeMgr-like class with its own methods. The
chain is probably:

```
gmd::GameDataMgr::sInstance
  → (member) BadgeMgr* mBadgeMgr
    → IsOwned(badge_id) [= FUN_7100348330]
    → GrantBadge(badge_id) [= ??? — find via xrefs to BadgeMgr's bitfield offset]
```

We need to find that "member" offset on GameDataMgr that points to
the BadgeMgr — that's a separate puzzle from the 0xEA0 file-offset
hits.

### Updated Phase A interpretation

The 0xEA0 hits in Phase A are **save-buffer access offsets**, NOT
live-badge-struct accesses. The "BadgeMgr" struct itself has the
bitfield at offset ≠ 0xEA0 (probably 0x00 — it's the first field of
its own class). The 0xEA0 references are the SERIALIZER reading
from the BadgeMgr and writing INTO the save buffer at the
trailing-region offset.

So the Phase A high-confidence candidates (q3 copy pair, comparator,
giant serializer) are all manipulating the **save buffer's** view of
the badge field, NOT the live BadgeMgr. Useful for understanding the
serialization, but the grant writer is upstream — somewhere that
calls `BadgeMgr::SetOwned(id)` or similar.

### Run order for next session (REVISED based on Run #2)

1. **★★ Decompile `FUN_7100348330` first.** This is the single
   highest-value target. Tiny function (probably <60 LoC). Reveals:
   - The badge bitfield's actual memory address pattern (literal vs.
     pointer chain from GameDataMgr).
   - The badge-ID arg format (u32? enum? mapped through table?).
   - The class of `this` if it has one (look for `[x0, #N]` early
     accesses → that's the bitfield offset).
2. **Decompile `FUN_71017520c0`** (the wall-jump ownership variant
   query). Cross-check: same struct? Same arg format? If yes, this
   is a confirmed family.
3. **Decompile `FUN_7100306a7c`** (the Magnet Coin behavior class —
   the caller of FUN_7100348330). The class name in Ghidra's
   signature panel might already be propagated (if any sym applies);
   even without, it's the BadgeBehavior class shape.
4. **Then decompile the original q3 copy pair** (FUN_7100b97330,
   FUN_7100b97510) and the comparator (FUN_7100b1f580). With the
   BadgeMgr class shape known, these become trivial to interpret.
5. **Then check `FUN_7100345238` around +0xbbc and +0x27d4** —
   confirm it's the save trailing-region serializer and locate the
   live-BadgeMgr→save-buffer write site.

### What the grant primitive will look like (anticipated)

Based on the FUN_7100348330 finding, the grant primitive almost
certainly takes this shape:

```cpp
namespace gmd {

// To be filled after FUN_7100348330 decompile reveals:
//   - Whether badge state is reached via a sInstance member offset
//     or via its own singleton
//   - The bitfield's offset within the BadgeMgr struct
constexpr uintptr_t kBadgeMgrOffset_in_GameDataMgr = /* TBD */;
constexpr uintptr_t kBitfieldOffset_in_BadgeMgr    = /* TBD, likely 0x00 */;

inline uint64_t* GetBadgeBitfield() {
    auto* gmd = GetSingleton();
    if (!gmd) return nullptr;
    auto* badge_mgr = *reinterpret_cast<void**>(
        reinterpret_cast<uintptr_t>(gmd) + kBadgeMgrOffset_in_GameDataMgr);
    if (!badge_mgr) return nullptr;
    return reinterpret_cast<uint64_t*>(
        reinterpret_cast<uintptr_t>(badge_mgr) + kBitfieldOffset_in_BadgeMgr);
}

void GrantBadge(uint8_t internal_id) {
    uint64_t* bitfield = GetBadgeBitfield();
    if (!bitfield) return;
    // Use __atomic_or_fetch since the M3 dirty-queue writer was lock-
    // free; assume the badge bitfield is also concurrently accessed.
    __atomic_or_fetch(bitfield, (1ULL << internal_id), __ATOMIC_SEQ_CST);
}

}  // namespace gmd
```

**Verification protocol** (same as M3.3 priority 1):
1. At boot, call `gmd::GrantBadge(9)` (Coin Reward — known internal ID).
2. Play any course briefly to trigger a save.
3. Quit, diff `game_data.sav`: bit 9 of u64 at file offset `0x0EA0`
   should be set.
4. Also check the BC challenge "shop-purchased" bit at `0x0F3C` —
   should NOT change (we're granting ownership, not simulating a
   shop purchase).

## 2026-05-24 — M3.2 Ghidra run #3: THREE candidate functions decompiled, all false positives ★

User pasted decompiles for the top three Phase A / D candidates from
the previous run. **All three turned out to be wrong leads.** This is
a useful negative result — it reveals a fundamental error in the
strategy premise that needs correcting before continuing.

### Decompile 1: `FUN_7100b97510` — IS OpenSSL DTLS, not a badge copier

The smoking gun is at the bottom of the function:

```c
FUN_7100c47330(*(undefined8 *)(lVar2 + 8),
               "external/openssl/ssl/record/rec_layer_d1.c", 0xd1);
```

This is the OpenSSL DTLS-error-report macro (file path = OpenSSL's DTLS
record-layer source; `0xd1` = `__LINE__`). The function's body — 10
consecutive 64-bit field copies starting at `param_1 + 0xE90` through
`+0xED8` — is **copying a `DTLS1_RECORD` or `DTLS_SESSION` struct**.
The "q3 copy at +0xEA0" was pure offset-collision noise; +0xEA0 is a
field within OpenSSL's session state.

⚠️ **Implication**: `FUN_7100b97330` (sibling), the entire
`FUN_7100b96xxx`–`FUN_7100b97xxx` region, and likely `FUN_7100b6xxxx`
through `FUN_7100c5xxxx` are all OpenSSL or its C++ wrapper.
**Exclude from future Phase A interpretation.**

### Decompile 2: `FUN_7100b1f580` — IS C++ runtime hash-table allocator

Uses `ExclusiveMonitorPass`/`ExclusiveMonitorsStatus` (ARM exclusive-
monitor atomics for lock-free allocation) + bucket-table iteration +
`memcpy` of variable-length entries. Calls into `FUN_7100b7fa50`,
`FUN_7100b7e8e0`, `FUN_7100b7f180`, `FUN_7100b7f3d0`, `FUN_7100b82670`
which all live in the same 0x7100b7xxxx region — a STL-like
`std::unordered_map<>` or similar generic container's bucket-copy
operation. The 0xEA0 access is the bucket array's offset within the
allocator's state. **Pure runtime, no badges.**

### Decompile 3: `FUN_7100345238` — IS a per-frame player effect-state tick

This is a 4000-LoC player state machine that runs every frame. Strongest
evidence: dispatches via a uVar38 0..9 → badge effect name table:

```c
if (uVar38 == 9) {
    FUN_71003496f4(lVar22, "HoppinSuper");  // ★ a badge effect name
} else if (uVar38 < 10) {
    pcVar20 = (&PTR_DAT_71034282e0)[uVar38 + 1];  // table of 9 badge effect names
    ...
}
```

This is the **active badge effect updater**, NOT the save serializer
and NOT the ownership store. It reads which effect is currently
engaged and updates per-frame state. The two 0xEA0 accesses (at +0xbbc
and +0x27d4) in a 10KB function are sub-struct offsets within the
per-player state object, almost certainly unrelated to badges.

Crucial side discovery: this function calls `FUN_7100348330` as part
of its inner loop — `if (uVar27 = FUN_7100348330(*plVar39); (uVar27 &
1) == 0) param_1+0x264 = 10;` — which means FUN_7100348330 is also
**not** the badge-ownership query. (See below.)

### Decompile 4: `FUN_7100348330` — is a per-stage attribute check, NOT badge ownership

The body shape:

```c
undefined8 FUN_7100348330(long param_1) {
    if (*(long *)(param_1 + 0x208) != 0) {
        // Get pointer to current-stage sub-object
        lVar7 = *(long *)(*(long *)(param_1 + 0x208) + 0xe0);
        if (lVar7 != 0) {
            if ((*(int *)(lVar7 + 0x694) == 1) || (...checks...)) {
                return 1;  // ★ stage property check
            }
            // Fall through to a float-table lookup keyed by [lVar7 + 0x39c]
            // (some per-stage attribute table; DAT_7103625850 is a global
            // pointer to a stage/world data array)
            ...
            if (*pfVar1 != 0.0) {
                return 1;  // ★ stage attribute is non-zero
            }
        }
    }
    return 0;
}
```

`FUN_7100348330` is a **per-stage/per-area property check** that
returns 1 if the current stage has some attribute set (likely
"allow-coin-suck-effect" or similar). The Magnet Coin BehaviorTick
calls it to decide whether the effect should engage in the current
stage context.

**The cheat anchor was downstream of ownership.** By the time we're
inside the Magnet Coin BehaviorTick, ownership is already implicit
(the behavior instance wouldn't exist otherwise). The `mov w20, w0`
that the cheat replaces is "should this effect engage right now (per
stage rules)", NOT "does the player own this badge".

### Lessons + corrected premise

**Wrong premise:** I assumed the file offset `0x0EA0` would also be
the live-state offset of the badge bitfield, by analogy with the
other trailing-region fields the save-diff sprint mapped. But the
"save trailing region is mirrored at save_buffer_base + file_offset"
finding was about the **save-OUT staging buffer**, NOT the live
GameDataMgr struct. The live BadgeMgr has the bitfield at offset 0
(or 0x08, or similar small offset) within ITSELF — the 0xEA0 only
appears when the serializer **writes** the bitfield into the save
buffer at the trailing-region position.

So a 0xEA0 immediate scan of `.text` predominantly surfaces:
- The save serializer's `str x_, [x_save_buf, #0xEA0]` (1 line)
- The save deserializer's `ldr x_, [x_save_buf, #0xEA0]` (1 line, if
  inline; may use a different pattern via memcpy)
- ~25 OpenSSL / C++ runtime false positives at the same offset within
  unrelated structs

**Corrected premise:** the live badge bitfield is at a DIFFERENT
offset in some `BadgeMgr` struct that GameDataMgr holds via a pointer
chain. The only `.text` literal we'd find for it is the offset INSIDE
the BadgeMgr struct (unknown — likely 0x00, 0x08, or 0x10), which
generates so many false matches that scanning for it is useless.

**Replanned strategy** (see next entry).

## 2026-05-24 — M3.2 strategy pivot: bit-iteration backwalk from PlayReport

The Phase A and Phase D anchors both failed. **Phase B/C remains
viable** but needs a deeper hook. Here's the new plan:

### The strongest remaining anchor: the badge_id_array builder

From Run #2 Phase B/C output:

| Site | Code | Interpretation |
|---|---|---|
| FUN_7101a5d3e8 (+0x248) | `add x1, x19, #0x434; bl Add("equip_badge_id")` | reads cached `equip_badge_id` from session struct |
| FUN_7101a5de58 (+0x6ac, +0x8d0) | `add x1, x19, #0x674; bl Add("badge_id_array")` | reads cached `badge_id_array` from session struct |
| **FUN_7101a5ea50 (+0xa60)** | **`stur w20, [x29, #-0x80]; bl Add("badge_id_array")`** | **builds the array ONE ID AT A TIME — w20 is the per-iteration badge ID** |

The first two read pre-built arrays (cached at session start).
**The third one is different** — `stur w20, [x29, #-0x80]` pushes one
badge ID at a time into a stack buffer, in what's clearly a loop
body. The CALLER of FUN_7101a5ea50 (or the surrounding loop) is the
**bit-iteration code** that walks the live badge bitfield and emits
one entry per set bit.

That iterator's load source IS the badge ownership bitfield.

### Concrete next action

Open FUN_7101a5ea50 in Ghidra (NOT just the +0xa60 site — the entire
function, plus its callers). Specifically:

1. **Decompile FUN_7101a5ea50 in full.** Its prologue should show
   where w20 comes from (likely `param_N` or loaded from a struct
   field). Its caller (visible via Ghidra's "References to" view)
   wraps it in a bit-iteration loop.

2. **Find xrefs to FUN_7101a5ea50.** For each caller, look for:
   - `clz` / `rbit` / `lsr` / `tbnz` / `tbz` instructions
   - A `ldr x_, [x_, #N]` that loads a u64 from some struct (that
     u64 IS the badge bitfield)
   - A counter increment + loop pattern

3. **Once the load source is identified**, that gives us the badge
   bitfield's runtime address pattern. The grant primitive is then:
   ```cpp
   *(uint64_t*)(badge_bitfield_addr) |= (1ULL << badge_internal_id);
   ```

### Alternative anchor: write to x19+0x674 (the cached array)

The other site, FUN_7101a5de58, passes `x19 + 0x674` as a pre-built
array. SOMEONE must populate `x19 + 0x674` before this read. Find
xrefs to `[x_, #0x674]` writes (specifically `str/stp x_, [x_, #0x674]`)
in functions that produce the same session struct x19. That writer is
the cache-fill path — and it presumably reads the live bitfield to
build the cache.

### Fallback anchor: Cheat Engine to find the live bitfield directly

If the Ghidra trace gets stuck, the **Cheat Engine** approach works
in finite time:

1. In Ryujinx, scan for the 8-byte LE pattern of the user's known
   badge ownership u64. From save-diff Match #1: byte sequence
   `00 02 00 00 0C 40 00 00` (= `0x0000400C00000200` = bits 9, 34,
   35, 46 set).
2. Two matches are expected: one in the save-OUT buffer (already
   known, ~`0x1E9BCCE7000 + 0xEA0`) and one in the **live
   BadgeMgr**.
3. Verify the live address: equip a new badge in-game without saving.
   The live address should update immediately; the save-buffer address
   shouldn't (it only refreshes on save).
4. With the live address known, set a memory write breakpoint on it
   in Cheat Engine. Trigger a badge acquisition (e.g., shop purchase).
   The breakpoint hits in the GRANT WRITER — its return address is
   the function we want to wire as the M3.2 grant primitive.

The Cheat Engine path is what the original
[docs/runtime-address-backtrace-plan.md](runtime-address-backtrace-plan.md)
outlined; the sprint-2 GameDataMgr discovery short-circuited it for
M3.3 counters, but for M3.2 badges that shortcut doesn't work and
the original plan is the right tool.

### Recommended order for next session

1. **First**: open FUN_7101a5ea50 in Ghidra, decompile, paste the
   body (small function — should be ~50 LoC). The caller chain
   visible in Ghidra's "References" panel tells us where the bit-
   iteration loop is.
2. **In parallel**: if Ryujinx is available, run the Cheat Engine
   scan for the user's known badge u64 bytes. Two parallel paths
   triangulate fast.
3. Once either path identifies the badge bitfield's live address,
   wire the grant primitive in [switch-mod/src/program/main.cpp](../switch-mod/src/program/main.cpp)
   and verify per the protocol above.

### Open question: does the BadgeMgr live under GameDataMgr or
separately?

The 0xEA0 file offset is in the save trailing region, so its data
gets serialized as part of GameDataMgr's save-out. That means
GameDataMgr either:
- (a) Holds the BadgeMgr as a member at some inner offset, or
- (b) Has a pointer to it that gets walked at serialization time

Either way, once we find the live BadgeMgr address via the Phase B/C
backwalk OR Cheat Engine, the GameDataMgr-relative position is one
pointer-deref backwalk away from the static analysis.

## 2026-05-24 — M3.2 Ghidra run #4: FUN_7101a5ea50 decompiled — NOT the bit iterator ★

User pasted FUN_7101a5ea50 + its caller FUN_7101a5de04. Two big findings,
one consolation prize, and the next clear target.

### Finding 1: FUN_7101a5ea50 is the `course_result` PlayReport builder
and `badge_id_array` is a STUB here

The badge handling in this function is:

```c
uStack_c0 = (char *)CONCAT44(uStack_c0._4_4_, 0xffffffff);   // = -1 sentinel
FUN_71001544b0(&uStack_c0, 0);                                // count = 0
FUN_71006124e0(auStack_50, &uStack_c0, "badge_id_array");     // Add(empty)
FUN_7101b5bca0("dev_badge_array");                            // dev label
```

The `stur w20, [x29, #-0x80]` pattern that Phase C flagged as
bit-iteration was actually **constant initialization**: w20 holds the
magic `0xffffffff` sentinel, and the buffer count is 0. The
`course_result` PlayReport always emits an empty `badge_id_array`
(the badges were already sent in `course_in` at course entry; no
need to repeat at clear).

★ **My Phase C interpretation was wrong.** The "per-iteration store"
pattern was a one-shot sentinel write, not a loop body.

### Finding 2: the REAL bit iterator is upstream of FUN_7101a5de58

The OTHER `badge_id_array` xref site (FUN_7101a5de58 at +0x6ac and
+0x8d0) does `add x1, x19, #0x674; bl Add("badge_id_array")` — passing
a pre-built array AS-IS. The session struct's `+0x674` slot is a
`sead::Buffer<int>` of owned badge IDs that gets populated upstream.

**The writer to `session_struct+0x674` is the bit iterator.** Find
that writer and we find the live BadgeMgr's bitfield address.

### Finding 3 (consolation prize): full session-struct field map

The FUN_7101a5ea50 decompile exposes the entire PlayReport session
struct's layout. The struct is ≥0x800 bytes. Partial map (highlighting
the badge-relevant fields):

| Offset | Field |
|---|---|
| +0x408, +0x439 | byte guard flags |
| +0x410 | flower_coin_course_in (u32) |
| +0x414 | yellow_coin_course_in (u32) |
| +0x418 | big_flower_coin packed bits 0-2 (byte) |
| +0x420 | local_player_num (u32) |
| +0x424 | player_rest_course_in (u32) |
| +0x428 | start_mmp (u32) |
| **+0x434** | **equip_badge_id (PlayReport course_in)** |
| +0x438 | touch_goal_top_enter (byte) |
| +0x43a | net_mode (byte) |
| +0x440..+0x45c | chara_type_array (4 entries, byte-flagged) |
| +0x460..+0x474 | yellow/flower coin counters |
| +0x478 | goal_id |
| +0x47c | world_wonder_flower |
| +0x480, +0x484 | total/current play_time_sec |
| +0x4ac, +0x4b0 | arena_score enter/result |
| +0x4b4 | hana_race_result |
| +0x4b8..+0x51c | rescue, item_bln, emote, ctrl_guide (sub-structs) |
| +0x544..+0x546 | world_mother_seed, last_ctrl_by_stcik, touch_goal_top_result (bytes) |
| +0x554..+0x560 | challenge_count, total_wonder_count, max_wonder_count, total_get_finish_seed_count |
| +0x568..+0x570 | course_result + result_flower source |
| +0x574..+0x5b0 | death/respawn counter arrays (4 sets of u32 quartets) |
| +0x5f4..+0x600 | ctrl_style_array (4 entries) |
| +0x634 | last_put_panel_id |
| **+0x674** | **badge_id_array (sead::Buffer<int>) ★ THE BIT-ITER OUTPUT** |
| +0x780 | course_in_utc (u64) |

This struct is the **per-`course_*`-event session record** — a builder/
DTO populated once per course entry, used by the `course_in` builder
(FUN_7101a5de58) and the `course_result` builder (FUN_7101a5ea50).

### Caller FUN_7101a5de04 — also not the bit iterator

```c
void FUN_7101a5de04(long param_1, uint param_2) {
    if (((*(byte*)(param_1+0x439) | *(byte*)(param_1+0x408)) == 0) &&
        (FUN_7101a5ea50(), (param_2 & 1) != 0) && DAT_7103640ce8 != 0) {
        // atomic OR 0x40 into [DAT_7103640ce8 + 0x40]
    }
}
```

This is a tiny "send-report-if-conditions-met" wrapper around the
builder. No bit iteration here. The session struct it passes was
populated earlier in the call chain.

### Next concrete action

**Find the writer to `session_struct + 0x674`.** It's somewhere
between the course-load event and the call to FUN_7101a5de58. Concrete
approach in Ghidra:

1. Find xrefs to `FUN_7101a5de58` (the course_in PlayReport builder).
   Its caller (let's call it FUN_7101a5dXXX) sets up the session struct
   immediately before calling.
2. In the caller, look for either:
   - A direct `str/stp x_, [x_, #0x674]` to write the badge_id_array
     pointer/length pair into the session struct, OR
   - A function call passing `session + 0x674` as an argument — that
     callee builds the array
3. The bit iteration is in that function or its caller. It looks like:

   ```text
   ldr x_owned, [x_badge_mgr, #N]    ; load 64-bit ownership bitfield
   ; then either:
   ;   loop iterating with rbit/clz to find next set bit, OR
   ;   unrolled per-bit `tbnz` checks
   ;   write each found bit position to the output buffer
   ```

   The `ldr x_owned, [x_badge_mgr, #N]` is the target — `x_badge_mgr`
   is the live BadgeMgr address (sourced via a pointer chain from
   `gmd::GameDataMgr::sInstance` or held in some other singleton).

### Alternative: skip Ghidra, use Cheat Engine (still recommended)

The static-analysis chain is at least 3 functions deep now. Cheat
Engine takes ~10 minutes:

1. Scan Ryujinx memory for `00 02 00 00 0C 40 00 00` (= the known
   badge u64 bytes). Two matches expected; the non-save-buffer one
   is the live BadgeMgr.
2. Equip a different badge in-game (no save) — live address updates
   immediately, save buffer doesn't. Confirms identity.
3. Memory-write breakpoint on the live address; trigger a badge
   acquisition (shop purchase). The breakpoint hits in the grant
   writer; its return address is the function to wire as M3.2 grant
   primitive.

### Updated next-session checklist

1. **(Static)** Find xrefs to FUN_7101a5de58 in Ghidra. Decompile the
   caller (should be a small function that does `init session →
   populate fields → call PlayReport builder`). Paste here.
2. **(Static)** Then find xrefs to whoever writes `[x_, #0x674]`
   within that caller chain. That writer is the bit iterator.
3. **(Dynamic, parallel)** Run the Cheat Engine scan. Faster path to
   the live BadgeMgr address than chasing 3 layers of session-struct
   setup code.

Both paths converge on the same answer: the live `BadgeMgr` bitfield
address, which the grant primitive writes to via OR-equal.

## 2026-05-24 — M3.2 Ghidra run #5: FUN_7101a5d93c found — master course-PlayReport orchestrator ★

User pasted two callers of FUN_7101a5de58. Decisive winner: FUN_7101a5d93c
is a 4-line orchestrator that calls the cache populator + both builders
back-to-back.

```c
void FUN_7101a5d93c(long param_1, long param_2)
{
    FUN_7101a5d9a0();                                    // ★ cache populator
    FUN_7101a5de04(param_1, *(int*)(param_2+4) == 1);    // course_result wrapper
    FUN_7101a5de58(param_1);                             // course_in builder
    FUN_7101a5e7a0(param_1 + 0x768);                     // ctrl-style sub-section
    if (*(int*)(param_1 + 0x638) != 0) {
        FUN_7101a6af10(auStack_28);
    }
}
```

`param_1` IS the session struct (passed directly to FUN_7101a5de58
which reads `param_1+0x674` for badge_id_array). The cache must be
populated BEFORE FUN_7101a5de58 runs — and `FUN_7101a5d9a0()` is the
only call beforehand. Its argless display in Ghidra is a decompiler
artifact (x0 was likely set in the caller and not propagated).

The other caller (FUN_7101a610bc) is a course state-machine event
handler that conditionally invokes FUN_7101a5de58 via a sub-object at
`param_1+0x48`. Less direct, but confirms the session-struct pattern
is reused across multiple event paths.

### Next concrete decompile target: FUN_7101a5d9a0

**This is now the highest-value target.** FUN_7101a5d9a0 is the only
function called before the PlayReport builders, and the session struct
slots (+0x434 equipped badge, +0x674 badge_id_array, and ~80 other
fields) get populated FROM live game state somewhere — almost
certainly in this function (or one it calls).

What we expect to see in the decompile:
- A read of `gmd::GameDataMgr::sInstance` (or a similar singleton)
- A pointer-chain walk to the live BadgeMgr (or whatever holds the
  ownership bitfield)
- A `ldr x_owned, [x_badge_mgr, #N]` loading the u64 bitfield
- A bit-iteration loop (rbit + clz, or tbnz unrolled) emitting one
  entry per set bit into the session_struct's badge_id_array buffer
- A write of the equipped badge to `[session, #0x434]`

If the bit iteration is inlined here, we have the live BadgeMgr
address pattern in one decompile. If it's hidden behind another call
(e.g., `BadgeMgr::PopulateOwnedArray(out_buffer)`), we follow one
more level.

### Updated grant primitive draft (now with the session-struct context)

```cpp
namespace gmd {

// To be filled after FUN_7101a5d9a0 decompile reveals:
//   - Whether BadgeMgr is reached via sInstance member offset, or
//     held in a separate singleton
//   - The bitfield's offset within the BadgeMgr struct (likely +0x00
//     or +0x08, since it'll be the primary field)
constexpr uintptr_t kBadgeMgrPath  = /* TBD */;
constexpr uintptr_t kBitfieldOff   = /* TBD */;

inline uint64_t* GetBadgeBitfield() {
    auto* gmd = GetSingleton();
    if (!gmd) return nullptr;
    // The path may be a single member offset OR a pointer-deref chain;
    // the FUN_7101a5d9a0 decompile will tell us.
    auto* badge_mgr = /* deref(gmd + kBadgeMgrPath) */;
    return reinterpret_cast<uint64_t*>(
        reinterpret_cast<uintptr_t>(badge_mgr) + kBitfieldOff);
}

void GrantBadge(uint8_t internal_id) {
    auto* bf = GetBadgeBitfield();
    if (!bf) return;
    __atomic_or_fetch(bf, (1ULL << internal_id), __ATOMIC_SEQ_CST);
}

}  // namespace gmd
```

### Confirmed call chain

```
(course-load event handler — TBD)
    └─> FUN_7101a5d93c(session, ev)
            ├─> FUN_7101a5d9a0()              ★ populates session from live state
            ├─> FUN_7101a5de04(session, ...)
            │       ├─> (guards)
            │       └─> FUN_7101a5ea50(session)   = course_result builder
            │              └─> Add("badge_id_array", []) [stub]
            ├─> FUN_7101a5de58(session)         = course_in builder
            │       └─> Add("badge_id_array", session+0x674) ★ uses cache
            ├─> FUN_7101a5e7a0(session+0x768)   = ctrl-style sub-section
            └─> (conditional FUN_7101a6af10)
```

We just need one more decompile (FUN_7101a5d9a0) to close the loop on
the live BadgeMgr access pattern.

## 2026-05-24 — M3.2 Ghidra run #6: FUN_7101a5d9a0 decompiled — RESULT populator, NOT badge populator ★

User pasted FUN_7101a5d9a0 + confirmed exactly one xref (the call from
FUN_7101a5d93c). **It's a result-time populator, not the badge cache
populator.** Two big byproduct findings keep the sprint moving despite
the negative result.

### Negative result: badge fields are NOT touched here

Scanning all writes to `param_1`:

| Offset | Field |
|---|---|
| +0x460..+0x474 | yellow/flower coin counters (course-end totals) |
| +0x478 | goal_id |
| +0x47c | world_wonder_flower |
| +0x480/+0x484 | total/current play_time_sec |
| +0x490/+0x494 | player_rest_course_out, total_1up_count |
| +0x498..+0x4b0 | result_mmp, friend_race_*, room_member_*, arena_score_* |
| +0x544 | world_mother_seed |
| +0x545 | last_ctrl_by_stcik |
| +0x546 | touch_goal_top_result |
| +0x558..+0x568 | wonder_count fields, course_result |
| +0x56c/+0x570 | result_flower source |
| +0x5f4..+0x600 | ctrl_style_array |

**Conspicuously absent: +0x434 (equip_badge_id) and +0x674 (badge_id_array).**

This function populates the RESULT-time fields (final counters, completion
state, last-touched controller, etc.). The badge fields are SETUP-time
fields populated at course ENTRY by a different code path. They stay
valid throughout the course (you can't change equipped badges mid-level),
which is why FUN_7101a5d93c can safely re-fire the `course_in` PlayReport
at result time without re-populating them.

So the **course-entry session populator** is what we need to find — a
different function entirely, running at level-load not level-end.

### ★ Byproduct 1: TWO new GameDataMgr hash keys at consumption sites

Buried in the population code:

```c
if (_ZN3gmd11GameDataMgr9sInstanceE != 0) {
    FUN_71003838ac(_ZN3gmd11GameDataMgr9sInstanceE, param_1 + 0x546, 0xed817774);
}
if ((*(int *)(param_2 + 4) != 2) && (_ZN3gmd11GameDataMgr9sInstanceE != 0)) {
    FUN_710012ae94(_ZN3gmd11GameDataMgr9sInstanceE, param_1 + 0x478, 0xf79bcbb0);
}
```

| Hash | Accessor | Target field | Semantic guess |
|---|---|---|---|
| **`0xed817774`** | bool getter (FUN_71003838AC) | `session+0x546` (touch_goal_top_result) | A boolean save flag for "did the player ever reach top of flag on the current/just-cleared course" or similar per-course progress bit |
| **`0xf79bcbb0`** | container-A counter reader (FUN_710012AE94) | `session+0x478` (goal_id) | A u32 counter — possibly "last goal_id achieved on current course" (0=normal, 1=secret, 2=fake), used to populate the PlayReport's goal_id field |

Both hashes are **NEW** — not in the sprint-2 shared-keys list and not
in MemetendoYT's 8. Add them to the corpus.

### ★ Byproduct 2: GameDataMgr accessor signatures CORRECTED

Sprint-2 doc claimed `FUN_710012AE94` is `(gmd, hash, &out_buf)`. **The
real call site here is `(gmd, out_ptr, hash)` — out BEFORE hash.** The
bool getter `FUN_71003838AC` uses the same `(this, out, hash)` shape.

Corrected API table:

| NSO offset | Role | Signature |
|---|---|---|
| `+0x710012AE94` | Container A counter **READER** | `(gmd, uint32_t* out, uint32_t hash)` ← corrected |
| `+0x710049F648` | Container A counter **WRITER** | `(gmd, uint32_t value, uint32_t hash)` |
| `+0x71003838AC` | Sub-bool **READER** | `(sub_obj, uint8_t* out, uint32_t hash)` |

The writer's `(gmd, value, hash)` ordering remains correct (confirmed
in the earlier decompile of FUN_710049F648).

### Implication for M3.3 wiring (independent of badges)

The corrected reader signature should be reflected in the M3.3 code
when wiring the smoke test. The grant primitive is unchanged
(`FUN_710049F648(gmd, value, hash)` is correct), but if we add a
read-back to verify, the call shape is `(gmd, &out, hash)` not
`(gmd, hash, &out)`.

### Where do the badge fields actually get populated?

Three candidates to chase:

1. **A course-entry handler** that runs when the player starts a
   course. Sets up the session struct's +0x434 and +0x674 along with
   other course-setup fields like `lucky_coin`, `local_player_rest`.
2. **A persistent "Player" object holds the equipped badge / owned
   bitfield, and a copy-on-demand helper writes them into the session
   struct when the course event fires.** The course-entry handler
   would call that helper.
3. **The session struct is partly long-lived** — the badge fields
   live there permanently from when the player picks them in the menu,
   and the course-event populator only refreshes the dynamic fields
   (which is what FUN_7101a5d9a0 does).

In any case, the **load source for the badge bitfield is upstream
of FUN_7101a5d93c**, in code that runs at course load OR at menu-
equip time. Two specific search targets:

1. **Find xrefs to writes of `[x_, #0x434]`** (equip_badge_id slot —
   smaller, more distinctive offset than +0x674 which could match many
   sead::Buffer writes). The writer is the equip-cache-populator. Its
   load source IS the live equipped-badge field; the owned bitfield is
   nearby in the same struct.
2. **Find xrefs to writes of `[x_, #0x674]`** — same approach for the
   array. The writer iterates the bitfield to produce the array. Higher
   noise than #1 but more direct (its read source IS the bitfield).

### Strongest recommendation: switch to Cheat Engine

After 4 successive Ghidra rounds (Phase A + Phase D + 5d93c + 5d9a0),
**we're 3 layers deep from the actual badge bitfield and still
walking the call chain**. The Cheat Engine path is now strictly
faster:

1. Scan Ryujinx for the 8-byte LE pattern `00 02 00 00 0C 40 00 00`
   (= the user's known badge u64). 2 matches expected.
2. The match at `~save_buffer + 0xEA0` is known; the OTHER one is
   the live BadgeMgr.
3. Verify the live address: equip-without-save updates it immediately;
   the save buffer doesn't.
4. Memory write breakpoint on the live address; trigger any badge
   acquisition (shop purchase). The breakpoint's return address is
   the grant writer function — wire it as M3.2 grant primitive.

This is 10-20 minutes of Cheat Engine vs. potentially another 2-3
Ghidra rounds. The static analysis HAS already produced its real
deliverables (corrected API signatures, 2 new hashes, complete
session struct map, full PlayReport call chain) — the badge bitfield
is now the lone remaining unknown and dynamic tooling is the right
match.

### Updated hash key corpus

Adding to [save-diff-findings.md](save-diff-findings.md) and
[scripts/brute_badge_field_hashes.py](../scripts/brute_badge_field_hashes.py)
for completeness:

```python
# Confirmed at FUN_7101a5d9a0 call sites (2026-05-24, M3.2 sprint):
0xed817774: "(bool flag — likely per-course top-of-flag-ever progress)",
0xf79bcbb0: "(counter — likely current course last goal_id 0/1/2)",
```

Run `brute_badge_field_hashes.py` again with these added if any
candidate name happens to land on them.

## 2026-05-24 — M3.2 Ghidra run #7: find_session_struct_populator.py — THREE clean candidates ★★

User ran [find_session_struct_populator.py](../scripts/ghidra/find_session_struct_populator.py).
**Three high-value targets surfaced**, all small focused functions
with both badge-offset writes AND gmd::sInstance access. After a
session of negative results, the intersection scoring worked exactly
as intended.

### Top-3 candidate analysis

| Score | Function | Pattern | Interpretation |
|---|---|---|---|
| **1700** | **`FUN_7101c62368`** | small (~0x200B); WRITES +0x674 + +0x67c; loads sInstance | **★ primary populator suspect** — writes the sead::Buffer (ptr, size) pair while accessing GameDataMgr |
| **1700** | **`FUN_71007350f8`** | tiny (~0x300B); READS +0x674 + +0x67c; calls Container-A WRITER (FUN_710049F648); loads sInstance | **★ badge→counter sync, or possibly the bit→bitfield write helper** — iterates the badge array and calls the M3.3 grant primitive with some hash |
| **2300** | `FUN_710071a4a8` | HUGE (~48KB); 12 hits across +0x434/+0x674/+0x67c; no gmd signal | multi-event player state machine; touches session struct in many branches but doesn't appear to populate from live state |

The score formula favored FUN_710071a4a8 because of the 2000-point
"writes-to-both-434-and-674" bonus, but that's misleading — the
function is way too big to be the focused populator. The two
**1700-score functions with sInstance access** are the real targets.

### Why FUN_71007350f8 is potentially huge

It reads from `[x0, #0x674]` and `[x19, #0x67c]` — getting badge
array pointer + size from passed-in and saved struct slots — then
calls the Container-A WRITER `FUN_710049F648(gmd, value, hash)` at
+0x148. The hash it uses at that call site **MIGHT BE the badge
ownership bitfield's GameDataMgr key**, if badges are in container A
after all (despite the brute-force coming up empty).

Alternative interpretations:
- The function iterates the badge array and calls the writer with a
  per-badge derived value (e.g., "badge N's last-equipped-at
  timestamp"). Less useful but still informative.
- The function emits a "total badges owned" counter via the writer.
  Useful for some derived stats but not the grant target.

### Recommended decompile order for next session

1. **★ FUN_7101c62368 first** — small, primary populator suspect.
   Should reveal:
   - How the live BadgeMgr address is reached from `sInstance`
   - The badge bitfield's offset within the BadgeMgr struct
   - Whether the array buffer is allocated by this function or by a
     callee
   - The `(ptr, size)` write pattern at +0x674/+0x67c
2. **★ FUN_71007350f8 second** — small, reveals the badge↔
   GameDataMgr writer relationship. The hash it passes to
   FUN_710049F648 at +0x148 is the headline data point.
3. **(Optional)** FUN_710071a4a8 around +0x1e40 and +0x887c (just
   those regions, not the whole 48KB) — to confirm it's an
   orchestrator rather than a populator.

### What we expect to see in FUN_7101c62368

Probable structure (predicted, to be confirmed):

```c
void FUN_7101c62368(SessionStruct* session) {
    auto* gmd = _ZN3gmd11GameDataMgr9sInstanceE;
    if (!gmd) return;
    auto* badge_mgr = /* deref(gmd + some_offset) */;
    if (!badge_mgr) return;

    // Build the owned-badge array:
    auto* buf  = AllocateOrGetBadgeBuffer(badge_mgr);  // sead::Buffer<int>
    size_t cnt = CountOrIterateOwnedBadges(badge_mgr);

    // Store into session struct:
    session->equip_badge_id = badge_mgr->equipped;     // at +0x434
    session->badge_id_array_ptr  = (uint32_t)buf;      // at +0x674
    session->badge_id_array_size = cnt;                // at +0x67c
    ...
}
```

If this pattern holds, the line `auto* badge_mgr = deref(gmd +
some_offset)` gives us the **live BadgeMgr access path**. The grant
primitive becomes trivial.

### What we expect to see in FUN_71007350f8

Probable structure:

```c
void FUN_71007350f8(SessionStruct* session) {
    auto* gmd = _ZN3gmd11GameDataMgr9sInstanceE;
    if (!gmd) return;

    // Read the badge array we just built:
    uint32_t* arr = (uint32_t*)(uintptr_t)session->badge_id_array_ptr;  // [x0, #0x674]
    uint32_t cnt  = session->badge_id_array_size;                        // [x19, #0x67c]

    // Compute derived value or sync state:
    uint32_t derived = /* popcount(arr) OR sum OR specific reduction */;

    // Write back via Container-A writer:
    FUN_710049F648(gmd, derived, /* SOME HASH — capture this! */);
}
```

The **hash constant** at the +0x148 call site is the key data point.
If it's a known hash from our corpus, we learn what derived counter
this is. If it's a NEW hash, we add it to the corpus and identify
its semantics.

### Updated next-session checklist

1. Decompile FUN_7101c62368 in full. Paste body here.
2. Decompile FUN_71007350f8 in full. Paste body here.
3. Identify the live BadgeMgr access pattern from #1.
4. Identify the hash constant + value passed to FUN_710049F648 from #2.
5. With the badge bitfield's live address pattern known, draft and
   wire the M3.2 grant primitive in [switch-mod/src/program/main.cpp](../switch-mod/src/program/main.cpp).
6. Verify via save-diff: `gmd::GrantBadge(9)` (Coin Reward) → bit 9
   set in u64 at file offset `0x0EA0`.

## 2026-05-24 — M3.2 Ghidra run #8: populator + writer decompiles — NEW ACCESSOR FAMILY discovered ★★★

User pasted FUN_7101c62368 + FUN_71007350f8 decompiles. **Neither
turns out to be the badge ownership writer**, but they revealed a
previously unknown GameDataMgr accessor family that's the most
promising lead yet.

### FUN_7101c62368 — multiplayer session populator (NOT badge ownership)

The function calls a NEW accessor 8 times with 8 different hashes,
each populating a consecutive session slot:

```c
FUN_7101f27b78(sInstance, &uStack_24, HASH);   // NEW: returns object pointer
FUN_xxxx(uStack_24, &out_value);                // typed extractor
session[OFF] = out_value;
```

| Session offset | Hash | Likely role |
|---|---|---|
| +0x660 | `0x2d8c6ec0` | per-player session field |
| +0x664 | `0x5f07db24` | per-player session field |
| +0x668 | `0xe82403c2` | per-player session field |
| +0x66c | `0xdd62141a` | per-player session field |
| +0x670 | `0xf0d05e3a` | per-player session field (used by FUN_71007350f8 as input to FUN_7100735858 course-info call) |
| **+0x674** | **`0x6ba8ad3d`** | **badge_id_array slot 0 — likely P1 equipped badge** |
| +0x678 | `0x1415f836` | badge_id_array slot 1 — likely P2 equipped badge |
| **+0x67c** | **`0xb1ae38a4`** | badge_id_array slot 2 — likely P3 equipped badge |

★ **`badge_id_array` is an INLINE array of per-player equipped badge
IDs**, NOT a `sead::Buffer<int>` with `(ptr, size)`. FUN_71007350f8
confirms this by reading slots [0x674]/[0x678]/[0x67c] as iVar2/iVar3/
iVar4 and packing them into a 4-element array (4th slot via
FUN_7100735858 — likely the local player or a special slot).

So `badge_id_array` in PlayReports = per-player equipped badges
(co-op), NOT the owned bitfield. Earlier sprint assumptions about
"bit iteration upstream" were wrong — there's no iteration here, just
3 hash-keyed reads.

### FUN_71007350f8 — course-event reporter (also NOT badge writer)

Despite using the M3.3 grant primitive `FUN_710049F648`, the call is:
```c
FUN_710049f648(sInstance, puVar15[3], 0xf20e6a36);  // course-derived counter
```

Plus 4 calls to ANOTHER accessor `FUN_71003877c4(gmd, value, hash)`:
- `0x3b241921` — difficulty enum (writes "Default"-prefixed string)
- `0x1b56d494` — result state enum (writes "Invalid"-prefixed string)
- `0x5bf37fa9` — current course (writes one of the 81 Course1..Course81 strings — the SAME table FUN_71003D4110 hashes!)
- `0xb8c53575` — another state enum

`FUN_71003877c4` is now confirmed: it's a **writer for enum-typed
fields** (takes a value, writes via hash).

### ★★★ The big discovery: TWO previously-unknown accessor families

**Family 1: Object-pointer getter `FUN_7101f27b78(gmd, out_obj_ptr, hash)`**

This is a NEW accessor pattern. Unlike the sprint-2 accessors that
return values directly, this one returns an object pointer that you
then pass to a typed extractor. This implies GameDataMgr holds a
table of `gmd::GameDataField*` (or similar) objects keyed by hash,
and the value extraction is virtual-dispatched per type.

**Critical implication for badge ownership:** if the badge ownership
bitfield is held as a GameDataField under SOME hash, it's accessible
via `FUN_7101f27b78`. The badge-ownership read path through this
accessor would look like:

```c
void* badge_obj = nullptr;
FUN_7101f27b78(sInstance, &badge_obj, HASH_BADGE_OWNED);
// then a u64-extractor:
uint64_t bitfield;
FUN_xxxx(badge_obj, &bitfield);
```

If we find that hash, we're done.

**Family 2: Enum-typed writer `FUN_71003877c4(gmd, value, hash)`**

This is what sprint 2 saw as "TBD — small int constants, probably an
enum bool getter". Now confirmed as a WRITER for enum fields. Useful
for the M3.3 grant set if any badge state turns out to be enum-typed.

### Updated hash key corpus (15 new keys from this round)

| Hash | Found at | Accessor | Role |
|---|---|---|---|
| `0x2d8c6ec0` | FUN_7101c62368 | FUN_7101f27b78 | per-player session field |
| `0x5f07db24` | FUN_7101c62368 | FUN_7101f27b78 | per-player session field |
| `0xe82403c2` | FUN_7101c62368 | FUN_7101f27b78 | per-player session field |
| `0xdd62141a` | FUN_7101c62368 | FUN_7101f27b78 | per-player session field |
| `0xf0d05e3a` | FUN_7101c62368 | FUN_7101f27b78 | per-player session field (course-info input) |
| `0x6ba8ad3d` | FUN_7101c62368 | FUN_7101f27b78 | **badge_id_array[0] (P1 equipped badge)** ★ |
| `0x1415f836` | FUN_7101c62368 | FUN_7101f27b78 | badge_id_array[1] (P2 equipped badge) |
| `0xb1ae38a4` | FUN_7101c62368 | FUN_7101f27b78 | **badge_id_array[2] (P3 equipped badge)** ★ |
| `0x3b241921` | FUN_71007350f8 | FUN_71003877c4 | difficulty enum |
| `0x1b56d494` | FUN_71007350f8 | FUN_71003877c4 | result state enum |
| `0x5bf37fa9` | FUN_71007350f8 | FUN_71003877c4 | current course (Course1..81) |
| `0xb8c53575` | FUN_71007350f8 | FUN_71003877c4 | another state enum |
| `0xf20e6a36` | FUN_71007350f8 | FUN_710049F648 (M3.3 writer) | course-derived counter |

### Next concrete action: harvest ALL hashes used with FUN_7101f27b78

If the badge ownership bitfield is hash-keyed and accessed via the
object-getter pattern, scanning every xref to FUN_7101f27b78 and
extracting the hash constant at each call site should surface the
ownership hash directly. New script: [scripts/ghidra/walk_object_accessor_hashes.py](../scripts/ghidra/walk_object_accessor_hashes.py).

Run it; the output lists every hash used with FUN_7101f27b78 across
the binary, grouped by enclosing function. The badge ownership hash
will be:
- In a function that does bit-iteration (rbit/clz/tbnz on a u64), OR
- In a function that handles `equip_menu` / `shop` / `badge_unlock`
  paths, OR
- In a function whose extractor wrapper writes to an 8-byte
  destination (u64-shaped) rather than a u32

Once found, the M3.2 grant primitive draft:

```cpp
void GrantBadge(uint8_t internal_id) {
    auto* gmd = GetSingleton();
    void* badge_field = nullptr;
    auto getter = reinterpret_cast<GetObjFn>(
        exl::util::modules::GetTargetStart() + 0x01F27B78);
    getter(gmd, &badge_field, HASH_BADGE_OWNED_BITFIELD);
    if (!badge_field) return;
    // badge_field points at the GameDataField wrapping the u64;
    // either it has a SetU64 method we call, OR we cast and write
    // directly (TBD from the extractor's decompile).
}
```

## 2026-05-24 — M3.2 Ghidra run #9: walk_object_accessor_hashes.py — no new candidates, sister populator confirmed, walker bug ★

User ran [walk_object_accessor_hashes.py](../scripts/ghidra/walk_object_accessor_hashes.py).
**The output produced no badge ownership candidate.** Cross-referencing
against run #8 also surfaced a hash-extraction bug in the walker.

### Raw output (verbatim)

```
Target: 7101f27b78 (NSO +0x1f27b78)
8 call site(s)

--- 0x2d8c6ec0 [per-player session field (run #8)]  (2 callsites) ---
    7101c6239c   in FUN_7101c62368 @ 7101c62368   -> post-extract: FUN_7101c62530
    7101c623c8   in FUN_7101c62368 @ 7101c62368   -> post-extract: FUN_7101c62590

--- 0x1415f836 [badge_id_array[1] P2 equipped (run #8)]  (1 callsite) ---
    7101c624a8   in FUN_7101c623ec @ 7101c623ec   -> post-extract: FUN_7101c62760

--- 0x5f0703c2  (1 callsite) ---
    7101c623f4   in FUN_7101c623ec @ 7101c623ec   -> post-extract: FUN_7101c625f0

--- 0xb1ae38a4 [badge_id_array[2] P3 equipped (run #8)]  (1 callsite) ---
    7101c624dc   in FUN_7101c623ec @ 7101c623ec   -> post-extract: FUN_7101c62940

--- 0xdd625e3a  (1 callsite) ---
    7101c6244c   in FUN_7101c623ec @ 7101c623ec   -> post-extract: FUN_7101b5ba60

--- 0xe824141a  (1 callsite) ---
    7101c62420   in FUN_7101c623ec @ 7101c623ec   -> post-extract: FUN_7101c62650

--- 0xf0d0ad3d  (1 callsite) ---
    7101c62478   in FUN_7101c623ec @ 7101c623ec   -> post-extract: FUN_7101c626e0
```

8 callsites total: 2 in FUN_7101c62368 (run-#8 populator) and 6 in a
**previously-unseen** FUN_7101c623ec.  No `BIT-ITER NEARBY` marker was
flagged at ANY callsite.

### The walker bug: the 4 "new" hashes are hybrids, not real hashes

Cross-checking against the 8 hashes that run #8 confirmed in
FUN_7101c62368 (slots +0x660..+0x67c):

| Run-#8 slot | Real hash | "New" hash in c623ec | Decomposition |
|---|---|---|---|
| slot 1 (+0x664) | `0x5f07db24` | `0x5f0703c2` | `0x5f07` (high of slot 1) `\|` `0x03c2` (low of slot 2) |
| slot 2 (+0x668) | `0xe82403c2` | `0xe824141a` | `0xe824` (high of slot 2) `\|` `0x141a` (low of slot 3) |
| slot 3 (+0x66c) | `0xdd62141a` | `0xdd625e3a` | `0xdd62` (high of slot 3) `\|` `0x5e3a` (low of slot 4) |
| slot 4 (+0x670) | `0xf0d05e3a` | `0xf0d0ad3d` | `0xf0d0` (high of slot 4) `\|` `0xad3d` (low of slot 5) |
| slot 6 (+0x678) | `0x1415f836` | `0x1415f836` ✓ | (correct — last-but-one callsite, no "next" to bleed from) |
| slot 7 (+0x67c) | `0xb1ae38a4` | `0xb1ae38a4` ✓ | (correct — last callsite) |

Every interior callsite returned `upper16[slot_i] | lower16[slot_{i+1}]`.
The two boundary callsites (the last two, with no following slot) are
correct.  This pattern is too systematic to be coincidence.

**Root cause** (now fixed in the script): the original backwalk
unconditionally overwrote `low_half` / `high_half` on every match and
did NOT stop at prior `bl` boundaries.  In the v1.0.0 binary the
compiler schedules adjacent FUN_7101F27B78 callsite arg-prep regions
such that LOW16 for slot i+1 ends up emitted before the `bl` of slot i
(plausibly via instruction scheduling to overlap latency of the load
of `sInstance` between calls).  The walker therefore picked up
slot_{i+1}'s LOW16 along with slot_i's HIGH16.

The script has been patched to:
1. Stop at any prior `bl` (callsite boundary).
2. Closest-to-bl wins for each half (first-hit-only).
3. Early-exit once both halves are known.
4. Honor LSL #16 on `mov` / `movz` as well as `movk`.

The user should re-run the patched script to confirm what the real
hashes in FUN_7101c623ec are.  Plausible expectation: c623ec is a
**sister populator** of c62368 — same accessor pattern, same 8 (or
similar) per-player session hashes, populating a DIFFERENT player slot
or a DIFFERENT multiplayer/co-op context.

### Patched re-run confirms — c623ec uses the SAME 8 hashes as c62368

User re-ran the patched walker.  All 8 callsites resolve cleanly, and
every hash matches a known run-#8 hash:

| Callsite | Hash | Run-#8 label | Post-extract |
|---|---|---|---|
| `7101c6239c` (c62368) | `0x2d8c6ec0` | per-player session field | FUN_7101c62530 |
| `7101c623c8` (c62368) | `0x5f07db24` | per-player session field | FUN_7101c62590 |
| `7101c623f4` (c623ec) | `0xe82403c2` | per-player session field | FUN_7101c625f0 |
| `7101c62420` (c623ec) | `0xdd62141a` | per-player session field | FUN_7101c62650 |
| `7101c6244c` (c623ec) | `0xf0d05e3a` | per-player session field (course-info input) | FUN_7101b5ba60 |
| `7101c62478` (c623ec) | `0x6ba8ad3d` | **badge_id_array[0] (P1 equipped)** | FUN_7101c626e0 |
| `7101c624a8` (c623ec) | `0x1415f836` | **badge_id_array[1] (P2 equipped)** | FUN_7101c62760 |
| `7101c624dc` (c623ec) | `0xb1ae38a4` | **badge_id_array[2] (P3 equipped)** | FUN_7101c62940 |

**Falsification of the FUN_7101F27B78 hypothesis is now complete and
exhaustive.**  Its entire keyspace across the binary is the 5
per-player session fields + the 3 equipped-badge slots.  No badge-
ownership-bitfield accessor lives here.

Note also that the original run #8 reported FUN_7101c62368 as
containing all 8 callsites — that was the decompiler's CTRL-flow
inlining view.  The 8 callsites are actually split across c62368 (2)
and c623ec (6); they are likely a populate-once + populate-per-player
pair, or two sister populators inlined into one source-level function.
Either way, neither contains a u64-bitfield consumer.

Moving on per the priority list below.

### Why FUN_7101c623ec is almost certainly NOT a badge-ownership site

Evidence:
1. Both confirmed-correct hashes in c623ec are the SAME hashes seen in
   c62368: `0x1415f836` (P2 badge equipped) and `0xb1ae38a4` (P3 badge
   equipped). The exact same per-player session fields.
2. The post-extract for these two known equipped-badge hashes are
   FUN_7101c62760 and FUN_7101c62940 respectively — same address
   region as the c62368 family of small per-slot extractors.
3. No `BIT-ITER NEARBY` marker fired at any callsite in either
   function.  c623ec contains no `rbit` / `clz` / `tbnz` / repeated
   `lsr` patterns that a 64-bit bitfield decoder would need.
4. The script found ONLY 8 xrefs to FUN_7101F27B78 across the entire
   binary, both in functions already characterized as session
   populators.  There is no third, badge-specific call site.

**Conclusion: FUN_7101F27B78 is the per-player-session-field accessor.
The badge ownership bitfield is NOT accessed through it.** The earlier
hypothesis ("badge ownership is hash-keyed via FUN_7101F27B78") is
falsified by exhaustion of the xref list.

### What we still don't know — and the path forward

The badge ownership bitfield is a u64 living in the live `gmd::`
state.  Save-diff proved it lands at file offset `0x0EA0` in the
trailing region (outside containers A and B).  That means there's
either:
- (Option A) A DIFFERENT GameDataMgr accessor family for u64-typed
  fields (e.g., a GetU64-by-hash analog of FUN_7101F27B78).  We have
  not enumerated this family yet.
- (Option B) A separate manager object (e.g., `BadgeMgr`) reached
  through a member pointer in `gmd` at a fixed struct offset rather
  than via hash.  In that case the writer signature is `(BadgeMgr*,
  uint64_t)` or `(BadgeMgr*, uint8_t internal_id)` and the discovery
  path is through whichever `nn::nin::badge` / equip-menu function
  loads the manager pointer from `gmd + N`.
- (Option C) The bitfield is stored in Container B (which run #8
  noted contains "typed-virtual" sub-objects at offsets +0x2b0..+0x2cc
  of gmd) under a hash that uses a DIFFERENT writer family from
  container A's `FUN_710049F648`.  Container-B writers are still TBD
  per the summary at the top of this doc.

### Recommended next concrete actions (priority order)

1. **★ Re-run the patched walker** to confirm the real hashes in
   FUN_7101c623ec.  Expected: 6 distinct hashes mirroring run #8's
   slots (likely a different player or different co-op mode), no
   surprises.  This rules out a small chance that one of the hybrid
   hashes was coincidentally a real per-callsite hash.

2. **★★ Enumerate OTHER accessor families on `gmd::sInstance`**.
   We have:
   - `FUN_710012AE94` — container-A counter READER
   - `FUN_710049F648` — container-A counter WRITER ★
   - `FUN_71003838AC` — sub-bool READER (INTRO / COMPLETE_GAME)
   - `FUN_71003877C4` — enum-typed WRITER (discovered run #8)
   - `FUN_7101F27B78` — object-pointer GETTER (this run)

   The `find_gamedatamgr_xrefs.py` from sprint 2 should be re-run
   with a wider radius and the new families excluded, to surface
   accessors we haven't seen yet.  A u64 GetByHash / SetByHash family
   is the prime target.

3. **★★ Examine the GameDataMgr vtable**.  The accessors above are
   all standalone functions.  If GameDataMgr has a typed-virtual
   dispatch table — likely, given run-#8's container-B "typed-virtual"
   struct stride observation — the badge bitfield accessor may live
   there.  Locate the vtable via `_ZTVN3gmd11GameDataMgrE` in
   [switch-mod/syms/100/gmd/GameDataMgr.sym](../switch-mod/syms/100/gmd/GameDataMgr.sym)
   and dump its slots.

4. **★ Find the live BadgeMgr from a known anchor**.  HamletDuFromage's
   "have all badges" cheat (if it exists in the cheat-DB submodule)
   would point straight at the live bitfield address.  Walking write
   xrefs to that address gives the grant primitive directly without
   needing to crack the GameDataMgr hash API at all.  Worth a grep
   pass through `cheats/` before more Ghidra time.

5. **Last resort: hook the save serializer** at a known offset and
   intercept the write of the u64 at save-buffer offset `0x0EA0`.
   That gives us the SOURCE pointer for the live bitfield — same end
   result as #4 but from the dataflow side rather than a memory
   anchor.

### Status of the M3.2 sprint after run #9

Eight Ghidra rounds + this one have hit a wall on the "hash-keyed via
FUN_7101F27B78" hypothesis.  The systematic exhaustion of that
hypothesis is itself a result: we now KNOW the badge ownership
bitfield is not in that accessor's keyspace, and we have a clear
shortlist of next places to look (options A/B/C above).

M3.3 (container-A grants) is unblocked and remains the higher-leverage
work to do next — flower_coin, regular_coin, the 6 Royal Seeds,
COMPLETE_GAME, INTRO are all ready to wire via `FUN_710049F648`.

M3.2 (badge bitfield) should pause on Ghidra rounds 10+ until either
(a) the cheat-DB anchor (#4 above) or (b) the GameDataMgr vtable
enumeration (#3 above) gives a fresh dataflow seed. Continuing to
hammer the current angle is yielding diminishing returns.

## 2026-05-24 — M3.2 Ghidra run #10: find_gamedatamgr_xrefs.py dispatch tally — many unknown accessors ★★

User re-ran the sprint-2 [find_gamedatamgr_xrefs.py](../scripts/ghidra/find_gamedatamgr_xrefs.py).
The dispatch-kind tally surfaced 80+ distinct direct-call targets from
sInstance loads.  The 5 already-mapped accessors account for ~160 of
the 419 direct calls; the remaining ~260 calls fan out across ~75
**unknown** accessor functions.  This is a much bigger keyspace than
sprint 2 implied.

### The 5 known accessors (recap)

| NSO offset | Calls | Role |
|---|---|---|
| `0x710012ae94` | 66 | container-A counter READER |
| `0x710049f648` | 46 | container-A counter WRITER ★ |
| `0x71003838ac` | 38 | sub-bool READER |
| `0x71003877c4` | 10 | enum-typed WRITER (run #8) |
| `0x7101f27b78` | 1 | object-pointer GETTER (run #9 — falsified for badges) |

(Note: the `0x7101f27b78` count of 1 reflects that ONE sInstance load
preps for the 8 object-getter calls run #9 found — this script counts
sInstance xrefs, not accessor xrefs.)

### High-leverage UNKNOWN direct targets

Sorted by call frequency:

| NSO offset | Calls | Note |
|---|---|---|
| `0x7100124134` | **42** | adjacent to container-A reader (`0x12ae94`) — likely sibling reader of different type |
| `0x7100472be4` | **41** | standalone, very common primitive |
| `0x7100221128` | 18 | paired with `0x7100221278` (8 calls) — sister functions |
| `0x7100370264` | 16 | near sub-bool reader (`0x3838ac`) |
| `0x71000e258c` | 14 | low-region accessor |
| `0x710049ea24` | **14** | **ADJACENT to container-A writer (`0x49f648`)** — prime sibling-writer suspect |
| `0x7100533fe4` | 13 | mid-region |
| `0x71005046dc` | 9 | mid-region |
| `0x71000ed07c` | 8 | low-region |
| `0x71005c1a18` | 7 | |
| `0x71005e2528` | 7 | **close to `FUN_71005E93FC`** (doc-flagged container-B writer candidate) |
| `0x71006650ec` | 7 | |
| `0x71006ad0e0` | 6 | |
| `0x7100387a84` | 6 | adjacent to enum writer (`0x3877c4`) |
| `0x71009b4400` | 6 | |
| `0x710049ea78` | 5 | second sibling adjacent to writer |
| `0x7101f2b354` | 5 | `0x7101f2****` cluster — siblings of run-#9 object-getter |
| `0x7101f290cc` | 4 | `0x7101f2****` cluster |
| `0x7101f2b584` | 4 | `0x7101f2****` cluster |
| `0x710059f894` | 1 | ★ explicit container-B writer candidate from sprint 2 doc |
| ... | | tail of 1-3-call accessors |

Plus 1 `vtable[0x20]` dispatch (worth identifying its concrete method).

### Spatial clustering observations

- **Cluster `0x710012XXXX`**: container-A reader `0x12ae94` + unknown `0x124134` (42 calls) + unknown `0x1242d4` (1 call) + unknown `0x154a40` (2 calls).  Strong hypothesis: `0x124134` is a u64-typed sibling reader (or a list/array reader) in the same module.
- **Cluster `0x710049XXXX`**: container-A writer `0x49f648` + unknowns `0x49ea24` (14) + `0x49ea78` (5).  The two adjacent unknowns are almost certainly typed-sibling writers — e.g., `0x49ea24` for one width (u64?) and `0x49ea78` for another.
- **Cluster `0x710038XXXX`**: sub-bool reader `0x3838ac` + enum writer `0x3877c4` + unknown `0x387a84` (6).  Likely another type in the typed-accessor family.
- **Cluster `0x710022XXXX`**: paired unknowns `0x221128` (18) + `0x221278` (8) — reader/writer pair?
- **Cluster `0x710037XXXX`**: unknowns `0x370264` (16) + `0x3703e4` (2) — paired.
- **Cluster `0x7101f2XXXX`**: object-getter `0x7101f27b78` + many small-frequency siblings (`0x7101f290cc`, `0x7101f2a3b8`, `0x7101f2ade0`, `0x7101f2b354`, `0x7101f2b584`, `0x7101f2c118`, `0x7101f2c474`, `0x7101f2c50c`, `0x7101f2cae0`, `0x7101f2cfdc`, `0x7101f2d4d8`).  This is a whole family of specialized typed-object accessors — almost certainly the type-virtual-dispatch ladder for container B.

### Plan for run #11: multi-target hash harvest

Rather than re-aim the single-target walker 20+ times, run a single
multi-target hash walker over the entire candidate list and cross-
correlate.  New script: [scripts/ghidra/walk_multi_accessor_hashes.py](../scripts/ghidra/walk_multi_accessor_hashes.py).

For each candidate accessor it emits:
- Total xref count walked
- Per-hash callsite count, bucketed as KNOWN (one of 26 corpus hashes)
  / UNKNOWN (new keyspace candidate) / unresolved
- Aggregate **u64-footprint count** (`ldr x_` / `str x_` within
  ±16 insns of the call — indicates 8-byte payload movement)
- Aggregate **bit-iter count** (`rbit` / `clz` / `tbz` / `tbnz`
  within ±16 insns — indicates bitfield decode)
- Composite score weighted toward "unknown hashes + u64 + bit-iter"

Output ends with:
1. Per-accessor breakdown (hash list w/ KNOWN / UNKNOWN labels)
2. Ranked summary (most-likely-badge-u64-accessor first)
3. Aggregated unknown-hash corpus across ALL candidates — these are
   the NEW hash keys we'll add to the project corpus, and one of
   them is likely the badge ownership key.

Decision tree for what the output reveals:

- If an accessor's hashes are mostly KNOWN (e.g., flower_coin /
  regular_coin / GRAND_SEED_*), it's a sibling reader/writer for the
  same fields — useful for cross-validation but not for new grants.
- If an accessor's hashes are mostly UNKNOWN AND it shows a high u64
  footprint, it's a strong badge-u64 accessor candidate.  Decompile
  that function next.
- If an accessor shows a high bit-iter footprint, its callers do
  bitfield decode — the value being read/written is a bitfield (u64
  badge mask is the obvious candidate).
- If the same UNKNOWN hash appears across MULTIPLE candidate accessors
  (e.g., one reader + one writer), it's a strong "this field is real,
  cross-validated" signal — those are the highest-confidence new hash
  keys to investigate.

## 2026-05-24 — M3.2 Ghidra run #11: walk_multi_accessor_hashes.py — cross-validated u64 candidate hashes ★★★

User ran [walk_multi_accessor_hashes.py](../scripts/ghidra/walk_multi_accessor_hashes.py)
against 22 candidate accessors.  The output is a goldmine.  Headline:
**4 hashes appear in BOTH `FUN_7100124134` (the prime adjacent-to-reader
candidate) AND `FUN_710049ea24` (the prime adjacent-to-writer candidate)**.
If those two functions form a u64-typed reader/writer sibling pair to
the known u32 counter accessors, then those 4 hashes name **u64 fields
hash-keyed in GameDataMgr** — and one of them is plausibly the badge
ownership bitfield.

### ★★★ Top finding: 4 cross-validated u64 candidate hashes

| Hash | Calls via `FUN_7100124134` (reader?) | Calls via `FUN_710049ea24` (writer?) |
|---|---|---|
| `0x0d5de3d5` | 1 | 1 |
| `0x1faf41e5` | 1 | 1 |
| `0x9fd4fe00` | 1 | 1 |
| `0xe237fbc6` | **2** | 1 |

These are the highest-confidence new hash keys to investigate.  Each
one is read AND written by the same sibling accessor pair — that's the
pattern of a real field, not a transient computation.  The single
write per field is consistent with a one-shot init/update path; the
multiple read of `0xe237fbc6` is consistent with that being a
frequently-queried field (e.g., "do I own badge N?" check).

Additionally, the readers and writers BOTH show high u64-footprint
counts (150 and 127 respectively) — `ldr x_` / `str x_` 8-byte memory
ops near the call sites, fitting a u64 payload.  `FUN_7100124134`
also shows 50 bit-iter hits (rbit/clz/tbz/tbnz) — heavy bit-decode
context, exactly what a badge ownership bitfield consumer would do.

### Per-accessor summary (by relevance)

**Prime targets — decompile these next:**

| Accessor | Calls | Unknown hashes | u64 fp | bit-iter | Verdict |
|---|---|---|---|---|---|
| `FUN_7100124134` | 55 | 13 | 150 | 50 | **★ prime u64 READER suspect.** Heavy bit-decode context. |
| `FUN_710049ea24` | 29 | 10 | 127 | 24 | **★ prime u64 WRITER suspect.** 4 keys cross-validated with the reader candidate. |
| `FUN_7100221128` | 35 | 22 | 101 | **55** | **★ highest bit-iter score** — but NO key overlap with 124134/49ea24. Different data domain — maybe per-course progress bitfields or per-stage flags. |

**Secondary targets:**

| Accessor | Calls | Notes |
|---|---|---|
| `FUN_7100221278` | 22 | Sister of 221128; 6 unknown hashes; high bit-iter (42).  Same family as 221128. |
| `FUN_7100472be4` | 56 | Standalone "very common" primitive; 18 unknown hashes; includes the KNOWN bool hash `0xed817774` — confirms it's a bool/flag READER or WRITER. |
| `FUN_71005e2528` | 10 | Doc-flagged container-B writer candidate; includes KNOWN `0xf4ee6827` (flower_coin) — suggests this is ANOTHER flower_coin-related accessor (probably the container-B sister of `FUN_710049F648`). |
| `FUN_71009b4400` | 8 | Includes KNOWN `0x89f1cc52` (INTRO_CUTSCENE_COMPLETED) — bool/flag accessor. |
| `FUN_71000ed07c` | 17 | Includes 5 KNOWN per-player session hashes + 4 UNKNOWN — almost certainly an additional per-player session populator (different from c62368/c623ec) — uninteresting for badges, but the 4 unknowns might be new session fields. |
| `FUN_7100370264` + `FUN_71003703e4` | 33 | Paired; 2 cross-validated keys (`0x7940dc77`, `0xdcf45353`).  Different domain — small subspace. |

**Hash-unresolved accessors (calling-convention may differ — w2 not directly built via mov/movk):**

| Accessor | Calls | Unresolved |
|---|---|---|
| `FUN_710059f894` | 27 | **27/27 (100%)** — explicit container-B writer candidate from sprint 2 doc; calls don't pass hash in w2 via mov/movk.  Hash likely loaded from memory (e.g., `ldr w2, [x_, #_]`).  **Decompile to determine signature.** |
| `FUN_7100533fe4` | 15 | 15/15 (100%) — same situation; calling convention obscures the walker. |
| `FUN_710049ea78` | 9 | 9/9 (100%) — second adjacent-to-writer sibling; calling convention obscures the walker. |
| `FUN_7100387a84` | 10 | 10/10 (100%) — same. |

These "100% unresolved" accessors are NOT a script bug per se — the
walker explicitly stops at prior `bl` and assumes hash is built via
immediate.  If the call site does `ldr w2, [x_, #const]` (load hash
from a struct/array) or reuses w2 from a prior call, the walker can't
recover it.  Decompiling one of these manually will reveal the actual
calling convention.

### Murmur3 brute-force re-run with new candidate hashes — STILL no hits

User's existing [scripts/brute_badge_field_hashes.py](../scripts/brute_badge_field_hashes.py)
extended with all 27 new high-priority hashes from this run (4
cross-validated + 23 secondary).  Result: **no candidate badge field
name reproduces any of them under Murmur3-32(seed=0)**.  Confirms:
- Field names are in Japanese / encoded / not in our candidate list, OR
- Field names use a different hash function than course names do.

Brute-forcing names is dead.  We need the function decompiles to know
what the hash keys mean.

### Aggregated UNKNOWN hash corpus (87 new hashes)

Across all 22 accessors, 87 distinct unknown hashes surfaced.  See
script output for the full list.  Notable cross-validations beyond
the headline 4:

| Hash | Cross-validated via |
|---|---|
| `0x0d5de3d5` ★★ | FUN_7100124134 + FUN_710049ea24 (R + W pair) |
| `0x1faf41e5` ★★ | FUN_7100124134 + FUN_710049ea24 (R + W pair) |
| `0x9fd4fe00` ★★ | FUN_7100124134 + FUN_710049ea24 (R + W pair) |
| `0xe237fbc6` ★★ | FUN_7100124134 + FUN_710049ea24 (R + W pair) |
| `0x7940dc77` ★ | FUN_7100370264 + FUN_71003703e4 (paired family) |
| `0xdcf45353` ★ | FUN_7100370264 + FUN_71003703e4 (paired family) |
| `0xaae9c08e` | FUN_7100472be4 + FUN_71009b4400 |
| `0xcd0b87d1` | FUN_7100472be4 + FUN_71009b4400 |
| `0xf49dab7f` | FUN_7100472be4 + FUN_71009b4400 |
| `0x2476e30e` | FUN_71003703e4 + FUN_71005046dc |

### Recommended next action: decompile the prime targets

Three Ghidra decompile windows, paste full bodies back:

1. **★★★ `FUN_7100124134`** — confirm it's a u64 reader; signature
   should be `(GameDataMgr*, uint64_t* out, uint32_t hash)`.  If the
   internal logic mirrors the container-A reader `FUN_710012AE94`'s
   bucket-walk but reads 8 bytes instead of 4, this is a u64 READER
   for a yet-unknown container or subspace.
2. **★★★ `FUN_710049ea24`** — confirm it's a u64 writer; signature
   should be `(GameDataMgr*, uint64_t value, uint32_t hash)`.  If the
   internal logic mirrors writer `FUN_710049F648` with a u64 payload,
   this IS the badge-grant primitive (modulo identifying which of the
   4 hashes is the badge key).
3. **★★ `FUN_7100221128`** — confirm what data domain this manages;
   the 55-hit bit-iter score is the highest of any accessor.  If it
   operates on a different struct than 124134/49ea24, it might be the
   per-course flag accessor we'd need for M3.5 anyway.

After decompiles:
- If 124134 + 49ea24 confirmed as u64 R/W pair: draft `GrantBadge()`
  using `FUN_710049ea24(gmd, 1ULL << internal_id, HASH_BADGE_OWNED)`
  for each of the 4 cross-validated hashes in turn, and verify via
  save-diff which one makes bit `internal_id` appear at file offset
  `0x0EA0`.
- If 49ea24 is something else: re-examine the next-highest-scoring
  candidates from the ranking.

This is the cleanest discovery path the sprint has produced.  The
cross-validated hash signal is exactly the signal we've been hunting
for since run #1.

## 2026-05-24 — M3.2 Ghidra run #12: prime-target decompiles — Container C discovered ★★★

User pasted decompiles of FUN_7100124134, FUN_710049ea24, FUN_7100221128.
The result: a **previously-unmapped container ("Container C") at
gmd+0x70..0x8c** holding hash-keyed BITFIELDS — exactly the shape the
badge ownership u64 needs.  But the writer hypothesis for 49ea24 was
wrong; it's a 1-bit/bool writer, not a u64 writer.  We still need to
find the container-C writer.

### FUN_7100124134 — Container-C per-bit READER  (4-arg, not 3!)

True signature:

```c
undefined8 FUN_7100124134(GameDataMgr* gmd,
                          byte* out_bit_value,
                          uint32_t hash,
                          uint32_t bit_index);
```

The walker harvested only `param_3` (w2 = hash) and missed `param_4` (w3 = bit_index).

Body:
1. Loads bucket array from `gmd+0x80`, bucket count from `gmd+0x8c`.
2. Open-addressing probe: `(hash mod count)` then linear probe.
3. On key match: indexes into typed-virtual sub-object array at
   `*(long*)(gmd+0x78)` (0x40-byte struct stride).
4. Calls vtable slot 0x20 on the sub-object — returns int `bit_count`
   (the size of this bitfield in bits).
5. If `bit_index < bit_count`, reads bit from `uint32_t[]` stored at
   sub-object +0x28:

   ```c
   *out_bit_value = (data[bit_idx >> 5] >> (bit_idx & 0x1f)) & 1;
   ```

6. On fall-through, tail-calls `FUN_7100124250(gmd+8, out, hash)` — a
   parent-container reader (the +8 substruct hierarchy).

**Implication: Container C is at gmd+0x70..0x8c, holds hash-keyed
bitfields of arbitrary width, stored as `uint32_t[]` arrays inside
typed-virtual sub-objects.**

### FUN_710049ea24 — Bool/1-bit WRITER (not the u64 writer)

True signature:

```c
void FUN_710049ea24(GameDataMgr* gmd, uint32_t value, uint32_t hash);
```

Body (concise):

```c
if (FUN_710049ea78(gmd + 0x68) & 1) return;          // init/lock gate
FUN_7101f263fc(gmd + 8, value & 1, hash);            // delegate, value masked to 1 bit
```

The `value & 1` clamp means this writes **a single bit / bool**.  It
delegates to `FUN_7101f263fc` — a generic typed-virtual writer in
the 0x7101f2**** cluster (same family as the run-#9 object-getter
FUN_7101F27B78).  The gmd+8 substructure is the parent container
holding the bool fields.

**This is NOT the badge bitfield writer.**  The cross-validation with
124134 on 4 hashes still suggests those 4 hashes name real
container-C fields — but the WRITER for those fields isn't 49ea24.

### FUN_7100221128 — Container-B READER (B-1 + B-2 fallback chain)

True signature:

```c
undefined8 FUN_7100221128(GameDataMgr* gmd, uint32_t* out_value, uint32_t hash);
```

Returns single u32:
1. **First**: probe container B-1 (bucket at gmd+0x260, count gmd+0x26c,
   limit gmd+0x250, struct array at gmd+0x258, stride 0x38, value at
   struct+0x1c).
2. **Fallback**: probe container B-2 (bucket at gmd+0x2c0, count
   gmd+0x2cc, limit gmd+0x2b0, struct array at gmd+0x2b8, stride 0x50,
   typed-virtual: vtable slot 0x20 returns int size, data pointer at
   struct+0x28, returns the first 4 bytes of that data).

**Container B returns u32s.**  Callers' 55 bit-iter hits indicate
the returned u32 is often used as a 32-bit bitfield.  But badges
(file offset 0x0EA0, u64 = 64 bits) don't fit in a single u32, so
badges are NOT in container B (unless they're split across two
hash-keyed fields, which would be unusual).

### Updated GameDataMgr container map

| Region | Offsets | Holds | Reader | Writer |
|---|---|---|---|---|
| Container A | `gmd+0xe0..0xf8` (run #11) | counters (u8/u16/u32) | `FUN_710012AE94` | `FUN_710049F648` ★ |
| Container B-1 | `gmd+0x250..0x26c` | u32 values (often 32-bit bitfields) | `FUN_7100221128` | TBD |
| Container B-2 | `gmd+0x2b0..0x2cc` | u32 values (typed-virtual sub-objects) | `FUN_7100221128` | TBD |
| **Container C** ★ | **`gmd+0x70..0x8c`** | **hash-keyed BITFIELDS (uint32_t[] storage, arbitrary bit count)** | **`FUN_7100124134` (per-bit)** | **TBD ★★★ — this is the M3.2 writer** |
| bool substruct | `gmd+0x08` + gmd+0x68 lock | hash-keyed bools | (TBD) | `FUN_710049ea24` → `FUN_7101f263fc` |

### Identifying the badge bitfield among the 4 cross-validated hashes

The 4 cross-validated hashes (`0x0d5de3d5`, `0x1faf41e5`, `0x9fd4fe00`,
`0xe237fbc6`) each appear in BOTH FUN_7100124134 (container-C reader)
AND FUN_710049ea24 (gmd+8 bool writer).  This is curious — why would
the same hash name a container-C bitfield AND a gmd+8 bool?

Possibilities:
- The hashes name fields that exist in multiple containers (each
  container is queried at different code paths; the hash is the
  universal key).
- One container is the "live" state; the other is a "shadow" or
  "delta queue" — the bool writer might queue a delta to be drained
  into container C at next save.
- The 4 hashes are NOT badges at all — they're shared bool/bitfield
  pairs.  In that case the badge bitfield's hash is hiding in
  FUN_7100124134's 36 UNRESOLVED callsites (where the hash is loaded
  from a struct/memory rather than built via mov/movk — the walker
  can't recover those).

Each hash maps to a specific bit_count via vtable slot 0x20 — and
**badges have a known count** (≤64).  An empirical check would
identify which of the 4 hashes (if any) maps to a 36/40/64-bit
bitfield rather than a 1-bit bool.

### Recommended next decompile order — find the Container-C WRITER

1. **★★★ `FUN_71001242d4`** — only 0x1a0 bytes (~104 bytes = ~26 insns)
   after the reader.  Tally:1 call, but writers are inherently rare
   (each badge grant is one event, vs many reads).  Prime structural
   sibling.  **Top suspect for the container-C writer.**

2. **★★ `FUN_7100124250`** — the function the reader tail-calls when
   container-C lookup fails.  Probably a parent/wrapper that descends
   into a different container (gmd+8 substructure).  Decompiling it
   tells us the container hierarchy.

3. **★★ `FUN_7101f263fc`** — the generic typed-virtual writer that
   FUN_710049ea24 delegates to.  This dispatches based on field type.
   If it has a "set bit N of bitfield" path, that's a higher-level
   entry point that handles container-C bitfields too.  Decompiling
   reveals the dispatch table.

4. **★ `FUN_710049ea78`** — the lock-check helper used by 49ea24.
   If there are sibling entry points near 0x49ea24 that share the
   same lock-check + delegate-to-typed-writer pattern, one of them
   might be the container-C bitfield writer.  Decompile is small
   (5 callsites).

### Empirical-shortcut alternative — probe mod

A 1-shot probe could short-circuit further static analysis: now that
FUN_7100124134 has a known signature, build a tiny mod that calls
it once per (hash × bit_idx) for the 4 cross-validated hashes from
bit 0..63.  Log the results; trigger a save with one specific badge
equipped; restart; log again.  The bit that flips identifies the
badge bitfield's hash AND the bit's internal_id mapping in one save
cycle.

Save the empirical shortcut for after we've decompiled #1-#3 above —
if any of those decompiles reveals the writer, we don't need the
probe at all.

### Sprint status — Container C discovery is the breakthrough

After 11 prior rounds chasing dead ends, run #12 finally identified
the storage container for hash-keyed bitfields.  The badge ownership
u64 lives in Container C (or, with low probability, is the few-bit
bool we'd see in gmd+8).  The writer is one or two decompiles away.

Once the writer is in hand, the M3.2 grant primitive draft from the
handoff doc still applies (substitute the new writer address and the
correct of-the-4-or-otherwise hash):

```cpp
void GrantBadge(uint8_t internal_id) {
    auto* gmd = *(GameDataMgr**)(GetTargetStart() + 0x0363F0F0);
    if (!gmd) return;
    using SetBitFn = void (*)(GameDataMgr*, uint32_t hash, uint32_t bit_idx);
    auto set_bit = reinterpret_cast<SetBitFn>(
        GetTargetStart() + /* TBD container-C writer offset */);
    set_bit(gmd, HASH_BADGE_OWNED, internal_id);
}
```

## 2026-05-24 — M3.2 Ghidra run #13: 1242d4 / 124250 / 7101f263fc decompiles — none is the writer ★

User pasted decompiles of the three recommended targets.  None is the
container-C bitfield WRITER — but `FUN_7101f263fc` reveals the generic
typed-virtual write machinery, which lets us pivot to an empirical
hook strategy.

### FUN_71001242d4 — second container-C per-bit READER (different fallback)

Same 4-arg signature as 124134: `(gmd, &out, hash, bit_idx)`.  Probes:
1. A virtual call on `gmd+0x68` (slot 0x20) — likely a delta/overlay container queried first.
2. Container C (gmd+0x80 bucket) — same as 124134.
3. Tail-calls `FUN_7100124518(gmd+8, &out, hash)` — bool/byte reader for the deeper substruct.

Not a writer.  Reveals a 3rd container layer (gmd+0x68) we hadn't
seen — but its semantics (delta/overlay vs persistent) need more
context to nail down.

### FUN_7100124250 — bool/byte reader for the gmd+8 substruct

3-arg `(substruct, &out_byte, hash)`.  Opens hashtable at substruct+0x18
(count substruct+0x24), open-addressing probe, on key match reads a
single byte from struct+0x16 of a 0x18-byte typed substruct array at
substruct+0x10.  This is the read counterpart of `FUN_7101f263fc`.

### FUN_7101f263fc — deferred-write bool WRITER for gmd+8 substruct ★

3-arg `(substruct, byte value, hash)`.  Pattern:

```c
// 1. Validate hash exists in container (substruct+0x18 bucket).
// 2. ARM ExclusiveMonitor atomic enqueue to ring buffer:
puVar5 = substruct + 0x38;           // head/state word
iVar9  = *(int*)(substruct + 0x28);  // capacity
while (head < capacity) {
    ExclusiveMonitorPass(puVar5, 0x10);
    if (cas_success) {
        *puVar5 = head + 1;
        if (ExclusiveMonitorsStatus() == 0) {
            // Got our slot — write the entry
            entry = substruct + 0x30 + slot_idx * 8;
            entry[4..7] = hash;       // key
            entry[0]    = value & 1;  // bool value
            // ... atomic finalization ...
        }
    }
}
```

This is **exactly the same lock-free deferred-write pattern** as the
container-A writer `FUN_710049F648` (which uses gmd+0xf8 ring buffer
+ gmd+0x100 head/state).  The substructure offsets here are
`+0x18 bucket / +0x28 capacity / +0x30 ring / +0x38 head`.

**Critical implication: there's almost certainly a SISTER WRITER in
the 0x7101f2**** cluster that takes a 4th argument `bit_idx` and
enqueues `(hash, bit_idx, value)` for container-C bitfields.**  The
writer family is structurally typed — bool, u32, u64, bitfield are
sibling entry points that all use the same deferred-write ring
buffer pattern but differ in payload shape.

### Pivot: empirical hook strategy

After 4 successful Ghidra rounds in this sprint (runs #10-13), we have
the discovery pattern down but the writer remains hidden in a cluster
of 20+ unidentified 0x7101f2**** sibling functions.  Rather than
brute-force decompile each, user opted to **install logging hooks on
the known typed-virtual writers** + the container-C reader, trigger a
badge unlock in-game, and watch which hook (if any) fires.

Hooks to install in [switch-mod/src/program/main.cpp](../switch-mod/src/program/main.cpp):

1. `FUN_710049F648` (NSO +0x49F648) — container-A counter writer.
   Logs `(gmd, value, hash)`.  If this fires on badge unlock, badges
   ARE in container A (unlikely per save-diff but worth confirming).
2. `FUN_7101f263fc` (NSO +0x1F263FC) — gmd+8 bool writer.  Logs
   `(substruct, value, hash)`.  If this fires, badges are tracked
   as bools in gmd+8 keyed by a per-badge hash (36 hashes).
3. `FUN_7100124134` (NSO +0x124134) — container-C per-bit reader.
   Logs `(gmd, &out, hash, bit_idx)`.  Will fire frequently in
   gameplay; expect a flurry on UI screens that show owned badges.
   Pre-and-post badge unlock comparison should reveal which
   (hash, bit_idx) pair was being queried right before/after the
   unlock event.

After badge unlock in Ryujinx, tail `Ryujinx_*.log` for `[smbwap`
entries.  The expected outcome matrix:

| Fires on badge unlock? | Interpretation |
|---|---|
| `FUN_710049F648` only | Badges are container-A counter (1 hash). Use 49F648 directly as grant primitive. |
| `FUN_7101f263fc` only | Badges are gmd+8 bool, one hash per badge. Use 7101f263fc as grant primitive; need to enumerate per-badge hashes. |
| `FUN_7100124134` queries after unlock | UI is querying the container-C bitfield; identifies the badge hash from the query args. The WRITER is somewhere else (yet to be hooked); follow up by adding hooks on the rest of the 0x7101f2 cluster. |
| Nothing fires | Badges use a completely different write path (e.g., a per-Mgr direct function). Add hooks on more candidates. |

This pivots from static-analysis-only to **dynamic discovery** for the
write path, while leveraging the static knowledge of the read path
(which gives us the hash + bit_idx interpretation in the log output).

## 2026-05-24 — M3.2 SOLVED ★★★★

After the dynamic hook pivot, the badge-grant primitive landed cleanly.
Spring Feet (internal_id 4) was granted live in-game with immediate UI
feedback (no save+reload required).

### The grant primitive

```cpp
static constexpr uint32_t kBadgeOwnedHash = 0x105df820;

void GrantBadge(uint8_t internal_id) {
    auto* gmd = *(GameDataMgr**)(GetTargetStart() + 0x0363F0F0);
    if (!gmd) return;
    long bucket = *(long*)(gmd + 0x80);
    uint32_t bucket_count = *(uint32_t*)(gmd + 0x8c);
    uint32_t limit = *(uint32_t*)(gmd + 0x70);
    long obj_array = *(long*)(gmd + 0x78);
    // Open-addressing probe for kBadgeOwnedHash:
    uint32_t cur = kBadgeOwnedHash % bucket_count;
    uint32_t initial = cur;
    do {
        uint32_t key = *(uint32_t*)(bucket + cur * 8);
        if (key == kBadgeOwnedHash) {
            uint32_t idx = *(uint32_t*)(bucket + cur * 8 + 4);
            if (idx >= limit) return;
            uint32_t* data = *(uint32_t**)(obj_array + idx * 0x40 + 0x28);
            if (!data) return;
            uint32_t word = internal_id >> 5;
            uint32_t mask = 1u << (internal_id & 0x1f);
            data[word]     |= mask;
            data[word + 2] |= mask;  // mirror at u32[2]/u32[3]
            return;
        }
        if (key == 0) return;
        cur = (cur + 1) % bucket_count;
    } while (cur != initial);
}
```

### Discovery summary — what each step contributed

| Stage | Result |
|---|---|
| Run #9 walker | Falsified FUN_7101F27B78 hypothesis exhaustively |
| Run #10 dispatch tally | Enumerated ~75 unknown GameDataMgr accessors |
| Run #11 multi-target hash harvest | Cross-validated 4 candidate u64 hashes shared by reader/writer siblings |
| Run #12 decompiles | Discovered Container C at gmd+0x70..0x8c; identified per-bit reader signature |
| Hook iteration 1 (writer hooks) | No badge writes visible to writer hooks — pivoted to dump-on-write |
| Hook iteration 2 (container-C diff dumper, cap=64) | Missed badge bitfield due to snapshot cap overflow |
| Hook iteration 3 (cap=256) | Captured 156 bitfields; still didn't see badge change on purchase |
| Hook iteration 4 (per-bit reader hook on FUN_71001242d4) | Surfaced hash `0x6d1b5c25` from successful badge UI queries — but this turned out to be a UI-slot bitmap, NOT the owned bitfield |
| Hook iteration 5 (direct write to 0x6d1b5c25) | Write persisted to file offset 0x1204; bit 30/31 filtered by save serializer; no in-game effect |
| Save-diff with "all bits" experiment | Revealed `0x6d1b5c25` writes hit file 0x1204, not 0x0EA0 — the real owned bitfield is at 0x0EA0 |
| Final grep for `00000200 0000400c` pattern | Identified `0x105df820` at live addr `0x20d3da8c70` as the actual owned bitfield |
| Hook iteration 6 (grant via 0x105df820) | ★ Spring Feet granted live in-game |

### Container map (final)

| Region | Holds | Reader | Writer | File offset (canonical) |
|---|---|---|---|---|
| Container A | counters | `FUN_710012AE94` | `FUN_710049F648` | pair region |
| Container B-1/B-2 | u32 values | `FUN_7100221128` | TBD (lower priority) | various |
| Container C | bitfields (uint32_t[]) | `FUN_7100124134` | direct memory write ★ | various |
| Container C entry `0x105df820` | **badge owned u64** | (above) | `GrantBadge()` above | **0x0EA0 u64 LE** |
| Container C entry `0x6d1b5c25` | UI-slot mask (auxiliary) | (above) | (not needed for grants) | 0x1204 u32 |
| gmd+8 substruct | bools | `FUN_7100124250` | `FUN_7101f263fc` | various |

### Sprint metrics

- Static-analysis Ghidra rounds: 14
- Dynamic hook iterations: 6
- Sister project lessons reused: 0 (this is the first save-data RE work for this codebase)
- Lines of mod code added: ~150
- M3.2 is unblocked end-to-end for the AP bridge (M4) work.

## 2026-05-25 — M3.3b SOLVED (no new RE required)

Royal Seed bool grant primitive shipped via `probe::grantContainerBBool`
in [switch-mod/src/program/main.cpp](../switch-mod/src/program/main.cpp).
Calls `FUN_710049EA24` at NSO `+0x0049EA24` (the high-level bool wrapper),
which gates on the gmd+0x68 init/lock and delegates to
`FUN_7101F263FC(gmd+8, value & 1, hash)` — already documented above
(lines 3170-3171, 3215, 3327, 3467) as the "deferred-write bool WRITER
for gmd+8 substruct".

**The entire M3.3b work was reading our own notes.** Sprint-2 had
already identified both the wrapper and its delegate; only the
`probe::grantContainerBBool` primitive (mirroring `grantContainerACounter`)
and the `isBoolHash()` dispatch branch in
[ApFrameBridge.cpp](../switch-mod/src/program/ap/ApFrameBridge.cpp)
`drainInbound()` needed writing.  Total switch-mod diff: ~70 LoC.

**Live validation 2026-05-25**: boot-time smoke test wrote all 8
documented bool hashes (6 Royal Seeds + COMPLETE_GAME + INTRO).
Save-diff confirmed 6 expected byte flips at pair-region value
offsets (W2 @ 0x0064, W3 @ 0x0384, W4 @ 0x01F4, W5 @ 0x036C,
W6 @ 0x00BC, COMPLETE_GAME @ 0x0044); W1 (0x0354) and INTRO (0x012C)
were already `0x01` in the test save and the writer was correctly
idempotent.  Existing `GmdBoolWriter` trampoline at NSO `+0x01F263FC`
(installed in M3.2 as a badge-investigation probe) provided free
observability: each log line showed `substruct = gmd + 8` exactly as
predicted.

**Lesson**: when sprint-2-style dataflow analysis produces a "writer
candidate" annotation, treat it as ready-to-ship pending a one-line
empirical confirmation.  We deferred M3.3b 24 hours by labelling the
M3.3 falsification a "container-B writer hunt" when the writer was
already named in our own findings doc.

---

## 2026-05-25 — M3.8 outbound: death-vs-noise discriminator

The "scene transition" active Nerve at vtable offset `0x33fd9a8` fires on
Mario death AND on player-controlled course transitions (restart, exit).
The outbound DeathLink filter rides on distinguishing these.

**5-event observation run (R-2026-05-25 08:03:50)** — user triggered, in order:

| Fire # | Event | Nerve `this` | `+0x18` (u64 LE)     |
|--------|-------|--------------|----------------------|
| 1      | restart course | `0x20c0daa7d0` (nerve_A) | `0x00ff003700000084` |
| 2      | death (pit)    | `0x20c0d57e30` (nerve_B) | `0x00ff000600000004` |
| 3      | death          | `0x20c0d57e30` (nerve_B) | `0x00ff000600000004` |
| 4      | exit course    | `0x20c0daa7d0` (nerve_A) | `0x00ff003700000084` |
| 5      | death          | `0x20c0d57e30` (nerve_B) | `0x00ff000600000004` |

**Discriminator**: `*(uint32_t*)(nerve + 0x18) == 0x00000004` -> death.
Per-instance signal: nerve_A and nerve_B are distinct C++ objects sharing
the same vtable, each storing a type-discriminating enum at `+0x18`.
Death is whitelisted in `NerveActivateOnce::Callback`'s scene-transition
branch (`kDeathDiscriminator_Off=0x18, kDeathDiscriminator_Val=0x4`); any
other value logs as `SCENE_TRANSITION non-death (state=0xN)` and drops --
conservative against unobserved siblings (world-map travel, palace clear,
pause-quit, file-select were NOT in the observation pass).

---

## 2026-05-25 — M3.8 inbound kill: corrected anchor

The bundled M3.8 scaffolding from 85ccf59 latched X22 from the flower_coin
writer at NSO `+0x49253C` and wrote uint8 0 to `+0x1C`, on the assumption
that both HamletDuFromage cheat anchors targeted the same "live_base"
struct.

**Falsified live 2026-05-25**:
```
[smbwap inf] synthKill: wrote HP=0 at live_base=0x20dc152070 +0x1C
```
Mario kept playing.  Cross-anchor was an inference, not a verified fact.
The two cheat anchors target two different structs:

- `+0x49253C` (Coins (Purple)) uses **X22** in `FUN_7100491f60`, where
  decompilation shows X22 = `(int*)param_1[7]` — a "stats / counters"
  sub-object.  Has flower_coin at `+0xC8`, but `+0x1C` is unrelated.
- `+0x2743C0` (Disable Death) uses **X9** in a different function — the
  death-check code path inside `FUN_7100273868`.

**Real HP location** — Ghidra trace of the death-check code path:
```
+0x2743b8: ldr   x9, [x9, x10, LSL #0x0]   ; x9 = HP-bearing struct
+0x2743bc: cbz   x9, LAB_71002743d0         ; null check
+0x2743c0: ldrsh w9, [x9, #0x38]            ; load HP (signed int16)
+0x2743c4: cmp   w9, #0
+0x2743c8: b.le  LAB_710027593c              ; <= 0 -> death handler
```

HP is a **signed int16 at +0x38** of the X9-pointed struct.  The `+0x1C`
the cheat manipulates is just an "alive flag" hack on a different field
in the same struct — overwriting it makes the death check skip, but
zeroing it doesn't trigger death the way zeroing the HP halfword does.

**Inline hook at the cbz** (NSO `+0x2743BC`) was the first retry —
exlaunch's relocator handles `cbz` + `b.le` per `__fix_cond_comp_test_branch`
in [hook_impl.cpp](../switch-mod/src/lib/hook/nx64/hook_impl.cpp).  Live
result: Ryujinx silently terminated when the patched code path was
reached, even though the latch never fired.  Some interaction in the
patch window we don't understand; abandoned for safety.

**Final implementation — trampoline at the function entry**.
`PlayerTickLatch::Callback` (`HOOK_DEFINE_TRAMPOLINE` at NSO `+0x273868`)
reads `X0 = param_1` (which the function uses internally to reach the HP
struct) and replicates the dereference chain in C:

```cpp
x8        = *(param_1 + 0x10);
arr       = *(x8 + 0x208);
ver       = *(x8 + 0x200);
off       = (ver > 0x23) ? 0x118 : 0;
hp_struct = *(arr + off);    // latch once via compare_exchange
```

`probe::synthKill` writes int16 0 to `hp_struct + 0x38` and sets
`g_synthetic_death_this_frame` (consumed by the outbound nerve filter to
suppress the echo).

**Live-validated 2026-05-25 09:00:34**:
```
PlayerTickLatch: latched live_base=0x20a1f27030 (HP int16 at +0x38; ver=0x7c off=0x118)
synthKill: wrote HP=0 (int16) at live_base=0x20a1f27030 +0x38
```
Mario died on the first frame.  Production path is now AP Bounce ->
`SMBWContext.on_deathlink` -> `LanServer.send_kill` -> Switch
`drainInbound` -> `probe::synthKill`.

**Lessons**:
- "Cheat DB anchor A and anchor B use the same register name" is NOT
  evidence they target the same struct.  Verify with decompile.
- exlaunch's inline-hook relocator nominally handles PC-relative
  branches, but the empirical record shows at least one specific patch
  window (the one starting at `+0x2743BC`) where it doesn't.  Function-
  entry trampolines are the safer default.
- When the function uses an internal dereference chain to reach the
  data you want to latch, replicate the chain in your hook callback
  rather than hooking deeper into the function body.


## 2026-05-26 — Wonder Seed gate-check RE (scaffold)

**Status**: SCAFFOLD ONLY.  Plan filed at `~/.claude/plans/we-have-had-
a-calm-eclipse.md`.  This block is the empty result template that the
Ghidra-running RE session fills in; the implementation session reads
this block to write the hook.  Three new Ghidra scripts shipped with
this scaffold ([scripts/ghidra/find_gate_strings.py](../scripts/ghidra/find_gate_strings.py),
[scripts/ghidra/walk_reader_compare_sites.py](../scripts/ghidra/walk_reader_compare_sites.py),
[scripts/ghidra/playreport_field_backtrace_seed.py](../scripts/ghidra/playreport_field_backtrace_seed.py)).

### Problem statement

Every prior attempt to control Mario's visible Wonder Seed total has
fallen over because the counter is **computed** at runtime by
popcount over per-acquisition flag arrays (`+0x3348` GoalSeed,
`+0x3AF8` Wonder-Phase, `+0x3480` shop) — there is no single stored
counter to write.  Writes to the contained flag bits collide with
per-course completion state.

The pivot: **leave the visible counter alone.**  Find the in-game
function(s) that consult Wonder Seed totals when deciding whether the
player may unlock the next world, enter a palace boss, or progress to
the final boss; hook those functions and substitute the AP-granted
per-world count derived from `items_received`.

Eventual implementation will push **per-world counts** (8 buckets:
W1, W2, W3, W4, W5, W6, Petal Isles, Special World) over a new wire
message.  Scope is **all seed-gated decisions in one hook** (or a
matched set if the call graph forces it).

### Known-going-in facts (don't re-derive)

- The lifetime counter at hash `0x8c20ccb7` (container A) is
  recomputed from per-course flags at save-time.  **Not a stable gate
  input candidate** — it would show stale values mid-session.  If a
  gate read this, the game's behavior would drift away from the
  visible counter as the player progressed, which doesn't happen.
- Hash `0xb9bd745d` is the **AP-grant queue**, NOT per-course seed
  storage (corrected 2026-05-25; see memory note
  `smbwap_wonder_seed_counter_candidate.md`).
- `FUN_7101F2B354` is statically inferred only — untested at runtime.
  Do not assume it.
- Confirmed seed-related accessors:
  - `FUN_710012AE94` — container-A reader, `(gmd, u32* out, u32 hash)`, 66 xrefs.
  - `FUN_71003838AC` — sub-bool reader, `(sub, u8* out, u32 hash)`, 38 xrefs.
  - `FUN_7100124134` — candidate per-bit reader, 42 xrefs (signature TBD).
- `course_result` PlayReport carries `total_get_finish_seed_count`
  (PlayReport builder chain: `FUN_7101a5d93c` → `FUN_7101a5d9a0` →
  `FUN_7101a5de58` → `FUN_7101a5ea50`).  Any non-PR-chain reader of
  the same backing storage is a gate-reader candidate.

### regions.json threshold corpus (2026-05-26)

Per-bucket thresholds from
[apworld/smbw_archipelago/data/regions.json](../apworld/smbw_archipelago/data/regions.json):

| Bucket | Thresholds |
|---|---|
| W1 Wonder Seed | 3, 10, 14 |
| W2 Wonder Seed | 4, 9, 14 |
| W3 Wonder Seed | 4, 10 |
| W4 Wonder Seed | 15 |
| W5 Wonder Seed | 6, 11 |
| W6 Wonder Seed | 15, 25 |
| Petal Isles Wonder Seed | 2, 5, 8, 10, 12, 15 |
| Special World Wonder Seed | 6, 16 |

Union: `{2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 14, 15, 16, 25}`.  Embedded
verbatim in `walk_reader_compare_sites.py` as `SEED_THRESHOLDS`.

### Phase results (2026-05-26 script runs)

#### Phase 0a — lifetime-counter falsifier

- Result: hash `0x8c20ccb7` does NOT appear in any walker output row.
  No `bl FUN_710012AE94(gmd, &out, 0x8c20ccb7)` callsite in the
  scanned readers was followed by a threshold-corpus cmp.
- Verdict: [x] confirmed stale (not a gate input)
- Notes: aligns with the 2026-05-25 memory correction.  The lifetime
  counter is recomputed-at-save-time only and is not consulted at
  decision points.

#### Phase 0b — `find_gate_strings.py`

- Result: 201 unique string addresses; 23 with ≥1 xref.
- **Strongest single hit**: `FUN_7100884040` loads BOTH `'Lock'`
  (at `0x71028d4dd6`) AND `'Unlock'` (at `0x71028d4ddb`).  Strings are
  adjacent in `.rodata` (5 bytes apart), and the function references
  both at `+0x4c` and `+0x74` from its entry.  **This is the only
  function in the binary that touches both gate-state vocabulary
  strings.**  However, it does NOT appear in the walker's top 30 —
  meaning it doesn't reach the scanned readers.  Either it inlines
  the gate logic, uses a reader we didn't scan, or is a state-enum
  helper (Lock/Unlock as state names).  Manual decompile required.
- Other high-xref hits worth following up:
  - `'GateOpen'` (8 xrefs) → `FUN_7101abd140`, `FUN_7101abd3c8`
    (gate/door object class)
  - `'Gate'` (2 xrefs) → `FUN_71005e7b28`
  - `'WonderSeed'` strings (3 instances) xref'd from:
    `FUN_71008b5768`, `FUN_71017ca460`, `FUN_71009290d0`,
    `FUN_7101b27240`, `FUN_71017d7138`, `FUN_71005eccec`,
    `FUN_7100938a40` — none overlap with the walker top 30.
  - `'SpecialWorld'` strings (3 instances) — all `(not in a function)`
    addresses; likely lookup tables.
  - `'Castle'` (10 xrefs) → many; broad term, low-value alone.
- Multi-string detection: **0 functions** touched ≥2 distinct
  seed-like terms via the script's matcher (the matcher counted
  distinct text contents, missing the `Lock`/`Unlock` adjacency in
  `FUN_7100884040` because they're different strings even though
  they're 5 bytes apart).  Manual cross-check above caught it.

#### Phase 1 — Cheat-DB anchor decompile

| Cheat anchor | Containing fn | Patches | Upstream call chain | Reads seed state? |
|---|---|---|---|---|
| NSO `+0x48A528` (Fast-Travel) | [pending — manual decompile] | | | |
| NSO `+0x5D9F58` (Fast-Travel) | [pending — manual decompile] | | | |
| NSO `+0x935E10` (Fast-Travel) | [pending — manual decompile] | | | |
| NSO `+0x48A818` (Top-of-Flag) | [pending — manual decompile] | | | |

Phase 1 was not run in the 2026-05-26 script-only session — these
require interactive Ghidra decompile work.  Next session should
populate this table by decompiling the four anchors and tracing 2-3
levels upstream.

#### Phase 2 — `walk_reader_compare_sites.py`

Script run summary:
- **5 readers scanned**: `FUN_710012ae94` (66 xrefs), `FUN_71003838ac`
  (38), `FUN_7100124134` (42), `FUN_7100472be4` (41), `FUN_71003d3fb0`
  (10).  Note xref counts came back higher in this run than the
  CLAUDE.md baseline — Ghidra picked up more after symbol import.
- **11 candidates with score=80** (strict gate-shape: imm-cmp matching
  a seed threshold + intervening `ldr` + nearby conditional branch).

**Score=80 candidate ranking** (highest confidence first):

| # | Function | Reader | Hash | cmp imm | Branch | Notes |
|---|---|---|---|---|---|---|
| 1 | `FUN_7100383418` | A | `0x90d4d0f2` | `#2`  | `b.ne`  | unknown hash; cmp w8,#2 d=3 |
| 2 | `FUN_710064d6c4` | A | `0xf4d9942a` | `#9`  | `b.hi`  | unknown hash; **#9 = W2 threshold** |
| 3 | `FUN_7101b5bcd0` | A | `0x2544a518` | `#2`  | `b.hi`  | unknown hash; multiple cmps |
| 4 | `FUN_710066e548` | B | **?**       | `#12` | `b.eq`  | hash unreconstructable; **#12 = Petal Isles threshold** |
| 5 | `FUN_7101c7d06c` | B | `0x1faf41e5` | `#2`  | `b.lt`  | unknown hash; gate-shape branch |
| 6 | `FUN_7100689880` | B | `0xe237fbc6` | `#5`  | `b.hi`  | unknown hash; **#5 = Petal Isles threshold** |
| 7 | `FUN_7101babf2c` | C | `0x65634476` | `#3`  | `b.ne`  | unknown hash; **#3 = W1 threshold** |
| 8 | `FUN_71006c12a0` | C | `0x42ffdf00` | `#2`  | `b.lt`  | unknown hash; **strict gate shape (b.lt = blocked-if-less)** |
| 9 | `FUN_7101c2155c` | A | `0xf4ee6827` | `#2`  | `b.cc`  | **known: flower_coin (PURPLE_COINS)** — not seeds, ruled out as gate input |
| 10 | `FUN_7101a5d9a0` | sub-bool | `0xed817774` | `#2` | `b.eq`  | **known: PlayReport builder** (`touch_goal_top_result`) — ruled out (telemetry) |
| 11 | `FUN_7101c6347c` | sub-bool | `0x89f1cc52` | `#6`  | `b.lt`  | **known: INTRO_CUTSCENE_COMPLETED** — bool reader; suspicious cmp #6 (Petal Isles?). Investigate. |

**Top 5 candidates for manual decompile** (highest priority — rank by
"unknown hash + gate-shape branch + threshold in regions corpus"):

1. **`FUN_71006c12a0`** (Reader C, hash `0x42ffdf00`, cmp `#2`, `b.lt`) —
   strictest gate shape.  Decompile to check: does it return bool?
   What are its callers?  Is its prologue trampoline-safe?
2. **`FUN_7100689880`** (Reader B, hash `0xe237fbc6`, cmp `#5`, `b.hi`) —
   `#5` is Petal Isles threshold; b.hi inverts to "if >5 take this
   path", which is the gate's "allow" branch.
3. **`FUN_710066e548`** (Reader B, hash unknown, cmp `#12`, `b.eq`) —
   `#12` matches Petal Isles.  Hash reconstruction failed; manually
   inspect `+0x710066e5d0..0x710066e5e8` to recover the immediate.
4. **`FUN_710064d6c4`** (Reader A, hash `0xf4d9942a`, cmp `#9`, `b.hi`) —
   `#9` is W2 threshold; b.hi is "allow" branch shape.
5. **`FUN_7101babf2c`** (Reader C, hash `0x65634476`, cmp `#3`, `b.ne`) —
   `#3` is W1 threshold; b.ne suggests equality test (less gate-like
   than b.lt/b.hi but still worth checking).

**The pattern that would be conclusive**: if any ONE of these
functions consistently reads the SAME hash with multiple threshold
values matching a single AP bucket — e.g., a function that reads
`0x42ffdf00` and compares to `{3, 10, 14}` (the W1 bucket) — that
hash is the per-world subtotal key for that bucket.  The script's
"buckets" summary line for each reader shows the seed-threshold
count per reader:

| Reader | seed-threshold hits | broad hits | weak | zero |
|---|---|---|---|---|
| A `FUN_710012ae94` | **11** | 27 | 8 | 39 |
| sub-bool `FUN_71003838ac` | 5 | 21 | 8 | 9 |
| B `FUN_7100124134` | 3 | 25 | 20 | 7 |
| C `FUN_7100472be4` | 3 | 7 | 2 | 44 |
| sibling `FUN_71003d3fb0` | 0 | 1 | 5 | 4 |

Container-A is the dominant reader for gate-shape compares (11 hits),
consistent with the M3.3 hypothesis that per-world subtotals live in
container A.

#### Phase 3 — `playreport_field_backtrace_seed.py`

Phase 1 backtrace results:

| PlayReport field | Load idiom | Note |
|---|---|---|
| `total_get_finish_seed_count` | `mov w2, w20` (no memory load) | Dead end — value pre-computed in `w20` upstream of the `Add` call; no `[base, #off]` pattern to scan |
| `wonder_seed` | `[x19, #0x1c]` | Very small offset; likely false positive (64 hits in Phase 2, all "GATE-CANDIDATE" but distributed) |
| `world_mother_seed` | `[x19, #0x47c]` AND `[x19, #0x190]` | The `+0x47c` pattern has only 17 total matches (15 non-PR) — **most focused**.  This is the palace-WIN bool slot. |
| `world_wonder_flower` | `[x19, #0x410]` | 64 hits; broad |

**Focused result (`[x19, #0x47c]`)**: 15 non-PR consumers and 2 PR-chain
consumers (`FUN_7101a5d9a0`, `FUN_7101a5ea50` — both already known
telemetry).  The 15 non-PR consumers include:

- **`FUN_71005572c0`** — reads `[x19, #0x47c]` twice (at `+0x10dc` and
  `+0x1f98`).  Worth a decompile: does it return a bool based on this
  field?  Pure-reader access at two sites in one function suggests
  a "look up the palace-WIN flag" helper.
- `FUN_710071a4a8` — writes `[x19, #0x47c]` three times (likely the
  setter; ruled out as a gate-check candidate).
- `FUN_71005e3274`, `FUN_710069a014`, `FUN_7101d151a8`,
  `FUN_7101d951b0`, `FUN_710152b3e8`, `FUN_7101aac760` — single
  read/write each; smaller scope, lower priority.

**Overlap with Phase 2 (functions in BOTH walker top 30 AND non-PR
load list)**: ZERO direct overlap.  This means the gate-check
function (whichever score=80 candidate it is) does NOT load
`[x19, #0x47c]` directly — it goes through a hash-keyed reader instead.
Consistent with container-A being the access path.

### Primary candidate

```
Verdict: MULTIPLE CANDIDATES — manual decompile required to converge
Primary candidate (highest priority): FUN_71006c12a0 @ NSO +0x6c12a0
Confidence anchors so far: [x] cmp+corpus  [ ] string  [ ] cheat  [ ] Nerve/event  [ ] PR-overlap
```

The 2026-05-26 script-only run produced 11 strict-gate-shape
candidates but no single function meets the ≥2 anchor bar yet.
Section-4 test #1 (cmp+corpus) is satisfied by every score=80 row;
none of those rows also has string adjacency (test #2), cheat
adjacency (test #3 — Phase 1 not yet run), Nerve caller (test #4 —
direct callers not yet enumerated), or non-PR overlap (test #5 —
zero overlap observed).  **The next session must do interactive
Ghidra work** to:

1. Decompile the top 5 candidates and confirm one returns a bool.
2. Walk the callers of each.
3. Decompile the four cheat anchors (Phase 1).
4. Inspect `FUN_7100884040` (the `Lock`/`Unlock` string holder) for
   inline gate logic.

Until at least 2 anchors are met for one specific function, the
implementation session MUST NOT install a return-value-overriding
hook.  An observability-only trampoline (smoke test 1) is permitted
on the top candidate as long as it doesn't modify return values.

#### The candidate function — `FUN_71006c12a0` (PRELIMINARY, needs decompile)

- NSO offset: `+0x6c12a0`
- Signature (inferred): unknown until decompile.  Hits Reader C
  (`FUN_7100472be4`) at `+0x6c12f0` with hash `0x42ffdf00` and arg
  layout consistent with `(gmd, &out, hash)` per Reader C's
  speculated signature.
- Decompile body: **NOT YET CAPTURED** — manual step required next
  session.
- What it reads: container-C (or sibling) field via hash `0x42ffdf00`
  (currently unknown semantic).
- How it decides: `cmp w8, #0x2` at `+0x6c1308` distance 6 from the
  reader call, followed by `b.lt 0x71006c1348` — **strict
  blocked-if-less gate shape**.
- Inputs: TBD.  If the function takes a "which world" enum, that's
  the per-world dispatch mechanism.
- Outputs / side effects: TBD; expect bool return.

**Other live candidates ranked by priority** (decompile in this order):

1. `FUN_71006c12a0` — strictest gate shape (b.lt)
2. `FUN_7100689880` — Petal Isles threshold #5 (b.hi)
3. `FUN_710066e548` — Petal Isles threshold #12 + missing hash
4. `FUN_710064d6c4` — W2 threshold #9 (b.hi)
5. `FUN_7101babf2c` — W1 threshold #3 (b.ne)
6. `FUN_7100884040` — Lock/Unlock string adjacency; not in walker top
   30, may inline gate logic
7. `FUN_71005572c0` — reads `[x19, #0x47c]` (world_mother_seed) twice;
   non-PR consumer of the most focused load pattern

#### Caller analysis

- Direct callers of top candidates: **NOT YET ENUMERATED**.  Next
  session: for each top-5 function, list `getReferencesTo(entry)` and
  classify each caller (regular fn / Nerve vtable region / data-table
  reference).
- Nerves among callers? Unknown.  World-map travel Nerve at
  `vt_off=0x33fd738` (CLAUDE.md M1) is the highest-priority caller to
  check for.
- Cheat-DB anchor adjacency: pending Phase 1.
- String adjacency: ZERO direct overlap between top-30 walker hits
  and the 23 functions surfaced by the string sweep.  Notable
  unexplained: `FUN_7100884040` carries `Lock`+`Unlock` strings but is
  not in the walker top 30.

#### Per-world semantics — CRITICAL for implementation

- Does the function read **one total** vs **per-world dispatch**?
  **NOT YET DETERMINED.**  The hypothesis is per-world dispatch
  (matching the AP world's 8-bucket structure), but the data so far
  shows each top candidate using a SINGLE hash at a SINGLE call site.
  Two interpretations remain on the table:
  - (a) **One function per bucket**: 8 separate gate-check functions,
    each hashed to one bucket.  Decompiling the top candidates and
    finding 8 distinct functions with hashes in the same
    {container A, container C} family would confirm this.
  - (b) **One generic function called 8 times**: one helper, called
    with a per-bucket (hash, threshold) tuple.  Decompiling would
    show the function takes the hash as a parameter rather than
    embedding it.
- Dispatch mechanism: TBD after decompile.
- For each of the 8 buckets, the hash / container / threshold:

  | Bucket | Hash | Container | Threshold immediates seen | Walker candidate |
  |---|---|---|---|---|
  | W1 | TBD | TBD | 3, 10, 14 | `FUN_7101babf2c` (cmp #3) |
  | W2 | TBD | TBD | 4, 9, 14 | `FUN_710064d6c4` (cmp #9) |
  | W3 | TBD | TBD | 4, 10 | none observed |
  | W4 | TBD | TBD | 15 | none observed |
  | W5 | TBD | TBD | 6, 11 | `FUN_7101c4dc90` (cmp #10 — could be W3 or W1) |
  | W6 | TBD | TBD | 15, 25 | `FUN_7100383418` (`cmp x11, #0x19`=25 in 2nd hit) |
  | Petal Isles | TBD | TBD | 2, 5, 8, 10, 12, 15 | `FUN_7100689880` (#5), `FUN_710066e548` (#12), `FUN_7101a59d24` (#8) |
  | Special World | TBD | TBD | 6, 16 | `FUN_7101c6347c` (#6 — INTRO bool, ruled out), `FUN_71004853e4` (cmp #8 close to W5/PI) |

  Manual decompile required to assign hashes confidently.

#### Prologue safety check (MANDATORY before any hook)

**NOT YET CAPTURED.**  Next session must paste the first 5 insns of
each viable candidate.  Paste into this block:

```
FUN_71006c12a0:
  +0x6c12a0:  <insn 1>
  +0x6c12a4:  <insn 2>
  +0x6c12a8:  <insn 3>
  +0x6c12ac:  <insn 4>
  +0x6c12b0:  <insn 5>
```

- Trampoline-safe? [ ] yes / [ ] no — reason:
- Alternative hook target if unsafe:

#### Falsified alternatives

| Candidate fn | Reason ruled out |
|---|---|
| `FUN_7101a5d9a0` | Known PlayReport builder (telemetry, not gate). Score=80 hit on `0xed817774` is the `touch_goal_top_result` value being attached to the report. |
| `FUN_7101c2155c` | Reads `flower_coin` hash (purple coins). AP world does not gate on coins; threshold #2 cmp is a "do you have 2 coins to spend in shop?" check, not a seed gate. |
| `FUN_7101c6347c` | Reads `INTRO_CUTSCENE_COMPLETED` bool. cmp #6 against a bool value is a state-machine compare, not a seed gate. |
| `FUN_7100383418` (rank #1) | cmp #2 b.ne is an LSB-test idiom `(local_5c \| 2) != 2` (tests bit 0). Function reads `WorldMap_OpenGate_Failure` event-name strings — post-event observer, not gate. |
| `FUN_7101b5bcd0` (rank #3) | State-machine dispatch on enum value. cmp #2 b.hi at +0x7101b5bd0c rejects values > 2; function dispatches on {0, 1, 2}. Hash `0x2544a518` is a 3-state enum, not seed count. |
| `FUN_71004853e4` (rank #18) | cmp #8 is `if (*param_3 != 8) iVar6 = *param_3; else iVar6 = 2;` — saturate-to-2 enum normalization. Hash `0xdcfeed18` used as course-index lookup. |
| `FUN_7101a59d24` (rank #14) | cmp #8 is `if (iStack_24 < 9) iStack_24++` — saturate-counter-at-9 increment. Hash `0x47b4307b` is a monotonic interaction count. |

### 🎯 BREAKTHROUGH (2026-05-26 iteration 3) — `FUN_7100935ce0` is the per-world dispatch target

After running [dump_gate_candidates_v2.py](../scripts/ghidra/dump_gate_candidates_v2.py)
and reading the full output ([dump_v2_output.txt](../scripts/ghidra/dump_v2_output.txt)),
the picture changes substantially.

**Most score=80 walker candidates are false positives.**  Decompile review
ruled out (in addition to the 4 ruled out in iteration 2): `FUN_710064d6c4`
(saturate-to-9 UI cap), `FUN_7100689880` (mode-dispatch switch),
`FUN_71006c12a0` (setup/teardown helper, discards reader result),
`FUN_7101babf2c` (record-copy helper; cmp was `param_2[5]==0x1f`, not #3),
`FUN_710066e548` (enum value `0xc` stored at struct offset, not threshold),
`FUN_710074e6cc`, `FUN_71005f4550`, `FUN_71005c7fcc` (all UI updaters).

Cause: the walker's "cmp + threshold-corpus immediate + ldr + branch"
pattern is too broad.  Small ints {2, 3, 5, 9, 10} appear in pervasive
non-gate idioms (vector size checks, enum dispatch, saturation caps,
LSB tests).  Tightening the scoring criteria would help in a future
iteration but the breakthrough below makes it unnecessary.

**The world-map aggregators are NOT gates either.**  `FUN_710048907c`
and `FUN_7100487b0c` (the two cheat-anchor common callers) iterate
courses and BUILD progress records (with `Lock`/`Unlock`/Top-of-Flag
bits) into lists at `param_1 + 0x650`.  They're downstream of the
gate, used for rendering the world-map UI.

**The breakthrough**: the 8 orphan-code call sites at
`0x7100480ff8..0x710048104c` form a hardcoded per-world dispatch:

```asm
mov w0, #0x1; bl 0x7100935ce0; tbnz w0,#0,0x71004816c8  ; world 1
mov w0, #0x3; bl 0x7100935ce0; tbnz w0,#0,0x71004816dc  ; world 3
mov w0, #0x4; bl 0x7100935ce0; tbnz w0,#0,0x71004816f0  ; world 4
mov w0, #0x5; bl 0x7100935ce0; tbnz w0,#0,0x7100481704  ; world 5
mov w0, #0x6; bl 0x7100935ce0; tbnz w0,#0,0x7100481718  ; world 6
mov w0, #0x7; bl 0x7100935ce0; tbnz w0,#0,0x710048172c  ; world 7 (PI?)
mov w0, #0x2; bl 0x7100935ce0; tbnz w0,#0,0x7100481740  ; world 2
mov w0, #0x9; bl 0x7100935ce0; tbnz w0,#0,0x7100481754  ; world 9 (Special?)
```

8 hardcoded world IDs `{1, 3, 4, 5, 6, 7, 2, 9}` — exactly matches the
8 AP buckets (W1-W6 + PI + Special) modulo the world-numbering scheme.
Each `tbnz w0,#0` branches to a per-world handler at
`0x71004816c8..0x7100481754` (20 bytes apart, 5 insns each).

**`FUN_7100935ce0` (500 bytes)**:
- Takes `param_1` (uint32_t world_id)
- Translates `param_1` → context via `FUN_71000d33e0` (`uStack_38 = param_1`)
- Reads hash at `context+0x68`
- Queries container A (byte at `gmd+0x18 + idx*0x18 + 0x16`) OR
  container C (bit 0 of `gmd+0x78 + idx*0x40 + 0x28`)
- Returns `bool` (any nonzero → true)
- **0 direct function callers; ONLY these 8 orphan sites call it.**

This is **the per-world dispatch helper we've been looking for**.

**Open questions that block hooking this**:

1. What does `FUN_7100935ce0` actually check?  Three semantic
   possibilities, distinguishable by playing the game:
   - "world N is unlocked / enterable" (the gate we want)
   - "world N's palace boss defeated"
   - "world N's Royal Seed acquired" (already AP-controlled via M3.3b)
2. What containing function holds the 8 orphan sites?  Ghidra reports
   "NOT inside any defined function" — needs manual Create Function
   at `0x7100480fd8` (or wherever the prologue starts upstream).
3. What do the 8 per-world handlers at `0x71004816c8..0x7100481754` do?
4. What calls the containing function? (top-level world-map tick? a
   transition event handler?)

**If `FUN_7100935ce0` checks world-unlock status**: hooking it is
perfect.  Trampoline at entry, read `param_1` (world ID), look up the
AP-granted seed count for that bucket, compare to the per-world
threshold, return true if AP says unlocked.  Prologue is trampoline-
safe (no PC-relative ops in first 5 insns).  0 fn callers means no
collateral damage outside the 8 dispatch sites.

**If it checks palace-defeated or Royal-Seed-acquired**: it's still
useful as a corroborator but not the gate.  We'd need to find what
calls the 8-orphan-dispatch function.

### 2026-05-26 iteration 7 — Static-analysis DEAD END; pivot to runtime

After iterations 6 and 7 the static-analysis path is exhausted.
Documented findings + the pivot recommendation:

**New confirmed facts** (useful even though they don't solve the gate
question):
- `0x60458608` is the **per-course Wonder Seed bitfield** (container-D
  shape; one bit per course).  Read via
  `FUN_7100124134(gmd, &out, 0x60458608, bit_index)`.  Written via
  `FUN_710049ea24(gmd, value, 0x60458608, bit_index)` — a 4-arg
  overload of the documented 3-arg Royal-Seed writer.  This is a
  USEFUL primitive for AP-driven seed granting at a per-course level.
- `0x580b7eb4` is an **adjacent per-course course-state** bitfield
  (semantics unconfirmed — possibly per-course "have ever played" or
  "have reached goal"; differentiated from seed-acquisition).
- `0x33bf655f` is a per-world data hash (read by `FUN_71005c1a18` with
  a per-world `lVar7` arg in `FUN_7100743d10`).
- **Internal world name strings** in `.rodata` at `0x71034d82f8..0x71034d8337`:
  "Invalid"=0, "Savanna"=W1 Pipe-Rock, DAT_71034d830a..8322=W2-W6
  (unparsed), "Nettai"=7 (= Petal Isles or W6 Deep Magma), "Castle"=
  Bowser-related (aliased to slot 2), "Himitu"=9 (= Special World).
- `FUN_7100743d10` is a per-world PlayReport-extension builder that
  reads per-world hashes for telemetry — **not a gate** but exposes
  the per-world hash inventory.
- `FUN_7101c41f20` is a per-course seed-bit **writer** triggered when
  a state field at `+0xa38 == 5` and a UI button input matches the
  second slot — looks like a "results screen apply" handler.

**The seed-count gate is NOT detectable statically.**  Specifically:
- The walker (cmp + threshold immediate + ldr + branch) produced 11
  score=80 candidates, all of which decompiled to non-gate idioms.
- All 4 cheat-DB Fast-Travel/Top-of-Flag anchors are per-course bit
  readers (container-D), not seed-count aggregators.
- The 8-world dispatch (`FUN_7100480fd8`) is a list builder that
  appends per-world IDs to a vector — not a gate.
- ZERO Nerve name-getters match ~45 gate-relevant English strings.
- ZERO popcount-shaped functions iterate over `0x60458608` bits.
- Per-world record tables `0x71029f0b34`/`0x71029f0f94`/`0x71029f13f4`
  are runtime-populated (all zeros statically); the per-world hash at
  `+0x68` of each record is unreadable without runtime observation.

**Three remaining hypotheses for how SMBW gates seed thresholds**
(none distinguishable statically):

1. **`FUN_7100935ce0` IS the gate**, reading a per-path "is unlocked"
   flag.  An unidentified SETTER runs the seed-count comparison
   elsewhere (post seed-collect Nerve) and sets the flag.
2. **The gate is flag-based via a different mechanism** entirely —
   a per-path state stored in a container we haven't identified.
3. **There is no runtime seed-count gate** — paths unlock via
   one-time progression events, and the AP world's seed-count
   requirements would map to a different in-game predicate.

**Recommendation: pivot to runtime observability.**

Cheapest experiment: a switch-mod observability trampoline on
`FUN_7100935ce0` that logs `(param_1, return_value)` for every call.
Boot Ryujinx, walk the world map, attempt entry to each gated
location.  Observations distinguish the three hypotheses:
- If the function fires on every gate-entry attempt and returns
  false → true at the exact moment seed count crosses the threshold,
  it's the gate (hypothesis 1).
- If it fires only at startup / world-map-load with stable values,
  it's a UI/static-state reader (hypothesis 2 or 3).
- If it doesn't fire at all on gate attempts, the gate is elsewhere
  (hypothesis 2 or 3).

Estimated cost: ~30-45 min to write the hook, ~15 min in-game testing.

Secondary observability: hook `FUN_7100124134` filtered on hash
`0x60458608` and log every call's (caller PC, bit_index, result).
Caller PC trace reveals what game logic queries the seed bitfield —
including any function that does its own per-bit iteration.

If both observability passes turn up nothing actionable, fall back to
Cheat Engine memory-access breakpoint on the live mirror of save
offset `0x3AF8` (per-course Wonder Phase seeds) — the breakpoint hit
PC is the gate function unambiguously.

**Status update for the implementation session**:
- DO NOT attempt to write a seed-count gate hook based on the
  current static evidence — every score=80 candidate has been
  falsified.
- DO write the observability hooks above.  They unblock everything.
- Hash `0x60458608` IS valuable separately: AP can grant per-course
  Wonder Seeds via `FUN_710049ea24(gmd, 1, 0x60458608, course_idx)`
  if the implementation session wants to expose that primitive (e.g.,
  for a future "AP-grants individual seeds" feature distinct from
  the gating problem).

### NEW direction (2026-05-26 iteration 2)

**The cheat-anchor analysis re-shapes the hunt entirely.**  All four
cheat anchors live inside functions of the same shape — `byte FUN(uint
hash, uint bit_index)` — that walks a container-D-style table in
GameDataMgr (offsets `+0x80` table-base, `+0x8c` table-size, `+0x70`
type-registry-size, `+0x78` type-registry-base) and returns the
bit at position `bit_index` from a u32[] data array.  These are the
**per-course bit readers** for container D.  The cheats force the
returned bit to 1 = "course is complete".  They are NOT gate checks
themselves — they are the LOOKUP that gates consult.

The gate check is one level up.  Two callers stand out:

- **`FUN_710048907c`** — called from 3 of the 4 cheat-anchored bit
  readers (FUN_710048a440, FUN_71005d9e70, FUN_710048a730).  This is
  the universal caller and likely the upstream "is this course / world
  / palace gated?" aggregator.  **NEXT-ITERATION HIGHEST PRIORITY.**
- **`FUN_7100487b0c`** — called from 2 of the 4.  Secondary candidate.

Even more compelling: **`FUN_7100935ce0`** (the function containing
the +0x935E10 cheat anchor) has 8 callers at orphan-code addresses
`0x7100480ff8`, `0x7100481004`, ..., `0x710048104c` — exactly 12 bytes
apart.  These are 8 consecutive `bl FUN_7100935ce0` calls inside a
larger function we haven't yet identified.  **8 calls = the 8 AP
buckets (W1, W2, W3, W4, W5, W6, PI, Special).**  This is the per-
world dispatch loop.  Finding the function that contains
`0x7100480ff8` and seeing what it does with the 8 return values is
the most direct path to the gate check.

**Hash recovery confirms register/memory passing**: the 4 failed-
reconstruction sites all pass the hash dynamically:
- `FUN_710074e6cc`: hash via `ldr w2, [x19, #0x264]`
- `FUN_71005f4550`: hash via `ldr w2, [x1, #0xa0]`
- `FUN_71005c7fcc`: hash via `mov w2, w1` (caller-passed)
- `FUN_710066e548`: hash via `mov w2, w22` (caller-passed)

These functions take the hash as a struct field or argument — exactly
the "generic helper called per-world with each bucket's hash" pattern.
`FUN_710074e6cc` and `FUN_71005f4550` are the highest-priority of
this group because they read the hash from a struct at a known offset:
the struct at `x19+0x264` (or `x1+0xa0`) is likely the per-world
descriptor table.

#### Open risks

- The 7 score=80 candidates with unknown hashes might still be NON-seed
  state (e.g., progression flags, coin counts in another currency,
  difficulty settings).  Decompile is the only way to disambiguate.
- `FUN_7100884040` carrying `Lock`/`Unlock` may turn out to be an
  audio-state helper or animation-state enum, not a gate.  Strings
  alone are not proof.
- The walker scored on "cmp + ldr + branch" — if the real gate uses
  a different idiom (e.g., switch table, computed jump, popcount of
  a bitfield), the walker would miss it.  Phase 1 (cheat DB anchors)
  is the orthogonal fallback that should catch this.
- The script's hash reconstruction failed on at least one high-priority
  candidate (`FUN_710066e548`).  Manual `+0x710066e5d0..0x710066e5e8`
  read needed.

### Implementation guidance (NOT READY — see "Next session priorities" below)

The 2026-05-26 script run met **only 1 of the 2 minimum confidence
anchors** required by the plan (cmp+corpus only; string adjacency
and cheat anchors not yet checked).  The follow-up session must
complete the Ghidra work below BEFORE installing any return-value-
overriding hook.

### Next session priorities (RE — must complete before implementation)

The remaining work is all interactive Ghidra (decompile-and-read),
not new automation.  Estimated ~3-4 hours.

**Priority A — Decompile the top 7 candidates** (≤30 min each):

For each of these, paste the decompile into a follow-up artifact
section, identify the return type, and check the function's caller
list.  Specifically answer: "Does this function return a bool, and
is it called from a world-map-transition or palace-entry path?"

1. `FUN_71006c12a0` @ NSO `+0x6c12a0` — strictest gate shape
2. `FUN_7100689880` @ NSO `+0x689880` — Petal Isles threshold #5
3. `FUN_710066e548` @ NSO `+0x66e548` — Petal Isles threshold #12,
   missing hash (manually read `+0x66e5d0..+0x66e5e8`)
4. `FUN_710064d6c4` @ NSO `+0x64d6c4` — W2 threshold #9
5. `FUN_7101babf2c` @ NSO `+0x1babf2c` — W1 threshold #3
6. `FUN_7100884040` @ NSO `+0x884040` — `Lock`/`Unlock` strings
7. `FUN_71005572c0` @ NSO `+0x5572c0` — reads `[x19, #0x47c]` twice

**Priority B — Phase 1 cheat-DB anchor decompile** (~45 min):

Decompile the four anchors at NSO `+0x48A528`, `+0x5D9F58`,
`+0x935E10`, `+0x48A818`.  Identify what the cheat patches and walk
2-3 levels upstream.  Cross-reference against the Priority A list —
if a cheat sits inside or directly calls one of the candidates,
that's an immediate anchor #3 (cheat-adjacency).

**Priority C — Hash reconstruction for unknown candidates** (~30 min):

The walker failed to reconstruct hashes at several score=80 sites:
- `FUN_710066e548` call `+0x66e5e8`
- `FUN_710074e6cc` call `+0x74e708`
- `FUN_71005c7fcc` call `+0x5c8000`
- `FUN_71005f4550` call `+0x5f45a8`

Manually inspect the 8-16 insns preceding each call to recover the
`w2` immediate.  If any reconstructs to a hash already in the
KNOWN_KEYS table or to a value that also appears at sites with
different thresholds in the same bucket — that's a per-world
subtotal hash, the most important deliverable.

**Priority D — Caller enumeration** (~30 min):

For each surviving candidate (after A+B+C), run
`getReferencesTo(entry)` in Ghidra (or write a small script).
Classify each caller:
- Direct call from another function → record the caller fn
- Address in a vtable region (`0x710334XXXX`/`0x71033fXXXX`/
  `0x71034BXXXX`) → record the vtable + slot index
- Address in `.data` table → record the table address + offset

A candidate with callers in the world-map / palace-door Nerve
vtables earns anchor #4.

**Priority E — Prologue safety capture** (~5 min per candidate):

For each surviving candidate, paste the first 5 instructions into the
artifact's "Prologue safety check" block.  Flag any PC-relative ops
(adrp / ldr-literal / b / bl).  If the prologue is unsafe, identify
a shared inner helper as the alternative hook target (M1
`FUN_7100559f7c` analog).

**Priority F — Fallback if A-E converge on no clear winner**:

Run Cheat Engine on Ryujinx with a memory-access breakpoint on the
live-state mirror of file offsets `0x3348` (GoalSeed) and `0x3AF8`
(Wonder Phase).  Trigger a gated transition.  The hit address is the
gate-check.  Time-expensive; only escalate if static work plateaus.

### Verification of this scaffold

What was delivered 2026-05-26:
- [x] Three new Ghidra scripts: [find_gate_strings.py](../scripts/ghidra/find_gate_strings.py),
      [walk_reader_compare_sites.py](../scripts/ghidra/walk_reader_compare_sites.py),
      [playreport_field_backtrace_seed.py](../scripts/ghidra/playreport_field_backtrace_seed.py)
- [x] All three scripts run cleanly on the live binary; output above
      includes one or more strong gate-shape candidates
- [x] Falsifier on the lifetime counter `0x8c20ccb7` ruled it out
- [x] 11 score=80 candidates identified, with 5 ranked for top-priority
      manual review
- [x] Known false positives (`FUN_7101a5d9a0` PlayReport,
      `FUN_7101c2155c` flower_coin, `FUN_7101c6347c` INTRO bool) marked
      in the falsified-alternatives table

What is NOT yet done (blocks implementation):
- [ ] Manual decompile of the top candidates
- [ ] Cheat-DB anchor Phase 1
- [ ] Hash reconstruction for the 4 unknown sites
- [ ] Caller enumeration / Nerve check
- [ ] Prologue safety capture
- [ ] Primary candidate named with ≥2 confidence anchors

## 2026-05-26 — Wonder Seed gate observability run

Two `HOOK_DEFINE_TRAMPOLINE` observability hooks were installed (no
state mutation, logging only) per the plan in
[wonder-seed-observability-hook-prompt.md](wonder-seed-observability-hook-prompt.md)
to disambiguate the three hypotheses about `FUN_7100935ce0`:

- **`WorldUnlockCheck`** @ NSO `+0x935ce0` — logs every call's
  `(world_id, return_value, saveLoaded)`.  Prologue verified safe via
  [scripts/dump_prologue.py](../scripts/dump_prologue.py).
- **`SeedBitfieldRead`** @ NSO `+0x124134` — logs every call where
  `hash == 0x60458608` (the per-course Wonder Seed bitfield confirmed
  in iteration 7) with `(bit_index, success, *out, saveLoaded)`.

### Test session (Ryujinx_1.3.3_2026-05-26_09-08-06.log)

3:46 of gameplay; the player loaded a save, walked the world map,
unlocked a level "with seeds" (their exact words), entered a course
and died, re-entered and cleared it, then attempted a third course
entry — at which point Ryujinx hung mid-actor-placement
(`MapObjHajimariChikaKazariJimenIwaA`, the W1 intro/cave decoration
batch was streaming).  The hang has no `User Break` / `GuestBroke`
trace in the log, the gmd.A_writer fired normally up to the final
`hash=0xa42ada00 value=1` save tick, and none of our trampolines'
callbacks ran at the time of the freeze — almost certainly a Ryujinx
level-streaming flake unrelated to the observability hooks.

### Representative log slice

```
00:00:16.544  installing WorldUnlockCheck @ 0x935ce0
00:00:16.544  installing SeedBitfieldRead @ 0x124134 (filtered on hash=0x60458608)
00:00:22.394  SaveDeserializerHook: captured save_struct=0x20d95c9470 ...
00:00:32.548  SeedBitfieldRead(hash=0x60458608, bit=0) -> success=1 value=1  saveLoaded=1
   ── no further SeedBitfieldRead or WorldUnlockCheck fires for the rest of the session ──
00:01:12.452  prepo.set_event ... event=world_activity         (player on world map)
00:01:20.599  prepo.set_event ... event=world_result           (player picks a course)
00:01:27.359  prepo.set_event ... event=course_in              (course loads)
00:03:19.984  gmd.D_writer hash=0x46721422 course=2 value=0x00000001
00:03:19.990  COURSE_CLEARED: nerve=0x20c0d992a8 (fire #1)
00:03:19.998  prepo.set_event ... event=course_result
00:03:44.565  prepo.set_event ... event=world_activity         (back on map)
00:03:45.280  prepo.set_event ... event=world_result           (next course)
00:03:46.32x  actor_placement: ... ⟂ log abruptly ends; Ryujinx hung
```

Totals across the entire session:

| Hook | Fires | Notes |
|---|---:|---|
| `WorldUnlockCheck(FUN_7100935ce0)` | **0** | Zero across boot, world map, unlock event, 2 course entries, 1 attempted entry |
| `SeedBitfieldRead(hash=0x60458608)` | **1** | Once only, at save-load (32.548 s), `bit=0 success=1 value=1` |

### Hypothesis verdict

**H3 confirmed for `FUN_7100935ce0` — it is NOT the per-world unlock
gate.**  The function (and the 8 hardcoded `bl 0x7100935ce0` orphan-
code sites at `0x7100480ff8..0x710048104c`) is never reached during
normal gameplay, including the explicit "unlock a level with seeds"
moment the player triggered.  The 8-call dispatch we identified in
[iteration 3 BREAKTHROUGH](#-breakthrough-2026-05-26-iteration-3---fun_7100935ce0-is-the-per-world-dispatch-target)
is either compile-time dead code, gated on an in-game state we did
not reach (intro cutscene? post-game?), or a debug/release toggle
that's never live in retail.  Whatever it is, **the per-world unlock
gate does not flow through it**.

**`FUN_7100124134(hash=0x60458608)` is the persistent-storage reader,
not a gate input.**  The single fire at save-load (32.548 s,
`bit=0 success=1 value=1`) is the save deserializer populating the
live state from the persisted bitfield.  After that population step,
the function is never invoked again for this hash for the entire
3 min 46 s of play — meaning the gate logic (and the world-map UI
that displays seed counts, and the per-course seed indicator on the
map) reads from a **different in-memory data structure**.  The
0x60458608 bitfield is write-back storage that the game refreshes
**from** that live cache on save, not the source of truth at decision
time.

This matches the H3 signal for that hook too: it's not on the gate
read path during gameplay.

### Concrete next session work

The next session is unblocked but pivots away from `FUN_7100935ce0`.
The right next move depends on which is cheapest to land:

**Option A (highest information yield, ~1 hour): caller-PC capture +
live-cache discovery.**  Modify `SeedBitfieldRead` to also log the
**caller's link register (LR)** on the save-load fire — the value of
LR at callback entry is the address inside the save-deserializer
function that did `bl FUN_7100124134`.  That caller IS the live-cache
populator; its decompile tells us:
1. Where it stores the bit value (= the live-cache address / struct
   offset).
2. What index/key it associates with each bit.

Once the live cache is mapped, the gate function decompiles trivially:
search Ghidra xrefs for reads of the live cache address/offset — that
short list contains the gate.

Caller-PC capture in exlaunch is one line in the callback:
```cpp
uintptr_t caller_pc;
__asm__ volatile("mov %0, x30" : "=r"(caller_pc));
```
(Note: `x30 == LR` only on entry to the callback; capture it before
any other call.)

**Option B (orthogonal, ~30 min once live cache is roughly located):
Cheat Engine pointer-scan + memory-access breakpoint** on the live
cache once Option A or static analysis localizes it.  The breakpoint
hit PC is the gate-check unambiguously.  Same fallback strategy
listed in iteration 7's recommendation; now better-targeted because
we know SeedBitfieldRead populates the cache and we can capture the
write destination there.

**Option C (cheapest but slowest, ~hours): broaden the observability
filter on `SeedBitfieldRead`.**  Drop the `hash == 0x60458608` filter
and bracket the gate event narrowly with a level-attempt fire-counter
toggled by player input.  If a different hash IS the gate input,
its reads will surface this way.  Discouraged: the 55+ unfiltered
xrefs will produce log spam that dwarfs the signal.

**Recommended: Option A.**  Smallest change, biggest unblock.  We
already have the trampoline installed; the diff is ~3 lines to log
LR.  Once that LR is logged, the static-analysis loop closes — we
read the caller, identify the live cache write, and the gate
function falls out from there.

**Do NOT remove the existing observability hooks** before Option A
lands — the next run needs SeedBitfieldRead to fire its one save-load
shot with the LR captured.  After Option A produces the live-cache
function, the hooks can be retired.

### Falsified candidates (carry forward)

| Candidate fn | Status | Falsifier |
|---|---|---|
| `FUN_7100935ce0` | ❌ ruled out | Zero fires across 3:46 of gameplay including an unlock event (2026-05-26 obs run) |
| `FUN_7100124134(hash=0x60458608)` as gate input | ❌ ruled out | Fires once at save-load only; never on gate-decision path (2026-05-26 obs run) |
| `FUN_7101a5d9a0` | ❌ ruled out | PlayReport telemetry builder |
| `FUN_7101c2155c` | ❌ ruled out | flower_coin (shop currency) |
| `FUN_7101c6347c` | ❌ ruled out | INTRO_CUTSCENE_COMPLETED bool |
| `FUN_7100383418` | ❌ ruled out | LSB-test idiom |
| `FUN_7101b5bcd0` | ❌ ruled out | State-machine dispatch |
| `FUN_71004853e4` | ❌ ruled out | Saturate-to-2 enum normalization |
| `FUN_7101a59d24` | ❌ ruled out | Saturate-counter-at-9 increment |

## 2026-05-26 — Wonder Seed gate observability run #2 (caller-PC capture)

Same `SeedBitfieldRead` hook plus a one-line addition:
`__builtin_return_address(0)` is captured BEFORE `Orig()`.  exlaunch's
`And64InlineHook` patches the original function entry with
`LDR X17 / BR X17 / <callback>` (an unconditional branch, not BLR — see
[src/lib/hook/nx64/hook_impl.cpp:584-585](../switch-mod/src/lib/hook/nx64/hook_impl.cpp)),
so LR on callback entry is the game-caller's return address.  Our C++
prologue spills LR to the stack and `__builtin_return_address(0)` reads
back from there — stable across `Orig()`.

### Result

Single line captured at boot:

```
00:00:35.092  SeedBitfieldRead(hash=0x60458608, bit=0) -> success=1 value=1
              saveLoaded=1  caller_ret=NSO+0x66e5ec  call_at=NSO+0x66e5e8
```

The `call_at=NSO+0x66e5e8` lands inside **`FUN_710066e548`** — a
function the walker had already flagged but iteration 3 falsified as a
gate (correctly: the `cmp #12` was the enum value `0xc` being assigned
to a struct field, not a threshold).  The hash the walker couldn't
statically reconstruct at that site is now confirmed: `0x60458608`.

### What FUN_710066e548 actually does — the live-cache populator

Decompile (verbatim from
[dump_v2_output.txt:1864-1936](../scripts/ghidra/dump_v2_output.txt)):

```c
void FUN_710066e548(long param_1) {
  iVar4 = FUN_71001e4ae0();                    // course count for current world
  *(int *)(param_1 + 0xa78) = iVar4;
  if (iVar4 >= 1) {
    uVar8 = 0;
    lVar6 = param_1 + 0x650;                   // ← live-cache base
    do {
      bVar3 = *(byte *)(param_1 + 0xa28);      // in-session byte mask (≤4 courses)
      ...
      *(uint *)(lVar2 + 0x10) = uVar7;         // course index
      *(bool *)(lVar2 + 0x0b) = uVar1 != 0;    // "set this session"
      if (uVar1 == 0) {
        FUN_7100124134(gmd, &local, 0x60458608, uVar8);   // ← our hooked read
        *(bool *)(lVar2 + 0x0c) = local != 0;             //   persistent bit
        if (local != 0 && *(int *)(lVar2 + 0x30) != 0xc) {
          *(int *)(lVar2 + 0x30) = 0xc;        // ← status enum = "completed"
          *(u8  *)(lVar2 + 0x34) = 1;          // ← dirty flag
        }
      } else {
        FUN_710064f4f0(0x580b7eb4, uVar8, &iStack_68);    // ← sibling state
        if (succ && *(int *)(lVar2 + 0x30) != iStack_68) {
          *(int *)(lVar2 + 0x30) = iStack_68;
          *(u8 *)(lVar2 + 0x34) = 1;
        }
      }
      ...
      uVar8 += 1;
      lVar6 += 0xd0;                           // 208-byte stride per record
    } while (uVar8 < count);
  }
}
```

**Inferred live-cache record layout** (208-byte stride at
`param_1 + 0x650 + i*0xd0`):

| Offset | Type | Field |
|---|---|---|
| `+0x08` | u8 | valid (set to 1) |
| `+0x0b` | u8 | set-this-session (mask bit at `param_1+0xa28`) |
| `+0x0c` | u8 | set-in-persistent (from `0x60458608`) |
| `+0x10` | u32 | course index |
| `+0x20` | u16 | UI status (0x100 if active) |
| **`+0x30`** | **i32** | **status enum** (`0xc` = "completed"; other values populated from `0x580b7eb4`) |
| `+0x34` | u8 | dirty flag |
| `+0x38` | u64 | back-pointer to `param_1` |
| `+0x50` | u32 | course index (mirror) |

### This is the SAME structure iteration 3 identified

[Iteration 3](#-breakthrough-2026-05-26-iteration-3---fun_7100935ce0-is-the-per-world-dispatch-target)
documented:

> `FUN_710048907c` and `FUN_7100487b0c` (the two cheat-anchor common
> callers) iterate courses and BUILD progress records (with `Lock`/
> `Unlock`/Top-of-Flag bits) into lists at `param_1 + 0x650`.  They're
> downstream of the gate, used for rendering the world-map UI.

We now have **three writers** to `param_1 + 0x650` (the world-map state
cache):

- `FUN_710066e548` — observed at save-load, reads `0x60458608` + `0x580b7eb4`
- `FUN_710048907c` — cheat-anchor convergence (3 of 4)
- `FUN_7100487b0c` — cheat-anchor convergence (2 of 4)

Iteration 3 was right that these populate, not gate.  But it didn't go
the next step: **the gate function is a READER of this cache**,
specifically of the status enum at offset `+0x30` (e.g.,
`ldr w*, [x*, #0x680]` for course 0, `+0xa48` for course 6, etc., for a
direct base pointer — or `ldr w*, [x*+0x30]` once iterated to the
per-course record).

### Second per-course state confirmed: `0x580b7eb4` is an int (not a bool)

The sibling hash `0x580b7eb4` (iteration 7's "adjacent per-course
course-state" of unknown semantics) is queried via
`FUN_710064f4f0(0x580b7eb4, course_idx, &int_out)` and the result is
stored directly into the cache's status enum.  Two implications:

1. `0x580b7eb4` is an INT-per-course, not a bitfield.  Likely the
   `goal_id` / exit-type / completion-state enum.
2. `FUN_710064f4f0` @ NSO `+0x64f4f0` is a NEW gmd reader (prologue
   has `adrp x8, #0x363f000` — the page containing
   `gmd::sInstance` at `+0x363F0F0`).  Probably container-A counter
   reader with the same signature shape as `FUN_710012ae94` but
   different bucket location.

### Three new prologue-verified hook candidates (all trampoline-safe)

| NSO offset | Function | Prologue first insn | Safety |
|---|---|---|---|
| `+0x66deec` | `FUN_710066deec` — sole caller of cache populator | `stp x29, x30, [sp, #-0x20]!` | ✅ safe |
| `+0x64f4f0` | `FUN_710064f4f0` — `0x580b7eb4` reader | `sub sp, sp, #0x50` | ✅ safe |
| `+0x66e548` | `FUN_710066e548` — cache populator (already known) | `sub sp, sp, #0x70` | ✅ safe |

### Concrete next session work

**Priority A — Decompile `FUN_710066deec` @ NSO `+0x66deec` (~20 min).**
This is the ONE caller of the cache populator (per dump_v2_output
"Direct callers: Total refs: 1").  Its prologue at +0x66defc loads
`x0 = [x0, #0x30]` — so it's reading a substructure at offset 0x30
from its own param_1, then `bl FUN_7100???dfb0` (one of its callees,
likely a child accessor).  Decompile reveals:
- What `param_1` represents (a world manager? a session struct?)
- What calls `FUN_710066deec` — the upstream gate-control entry point
- Whether the populator runs every frame, only at world-map-load, or
  only on certain events

**Priority B — Decompile `FUN_710064f4f0` @ NSO `+0x64f4f0` (~15 min).**
Recover its container source.  If it's container-A (`gmd+0xe0..0xf8`),
then `0x580b7eb4` is a counter-per-course and the M3.3
`grantContainerACounter` primitive can already grant it.  This would
let AP grant per-course completion state directly.

**Priority C — Cache-read xref walk (~30 min).**  In Ghidra, find all
readers of the cache record's status field (offset `+0x30` of any
0xd0-byte record at `param_1 + 0x650`).  Concrete searches:

- `ldr w*, [x*, #0x680]` — status field of course 0 record (`+0x650 + 0x30`)
- `ldr w*, [x*, #0x750]` — status field of course 1 record (`+0x650 + 0xd0 + 0x30`)
- Generic: `ldr w*, [x*, #0x30]` within functions that also touch
  `param_1 + 0xa78` (the count field) or that iterate by `+0xd0`

The smaller list those produce is the candidate gate-reader set.  Cross-
reference against world-map travel Nerves (vtable `0x33fd738`) for
caller context.  Expected: one function does `cmp w8, #0xc; b.eq <allow>`
or similar — that's the gate.

**Priority D — Optional confirmation hook**: install a trampoline on
`FUN_710066deec`.  It should fire whenever the world-map UI refreshes
the live cache.  Frequency tells us whether the cache is per-frame
(unlikely; would have shown more SeedBitfieldRead fires) or
event-driven.  Low priority — Priority A's decompile likely answers
this directly.

### Implementation note for the next implementation session (once gate is found)

The eventual gate-override hook will read the world ID + course index
from the cache record's struct context, look up the AP-granted Wonder
Seed count for that world bucket, compare against the per-world
threshold table (from [regions.json](../apworld/smbw_archipelago/data/regions.json)),
and either:

- (A) Return the override result from the gate function directly (if
  the gate is a single bool reader of the status enum), OR
- (B) Modify the cache record's status enum field before the gate
  reads it (if the gate runs against the cache and the cache is
  refreshed independently of AP) — note this approach competes with
  the legitimate writers (`FUN_710066e548`, `FUN_710048907c`,
  `FUN_7100487b0c`) and needs the same "absolute-overwrite + periodic
  tick" pattern as the badge sync.

Option A is preferred if structurally feasible.

### Remove the observability hooks once Priority A-C land

`WorldUnlockCheck` should be removed (confirmed irrelevant).
`SeedBitfieldRead` can stay during gate-hook development as a sanity
signal but should be retired before shipping.

### 2026-05-26 — REVISION after decompiling FUN_710066deec + FUN_710064f4f0

**Reversal of the previous section's interpretation.**  User pasted
decompiles of the two follow-up targets.  Both functions decompile
cleanly — but their semantics overturn the "gate is a reader of
`+0x30`" hypothesis.

#### `FUN_710064f4f0` — per-course PLAYER CHARACTER lookup, not status

The function reads an int from a typed-virtual container at
`gmd+0x2b0..0x2cc` keyed by `(hash, course_idx)`, then resolves that
int through a 12-way string-match against character names: `Mario`,
`Luigi`, `Peach`, `Daisy`, `KinopioYellow`, `KinopioBlue`, `Kinopico`,
`Totten`, `YoshiGreen`, `YoshiRed`, `YoshiYellow`, `YoshiBlue` (indices
0..11).  Returns 1 + writes the character index to `*param_3`.

So **`0x580b7eb4` is per-course "which character last played this
course"** — for displaying the character portrait icon next to the
course on the world map.  Not a state enum.

This also surfaces a new container we'll call **Container E** at
`gmd+0x2b0..0x2cc`:

| Offset | Field | Role |
|---|---|---|
| `+0x2b0` | u32 | container-E limit |
| `+0x2b8` | ptr | container-E typed-sub-obj array (stride 0x50) |
| `+0x2c0` | ptr | container-E bucket array (8-byte entries: key + idx) |
| `+0x2cc` | u32 | container-E bucket count |

Sub-obj layout (different from container C): vtable at `+0`, size at
`+0x20`, **data pointer at `+0x28` pointing to a `uint32_t[]` array**
indexed by course (not bit-indexed).  So container E holds u32-per-key
arrays.

#### `FUN_710066deec` — world-map UI cache REBUILD orchestrator

```c
void FUN_710066deec(long param_1) {
  FUN_710066dfb0(*(undefined8 *)(param_1 + 0x30));     // sub-thing setup

  // 6 sequential cache populators — each fills different fields of the
  // same `param_1 + 0x650 + i*0xd0` record array:
  FUN_710066e548(param_1);    // ← our SeedBitfieldRead's caller
  FUN_710066e964(param_1);
  FUN_710066e36c(param_1);
  FUN_710066ed90(param_1);
  FUN_710066e134(param_1);
  FUN_710066e6c0(param_1);

  // Per-record sentinel resets at +0x9:
  *(undefined *)(param_1 + 0x659) = 0;     // record 0 (+0x650 + 0x9)
  *(undefined *)(param_1 + 0x729) = 0;     // record 1 (+0x650 + 0xd0 + 0x9)
  *(undefined *)(param_1 + 0x7f9) = 0;     // record 2
  *(undefined *)(param_1 + 0x8c9) = 0;     // record 3

  // Input-mapping switch: Key A or Key B set
  if (*(char *)(param_1 + 0xa7e) == '\0') { ... Key_A ... }
  else                                     { ... Key_B + Key_A ... }
}
```

This is a **whole-cache rebuild**, not a single-field update.  Run on
world-map re-entry, save load, and any cache-invalidation event.  The
`+0x30` "status enum" we tentatively called the gate input is actually
**the world-map icon enum** — values 0..11 are character portraits
(see Container E above), value `0xc` is the "completed" stamp.

**The cache is rendering, not gating.**  The corrected mental model:

```
┌────────────────────────────────────────────────────────────────────┐
│ World-map UI render path (NOT the gate):                            │
│                                                                     │
│   persistent storage (gmd container D + container E)                │
│         │                                                           │
│         ▼                                                           │
│   FUN_710066deec [cache rebuild orchestrator]                       │
│         │  calls 6 populators including                             │
│         │  FUN_710066e548 → reads 0x60458608 (cleared) + 0x580b7eb4 │
│         ▼  (last-char) into +0x30                                   │
│   cache at param_1 + 0x650 (208 B/record per course)                │
│         │                                                           │
│         ▼                                                           │
│   world-map renderer (draws character icon or "cleared" stamp)      │
└────────────────────────────────────────────────────────────────────┘
```

The Wonder Seed gate is **a separate code path** that we haven't yet
observed.  Iteration 7's static analysis already implied this:

> ZERO popcount-shaped functions iterate over `0x60458608` bits.

If the gate doesn't popcount the per-course bitfield, it must consult
an already-aggregated **per-world Wonder Seed COUNT** stored under a
hash we haven't identified.

#### Where the gate input lives — narrowed hypothesis

Iteration 7's third critical finding:

> Per-world record tables `0x71029f0b34`/`0x71029f0f94`/`0x71029f13f4`
> are runtime-populated (all zeros statically); the per-world hash at
> `+0x68` of each record is unreadable without runtime observation.

These per-world record tables hold a **per-world hash at `+0x68`**.
Those are almost certainly the per-world subtotal hashes — one hash
per AP bucket (W1, W2, W3, W4, W5, W6, Petal Isles, Special), each
keying a container-A counter that the game increments at seed
acquisition and decrements / checks at gate-decision time.

### Next observability move — choose ONE

**Option α (precise, low-spam): Dump per-world record tables at
runtime.**  Add a one-shot dumper called from
`NerveActivateOnce::Callback` after `markSaveLoaded` (or from the next
fire of `SeedBitfieldRead`).  Walk each of the 3 base tables and dump
`record[+0x68]` for the first ~16 entries.  Returns the per-world
hash list directly.  Once we have those hashes, hook
`FUN_710012ae94` (container-A reader) filtered on hash IN that list +
log `(caller_pc, hash, value)`.  Caller PC during a gate attempt
identifies the gate function unambiguously.

Estimated cost: ~30 min to write the dumper + filter; ~5 min in-game
to capture.

**Option β (broad, more spam, no static work): Container-A reader
sampling.**  Hook `FUN_710012ae94` (NSO `+0x12ae94`, container-A
counter reader) with the same caller-PC capture pattern as
`SeedBitfieldRead`.  No hash filter; capture a budget of ~200 calls
across boot + 2 minutes of gameplay including a gate attempt.  The
caller PCs that fire ONLY around gate attempts are the gate-readers;
the static hash they query is the per-world subtotal hash.

Estimated cost: ~15 min to write; needs careful in-game timing to
bracket the gate attempt; risk of log flood.

**Option γ (orthogonal): Hook `FUN_7100884040` (Lock/Unlock string
carrier).**  Iteration 0b identified this as the only function in the
binary that touches BOTH `'Lock'` and `'Unlock'` strings (`+0x4c` and
`+0x74` from entry).  It's not in the cmp-shape walker's top 30, so
it never decompiles as a gate via the static-pattern heuristic — but
if it's the gate's UI-result display function, hooking it tells us
WHEN gate decisions happen.  Add `caller_pc` capture too.  Two
possibilities:
- Hook fires → its caller is the gate function (1-hop trace).
- Hook never fires → not the gate result display; rule out.

Estimated cost: ~10 min to write + deploy.  Smallest blast radius if
it doesn't work.

**Recommended: Option α.**  The per-world record tables are an
already-known data structure with a known address; the dump is
unambiguous and the resulting per-world hash list collapses the rest
of the problem.  Option β is the fallback if α's hashes don't appear
on container-A reads near gate attempts.  Option γ is a cheap
parallel experiment but probably less informative.

### Updated falsified candidates

Add to the table:

| Candidate fn | Status | Falsifier |
|---|---|---|
| `FUN_710066e548` as gate-reader | ❌ ruled out (now confirmed) | World-map UI cache populator: writes `+0x30` icon enum (character index or `0xc` "cleared stamp"), not a gate input |
| `FUN_710066deec` as gate | ❌ ruled out | Cache-rebuild orchestrator; calls 6 populators + clears sentinels |
| `+0x30` of `param_1 + 0x650 + i*0xd0` records as gate input | ❌ ruled out | UI icon enum, not status (0..11 = character portrait, 0xc = cleared stamp) |
| `param_1 + 0x650` cache | ⚠️ render-state, NOT gate-state | Multiple writers populate it for world-map rendering |

## 2026-05-26 — observability run #3 (per-world table dump + container-A reader)

Two additions landed:

1. **Per-world record table dumper** (`probe::dumpPerWorldTables()`) —
   reads 256 bytes from each of the 3 base addresses iteration 7
   flagged: NSO `+0x29f0b34` / `+0x29f0f94` / `+0x29f13f4`.  One-shot,
   triggered from the first `SeedBitfieldRead` fire at save-load.
2. **`ContainerAReader` hook** on `FUN_710012ae94` @ NSO `+0x12ae94`
   (container-A counter reader, ~66 xrefs).  Per-hash budget = 5;
   caller-PC capture via `__builtin_return_address(0)`.  **Hook
   installed without crash** — the `cbz w2` in the prologue WAS
   relocator-handled correctly by exlaunch (contra the
   PlayerHpStructLatch inline-hook precedent at `+0x2743BC`, which
   was a different code path).

### Test session (Ryujinx_1.3.3_2026-05-26_09-34-58.log)

5:50 of gameplay; the player loaded a save (W2 with 4 Wonder Seeds),
played + died + cleared a W2 course (+1 Wonder Seed → 5 W2 seeds),
opened a W2 gate requiring 5 seeds, transitioned to W3 (Fluffy-Fluff
Peaks, 0 seeds), bought 1 Wonder Seed from a Poplin house, and
**twice tried to enter a level needing more than 1 seed — was denied
in the UI**.

### Reversal #1: iteration 7's "per-world record tables" interpretation is wrong

The 3 addresses are NOT per-world record tables.  Runtime dump shows:

**Table 0** @ NSO `+0x29f0b34`: first 0x70 bytes zero, then a dense
array of u32 hashes starting at `+0x70`:
```
+0x070: 0235d948 6303c6d1 d8163612 eb41ddd5
+0x080: a6140d7c 6f9dfc59 e7773731 406f7425
+0x090: f3d3849c a4dbcac9 b465ad47 6d1b5c25   ← known: badge UI bitmap
+0x0a0: cea26ca1 48e0dec7 72128f1d f1c0ef95
+0x0b0: 4f262e62 6d2e5a9f 7ae70f64 43318285
+0x0c0: 55815859 64fe8cc4 fc951a99 6af4303e   ← known: W1 Royal Seed @ +0xc0
+0x0d0: 05015b41 df9a528a 5db62877 2c51d948
+0x0e0: 03722209 e1f4956f 48c27a6f 2309a645
+0x0f0: 46721422 b161b8ab 57af904b 7b90402e   ← known: 0x46721422 = D-writer
```

The mix of known container-C bitfield keys (`0x6d1b5c25`), known
container-B bool keys (`0x55815859`), and known container-D
per-course bitfield keys (`0x46721422`, `0xb161b8ab`) means this is
**not a per-world index**.  It's a **flat schema/registry table** of
all hash-keyed save-data fields in some indexing order.

**Table 1** @ NSO `+0x29f0f94`: all zeros (256 bytes scanned).

**Table 2** @ NSO `+0x29f13f4`: zeros for first `+0x70`, then a header
`00000000 ffffffff 00000002 00000000` followed by small ints (0..8):
```
+0x080: 00000001 00000004 00000002 00000003
+0x090: 00000002 00000002 00000008 00000008
+0x0a0: 00000008 00000000 00000001 00000001
+0x0b0: 00000002 00000001 00000001 00000001
+0x0c0..0x0f0: many 00000001s
```

The 0..8 values are likely **type/size discriminators** parallel to
table 0's hash array (entry N's type at table-2 +0x80 + N*4).  So
tables 0 and 2 together form a **field schema**: hash → type.
Iteration 7's "hash at +0x68 of each record" claim was a
misreading — the +0x68 figure must have come from a different code
path that loads ONE entry into a context struct.

**Implication**: there's no per-world subtotal hash list to extract
from these tables.  The persistent per-world seed count must be
stored elsewhere — either computed on-the-fly from the per-course
bitfield `0x60458608`, or kept in a different container we haven't
mapped yet.

### Discovery #1: the 5-hash "current world" mirror

By cross-referencing `gmd.A_writer` fires against the gameplay
timeline (course-clears, world transitions, Poplin purchases), we
identified **a 5-hash group that ALL update synchronously** with the
current world's Wonder Seed count:

```
0x21f89ab1
0x8c20ccb7   ← CLAUDE.md claimed "lifetime counter" — that was WRONG
0xeeff353b
0x390eb960
0xa0e5f253
```

Event log:

| Timestamp | Event | All 5 hashes write |
|---|---|---|
| 00:00:31.373 | save-load deserializer populates current world (W2) | value=4 (player had 4 W2 seeds at save) |
| 00:03:00.287 | W2 course clear → +1 seed | 4 → 5 |
| 00:03:30.022 | W2→W3 transition (write phase 1) | 5 → 0 |
| 00:04:35.338 | W3 world-map fully loaded | 0 (still) |
| 00:04:58.372 | Poplin house W3 Wonder Seed purchase | 0 → 1 |

**Interpretation**: these are not 5 different counters — they're the
same "currently-viewed world's seed count" value mirrored under 5
hash keys (probably for different sub-systems: UI, gate-check,
PlayReport `wonder_seed` field, save flush, ?).  The CLAUDE.md note
that `0x8c20ccb7` is "lifetime, recomputed at save" was empirically
falsified: this value RESETS to 0 on every world transition.

### Discovery #2: per-world persistent storage is NOT in container A

When the player transitions OUT of W2 (3:30), the W2 count (5) is
**discarded**, not stored in a parallel "W2 counter" hash slot.  We
observed no writes to any container-A hash like "W2_seed_count = 5"
before the transition.  Same on W3 entry — only zero-fill.

This means **the persistent per-world Wonder Seed count is computed
on-the-fly at world entry, not stored**.  The most likely source:
counting set bits in the per-course Wonder Seed bitfield `0x60458608`
filtered to courses belonging to the current world.

Implication for the gate: even if we override the 5-hash "current
view" slot, the game will recompute it the next time the player
re-enters that world's map.  So a durable AP override has to either
(a) write to the per-course bitfield, or (b) hook the recomputation
function.

### Discovery #3: 4 candidate gate-reader caller PCs

Reads of the 5-hash group surfaced these call sites (`call_at` is the
exact `bl FUN_710012ae94` instruction):

| Caller (`call_at`) | Hash read | When |
|---|---|---|
| `NSO+0x5f45a8` | `0x390eb960` | save-load (×2) |
| `NSO+0x5ee8c4` | `0x390eb960` | save-load (×3) |
| `NSO+0x82be90` | `0xeeff353b` | save-load, after course-clear, at W3 transition |
| `NSO+0x5af348` | `0xeeff353b` | course_in, W3 entry |
| `NSO+0x89c8d0` ★ | many hashes | **dominant batch reader** — fires on every UI tick; almost certainly NOT the gate |

**Of the four non-batch callers, none captured a fire DURING the W2
gate-pass event (3:27) or the late-session DENIAL events (5:11+).**
Reason: per-hash budget was exhausted (5 fires) by save-load reads
before the gate decisions ran.

### Discovery #4: ContainerAReader cbz-prologue trampoline DID work

The hook installed cleanly and ran for 5:50 of gameplay without
triggering any guest abort.  This is a positive data point for future
trampoline hooks on functions with `cbz/cbnz/tbz/tbnz` in the
prologue — exlaunch's relocator handles them correctly when invoked
via `HOOK_DEFINE_TRAMPOLINE::InstallAtOffset` (function entry).  The
prior failure at `+0x2743BC` was specifically an INLINE hook
(`HOOK_DEFINE_INLINE`) mid-function, a different code path.

### Numbers

| Channel | Output |
|---|---|
| `installing ...` lines | 18 |
| `pwr table=N off=...` lines | 51 (16 rows × 3 tables + 3 headers) |
| `A_reader` fires | 147 |
| Unique `(hash, caller_pc)` pairs in A_reader | ~60 |
| `gmd.A_writer` fires | ~90 |
| `gmd.D_writer` fires | ~6 (course-clear writes to per-course bitfields) |
| `WorldUnlockCheck(...)` fires | **0** (still never fires — `FUN_7100935ce0` confirmed irrelevant for the 3rd time) |
| `SeedBitfieldRead(hash=0x60458608, ...)` fires | 1 (boot-time only, as expected) |

### Verdict

We have STRONG evidence that:
- The Wonder Seed gate consults the per-current-world count (one of
  the 5 mirror hashes), NOT the per-course bitfield directly.
- The actual gate-reader function is among `{+0x5f45a8, +0x5ee8c4,
  +0x82be90, +0x5af348}` or another caller we missed due to budget.

We do NOT yet have:
- The exact gate-reader function for the threshold compare.
- Confirmation that overriding the 5-hash group is sufficient to
  unlock gates (vs. needing to write the per-course bitfield).

### Concrete next session work

**Iteration #4 — budget tuning, then re-run the same protocol:**

Replace `s_reader_hash[]/s_reader_count[]` (per-hash budget) with
`s_reader_pairs[]` (per-`(hash, caller_pc)`-pair budget) and BLOCKLIST
the dominant batch caller `caller_ret=0x89c8d4` entirely.  Each
distinct call-site to each hash then gets its own 5-fire quota,
which guarantees the gate-decision reads (whichever caller they
come from) will be captured even after save-load uses many slots.

Pseudocode:

```cpp
struct ReaderPairSlot {
    std::atomic<uint64_t> key{0};   // (hash << 32) | (caller_pc & 0xFFFFFFFF)
    std::atomic<uint32_t> count{0};
};
static ReaderPairSlot s_reader_pairs[256];   // 256 slots × 12 B = 3 KB

bool readerBudgetTake(uint32_t hash, uintptr_t caller_pc) {
    if (hash == 0) return false;
    // Blocklist: skip the dominant batch reader (~95% of fires).
    constexpr uintptr_t kBatchReaderCallSite = 0x89c8d0;
    if (caller_pc == kBatchReaderCallSite) return false;
    uint64_t key = (static_cast<uint64_t>(hash) << 32)
                 | (static_cast<uint32_t>(caller_pc));
    // linear-scan CAS-claim or hit-count
    ...
}
```

Then re-run the same test protocol with one critical addition: when
trying to enter the gated W3 level, **wait ~5 seconds** between
denial attempts so the per-frame UI-batch reads don't crowd out the
gate-attempt reads.

Expected result: the gate-attempt reads of `0xeeff353b` /
`0x390eb960` / `0x21f89ab1` etc. surface with **a NEW caller PC**
(not `0x82be90` / `0x5af348` / etc., which we already know).  That
new caller is the gate function.

Alternative if iteration #4 surfaces nothing: hook the FUNCTION that
contains the gate caller PC trace.  Iteration 3's "8-call orphan
dispatch site" at `0x7100480ff8..0x710048104c` (the `FUN_7100935ce0`
callers) is still a candidate IF it's reached via a path we haven't
exercised.  Try fast-travel cursor input.

### Updated falsified candidates

| Candidate fn / hypothesis | Status | Falsifier |
|---|---|---|
| `0x8c20ccb7` is "lifetime Wonder Seed counter" (CLAUDE.md) | ❌ ruled out | Resets to 0 on W2→W3 transition; tracks current-world count |
| 3 tables at NSO `+0x29f0b34..+0x29f13f4` are "per-world record tables with hash at +0x68" (iteration 7) | ❌ ruled out | Tables are flat schema/registry (hash array + parallel type array); not per-world indexed |
| Persistent per-world Wonder Seed count is stored in container A | ❌ ruled out | No writes to "W2_count = 5" before W3 transition; only "current view" slot reset |

## 2026-05-26 — 🎯 GATE FUNCTION FOUND — observability run #4

After iteration #3 hit budget exhaustion, iteration #4 switched to a
per-`(hash, caller_pc)`-pair budget and blocklisted the dominant
batch caller at `NSO+0x89c8d0`.  The user ran a focused 30-second
test: load save, two gate-denial attempts spaced 3 seconds apart in
W3 with only 1 Wonder Seed.

### The match: `call_at=NSO+0x1787b8c` fires exactly twice

```
00:00:38.735  A_reader hash=0x390eb960 value=1  saveLoaded=1  call_at=NSO+0x1787b8c
   ─ ~7s gap ─
00:00:45.908  A_reader hash=0x390eb960 value=1  saveLoaded=1  call_at=NSO+0x1787b8c
```

Two reads, ~7 seconds apart, both returning the W3 current-world seed
count of 1.  Matches the user's "try → wait 3s → try → wait 3s →
quit" protocol exactly.  All other readers of the same hash fired
either at boot/save-load or in tight UI-refresh bursts.

### The gate function: `FUN_71001787b40` @ NSO `+0x1787b40`

Disassembled directly from `main.nso` via [scripts/dump_prologue.py](../scripts/dump_prologue.py):

```asm
+0x1787b40: sub  sp, sp, #0x40                  ; prologue (trampoline-safe)
+0x1787b44: stp  x29, x30, [sp, #0x20]
+0x1787b48: stp  x20, x19, [sp, #0x30]
+0x1787b4c: add  x29, sp, #0x20
+0x1787b50: ldr  x8,  [x0, #0x20]
+0x1787b54: mov  x19, x0                        ; x19 = self (the gate object)
+0x1787b58: sub  x0, x29, #8
+0x1787b5c: and  x8, x8, #-4
+0x1787b60: ldr  w1, [x8]
+0x1787b64: bl   #0x5372d0                      ; some sub-eval (course/gate id?)
+0x1787b68: ldr  x8, [x19, #0x28]
+0x1787b6c: mov  w2, #0xb960                    ; w2 = 0x390eb960 — the per-current-world
+0x1787b70: movk w2, #0x390e, lsl #16           ;   Wonder Seed count hash
+0x1787b74: add  x1, sp, #0xc
+0x1787b78: and  x8, x8, #-4
+0x1787b7c: ldr  w8, [x8]
+0x1787b80: stp  wzr, w8, [sp, #0xc]
+0x1787b84: adrp x8, #0x363f000
+0x1787b88: ldr  x0, [x8, #0xf0]                ; x0 = gmd::sInstance @ +0x363F0F0
+0x1787b8c: bl   FUN_710012ae94                 ; READ count: A_reader(gmd, &out, 0x390eb960)
+0x1787b90: sub  x0, x29, #8
+0x1787b94: bl   #0x3b30a0
+0x1787b98: cbz  x0, #0x1787bc4                 ; bail to "no threshold available" path
+0x1787b9c: ldr  w20, [sp, #0xc]                ; w20 = count
+0x1787ba0: bl   FUN_71001787bd8                ; GET threshold
+0x1787ba4: cmp  w20, w0                        ; cmp count, threshold
+0x1787ba8: cset w1, ge                         ; w1 = (count >= threshold) ? 1 : 0
+0x1787bac: add  x0, x19, #0x30                 ; x0 = &self[+0x30]
+0x1787bb0: bl   FUN_710032aea4                 ; SET gate-passed flag
+0x1787bb4: ldp  x20, x19, [sp, #0x30]
+0x1787bb8: ldp  x29, x30, [sp, #0x20]
+0x1787bbc: add  sp, sp, #0x40
+0x1787bc0: ret
```

**Behavior**: takes `x0` = gate object pointer, computes
`count_in_current_world >= threshold_for_this_gate`, stores the
boolean result into `self[+0x30]` (via the setter `FUN_710032aea4`).

**Companion functions**:
- **`FUN_71001787bd8`** — threshold getter.  Reads `*(self+0x39)` flag
  byte; fast path returns `*(self+0x34)` directly; slow path follows
  pointer chain `*(self+0x10) → *(self+0x18) → *(x8+0xc)` (likely a
  child container of thresholds).
- **`FUN_710032aea4`** — flag setter; stores the bool into the gate
  object's state struct.
- **`FUN_71001787b04`** — direct caller of `FUN_71001787b40`; does
  post-evaluation bitfield update on `*(gate+0x18)`.

**Prologue trampoline-safe** (verified): `sub sp` + `stp` + `stp` +
`add x29` + `ldr` — no PC-relative ops in the first 5 instructions.

### Implementation paths for AP override

Three viable approaches, ordered by cleanness:

**(A) Override the threshold** — hook `FUN_71001787bd8`, intercept
the return value, return 0 when AP says the player should pass this
gate.  Pros: tiny diff, single function-pointer override, natural
game-state recompute happens correctly.  Cons: need to identify
WHICH gate is being queried (from the `self` pointer) to decide
whether AP wants this one to pass.

**(B) Override the count read** — hook `FUN_710012ae94` filtered on
`caller_pc == NSO+0x1787b90` (just this one call site).  Substitute
the AP-granted per-world seed count for the queried world.  Pros:
single-purpose narrow override; we already hook this function.
Cons: same gate-identity problem as (A); also the natural count
gets used for UI display elsewhere — we don't want to override THAT.

**(C) Override the predicate result** — hook `FUN_71001787b40` itself,
call Orig() to let it compute naturally, then write `true` into
`self[+0x30]` (via the same setter or direct memory write) if AP
wants the gate to pass.  Pros: single hook, doesn't perturb counts
elsewhere.  Cons: need to identify gate from `self`; flag-offset
within `+0x30` substruct still needs decompiling.

**Recommended: (A) threshold override.**  Hook `FUN_71001787bd8`,
inspect `self` to identify the gate, return AP's threshold (0 if
pass, original if not).  The lowest-impact change with the highest
signal-to-correctness.

### Gate identity — open question

`FUN_71001787b40` takes `x0` = a gate-state struct.  To override per
AP, we need to know which gate is being queried.  Options:

1. **Stable pointer**: each gate has a unique struct pointer at a
   known location.  Hook Orig() and snapshot `x0` for each fire;
   correlate with which gate the user is approaching.
2. **Identifier field**: read `*(self + N)` for some N — likely a
   gate-id, course-id, or world-id field.  Inspection from the
   decompile of FUN_71001787b04 (the caller) or
   FUN_71001787b40's `bl #0x5372d0` early call would reveal this.
3. **Caller dispatch**: the call site at `+0x1787b14` is the only
   caller of FUN_71001787b40 we've observed.  Look at FUN_71001787b04's
   callers to find per-gate dispatch logic.

### Next session work (final stretch)

1. **Decompile `FUN_71001787b40` + caller chain `FUN_71001787b04` →
   its callers**.  Identify the gate-identity field in `self`.
2. **Test the 5-hash override hypothesis**: if AP writes 5 to all 5
   per-current-world hashes (`0x21f89ab1, 0x8c20ccb7, 0xeeff353b,
   0x390eb960, 0xa0e5f253`) just before the user approaches a W3
   gate needing 5 seeds, does the gate pass?  This is the cheapest
   first AP override path — write per-world counts to the current
   view slot and let the natural game logic handle it.
3. **OR** if step 2 doesn't work (because the recompute on world
   re-entry undoes it), implement the threshold-override hook on
   `FUN_71001787bd8`.

### Closeout for observability hooks

`WorldUnlockCheck` — confirmed irrelevant across 4 runs, 0 fires.
**Remove it.**

`SeedBitfieldRead` — fires once at save-load to populate the
world-map UI render cache; never on the gate path.  **Remove it**
once the gate-override implementation is committed.

`ContainerAReader` — the key hook that surfaced the gate function.
**Keep installed** during gate-override development as a sanity
signal; retire before shipping.

`probe::dumpPerWorldTables` — one-shot debug aid that did NOT yield
the per-world hash list (tables turned out to be schema/registry,
not per-world records).  Useful negative finding; **remove**.

## 2026-05-26 — ✅ HYPOTHESIS CONFIRMED — 5-hash override unlocks gates

Iteration #5 landed `probe::pushWonderSeedOverride(uint32_t value)` —
writes `value` to all 5 per-current-world Wonder Seed count hashes via
the existing container-A counter writer at NSO `+0x0049F648`.  Wired
into `NerveActivateOnce::Callback` on a ~120-fire tick (~2 s under
normal gameplay), gated on `probe::isSaveLoaded()`.

**User test (2026-05-26)**: same save state from iteration #4 (W3 with
1 actual Wonder Seed; a gate in W3 was previously denying entry).
Build deployed; player loaded the save and walked to the same gate.

**Result**:
- Wonder Seed counter UI showed **99** (the override value).
- The gate that previously denied entry **opened**.
- Level entry succeeded.

This closes the gate-question.  The Wonder Seed gate predicate
`FUN_71001787b40` reads container-A hash `0x390eb960` for the count;
overriding that hash (and its 4 mirrors) is sufficient to satisfy any
seed-gated path in the game.

### Production path for AP integration

Drive `probe::pushWonderSeedOverride(per_world_count)` from the bridge
on every Switch `HelloMsg` + every AP `ReceivedItems` for any Wonder
Seed item + a periodic ~2 s tick.  The bridge tracks
`per_world_count[8]` derived from AP `items_received` filtered to
Wonder Seed items per bucket (W1-W6, Petal Isles, Special).

When the player switches worlds on the map, the natural deserializer
writes the new world's count from per-course bitfield computation.
Our tick then overwrites with AP's count for the current world.
Brief flicker possible on world transitions (~2 s window) but the
override re-asserts on the next tick.

### Required additional plumbing

1. **Current-world tracking.**  The override has to use the
   *currently-viewed* world's AP count, not all-worlds-combined.
   The current world index is in container-A hash `0x9f5ead3c`
   (observed empirically: W2→3 transition at run 09-34-58 04:35.272
   wrote it from 2 to 3).  Bridge can subscribe to writes of this
   hash (we already log them) or the Switch can hash-route the
   override at write time.
2. **Wire schema.**  New `WonderSeedOverride { current_world: u8,
   count: u32 }` message OR generalize: just send the 8-bucket
   per-world array and let the Switch pick the right one based on
   `0x9f5ead3c`.
3. **Wonder Seed AP items** (W1..W6 Wonder Seed, Petal Isles Wonder
   Seed, Special World Wonder Seed) need to be added to
   `apworld/smbw_archipelago/data/items.json` and Items.py if not
   already; the bridge counts them per bucket.

### Iteration #5 status

Smoke test code as-shipped writes a hard-coded `99` to all 5 hashes
unconditionally.  This is GOOD for verifying the hypothesis but
should NOT be merged to production — the value needs to be
AP-derived per-world.

**Next session work** (M3 implementation):
- Replace the hard-coded `pushWonderSeedOverride(99)` in
  `NerveActivateOnce::Callback` with a bridge-driven call that uses
  the AP-derived count for the current world.
- Add the per-world current count message type on the wire.
- Wire `pushWonderSeedOverride` to fire on `HelloMsg` and on
  per-world-count updates (when AP grants a Wonder Seed item for a
  world).
- The 2 s tick is the safety net for world-map navigation.
- Add tests for the new wire message; live-validate end-to-end with
  AP sending W1-only seeds and confirming W1 gates open but W2 gates
  stay closed.

### Decommissioning the observability hooks

Now that we have a working gate override, the following hooks can be
retired (in the same PR or a follow-up):

- `WorldUnlockCheck` (NSO `+0x935ce0`) — confirmed not the gate, 0
  fires across 4 runs.
- `SeedBitfieldRead` (NSO `+0x124134`, filtered on `0x60458608`) —
  used for the iteration #2 caller-PC capture; mission complete.
- `ContainerAReader` (NSO `+0x12ae94`) — used for the iteration #3-4
  caller-PC bucket; mission complete.  Keeps fires logged via the
  existing `gmd.A_writer` trampoline if needed for debugging.
- `probe::dumpPerWorldTables` — call site in SeedBitfieldRead's first
  fire.  Negative result.
- `probe::readerBudgetTake` slot arrays — only used by ContainerAReader.

The `GmdContainerAWriter` trampoline at NSO `+0x49F648` MUST stay —
that's the writer our override uses + the save-loaded gate signal.
