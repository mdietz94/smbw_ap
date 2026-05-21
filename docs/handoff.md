# SMBW Archipelago — handoff doc

Last updated: 2026-05-20 evening — M2.4 IPC payload-capture path proven.

This is the "next session, hi me again" doc. Read it first.

## TL;DR — where you are right now

**M1 done. M2.4 now spec-complete on the Switch side via the IPC-layer pattern.**

- **M1**: Both critical-path hooks work end-to-end. `WONDER_SEED_AWARDED` fires on every Wonder Seed grab; `COURSE_CLEARED` fires on every successful flagpole touch *and* on Royal Seed palace clears. **330 of 663 AP checks covered (49.8%)**.
- **M2.4 (real hardware)**: After a multi-step bisect on 2026-05-20, the working pattern is:
  1. Hook `nn::prepo::PlayReport::SetEventId` to capture the **room name** (event id) — safe.
  2. Hook the IPC-client `CmifProxyImpl<IPrepoService>::_nn_sf_sync_SaveReport{,WithUser}` to capture the **already-serialized payload bytes** — safe.
  3. The decoder runs in the **PC-side Python bridge** (M4), not on Switch — bytes ship over the wire.

  **Critical**: hooking PlayReport class members `Save()`/`Save(Uid&)`/any `Add(...)` overload triggers a delayed SDK abort on a *different thread* depending on which validator notices first (ModuleSystemWorker1, gmd::SaveDataMgr observed; see "What didn't work"). The IPC client layer below the PlayReport class is below those validators and works cleanly.

  **What we captured on 2026-05-20 to prove the path**:
  - `room=course_in pay=0x... size=355 flags=0x0` from entering Bulrush Coming Through
  - First 64 bytes of the payload, e.g. `de 00 0f ab 73 61 76 65 64 61 74 61 5f 69 64 d9 23 62 38 31 33 65 36 37 35...`
  - Ryujinx's prepo decoder confirmed the same buffer represents: `stage_info.stage_key=2308078743, world_no=1, course_no=30, lucky_coin=135, world_wonder_flower=14, equip_badge_id=[34]`, etc.

**Open questions / next session**:
- Build the PC-side payload decoder (Python; ~50 LoC, format is CBOR with one Nintendo extension — see "PlayReport payload format" below).
- Expand the room-name corpus by playing through clears, secret exits, and palace boss fights — each adds one room name to the M2.4 spec.
- Cover **10-coins** (305 checks) via Nerve hook (M2.2).
- Cover **badge unlocks** (24 checks) via either Nerve hook or function-name xref (M2.3).
- Start the **LAN protocol / Python bridge** (M4) — the IPC hook captures bytes that need to ship to the bridge.

## Project layout

```
C:\Users\maxwe\Documents\smwonder_archipelago\
├── docs\
│   ├── handoff.md          ← you are here
│   └── milestones.md       ← read this next for the M2+ plan
├── manual_smbwonder_zim\   ← the existing Archipelago Manual world (Python apworld)
│                             we'll convert this to an integrated apworld
│                             once the Switch mod talks to a host service
└── switch-mod\             ← the Switch subsdk (forked from fruityloops1/wondar)
    ├── CMakeLists.txt        — modified: -fpermissive, symlink shim for include/
    ├── cmake\toolchain.cmake — devkitA64 cross-compile
    ├── include\, lib\        — wondar's vendored headers + sead/imgui/NN SDK submodules
    └── src\
        ├── program\
        │   ├── main.cpp          ← all our hook installs and callbacks live here
        │   ├── util\Log.hpp      ← ported from smo_archipelago — svcOutputDebugString
        │   ├── util\Log.cpp        sink, level prefixes, no thread_local
        │   ├── util\TargetActorProbe.{hpp,cpp}  ← legacy probe, currently a stub
        │   └── pe\               — wondar's debug UI (mostly disabled in our build)
        └── lib\                  — wondar's inlined exlaunch source
```

Original wondar (upstream): https://github.com/fruityloops1/wondar
Our fork: https://github.com/mdietz94/wondar (uncommitted local changes on top)
The plan document the project started from: `C:\Users\maxwe\.claude\plans\rustling-strolling-marble.md`

## Build + deploy (the daily dev loop)

