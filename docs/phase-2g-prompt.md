# Phase 2g kickoff prompt

Paste this verbatim into a fresh Claude Code session in this worktree. The prompt is self-contained — the agent reads memory + the codebase, doesn't need session history.

---

You are working on `smwonder_archipelago` — an Archipelago multiworld integration for Super Mario Bros. Wonder (SMBW v1.0.0) on modded Switch + Ryujinx. Project orientation in `CLAUDE.md`.

**Your task: Phase 2g of the hakkun migration.** Restore the M3 grant primitives in the `probe::` namespace so AP items received from the bridge actually write to live SMBW game state. Currently (post-Phase 2f, PR #61) the bridge connects + outbound flows work but inbound grants land in no-op stubs at `switch-mod/src/probe/Stubs.cpp`. After Phase 2g, SMBW AP is fully functional on hakkun matching the M1-M4 behavior the project had on exlaunch.

**Critical context before you write code:**

1. **Read memory first.** Memory files `smbwap-hakkun-migration` (overall migration state + API cheat sheet) and `smbwap-phase-2g-handoff` (detailed Phase 2g brief: priority-ordered function list, signatures, NSO offsets, gotchas, definition-of-done) are the load-bearing context. They were written specifically for this session.
2. **Branch.** You're on or branching off `claude/hakkun-migration-phase-2f` (the head of the migration stack). Phase 2g stacks one more PR on top — same pattern as 2a/2b/2c/2d/2f. Don't merge to master yourself; the user merges via the GitHub PR cascade.
3. **Legacy reference.** `switch-mod/src/program/main.cpp` lines 38-870 and 890-1700+ contain the original `probe::` implementation (~2700 lines). Read it carefully — most of it ports cleanly with the API translations in `[[smbwap-hakkun-migration]]`, but several call sites use exlaunch primitives (`HOOK_DEFINE_TRAMPOLINE`, `exl::util::modules::GetTargetStart`, `nn::Result` from exlaunch's nn headers) that need translation to the hakkun equivalents listed in that memory.
4. **Build env.** PowerShell on Windows. Build cycle:
   ```pwsh
   $env:Path = 'C:\Program Files\LLVM\bin;C:\devkitPro\msys2\usr\bin;' + $env:Path
   & 'C:\Program Files\CMake\bin\cmake.exe' --build switch-mod\build
   ```
   Build artifacts: `switch-mod/build/exefs/{subsdk9, main.npdm}`. Deploy to Ryujinx via `Copy-Item ... -Destination "$env:APPDATA\Ryujinx\mods\contents\010015100b514000\smbwap\exefs\"` then launch with `Ryujinx.exe '<NSP path>'`. SD path for real hardware is `D:\atmosphere\contents\010015100B514000\exefs\` when the user has the SD mounted.
5. **Real-hw cycle.** ~15 min round-trip on the user's Switch (Atmosphere 1.11.1 + HATS-2026-05-11 + SMBW v1.0.0). Don't propose a real-hw test until Ryujinx confirms the grants apply end-to-end (badge shows up in inventory, Royal Seed unlocks world, etc.).
6. **Don't break Phase 2f.** Phase 2f shipped a working bridge connection + outbound flow. Phase 2g additions must not regress the build or the Ryujinx-confirmed dynamic-relocation resolution. Build with `-Wl,--unresolved-symbols=ignore-all` already in `switch-mod/CMakeLists.txt`; don't remove it.

**Phase 2g sub-phasing (recommended).** Don't try to land all 12 probe:: functions in one commit. Stack commits in priority order from `[[smbwap-phase-2g-handoff]]`:

* Commit 1: gates (markSaveLoaded + isSaveLoaded + isInSceneTransitionWindow). Easy, unblocks the apply flow.
* Commit 2: container-A counter (grantContainerACounter + incrementContainerACounter + the gmd::sInstance dereference helper at NSO +0x363F0F0).
* Commit 3: container-B bool (grantContainerBBool via FUN_710049EA24 wrapper at NSO +0x49EA24).
* Commit 4: container-C badge bitfield (setBadgeBitfieldAbsolute) — direct memory write to gmd+0x70..0x8c per CLAUDE.md "M3.2 badge-grant" section.
* Commit 5: per-course bitfield + WonderSeed override (setPerCourseBitfieldAbsolute via FUN_7101F2B354 at NSO +0x1F2B354; pushWonderSeedOverride writes the 5 mirror hashes; the GmdContainerAWriter callback gets the WS interceptor logic).
* Commit 6: synthKill + PlayerTickLatch (DeathLink). Optional in 2g — could defer.
* Commit 7: setContainerCBit + dumpSaveField (generics + diagnostic).

Test each commit in Ryujinx before stacking the next. Final commit triggers a single real-hw cycle on D:\.

**Each function port checklist:**

* Translate `HOOK_DEFINE_TRAMPOLINE(...) { static void Callback(...); }; void X::Callback(...) { ... Orig(...); }` to `HkTrampoline<R, Args...> xHook = hk::hook::trampoline([](Args... a){ ...; xHook.orig(a...); });` at namespace scope.
* Translate `X::InstallAtOffset(off)` → `xHook.installAtMainOffset(off)` and `X::InstallAtSymbol("...")` → `xHook.installAtSym<"...">()`. Symbol resolution is at install time via `hk::ro::lookupSymbol` because `HK_DISABLE_SAIL` is defined.
* Translate `exl::util::modules::GetTargetStart()` → `hk::ro::getMainModule()->range().start()`. The gmd::sInstance dereference: `auto* gmd = *reinterpret_cast<void**>(hk::ro::getMainModule()->range().start() + 0x363F0F0);`. Check for null.
* If a probe:: function needs to call into the legacy NSO offset directly (e.g. FUN_710049F648 for the container-A writer), the cleanest approach is to install a no-op `HkTrampoline` on that offset and call `.orig(...)` from the probe:: function. This shares the trampoline machinery with the existing Phase 2d hooks. Alternative: use `hk::ro::lookupSymbol("FUN_...")` if the symbol is exported (it isn't — these are anonymous game functions, so the trampoline approach is the right one).

**Definition of done:**

1. Bridge sends a `SetBadgesAbsolute(bits=0x10)` grant → game inventory shows Spring Feet badge owned within ~2 seconds.
2. Bridge sends a `SetRoyalSeedsAbsolute(mask=0x07)` → worlds 1-3 unlock in the world map.
3. Bridge sends a `GrantHashKeyed(hash=0xf4ee6827, value=99)` (flower coin) → coin counter increments on next save+reload.
4. `apworld/smbw_archipelago/` pytest suite still passes 536/1 (Python side untouched).
5. Real-hw: same flows work on Atmosphere 1.11.1.
6. No new crash reports.

**PR conventions:** title `M-Hakkun Phase 2g: restore probe:: grant primitives` (or per-commit titles if sub-phased). Stack on `claude/hakkun-migration-phase-2f`. Document the gmd::sInstance offset, the no-op trampoline pattern used for in-NSO callsite dispatch, and any new gotchas (race conditions during scene transitions, save-load deserialization ordering, etc.) so Phase 3 cleanup and any future contributor have the context.

Begin by reading the two memory files. Then read the relevant sections of `switch-mod/src/program/main.cpp` (the legacy probe:: namespace) — don't rewrite from scratch, port. Then start with the gates commit.
