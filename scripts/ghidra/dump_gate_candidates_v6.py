# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — Wonder Seed gate RE
# (2026-05-26 iteration 7 — LAST static-analysis shot).
#
# Hash 0x60458608 was identified in iteration 4 as the per-course
# Wonder Seed bitfield (FUN_710066e548 iterates over its bits for UI
# display).  If the SEED-COUNT GATE exists, it's almost certainly a
# function that:
#   - Reads hash 0x60458608 (via FUN_7100124134 per-bit reader OR
#     possibly a u64 bulk reader we haven't found)
#   - In a loop, accumulates a count (popcount) of set bits
#   - Compares the count to a threshold immediate
#   - Returns a bool / branches to "locked" path
#
# This script:
#   1. Scans .text for every immediate-load of 0x60458608.  This catches
#      mov/movz/movk chains that load this 32-bit value into any
#      register.
#   2. For each hit, identifies the enclosing function.
#   3. For each enclosing function, dumps the decompile (so we can see
#      whether it's a popcount + threshold-compare).
#
# Also scan for related per-course bitfield hash candidates:
#   - 0x580b7eb4 (seen in FUN_710066e548 alongside 0x60458608)
#   - 0x105df820 (badge_owned bitfield — for reference comparison)
#   - 0x6d1b5c25 (badge UI-slot bitmap)
#
# Output to file.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

import os

OUTPUT_FILE_PATH = r"C:\Users\maxwe\Documents\smwonder_archipelago\.claude\worktrees\funny-lehmann-06e394\scripts\ghidra\dump_v6_output.txt"

# Hashes to hunt for.
TARGET_HASHES = [
    (0x60458608, "*** Per-course Wonder Seed bitfield (THE candidate)"),
    (0x580b7eb4, "Adjacent hash seen in FUN_710066e548 (course-state)"),
    (0x105df820, "badge_owned bitfield (reference)"),
    (0x6d1b5c25, "badge UI-slot bitmap (reference)"),
]

# Mnemonics that can load a constant.
IMM_LOAD_MNEMONICS = ("mov", "movz", "movk", "movn")

# Max hits to surface per hash (avoid output flood).
MAX_HITS_PER_HASH = 32

# Max decompiles to dump per hash.
MAX_DECOMPILES_PER_HASH = 6


_out_lines = []


def out(s=""):
    _out_lines.append(s)
    print(s)


def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def _imm_at_op(ins, op_idx):
    try:
        objs = ins.getOpObjects(op_idx)
    except Exception:
        return None
    for o in objs:
        if o is None:
            continue
        if o.__class__.__name__ == "Scalar":
            try:
                return o.getUnsignedValue() & 0xFFFFFFFFFFFFFFFF
            except Exception:
                try:
                    return o.getValue() & 0xFFFFFFFFFFFFFFFF
                except Exception:
                    return None
    return None


def _lsl_shift_from_insn(ins):
    s = str(ins)
    low = s.lower()
    i = low.find("lsl #")
    if i < 0:
        return 0
    rest = s[i + 5:].strip()
    num = ""
    for ch in rest:
        if ch in "0123456789abcdefxABCDEFX":
            num += ch
        else:
            break
    if not num:
        return 0
    try:
        return int(num, 0)
    except ValueError:
        return 0


def find_immediate_uses_for_hash(hash_val):
    """Scan all instructions for those that contribute to building
    `hash_val` as a 32-bit constant.  Two patterns matter:
      1. Single `mov` or `movz` with shifted imm == hash_val.
      2. `movz wN, #lo` + `movk wN, #hi, LSL #16` pair.

    Returns list of (Instruction, "single"|"pair", peer_instruction).
    For pairs, the returned instruction is the SECOND of the pair (so
    the call site is downstream from it)."""
    listing = currentProgram.getListing()
    hits = []

    lo16 = hash_val & 0xFFFF
    hi16 = (hash_val >> 16) & 0xFFFF

    inst_iter = listing.getInstructions(True)
    pending = {}  # reg_name -> (low_value_so_far, instruction)
    for ins in inst_iter:
        if monitor.isCancelled():
            return hits
        mnem = ins.getMnemonicString().lower()
        if mnem not in IMM_LOAD_MNEMONICS:
            continue
        # Identify destination register.
        try:
            dst_objs = ins.getOpObjects(0)
        except Exception:
            continue
        dst_reg = None
        for o in dst_objs:
            if o is None:
                continue
            if o.__class__.__name__ == "Register":
                dst_reg = o.getName().lower()
                break
        if dst_reg is None:
            continue
        imm = _imm_at_op(ins, 1)
        if imm is None:
            continue
        shift = _lsl_shift_from_insn(ins)

        if mnem in ("mov", "movz"):
            val = (imm << shift) & 0xFFFFFFFF
            # Single-insn match (e.g., mov w0, #0x60458608 — not actually
            # encodable in one insn; but if hash happens to fit in a
            # MOVZ-shifted form, catch it).
            if val == hash_val:
                hits.append((ins, "single", None))
                pending.pop(dst_reg, None)
            elif (imm == lo16 and shift == 0):
                # Low half — register pending high half.
                pending[dst_reg] = (val, ins)
            else:
                pending.pop(dst_reg, None)
        elif mnem == "movk":
            val = (imm << shift) & 0xFFFFFFFF
            if dst_reg in pending and shift == 16:
                low_val, low_ins = pending[dst_reg]
                combined = low_val | val
                if combined == hash_val:
                    hits.append((ins, "pair", low_ins))
            pending.pop(dst_reg, None)
        elif mnem == "movn":
            val = (~(imm << shift)) & 0xFFFFFFFF
            if val == hash_val:
                hits.append((ins, "single", None))
            pending.pop(dst_reg, None)

        if len(hits) >= MAX_HITS_PER_HASH:
            return hits

    return hits


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
            return ["[decompile failed]"]
        decomp = res.getDecompiledFunction()
        if decomp is None:
            return ["[no decompiled output]"]
        return decomp.getC().splitlines()
    finally:
        di.dispose()


