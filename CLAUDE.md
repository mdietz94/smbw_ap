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
│   └── milestones.md               M1-M7 roadmap
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
- Apply [switch-mod/syms/100/sdk.sym](switch-mod/syms/100/sdk.sym) as Ghidra labels via a ~20-line Jython script that parses each `name = __main_start + 0xOFFSET;` line. Massive nav speedup once `nn::`, `sead::`, etc. names appear in the listing.

## What's done, what's next

**M1 (✅ done)**: Two hooks proven end-to-end:
- `WONDER_SEED_AWARDED` — every Wonder Seed grab (124 AP checks).
- `COURSE_CLEARED` — every successful flagpole touch + every palace boss clear (206 AP checks lumped; exit-type splitting is M2.5).
- Total: 330 / 663 AP checks covered (49.8%).

**M2.4 (✅ Switch-side done, decoder pending)**: PlayReport payload capture via the IPC-layer pattern (see above). Room name corpus + per-event field map grows organically as the user plays through more content.

**Next** (per [docs/milestones.md](docs/milestones.md) section M2/M3/M4):
- Expand M2.4 room-name corpus (one level clear + one palace clear + one secret exit).
- Write the Python decoder for the CBOR-ish PlayReport format.
- M2.2: 10-coin Nerve hunt (305 AP checks, biggest remaining bucket).
- M4: LAN socket + Python bridge — the moment the captured IPC bytes ship over the wire to a real consumer.

## Reference: sister project

The user's existing `smo_archipelago` project (Super Mario Odyssey integration; same architecture pattern) lives at `C:\Users\maxwe\Documents\smo_archipelago\`. Mirror its `switch-mod/src/ap/ApClient.cpp` when wiring up the LAN socket; mirror its `apworld/` and `scripts/` layout when building the Python bridge.
