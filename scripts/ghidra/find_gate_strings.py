# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — Wonder Seed gate RE
# (2026-05-26 plan: Phase 0b, cheap string-anchor sweep).
#
# Scan .rodata for English UI / debug strings that are likely to appear
# near gate-check logic, dump the enclosing function of each xref, and
# rank by xref count.  Modeled on find_badge_strings.py.
#
# The terms are deliberately broad — Nintendo binaries often contain
# both localized strings AND internal debug-name fragments in .rodata.
# Even a single hit on "Wonder Seed" near a reader callsite is enough
# corroboration for Section-4 test #2 (string adjacency) in the plan.
#
# Output: one block per term with up to MAX_XREFS_PER_STRING xref-site
# function names per occurrence.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

# Gate-relevant search terms.  Group A: direct "seed required" phrasing.
# Group B: world/region names — these often appear in palace-door /
# world-transition object class names.  Group C: gate-state vocabulary
# from menu and UI code paths.
SEARCH_TERMS = [
    # Group A — direct seed-vs-gate phrasing
    "WonderSeed",
    "WonderFlower",
    "Wonder Seed",
    "Wonder Seeds",
    "Need more",
    "More Wonder",
    "Required",
    "Requirement",
    "Locked",
    "Unlock",
    "Unlocked",
    "Lock",
    "Gate",
    "GateOpen",
    "GateClose",
    "Open",
    "Available",
    "Available?",
    "CanEnter",
    "CanPlay",
    "EnterStage",
    "EnterCourse",
    # Group B — world / palace / area names
    "PetalIsles",
    "Petal Isles",
    "Pipe-Rock",
    "PipeRock",
    "Fluff-Puff",
    "FluffPuff",
    "Sunbaked",
    "Shining Falls",
    "ShiningFalls",
    "Fungi Mines",
    "FungiMines",
    "Deep Magma",
    "DeepMagma",
    "World Bowser",
    "WorldBowser",
    "SpecialWorld",
    "Special",
    "Castle",
    "Palace",
    "Bowser",
    # Group C — gate-state vocabulary
    "Progress",
    "Progression",
    "Worldmap",
    "WorldMap",
    "StageSelect",
    "CourseSelect",
    "Course",
    "Stage",
]

# Skip strings already known from prior sprints (avoid noise).
SKIP_STRINGS = set([
    "GiveBadgeIdOnCourseClear",
    "UnlockBadgeIdOnCourseClear",
])

# Hard caps to keep output manageable.
MAX_RESULTS = 600       # total unique string addresses we'll examine
MAX_XREFS_PER_STRING = 6
MAX_STRING_HITS_PER_TERM = 32  # cap occurrences of any single term


def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def find_all_string_addresses(needle):
    """Yield up to MAX_STRING_HITS_PER_TERM addresses where the
    null-terminated `needle` occurs in initialized non-executable
    memory.  Pattern after find_badge_strings.py but cap per-term."""
    pattern = bytearray()
    for c in needle:
        pattern.append(ord(c))
    pattern.append(0)
    memory = currentProgram.getMemory()
    start = memory.getMinAddress()
    yielded = 0
    while start is not None and yielded < MAX_STRING_HITS_PER_TERM:
        try:
            found = memory.findBytes(start, bytes(pattern), None, True, monitor)
        except Exception:
            return
        if found is None:
            return
        # Defensive: must lie in a non-executable block.
        block = memory.getBlock(found)
        if block is not None and block.isExecute():
            start = found.add(1)
            continue
        yield found
        yielded += 1
        start = found.add(len(pattern))


def read_cstring(a, max_len=256):
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
    try:
        return bs.decode("ascii", "replace")
    except Exception:
        return repr(bs)


def collect_string_info(string_addr):
    text = read_cstring(string_addr)
    ref_mgr = currentProgram.getReferenceManager()
    fn_mgr = currentProgram.getFunctionManager()
    refs = list(ref_mgr.getReferencesTo(string_addr))
    callers = []
    for r in refs:
        src = r.getFromAddress()
        fn = fn_mgr.getFunctionContaining(src)
        if fn is not None:
            label = "%s @ %s" % (fn.getName(), fn.getEntryPoint())
        else:
            label = "(not in a function)"
        callers.append((src, label))
    return text, refs, callers


def main():
    print("SMBW Archipelago: find_gate_strings.py")
    print("  (Wonder Seed gate-check RE — Phase 0b string sweep)")
    print("  scanning %d search terms" % len(SEARCH_TERMS))
    print("")

    seen = set()
    string_addrs = []
    for term in SEARCH_TERMS:
        if monitor.isCancelled():
            break
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

    rows = []
    for a in string_addrs:
        if monitor.isCancelled():
            break
        text, refs, callers = collect_string_info(a)
        if text in SKIP_STRINGS:
            continue
        rows.append((len(refs), a, text, callers))
    # Sort by xref count desc, then address asc.
    rows.sort(key=lambda r: (-r[0], r[1].getOffset()))

    # Print only strings that have at least 1 xref — strings with zero
    # xrefs are typically debug-name leftovers and aren't useful here.
    n_with_xref = sum(1 for r in rows if r[0] > 0)
    print("Strings with >= 1 xref: %d" % n_with_xref)
    print("")

    seen_fn_under_seed_term = {}  # fn_label -> set of seed-like terms seen
    SEED_TERMS = ("Wonder Seed", "Wonder Seeds", "WonderSeed",
                  "Need more", "More Wonder", "Required", "Locked",
                  "Unlock", "Gate", "CanEnter", "CanPlay",
                  "EnterStage", "EnterCourse")

    for xref_count, a, text, callers in rows:
        if xref_count == 0:
            continue
        print("[ %3d xref(s) ]  %s  %r" % (xref_count, a, text))
        is_seed_like = any(s.lower() in text.lower() for s in SEED_TERMS)
        for i, (src, fn_label) in enumerate(callers):
            if i >= MAX_XREFS_PER_STRING:
                print("                   ... (%d more)" %
                      (xref_count - MAX_XREFS_PER_STRING))
                break
            print("                   @ %s in %s" % (src, fn_label))
            if is_seed_like and fn_label != "(not in a function)":
                seen_fn_under_seed_term.setdefault(fn_label, set()).add(text)

    print("")
    print("===== functions touching multiple seed-like strings =====")
    print("(these are highest-priority for decompile — a function that")
    print(" loads BOTH a 'WonderSeed' string AND a 'Locked' string is")
    print(" very likely the gate-decision or gate-UI builder)")
    multi = [(fn, ts) for fn, ts in seen_fn_under_seed_term.items()
             if len(ts) >= 2]
    multi.sort(key=lambda kv: -len(kv[1]))
    if not multi:
        print("  (none — only single-string xrefs found)")
    for fn, ts in multi[:20]:
        print("  %s  -- terms: %s" % (fn, ", ".join(sorted(ts))))

    print("")
    print("Done.")
    print("")
    print("Cross-reference guide:")
    print("  - For each highly-xref'd seed-like string: decompile the")
    print("    enclosing function and look for a `bl FUN_710012AE94`")
    print("    (container-A reader) or sibling reader call.")
    print("  - String adjacency satisfies plan Section-4 test #2.")
    print("    Combined with the reader+cmp evidence from")
    print("    walk_reader_compare_sites.py, two of the three required")
    print("    confidence anchors are reached.")


main()
