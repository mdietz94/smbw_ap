# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — Wonder Seed gate RE
# (2026-05-26 iteration 4).
#
# After iteration 3 found the breakthrough: 8 orphan-code call sites at
# 0x7100480ff8..0x710048104c form a hardcoded per-world dispatch over
# FUN_7100935ce0(world_id), with each tbnz branching to a per-world
# handler.  But:
#   - The CONTAINING function of the 8 orphan sites isn't recognized
#     by Ghidra.  We auto-create it.
#   - We don't yet know what each per-world handler does (8 targets
#     at 0x71004816c8..0x7100481754, 5 insns each).
#   - We never decompiled the functions that load the "WonderSeed"
#     string from .rodata (iteration 1's string sweep found 7 such
#     functions but we skipped them).
#
# This script:
#   1. Tries to disassemble + create-function at 0x7100480fd8
#      (an instruction or two before the dispatch).  If that fails,
#      tries earlier offsets.
#   2. Dumps the function containing 0x7100480ff8 (after creation).
#   3. Dumps a wider disasm window around the per-world handlers
#      (0x71004816c8..0x7100481760) so we can read each branch's
#      action by hand.
#   4. Dumps the 7 functions that xref "WonderSeed" string fragments.
#   5. Adds new walker-style entries we haven't dumped: FUN_71000d33e0
#      (the context-builder called inside FUN_7100935ce0).
#
# Output: BOTH console AND file at OUTPUT_FILE_PATH.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

import os

OUTPUT_FILE_PATH = r"C:\Users\maxwe\Documents\smwonder_archipelago\.claude\worktrees\funny-lehmann-06e394\scripts\ghidra\dump_v3_output.txt"

# Address inside the orphan dispatch region — script will try to
# create a function here or earlier so the containing function can be
# decompiled.
DISPATCH_ANCHOR = 0x7100480fd8

# Fallback addresses to try if DISPATCH_ANCHOR fails to start a fn.
DISPATCH_FALLBACKS = [
    0x7100480fc0,
    0x7100480fa0,
    0x7100480f80,
    0x7100480f40,
    0x7100480f00,
    0x7100480e80,
    0x7100480e00,
    0x7100480d00,
    0x7100480c00,
]

# The 8 per-world tbnz branch targets.  We'll dump a 20-byte window
# at each (= 5 insns per target, the per-world handler body).
PER_WORLD_HANDLER_BASES = [
    (0x71004816c8, "world 1 (W1?) handler"),
    (0x71004816dc, "world 3 (W3?) handler"),
    (0x71004816f0, "world 4 (W4?) handler"),
    (0x7100481704, "world 5 (W5?) handler"),
    (0x7100481718, "world 6 (W6?) handler"),
    (0x710048172c, "world 7 (PI?) handler"),
    (0x7100481740, "world 2 (W2?) handler"),
    (0x7100481754, "world 9 (Special?) handler"),
]

# Functions that xref the "WonderSeed" string family (from iteration 1's
# string sweep).  We never decompiled these — high-value lookup.
WONDERSEED_STRING_CALLERS = [
    (0x71008b5768, "WonderSeed @ 71034213ed; 3 xrefs"),
    (0x71017ca460, "WonderSeed @ 71034213ed"),
    (0x71009290d0, "WonderSeed @ 710348622e"),
    (0x7101b27240, "WonderSeed @ 710348622e"),
    (0x71017d7138, "WonderSeed @ 710290302c"),
    (0x71005eccec, "WonderSeed @ 7103494dae"),
    (0x7100938a40, "WonderSeed @ 7103495df5"),
    # Two more from the "WonderFlower" hits:
    (0x7101b29618, "WonderFlower @ 710348655f"),
    (0x710093a204, "WonderFlower @ 710349789b"),
]

# Inner helpers from FUN_7100935ce0 that we haven't decompiled.
INNER_HELPERS = [
    (0x71000d33e0, "FUN_71000d33e0 — context builder; lVar10 holds the hash at +0x68"),
    (0x71000d3830, "FUN_71000d3830 — used in uStack_3c init"),
]

PROLOGUE_INSNS = 8


# ---- shared helpers ----

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


def try_create_function_at(off_full):
    """Try to disassemble + create a function at off_full.  Returns the
    resulting Function or None."""
    from ghidra.app.cmd.disassemble import DisassembleCommand
    from ghidra.app.cmd.function import CreateFunctionCmd
    from ghidra.program.model.address import AddressSet

    a = _addr(off_full)
    listing = currentProgram.getListing()
    fn_mgr = currentProgram.getFunctionManager()

    # If something exists at a but isn't disassembled, force disasm.
    if listing.getInstructionAt(a) is None:
        try:
            cmd = DisassembleCommand(a, None, True)
            cmd.applyTo(currentProgram, monitor)
        except Exception as e:
            out("    DisassembleCommand failed at 0x%x: %s" % (off_full, e))
            return None

    # If already inside a function, return that function.
    fn = fn_mgr.getFunctionContaining(a)
    if fn is not None:
        return fn

    # Otherwise create a function.
    try:
        cmd = CreateFunctionCmd(a)
        ok = cmd.applyTo(currentProgram, monitor)
        if not ok:
            out("    CreateFunctionCmd returned false at 0x%x" % off_full)
            return None
    except Exception as e:
        out("    CreateFunctionCmd raised at 0x%x: %s" % (off_full, e))
        return None

    fn = fn_mgr.getFunctionAt(a)
    if fn is None:
        fn = fn_mgr.getFunctionContaining(a)
    return fn