```pwsh
$env:DEVKITPRO = "C:\devkitPro"
$env:PATH = "C:\devkitPro\msys2\usr\bin;" + $env:PATH

# build
& "C:\Program Files\CMake\bin\cmake.exe" --build `
    "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod\build"

# deploy
$dst = "$env:APPDATA\Ryujinx\mods\contents\010015100b514000\smbwap\exefs"
Copy-Item "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod\build\subsdk9" `
          -Destination $dst -Force
Copy-Item "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod\build\subsdk9.npdm" `
          -Destination "$dst\main.npdm" -Force
```

**First-time configure** (only needed once, or after blowing away `build/`):

```pwsh
& "C:\Program Files\CMake\bin\cmake.exe" `
    -S "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod" `
    -B "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod\build" `
    -G Ninja `
    -DCMAKE_TOOLCHAIN_FILE="C:/Users/maxwe/Documents/smwonder_archipelago/switch-mod/cmake/toolchain.cmake"
```

If the build fails with a *symlink* error (`sead/container/seadPtrArray.h: No such file or directory`), the CMake-side shim that materializes the broken POSIX symlinks didn't run. Re-run cmake configure; the shim copies `lib/sead/include`, `lib/NintendoSDK/include/{nn,nvn,vapours}` into `build/symlink-shims/` and adds it to the include path. See `CMakeLists.txt` around the `_SYMLINK_SHIM_DIR` block.

## Tailing the in-game log

Ryujinx writes `svcOutputDebugString` to its file logs at:

```
C:\Users\maxwe\Desktop\Switch\ryujinx-1.3.3\Logs\Ryujinx_1.3.3_<timestamp>.log
```

Our log lines are prefixed `[smbwap inf]` / `[smbwap dbg]` etc. and tagged `KernelSvc OutputDebugString` by Ryujinx. To filter live during play:

```pwsh
$latest = Get-ChildItem "C:\Users\maxwe\Desktop\Switch\ryujinx-1.3.3\Logs\Ryujinx_*.log" |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content -Wait $latest.FullName | Select-String '\[smbwap'
```

Logs are written with embedded NUL bytes (Ryujinx quirk) so for offline parsing, strip them first:

```bash
tr -d '\0' < "<log>" > /tmp/log_clean.txt
grep '\[smbwap' /tmp/log_clean.txt
```

## Game version + dump artifacts

- **Target version: SMBW v1.0.0** (BID `CD6E42AEE7934F4D`). Internal codename: `Secred.nss`.
- **NSP** at `C:\Users\maxwe\Desktop\Roms\Switch\Super Mario Bros. Wonder\Super Mario Bros. Wonder [010015100B514000][v0][Base].nsp`.
- **Extracted `main.nso`** at `C:\Users\maxwe\Desktop\Roms\Switch\Super Mario Bros. Wonder\main.nso` (26.7 MB).
- Title key (extracted from the bundled `.tik` at offset 0x180): `56c80b14d4923b7ece12c6c45e25e86b`.
- Tool used: `C:\Users\maxwe\Desktop\Switch\hactool.exe`. Keys at `C:\Users\maxwe\.switch\prod.keys`.
- **Do not apply the v1.0.1 update** in Ryujinx — every offset in our mod is pinned to v1.0.0.

## The two working hooks

Both live in `src\program\main.cpp`. NSO base when loaded is `0x7100000000`.

### 1. `NerveActivateOnce` at NSO `+0x559f7c`

Traps `FUN_7100559f7c` — a shared "one-shot Nerve activate" helper with 19 xrefs. Used by many Nerve classes' execute slots (`if (flag == 0) FUN_7100559f7c(this);`). At callback entry, we read `*nerve` (the vtable pointer) and compute its NSO offset (= `vtable - GetTargetStart()`) for filtering by class identity.

**Currently filtered**:

| `vt_off` | Event | Status |
| --- | --- | --- |
| `0x3345728` | **Wonder Seed pickup** (fires WONDER_SEED_AWARDED) | ✅ |
| `0x33fd690` | RequestEventCourseExitByAreaTag (looked promising; never fires on flag touch) | inactive |
| `0x3345bc0`, `0x3345cf8`, `0x3345e30` | RequestEventGoal{Base, GateFinish, TreasureChest} (passive registrations; never fire on touch) | inactive |

**Other vtables observed firing during gameplay** (kept for reference, not target-filtered):

| `vt_off` | What it fires on | Notes |
| --- | --- | --- |
| `0x33fd9a8` | Mario death; ~50ms after Wonder Seed grab; world map travel | "scene transition" |
| `0x33fd870` | Damage; power-up pickup (sometimes) | "player state animation" |
| `0x3346330` | Wonder Flower touched (Wonder phase start) | useful for `wonder_flower_rando` AP opt-in |
| `0x33fd4c8` | Menu exit (quit-to-map) | distinct from level-clear path |
| `0x33fd738` | World map travel (W1→W2 transition) | overworld nav event |

### 2. `SetCourseClearFlagExecute` at NSO `+0x1bf28cc`

Direct trampoline on `FUN_7101bf28cc`, which is slot 8 (execute) of the **`SetCourseClearFlagToGameData` Nerve** (vtable at NSO `+0x34b14e8`).

The function name says it all: it writes the "course cleared" flag to GameData (save data). Body shape:

```c
bl FUN_710059f894          ; open GameData accessor
mov w0, #0xdf82e9ab          ; hash of save-data field name (probably "is_clear" or similar)
bl FUN_71003d3fb0            ; write field by hash
bl FUN_71005e93fc            ; check success
tbz w0, #0x0, fail_path
```

Validated negative:
- Menu quit: silent (different nerve, `vt_off=0x33fd4c8`).
- Death + game over: silent.
- World map travel: silent.

Validated positive: fires exactly once on flag-touch level clear.

**Important nuance**: this covers all *valid* clear types (Normal Exit, Secret Exit, Fake Exit, Top-of-Flag, and likely palace boss clears too). It does NOT, by itself, *distinguish* between them. The 199 Goal-family AP checks split into 4 sub-types in the manual:

| Sub-type | AP checks | Distinguisher TBD |
| --- | --: | --- |
| Normal Exit | 96 | (default, no special property) |
| Top of Flag | 89 | read Mario's Y at clear time |
| Secret Exit | 9 | read the goal pole actor's exit-id field |
| Fake Exit | 5 | same — fake-flag flag |

For M1's feasibility check we just needed "any valid clear" to fire reliably. For M2 splitting comes next — we read additional state at the hook callsite.

## M2.4 working pattern: SetEventId + IPC SaveReport

Real-hardware compatible payload extraction. Two hooks total, both `InstallAtSymbol`.

### Hook A — `nn::prepo::PlayReport::SetEventId`

Symbol: `_ZN2nn5prepo10PlayReport10SetEventIdEPKc` (sdk +0x3a81a0).
Signature: `Result SetEventId(this, const char* event_id)`.

Logs: `prepo.set_event this=<ptr> event=<room_name>`.

Fires whenever the game sets the room name on a PlayReport (with-event-id ctor calls this internally; the no-arg ctor leaves it for the game to call after). The hook on the with-event-id ctor (`_ZN2nn5prepo10PlayReportC2EPKc`) *also* installed; harmless duplication — in practice the game uses the no-arg ctor + SetEventId path, so the ctor hook rarely fires.

### Hook B — `CmifProxyImpl<IPrepoService>::_nn_sf_sync_SaveReport{,WithUser}`

Symbols (long; verbatim from sdk.sym):
```
_ZN2nn2sf4cmif6client6detail13CmifProxyImpl...E22_nn_sf_sync_SaveReportERKNS0_7InArrayIcEERKNS0_8InBufferEm
                                                  (sdk +0x3a9f8c)
