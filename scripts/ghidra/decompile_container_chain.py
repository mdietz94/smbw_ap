# -*- coding: utf-8 -*-
# Ghidra script for SMBW Archipelago — Wonder Seed RE re-open (2026-05-28).
#
# Decompile the container-B + per-course writer chains and place rich
# Ghidra comments so subsequent navigation has the full picture without
# re-reading scattered docs.
#
# Targets (chain starts at the top, descends into delegates):
#
#   FUN_710049EA24 — container-B high-level wrapper.
#     * CLAUDE.md documents the 3-arg shape (gmd, value, hash) used for
#       Royal-Seed-style bool grants -> delegates to FUN_7101F263FC.
#     * docs/static-analysis-findings.md iteration 7 (2026-05-26) claims a
#       4-arg overload (gmd, value, hash, bit_index) used for the per-
#       course Wonder Seed bitfield write at hash 0x60458608.
#     * The decompile output is the deciding evidence.
#
#   FUN_71005E93FC — container-B inner delegate (called from FUN_710049EA24).
#     Documented signature: (gmd+8 substruct, u8 value & 1, hash).
#     Worth re-decompiling: if the "4-arg overload" claim is correct, the
#     w3 arg gets consumed here too (or in a sibling).
#
#   FUN_7101F263FC — deferred-write bool setter (we already trampoline
#     this; reading the decompile confirms how the dirty-queue insert
#     handles hash 0x60458608 specifically).
#
#   FUN_7101F2B354 — per-course u32 bitmask writer.  4-arg
#     (gmd, value, hash, course_index).  Confirmed by use in
#     setPerCourseBitfieldAbsolute.  Decompile to identify which gmd+0xXX
#     substruct it routes into — this might be the same anchor as the
#     hypothesized container D for 0x60458608.
#
#   FUN_7100124134 — per-course bit reader.  4-arg (gmd, out*, hash,
#     bit_index).  Mirror of the write path; decompile shows the read
#     storage anchor.
#
#   FUN_71001787B40 — Wonder Seed gate predicate.  Reads container-A
#     hash 0x390eb960; we know this from RE.  Decompile to confirm
#     threshold semantics and check whether it ALSO reads the per-course
#     bitfield (0x60458608) anywhere it shouldn't.
#
# The decompile body is dumped to console; key call sites get
# "INVESTIGATE-WS:" plate comments inserted in the listing so they show
# up in Ghidra's UI.  Idempotent: re-running deduplicates.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

TARGETS = [
    (0x00049EA24, "FUN_710049EA24",
     "container-B high-level wrapper -- test the 4-arg overload claim"),
    (0x0005E93FC, "FUN_71005E93FC",
     "container-B inner delegate (gmd+8 substruct, value, hash)"),
    (0x001F263FC, "FUN_7101F263FC",
     "deferred-write bool setter on gmd+8 dirty queue"),
    (0x001F2B354, "FUN_7101F2B354",
     "per-course u32 bitmask writer (gmd, value, hash, course_index)"),
    (0x000124134, "FUN_7100124134",
     "per-course bit reader (gmd, out*, hash, bit_index)"),
    (0x001787B40, "FUN_71001787B40",
     "Wonder Seed gate predicate (reads container-A 0x390eb960)"),
    (0x000935CE0, "FUN_7100935CE0",
     "suspected per-world unlock gate (8-call dispatch over world ids)"),

    # Added 2026-05-29 after find_wonder_seed_acquisition_chain.py
    # identified the shortest path from the WonderSeedAwarded Nerve
    # execute slot to a container primitive (3 hops, ending at the
    # per-course bit READER).  Decompile these to find the WRITE side
    # of the natural seed-grab path -- which is likely inlined inside
    # one of these two callers rather than going through a known
    # primitive function.
    (0x001E02188, "FUN_7101E02188",
     "WonderSeedAwarded Nerve execute slot -- root of seed-grab path"),
    (0x00081D220, "FUN_710081D220",
     "3-hop parent of the per-course READER -- read-modify-write candidate"),
    (0x00066E548, "FUN_710066E548",
     "loads BOTH 0x60458608 and 0x580b7eb4 -- popcount/aggregator candidate"),
    (0x000743D10, "FUN_7100743D10",
     "loads 0x580b7eb4 + WS animation mirror -- per-course->mirror bridge"),
]

COMMENT_TAG = "INVESTIGATE-WS: "


def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def _decompile(fn):
    from ghidra.app.decompiler import DecompInterface
    from ghidra.util.task import ConsoleTaskMonitor

    ifc = DecompInterface()
    ifc.openProgram(currentProgram)
    try:
        result = ifc.decompileFunction(fn, 120, ConsoleTaskMonitor())
        if result is None:
            return None
        df = result.getDecompiledFunction()
        if df is None:
            return None
        return df.getC()
    finally:
        ifc.dispose()


