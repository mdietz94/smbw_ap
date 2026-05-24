# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — sprint 2, Phase 2.1.
#
# For each known trailing-region offset in the save buffer (see
# docs/save-diff-findings.md), scan the entire .text for instructions
# that use that offset as an immediate.  Each occurrence is a candidate
# read or write of the corresponding save field.
#
# The save file's trailing region (file >= 0xBF0) is mirrored byte-for-
# byte in live memory at `live_state_base + file_offset` per the runtime
# anchor finding in docs/save-diff-findings.md.  So an instruction like
#
#     str  w0, [x22, #0x4408]
#
# is the per-course CourseClear write (capture #2's array slot 6).
#
# AArch64 immediate-displacement loads/stores:
#   ldr / str / ldrb / strb / ldrh / strh
#   ldr Wt, [Xn, #imm]    : imm is 0..0x3FFC for 32-bit unscaled,
#                            or up to 0x3FFC scaled by 4 for 32-bit reg.
#   ldrb/strb              : 0..0xFFF
#   ldrh/strh              : 0..0x1FFE scaled by 2
#
# So large offsets like 0x4408 may appear:
#   (a) directly: `ldr w0, [x22, #0x4408]` (if the encoding fits)
#   (b) via `add x_tmp, x_base, #0x<hi>, lsl #12; add x_tmp, x_tmp, #0x<lo>; str ...`
#   (c) via `movz x_tmp, #0x4408; add ...; str ...`
#
# This script scans for (a) and (c).  For (b) we'd need a smarter
# instruction-pair matcher; if (a)+(c) doesn't surface enough, extend
# the matcher.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

# Known trailing-region offsets (file offsets, equal to live offsets
# from save_buffer_base for the trailing region per the M3.4 anchor).
OFFSETS = [
    # Per-course CourseClear arrays (4-byte stride):
    (0x43F0, "CourseClear/Normal"),
    (0x4408, "CourseClear/Normal slot 6"),  # capture #2 specific
    (0x4438, "CourseClear/Badge"),
    (0x4460, "CourseClear/Palace"),
    (0x4488, "CourseClear/Arena"),
    (0x44B0, "CourseClear/Breaktime"),
    # Per-course GoalSeed arrays:
    (0x3348, "GoalSeed/Normal"),
    (0x3360, "GoalSeed/Normal slot 6"),     # capture #2 specific
    (0x3390, "GoalSeed/Badge"),
    (0x33E0, "GoalSeed/Arena"),
    (0x3408, "GoalSeed/Breaktime"),
    # Per-course WonderSeed (mid-course Wonder Phase):
    (0x3AF8, "WonderSeed/Normal"),
    # Per-course PurpleCoin arrays (10-coins):
    (0x1718, "PurpleCoin/Normal"),
    (0x1760, "PurpleCoin/Badge"),
    (0x1788, "PurpleCoin/Palace"),
    (0x17B0, "PurpleCoin/Arena"),
    # Misc save-data anchors:
    (0x0CD3, "ClapperGate/Palace"),
    (0x0D3C, "shared course-clear bitfield"),
    (0x0EA0, "badge ownership u64"),
    (0x167C, "Lives (u8)"),
    (0x16B8, "equipped-badge name-hash"),
    (0x3480, "shop wonder-seed flag"),
    (0x50B8, "savedata_id UUID anchor"),
    (0x53E8, "active BC marker u32"),
    (0x53EC, "active BC name-hash 8 bytes"),
    # Hash-pair-region offsets we know from save-diff for cross-ref
    # (won't be useful as live-state writers since the pair region
    # is hash-keyed at runtime, but useful for confirming false-positives):
    (0x0894, "pair: flower_coin value"),
    (0x08AC, "pair: regular_coin value"),
]

# Cheats DB code-site anchors with known load/store offsets — these are
# candidate live-state structure offsets in code memory (NOT file
# offsets; the live struct is laid out differently from the save file).
# Listing them so the cross-reference table at the end can call out
# overlaps as either confirmation or sanity-check.
LIVE_STRUCT_OFFSETS = [
    (0x60, "live-state: lives (cheat @ +0x45AA34 STR W7,[X22,#0x60])"),
    (0xC8, "live-state: flower_coin (cheat @ +0x49253C STR W10,[X22,#0xC8])"),
    (0xF8, "live-state: vertical velocity / moon-jump (cheat @ +0x1E5D024)"),
    (0x150, "live-state: swim state (cheat @ +0xED250)"),
    (0x1C, "live-state: hp/death byte (cheat @ +0x2743C0)"),
]

# Mnemonics we'll inspect for offset-immediate uses.
DISP_MNEMONICS = (
    "ldr", "ldrb", "ldrh", "ldrsb", "ldrsh", "ldrsw",
    "str", "strb", "strh",
    "ldp", "stp",
)

# Mnemonics that load a constant into a register (for the
# `movz + add` chain detection):
IMM_LOAD_MNEMONICS = ("mov", "movz", "movk", "movn")