_ZN2nn2sf4cmif6client6detail13CmifProxyImpl...E30_nn_sf_sync_SaveReportWithUserERKNS_7account3UidERKNS0_7InArrayIcEERKNS0_8InBufferEm
                                                  (sdk +0x3a9fac)
```

Signatures (after `this`):
- `SaveReport(InArray<char>& room, InBuffer& payload, ulong flags)`
- `SaveReportWithUser(Uid& uid, InArray<char>& room, InBuffer& payload, ulong flags)`

`InArray<char>` and `InBuffer` are 16-byte `{ptr, size}` structs passed by const reference. `Uid` is a 16-byte `{u64[2]}` also by const reference. The Add-overload Old/Old2 variants exist (`SaveReportOld`, `SaveReportOld2`, `SaveReportWithUserOld`, `SaveReportWithUserOld2`) but aren't used by the current build — observed `prepo.set_event` always pairs with one of the two non-Old methods. If a session shows `set_event` without a paired IPC line, add the Old variants.

Logs (per call):
```
prepo.ipc.save     this=0x.. room=<name> pay=0x.. size=<n> flags=0x..
prepo.ipc.save_uid this=0x.. uid=0x.. room=<name> pay=0x.. size=<n> flags=0x..
prepo.ipc.bytes(64/<n>): <hex>...
```

The hex line is the first 64 bytes of the serialized payload. Decoded shape is the PlayReport CBOR-ish format documented below.

### What we did NOT hook on PlayReport (and why)

Hooking *any* of the PlayReport class member functions besides ctor and SetEventId triggered a delayed guest abort:

| Hooks installed | Abort thread | Abort site | Time after last save |
|---|---|---|---|
| All 10 (ctor, Save × 2, Add × 5, Struct::Add × 2) | `ModuleSystemWorker1` | `nn::account::ShowUserCreator` (sdk +0x198284) | ~6 s |
| ctor + SetEventId + Save + Save(Uid&) | `gmd::SaveDataMgr` | nnSdk +0xa4264 | ~5 s |
| ctor + SetEventId only | (none — clean) | — | — |
| ctor + SetEventId + IPC SaveReport{,WithUser} | (none — clean) | — | — |

The crash relocates to whichever validator subsystem notices the inconsistency first. The IPC client layer below the PlayReport class is below those validators — that's why hooking *there* is safe even though hooking the public API isn't.

### PlayReport payload format

Reverse-engineered 2026-05-20 from three full payloads (world_activity / world_result / course_result, all from W1-1). Decoder is in [bridge/play_report.py](bridge/play_report.py); 44 tests pass including end-to-end decoding of all three live captures.

```
Header (3 bytes):
    0xDE                      magic
    u16 BE                    entry count

