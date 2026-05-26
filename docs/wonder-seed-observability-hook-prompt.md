# Next-session handoff prompt — Wonder Seed gate observability hook

Copy-paste the prompt block below into a new session. It is
self-contained; the new agent will read the right docs from cold.

The plan is also encoded in this file (below the prompt) for the
agent and the human to reference together.

---

## Agent prompt (copy-paste into a new session)

```
I'm continuing the SMBW Archipelago "Wonder Seed gate" investigation
for the smwonder_archipelago project.  This session does ONE thing:
install observability-only switch-mod trampolines, deploy, capture
logs, analyze.  No gate hook, no state mutation.

Before doing anything else, read these in order (they have the full
context):

1. CLAUDE.md (project orientation; focus on
   - "Two hook patterns established"
   - "Critical gotchas" (especially #1 thread_local and #2 prologue
     PC-relative ops)
   - "GameDataMgr (gmd::) save-data API"
   - "Daily dev loop" (build + deploy commands)
2. docs/handoff.md (current state)
3. docs/static-analysis-findings.md — the entire section starting at
   "## 2026-05-26 — Wonder Seed gate-check RE (scaffold)" through
   "iteration 7 — Static-analysis DEAD END; pivot to runtime"
4. docs/wonder-seed-observability-hook-prompt.md (this file — the
   detailed plan is below the agent-prompt block; code template,
   exact NSO offsets, hook signatures, test protocol)

TL;DR of where we are:

After 7 iterations of static analysis we identified FUN_7100935ce0 @
NSO +0x935ce0 as the highest-likelihood candidate for the per-world
unlock gate.  It takes a world ID (uint32_t), returns a bool (low bit
of return), and is called from a hardcoded 8-call dispatch over world
IDs {1, 3, 4, 5, 6, 7, 2, 9} — matching the 8 AP Wonder Seed buckets
(W1, W2, W3, W4, W5, W6, Petal Isles, Special).  The per-world record
table that holds the queried hash at offset +0x68 is runtime-populated
(.bss), so STATIC analysis cannot determine what the function actually
checks.  We need RUNTIME observation.

Three hypotheses for what FUN_7100935ce0 checks (the log will
disambiguate):
- H1: It IS the gate (returns false → true when seed count crosses
  threshold).
- H2: It's a UI/static-progress reader (stable returns; fires only at
  world-map-load).
- H3: It's unrelated to the gate (rarely fires or fires only at
  startup).

Your task this session:

1. Install TWO HOOK_DEFINE_TRAMPOLINE observability hooks in
   switch-mod/src/program/main.cpp following the patterns already
   there (copy GmdContainerAWriter / GmdBoolWriter shape):

   a. WorldUnlockCheck on FUN_7100935ce0 @ NSO +0x935ce0
      Signature: uint64_t Callback(uint32_t world_id)
      Log every call with (world_id, return_value, isSaveLoaded).
      Prologue safety: ALREADY VERIFIED SAFE (see plan).

   b. SeedBitfieldRead on FUN_7100124134 @ NSO +0x124134
      Signature: uint64_t Callback(void* gmd, uint8_t* out, uint32_t hash, uint32_t bit_index)
      Filter: only log when hash == 0x60458608 (per-course Wonder
      Seed bitfield — confirmed in iteration 7).  ~55 unfiltered
      xrefs would spam logs.
      Capture *out AFTER calling Orig.

2. CRITICAL — Before installing the SeedBitfieldRead hook, verify
   prologue safety of FUN_7100124134 per CLAUDE.md gotcha #2.  Read
   the first 5 instructions; if any of adrp/adr/ldr-literal/b/bl
   appears, document the problem in this plan file and pick an
   alternative hook target (e.g., hook a specific caller instead).
   The exact disasm of FUN_7100935ce0's prologue is already pasted
   in this plan as verified-safe; the FUN_7100124134 prologue has
   NOT been verified yet.

3. Build the subsdk per "Daily dev loop" in CLAUDE.md.  Confirm it
   compiles cleanly.  Ask me to deploy + run Ryujinx; I'll provide
   the log output.

4. After I provide log output, append a section to
   docs/static-analysis-findings.md titled
   "## YYYY-MM-DD — Wonder Seed gate observability run" that:
   - Pastes a representative slice of the log
   - Identifies which hypothesis (H1/H2/H3) the data supports
   - Recommends the concrete next session work
     (e.g., "implement the seed-count gate hook on FUN_7100935ce0
      using the AP per-world counts; here's the dispatch table")
     OR (e.g., "FUN_7100935ce0 is not the gate; the gate is at FUN_X
      based on the caller PC captured during the gate attempt;
      next session investigates FUN_X")

HARD CONSTRAINTS:

- No state-mutating hooks this session.  Logging only.
- No thread_local (CLAUDE.md gotcha #1).
- Verify prologue safety BEFORE installing each trampoline
  (gotcha #2).
- Do not touch any other hooks or code paths.  Surgical additions
  only.
- If anything is unclear, ASK before guessing.  This session is
  cheap to redo with a clarification but expensive to redo if you
  build the wrong thing.
```

