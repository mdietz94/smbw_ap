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
