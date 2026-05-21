# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — M3.2 badge RE step 4.
#
# Broader sweep: list every C-string in memory that contains "Badge" or
# "badge", with xref counts and first-caller info.  We're hoping to
# find function names like "AddBadgeToBag" / "BadgeBag::Add" /
# "OnNewBadgeUnlocked" / "GrantBadge" that point to the actual grant
# path — distinct from the property-getter functions we already
# identified in step 3.
#
# Output is sorted by xref count descending so the most-referenced
# strings (likely the API entry points) show up first.
#
# Run from Ghidra: Window -> Script Manager -> New Script -> Python.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

SEARCH_TERMS = ["Badge", "badge"]

# Skip strings we've already characterized in earlier scripts.
SKIP_STRINGS = {
    "GiveBadgeIdOnCourseClear",
    "UnlockBadgeIdOnCourseClear",
}

# Hard cap on results so the console doesn't drown if "badge" matches
# thousands of times (it shouldn't but be defensive).
MAX_RESULTS = 200

# Per-string: how many xrefs to show inline.
MAX_XREFS_PER_STRING = 3


def addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def find_all_string_addresses(needle):
    """Yield all addresses where the null-terminated `needle` occurs in
    memory.  Uses repeated forward-direction findBytes from successively
    advanced search starts."""
    pattern = bytearray()
    for c in needle:
        pattern.append(ord(c))
    pattern.append(0)  # null terminator
    memory = currentProgram.getMemory()
    start = memory.getMinAddress()
    while start is not None:
        found = memory.findBytes(start, bytes(pattern), None, True, monitor)
        if found is None:
            return
        yield found
        # Advance past this match to find the next.
        start = found.add(len(pattern))


def read_cstring(a, max_len=256):
    """Read up to a null terminator from address a (Ghidra Address).
    Returns the decoded text, or "" if read fails."""
    memory = currentProgram.getMemory()
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


def collect_string_info(string_addr):
    """For a string address, return (text, xrefs, first_caller_funcs).
    first_caller_funcs is a list of (xref_addr, fn_label) tuples."""
    text = read_cstring(string_addr)
    ref_mgr = currentProgram.getReferenceManager()
    fn_mgr = currentProgram.getFunctionManager()
    refs = list(ref_mgr.getReferencesTo(string_addr))
    callers = []
    for r in refs:
        src = r.getFromAddress()
        fn = fn_mgr.getFunctionContaining(src)
        if fn is not None:
            callers.append((src, "%s @ %s" % (fn.getName(), fn.getEntryPoint())))
        else:
            callers.append((src, "(not in a function)"))
    return text, refs, callers


def main():
    print("SMBW Archipelago: find_badge_strings.py")
    print("Searching for strings containing %r..." % SEARCH_TERMS)

    # Collect unique string addresses across all search terms.
    seen = set()
    string_addrs = []
    for term in SEARCH_TERMS:
        for a in find_all_string_addresses(term):
            key = a.getOffset()
            if key in seen:
                continue
            seen.add(key)
            string_addrs.append(a)
            if len(string_addrs) >= MAX_RESULTS:
                break
        if len(string_addrs) >= MAX_RESULTS:
            break

    print("Found %d unique string occurrences." % len(string_addrs))
    print("")

    # Collect info and sort by xref count descending so the most
    # API-like strings surface first.
    rows = []
    for a in string_addrs:
        text, refs, callers = collect_string_info(a)
        if text in SKIP_STRINGS:
            continue
        rows.append((len(refs), a, text, callers))
    rows.sort(key=lambda r: (-r[0], r[1].getOffset()))

    for xref_count, a, text, callers in rows:
        print("[ %2d xref(s) ]  %s  %r" % (xref_count, a, text))
        for i, (src, fn_label) in enumerate(callers):
            if i >= MAX_XREFS_PER_STRING:
                print("                  ... (%d more not shown)" %
                      (len(callers) - MAX_XREFS_PER_STRING))
                break
            print("                  @ %s in %s" % (src, fn_label))

    print("")
    print("Done.")


main()