---

## Detailed plan (reference material for the agent and the human)

### Background

After 7 iterations of static analysis on SMBW NSO 1.0.0 — full RE log
in [docs/static-analysis-findings.md](static-analysis-findings.md)
under the 2026-05-26 sections — we converged on
**`FUN_7100935ce0`** @ NSO `+0x935ce0` as the highest-likelihood gate
candidate. The reasoning:

- It's a per-world boolean reader, returning true if a per-world flag
  is set in container A (byte) or container C (bit).
- It's called from a hardcoded 8-call dispatch over world IDs
  `{1, 3, 4, 5, 6, 7, 2, 9}` — matching the AP world's 8 buckets
  (W1, W2, W3, W4, W5, W6, Petal Isles, Special World).
- It has 0 direct function callers; only 8 orphan-code call sites.
  Hooking it has minimal collateral damage.
- The flag-hash at record `+0x68` is populated at runtime so static
  analysis can't read it. Runtime observation is needed.

The walker (cmp + threshold immediate + ldr + branch shape) produced
11 score=80 candidates, every one of which decompiled to non-gate
idioms (LSB-test, enum dispatch, saturation cap, state-machine,
vector-bounds check). The seed-count gate, if it exists in vanilla
SMBW, does NOT iterate or popcount the per-course Wonder Seed
bitfield `0x60458608` — only 2 functions in the binary load that
hash and neither does a threshold compare.

The most plausible remaining hypothesis is that gates are
**flag-based, not count-based**: a setter somewhere runs the count
comparison once (on seed pickup events) and sets a per-path "unlocked"
flag. `FUN_7100935ce0` reads that flag.

### Hook 1 — `WorldUnlockCheck` on `FUN_7100935ce0`

- NSO offset: `+0x935ce0`
- Signature (inferred): `uint64_t FUN_7100935ce0(uint32_t world_id)`
- Returns: low bit of the byte/bit queried for `world_id`. Callers
  use `tbnz w0, #0, ...` to branch on it.
- **Prologue safety: VERIFIED SAFE** (iteration 4 dump confirmed; no
  PC-relative ops in first 5 insns):
  ```
  +0x935ce0:  sub  sp, sp, #0x70
  +0x935ce4:  stp  x29, x30, [sp, #0x40]
  +0x935ce8:  stp  x22, x21, [sp, #0x50]
  +0x935cec:  stp  x20, x19, [sp, #0x60]
  +0x935cf0:  add  x29, sp, #0x40
  ```
- Direct callers: 0 fn + 8 orphan-code sites at
  `0x7100480ff8..0x710048104c` inside `FUN_7100480fd8`.

### Hook 2 — `SeedBitfieldRead` on `FUN_7100124134`

- NSO offset: `+0x124134`
- Signature (from M3.3b accessor docs):
  `uint64_t FUN_7100124134(void* gmd, uint8_t* out, uint32_t hash, uint32_t bit_index)`
- Returns: success flag (0/1). Bit value is written to `*out`.
- **Prologue safety: NOT YET VERIFIED.** The agent must read the
  first 5 instructions and check for PC-relative ops before
  installing. If unsafe, alternatives include:
  - Hook only the `FUN_710066e548` caller's `bl` site (one specific
    callsite, more invasive but localized).
  - Hook the deeper container-D internal reader (different offset).
- 55+ xrefs in the binary; **must filter on `hash == 0x60458608`** in
  the callback to avoid log spam.

### Code template

Place near the other `HOOK_DEFINE_TRAMPOLINE` definitions in
`switch-mod/src/program/main.cpp` (similar shape to
`GmdContainerAWriter` and `GmdBoolWriter`):

