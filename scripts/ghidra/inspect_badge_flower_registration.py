# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — M3.2 badge RE step 6.
#
# The previous script found exactly ONE xref to the "BadgeFlower"
# string, deep inside a large function (FUN_71007c031c @ 0x71007c031c,
# xref at 0x71007c0760 — offset 0x444 / ~273 instructions into the
# body).  This pattern strongly suggests a class-registration chain:
# a long function that registers EVERY actor type at startup, calling
# RegisterActor("<Name>", <ctor_function_ptr>) once per actor.
#
# This script:
#   1. Prints the prologue of FUN_71007c031c (8 insns) for context.
#   2. Prints 20 instructions before and 40 after the xref site, so
#      we can see the BadgeFlower registration block.  We'll spot
#      the constructor / factory function pointer being loaded next
#      to the class-name string, and the RegisterActor call that
#      consumes both.
#   3. For every `bl` callsite in the window, resolves the target
#      function name if Ghidra knows it.
#   4. For every `adrp + add` pair, attempts to recover the loaded
#      address and resolve it (string contents OR function name).

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

FN_ENTRY = 0x71007c031c
XREF_SITE = 0x71007c0760

INSN_BEFORE_XREF = 20
INSN_AFTER_XREF  = 40
PROLOGUE_INSNS = 8


def addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def get_label_for_target(target_int):
    """Best-effort: if target_int is a function entry, return its name.
    If it points inside a function, return that.  Otherwise try to read
    a C-string from there.  Returns short label or empty string."""
    if target_int <= 0 or target_int < 0x7100000000:
        return ""
    fn_mgr = currentProgram.getFunctionManager()
    try:
        a = addr(target_int)
    except Exception:
        return ""
    fn = fn_mgr.getFunctionContaining(a)
    if fn is not None:
        if fn.getEntryPoint().getOffset() == target_int:
            return "fn:%s" % fn.getName()
        else:
            return "in-fn:%s+0x%x" % (
                fn.getName(),
                target_int - fn.getEntryPoint().getOffset())
    # Maybe a string?
    memory = currentProgram.getMemory()
    bs = bytearray()
    for i in range(64):
        try:
            b = memory.getByte(a.add(i)) & 0xff
        except Exception:
            break
        if b == 0 or b < 0x20 or b > 0x7e:
            if b == 0 and len(bs) >= 3:
                return "str:%r" % bs.decode("ascii", "replace")
            break
        bs.append(b)
    return ""


def emit_with_annotation(ins, marker=""):
    """Print one instruction with a one-line trailing annotation if it
    has a recognizable bl target or adrp+add-style address load."""
    line = "%s     %s%s" % (marker if marker else "    ", ins, "")
    mnem = ins.getMnemonicString().lower()
    note = ""
    if mnem in ("bl", "b") and ins.getNumOperands() >= 1:
        try:
            target = ins.getAddress(0)  # branch target
            if target is not None:
                tlabel = get_label_for_target(target.getOffset())
                if tlabel:
                    note = "    ; -> %s" % tlabel
        except Exception:
            pass
    elif mnem == "adrp":
        # adrp loads a page; the next `add` completes it. We can read
        # the resolved address from Ghidra's reference manager if
        # auto-analyzer worked it out.
        refs = list(ins.getReferencesFrom())
        for r in refs:
            try:
                tlabel = get_label_for_target(r.getToAddress().getOffset())
                if tlabel:
                    note = "    ; ref -> %s" % tlabel
                    break
            except Exception:
                pass
    print("%s%s" % (line, note))


def main():
    print("SMBW Archipelago: inspect_badge_flower_registration.py")
    listing = currentProgram.getListing()

    # 1. Prologue.
    print("")
    print("===== prologue of FUN_%x (%d insns) =====" %
          (FN_ENTRY, PROLOGUE_INSNS))
    ins = listing.getInstructionAt(addr(FN_ENTRY))
    count = 0
    while ins is not None and count < PROLOGUE_INSNS:
        emit_with_annotation(ins)
        ins = ins.getNext()
        count += 1

    # 2. Window around the xref.
    print("")
    print("===== window around xref site 0x%x (-%d / +%d insns) =====" %
          (XREF_SITE, INSN_BEFORE_XREF, INSN_AFTER_XREF))

    site_addr = addr(XREF_SITE)
    # Walk backward.
    before = []
    cur = listing.getInstructionBefore(site_addr)
    while cur is not None and len(before) < INSN_BEFORE_XREF:
        before.append(cur)
        cur = cur.getPrevious()
    before.reverse()

    at = listing.getInstructionAt(site_addr) or listing.getInstructionContaining(site_addr)

    after = []
    cur = at.getNext() if at is not None else None
    while cur is not None and len(after) < INSN_AFTER_XREF:
        after.append(cur)
        cur = cur.getNext()

    for ins in before:
        emit_with_annotation(ins)
    if at is not None:
        emit_with_annotation(at, marker="==>")
    else:
        print("==>  (no instruction at site)")
    for ins in after:
        emit_with_annotation(ins)

    print("")
    print("Done.")


main()
