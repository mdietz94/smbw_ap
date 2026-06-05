---
name: smbw-reverse-engineering
description: >-
  Reverse-engineer Super Mario Bros. Wonder (v1.0.0 NSO) and add/maintain Switch
  subsdk hooks. Use when finding a new hook target in Ghidra, identifying a Nerve
  or game function, installing a hakkun trampoline (NSO-offset or SDK-symbol),
  reading the GameDataMgr/save accessors, or debugging a hook crash. Covers the
  two hook patterns, the Nerve system, NSO address-space layout, the critical
  crash gotchas, Ghidra setup + the script inventory, and bundles the full RE
  decompile journal. Triggers: "find a hook", "reverse engineer", "which Nerve",
  "Ghidra", "hook crashes the game", "NSO offset", "trampoline".
---

# SMBW reverse engineering & hooking

Target: **SMBW v1.0.0**, BID `CD6E42AEE7934F4D`, codename `Secred.nss`. Extracted
NSO at `C:\Users\maxwe\Desktop\Roms\Switch\Super Mario Bros. Wonder\main.nso`
(hactool at `…\Desktop\Switch\hactool.exe`, keys at `~/.switch/prod.keys`).
**Never apply the v1.0.1 update** — every offset is pinned to v1.0.0.

Subsdk framework is **hakkun** (LLVM 19 + LibHakkun). Live source: `switch-mod/src/`
(`main.cpp` = hook installs; `probe/` = game primitives; `ap/` = LAN bridge;
`util/`). The retired exlaunch tree at `switch-mod/src/program/`, `src/lib/`,
`module/` is excluded from the build but kept on disk for reference. **CLAUDE.md's
old "Hook::InstallAtOffset/AtSymbol" wording is the exlaunch API — the live
hakkun equivalents are below.**

## Bundled reference (read for full detail)

- [`reference/smbw-re-map.md`](reference/smbw-re-map.md) — ★ **the current-state
  map. Read this first.** Present-tense ledger of every confirmed offset, hash
  key, container, struct field, hook, save offset, and ruled-out lead. Use it for
  *what is true now*; drop to the journal below only for *how we found it*.
- [`reference/static-analysis-findings.md`](reference/static-analysis-findings.md)
  — the master RE journal: every decompile round, the GameDataMgr API surface,
  the Murmur3 course-name hash, badge/seed sprints, ruled-out leads. Append-only
  and large (may contain superseded claims — defer to the map); grep it by date
  heading or `FUN_` address.
- The **smbw-save-data** skill owns the GameDataMgr grant API, container layout,
  and hash-key table (the *what to write*). This skill owns the *how to find &
  hook* it.

## The two hook patterns (hakkun)

All hooks are declared as file-scope `HkTrampoline` and installed in
`extern "C" void hkMain()` ([switch-mod/src/main.cpp](../../../switch-mod/src/main.cpp)).

```cpp
// Declaration — captures the original via .orig():
HkTrampoline<void, void*> myHook = hk::hook::trampoline([](void* nerve) {
    // ... read state at callback entry ...
    myHook.orig(nerve);            // call through to the real function
});
```

**Pattern 1 — NSO offset hook** (for RE'd game functions; addresses are
NSO-relative, base `0x7100000000`):

```cpp
installHook("NerveActivateOnce", 0x00559f7c,
            myHook.installAtMainOffset(0x00559f7c));
```

**Pattern 2 — SDK symbol hook** (mangled C++ symbol via `hk::ro::lookupSymbol`;
survives SDK rebuilds):

```cpp
installSymHook("PlayReportSetEventId",
    myHook.installAtSym<"_ZN2nn5prepo10PlayReport10SetEventIdEPKc">());
```

`installAtSym` returns a **failed Result** (not an abort) if the symbol is
missing — `installHook`/`installSymHook` log the failure and let the rest of
`hkMain` proceed. (Contrast the exlaunch `R_ABORT_UNLESS`, which killed the
subsdk before the game window opened.) NSO and SDK have **independent base
offsets** — `installAtMainOffset` for game code, `installAtSym` resolves both
transparently. The full Nintendo SDK symbol table is in
[switch-mod/syms/100/sdk.sym](../../../switch-mod/syms/100/sdk.sym).

## Critical gotchas — these crash the game (don't relearn)

1. **Never use `thread_local` in subsdk code.** No TLS allocator is registered
   before our code runs; `SetMemoryAllocatorForThreadLocal` aborts at module
   load. Signature: Result `0xCA8`, User Break, stack ends at that symbol. Use
   `static std::atomic<…>` + manual TID check.
2. **Inline hooks patch the first ~5 instructions (20 bytes).** Any PC-relative
   instruction (adrp, ldr-literal, b/bl) in those bytes corrupts the trampoline,
   with **delayed** symptoms. Verify the prologue is clean, or hook a shared
   inner helper and filter by caller identity (this is why we trap
   `FUN_7100559f7c` not the per-Nerve slot 8). hakkun's relocator is similar
   machinery — `installAtMainOffset` still wants a clean prologue.