def dump_function_block(fn, label_extra=""):
    if fn is None:
        out("  [no function]")
        return
    out("  Function: %s @ %s   %s" % (fn.getName(), fn.getEntryPoint(), label_extra))
    out("  Signature: %s" % fn.getSignature())
    body_size = fn.getBody().getNumAddresses()
    out("  Body size: %d bytes" % body_size)

    out("")
    out("  -- Prologue (first %d insns) --" % PROLOGUE_INSNS)
    prologue = dump_prologue(fn)
    for addr, ins, bs in prologue:
        out("    %s   %-20s  %s" % (addr, bs, ins))
    flagged = has_pc_relative_in_prologue(prologue)
    if flagged:
        out("  PROLOGUE TRAMPOLINE UNSAFE: %d PC-relative op(s)" % len(flagged))
        for i, s in flagged:
            out("    [insn %d] %s" % (i, s))
    else:
        out("  Prologue trampoline-safe.")

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
    c_lines = get_decompile_text(fn)
    for ln in c_lines:
        out("    %s" % ln)


def investigate_dispatch_function():
    out("")
    out("################################################################")
    out("####  STEP 1: define + dump the orphan-dispatch containing fn  ####")
    out("################################################################")
    fn_mgr = currentProgram.getFunctionManager()

    # Check if 0x7100480ff8 is now inside a function (in case prior runs
    # of this script already created it).
    a = _addr(0x7100480ff8)
    fn = fn_mgr.getFunctionContaining(a)
    if fn is None:
        out("  No function containing 0x7100480ff8 yet.  Trying to create...")
        for anchor in [DISPATCH_ANCHOR] + DISPATCH_FALLBACKS:
            out("    Trying anchor 0x%x..." % anchor)
            fn = try_create_function_at(anchor)
            if fn is not None and fn.getBody().contains(a):
                out("    SUCCESS: function %s @ %s contains 0x7100480ff8"
                    % (fn.getName(), fn.getEntryPoint()))
                break
            elif fn is not None:
                out("    Created %s @ %s, but doesn't contain target."
                    % (fn.getName(), fn.getEntryPoint()))
                fn = None
        if fn is None:
            out("  Could not auto-create a function covering 0x7100480ff8.")
            out("  ACTION REQUIRED: open Ghidra, right-click 0x7100480fd8")
            out("  (or earlier near 0x7100480e00) -> Create Function, then")
            out("  re-run this script.")
            return

    out("")
    out("  Found containing function for the 8-orphan dispatch:")
    dump_function_block(fn, label_extra="(orphan-dispatch container)")


def dump_per_world_handlers():
    out("")
    out("################################################################")
    out("####  STEP 2: per-world handler bodies (20 bytes each)  ########")
    out("################################################################")
    listing = currentProgram.getListing()
    for handler_addr, label in PER_WORLD_HANDLER_BASES:
        out("")
        out("  --- handler @ 0x%x (%s) ---" % (handler_addr, label))
        cur = listing.getInstructionAt(_addr(handler_addr))
        if cur is None:
            # Try to disassemble the region.
            try:
                from ghidra.app.cmd.disassemble import DisassembleCommand
                cmd = DisassembleCommand(_addr(handler_addr), None, True)
                cmd.applyTo(currentProgram, monitor)
                cur = listing.getInstructionAt(_addr(handler_addr))
            except Exception as e:
                out("    [disasm failed: %s]" % e)
                continue
        if cur is None:
            out("    [no instruction at 0x%x]" % handler_addr)
            continue
        count = 0
        while cur is not None and count < 8:
            addr = cur.getAddress()
            if addr.getOffset() >= handler_addr + 0x40:
                break
            try:
                bs = cur.getBytes()
                bs_hex = " ".join("%02x" % (b & 0xff) for b in bs)
            except Exception:
                bs_hex = "?"
            out("    %s   %-20s  %s" % (addr, bs_hex, cur))
            cur = cur.getNext()
            count += 1


def dump_targets(targets, title):
    out("")
    out("################################################################")
    out("####  %s  ####" % title)
    out("################################################################")
    fn_mgr = currentProgram.getFunctionManager()
    for off_full, label in targets:
        if monitor.isCancelled():
            break
        out("")
        out("================================================================")
        out("=== %s  --  %s" % (("0x%x" % off_full), label))
        out("================================================================")
        a = _addr(off_full)
        fn = fn_mgr.getFunctionAt(a)
        if fn is None:
            fn = fn_mgr.getFunctionContaining(a)
        if fn is None:
            out("  [no function at 0x%x]" % off_full)
            continue
        dump_function_block(fn)


def main():
    out("SMBW Archipelago: dump_gate_candidates_v3.py")
    out("  (Wonder Seed gate-check RE — iteration 4)")
    out("  Targets: orphan-dispatch container + per-world handlers +")
    out("           WonderSeed-string callers + inner helpers")
    out("  Output: %s" % OUTPUT_FILE_PATH)

    investigate_dispatch_function()
    dump_per_world_handlers()
    dump_targets(WONDERSEED_STRING_CALLERS, "STEP 3: WonderSeed-string-loading functions")
    dump_targets(INNER_HELPERS, "STEP 4: inner helpers (context builder etc.)")

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
