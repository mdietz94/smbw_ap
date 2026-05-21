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