3. **Hooking `nn::prepo::PlayReport` beyond ctor + SetEventId crashes the game.**
   Even no-op trampolines on `Save()`/`Add()` trigger a delayed abort 5–6 s later
   on a different SDK validator thread. Drop to the IPC client layer
   `CmifProxyImpl<IPrepoService>::_nn_sf_sync_SaveReport{,WithUser}`, which sees
   the already-serialized payload below the audit checkpoints.
4. **PlayReport uses the no-arg ctor + `SetEventId("room")`**, not the
   with-event-id ctor. Hook `SetEventId` to catch room names.
5. **Always import the whole `switch-mod/syms/100/` tree into Ghidra**, not just
   `sdk.sym`. The `gmd::GameDataMgr::sInstance` anchor sat unused in
   `syms/100/gmd/GameDataMgr.sym` for a whole sprint. Use
   [scripts/ghidra/import_sdk_symbols.py](../../../scripts/ghidra/import_sdk_symbols.py).

## Nerve system primer

Nintendo's Nerve system has two flavors with different hook strategies:

- **Active Nerves** tick every frame. Execute shape:
  `if (flag==0) FUN_7100559f7c(this); FUN_7100005390(this+0x68);`. **Hook
  strategy**: trampoline `FUN_7100559f7c` (NSO `+0x559f7c`), filter by `nerve[0]`
  (the vtable pointer); `vt_off = vtable - GetTargetStart()` identifies the class.
- **One-shot dispatch Nerves**: `execute` is called explicitly at a moment.
  Inline body. **Hook strategy**: trampoline directly on the execute function
  after verifying a clean prologue (e.g. `SetCourseClearFlagToGameData` at
  `+0x1bf28cc`, slot 8).

**Recipe for a new Nerve hook:**
1. Ghidra strings → the event name (`RequestEventXxx`, `SetXxx`, `GetXxx`).
2. Find the getter returning that string; its address is **vtable slot 0** in a
   Nerve vtable region (`0x710334XXXX` / `0x71033fXXXX` / `0x71034BXXXX`).
3. Read the vtable. Shared base slots `LAB_71014498**`, `LAB_7101e078e4`;
   event-specific overrides at slots 7, 8, high ones. **Slot 8 is execute.**
4. Is slot 8's address in `FUN_7100559f7c`'s 19-entry xref list?
   - **Yes** → active Nerve. Use the `NerveActivateOnce` filter; add the vtable
     offset (`kVtableOff_*` in main.cpp).
   - **No** → one-shot. Peek the execute prologue; if clean, hook directly.

The DeathLink hook is a worked example: vtable `0x33fd9a8` (SceneTransition)
fires on many events; the 64-bit discriminator at `nerve+0x18`
(`0x00ff000600000004` = Mario death vs `…0084` = controlled exit) distinguishes
them — see `kDeathDiscriminator_Val` in main.cpp.

## NSO address-space layout

- Base `0x7100000000`; `.text` ≈ `0x7100000000`–`0x7102800000`.
- `.rodata` strings: `0x71028XXXXX`–`0x71029XXXXX`.
- Nerve vtables: `0x710334XXXX`, `0x71033fXXXX`, `0x71034BXXXX`.
- `0x71000ac930` appears in every Nerve vtable's `-8` slot (generic
  destructor/dispatch helper, not std::type_info).
- Runtime live-heap mapping: on dev Ryujinx, `B_main = 0x08504000` maps live
  `0x0bxxxxxx` heap pointers ↔ Ghidra addresses (memory
  `[[reference_smbw_runtime_main_base]]`).

## Ghidra tooling

- **Ghidra 12.1** + **JDK 21**. The Adubbz Switch loader / overlay extension is
  **no longer used** — `main.nso` is loaded directly (base `0x7100000000`).
- One-time: run `import_sdk_symbols.py` (walks the whole `syms/100/` tree,
  ~27k named symbols incl. `gmd::GameDataMgr::sInstance @ +0x0363F0F0`).
- **Script inventory + run order** is in
  [scripts/ghidra/README.md](../../../scripts/ghidra/README.md) — sprint-2
  dataflow scripts (the ones that cracked the grant API), the Wonder-Seed
  gate-check scripts, the WS-persistence scripts, and the sprint-1 string-grep
  dead-end (don't repeat it).
- A Ghidra MCP bridge is configured (memory `[[reference_ghidra_mcp]]`); note the
  ARM64 hash-loading encoding gotcha (memory `[[reference_smbw_arm64_hash_loading]]`)
  — byte-searching for a 32-bit hash constant returns near-zero hits because the
  constant is built via `mov`/`movk` pairs, not stored as a literal.

## Hash function

`FUN_71003D4110` is **Murmur3-32 (seed 0)** over 81 hardcoded course-name
strings (identified by the constant signature `0xcc9e2d51 / 0x1b873593 /
0xe6546b64 / …`). The hash function for *field* names (flower_coin etc.) is still
unknown — Murmur3 of the obvious English names doesn't match; likely internal /
Japanese / precomputed. Not blocking: the verified hashes are tabulated in the
**smbw-save-data** skill.