```cpp
// 2026-MM-DD: observability hook for the suspected per-world unlock
// gate.  Logs every call; never overrides.  See
// docs/wonder-seed-observability-hook-prompt.md for context.
HOOK_DEFINE_TRAMPOLINE(WorldUnlockCheck) {
    static uint64_t Callback(uint32_t world_id);
};
uint64_t WorldUnlockCheck::Callback(uint32_t world_id)
{
    uint64_t result = Orig(world_id);
    SMBWAP_LOG_INFO("WorldUnlockCheck(world_id=%u) -> %llu  saveLoaded=%d",
                    world_id,
                    static_cast<unsigned long long>(result),
                    probe::isSaveLoaded() ? 1 : 0);
    return result;
}

// 2026-MM-DD: observability hook on the per-bit container-D reader,
// filtered to the per-course Wonder Seed bitfield (0x60458608).
HOOK_DEFINE_TRAMPOLINE(SeedBitfieldRead) {
    static uint64_t Callback(void* gmd, uint8_t* out_bit,
                             uint32_t hash, uint32_t bit_index);
};
uint64_t SeedBitfieldRead::Callback(void* gmd, uint8_t* out_bit,
                                    uint32_t hash, uint32_t bit_index)
{
    uint64_t result = Orig(gmd, out_bit, hash, bit_index);
    if (hash == 0x60458608u) {
        SMBWAP_LOG_INFO("SeedBitfieldRead(hash=0x60458608, bit=%u) -> success=%llu value=%u",
                        bit_index,
                        static_cast<unsigned long long>(result),
                        out_bit ? *out_bit : 0xFFu);
    }
    return result;
}
```

In `exl_main()` (alongside the existing `::InstallAtOffset` calls):

```cpp
SMBWAP_LOG_INFO("smbwap: installing WorldUnlockCheck @ 0x935ce0");
WorldUnlockCheck::InstallAtOffset(0x935ce0);
SMBWAP_LOG_INFO("smbwap: installing SeedBitfieldRead @ 0x124134");
SeedBitfieldRead::InstallAtOffset(0x124134);
```

### Build + deploy

Lifted from CLAUDE.md "Daily dev loop":

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

Live log tail:

```pwsh
$latest = Get-ChildItem "C:\Users\maxwe\Desktop\Switch\ryujinx-1.3.3\Logs\Ryujinx_*.log" |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content -Wait $latest.FullName | Select-String '\[smbwap'
```

### Test protocol

1. Launch Ryujinx with the new build.
2. Start a fresh save OR load one where some worlds are still gated
   (Petal Isles → W2 threshold path, for example).
3. Tail the log (command above).
4. **Boot to title screen.** Capture any `WorldUnlockCheck` fires
   pre-save-load.
5. **Select save.** Capture new fires.
6. **Walk the world map.** Capture fires correlated with movement /
   cursor position.
7. **Approach a gated transition** (e.g., a Petal Isles flower-path
   that requires more seeds than you have). Press the input that
   normally triggers the "you need more seeds" prompt. Note exactly
   when `WorldUnlockCheck` fires and what it returns.
8. **Collect more Wonder Seeds** until the threshold is met. Cross
   the same gate. Note return-value change.
9. **Repeat for a different gate** (different world).

### What the log distinguishes

| Log pattern | Hypothesis confirmed | Next implementation step |
|---|---|---|
| `WorldUnlockCheck(N) -> 0` right before "blocked" UX; `-> 1` after threshold cross | H1: this IS the gate | Implement return-override hook reading AP per-world seed counts |
| Stable returns; fires only at world-map-load; no per-attempt fires | H2: UI/static-progress reader | Pivot to finding the actual gate (caller-PC trace via `SeedBitfieldRead` log helps) |
| Never fires on gate attempts (or fires only at startup unrelated to gates) | H3: gate is elsewhere | Cheat Engine memory-access breakpoint on live mirror of save offset 0x3AF8 |

`SeedBitfieldRead` is a corroborator:
- Reads correlated with seed pickup → confirms `0x60458608` is the
  bitfield being SET on collection.
- Reads during gate attempts → reveals a previously-unknown caller
  doing seed-count aggregation (the static walker missed it).
- Optional enhancement (only if first pass needs more info): capture
  caller PC via inline-asm read of the link register. Skip in v1
  unless required.

### Deliverable from the session

1. The two trampolines installed in `main.cpp`, compiling cleanly.
2. A captured log slice (filtered for `[smbwap` lines) covering:
   - Boot → save-load
   - World-map walk
   - One blocked gate attempt
   - One unlocked gate attempt
3. A new section in `docs/static-analysis-findings.md`:
   `## YYYY-MM-DD — Wonder Seed gate observability run` that:
   - Pastes a representative log slice
   - States which of H1 / H2 / H3 the data supports
   - Names the concrete next session's work

### Constraints summary

- **No state mutation.** Pure logging trampolines.
- **No `thread_local`** (CLAUDE.md gotcha #1).
- **Verify prologue safety** before installing each hook (gotcha #2).
  `FUN_7100935ce0` is verified; `FUN_7100124134` is NOT.
- **Filter `SeedBitfieldRead` on hash 0x60458608**; do not log every
  call.
- **Do not modify the AP world / bridge.** This session is purely
  switch-mod observability.
- **Both hooks log `isSaveLoaded()`** so we can correlate fires with
  pre/post save-data-loaded state.
