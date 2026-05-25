# SMBW Archipelago — orientation for Claude Code

This is the project-overview doc. For current-state details and the M-numbered roadmap, always read [docs/handoff.md](docs/handoff.md) first and [docs/milestones.md](docs/milestones.md) second.

## What this project is

An Archipelago multiworld integration for **Super Mario Bros. Wonder** (SMBW v1.0.0) running on modded Switch + Ryujinx. The architecture mirrors the user's existing `smo_archipelago` project (Super Mario Odyssey): a Switch subsdk hooks the game NSO, ships events over LAN to a PC-side Python service, which bridges to the Archipelago server.

- **Dev target**: Ryujinx 1.3.3 (fast iteration loop).
- **Production target**: real modded Switch under Atmosphere CFW (M6).
- **Replaces**: the user's existing Archipelago Manual world at `manual_smbwonder_zim/` (manual checkbox-ticking) with automatic event-driven check-detection.

## Repo layout

```
smwonder_archipelago/             ← outer git repo (this one)
├── CLAUDE.md                       you are here
├── docs/
│   ├── handoff.md                  current state + recent decisions (READ FIRST)
│   ├── milestones.md               M1-M7 roadmap
│   └── save-diff-grants.md         M3 grants handoff (Ghidra dead-end; pivot to save-diff)
├── bridge/                         PC-side Python: PlayReport decoder + tests
│   ├── play_report.py              CBOR-ish format decoder (see header docstring)
│   └── test_play_report.py         44 tests; 3 live W1-1 fixtures
├── manual_smbwonder_zim/           existing Archipelago Manual apworld (Python)
│                                   gets replaced when M4+M5 land
└── switch-mod/                     fork of mdietz94/wondar (its own git repo)
    ├── CMakeLists.txt              modified: -fpermissive + symlink shim for Windows
    ├── cmake/toolchain.cmake       devkitA64 cross-compile
    ├── syms/100/sdk.sym            ★ Nintendo SDK symbol map for InstallAtSymbol
    ├── lib/                        vendored sead/imgui/NintendoSDK submodules
    ├── src/
    │   ├── lib/                    wondar's inlined exlaunch source
    │   └── program/
    │       ├── main.cpp            ★ all our hook installs and callbacks
    │       ├── util/Log.{hpp,cpp}  smbwap kernel-debug logger (no thread_local)
    │       ├── util/TargetActorProbe.{hpp,cpp}  legacy probe (stub)
    │       └── pe/                 wondar's DbgGui (disabled in our build)
    └── build/                      ← gitignored, CMake artifacts
```

The outer repo `.gitignore`s `switch-mod/` because switch-mod is itself a git repo (fork of `mdietz94/wondar`). It may be promoted to a git submodule once published.

## Daily dev loop

PowerShell. Build + deploy:

```pwsh
$env:DEVKITPRO = "C:\devkitPro"
$env:PATH = "C:\devkitPro\msys2\usr\bin;" + $env:PATH

& "C:\Program Files\CMake\bin\cmake.exe" --build `
    "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod\build"

$dst = "$env:APPDATA\Ryujinx\mods\contents\010015100b514000\smbwap\exefs"
Copy-Item "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod\build\subsdk9" `
          -Destination $dst -Force
Copy-Item "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod\build\subsdk9.npdm" `
          -Destination "$dst\main.npdm" -Force
```

First-time configure (only after blowing away `build/`):

```pwsh
& "C:\Program Files\CMake\bin\cmake.exe" `
    -S "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod" `
    -B "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod\build" `
    -G Ninja `
    -DCMAKE_TOOLCHAIN_FILE="C:/Users/maxwe/Documents/smwonder_archipelago/switch-mod/cmake/toolchain.cmake"
