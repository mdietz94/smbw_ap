# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — M3.2 badge RE step 5.
#
# Hunt for the BadgeFlower actor's vtable so we can identify its touch
# handler (the function that fires when the player picks up a badge
# flower in the world).  Strategy:
#
#   1. Find xrefs to the "BadgeFlower" class-name string.
#   2. Among xrefs that sit in code, isolate the SHORT functions
#      (heuristic: function body <= 12 instructions, ends with `ret`).
#      A short function that loads the class-name string is almost
#      certainly the actor's class-name getter (slot 0 of the vtable
#      per the SMBW Nerve / Actor convention from M1).
#   3. For each candidate getter function, find data xrefs to its
#      entry address.  Those data sites are the vtable's slot 0 — the
#      vtable lives in surrounding memory.
#   4. Dump 24 qwords (~12 slots × 2 vtables) around each data xref
#      with attempted function-name resolution per qword.  Highlight
#      slots whose values look like function pointers.
#
# After running, share the output.  The touch handler is likely
# somewhere in slots 8..20 of the vtable.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

CLASS_NAME_STRING = "BadgeFlower"
CLASS_NAME_ADDR_HINT = 0x710295d1f7  # from strings dump

# A class-name getter is typically tiny: adrp + add + ret, or wrapper
# of similar size.  Cap at 12 instructions.
SHORT_FN_INSN_LIMIT = 12

# Vtable dump window — how many qwords on each side of the data xref.
QWORDS_BEFORE = 4
QWORDS_AFTER = 16


def addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def find_string_addr(needle):
    """Search memory for a STANDALONE null-terminated occurrence of
    `needle` — i.e. the byte before the match must also be a null
    terminator (so we don't match `BadgeFlower\0` as a suffix of e.g.
    `IsCreateBadgeFlower\0`, which we hit on the first run).
    """
    pattern = bytearray()
    for c in needle:
        pattern.append(ord(c))
    pattern.append(0)
    memory = currentProgram.getMemory()
    start = memory.getMinAddress()
    while start is not None:
        found = memory.findBytes(start, bytes(pattern), None, True, monitor)
        if found is None:
            return None
        # Verify standalone: byte at -1 should be 0 (separator).
        prev_addr = found.subtract(1)
        try:
            prev = memory.getByte(prev_addr) & 0xff
        except Exception:
            # Out-of-bounds before; accept the match.
            return found
        if prev == 0:
            return found
        # Substring match — advance past this one and keep looking.
        start = found.add(1)


def is_short_function(fn):
    """Heuristic: function body has <= SHORT_FN_INSN_LIMIT instructions
    and ends with `ret`."""
    listing = currentProgram.getListing()
    ins = listing.getInstructionAt(fn.getEntryPoint())
    count = 0
    last_mnem = None
    body_end = fn.getBody().getMaxAddress()
    while ins is not None and ins.getAddress().compareTo(body_end) <= 0:
        last_mnem = ins.getMnemonicString().lower()
        count += 1
        if count > SHORT_FN_INSN_LIMIT:
            return False
        ins = ins.getNext()
    return last_mnem == "ret"


def dump_short_function(fn):
    listing = currentProgram.getListing()
    ins = listing.getInstructionAt(fn.getEntryPoint())
    body_end = fn.getBody().getMaxAddress()
    while ins is not None and ins.getAddress().compareTo(body_end) <= 0:
        print("    %s" % ins)
        ins = ins.getNext()


