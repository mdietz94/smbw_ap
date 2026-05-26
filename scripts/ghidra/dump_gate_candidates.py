# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — Wonder Seed gate RE
# (2026-05-26 iteration 2).
#
# For each candidate function in CANDIDATES and each cheat anchor in
# CHEAT_ANCHORS, dump everything we need to evaluate gate-check
# semantics WITHOUT having to navigate Ghidra by hand:
#
#   1. Decompile body (DecompInterface)
#   2. Prologue (first 8 instructions, for trampoline safety check)
#   3. Direct callers (classified as function / vtable / data / orphan)
#   4. (Cheat anchors only) the 6 instructions around the anchor with
#      bytes, to characterize the patch shape (MOV W8,#1 / B over fail).
#
# Output is one block per candidate; paste back into chat for the next
# iteration of analysis.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

# (nso_offset_full, short_label).  Top candidates from
# walk_reader_compare_sites.py (2026-05-26).  Ordered by priority.
CANDIDATES = [
    (0x71006c12a0, "#11 score=80 cmp #2 b.lt STRICT GATE SHAPE (Reader C, hash=0x42ffdf00)"),
    (0x7100689880, "#9  score=80 cmp #5 b.hi (Reader B, hash=0xe237fbc6) -- PI threshold"),
    (0x710066e548, "#7  score=80 cmp #12 b.eq (Reader B, hash=?) -- PI threshold; HASH MISSING"),
    (0x710064d6c4, "#2  score=80 cmp #9 b.hi (Reader A, hash=0xf4d9942a) -- W2 threshold"),
    (0x7101babf2c, "#10 score=80 cmp #3 b.ne (Reader C, hash=0x65634476) -- W1 threshold"),
    (0x7100884040, "Lock/Unlock string carrier -- NOT in walker top 30; may inline gate"),
    (0x71005572c0, "non-PR reader of [x19,#0x47c] (world_mother_seed) -- reads twice"),
    (0x7101c4dc90, "#15 score=70 TWO sites both cmp #10 with DIFFERENT hashes -- possible per-world dispatch"),
    (0x7100383418, "#1  score=80 cmp #2 b.ne; SECOND hit cmp #25 (W6!) via 0x46d90c82"),
    (0x7101b5bcd0, "#3  score=80 cmp #2 b.hi; multiple cmps"),
    (0x71004853e4, "#18 score=65 cmp #8"),
    (0x7101a59d24, "rank ~14 score=70 cmp #8 (PI threshold)"),
]

# Cheat DB anchors (per docs/static-analysis-findings.md lines ~217-230).
CHEAT_ANCHORS = [
    (0x7100000000 + 0x48A528, "Fast-Travel anchor"),
    (0x7100000000 + 0x5D9F58, "Fast-Travel anchor"),
    (0x7100000000 + 0x935E10, "Fast-Travel anchor"),
    (0x7100000000 + 0x48A818, "Top-of-Flag anchor"),
]

# Deeper hash-reconstruction targets (sites the walker's reconstructor
# failed on).  We do a wider backwalk and look for movz/movk pairs
# split further apart than BACKWALK_INSNS=20.
HASH_RECOVERY_SITES = [
    (0x710066e5e8, "FUN_710066e548 cmp #12"),
    (0x710074e708, "FUN_710074e6cc cmp #2"),
    (0x71005c8000, "FUN_71005c7fcc cmp #1"),
    (0x71005f45a8, "FUN_71005f4550 cmp #1"),
]

# How many prologue instructions to dump per function.
PROLOGUE_INSNS = 8

# Vtable region heuristic (CLAUDE.md: Nerve vtables live in these
# 64KB windows).
VTABLE_REGIONS = [
    (0x7103340000, 0x7103350000),
    (0x71033f0000, 0x7103400000),
    (0x71034b0000, 0x71034c0000),
]


def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def _in_vtable_region(off):
    for lo, hi in VTABLE_REGIONS:
        if lo <= off < hi:
            return True
    return False


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


def _dest_reg_name(ins):
    try:
        objs = ins.getOpObjects(0)
    except Exception:
        return None
    for o in objs:
        if o is None:
            continue
        if o.__class__.__name__ == "Register":
            return o.getName().lower()
    return None


def _w_to_x(reg_name):
    if not reg_name:
        return None
    n = reg_name.lower()
    if n.startswith("w") and n[1:].isdigit():
        return "x" + n[1:]
    return n


