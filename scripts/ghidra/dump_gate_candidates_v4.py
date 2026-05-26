# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — Wonder Seed gate RE
# (2026-05-26 iteration 5).
#
# Iteration 4 confirmed FUN_7100480fd8 is the 8-world dispatch CONTAINER
# but it's a list-BUILDER not a gate.  This iteration drills into:
#
#   1. FUN_710066deec  — the sole caller of FUN_710066e548 (which
#      iterates per-course Wonder-Seed bits via hash 0x60458608 and
#      compares state to 0xc/12 — Petal Isles threshold).  If the
#      caller aggregates these into a total + threshold compare, this
#      is the gate.
#   2. FUN_7101c46acc — what the 8-world handlers in FUN_7100480fd8
#      call.  Knowing what kind of list is built reveals the semantic
#      of FUN_7100935ce0.
#   3. FUN_71000d3ca4 + FUN_71000d3c50 — called at the head of
#      FUN_7100480fd8 ("get world hash" + "get something").  Their
#      semantics inform what kind of object the dispatch represents.
#   4. FUN_710096f234 — the aggregator called with `2` after the
#      8-world dispatch.
#   5. Hex dump of the 8-entry per-world data table at 0x71029f2410
#      (32 bytes — each world's 4-byte handler arg).  If these are
#      readable strings or known hash constants, they tell us what
#      semantic the 8-world dispatch represents.
#   6. The data structure starting at 0x71029f0b34 / 0x71029f0f94 (the
#      0x70-byte per-world records from FUN_71000d33e0).  Dump first
#      record's bytes to see the hash at +0x68.
#
# Output to file as before.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

import os

OUTPUT_FILE_PATH = r"C:\Users\maxwe\Documents\smwonder_archipelago\.claude\worktrees\funny-lehmann-06e394\scripts\ghidra\dump_v4_output.txt"

# Functions to decompile.
FUNCTIONS = [
    (0x710066deec, "*** sole caller of FUN_710066e548 (per-course Wonder Seed bit iter)"),
    (0x7101c46acc, "*** called by 8-world dispatch handlers in FUN_7100480fd8"),
    (0x71000d3ca4, "called at head of FUN_7100480fd8 (gets a uint at [x0])"),
    (0x71000d3c50, "called at head of FUN_7100480fd8 (gets a uint at [x0])"),
    (0x710096f234, "aggregator called with arg `2` after 8-world dispatch"),
]

# Data tables to hex-dump.
DATA_TABLES = [
    (0x71029f2410, 0x40, "per-world handler arg table (8 x 4-byte entries)"),
    (0x71029f0b34, 0x80, "per-world record table A (first record, 0x70 bytes; hash at +0x68)"),
    (0x71029f0f94, 0x80, "per-world record table B (first record, 0x70 bytes; hash at +0x68)"),
    (0x71029f13f4, 0x80, "per-world record table fallback (DAT_71029f13f4)"),
]

PROLOGUE_INSNS = 8


# ---- shared helpers (same as v3) ----

_out_lines = []


def out(s=""):
    _out_lines.append(s)
    print(s)


def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


VTABLE_REGIONS = [
    (0x7103340000, 0x7103350000),
    (0x71033f0000, 0x7103400000),
    (0x71034b0000, 0x71034c0000),
]


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


def get_decompile_text(fn, timeout_sec=60):
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


def dump_function(off_full, label):
    out("")
    out("================================================================")
    out("=== FUNCTION 0x%x  --  %s" % (off_full, label))
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
    out("  Body size: %d bytes" % fn.getBody().getNumAddresses())

    out("")
    out("  -- Prologue (first %d insns) --" % PROLOGUE_INSNS)
    for addr, ins, bs in dump_prologue(fn):
        out("    %s   %-20s  %s" % (addr, bs, ins))

    out("")
    out("  -- Direct callers --")
    callers = get_callers(fn)
    out("  Total refs: %d" % len(callers))
    for k, lab, src, rt in callers[:30]:
        out("    [%s][%s]  %s" % (k, rt, lab))
    if len(callers) > 30:
        out("    ... (%d more not shown)" % (len(callers) - 30))

    out("")
    out("  -- Decompile --")
    for ln in get_decompile_text(fn):
        out("    %s" % ln)


def dump_data_table(off_full, n_bytes, label):
    out("")
    out("================================================================")
    out("=== DATA TABLE 0x%x  (%d bytes)  --  %s" % (off_full, n_bytes, label))
    out("================================================================")
    memory = currentProgram.getMemory()
    a = _addr(off_full)
    bs = bytearray()
    for i in range(n_bytes):
        try:
            bs.append(memory.getByte(a.add(i)) & 0xff)
        except Exception as e:
            out("  [read failed at +0x%x: %s]" % (i, e))
            break

    # Hex dump 16 bytes per line.
    for i in range(0, len(bs), 16):
        hex_str = " ".join("%02x" % b for b in bs[i:i+16])
        ascii_str = "".join(
            chr(b) if 0x20 <= b < 0x7f else "."
            for b in bs[i:i+16]
        )
        out("  +0x%04x:  %-48s  |%s|" % (i, hex_str, ascii_str))

    # Interpret as 32-bit LE values for known offsets.
    out("")
    out("  -- 32-bit LE values --")
    for i in range(0, len(bs) - 3, 4):
        v = bs[i] | (bs[i+1] << 8) | (bs[i+2] << 16) | (bs[i+3] << 24)
        out("    +0x%04x: 0x%08x  (%d)" % (i, v, v))

    # Interpret hash at +0x68 if record is big enough.
    if len(bs) >= 0x6c:
        h = bs[0x68] | (bs[0x69] << 8) | (bs[0x6a] << 16) | (bs[0x6b] << 24)
        out("")
        out("  -- HASH AT +0x68: 0x%08x --" % h)


def main():
    out("SMBW Archipelago: dump_gate_candidates_v4.py")
    out("  (Wonder Seed gate-check RE — iteration 5)")
    out("  Targets: %d functions + %d data tables" % (len(FUNCTIONS), len(DATA_TABLES)))
    out("  Output: %s" % OUTPUT_FILE_PATH)

    for off, label in FUNCTIONS:
        if monitor.isCancelled():
            break
        dump_function(off, label)

    for off, n, label in DATA_TABLES:
        if monitor.isCancelled():
            break
        dump_data_table(off, n, label)

    out("")
    out("Done.")

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
