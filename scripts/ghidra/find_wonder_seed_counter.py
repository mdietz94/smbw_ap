# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — M3.3 step 1.
#
# Pivoting from M3.2 (badges deferred to save-diff) to M3.3 Wonder
# Seeds.  Concrete anchor: HamletDuFromage's "[seed]" cheat patches
# NSO +0x12AF6C to force a Wonder Seed counter read to 100.  That
# means +0x12AF6C is an INSTRUCTION (not a data field) — typically a
# load like `ldr w?, [base + offset]` that reads the actual counter.
#
# This script:
#   1. Identifies the function containing +0x12AF6C.
#   2. Dumps 12 instructions before and 24 after the patched site so
#      we can see the load and figure out what it reads from.
#   3. Annotates `bl` and `adrp` references the way the badge-RE
#      scripts did.
#
# After this we'll know the actual counter's data address; a second
# script will find writers to that data address (the increment
# function = our M3.3 target).

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

CHEAT_PATCH_SITE = 0x710012AF6C

INSN_BEFORE = 12
INSN_AFTER  = 24


def addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def get_label_for_target(target_int):
    if target_int <= 0 or target_int < 0x7100000000:
        return ""
    if target_int > 0x7200000000:
        return ""
    fn_mgr = currentProgram.getFunctionManager()
    try:
        a = addr(target_int)
    except Exception:
        return ""
    try:
        fn = fn_mgr.getFunctionContaining(a)
    except Exception:
        fn = None
    if fn is not None:
        if fn.getEntryPoint().getOffset() == target_int:
            return "fn:%s" % fn.getName()
        else:
            return "in-fn:%s+0x%x" % (
                fn.getName(),
                target_int - fn.getEntryPoint().getOffset())
    memory = currentProgram.getMemory()
    bs = bytearray()
    for i in range(64):
        try:
            b = memory.getByte(a.add(i)) & 0xff
        except Exception:
            return ""
        if b == 0:
            if len(bs) >= 3:
                return "str:%r" % bs.decode("ascii", "replace")
            return ""
        if b < 0x20 or b > 0x7e:
            return ""
        bs.append(b)
    return ""


def emit_with_annotation(ins, marker=""):
    prefix = marker if marker else "    "
    mnem = ins.getMnemonicString().lower()
    note = ""
    if mnem in ("bl", "b", "b.ne", "b.eq") and ins.getNumOperands() >= 1:
        try:
            target = ins.getAddress(0)
            if target is not None:
                tlabel = get_label_for_target(target.getOffset())
                if tlabel:
                    note = "    ; -> %s" % tlabel
        except Exception:
            pass
    elif mnem in ("adrp", "ldr"):
        refs = list(ins.getReferencesFrom())
        for r in refs:
            try:
                tlabel = get_label_for_target(r.getToAddress().getOffset())
                if tlabel:
                    note = "    ; ref -> %s" % tlabel
                    break
            except Exception:
                pass
        # Also: if the instruction has a resolved memory operand, surface it.
        if not note:
            for r in refs:
                try:
                    target_int = r.getToAddress().getOffset()
                    if 0x7100000000 <= target_int < 0x7200000000:
                        note = "    ; ref -> 0x%x" % target_int
                        break
                except Exception:
                    pass
    print("%s %s%s" % (prefix, ins, note))


def main():
    print("SMBW Archipelago: find_wonder_seed_counter.py")
    site = addr(CHEAT_PATCH_SITE)
    listing = currentProgram.getListing()
    fn_mgr = currentProgram.getFunctionManager()

    fn = fn_mgr.getFunctionContaining(site)
    if fn is None:
        print("WARN: site 0x%x is not inside any analyzed function." %
              CHEAT_PATCH_SITE)
        print("      (Try Analysis -> Auto Analyze to deepen coverage,")
        print("       or read on — the disasm window will still print.)")
    else:
        print("Containing function: %s @ %s" %
              (fn.getName(), fn.getEntryPoint()))

    print("")
    print("===== disasm window around 0x%x (-%d / +%d insns) =====" %
          (CHEAT_PATCH_SITE, INSN_BEFORE, INSN_AFTER))

    # Walk back.
    before = []
    cur = listing.getInstructionBefore(site)
    while cur is not None and len(before) < INSN_BEFORE:
        before.append(cur)
        cur = cur.getPrevious()
    before.reverse()

    at = listing.getInstructionAt(site) or listing.getInstructionContaining(site)

    after = []
    cur = at.getNext() if at is not None else None
    while cur is not None and len(after) < INSN_AFTER:
        after.append(cur)
        cur = cur.getNext()

    for ins in before:
        emit_with_annotation(ins)
    if at is not None:
        emit_with_annotation(at, marker="==> ")
    else:
        print("    ==> (no instruction at site — try defining as code first)")
    for ins in after:
        emit_with_annotation(ins)

    print("")
    print("Done.")
    print("")
    print("What to look for in the output:")
    print("  - The instruction at the ==>  marker is the one the cheat")
    print("    overwrites with `mov w?, #0x64` (= 100).  Original is")
    print("    likely an ldr/ldrb/ldrh — that's the WONDER SEED COUNTER READ.")
    print("  - Its operand format `[Xn, #offset]` tells us the data field")
    print("    location.  Either:")
    print("      a) `[Xreg + small_imm]` — the field is at a small offset from")
    print("         an object pointer; trace Xreg back to see the object")
    print("         class (likely a save-data or per-world-state struct).")
    print("      b) `[Xreg, #large_imm]` or PC-relative load — gives us")
    print("         the data address directly; from that address we can find")
    print("         writers (the increment function = M3.3 target).")


main()
