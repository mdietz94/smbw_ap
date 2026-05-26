# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — Wonder Seed gate RE
# (2026-05-26 iteration 3).
#
# Round-2 follow-up after iteration 2 produced:
#   - Four cheat anchors converged on a per-course bit reader pattern
#     (NOT gate functions themselves, but their CALLERS are gates).
#   - FUN_710048907c is called from 3/4 cheat-anchored bit readers.
#   - FUN_7100935ce0 has 8 consecutive orphan-code call sites at
#     0x7100480ff8..0x710048104c (12 bytes apart) — strongly suggests
#     a per-world dispatch loop over 8 buckets.
#   - 4 walker call sites pass the hash dynamically (via register or
#     struct field), indicating a generic gate helper called per-world
#     with each bucket's hash.
#
# This round dumps:
#   1. The aggregator callers (FUN_710048907c, FUN_7100487b0c)
#   2. The likely per-world dispatch loop containing 0x710048101X
#   3. The dynamic-hash gate helpers (FUN_710074e6cc, FUN_71005f4550)
#   4. The first 4 candidates that got truncated last run
#      (FUN_71006c12a0, FUN_7100689880, FUN_710066e548, FUN_710064d6c4)
#   5. FUN_7101babf2c (rank #10, cmp #3 b.ne = W1 threshold)
#   6. FUN_7100884040 (Lock/Unlock string carrier)
#
# CRITICAL CHANGE: output is written to BOTH the console AND a file at
# OUTPUT_FILE_PATH so it survives Ghidra console buffer overflow.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

import os

# File output path — adjust if running outside the expected dev box.
OUTPUT_FILE_PATH = r"C:\Users\maxwe\Documents\smwonder_archipelago\.claude\worktrees\funny-lehmann-06e394\scripts\ghidra\dump_v2_output.txt"

# (nso_offset_full, label)
TARGETS = [
    # New high-priority targets from cheat-anchor analysis
    (0x710048907c, "*** CHEAT-ANCHOR CONVERGENCE (3 of 4 bit readers) — likely upstream gate aggregator"),
    (0x7100487b0c, "*** CHEAT-ANCHOR CONVERGENCE (2 of 4 bit readers) — secondary aggregator"),
    (0x7100935ce0, "*** has 8 orphan-code callers at 0x7100480ff8..0x710048104c — per-world dispatch?"),

    # Dynamic-hash gate helpers (hash passed via struct/reg)
    (0x710074e6cc, "*** DYNAMIC HASH from [x19,#0x264] — per-world descriptor pattern"),
    (0x71005f4550, "*** DYNAMIC HASH from [x1,#0xa0] — per-world descriptor pattern"),
    (0x71005c7fcc, "DYNAMIC HASH via mov w2,w1 (caller-passed)"),
    (0x710066e548, "rank #7 cmp #12 b.eq — Petal Isles threshold; hash via w22"),

    # Candidates that were truncated off the top of the previous run
    (0x71006c12a0, "#11 cmp #2 b.lt — strictest gate shape (hash=0x42ffdf00)"),
    (0x7100689880, "#9 cmp #5 b.hi — PI threshold (hash=0xe237fbc6)"),
    (0x710064d6c4, "#2 cmp #9 b.hi — W2 threshold (hash=0xf4d9942a)"),
    (0x7101babf2c, "#10 cmp #3 b.ne — W1 threshold (hash=0x65634476)"),

    # Lock/Unlock string carrier (string anchor #2 evidence)
    (0x7100884040, "Lock/Unlock string carrier"),

    # Function containing the 8 orphan call sites — find it via getFunctionContaining
    # (will be looked up by address below)
]

# Per-bucket orphan-call-site investigation: we want to know what
# function contains addresses 0x7100480ff8..0x710048104c.  If they're
# inside a function, we dump that function once.  If they're orphan
# (no function defined), we dump a wider disasm window.
ORPHAN_SITES = [
    0x7100480ff8,
    0x7100481004,
    0x7100481010,
    0x710048101c,
    0x7100481028,
    0x7100481034,
    0x7100481040,
    0x710048104c,
]

PROLOGUE_INSNS = 8

VTABLE_REGIONS = [
    (0x7103340000, 0x7103350000),
    (0x71033f0000, 0x7103400000),
    (0x71034b0000, 0x71034c0000),
]


# ---- shared formatting helpers (mirror dump_gate_candidates.py) ----


_out_lines = []


def out(s=""):
    _out_lines.append(s)
    print(s)


