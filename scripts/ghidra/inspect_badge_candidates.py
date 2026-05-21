# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — M3.2 badge RE step 3.
#
# After inspect_badge_dispatch.py we identified three CANDIDATE badge-
# action functions inside FUN_7101b1fb6c's test-harness body:
#
#   FUN_7101a592fc  — suspected "give badge" (called before the
#                     GiveBadgeIdOnCourseClear debug-name)
#   FUN_7101b204a8  — suspected "unlock badge" (called before
#                     UnlockBadgeIdOnCourseClear)
#   FUN_7101843e0c  — third candidate, called with a debug-name string
#                     at 0x71028af860 (content TBD)
#
# This script answers three questions for each candidate:
#   (1) Prologue: signature hints (which arg registers are saved /
#       used) and trampoline-safety (no PC-relative loads in first 5).
#   (2) Xref count + first few callers: distinguishes a "real" game
#       API (heavily called from gameplay paths) from a "test-only"
#       probe (only called from FUN_7101b1fb6c-like harnesses).
#   (3) For FUN_7101843e0c: read the debug name at 0x71028af860 to
#       know what operation it represents.
#
# Also dumps the entry-to-first-use slice of FUN_7101b1fb6c so we can
# see how x20 gets set up — that tells us the actual calling
# convention for the candidates.
#
# Run from Ghidra: Window -> Script Manager -> New Script -> Python.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

CANDIDATES = [
    ("FUN_7101a592fc (suspected Give)",   0x7101a592fc),
    ("FUN_7101b204a8 (suspected Unlock)", 0x7101b204a8),
    ("FUN_7101843e0c (third candidate)",  0x7101843e0c),
]

# Mystery string referenced near the third candidate.
THIRD_DEBUG_STRING_ADDR = 0x71028af860

# The test harness — we want to see how x20 is set up early.
HARNESS_FN = 0x7101b1fb6c
HARNESS_INSN_COUNT = 60

PROLOGUE_INSN_COUNT = 20
MAX_CALLERS_TO_PRINT = 10


def addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def dump_prologue(fn_int, n=PROLOGUE_INSN_COUNT):
    """Print first n instructions of the function at fn_int."""
    listing = currentProgram.getListing()
    a = addr(fn_int)
    ins = listing.getInstructionAt(a)
    count = 0
    while ins is not None and count < n:
        print("    %s" % ins)
        ins = ins.getNext()
        count += 1


def dump_xref_callers(fn_int):
    """Print all xref sources to fn_int with their containing-function
    info — tells us if the candidate is called from real game code or
    only from test harnesses."""
    ref_mgr = currentProgram.getReferenceManager()
    fn_mgr = currentProgram.getFunctionManager()
    a = addr(fn_int)
    refs = list(ref_mgr.getReferencesTo(a))
    # Filter to CALL-type references (skip data references, fall-throughs).
    call_refs = []
    for r in refs:
        rt = r.getReferenceType()
        # ARM64 calls show up as UNCONDITIONAL_CALL / CONDITIONAL_CALL /
        # COMPUTED_CALL.  Conservative: take anything with `isCall()`.
        if hasattr(rt, "isCall") and rt.isCall():
            call_refs.append(r)

    print("    total xrefs:     %d" % len(refs))
    print("    of which calls:  %d" % len(call_refs))
    if not call_refs:
        # Some Ghidra setups don't classify branches as calls reliably;
        # fall back to showing all refs in that case.
        call_refs = refs
        print("    (showing all xrefs as call classification is empty)")

    shown = 0
    for r in call_refs:
        if shown >= MAX_CALLERS_TO_PRINT:
            print("    ... (%d more not shown)" % (len(call_refs) - shown))
            break
        src = r.getFromAddress()
        rt = r.getReferenceType().getName()
        fn = fn_mgr.getFunctionContaining(src)
        fn_str = "(not in a function)"
        if fn is not None:
            fn_str = "%s @ %s" % (fn.getName(), fn.getEntryPoint())
        print("    - %s (%s) in %s" % (src, rt, fn_str))
        shown += 1


def read_cstring(addr_int, max_len=128):
    """Read a null-terminated ASCII string from memory starting at
    addr_int."""
    memory = currentProgram.getMemory()
    a = addr(addr_int)
    bs = bytearray()
    for i in range(max_len):
        try:
            b = memory.getByte(a.add(i)) & 0xff
        except Exception:
            break
        if b == 0:
            break
        bs.append(b)
    return bs.decode("ascii", "replace")


def main():
    print("SMBW Archipelago: inspect_badge_candidates.py")

    # (3) Mystery string for the third candidate first — quick win.
    print("")
    print("===== debug name @ 0x%x =====" % THIRD_DEBUG_STRING_ADDR)
    s = read_cstring(THIRD_DEBUG_STRING_ADDR)
    print("  content: %r" % s)

    # (1) + (2) For each candidate.
    for label, fn_int in CANDIDATES:
        print("")
        print("===== %s (0x%x) =====" % (label, fn_int))
        print("  -- prologue (%d insns) --" % PROLOGUE_INSN_COUNT)
        dump_prologue(fn_int)
        print("  -- callers --")
        dump_xref_callers(fn_int)

    # (extra) Entry-to-first-string-use slice of FUN_7101b1fb6c.
    # Tells us where x20 (the candidate-fn arg) is set up.
    print("")
    print("===== test-harness FUN_%x setup (%d insns from entry) =====" %
          (HARNESS_FN, HARNESS_INSN_COUNT))
    dump_prologue(HARNESS_FN, n=HARNESS_INSN_COUNT)

    print("")
    print("Done.")


main()