def _plate(addr, text):
    """Append text to the plate comment at addr, deduplicating on the tag."""
    listing = currentProgram.getListing()
    existing = listing.getComment(0, addr)  # 0 == PLATE_COMMENT
    if existing is None:
        existing = ""
    payload = COMMENT_TAG + text
    if payload in existing:
        return  # idempotent
    new = existing + ("\n" if existing else "") + payload
    listing.setComment(addr, 0, new)


def _scan_called_functions(fn):
    """Return list of (call_site_addr, callee_fn) for every call inside fn.

    Avoids `fn.getBody()` -- it triggers Ghidra's calling-convention
    analyzer which throws java.lang.IndexOutOfBoundsException on at
    least one fn in this binary whose prototype can't be resolved.
    Jython's `except Exception` DOES NOT catch that Java Throwable.
    Walk from entry to next-fn-entry instead.
    """
    listing = currentProgram.getListing()
    fn_mgr = currentProgram.getFunctionManager()
    try:
        entry = fn.getEntryPoint()
    except:
        return []
    next_entry = None
    try:
        nxt_fn = fn_mgr.getFunctionAfter(entry)
        if nxt_fn is not None:
            next_entry = nxt_fn.getEntryPoint()
    except:
        pass

    out = []
    ins = listing.getInstructionAt(entry)
    steps = 0
    MAX_INSNS = 8192
    while ins is not None and steps < MAX_INSNS:
        if monitor.isCancelled():
            break
        addr = ins.getAddress()
        if next_entry is not None and addr.compareTo(next_entry) >= 0:
            break
        steps += 1
        try:
            mn = ins.getMnemonicString().lower()
        except:
            ins = listing.getInstructionAfter(addr)
            continue
        if mn in ("bl", "blr", "b"):
            try:
                refs = ins.getReferencesFrom()
            except:
                refs = []
            for r in refs:
                try:
                    is_call = r.getReferenceType().isCall()
                except:
                    is_call = False
                if not is_call and mn != "b":
                    continue
                try:
                    callee_addr = r.getToAddress()
                    callee = fn_mgr.getFunctionAt(callee_addr)
                except:
                    callee = None
                if callee is not None:
                    out.append((addr, callee))
        ins = listing.getInstructionAfter(addr)
    return out


def main():
    print("SMBW Archipelago: decompile_container_chain.py")
    fn_mgr = currentProgram.getFunctionManager()
    image_base = currentProgram.getImageBase().getOffset()
    print("  image base: 0x%x" % image_base)
    print("")

    decompile_text = []
    for nso_off, name_hint, role in TARGETS:
        if monitor.isCancelled():
            break
        ent = image_base + nso_off
        fn = fn_mgr.getFunctionAt(_addr(ent))
        if fn is None:
            fn = fn_mgr.getFunctionContaining(_addr(ent))
        if fn is None:
            print("===== 0x%x %s =====  [NO FUNCTION]" % (ent, name_hint))
            continue
        print("===== 0x%x %s =====" % (ent, name_hint))
        print("  role: %s" % role)
        print("  ghidra name: %s @ %s" % (fn.getName(), fn.getEntryPoint()))

        # Plate-comment the entry point.
        _plate(fn.getEntryPoint(),
               "%s (chain target: %s)" % (role, name_hint))

        # Walk callees for context.
        callees = _scan_called_functions(fn)
        seen = set()
        for site, callee in callees:
            cname = callee.getName()
            if cname in seen:
                continue
            seen.add(cname)
            _plate(site,
                   "calls %s @ %s" % (cname, callee.getEntryPoint()))
        if callees:
            print("  callees (deduped, first 8):")
            for site, callee in callees[:8]:
                print("    %s -> %s @ %s"
                      % (site, callee.getName(), callee.getEntryPoint()))
        else:
            print("  (no callees in body)")

        # Decompile.
        c = _decompile(fn)
        if c is None:
            print("  (decompile failed)")
            print("")
            continue
        decompile_text.append((name_hint, c))
        print("  decompile: %d chars (printed at end of output)"
              % (len(c) if c else 0))
        print("")

    # Print decompile bodies at the end, separated and labeled, so the
    # earlier per-function summaries don't get buried.
    print("")
    print("============================================================")
    print("===== decompile bodies =====")
    print("============================================================")
    for name_hint, c in decompile_text:
        print("")
        print("----- %s -----" % name_hint)
        for line in c.split("\n"):
            print("  %s" % line)
    print("")
    print("Done.")
    print("")
    print("Next steps:")
    print("  - Read the FUN_710049EA24 decompile.  If the function accesses")
    print("    a 4th register (w3 / x3) in its body, the 4-arg overload claim")
    print("    is confirmed.  Compare the dispatch keys to hash 0x60458608.")
    print("  - Read the FUN_7101F2B354 decompile.  Note which gmd+0xXX")
    print("    substruct it routes through.  If different from gmd+0x08")
    print("    (container B), that's a new substruct -- update")
    print("    docs/static-analysis-findings.md.")
    print("  - Listing now has INVESTIGATE-WS plate comments at chain entry")
    print("    points + call sites; navigate via Ghidra Search > Comments.")


main()
