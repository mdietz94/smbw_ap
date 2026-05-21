# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — M3.2 badge RE step 2.
#
# Follow-up to find_badge_functions.py.  Both badge debug-name strings
# (GiveBadgeIdOnCourseClear, UnlockBadgeIdOnCourseClear) are referenced
# from the SAME function FUN_7101b1fb6c, and ALSO from two data-table
# entries 0x68 bytes apart.  This script dumps:
#
# 1. ~12 instructions of disassembly on either side of each in-function
#    xref site — so we can see what the function does around each name
#    reference (calls, branches, register usage).
# 2. 64 bytes around each data-table entry, with aligned 8-byte qword
#    interpretation — so we can tell whether each table entry is a
#    {name_ptr, fn_ptr, ...} struct.
#
# Run from Ghidra: Window -> Script Manager -> File menu -> New Script ->
# Python.  Paste contents, save, hit ▶.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

# Sites from find_badge_functions.py output (2026-05-20):
IN_FUNCTION_XREFS = [
    ("GiveBadgeIdOnCourseClear-load",   0x7101b1fdf4),
    ("UnlockBadgeIdOnCourseClear-load", 0x7101b1fe2c),
]

DATA_TABLE_XREFS = [
    ("near GiveBadgeIdOnCourseClear-table-entry",   0x710070f248),
    ("near UnlockBadgeIdOnCourseClear-table-entry", 0x710070f2b0),
]

# Window sizes.
INSN_BEFORE = 8       # instructions before the xref site
INSN_AFTER  = 16      # instructions after
TABLE_WINDOW = 32     # bytes on each side of a table-entry address


def addr(n):
    """Convert a plain Python int to a Ghidra Address."""
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def dump_disasm_window(label, site_int):
    print("")
    print("===== disasm around %s (0x%x) =====" % (label, site_int))
    listing = currentProgram.getListing()
    fn_mgr = currentProgram.getFunctionManager()
    site = addr(site_int)
    fn = fn_mgr.getFunctionContaining(site)
    fn_label = "(not in a function)"
    if fn is not None:
        fn_label = "%s @ %s" % (fn.getName(), fn.getEntryPoint())
    print("  containing function: %s" % fn_label)
    print("")

    # Walk backward by getInstructionBefore until we hit INSN_BEFORE count
    # or run out (e.g. before function start).
    before = []
    cur = listing.getInstructionBefore(site)
    while cur is not None and len(before) < INSN_BEFORE:
        before.append(cur)
        cur = cur.getPrevious()
    before.reverse()

    # The instruction AT the site (or the one Ghidra found there).
    at = listing.getInstructionAt(site)
    if at is None:
        at = listing.getInstructionContaining(site)

    # And forward.
    after = []
    cur = at.getNext() if at is not None else None
    while cur is not None and len(after) < INSN_AFTER:
        after.append(cur)
        cur = cur.getNext()

    for ins in before:
        print("       %s" % ins)
    if at is not None:
        print("  ==>  %s" % at)
    else:
        print("  ==>  (no instruction at site)")
    for ins in after:
        print("       %s" % ins)


def dump_table_context(label, site_int):
    print("")
    print("===== bytes around %s (0x%x) =====" % (label, site_int))
    memory = currentProgram.getMemory()
    site = addr(site_int)
    start_int = site_int - TABLE_WINDOW
    print("  range: 0x%x..0x%x (%d bytes; site %+d from window start)" %
          (start_int, start_int + 2 * TABLE_WINDOW, 2 * TABLE_WINDOW, TABLE_WINDOW))
    print("")

    # 16 bytes per row, with aligned 8-byte LE qword interpretation
    # appended to each row.
    rows = (2 * TABLE_WINDOW) // 16
    for row in range(rows):
        row_addr_int = start_int + row * 16
        row_addr = addr(row_addr_int)
        bs = []
        for i in range(16):
            try:
                bs.append(memory.getByte(row_addr.add(i)) & 0xff)
            except Exception:
                bs.append(-1)
        hex_str = " ".join("%02x" % b if b >= 0 else "??" for b in bs)
        ascii_str = "".join(chr(b) if 0 < b and 0x20 <= b < 0x7f else "."
                            for b in bs)
        # Two aligned 8-byte LE qwords per row.
        qwords = []
        for q in range(2):
            base = q * 8
            valid = all(b >= 0 for b in bs[base:base + 8])
            if not valid:
                qwords.append("??")
                continue
            v = 0
            for k in range(8):
                v |= bs[base + k] << (k * 8)
            qwords.append("0x%016x" % v)
        marker = " <-- site" if row_addr_int <= site_int < row_addr_int + 16 else ""
        print("  0x%x  %s  |%s|  q0=%s  q1=%s%s" %
              (row_addr_int, hex_str, ascii_str, qwords[0], qwords[1], marker))


def main():
    print("SMBW Archipelago: inspect_badge_dispatch.py")
    for label, site in IN_FUNCTION_XREFS:
        dump_disasm_window(label, site)
    for label, site in DATA_TABLE_XREFS:
        dump_table_context(label, site)
    print("")
    print("Done.")


main()