# Limit per-offset output to avoid console flood.
MAX_HITS_PER_OFFSET = 32


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


def _scan_displacement_uses(memory_view, offset_int):
    """Iterate all instructions in the program and yield those whose
    last operand has an immediate displacement equal to `offset_int`.
    Operates on the current program."""
    listing = currentProgram.getListing()
    hits = []
    inst_iter = listing.getInstructions(True)  # forward
    for ins in inst_iter:
        if monitor.isCancelled():
            return hits
        mnem = ins.getMnemonicString().lower()
        if mnem not in DISP_MNEMONICS:
            continue
        # The displacement is typically the last operand's scalar.
        # Try the last 2 operand indices.
        nops = ins.getNumOperands()
        for op_idx in (nops - 1, nops - 2):
            if op_idx < 0:
                continue
            imm = _imm_at_op(ins, op_idx)
            if imm is None:
                continue
            if imm == offset_int:
                hits.append(ins)
                break
        if len(hits) >= MAX_HITS_PER_OFFSET:
            return hits
    return hits


def _scan_imm_constant_loads(offset_int):
    """Scan all instructions for mov/movz/movk loading the exact value
    `offset_int` as an immediate.  These are candidate `live_base +
    offset` derivations via the offset-into-register pattern."""
    listing = currentProgram.getListing()
    hits = []
    inst_iter = listing.getInstructions(True)
    for ins in inst_iter:
        if monitor.isCancelled():
            return hits
        mnem = ins.getMnemonicString().lower()
        if mnem not in IMM_LOAD_MNEMONICS:
            continue
        # operand 1 holds the immediate.
        imm = _imm_at_op(ins, 1)
        if imm is None:
            continue
        if imm == offset_int:
            hits.append(ins)
        if len(hits) >= MAX_HITS_PER_OFFSET:
            return hits
    return hits


def _fmt_hit(ins):
    fn_mgr = currentProgram.getFunctionManager()
    fn = fn_mgr.getFunctionContaining(ins.getAddress())
    if fn is None:
        return "%s   %s   (no fn)" % (ins.getAddress(), ins)
    delta = ins.getAddress().getOffset() - fn.getEntryPoint().getOffset()
    return "%s   %s   in %s+0x%x" % (
        ins.getAddress(), ins, fn.getName(), delta)


def process_offset(offset_int, label):
    print("")
    print("===== 0x%x  (%s) =====" % (offset_int, label))
    memory_view = currentProgram.getMemory()
    disp_hits = _scan_displacement_uses(memory_view, offset_int)
    imm_hits = []
    # Only scan imm loads if the offset doesn't fit a single 12-bit
    # displacement (large offsets are usually built via movz + add).
    if offset_int > 0xFFF:
        imm_hits = _scan_imm_constant_loads(offset_int)

    print("  ldr/str disp matches: %d  (showing up to %d)" %
          (len(disp_hits), MAX_HITS_PER_OFFSET))
    for ins in disp_hits[:MAX_HITS_PER_OFFSET]:
        print("    %s" % _fmt_hit(ins))
    if offset_int > 0xFFF:
        print("  mov/movz/movk imm matches: %d  (showing up to %d)" %
              (len(imm_hits), MAX_HITS_PER_OFFSET))
        for ins in imm_hits[:MAX_HITS_PER_OFFSET]:
            print("    %s" % _fmt_hit(ins))


def main():
    print("SMBW Archipelago: find_offset_constant_xrefs.py")
    print("  scanning .text for displacement / imm uses of known offsets")
    print("")
    main_base = currentProgram.getImageBase().getOffset()
    print("  main base: 0x%x" % main_base)

    for off, label in OFFSETS:
        if monitor.isCancelled():
            break
        process_offset(off, label)

    print("")
    print("===== live-struct offset reference =====")
    print("(from HamletDuFromage cheat DB; cheat addresses already known,")
    print(" listing these here only for cross-check when reading the output)")
    for off, label in LIVE_STRUCT_OFFSETS:
        print("  +0x%-4x  %s" % (off, label))

    print("")
    print("How to read the output:")
    print("  - Functions with MANY hits across different offsets are likely")
    print("    the save SERIALIZER or DESERIALIZER (copying whole region).")
    print("  - Functions with a SINGLE hit on a SINGLE offset are likely")
    print("    GAMEPLAY-TIME WRITERS (e.g. 'on-course-clear, set this bit').")
    print("    Those are the hook candidates.")
    print("  - For per-course array bases (0x4408, etc.), the hit's enclosing")
    print("    function is the writer; its caller chain is the grant API.")
    print("")
    print("Cross-check: any function containing both a known cheat-DB anchor")
    print("offset (e.g. +0xC8 for flower_coin live-state) AND a save offset")
    print("(e.g. 0x894 for flower_coin save-file) is doing serialization —")
    print("a high-value chokepoint.")


main()
