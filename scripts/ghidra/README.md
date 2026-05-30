# Ghidra automation scripts

Small Jython / PyGhidra scripts that automate reverse-engineering tasks
on the SMBW NSO. They print to the Ghidra console.

## Running

1. Open the SMBW NSO in Ghidra (`main.nso` from
   `C:\Users\maxwe\Desktop\Roms\Switch\Super Mario Bros. Wonder\`)
   using the Adubbz Switch loader.
2. Run auto-analysis through at least `Function Start Analyzer`.
3. Apply the NN SDK symbol map per [docs/handoff.md](../../docs/handoff.md)
   "Tools used".
4. **Window → Script Manager → File menu (top-left of the Script Manager
   pane) → New Script → Jython** (or Python).  Paste the contents of the
   script you want to run, save under any name, hit the green ▶ Run button.
5. Output appears in **Window → Console**.

The scripts are Python 2 / Jython AND Python 3 / PyGhidra compatible.

## Inventory

### Sprint 2 (2026-05-24) — dataflow + symbol-aware

The first sprint (M3.2 / M3.3 / M3.3b, 2026-05-20→21) declared static RE
a dead-end after 11 string-grep + small-window-disasm scripts. Sprint 2
reframes the problem with five new scripts using:

- **The full sym files** (gmd, sead, main, sdk) imported into Ghidra
  via `import_sdk_symbols.py` — the previous sprint never did this.
- **Backward dataflow** from known hook anchors (FUN_71003d3fb0,
  GameDataMgr::sInstance) to enumerate the API surface.
- **Forward dataflow** from PlayReport field-name strings to find
  what registers/offsets the value-load uses — reveals live-struct layout.
- **Cross-referencing with the HamletDuFromage cheat DB** (30+ NSO
  code-site anchors with known semantics) and MemetendoYT's verified
  8 hash keys.

Run in this order:

| # | Script | Purpose | Phase |
|---|---|---|---|
| 1 | `import_sdk_symbols.py` | Import all sym files (gmd, sead, main, sdk) as Ghidra labels. ★ Pre-requisite for every other Sprint-2 script. Brings 27,000+ named symbols including `gmd::GameDataMgr::sInstance @ +0x0363f0f0`. | Setup / 4.3 |
| 2 | `find_gamedatamgr_xrefs.py` | Walk every xref to NSO +0x0363f0f0 (the GameDataMgr singleton). Classifies each xref as direct call / vtable dispatch / data-load. Heavy consumers = save serializer/deserializer (chokepoint hooks). | 1+5 helper |
| 3 | `walk_hash_writer_xrefs.py` | Walk every xref to `FUN_71003d3fb0` (the generic hash-keyed save-data writer). Reconstructs the `w0` constant at each call site; cross-checks against MemetendoYT's 8 verified keys. **One match unlocks all 6 Royal Seeds + flower_coin + regular_coin + COMPLETE_GAME + INTRO_CUTSCENE.** | 1.2 |
| 4 | `find_offset_constant_xrefs.py` | Scan .text for every load/store/imm-load instruction that uses a known trailing-region offset (0x4408, 0x3360, 0x0EA0, etc.) as immediate. Identifies serializer (cluster of offsets) vs gameplay writer (isolated). | 2.1 |
| 5 | `playreport_field_backtrace.py` | For each known PlayReport field name (M2.4 corpus), follow string xrefs into the calling function, identify the `bl PlayReport::Add` call, back-trace the value-load. Reveals live-struct layout: e.g., `world_wonder_flower` likely loads from `[x22, #0xC8]` (matches cheat DB's flower_coin writer). | 5.2 |
| 6 | `find_badge_writer_path.py` | **M3.2 badge-grant discovery (run #1).** Combines 4 dataflow angles: (A) scan `.text` for displacement uses of badge bitfield file offset `0x0EA0`; (B) backwalk PlayReport `equip_badge_id` xrefs; (C) backwalk PlayReport `badge_id_array` + flag nearby `clz`/`rbit`/`ubfx` bit-iteration; (D) decode cheat-DB badge-effect anchors (`+0x306AEC`, `+0x306B90`, `+0x1751DB8`, `+0x33186C`) and backward-walk their pre-cheat loads. Cross-reference output identifies the save serializer (Phase A+B overlap) and the badge-effect gating function (Phase B+D overlap), from which one xref back is the grant writer. After run #1 the 0xEA0 file-offset premise turned out wrong (it's a save-OUT-buffer offset, not a live-state offset) — see findings doc for ruled-out leads. | M3.2 |
| 7 | `find_session_struct_populator.py` | **M3.2 badge-grant discovery (run #6).** Refined script after 5 runs traced the consumer side (FUN_7101a5d93c → FUN_7101a5de58 reading session+0x674 badge_id_array, session+0x434 equip_badge_id). This script finds the POPULATOR by intersection: functions that BOTH write to those offsets AND access `gmd::GameDataMgr::sInstance` (directly or via known accessors). Ranks candidates by score — highest = most likely badge populator. Run #7 surfaced FUN_7101c62368 as the multiplayer session populator, revealing a NEW accessor `FUN_7101F27B78`. | M3.2 |
| 8 | `walk_object_accessor_hashes.py` | **M3.2 badge-grant discovery (run #9).** Walks every xref to `FUN_7101F27B78` (NSO +0x01F27B78) — the newly-discovered object-pointer GameDataMgr accessor with signature `(gmd*, void** out_obj_ptr, uint32_t hash)`. Reconstructs the 32-bit hash constant at each callsite (mov/movk pair walking) and identifies post-call typed-extractor function. Flags callsites with bit-iteration (rbit/clz/tbnz) nearby — those are u64 bitfield consumers and the badge ownership hash should be among them. | M3.2 |

Plan + rationale: see [`~/.claude/plans/i-would-like-to-resilient-pancake.md`] (M3.3) and `docs/static-analysis-findings.md` (M3.2 badge sprint).

### Wonder Seed gate-check RE (2026-05-26) — find what gates world/palace/boss entry

The user's pivot: instead of overwriting the visible Wonder Seed counter
(which is recomputed from per-acquisition flag arrays at runtime), find
the in-game function(s) that check "do you have enough Wonder Seeds to
enter X?" and (in a future session) patch them to consult the
AP-granted per-world count. Display stays vanilla; only gating
decisions become AP-authoritative.

Plan: `~/.claude/plans/we-have-had-a-calm-eclipse.md`. Strategy is
cheap-first cascading toward expensive: cheat-DB anchors → reader-cmp
walker → corroborator passes → (fallback) Cheat Engine read-watch.
Scripts below are the static-analysis half.

Run in this order:

| # | Script | Purpose | Phase |
|---|---|---|---|
| 1 | `find_gate_strings.py` | **Phase 0b** string sweep. Searches `.rodata` for gate-related UI / debug terms (`"Wonder Seed"`, `"Locked"`, `"Required"`, world names, palace names). Ranks xref'd strings by callsite count; highlights functions that touch multiple seed-like strings (string-adjacency evidence per plan Section-4 test #2). | Wonder Seed gate |
| 2 | `walk_reader_compare_sites.py` ★ | **Phase 2 workhorse.** Walks every xref to the known GameDataMgr READERS (`FUN_710012AE94`, `FUN_71003838AC`, `FUN_7100124134`, etc.). At each call site: (a) backward-reconstructs the hash constant (reused logic from `walk_hash_writer_xrefs.py`), (b) forward-scans up to 24 insns for a `cmp`/`subs`/`cbz`/`cbnz` against a Wonder Seed threshold from `regions.json` ({2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 14, 15, 16, 25}). Scores call sites by gate-shape and ranks unique enclosing functions globally — top 30 are the gate-check candidates. | Wonder Seed gate |
| 3 | `playreport_field_backtrace_seed.py` | **Phase 3 corroborator.** Backtraces the value-load idiom (`ldr Wn, [Xm, #imm]`) for seed-related PlayReport fields (`total_get_finish_seed_count`, `wonder_seed`, `world_mother_seed`, `world_wonder_flower`), then Phase 2 of the script scans the whole binary for OTHER consumers of the same load pattern. Filters out the PlayReport-builder chain (`FUN_7101a5d93c` family); remaining hits are GATE-CANDIDATE readers — functions that touch the same backing storage but aren't telemetry. | Wonder Seed gate |

How the three combine: a function that appears at the top of #2's
ranked list **and** is xref'd by #1's seed-like string list **and**
shows up as a GATE-CANDIDATE in #3 is a high-confidence hit (≥3 of
plan Section-4 evidence criteria).

### Wonder Seed RE re-open (2026-05-28) — persistence + crash-avoidance

Pivot: the gate-RE work shipped a read-side override that lets gates
pass via AP, but the underlying per-course bitfield (hash `0x60458608`,
container D) is never written, so the count resets on save/reload.
Static-analysis sprint 2 noted a 4-arg overload of `FUN_710049EA24`
that allegedly writes individual bits at hash `0x60458608` indexed by
a `bit_index` 4th arg, but it was never tested.  This script set drives
that hypothesis and discovers any other unmapped containers we missed.
Plan + protocol in [docs/wonder-seed-re-reopen-2026-05-28.md](../../docs/wonder-seed-re-reopen-2026-05-28.md).

| # | Script | Purpose | Phase |
|---|---|---|---|
| 1 | `find_hash_immediate_loads.py` | Given a list of 32-bit literals (seeded with `0x60458608`, the 5 WS mirror hashes, container-C badge hashes, etc.), find every function that materializes each literal via `mov`/`movz`/`movk`/`movn`.  Inverts `walk_hash_writer_xrefs.py`: that script harvests callers of known accessors, this finds every loader regardless of accessor.  Inter-literal correlation table at the end surfaces aggregator functions (those touching ≥2 seeded literals). | WS persistence |
| 2 | `walk_gmd_field_access.py` | For every `gmd::GameDataMgr::sInstance` dereference site, walk forward N insns and tally `(gmd+0xXX, ldr/str variant)` accesses.  Output is a histogram of substruct usage with "DOCUMENTED" vs "NEW" tags.  Surfaces unmapped containers (D/E/F) and pins the substruct anchor used by per-course writers vs container-B writers. | WS persistence |
| 3 | `decompile_container_chain.py` | Decompile `FUN_710049EA24` + delegates + `FUN_7101F2B354` + `FUN_7100124134` + the gate predicate `FUN_71001787B40` + suspected per-world gate `FUN_7100935CE0`.  Places `INVESTIGATE-WS:` plate comments at chain entry points and inner call sites so Ghidra navigation has the rationale inline.  Idempotent comment merging. | WS persistence |
| 4 | `find_wonder_seed_acquisition_chain.py` | BFS from the WonderSeedAwarded Nerve vtable's execute slot (NSO+0x3345728+0x40), depth-limited, flagging hits on the known writer offsets (`+0x49F648`, `+0x49EA24`, `+0x1F2B354`, etc.).  Output is an indented call tree + a "writer-target hits" summary listing every chain that reaches a primitive.  Combined with the live SeedTrace hooks, this gives both the static AND dynamic picture of the natural seed-grant pipeline. | WS persistence |

#### Companion host-side script

[`brute_seed_field_hashes.py`](brute_seed_field_hashes.py) —
Murmur3-32 (plus FNV-1a, CRC-32) brute force over Wonder Seed /
per-course / world / Japanese-romaji candidate names against the 10
unknown seed hashes.  Run before any deep static work; a hit names
the field directly and might short-circuit weeks of decompile.
Mirror of `brute_badge_field_hashes.py` (which produced zero hits,
i.e. expect the same here — but the negative result is itself useful
for the docs).

Companion Phase-1 manual pass (no new script needed): decompile the
HamletDuFromage Fast-Travel cheat anchors at NSO `+0x48A528`,
`+0x5D9F58`, `+0x935E10`, and `+0x48A818` (Top-of-Flag), walk
upstream 2-3 levels, and record what they short-circuit. Anchors are
catalogued in `docs/static-analysis-findings.md` lines ~217-230.

Output artifact: the runner appends a dated `## YYYY-MM-DD — Wonder
Seed gate-check RE` section to `docs/static-analysis-findings.md`
following the schema scaffolded in this commit. The implementation
session uses that artifact (plus `CLAUDE.md`) to write the hook
without re-running Ghidra.

### Companion Python tool (host-side)

[`scripts/brute_badge_field_hashes.py`](../brute_badge_field_hashes.py) —
Murmur3-32 brute-force of 710 candidate badge field name strings against
the 22 Ghidra-observed unknown 32-bit hashes from `walk_hash_writer_xrefs.py`.
Used to rule out the cheap hypothesis "maybe badges are hash-keyed under
a name we haven't tried". Run before `find_badge_writer_path.py` — a hit
would short-circuit the Ghidra work (badge grant via existing M3.3
primitive). Run 2026-05-24: zero hits, confirming badges live outside
container A (see findings doc).

### Sprint 1 (2026-05-20→21) — string-grep dead-end (history)

These were the original 11 scripts that exhausted string-anchored
discovery and motivated the save-diff pivot. **Don't repeat their
approach** — Sprint 2 is intentionally orthogonal. Kept for posterity
because the inspect_* scripts contain useful disasm-window code that
Sprint-2 scripts pattern after.

| Script | Purpose | Milestone |
|---|---|---|
| `find_badge_functions.py` | Find xrefs to `GiveBadgeIdOnCourseClear` and `UnlockBadgeIdOnCourseClear` strings; dump containing functions + prologues so we can pick the right hook target | M3.2 |
| `inspect_badge_dispatch.py` | Follow-up: dump disasm around each in-function xref site + bytes/pointer-interpretation around each data-table-entry xref, to figure out the badge-dispatch shape | M3.2 |
| `inspect_badge_candidates.py` | Step 3: now that `FUN_7101b1fb6c` is identified as a test harness, dump prologues + xref counts of the three candidate grant functions it calls. Distinguishes "real game API" (heavily called) from "test probe" (only called from harnesses) | M3.2 |
| `find_badge_strings.py` | Step 4: broader sweep — list every C-string containing "Badge"/"badge" with xref counts, sorted by xref count desc.  Hoping to surface a function-named string like `AddBadgeToBag` that's heavily referenced — the real grant entry point | M3.2 |
| `find_badge_flower_vtable.py` | Step 5: find the BadgeFlower actor's vtable. Identifies the short class-name-getter function that loads the "BadgeFlower" string, then dumps the surrounding qwords where its address appears as data (the vtable). Slot 8 (execute) and other slots in the dump are candidate touch / interaction handlers | M3.2 |
| `inspect_badge_flower_registration.py` | Step 6: the single xref to "BadgeFlower" is deep inside a large function — looks like an actor-registration chain. Dumps the prologue + a window around the xref site with `bl` target / `adrp` reference resolution, so we can see the `RegisterActor("BadgeFlower", &ctor)` style call and pluck the ctor pointer | M3.2 |
| `find_badge_acquisition_paths.py` | Step 7 (final M3.2 attempt — deferred): scan the four badge acquisition context strings (BadgeShop / BadgeChallenge / BadgeHouse / BadgeMedley) across all their address occurrences.  Result: every xref is a label-use (struct init / string-compare / log format), no shared grant helper.  M3.2 deferred to save-diff approach per `docs/milestones.md` | M3.2 |
| `find_wonder_seed_counter.py` | M3.3 step 1: disassemble around NSO +0x12AF6C (HamletDuFromage's "[seed]" cheat patch site) to identify the original Wonder Seed counter `ldr` instruction and the data field address it reads from | M3.3 |
| `dump_wonder_seed_fn_prologue.py` | M3.3 step 2: dump the first 30 instructions of FUN_710012ae94 (the seed-getter we identified in step 1) — confirms trampoline safety + calling convention before we add a subsdk hook | M3.3 |