def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def _in_vtable_region(off):
    for lo, hi in VTABLE_REGIONS:
        if lo <= off < hi:
            return True
    return False


def classify_caller(src_addr, fn_mgr):
    src_off = src_addr.getOffset()
    fn = fn_mgr.getFunctionContaining(src_addr)
    if fn is not None:
        return ("fn", "%s @ %s" % (fn.getName(), fn.getEntryPoint()))
    if _in_vtable_region(src_off):
        return ("vtable", "VTABLE @ 0x%x" % src_off)
    listing = currentProgram.getListing()
    ins = listing.getInstructionAt(src_addr)
    if ins is not None:
        return ("orphan", "orphan code @ 0x%x  %s" % (src_off, ins))
    return ("data", "data ref @ 0x%x" % src_off)


def get_callers(fn):
    ref_mgr = currentProgram.getReferenceManager()
    fn_mgr = currentProgram.getFunctionManager()
    refs = list(ref_mgr.getReferencesTo(fn.getEntryPoint()))
    out_refs = []
    for r in refs:
        src = r.getFromAddress()
        ref_type = str(r.getReferenceType())
        kind, label = classify_caller(src, fn_mgr)
        out_refs.append((kind, label, src, ref_type))
    return out_refs


def dump_prologue(fn, n=PROLOGUE_INSNS):
    listing = currentProgram.getListing()
    cur = listing.getInstructionAt(fn.getEntryPoint())
    rows = []
    while cur is not None and len(rows) < n:
        addr = cur.getAddress()
        try:
            bs = cur.getBytes()
            bs_hex = " ".join("%02x" % (b & 0xff) for b in bs)
        except Exception:
            bs_hex = "?"
        rows.append((addr, str(cur), bs_hex))
        cur = cur.getNext()
    return rows


def has_pc_relative_in_prologue(prologue_rows):
    UNSAFE = ("adrp", "adr", "b", "bl", "b.", "cbz", "cbnz", "tbz",
              "tbnz", "ret", "br", "blr")
    flagged = []
    for i, (addr, ins, _) in enumerate(prologue_rows[:5]):
        s = ins.lower().strip()
        first = s.split()[0] if s.split() else ""
        if first in UNSAFE or first.startswith("b."):
            flagged.append((i, ins))
        if first == "ldr" and ("0x71" in s or "[pc" in s):
            flagged.append((i, ins + "  (likely PC-relative literal)"))
    return flagged


def get_decompile_text(fn, timeout_sec=45):
    try:
        from ghidra.app.decompiler import DecompInterface
        from ghidra.util.task import ConsoleTaskMonitor
    except Exception as e:
        return ["[decompile import failed: %s]" % e]
    di = DecompInterface()
    di.openProgram(currentProgram)
    try:
        res = di.decompileFunction(fn, timeout_sec, ConsoleTaskMonitor())
        if res is None or not res.decompileCompleted():
            err = res.getErrorMessage() if res else "(no result)"
            return ["[decompile failed: %s]" % err]
        decomp = res.getDecompiledFunction()
        if decomp is None:
            return ["[no decompiled output]"]
        return decomp.getC().splitlines()
    finally:
        di.dispose()


def dump_target(off_full, label):
    out("")
    out("================================================================")
    out("=== TARGET 0x%x  --  %s" % (off_full, label))
    out("================================================================")
    fn_mgr = currentProgram.getFunctionManager()
    a = _addr(off_full)
    fn = fn_mgr.getFunctionAt(a)
    if fn is None:
        fn = fn_mgr.getFunctionContaining(a)
    if fn is None:
        out("  [no function at 0x%x]" % off_full)
        return

    out("  Function: %s @ %s" % (fn.getName(), fn.getEntryPoint()))
    out("  Signature: %s" % fn.getSignature())
    body_size = fn.getBody().getNumAddresses()
    out("  Body size: %d bytes" % body_size)

    out("")
    out("  -- Prologue (first %d insns, with bytes) --" % PROLOGUE_INSNS)
    prologue = dump_prologue(fn)
    for addr, ins, bs in prologue:
        out("    %s   %-20s  %s" % (addr, bs, ins))

    flagged = has_pc_relative_in_prologue(prologue)
    if flagged:
        out("  PROLOGUE TRAMPOLINE UNSAFE: %d PC-relative op(s) in first 5 insns" % len(flagged))
        for i, s in flagged:
            out("    [insn %d] %s" % (i, s))
    else:
        out("  Prologue trampoline-safe: no PC-relative ops in first 5 insns")

    out("")
    out("  -- Direct callers --")
    callers = get_callers(fn)
    n_fn = sum(1 for k, _, _, _ in callers if k == "fn")
    n_vt = sum(1 for k, _, _, _ in callers if k == "vtable")
    n_or = sum(1 for k, _, _, _ in callers if k == "orphan")
    n_dt = sum(1 for k, _, _, _ in callers if k == "data")
    out("  Total refs: %d  (fn=%d, vtable=%d, orphan=%d, data=%d)" %
        (len(callers), n_fn, n_vt, n_or, n_dt))
    for kind in ("vtable", "fn", "orphan", "data"):
        for k, lab, src, rt in callers:
            if k != kind:
                continue
            out("    [%s][%s]  %s" % (kind, rt, lab))

    out("")
    out("  -- Decompile --")
    c_lines = get_decompile_text(fn)
    for ln in c_lines:
        out("    %s" % ln)