```

Tail the live game log (Ryujinx writes `svcOutputDebugString` to its file log):

```pwsh
$latest = Get-ChildItem "C:\Users\maxwe\Desktop\Switch\ryujinx-1.3.3\Logs\Ryujinx_*.log" |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content -Wait $latest.FullName | Select-String '\[smbwap'
```

Our log lines are prefixed `[smbwap inf]` / `[smbwap dbg]`. Ryujinx writes them with embedded NUL bytes; offline parsing wants `tr -d '\0'` first.

## Game artifacts

- Target version: **SMBW v1.0.0**, BID `CD6E42AEE7934F4D`, internal codename `Secred.nss`.
- Extracted NSO: `C:\Users\maxwe\Desktop\Roms\Switch\Super Mario Bros. Wonder\main.nso`.
- Tool used to extract: `C:\Users\maxwe\Desktop\Switch\hactool.exe`; keys at `C:\Users\maxwe\.switch\prod.keys`.
- **Do not apply the v1.0.1 update in Ryujinx** — every offset in our hooks is pinned to v1.0.0.

## Two hook patterns established

All hooks live in [switch-mod/src/program/main.cpp](switch-mod/src/program/main.cpp). NSO base when loaded is `0x7100000000`; `exl::util::modules::GetTargetStart()` returns this.

### Pattern 1: NSO offset hook (M1)

`Hook::InstallAtOffset(0xXXXXXX)` — addresses are NSO-relative. Used for game functions we've reverse-engineered in Ghidra. Examples:

- `NerveActivateOnce::InstallAtOffset(0x559f7c)` — traps the shared "Nerve one-shot activate" helper. Read `nerve[0]` (the vtable pointer) at callback entry; filter by `vt_off = vtable - GetTargetStart()` to identify *which* Nerve fired.
- `SetCourseClearFlagExecute::InstallAtOffset(0x1bf28cc)` — direct trampoline on the SetCourseClearFlagToGameData Nerve's `execute` slot 8.

### Pattern 2: SDK symbol hook (M2.4, FSHacks)

`Hook::InstallAtSymbol("_ZN2nn...")` — resolves a mangled C++ symbol via `nn::ro::LookupSymbol`. Survives game/SDK rebuilds. Examples:

- `pe::FSHacks::OpenFile::InstallAtSymbol("_ZN2nn2fs8OpenFileEPNS0_10FileHandleEPKci")`
- `PlayReportSetEventId::InstallAtSymbol("_ZN2nn5prepo10PlayReport10SetEventIdEPKc")`
- The two IPC SaveReport methods (mangled names are huge; see main.cpp).

The full Nintendo SDK symbol table is in [switch-mod/syms/100/sdk.sym](switch-mod/syms/100/sdk.sym) — every symbol has its sdk-relative offset.

## Critical gotchas (don't relearn these)

1. **Never use `thread_local` in subsdk code.** No TLS allocator is registered before our code runs; nnSdk's `SetMemoryAllocatorForThreadLocal` Aborts at module load. Crash signature: Result `0xCA8`, User Break, stack ends at `SetMemoryAllocatorForThreadLocal`. Use `static std::atomic<...>` + manual TID check instead.

2. **`exlaunch`'s And64InlineHook patches the first 5 instructions (20 bytes).** Any PC-relative instruction (adrp, ldr-literal, b/bl) in those bytes corrupts the trampoline. Symptoms appear *delayed* — typically the next time the function or another code path runs through the patched region. Wonder Seed Nerve slot 8 (`FUN_7101562fb4`) got bitten by this. Workaround: hook a shared inner helper instead (e.g. `FUN_7100559f7c`) and filter by caller identity.

3. **Hooking `nn::prepo::PlayReport` member functions beyond ctor + SetEventId crashes the game.** Even no-op trampolines on `Save()` trigger a delayed guest abort 5-6 s later on a different SDK validator thread (`ModuleSystemWorker1` or `gmd::SaveDataMgr` depending on which audit runs first). **Workaround**: drop below the PlayReport class to the IPC client layer `CmifProxyImpl<IPrepoService>::_nn_sf_sync_SaveReport{,WithUser}` which sees the already-serialized payload and is below the audit checkpoints.

4. **NSO and SDK have independent base offsets.** Game symbols use `__main_start + 0x...` (NSO-relative); SDK symbols use `__sdk_start + 0x...`. Don't mix them — InstallAtOffset is for the former, InstallAtSymbol resolves both transparently.

5. **R_ABORT_UNLESS in `InstallAtSymbol`** aborts hard if the symbol can't be resolved. If a symbol typo or version mismatch slips in, the subsdk dies before the game window opens. Watch the early log lines after `installing X @ Y` to confirm the install succeeded.

6. **PlayReport ABI surprise**: the with-event-id ctor is rarely used. The game almost always uses the no-arg ctor + `SetEventId("room_name")`. If you hook only the with-event-id ctor, you'll see zero firings even when reports are being sent. Hook `SetEventId` instead.

7. **Always import `switch-mod/syms/100/*.sym` into Ghidra at project setup** — not just `sdk.sym`. The previous M3 static-RE sprint missed the `gmd::GameDataMgr::sInstance` anchor that's been sitting in [switch-mod/syms/100/gmd/GameDataMgr.sym](switch-mod/syms/100/gmd/GameDataMgr.sym) the whole time. Use [scripts/ghidra/import_sdk_symbols.py](scripts/ghidra/import_sdk_symbols.py) — it walks the entire `syms/100/` tree.

## GameDataMgr (gmd::) save-data API

Discovered 2026-05-24 via static-analysis sprint 2. Full decompile details in [docs/static-analysis-findings.md](docs/static-analysis-findings.md).

**The singleton anchor**: `gmd::GameDataMgr::sInstance` lives at NSO `+0x0363F0F0`. Dereferencing this qword at runtime gives the live `GameDataMgr*`. This replaces any pointer-scan workflow for finding the live save-data state.

**The grant primitive (M3.3 counters)** — ✅ **shipped 2026-05-25** as
`probe::grantContainerACounter(hash, value)` in
[switch-mod/src/program/main.cpp](switch-mod/src/program/main.cpp).
Live-validated end-to-end with `flower_coin`: the boot-time smoke test
called `grantContainerACounter(0xf4ee6827, 99)`, saved + quit, save-diff
showed file offset 0x0894 went `06 00 → 63 00`.  `regular_coin` works by
the same path.  Wire protocol gained `GrantHashKeyedMsg` (bridge ↔ Switch),
inbound dispatch in [switch-mod/src/program/ap/ApFrameBridge.cpp](switch-mod/src/program/ap/ApFrameBridge.cpp).

⚠️ **M3.3b Royal Seeds** — primitive call FALSIFIED 2026-05-25.  The
same writer was called with hash `0x55815859` value `1` (GRAND_SEED_WORLD1),
trampoline confirmed the call, but post-save the value at file offset
`0x0354` stayed at `0`.  Container-A writer is typed and silently no-ops
on bool-typed slots.  M3.3b needs the container-B writer (candidate
`FUN_71005E93FC`, per docs/static-analysis-findings.md).  Bridge plumbing
(`royal_seed_table.py`, `send_grant_hash_keyed`, `GrantHashKeyedMsg`,
`drainInbound` dispatch) is kept wired and reusable -- when the bool
writer ships, only the Switch-side dispatcher branches on hash to route
the 6 Royal Seed hashes to it.  `ap_client._handle_received_items` warns
on Royal Seed forwards today so the operator isn't surprised.

```cpp
// Container A counter writer.  Lock-free, thread-safe (uses ARM
// exclusive-monitor atomics on the dirty queue at gmd->[+0xf8]).
// Deferred-write: the value is queued and applied to the persistent
// container at next save.
void FUN_710049F648(GameDataMgr* gmd, uint32_t value, uint32_t hash);
// at NSO +0x0049F648
```

Confirmed hash keys (cross-verified via MemetendoYT save editor):

| Hash | Field | Width | Save offset |
|---|---|---|---|
| `0xf4ee6827` | flower_coin (purple coins) | u16 | 0x0894 |
| `0x17f0bb21` | regular_coin | u8 | 0x08AC |
| `0x55815859` | GRAND_SEED_WORLD1 (Royal Seed W1) | u8 bool | (pair region) |
| `0x49abba86` | GRAND_SEED_WORLD2 | u8 bool | (pair region) |
| `0xb550d8d6` | GRAND_SEED_WORLD3 | u8 bool | (pair region) |
| `0x1dcf7f6e` | GRAND_SEED_WORLD4 | u8 bool | (pair region) |
| `0x0d5a3e00` | GRAND_SEED_WORLD5 | u8 bool | (pair region) |
| `0xd4660d2b` | GRAND_SEED_WORLD6 | u8 bool | (pair region) |
| `0x5d3ec9b4` | COMPLETE_GAME | u8 bool | (pair region) |
| `0x89f1cc52` | INTRO_CUTSCENE_COMPLETED | u8 bool | (pair region) |
| `0xdf82e9ab` | "current course" hash (lookup key, not a writable field directly) | — | — |

**The API surface** (confirmed roles):

| NSO offset | Role | Signature |
|---|---|---|
| `+0x710012AE94` | Container A **reader** (counter GET) | `(gmd, uint32_t* out, uint32_t hash)` ← corrected 2026-05-24 from FUN_7101a5d9a0 call site |
| **`+0x710049F648`** | Container A **WRITER** (counter SET) | `(gmd, value, hash)` ★ grant primitive |
| `+0x71003838AC` | Sub-bool **reader** (handles INTRO, COMPLETE_GAME reads) | `(sub_obj, uint8_t* out, uint32_t hash)` |
| `+0x71003D3FB0` | Stage-info hash → course-index **translator** | `(top_hash, &out_index)` |
| `+0x71003D4110` | **Murmur3-32(course_name) → course_index** lookup over 81 hardcoded course strings | `(target_hash, &out_index)` |

Additional hash keys discovered 2026-05-24 in `FUN_7101a5d9a0` call sites:
- `0xed817774` — container-B bool flag (read into session+0x546 `touch_goal_top_result`; semantics likely per-course "ever-touched-top-of-flag" progress bit)
- `0xf79bcbb0` — container-A counter (read into session+0x478 `goal_id`; semantics likely last goal_id reached on current course)

⚠️ **Past wrong guesses now corrected**: the previous CLAUDE.md comment "`FUN_71003D3FB0` writes field by hash" was wrong — it's a hash-to-course-index translator, not a writer. `FUN_71003838AC` is a reader (not "unified get/set" as initially guessed).

**The hash function for FIELD NAMES is unknown** (Murmur3 of obvious names like `"flower_coin"` doesn't match). Not blocking — we already have the 8 verified hashes. May be a different algorithm, may use internal/Japanese strings, may be precomputed offline.

**Deferred-write implication**: a write via `FUN_710049F648` is applied to the live container at the next save. For UI to refresh immediately (in-game purple coin counter, etc.), the grant code should ALSO write the live-state struct field directly (HamletDuFromage cheat anchors give the offsets: flower_coin at `live_base + 0xC8`, lives at `live_base + 0x60`, etc.). Dual-write strategy described in [docs/static-analysis-findings.md](docs/static-analysis-findings.md).

⚠️ **Save-survival caveat for all container-A grants** (coin counters
today; bools once the M3.3b writer ships) — same root cause as the M3.2
badge follow-up. The `FUN_710049F648` write queues to the dirty buffer
at `gmd->[+0xf8]`; if the player loads a fresh save before the buffer
flushes, the grant is lost.  Two mitigations: (a) explicit save after
each grant (the smoke-test path), or (b) the M4.5 bridge
replay-on-`HelloMsg` work that re-emits every received item every time
the Switch reconnects.  Today's M3.3 wiring relies on (a); (b) is the
only durable fix and covers badges + container-A items + future
container-B items uniformly.

⚠️ **Critical — the save-diff sprint did NOT produce a live-grant mechanism.** The file-offset writers anchored on the `savedata_id` UUID at file offset `0x50b8` modify only the **save-OUT staging buffer**, which exists transiently during/after save serialization. The game populates this buffer FROM live state on every save; writes into it are overwritten on the next save event and never change live gameplay. What that work produced is a **save-file editor capability** (offline modification of `game_data.sav`) and a **verification target** (predict the bytes a successful live grant will write). For ALL live in-game grants, the only path we have is the GameDataMgr API above (`FUN_710049F648` for container-A counters; other accessors TBD for container-B fields like badges and per-course flags). See [docs/runtime-address-backtrace-plan.md](docs/runtime-address-backtrace-plan.md) for the discovery of this distinction.

### M3.2 badge-grant: ✅ SOLVED 2026-05-24

Badges live in **Container C** at `gmd+0x70..0x8c` — a previously
unmapped sub-container holding hash-keyed bitfields. The badge owned
bitfield is at:

- **Hash**: `0x105df820`
- **File offset**: `0x0EA0` (u64 LE)
- **Bit position == internal_id == badge ID** (e.g., bit 4 = Spring Feet)

**The grant primitive** (in [switch-mod/src/program/main.cpp](switch-mod/src/program/main.cpp)
under `namespace probe`):

```cpp
probe::setBadgeBitfieldAbsolute(uint64_t bits);  // overwrites whole bitfield, returns bool
```

Walks the container-C bucket at `gmd+0x80` for the badge hash, follows
to the typed-sub-obj at `gmd+0x78 + idx*0x40 + 0x28`, writes `bits` to
the live `uint32_t[]` data (`data[0..1] = lo, hi` plus mirror at
`data[2..3]`).  Validated end-to-end via Spring Feet (bit 4 = 0x10) —
badge appears immediately in the live game UI without save+reload.

**M4 follow-up #2 — AP-authoritative badge sync (shipped 2026-05-25)**.
The original M3.2 primitive `probe::grantBadgeBit(internal_id)` (OR a
single bit) has been **replaced** by `setBadgeBitfieldAbsolute`.
Rationale: in-game badge acquisition (Poplin shop, badge house, badge
medley, badge challenges) bypassed AP, so AP wasn't the sole authority
over the badge pool — required for M5.  Solution: bridge holds the
canonical `_badge_mask` derived from AP `items_received`, and
overwrites the Switch's bitfield to that exact set on three triggers:
(1) every AP `ReceivedItems`, (2) every Switch `HelloMsg`
(replay-on-reconnect; subsumes the planned M4.5 replay), and (3) a
~2 s periodic tick that reverts any in-game pickup within seconds.
Idempotent by construction — same input always produces the same final
state.  Wire type: `SetBadgesAbsoluteMsg { bits: u64 }`
([bridge/wire.py](bridge/wire.py)) ↔ `WireSetBadgesAbsolute`
([switch-mod/src/program/ap/ApProtocol.hpp](switch-mod/src/program/ap/ApProtocol.hpp)).

⚠️ **Gotchas during discovery (don't relearn)**:
- Hash `0x6d1b5c25` is an auxiliary "UI-slot" bitmap (file offset
  `0x1204`), NOT the owned bitfield. Both are queried by the badge
  inventory UI; only `0x105df820` controls actual ownership.
- The save serializer filters out bit positions that don't correspond
  to real badges in the registry (we saw bits 30-31 stripped during
  the "all bits" experiment). Stick to valid internal_ids.
- Murmur3-32 brute force ruled out naming the hash — field name is
  Japanese / encoded / pre-computed. Discovered via in-game probing.
- 14 Ghidra rounds + 6 hook iterations. Full forensics in
  [docs/static-analysis-findings.md](docs/static-analysis-findings.md)
  under "2026-05-24 — M3.2 SOLVED".

## Nerve system primer

Nintendo's Nerve system has two flavors with different hook strategies:

- **Active Nerves**: tick every frame. Execute method shape: `if (flag == 0) FUN_7100559f7c(this); FUN_7100005390(this+0x68);`. The first call does one-shot work, the second advances state. Examples: Wonder Seed pickup, scene transition. **Hook strategy**: trampoline `FUN_7100559f7c`, filter by `nerve[0]` (vtable).
- **One-shot dispatch Nerves**: `execute` is called explicitly by other code at a specific moment. Inline body. Examples: `SetCourseClearFlagToGameData`. **Hook strategy**: trampoline directly on the execute function. Validate the prologue has no PC-relative loads first.

Recipe for finding a new Nerve hook target:

1. Search Ghidra strings for the event name (`RequestEventXxx`, `SetXxx`, `GetXxx`).
2. Find the getter that returns the string; its address is **vtable slot 0** somewhere in `0x710334XXXX` / `0x71033fXXXX` / `0x71034BXXXX` (the Nerve vtable regions).
3. Read the vtable layout. Shared base method slots: `LAB_71014498**`, `LAB_7101e078e4`. Event-specific overrides: slots 7, 8, and a few high ones.
4. **Slot 8 is execute.** Check whether slot 8's address appears in `FUN_7100559f7c`'s 19-entry xref list:
   - Yes → active Nerve. Use `NerveActivateOnce` and add the vtable offset to the filter.
   - No → one-shot. Peek the execute function's prologue. If clean, hook directly.

## Address space layout in the loaded NSO

- Base: `0x7100000000`.
- `.text`: roughly `0x7100000000`-`0x7102800000`.
- `.rodata` strings: `0x71028XXXXX`-`0x71029XXXXX`.
- Vtables: `0x710334XXXX`, `0x71033fXXXX`, `0x71034BXXXX` (Nerve regions we've mapped).
- Itanium-style typeinfo: `0x71000ac930` is a function appearing in every Nerve vtable's `-8` slot (probably a generic destructor or dispatch helper, not std::type_info).

## Tools

- **Ghidra 11.3 or 11.4** + **Adubbz Switch Loader 1.7.0** (`File → Install Extensions`).
- **JDK 21** required by Ghidra 11.x.
- **One-time Ghidra setup**: run [scripts/ghidra/import_sdk_symbols.py](scripts/ghidra/import_sdk_symbols.py) — walks the entire `switch-mod/syms/100/` tree (gmd, sead, main, sdk) and applies ~97 named labels. Massive nav speedup once `gmd::GameDataMgr::sInstance`, `nn::`, `sead::`, etc. names appear in the listing.
- **Sprint-2 RE scripts** (2026-05-24, used to crack the M3 grant API): [scripts/ghidra/find_gamedatamgr_xrefs.py](scripts/ghidra/find_gamedatamgr_xrefs.py), [scripts/ghidra/walk_hash_writer_xrefs.py](scripts/ghidra/walk_hash_writer_xrefs.py), [scripts/ghidra/find_offset_constant_xrefs.py](scripts/ghidra/find_offset_constant_xrefs.py), [scripts/ghidra/playreport_field_backtrace.py](scripts/ghidra/playreport_field_backtrace.py). Run order + roles in [scripts/ghidra/README.md](scripts/ghidra/README.md).

## What's done, what's next

**M1 (✅ done)**: Two hooks proven end-to-end:
- `WONDER_SEED_AWARDED` — every Wonder Seed grab (124 AP checks).
- `COURSE_CLEARED` — every successful flagpole touch + every palace boss clear (206 AP checks lumped; exit-type splitting is M2.5).
- Total: 330 / 663 AP checks covered (49.8%).

**M2.4 + M2.5 (✅ done — Switch capture, Python decoder, full corpus)**: PlayReport payload capture via the IPC-layer pattern; Python decoder in [bridge/play_report.py](bridge/play_report.py) handles the Nintendo CBOR-ish format end-to-end. **87 tests pass against 9 live fixtures** covering all 5 observed room types (`world_activity`, `world_result`, `course_in`, `course_result`, `koopajr_result`) and edge cases (Top of Flag, Secret Exit, palace LOSS, palace WIN, inter-world transition).

The M2.5 exit-type discriminator table is locked in (199/199 goal+palace AP checks structurally classifiable). Importantly: a palace WIN emits BOTH `course_result` AND `koopajr_result` ~1 ms apart for the same event — the bridge prefers `koopajr_result` when both fire; `course_result.world_mother_seed == True` is a defensive cross-check.

**M2.6 (✅ done — bridge skeleton + course correlation)**: state + protocol + processor in `bridge/`, 106 Python tests passing across PlayReport decode + event-routing.

**Save-diff sprint (✅ done, 2026-05-22..23)**: byte-exact write targets identified for badges (file offset `0x0EA0` u64 bitfield), per-course flag arrays (16+ trailing-region u32 arrays, stride 4), and pair-region keys. Full layout + runtime anchor in [docs/save-diff-findings.md](docs/save-diff-findings.md). Externally cross-verified against MemetendoYT/SMBW-SaveGame-Editor.

**Static-analysis sprint 2 (✅ done, 2026-05-24)**: **the M3 grant API is decompiled**. Full details in [docs/static-analysis-findings.md](docs/static-analysis-findings.md). The previous (2026-05-21) "dead-end" verdict was wrong; this sprint succeeded with: imported sym files (finding the singleton anchor), dataflow-anchored xref harvesting (finding the writer at `+0x0049F648`), cross-reference against the HamletDuFromage cheat DB + MemetendoYT (validating the 8 known hash keys), and direct decompile of the GameDataMgr accessors. Key result: `FUN_710049F648(gmd, value, hash)` is the universal counter writer for container A — enabling flower_coin, regular_coin, all 6 Royal Seeds, COMPLETE_GAME, and INTRO_CUTSCENE_COMPLETED grants in one function call.

**Next** (per [docs/handoff.md](docs/handoff.md) "Next session priorities" 2026-05-24):
- **Priority 1**: wire `gmd::GrantFlowerCoin(99)` smoke test in [switch-mod/src/program/main.cpp](switch-mod/src/program/main.cpp); validate via save-diff. ★ One-call test of the entire static-analysis sprint deliverable.
- **Priority 2**: generalize to all 8 hash-keyed grants (regular_coin, 6 Royal Seeds, COMPLETE_GAME, INTRO). The Royal Seed test is the experimental case — if container A also holds the seed bools, M3.3b is solved with no further RE.
- **Priority 3**: bridge integration — extend [bridge/protocol.py](bridge/protocol.py) with `GrantHashKeyed` message variant.
- **Priority 4** (only if Royal Seeds DON'T work via container A): decompile `FUN_71005E93FC` + `FUN_710059F894` for container-B writes.
- **M3.8 DeathLink detection** + **M4.1/M4.2 LAN socket** + **DeathLink trigger** + **per-course flag writers via [scripts/ghidra/find_offset_constant_xrefs.py](scripts/ghidra/find_offset_constant_xrefs.py)** — all queued but lower priority than proving the grant primitive end-to-end.
- Deferred: M2.2 (10-coin), M3.1 (power-ups), M3.4 (chars), M3.5/M3.6/M3.7, M5/M6/M7.
- ✅ M3.2 done — see badge-grant section above.

## Reference: sister project

The user's existing `smo_archipelago` project (Super Mario Odyssey integration; same architecture pattern) lives at `C:\Users\maxwe\Documents\smo_archipelago\`. Mirror its `switch-mod/src/ap/ApClient.cpp` when wiring up the LAN socket; mirror its `apworld/` and `scripts/` layout when building the Python bridge.
