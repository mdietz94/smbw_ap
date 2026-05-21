# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — M3.2 badge RE step 7.
#
# Last-attempt scan before pivoting to Wonder Seeds.  The badge system
# has four acquisition contexts visible in the strings:
#   - BadgeShop      (you buy badges)
#   - BadgeChallenge (you earn a badge from a challenge course)
#   - BadgeHouse     (special building that grants badges?)
#   - BadgeMedley    (a multi-badge / variety mode)
#
# Each must contain a code path that ends with "add badge to player's
# collection".  Hopefully that "add" call is a SHARED helper across at
# least two of these contexts — a `bl <SAME_FN>` pattern appearing in
# the Shop AND Challenge AND House code paths.
#
# This script:
#   1. Finds xrefs to every "BadgeShop" / "BadgeChallenge" /
#      "BadgeHouse" / "BadgeMedley" / "BadgeChallengeIntro" /
#      "BadgeChallengeCourse" string occurrence in the binary.
#   2. For each xref in a function: prints a window of 6 before + 16
#      after, annotating each `bl` target with the target function
#      name when known.
#   3. For each xref NOT in a function (likely registry / enum
#      entries): dumps 4 qwords around it.
#
# After running, scan the `bl` annotations in the output for a target
# function that appears under multiple different acquisition contexts.
# That target is the most likely shared grant helper.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

# Known addresses of each badge-acquisition string from the strings dump.
TARGETS = [
    ("BadgeShop",            0x71028bcb95),
    ("BadgeShop-alt-1",      0x7103478f8d),
    ("BadgeShop-alt-2",      0x7103487e60),
    ("BadgeChallenge",       0x710347da18),
    ("BadgeChallenge-alt",   0x7103489f7f),
    ("BadgeHouse",           0x7103489faa),
    ("BadgeMedley",          0x7103489fe9),
    ("BadgeChallengeIntro",  0x71028e3865),
    ("BadgeChallengeCourse", 0x7102916080),
    ("_BadgeHouse",          0x71028f6f3f),
]

INSN_BEFORE_XREF = 6
INSN_AFTER_XREF  = 16

# For non-function xrefs, qwords on each side of the site.
QWORDS_BEFORE = 2
QWORDS_AFTER  = 4


def addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def get_label_for_target(target_int):
    # Cheap reject for values that can't be a valid NSO address.
    if target_int <= 0 or target_int < 0x7100000000:
        return ""
    if target_int > 0x7200000000:
        # Beyond our binary's address space — treat as random data.
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
    # String? — wrap getByte in a defensive try since random qwords
    # may resolve to unmapped memory.
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
    elif mnem == "adrp":
        refs = list(ins.getReferencesFrom())
        for r in refs:
            try:
                tlabel = get_label_for_target(r.getToAddress().getOffset())
                if tlabel:
                    note = "    ; ref -> %s" % tlabel
                    break
            except Exception:
                pass
    print("%s %s%s" % (prefix, ins, note))


def dump_disasm_window(site_int):
    listing = currentProgram.getListing()
    site = addr(site_int)

    before = []
    cur = listing.getInstructionBefore(site)
    while cur is not None and len(before) < INSN_BEFORE_XREF:
        before.append(cur)
        cur = cur.getPrevious()
    before.reverse()

    at = listing.getInstructionAt(site) or listing.getInstructionContaining(site)

    after = []
    cur = at.getNext() if at is not None else None
    while cur is not None and len(after) < INSN_AFTER_XREF:
        after.append(cur)
        cur = cur.getNext()

    for ins in before:
        emit_with_annotation(ins)
    if at is not None:
        emit_with_annotation(at, marker="==> ")
    else:
        print("    ==> (no instruction at site)")
    for ins in after:
        emit_with_annotation(ins)


def dump_qword_context(site_int):
    memory = currentProgram.getMemory()
    start_int = site_int - QWORDS_BEFORE * 8
    end_int = site_int + QWORDS_AFTER * 8
    for off_int in range(start_int, end_int, 8):
        a = addr(off_int)
        bs = []
        ok = True
        for i in range(8):
            try:
                bs.append(memory.getByte(a.add(i)) & 0xff)
            except Exception:
                ok = False
                break
        if not ok:
            print("    0x%x: (unreadable)" % off_int)
            continue
        v = 0
        for k in range(8):
            v |= bs[k] << (k * 8)
        label = get_label_for_target(v) if v != 0 else ""
        marker = " <--" if off_int == site_int else ""
        annotation = ("  ; %s" % label) if label else ""
        print("    0x%x: 0x%016x%s%s" % (off_int, v, marker, annotation))


def process_target(label, addr_int):
    print("")
    print("===== %s @ 0x%x =====" % (label, addr_int))
    a = addr(addr_int)
    ref_mgr = currentProgram.getReferenceManager()
    fn_mgr = currentProgram.getFunctionManager()

    refs = list(ref_mgr.getReferencesTo(a))
    if not refs:
        print("  (no xrefs)")
        return

    in_fn = []
    not_in_fn = []
    for r in refs:
        src = r.getFromAddress()
        fn = fn_mgr.getFunctionContaining(src)
        if fn is None:
            not_in_fn.append(r)
        else:
            in_fn.append((r, fn))

    print("  %d xref(s): %d in-function, %d data/unanalyzed" %
          (len(refs), len(in_fn), len(not_in_fn)))

    for r, fn in in_fn:
        print("")
        print("  ---- xref %s in %s @ %s ----" %
              (r.getFromAddress(), fn.getName(), fn.getEntryPoint()))
        dump_disasm_window(r.getFromAddress().getOffset())

    for r in not_in_fn:
        print("")
        print("  ---- xref %s (data / unanalyzed) ----" % r.getFromAddress())
        dump_qword_context(r.getFromAddress().getOffset())


def main():
    print("SMBW Archipelago: find_badge_acquisition_paths.py")
    for label, addr_int in TARGETS:
        process_target(label, addr_int)
    print("")
    print("Done.")


main()