def investigate_orphan_sites():
    out("")
    out("################################################################")
    out("####  ORPHAN-CODE INVESTIGATION (8 consecutive bl sites)  ######")
    out("################################################################")
    fn_mgr = currentProgram.getFunctionManager()
    listing = currentProgram.getListing()

    # Find the containing function (if any) for each orphan site.
    seen_fns = set()
    for site in ORPHAN_SITES:
        a = _addr(site)
        fn = fn_mgr.getFunctionContaining(a)
        if fn is not None:
            key = fn.getEntryPoint().getOffset()
            if key not in seen_fns:
                seen_fns.add(key)
                out("")
                out("  Orphan site 0x%x is INSIDE function %s @ %s" %
                    (site, fn.getName(), fn.getEntryPoint()))
        else:
            out("  Orphan site 0x%x is NOT inside any defined function" % site)

    # Dump a wide disasm window spanning all 8 sites.
    out("")
    out("  -- Disasm window spanning all 8 orphan call sites --")
    start = ORPHAN_SITES[0] - 0x20
    end = ORPHAN_SITES[-1] + 0x10
    cur = listing.getInstructionAt(_addr(start))
    if cur is None:
        # Try slightly after start.
        cur = listing.getInstructionAfter(_addr(start - 0x10))
    count = 0
    while cur is not None and cur.getAddress().getOffset() <= end and count < 100:
        addr = cur.getAddress()
        try:
            bs = cur.getBytes()
            bs_hex = " ".join("%02x" % (b & 0xff) for b in bs)
        except Exception:
            bs_hex = "?"
        marker = "*" if addr.getOffset() in ORPHAN_SITES else " "
        out("  %s %s   %-20s  %s" % (marker, addr, bs_hex, cur))
        cur = cur.getNext()
        count += 1

    # If a containing function was found, dump its full decompile.
    for site in ORPHAN_SITES[:1]:  # only need to check the first; same fn if any
        a = _addr(site)
        fn = fn_mgr.getFunctionContaining(a)
        if fn is None:
            out("")
            out("  No containing function defined.  These 8 sites appear")
            out("  in code that Ghidra hasn't recognized as a function.")
            out("  Try Right-click -> Create Function on 0x%x or earlier" % site)
            out("  to disassemble the surrounding region.")
            break
        out("")
        out("  -- Decompile of containing function %s --" % fn.getName())
        c_lines = get_decompile_text(fn)
        for ln in c_lines:
            out("    %s" % ln)


def main():
    out("SMBW Archipelago: dump_gate_candidates_v2.py")
    out("  (Wonder Seed gate-check RE — iteration 3)")
    out("  targets=%d  orphan_sites=%d" % (len(TARGETS), len(ORPHAN_SITES)))
    out("  output also written to: %s" % OUTPUT_FILE_PATH)

    for off, label in TARGETS:
        if monitor.isCancelled():
            break
        dump_target(off, label)

    if not monitor.isCancelled():
        investigate_orphan_sites()

    out("")
    out("Done.")

    # Write captured output to file.
    try:
        d = os.path.dirname(OUTPUT_FILE_PATH)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        f = open(OUTPUT_FILE_PATH, "w")
        try:
            for ln in _out_lines:
                f.write(ln)
                f.write("\n")
        finally:
            f.close()
        print("")
        print("[file] Wrote %d lines to %s" % (len(_out_lines), OUTPUT_FILE_PATH))
    except Exception as e:
        print("[file] FAILED to write %s: %s" % (OUTPUT_FILE_PATH, e))


main()
