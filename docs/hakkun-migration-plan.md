# Hakkun migration plan

**Status**: planned 2026-05-27, not started. Branch `claude/nervous-tesla-a8c99b` has defensive SD-log + hook-mask work that should land first as a separate PR, independently of this migration.

## TL;DR

`switch-mod/` is built on **exlaunch** (wondar's fork). Exlaunch's subsdk loader is non-functional on the user's current real-hardware setup (Atmosphere 1.11.1 + HATS-2026-05-11 + SMBW v1.0.0). Any exlaunch subsdk file present in the game's exefs override crashes the game at `nninitStartup → SetHeapSize+0x78` before any subsdk code can run. **Migrate to hakkun**, the framework `smo_archipelago` uses, which is validated on the same physical Switch.

See [smbwap_exlaunch_real_hw_broken.md](../C:\Users\maxwe\.claude\projects\C--Users-maxwe-Documents-smwonder-archipelago\memory\smbwap_exlaunch_real_hw_broken.md) (memory file) for the diagnostic that produced this conclusion — six identical crash reports across smbwap, bare wondar, IPS-patched variants, and NPDM bisects.

## What we know works

- **`smo_archipelago/switch-mod/`** on the same physical Switch hardware boots and runs reliably. It uses hakkun via the `sys/hakkun` submodule and `LibHakkun`-style trampolines.
- Hakkun's subsdk init mechanism is different enough from exlaunch's `DT_INIT` approach that it survives whatever HOS/Atmosphere ABI change broke exlaunch.
- The Switch toolchain on this dev machine (devkitA64 + CMake + Ninja + the symlink shim for Windows checkouts) is already configured.

## What's NOT proven yet

- Whether SMBW v1.0.0 + hakkun + this same Atmosphere works. SMO is a *different* application title, so it's possible — though unlikely — that something specific about SMBW's NPDM/SDK/whatever still rejects hakkun-style subsdks too. **Phase 1's test exists explicitly to rule this in or out before we burn time porting hooks.**

## Reference implementation

`C:\Users\maxwe\Documents\smo_archipelago\switch-mod\` — read these files as the model for every step below:

| Concern | smo file to read |
|---|---|
| Entry point + boot hook installation | `src/main.cpp` |
| Submodule layout, build system | `CMakeLists.txt`, `.gitmodules`, `sys/hakkun/CMakeLists.txt` |
| NPDM template | `config/npdm.json` |
| Single hook example (the right pattern) | `src/main.cpp` `gameSystemInitHook` |
| Trampoline-by-symbol example | `src/main.cpp` `gameSystemInitHook.installAtSym<...>()` |
| Trampoline-by-offset example | `src/hooks/CreditsStartHook.cpp` (`writeBranchLinkAtMainOffset`) |
| Logging + SD drain (proven pattern, port verbatim) | `src/util/Log.{hpp,cpp}` |
| Worker thread spawn from a frame hook | `src/main.cpp` `ApClient::instance().start(...)` |

## Phase 1: skeleton swap (no hooks)

**Goal**: prove hakkun's subsdk loader boots SMBW on real hardware. Zero functional hooks installed; the only "code path" exl_main exercises is hakkun's own initialization plus a trivial print confirming `hkMain` ran.

### Steps

1. New branch off master: `claude/hakkun-migration-phase-1`.
2. Add submodule `switch-mod/sys/hakkun` pointing at the same hakkun revision smo uses (read `smo_archipelago/.gitmodules` for the URL + pin). Initialize recursively.
3. Replace [switch-mod/CMakeLists.txt](../switch-mod/CMakeLists.txt) with a hakkun-shaped equivalent. Use `smo_archipelago/switch-mod/CMakeLists.txt` as the template. Keep our project name (`smbw_archipelago`), title ID (`0x010015100B514000`), and the Windows-symlink shim. Drop:
   - The `add_nso_target_subsdk` flow (hakkun has its own).
   - The exlaunch `-init=exl_module_init` linker flag.
   - The `EXL_PROGRAM_ID` / `EXL_LOAD_KIND` macros.
   - The `IMGUI_USER_CONFIG` macro (we're not bringing imgui into the hakkun build yet).
4. Replace [switch-mod/module/](../switch-mod/module/) with hakkun's module config:
   - Use smo's `config/npdm.json` verbatim as the starting point (it's known to boot on the same physical Switch).
   - Adjust title ID to `0x010015100B514000`.
   - Don't reintroduce the `system_resource_size` debate — start with smo's `0x0`, deviate only if smo's tests fail on SMBW.
5. Replace [switch-mod/src/program/main.cpp](../switch-mod/src/program/main.cpp) with a **minimal `hkMain`**:
   ```cpp
   #include "hk/hook/Trampoline.h"
   #include "hk/types.h"
   #include "util/Log.hpp"
   extern "C" void hkMain() {
       SMBWAP_LOG_INFO("=== smbwap hkMain START ===");
       // NO hooks yet. Phase 1 = boot only.
       SMBWAP_LOG_INFO("=== smbwap hkMain END ===");
   }
   ```
6. Port [switch-mod/src/program/util/Log.{hpp,cpp}](../switch-mod/src/program/util/Log.cpp) by **copying smo's exact Log.cpp** (drop the bridge-forwarding code path for now; keep ring buffer + `drainPendingToSd`). Replace `smoap` namespace + log prefixes with `smbwap`. Build flag `SMBWAP_DEBUG_SD_LOG=ON` enables the drain. This unblocks the entire diagnostic story because we'll finally have on-device boot logs.
7. Remove everything else under `switch-mod/src/`:
   - `src/lib/{hook,reloc,init,patch,...}` (exlaunch infra, replaced by hakkun).
   - `src/program/{ExceptionHandler*,imgui_nvn*,pe/*}` (wondar-specific bring-up we'll re-add only if needed).
   - `src/program/ap/*` — **keep on the branch but exclude from the Phase 1 build** by not adding them to `SOURCES_CXX`. We'll re-enable in Phase 2.
   - `src/program/{badge_table,coin_table,...}` — same: keep on disk, exclude from build.
8. CMake check: the build should produce `subsdk9` (or whatever name hakkun emits — match smo) + npdm. Verify with Ryujinx FIRST:
   - Deploy to `%APPDATA%\Ryujinx\mods\contents\010015100b514000\smbwap\exefs\`.
   - Launch SMBW.
   - Tail Ryujinx log for `[smbwap inf] === smbwap hkMain START ===` and `END`.
   - If those lines appear → hakkun boot works in Ryujinx → proceed to real-hw test.

### Phase 1 acceptance criteria (real hardware)

Run on the user's actual Switch (Atmosphere 1.11.1, HATS-2026-05-11):

- [ ] Game reaches title screen (i.e., past `nninitStartup`).
- [ ] `sd:/smbwap_boot.log` exists after boot and contains the `=== smbwap hkMain START ===` line. This is the **first time** we will have ever seen on-device log output — Phase 1's whole reason for being is to establish this signal.
- [ ] No new Atmosphere crash report.

### Phase 1 failure modes + decision tree

| Failure | Interpretation | Next action |
|---|---|---|
| Game crashes at `SetHeapSize+0x78` (identical to exlaunch) | Hakkun *also* doesn't work on this Atmosphere/SMBW combo. Something about SMBW v1.0.0 + this Atmosphere version rejects ANY subsdk. | **Stop migration.** Open issue on hakkun upstream + smo project. Consider: (a) waiting for Atmosphere or hakkun fix, (b) running on Ryujinx-only as a workaround, (c) investigating whether SMBW *update* (v1.0.1+) has different SDK behavior — though our code is pinned to 1.0.0 offsets. |
| Game crashes elsewhere (new PC, new fault) | Hakkun boots far enough to expose a different problem (likely our NPDM, our submodule pin, or a missing service). | Read the new crash report. Bisect what changed vs smo's working config. |
| Game boots but `smbwap_boot.log` doesn't appear | Hakkun runs but our `SMBWAP_DEBUG_SD_LOG` drain didn't fire. Could mean hkMain itself ran (good) but the drain hook (likely an `hk::hook::trampoline` on game framework init) wasn't ported. Re-check Log.cpp port. | Iterate on the drain trigger. The log content matters more than the boot itself for Phase 2. |
| Game boots AND log appears | **Phase 1 done.** Phase 2 next. |

## Phase 2: port hooks 1:1

**Goal**: every exlaunch hook gets a hakkun equivalent. Callback bodies stay byte-identical (they just access game memory; the framework around them changes). Functional parity with Ryujinx: course clears, Wonder Seeds, badge writes, PlayReport capture, the M3.3 Wonder Seed gate override, DeathLink, all of it.

### Hook inventory (from current `switch-mod/src/program/main.cpp::exl_main`)

| Group bit | Exlaunch hook | Hakkun port target |
|---|---|---|
| CORE_INIT | `CreateRootHeap::InstallAtOffset(0x005a66f8)` | `HkTrampoline<...>` with `.installAtMainOffset(0x005a66f8)` |
| CORE_INIT | `CreateFileDeviceMgr::InstallAtOffset(0x005a6110)` | same |
| CORE_INIT | `GameFrameworkInitialize::InstallAtOffset(0x005a5cfc)` | same; also where ApClient::start runs (mirror smo) |
| M1_EVENTS | `NerveActivateOnce::InstallAtOffset(0x00559f7c)` | same |
| M1_EVENTS | `SetCourseClearFlagExecute::InstallAtOffset(0x001bf28cc)` | same |
| M1_EVENTS | `GameGoalReachedExecute::InstallAtOffset(0x0015b77a8)` | same |
| PLAYREPORT | `PlayReportCtor::InstallAtSymbol("_ZN2nn5prepo10PlayReportC2EPKc")` | `.installAtSym<"_ZN2nn5prepo10PlayReportC2EPKc">()` |
| PLAYREPORT | `PlayReportSetEventId::InstallAtSymbol(...)` | same |
| PLAYREPORT | `PrepoIpcSaveReport[WithUser]::InstallAtSymbol(...)` | same |
| GRANTS | `GmdContainerAWriter::InstallAtOffset(0x0049f648)` | same |
| GRANTS | `GmdBoolWriter::InstallAtOffset(0x01f263fc)` | same |
| PROBES | `GmdC2BitReader`, `GmdContainerDWriter`, `SaveDeserializerHook`, `WorldUnlockCheck`, `SeedBitfieldRead`, `ContainerAReader`, `PlayerTickLatch` | same — these are observability hooks; port them but they can be gated behind a compile flag if size becomes a concern |
| FSHACKS | `InitActorPlacementInfo::InstallAtOffset(0x0005815c)` | same |
| FSHACKS | `pe::installFSHacks()` | port `pe/Hacks/FSHacks.cpp` to use `HkTrampoline.installAtSym`; same callback body |

### Steps

1. Restore `src/program/ap/*` to the build (was excluded in Phase 1).
2. For each `HOOK_DEFINE_TRAMPOLINE` in [switch-mod/src/program/main.cpp](../switch-mod/src/program/main.cpp):
   - Convert the macro-defined struct to an `HkTrampoline<R, Args...>` declared at namespace scope.
   - Keep the callback body identical (same `Orig()` calls, same logging, same enqueue calls).
   - Replace `::InstallAtOffset(off)` / `::InstallAtSymbol(sym)` calls in `exl_main` with `hook.installAtMainOffset(off)` / `hook.installAtSym<sym>()` in `hkMain`.
3. Preserve the existing `SMBWAP_HOOK_MASK` compile-time bitfield from [switch-mod/CMakeLists.txt](../switch-mod/CMakeLists.txt) — it's useful for bisecting any hakkun-side hook issues, and the per-group `if constexpr` blocks port cleanly.
4. Drop the wondar "just dont crash, idiot" SDK ret-patch (`*((u32*)(sdk+0x399790)) = 0xD65F03C0`) **for now**. If Phase 1 boots without it, we don't need it; if a later-stage crash appears that traces back to GPU error handling, port it to hakkun's `hk::hook::writeBytesAtSdkOffset` (or similar — read hakkun source). Document the decision.
5. Update `apworld/smbw_archipelago/client/` — should require zero changes since the wire protocol is unchanged. Re-run the existing 207-test pytest suite locally to confirm nothing in the Python side regressed.

### Phase 2 acceptance criteria

- Real hardware: boot to title screen, load save, run W1-1, collect at least one Wonder Seed, see it flow through to the AP client window (network + LAN bridge functional).
- Ryujinx: previous behaviors all still work (the regression suite). Specifically validate:
  - Wonder Seed pickup → AP location check.
  - Course clear → AP location check.
  - Badge received from AP → bitfield write visible in game.
  - Royal Seed received from AP → world unlock.
- 207 Python tests pass.
- `sd:/smbwap_boot.log` contains the full hkMain install trace.

### Phase 2 risks

- **Hakkun's `installAtSym<>` symbol-resolution timing may differ from exlaunch's.** Smo handles this by deferring sym-resolve hooks until after `GameSystem::init` returns. We may need a similar phasing for our PlayReport IPC hooks.
- **Threading model differs subtly.** Exlaunch's `Orig()` is a thread-local function pointer; hakkun's is a lambda capture. Reentry semantics in our `NerveActivateOnce` callback (which can fire concurrently across threads) should be re-audited.
- **`R_ABORT_UNLESS` on a missing symbol kills the subsdk hard.** Hakkun's behavior may differ. Audit every InstallAtSymbol → installAtSym conversion for what happens on miss.

## Phase 3: cleanup

**Goal**: remove dead exlaunch infrastructure, update docs, leave the repo in a state where the next contributor doesn't see two parallel hook frameworks.

### Steps

1. Delete `switch-mod/src/lib/{hook,reloc,init,patch,util/sys,diag,...}` — all exlaunch primitive code.
2. Delete `switch-mod/src/program/util/modules.hpp` (exlaunch helper, replaced by hakkun's module API).
3. Delete `switch-mod/module/subsdk9.{ld,specs.template,json.template}` if hakkun uses different conventions; otherwise migrate values.
4. Delete `switch-mod/syms/100/*.sym` for symbols we no longer reference (most can stay — they're documentation).
5. Update [CLAUDE.md](../CLAUDE.md):
   - Replace "wondar's exlaunch fork" references with "hakkun (mirrors smo_archipelago)".
   - Update "Two hook patterns established" section — the InstallAt{Offset,Symbol} examples need to match the new API.
   - Update "Daily dev loop" — the build command stays similar but binary paths may change.
   - Remove the "Critical gotchas → 5. R_ABORT_UNLESS in InstallAtSymbol" line if hakkun handles missing symbols differently.
6. Update [docs/handoff.md](handoff.md) — describe the migration in the project history section.
7. Update [docs/milestones.md](milestones.md) — add a milestone entry "M-Hakkun: framework migration, real-hw boot restored".
8. Update [.gitmodules](../.gitmodules) — remove inlined exlaunch refs, add hakkun.
9. Confirm `pip install -e apworld/smbw_archipelago/_setup` still works for the setup wizard (the build commands in the wizard are likely fine; just verify).

### Phase 3 acceptance criteria

- `grep -ri "exl_main\|EXL_ABORT\|exlaunch\|wondar" switch-mod/src/` returns nothing in code (only allowed in historical doc references).
- `git status` after `cmake --build` is clean.
- Full real-hw playthrough of at least one course works end-to-end.

## Cross-cutting risks + rollback

- **Sunk-cost trap**: if Phase 1 reveals hakkun also doesn't boot SMBW on this Atmosphere, do NOT proceed to Phase 2. The diagnostic was structured so Phase 1 is short specifically to avoid wasting more real-hw cycles.
- **Branch hygiene**: each phase = one PR. Phase 1 and 2 are individually shippable to Ryujinx-only users; only Phase 3 cleanup needs both prior phases merged.
- **Ryujinx regression**: Ryujinx has been our primary dev target throughout M1-M4. Every phase must preserve full Ryujinx functionality before being merged. The 207-test suite + manual W1-1 playthrough are the bar.

## Estimate

- Phase 1: 2-3 hours of focused work + 1 real-hw test.
- Phase 2: 3-5 hours + 2-3 real-hw tests.
- Phase 3: 1-2 hours + 1 final real-hw test.
- Total: ~6-10 hours over 4-6 real-hw test cycles.

---

# Kickoff prompt (self-contained for next agent / session)

You are working on `smwonder_archipelago` — an Archipelago multiworld integration for Super Mario Bros. Wonder (SMBW v1.0.0) on modded Switch + Ryujinx. The full project orientation is in `CLAUDE.md` at the repo root.

**Your task: Phase 1 of the hakkun migration described in [docs/hakkun-migration-plan.md](docs/hakkun-migration-plan.md).** Read that doc end-to-end first, then proceed.

**Critical context before you start writing code:**

1. The exlaunch framework currently in `switch-mod/` is **non-functional on the user's real Switch hardware** (Atmosphere 1.11.1 + HATS-2026-05-11 + SMBW v1.0.0). Six identical crash reports at `nninitStartup → SetHeapSize+0x78` triggered the migration. See memory file `smbwap_exlaunch_real_hw_broken.md` for the full diagnostic — it ruled out NPDM tweaks, the wondar "just dont crash" patch (applied via Atmosphere IPS at `sd:/atmosphere/exefs_patches/`), and content-level changes to our subsdk. The conclusion: the **mere presence** of any exlaunch subsdk file in the game's exefs override breaks boot. Don't waste time re-running variations of what's already been tested.
2. The reference implementation is `C:\Users\maxwe\Documents\smo_archipelago\switch-mod\` — same toolchain, same physical Switch, validated working. Read its main.cpp, CMakeLists.txt, .gitmodules, and config/npdm.json before authoring any hakkun code.
3. Build environment: PowerShell on Windows, devkitA64 at `C:\devkitPro`, CMake at `C:\Program Files\CMake\bin\cmake.exe`, Ninja generator. The current worktree is `C:\Users\maxwe\Documents\smwonder_archipelago\.claude\worktrees\nervous-tesla-a8c99b` — submodules are initialized. For the migration, **create a new branch** off master: `claude/hakkun-migration-phase-1`.
4. The user is patient but real-hardware test cycles are 15 minutes each (subsdk crashes during boot can require Atmosphere reinstall via Hekate-RCM). Do NOT propose a real-hw test until you have local Ryujinx confirmation that hkMain runs end-to-end.
5. Phase 1 success is **NOT** "hooks work" — it's literally "hakkun's subsdk loader boots SMBW and our hkMain logs a line to `sd:/smbwap_boot.log`". Resist scope creep into hook porting; that's Phase 2.

**Phase 1 deliverable**: a PR with subject "M-Hakkun Phase 1: skeleton swap (no hooks)" containing the minimal hakkun-based subsdk described in the plan. Drag-and-drop port of smo's `Log.{hpp,cpp}` for SD drain. The PR description must include:
- A Ryujinx log excerpt showing `[smbwap inf] === smbwap hkMain START ===` (proves hkMain ran).
- A statement of what Phase 1 acceptance test you ran (or want the user to run) on real hardware.

**Before opening the PR**, run the local test suite under `apworld/smbw_archipelago/`:
```pwsh
cd C:\Users\maxwe\Documents\smwonder_archipelago
python -m pytest apworld/smbw_archipelago/
```
All 207 tests should still pass (we're not touching the Python side in Phase 1).

If your Phase 1 build fails CMake configure or doesn't produce the right subsdk artifact, the most likely cause is a mismatch between hakkun's `add_nso_target_subsdk`-equivalent and our project name. Read `smo_archipelago/switch-mod/CMakeLists.txt` lines 1-50 carefully — the project name `smo_archipelago` is wired into several places we need to rename to `smbw_archipelago`.

Begin by reading the plan doc, then exploring `C:\Users\maxwe\Documents\smo_archipelago\switch-mod\` to understand the target structure. Do not start writing files until you can summarize back the differences between smo's CMakeLists.txt and our current one in 3-5 bullet points.