def dump_vtable_window(data_xref_addr_int):
    """Print qwords around a data xref address with function-name
    resolution per qword."""
    memory = currentProgram.getMemory()
    fn_mgr = currentProgram.getFunctionManager()
    start_int = data_xref_addr_int - QWORDS_BEFORE * 8
    end_int = data_xref_addr_int + QWORDS_AFTER * 8
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
            print("  0x%x: (unreadable)" % off_int)
            continue
        # Little-endian qword.
        v = 0
        for k in range(8):
            v |= bs[k] << (k * 8)
        # Resolve as function name if v points at a known function.
        fn_label = ""
        if v != 0:
            try:
                target_addr = addr(v)
                fn = fn_mgr.getFunctionContaining(target_addr)
                if fn is not None and fn.getEntryPoint().getOffset() == v:
                    fn_label = "  -> %s (function entry)" % fn.getName()
                elif fn is not None:
                    fn_label = "  -> inside %s" % fn.getName()
            except Exception:
                pass
        marker = " <-- string-ref site" if off_int == data_xref_addr_int else ""
        print("  0x%x: 0x%016x%s%s" % (off_int, v, fn_label, marker))


def main():
    print("SMBW Archipelago: find_badge_flower_vtable.py")
    string_addr = find_string_addr(CLASS_NAME_STRING)
    if string_addr is None:
        # Fall back to the hint.
        string_addr = addr(CLASS_NAME_ADDR_HINT)
        print("WARN: findBytes failed; using hint address %s" % string_addr)
    else:
        print("'%s' string found at: %s" % (CLASS_NAME_STRING, string_addr))

    ref_mgr = currentProgram.getReferenceManager()
    fn_mgr = currentProgram.getFunctionManager()

    refs = list(ref_mgr.getReferencesTo(string_addr))
    print("Total xrefs to string: %d" % len(refs))

    # Partition into in-function vs not-in-function.
    in_fn_refs = []
    other_refs = []
    for r in refs:
        src = r.getFromAddress()
        fn = fn_mgr.getFunctionContaining(src)
        if fn is None:
            other_refs.append(r)
        else:
            in_fn_refs.append((r, fn))

    print("  in-function: %d" % len(in_fn_refs))
    print("  other (data tables / unanalyzed): %d" % len(other_refs))
    print("")

    # Find candidate class-name-getter functions.
    seen_fns = set()
    short_fns = []
    for _, fn in in_fn_refs:
        eid = fn.getEntryPoint().getOffset()
        if eid in seen_fns:
            continue
        seen_fns.add(eid)
        if is_short_function(fn):
            short_fns.append(fn)

    if not short_fns:
        print("No short candidate getter functions found.  Listing ALL in-")
        print("function xrefs so we can pick by hand:")
        for r, fn in in_fn_refs:
            print("  - xref at %s in %s @ %s" %
                  (r.getFromAddress(), fn.getName(), fn.getEntryPoint()))
        print("")
        return

    print("Candidate class-name getter function(s):")
    for fn in short_fns:
        print("")
        print("  function %s @ %s (body):" %
              (fn.getName(), fn.getEntryPoint()))
        dump_short_function(fn)

        # Find data xrefs to this function's entry — those are vtable
        # slot-0 locations.
        getter_addr = fn.getEntryPoint()
        getter_int = getter_addr.getOffset()
        getter_refs = list(ref_mgr.getReferencesTo(getter_addr))
        print("  xrefs to function entry: %d" % len(getter_refs))
        data_refs = []
        for r in getter_refs:
            rt = r.getReferenceType()
            src = r.getFromAddress()
            # Filter out call refs (those are real callers); we want
            # DATA refs (vtable entries).
            in_fn = fn_mgr.getFunctionContaining(src) is not None
            if not in_fn:
                data_refs.append(r)
            elif hasattr(rt, "isCall") and not rt.isCall():
                data_refs.append(r)
        print("  candidate vtable-slot-0 sites: %d" % len(data_refs))
        for r in data_refs:
            src = r.getFromAddress()
            print("")
            print("  === vtable window around %s ===" % src)
            dump_vtable_window(src.getOffset())

    # Also dump the non-function xrefs in case the actor name appears in
    # a registration table directly.
    if other_refs:
        print("")
        print("Other (non-function) xrefs to '%s' for completeness:" %
              CLASS_NAME_STRING)
        for r in other_refs:
            print("")
            print("  === %s ===" % r.getFromAddress())
            dump_vtable_window(r.getFromAddress().getOffset())

    print("")
    print("Done.")


main()
