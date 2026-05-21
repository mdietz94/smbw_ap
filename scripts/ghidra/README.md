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

| Script | Purpose | Milestone |
|---|---|---|
| `find_badge_functions.py` | Find xrefs to `GiveBadgeIdOnCourseClear` and `UnlockBadgeIdOnCourseClear` strings; dump containing functions + prologues so we can pick the right hook target | M3.2 |
| `inspect_badge_dispatch.py` | Follow-up: dump disasm around each in-function xref site + bytes/pointer-interpretation around each data-table-entry xref, to figure out the badge-dispatch shape | M3.2 |
| `inspect_badge_candidates.py` | Step 3: now that `FUN_7101b1fb6c` is identified as a test harness, dump prologues + xref counts of the three candidate grant functions it calls. Distinguishes "real game API" (heavily called) from "test probe" (only called from harnesses) | M3.2 |
| `find_badge_strings.py` | Step 4: broader sweep — list every C-string containing "Badge"/"badge" with xref counts, sorted by xref count desc.  Hoping to surface a function-named string like `AddBadgeToBag` that's heavily referenced — the real grant entry point | M3.2 |
| `find_badge_flower_vtable.py` | Step 5: find the BadgeFlower actor's vtable. Identifies the short class-name-getter function that loads the "BadgeFlower" string, then dumps the surrounding qwords where its address appears as data (the vtable). Slot 8 (execute) and other slots in the dump are candidate touch / interaction handlers | M3.2 |
| `inspect_badge_flower_registration.py` | Step 6: the single xref to "BadgeFlower" is deep inside a large function — looks like an actor-registration chain. Dumps the prologue + a window around the xref site with `bl` target / `adrp` reference resolution, so we can see the `RegisterActor("BadgeFlower", &ctor)` style call and pluck the ctor pointer | M3.2 |
| `find_badge_acquisition_paths.py` | Step 7 (final M3.2 attempt before pivot): scan the four badge acquisition context strings (BadgeShop / BadgeChallenge / BadgeHouse / BadgeMedley) across all their address occurrences, dumping disasm windows with `bl` target annotations.  Hoping to find a shared grant-helper function called from multiple acquisition contexts | M3.2 |