def deep_hash_reconstruct(call_site, max_insns=64):
    """Wider backwalk than the standard reconstructor.  Tracks the
    value loaded into w2/x2 across mov/movz/movk chains up to 64
    instructions back.  Handles split halves where movk and movz are
    far apart."""
    listing = currentProgram.getListing()
    # State per register: value or None if undetermined.
    # We track x0/x1/x2/x3 and aliases.
    val = {"x0": None, "x1": None, "x2": None, "x3": None}
    cur = listing.getInstructionBefore(call_site)
    walked = 0
    # Walk backward, applying forward semantics in reverse?  Simpler:
    # walk backward, record the LAST write to each reg, then return
    # whatever is in x2 at the call.  Since we encounter writes in
    # reverse, the FIRST one we see is the most recent.
    seen = {"x0": False, "x1": False, "x2": False, "x3": False}
    # Pending state for movk: we may see movk first (backward), then
    # a movz further back.  Track per-register.
    pending = {"x0": None, "x1": None, "x2": None, "x3": None}
    while cur is not None and walked < max_insns:
        mnem = cur.getMnemonicString().lower()
        dst = _w_to_x(_dest_reg_name(cur))
        if dst in val and not seen[dst]:
            if mnem == "mov":
                imm = _imm_at_op(cur, 1)
                if imm is not None:
                    shift = _lsl_shift_from_insn(cur)
                    v = (imm << shift) & 0xFFFFFFFF
                    if pending[dst] is not None:
                        v |= pending[dst]
                    val[dst] = v
                    seen[dst] = True
                else:
                    # mov w?, wN -- alias chasing; mark unknown for now
                    seen[dst] = True
            elif mnem == "movz":
                imm = _imm_at_op(cur, 1)
                if imm is not None:
                    shift = _lsl_shift_from_insn(cur)
                    v = (imm << shift) & 0xFFFFFFFF
                    if pending[dst] is not None:
                        v |= pending[dst]
                    val[dst] = v
                    seen[dst] = True
            elif mnem == "movk":
                imm = _imm_at_op(cur, 1)
                if imm is not None:
                    shift = _lsl_shift_from_insn(cur)
                    v = (imm << shift) & 0xFFFFFFFF
                    pending[dst] = (pending[dst] or 0) | v
                    # Don't mark seen; expect a movz earlier (backward)
                    # to complete the constant.
            elif mnem == "movn":
                imm = _imm_at_op(cur, 1)
                if imm is not None:
                    shift = _lsl_shift_from_insn(cur)
                    v = (~(imm << shift)) & 0xFFFFFFFF
                    val[dst] = v
                    seen[dst] = True
            else:
                seen[dst] = True  # killed by some other write; stop tracking
        cur = cur.getPrevious()
        walked += 1
    return val


def classify_caller(src_addr, fn_mgr):
    """Classify a caller reference site.  Returns (kind, label)."""
    src_off = src_addr.getOffset()
    fn = fn_mgr.getFunctionContaining(src_addr)
    if fn is not None:
        return ("fn", "%s @ %s" % (fn.getName(), fn.getEntryPoint()))
    # Not in a function — check if it's in a vtable region.
    if _in_vtable_region(src_off):
        return ("vtable", "VTABLE @ 0x%x" % src_off)
    # In a code region but no function?
    listing = currentProgram.getListing()
    ins = listing.getInstructionAt(src_addr)
    if ins is not None:
        return ("orphan", "orphan code @ 0x%x  %s" % (src_off, ins))
    # Data reference.
    return ("data", "data ref @ 0x%x" % src_off)


def get_callers(fn):
    """Return [(kind, label, src_addr)] for every reference to fn's
    entry point."""
    ref_mgr = currentProgram.getReferenceManager()
    fn_mgr = currentProgram.getFunctionManager()
    refs = list(ref_mgr.getReferencesTo(fn.getEntryPoint()))
    out = []
    for r in refs:
        src = r.getFromAddress()
        ref_type = str(r.getReferenceType())
        kind, label = classify_caller(src, fn_mgr)
        out.append((kind, label, src, ref_type))
    return out