Body: `entry_count` flat (key, value) pairs.

Opcodes (LIVE = observed in captured bytes; otherwise GUESSED):
    0x00..0x7F                inline uint 0..127             [LIVE]
                              (Nintendo extends CBOR's 0..23 range —
                              0x18..0x1B are NOT reserved here)
    0x80..0x8F                open struct: N=op&0xF entries
                              follow as (key, value) pairs   [LIVE]
    0x90..0x9F                open array: N=op&0xF values    [LIVE]
    0xA0..0xBF                short text string, len = op & 0x1F  [LIVE]
    0xC2                      false                          [LIVE]
    0xC3                      true                           [LIVE]
    0xCC + u8                 uint 128..255                  [LIVE]
    0xCD + u16 BE             uint 256..65535                [GUESSED]
    0xCE + u32 BE             uint                           [LIVE]
    0xCF + u64 BE             uint                           [GUESSED]
    0xD0 + s8                 signed int -128..127           [GUESSED]
    0xD1 + s16 BE             signed int -32768..32767       [GUESSED]
    0xD2 + s32 BE             signed int                     [LIVE — W1-2
                              stage_key = 232160011]
    0xD3 + s64 BE             signed int                     [LIVE — W1-1
                              stage_key = 2937190396 = 0xAF11F7FC, doesn't
                              fit positive s32 so encoder picks s64]
                              Used by Struct::Add(long) — encoder picks
                              smallest signed width that fits.
                              (Top-level PlayReport::Add uses the unsigned
                              0xCC..0xCF path instead.)
    0xD7 + u8 + u64 BE        Any64BitId: 1-byte TypeCode + 8-byte u64 Value;
                              decoded as
                              {"TypeCode": int, "Value": int} [LIVE]
    0xD9 + u8 + N chars       medium text string, 0..255 ch  [LIVE]
    0xFF                      literal -1                     [LIVE]

Unmapped (no live capture — decoder raises DecodeError):
    - floats (single / double / half)
    - negatives other than -1 (maybe inline range in 0xE0..0xFE?)
    - structs / arrays with >15 entries (extension opcode TBD)
    - strings >255 chars (likely 0xD8 + u16 + chars)