def process_hash(hash_val, label):
    out("")
    out("################################################################")
    out("####  HASH 0x%08x  --  %s" % (hash_val, label))
    out("################################################################")
    fn_mgr = currentProgram.getFunctionManager()
    hits = find_immediate_uses_for_hash(hash_val)
    out("  Found %d immediate-load site(s)" % len(hits))
    if not hits:
        return

    # Group by enclosing function.
    by_fn = {}
    for ins, kind, peer in hits:
        fn = fn_mgr.getFunctionContaining(ins.getAddress())
        key = fn.getEntryPoint().getOffset() if fn else None
        if key not in by_fn:
            by_fn[key] = {"fn": fn, "sites": []}
        by_fn[key]["sites"].append((ins, kind, peer))

    out("  Unique enclosing functions: %d" % len(by_fn))
    out("")
    out("  -- All hits --")
    for ins, kind, peer in hits:
        fn = fn_mgr.getFunctionContaining(ins.getAddress())
        if fn is not None:
            fn_label = "%s+0x%x @ %s" % (
                fn.getName(),
                ins.getAddress().getOffset() - fn.getEntryPoint().getOffset(),
                fn.getEntryPoint())
        else:
            fn_label = "(orphan @ 0x%x)" % ins.getAddress().getOffset()
        if kind == "pair" and peer is not None:
            out("    %s   %s   [pair w/ %s: %s]   %s" %
                (ins.getAddress(), ins, peer.getAddress(), peer, fn_label))
        else:
            out("    %s   %s   [%s]   %s" %
                (ins.getAddress(), ins, kind, fn_label))

    # Decompile up to MAX_DECOMPILES_PER_HASH enclosing functions.
    # Order: functions NOT previously surfaced first (FUN_710066e548
    # is already known and ruled out).
    KNOWN_RULED_OUT = {0x710066e548}
    fn_keys = list(by_fn.keys())
    fn_keys.sort(key=lambda k: (k in KNOWN_RULED_OUT, k or 0))

    decomp_count = 0
    out("")
    out("  -- Decompiles of enclosing functions --")
    for key in fn_keys:
        if decomp_count >= MAX_DECOMPILES_PER_HASH:
            break
        info = by_fn[key]
        fn = info["fn"]
        if fn is None:
            continue
        marker = " (ALREADY RULED OUT)" if key in KNOWN_RULED_OUT else ""
        out("")
        out("  ============= %s @ %s%s =============" %
            (fn.getName(), fn.getEntryPoint(), marker))
        out("  Body size: %d bytes" % fn.getBody().getNumAddresses())
        out("  Hit sites in this fn:")
        for ins, kind, peer in info["sites"]:
            out("    %s   %s" % (ins.getAddress(), ins))
        out("")
        out("  Decompile:")
        for ln in get_decompile_text(fn):
            out("    %s" % ln)
        decomp_count += 1


def main():
    out("SMBW Archipelago: dump_gate_candidates_v6.py")
    out("  (Wonder Seed gate-check RE — iteration 7 — last static shot)")
    out("  Hunting %d target hashes" % len(TARGET_HASHES))
    out("  Output: %s" % OUTPUT_FILE_PATH)

    for h, label in TARGET_HASHES:
        if monitor.isCancelled():
            break
        process_hash(h, label)

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