def dump_prologue(fn, n=PROLOGUE_INSNS):
    """Return [(addr, insn_str, bytes_hex)] for the first n insns."""
    listing = currentProgram.getListing()
    cur = listing.getInstructionAt(fn.getEntryPoint())
    out = []
    while cur is not None and len(out) < n:
        addr = cur.getAddress()
        # Get bytes.
        try:
            bs = cur.getBytes()
            bs_hex = " ".join("%02x" % (b & 0xff) for b in bs)
        except Exception:
            bs_hex = "?"
        out.append((addr, str(cur), bs_hex))
        cur = cur.getNext()
    return out


def has_pc_relative_in_prologue(prologue_rows):
    """Returns list of (idx, insn_str) for any PC-relative op in the
    first 5 instructions (the inline-hook patch window)."""
    UNSAFE = ("adrp", "adr", "b", "bl", "b.", "cbz", "cbnz", "tbz",
              "tbnz", "ret", "br", "blr")
    flagged = []
    for i, (addr, ins, _) in enumerate(prologue_rows[:5]):
        s = ins.lower().strip()
        first = s.split()[0] if s.split() else ""
        if first in UNSAFE or first.startswith("b."):
            flagged.append((i, ins))
        # ldr literal: `ldr w?, =<imm>` or `ldr w?, [PC-rel]`
        if first == "ldr" and ("0x71" in s or "[pc" in s):
            flagged.append((i, ins + "  (likely PC-relative literal)"))
    return flagged


def get_decompile_text(fn, timeout_sec=30):
    """Run the Ghidra decompiler on `fn` and return its C output as a
    list of lines.  Returns None on failure."""
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
        c = decomp.getC()
        return c.splitlines()
    finally:
        di.dispose()


def dump_candidate(off_full, label):
    print("")
    print("================================================================")
    print("=== CANDIDATE 0x%x  --  %s" % (off_full, label))
    print("================================================================")
    fn_mgr = currentProgram.getFunctionManager()
    a = _addr(off_full)
    fn = fn_mgr.getFunctionAt(a)
    if fn is None:
        fn = fn_mgr.getFunctionContaining(a)
    if fn is None:
        print("  [no function at 0x%x]" % off_full)
        return

    print("  Function: %s @ %s" % (fn.getName(), fn.getEntryPoint()))
    sig = fn.getSignature()
    print("  Signature: %s" % sig)
    body_size = fn.getBody().getNumAddresses()
    print("  Body size: %d bytes" % body_size)

    print("")
    print("  -- Prologue (first %d insns, with bytes) --" % PROLOGUE_INSNS)
    prologue = dump_prologue(fn)
    for addr, ins, bs in prologue:
        print("    %s   %-20s  %s" % (addr, bs, ins))

    flagged = has_pc_relative_in_prologue(prologue)
    if flagged:
        print("  PROLOGUE TRAMPOLINE UNSAFE: %d PC-relative op(s) in first 5 insns" % len(flagged))
        for i, s in flagged:
            print("    [insn %d] %s" % (i, s))
    else:
        print("  Prologue trampoline-safe: no PC-relative ops in first 5 insns")

    print("")
    print("  -- Direct callers --")
    callers = get_callers(fn)
    n_fn = sum(1 for k, _, _, _ in callers if k == "fn")
    n_vt = sum(1 for k, _, _, _ in callers if k == "vtable")
    n_or = sum(1 for k, _, _, _ in callers if k == "orphan")
    n_dt = sum(1 for k, _, _, _ in callers if k == "data")
    print("  Total refs: %d  (fn=%d, vtable=%d, orphan=%d, data=%d)" %
          (len(callers), n_fn, n_vt, n_or, n_dt))
    # Show vtable hits first (most interesting), then function callers.
    for kind in ("vtable", "fn", "orphan", "data"):
        for k, label, src, rt in callers:
            if k != kind:
                continue
            print("    [%s][%s]  %s" % (kind, rt, label))

    print("")
    print("  -- Decompile --")
    c_lines = get_decompile_text(fn)
    for ln in c_lines:
        print("    %s" % ln)