```

Important nuances:
- 0x80-0x8F (structs) vs 0x90-0x9F (arrays) are explicitly different opener ranges — the decoder picks dict vs list by the opener nibble, not by peeking at children.
- The encoder uses different opcodes for the same magnitude depending on which `Add` overload was called: top-level `PlayReport::Add(long)` minimizes *unsigned* width (`cc`/`ce`); `Struct::Add(long)` minimizes *signed* width (`d0`/`d1`/`d2`/`d3`). Compare W1-1 stage_key=2937190396 (high bit set as s32 → bumps to s64 → `d3`) vs W1-2 stage_key=232160011 (fits positive s32 → `d2`). Both decode to plain Python ints.
- `arena_score_enter = 4294967295` and `last_put_panel_id = -1` are both "all-ones" semantically but encode differently — the former is `ce ff ff ff ff` (genuine u32 max), the latter is `ff` (the -1 short form). The C++ caller's signedness flows through.

Test fixtures and assertion sets live in [bridge/test_play_report.py](bridge/test_play_report.py). Iterate by playing through new scenarios (secret exit, palace clear, item pickup), pasting the new `prepo.ipc.bytes(...)` lines into a fixture, and adding assertions.

### Known room names so far (corpus grows organically)

| Room name | When it fires | Notable fields |
|---|---|---|
| `bootup_time` | application launch | `BootupTimeUs` |
| `erepo_region` | boot | `Region`, `Language`, `StandardTimeName`, `UtcOffsetSeconds` |
| `erepo_time`, `erepo_playstyle`, `erepo_network_status`, `erepo_active_beacon` | boot | SDK telemetry, skip |
| `game_option` | settings change / boot finalize | `savedata_id`, `play_mode`, `scene_type`, control-type arrays |
| `world_activity` | world-map activity update | `stage_info.{stage_key, world_no}`, `wonder_seed`, `wonder_coin` |
| `world_result` | world-map → course transition | `stage_info` (source), **`next_stage_info.{stage_key, course_id, stage_type, world_no, world_kind}`** (destination) |
| `course_in` | course actually loading | **`stage_info.{stage_key, world_no, course_no}`**, `local_player_rest`, `lucky_coin`, `world_wonder_flower`, `equip_badge_id[]` |
| **`course_result`** | **course CLEARED — fires ~8 ms after M1 `COURSE_CLEARED` nerve** | **`stage_info.{stage_key, world_no, course_no}` identifies the cleared course; `course_result` (1=clear), `touch_goal_top_{enter,result}` (bool, Top-of-Flag distinguisher), `goal_id`, `badge_id_array`, `total_get_finish_seed_count`, all flower-coin / yellow-coin counts** |
| `koopajr_result` | palace boss clear (from pre-M2.4 RE; not yet captured live) | `stage_info`, `battle_result`, `badge_id_array`, `koopajr_total_time` |

The `course_result` discovery (2026-05-20) closes the M2.5 distinguisher question: every clear-state field we need (Top of Flag, goal identity, coin counts, badges held) is in the payload. See [bridge/test_play_report.py](bridge/test_play_report.py) `COURSE_RESULT` fixture — 1577 bytes, 57 fields, decoded end-to-end and asserted against Ryujinx's reference output.

## What didn't work (don't repeat these)

Saving these so the next attempt doesn't burn time re-deriving them.

1. **Hooking ActorBase::ctor at NSO `+0x231204`** (wondar's existing offset). Fires only 3-5 times early in boot for *system* actors. Game-level actors (Mario, enemies, items, goal poles) construct through a different path we never identified. The `s_ActorList` in wondar's ActorBrowser is *not* populated by Goal/WonderSeed instances.
2. **Hash-based actor-class registry path through `FUN_7100362920`**. Too many indirection layers (class registry → per-class instance pool → instance descriptor with `+0x178` index field → ??? → actor). After 30 min of decompilation, never reached a vtable. Abandoned.
3. **Hooking `FUN_7101562fb4`** (Wonder Seed Nerve's slot-8 execute) directly. Crashed level loads ~2.4s after first fire. Likely a trampoline relocation issue (some instruction pattern in its first 16 bytes that exlaunch's And64InlineHook doesn't handle). **Avoid hooking Wonder Seed slot 8 directly.** Use the shared helper at `FUN_7100559f7c` and filter by vtable — that's what works.
4. **Hooking `FUN_7100299488`** (the per-actor level-load dispatcher with the hardcoded `"ObjectGoalPole"` check). Fires at level *load*, not on touch — it's a level-init registration handler, not an event dispatcher. The post-match callees `FUN_7100299738` (sead::ActorReference ctor) and `FUN_7100299800` (ActorReference swap) just stash a weak-ref to the goal actor for later use.
5. **String-search → vtable approach for `RequestEventGetFinishWonderSeed`** (NSO offset `0x33457b8`). The vtable exists, the class exists, but the Nerve *never activates during gameplay*. It's a passive registration that the engine has wired but never ticks. The actual Wonder Seed pickup goes through a sibling Nerve at `0x3345728` whose name we never resolved (slot 0 tail-calls into `FUN_71015636cc`; never confirmed what name it returns). Naming was a red herring — go by empirical firing, not by string.
6. **Hooking the 4 `RequestEventGoal*` and `RequestEventCourseExitByAreaTag` Nerves**. All passive registrations. None fire on flag-touch despite their slot-8 execute methods being in `FUN_7100559f7c`'s xref list. The goal-clear path bypasses Nerve tick entirely and writes save data directly through `SetCourseClearFlagToGameData`.
7. **Hooking `nn::prepo::PlayReport::Save()`, `Save(Uid&)`, or any `Add(...)` overload**. Installs cleanly, fires correctly, but triggers a delayed guest abort 5-6 seconds later on a *different SDK thread* (ModuleSystemWorker1, then gmd::SaveDataMgr depending on which validator notices). The SDK has cross-subsystem audits that detect PlayReport-state inconsistency introduced by the trampoline; the abort surfaces wherever the next prepo-touching subsystem runs. **Workaround**: drop below the PlayReport class to the IPC client (`CmifProxyImpl<IPrepoService>::_nn_sf_sync_SaveReport{,WithUser}`) which sees the already-serialized payload. See "M2.4 working pattern" above.

## Critical engine knowledge

Nintendo's Nerve system in SMBW has **two distinct flavors** that need different hook strategies:

- **Active Nerves**: tick every frame while live. Execute method has shape `if (flag == 0) FUN_7100559f7c(this); FUN_7100005390(this+0x68);`. The first call does the one-shot work, the second advances state. Examples: Wonder Seed pickup, Wonder phase start, scene transitions, damage/power-up animations. **Hook strategy**: trampoline on `FUN_7100559f7c`, filter by vtable.
- **One-shot dispatch Nerves**: their class exists but `execute` is called explicitly by other code at a specific moment (not per-frame). Execute method does its work inline. Examples: `SetCourseClearFlagToGameData`. **Hook strategy**: trampoline directly on the execute function. Validate by checking the prologue is clean (no `adrp`/`ldr literal` in first 5 instructions, since `And64InlineHook` has a fixed-size relocation buffer).

Recipe to find any new Nerve hook target:

1. Search Ghidra strings for the event name (e.g., `RequestEventXxx`, `SetXxx`, `GetXxx`).
2. Find the getter that returns the string. The getter's address appears as **slot 0** of a vtable somewhere in `0x710334XXXX` or `0x71034BXXXX` (the Nerve vtable region) or `0x71033fXXXX` (a different family).
3. Read the vtable layout. Shared base method slots are `LAB_71014498**` and `LAB_7101e078e4`. Event-specific overrides are slots 7, 8, and a few high ones.
4. **Slot 8 is execute.** Check whether slot 8's address is in `FUN_7100559f7c`'s 19-entry xref list:
   - **Yes** → it's an active Nerve. Use `NerveActivateOnce` and add the vtable offset to the filter.
   - **No** → it's a one-shot. Peek the execute function's prologue. If clean, hook directly.

Address space layout in the loaded NSO:

- Base: `0x7100000000` (loaded by Ryujinx; `exl::util::modules::GetTargetStart()` returns this).
- `.text` code: roughly `0x7100000000`–`0x7102800000`.
- `.rodata` strings: `0x71028XXXXX`–`0x71029XXXXX` typically.
- Vtables: `0x710334XXXX`, `0x71033fXXXX`, `0x71034BXXXX` (Nerve regions we've seen).
- Itanium-style typeinfo: `0x71000ac930` is a function that appears in every Nerve vtable's `-8` slot (probably a generic destructor or dispatch helper, not std::type_info).

## Tools used

- **Ghidra 11.3 or 11.4** + **Adubbz Switch Loader 1.7.0** (`File → Install Extensions`).
- **JDK 21** required by Ghidra 11.x.
- `wondar\syms\100\sdk.sym` is the NN SDK symbol map. Apply it as Ghidra labels via a Jython script (~20 lines: parse each `name = __main_start + 0xOFFSET;` line and apply to base + offset). Hugely speeds up navigation once `nn::`, `sead::`, etc. names appear in the listing.

## Things to test next session (priority order)

1. ✅ ~~Boss clear test~~ — done post-M1. `COURSE_CLEARED` fires on Royal Seed palace clears. +7 AP checks for free.
2. ✅ ~~Wire PlayReport hooks~~ — done. Initial naive approach (all 10 class-member hooks) crashed; final pattern is ctor + SetEventId + IPC SaveReport{,WithUser}. See "M2.4 working pattern".
3. **Expand the room-name corpus**: play through a normal level clear, a secret exit (W1-2 Piranha Plants on Parade), and a palace clear (Pipe-Rock Plateau Palace). Each adds one room name + field map to the M2.4 spec.
4. **Build the Python decoder** (~50 LoC, CBOR-ish format documented above). Test against the captured `course_in` 355-byte payload — we have it in the 2026-05-20 20:52 log if needed.
5. **10-coin Nerve hunt**: search Ghidra strings for `RequestEventGetBigTenLuckyCoin`, `BigTenLuckyCoin`, `TenCoin`. Find vtable, identify hook approach (shared helper vs direct execute). 305 AP checks, biggest remaining bucket.
6. **Goal exit-type distinguisher**: confirm whether `course_in` or the (yet-unobserved) course-clear report carries `exit_type` / `goal_kind`. If so, M2.5 is solved for free. If not, fall back to dumping `nerve+0x40/+0x68/+0x90` at `SetCourseClearFlagExecute` callback entry.
7. **LAN socket**: re-enable the commented `nn::socket::Initialize` block in `main.cpp` and add a simple TCP outbound to your PC. SMO's pattern is in `smo_archipelago\switch-mod\src\ap\ApClient.cpp`. This is the moment the captured IPC bytes ship over the wire to the Python bridge.

See `milestones.md` for the full M2+ plan.

## State of the codebase

`switch-mod/` is a fork of `mdietz94/wondar` (its own git repo). Local diffs from upstream now living on top:

- `CMakeLists.txt`: `-fpermissive` (libstdc++15 `std::construct_at` const fix from smo_archipelago); symlink-shim block to materialize broken POSIX symlinks on Windows checkouts.
- `src/program/main.cpp`: hooks added — `NerveActivateOnce`, `SetCourseClearFlagExecute` (M1); `PlayReportCtor`, `PlayReportSetEventId`, `PrepoIpcSaveReport`, `PrepoIpcSaveReportWithUser` (M2.4). Crashy hooks left as definitions only, install lines commented out with explanation (PlayReport::Save{,Uid&}, all PlayReport::Add overloads, Struct::Add overloads). `nvnImGui` install disabled; wondar's hardcoded `RwPages` SDK patch at `+0x399790` disabled.
- `src/program/util/Log.hpp`/`Log.cpp` (new): smbwap kernel-debug logger, ported from smo_archipelago.
- `src/program/util/TargetActorProbe.hpp`/`TargetActorProbe.cpp` (new): legacy actor-vtable runtime-discovery probe, currently a stub.
- `src/program/pe/DbgGui/Windows/ActorBrowser.cpp`: lightly modified to call into the probe stub.

The outer repo (`smwonder_archipelago/`) is a separate git repo holding `docs/`, `manual_smbwonder_zim/`, and `CLAUDE.md`. `switch-mod/` is excluded via outer `.gitignore` for now — it's tracked by its own repo and may be promoted to a submodule once pushed.

Decide before upstreaming: the symlink-shim and `-fpermissive` fixes are general Windows-build-fixes worth a PR to wondar; the prepo hooks are SMBW-specific and stay private.