def dump_cheat_anchor(off_full, label):
    print("")
    print("================================================================")
    print("=== CHEAT ANCHOR 0x%x  --  %s" % (off_full, label))
    print("================================================================")
    fn_mgr = currentProgram.getFunctionManager()
    listing = currentProgram.getListing()
    a = _addr(off_full)

    # Print 8 instructions centered on the anchor.
    cur = listing.getInstructionAt(a)
    if cur is None:
        # Try previous instruction (anchor may be mid-instruction
        # if the assembly hasn't been disassembled).
        cur = listing.getInstructionAfter(_addr(off_full - 16))
    if cur is None:
        print("  [no instruction near 0x%x]" % off_full)
        return

    # Back up 3 instructions.
    look = cur
    for _ in range(3):
        prev = look.getPrevious()
        if prev is None:
            break
        look = prev

    print("  -- Disasm window around anchor (anchor marked with '>') --")
    for i in range(10):
        if look is None:
            break
        marker = ">" if look.getAddress().equals(a) else " "
        try:
            bs = look.getBytes()
            bs_hex = " ".join("%02x" % (b & 0xff) for b in bs)
        except Exception:
            bs_hex = "?"
        print("  %s %s   %-20s  %s" %
              (marker, look.getAddress(), bs_hex, look))
        look = look.getNext()

    fn = fn_mgr.getFunctionContaining(a)
    if fn is None:
        print("  [anchor not inside any function]")
        return
    print("")
    print("  Anchor inside function: %s @ %s" % (fn.getName(), fn.getEntryPoint()))

    print("")
    print("  -- Direct callers of containing fn --")
    callers = get_callers(fn)
    for kind in ("vtable", "fn", "orphan", "data"):
        for k, label, src, rt in callers[:20]:
            if k != kind:
                continue
            print("    [%s][%s]  %s" % (kind, rt, label))
    if len(callers) > 20:
        print("    ... (%d more)" % (len(callers) - 20))

    print("")
    print("  -- Decompile of containing function --")
    c_lines = get_decompile_text(fn)
    for ln in c_lines:
        print("    %s" % ln)


def dump_hash_recovery(site_off, label):
    print("")
    print("================================================================")
    print("=== HASH RECOVERY  call_site=0x%x  --  %s" % (site_off, label))
    print("================================================================")
    a = _addr(site_off)
    listing = currentProgram.getListing()

    # Print 16 instructions before the call.
    cur = listing.getInstructionAt(a)
    if cur is None:
        print("  [no instruction at 0x%x]" % site_off)
        return
    back = []
    look = cur
    for _ in range(16):
        prev = look.getPrevious()
        if prev is None:
            break
        back.append(prev)
        look = prev
    back.reverse()
    print("  -- 16 insns before the bl --")
    for ins in back:
        try:
            bs = ins.getBytes()
            bs_hex = " ".join("%02x" % (b & 0xff) for b in bs)
        except Exception:
            bs_hex = "?"
        print("    %s   %-20s  %s" % (ins.getAddress(), bs_hex, ins))
    print("    %s   %-20s  %s  <-- the bl" % (a, "", cur))

    print("")
    print("  -- Deep reconstruction (64-insn backwalk per reg) --")
    val = deep_hash_reconstruct(a, max_insns=64)
    for reg in ("x0", "x1", "x2", "x3"):
        v = val[reg]
        if v is None:
            print("    %s: <unresolved>" % reg)
        else:
            print("    %s: 0x%08x" % (reg, v))


def main():
    print("SMBW Archipelago: dump_gate_candidates.py")
    print("  (Wonder Seed gate-check RE — iteration 2)")
    print("  candidates=%d  cheat_anchors=%d  hash_recovery_sites=%d"
          % (len(CANDIDATES), len(CHEAT_ANCHORS), len(HASH_RECOVERY_SITES)))
    print("  (decompiler invocations may take 10-30s each; total ~3-5 min)")

    for off, label in CANDIDATES:
        if monitor.isCancelled():
            break
        dump_candidate(off, label)

    print("")
    print("################################################################")
    print("####  CHEAT-DB ANCHORS  ########################################")
    print("################################################################")
    for off, label in CHEAT_ANCHORS:
        if monitor.isCancelled():
            break
        dump_cheat_anchor(off, label)

    print("")
    print("################################################################")
    print("####  HASH RECOVERY  ###########################################")
    print("################################################################")
    for off, label in HASH_RECOVERY_SITES:
        if monitor.isCancelled():
            break
        dump_hash_recovery(off, label)

    print("")
    print("Done.")
    print("")
    print("Paste the output back into chat for next-iteration analysis.")
    print("If any candidate is huge (>500 lines decompile), the iteration")
    print("agent will pre-filter to the gate-decision branch.")


main()
