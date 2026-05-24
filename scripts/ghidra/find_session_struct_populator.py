# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — M3.2 badge sprint, run #6.
#
# Background: the PlayReport session struct's badge fields at +0x434
# (equip_badge_id) and +0x674 (badge_id_array as sead::Buffer<int>)
# are populated at course-LOAD time by an unknown function.  Sprint
# runs #1-5 traced the consumer side (FUN_7101a5d93c → FUN_7101a5de58
# → reads from session+0x674) and ruled out FUN_7101a5d9a0 (a result-
# time populator that doesn't touch the badge fields).
#
# This script finds the POPULATOR by intersection:
#
#   1. Scan .text for displacement writes/reads at offsets 0x434
#      and 0x674 (both fit in single ldr/str immediates).
#   2. For each containing function, check if it ALSO accesses
#      gmd::GameDataMgr::sInstance (NSO +0x0363f0f0) — directly via
#      its xrefs OR indirectly via calling a known accessor
#      (FUN_710012AE94 reader, FUN_710049F648 writer, FUN_71003838AC
#      bool getter).
#   3. The intersection is the populator candidate set.  Functions
#      with all three (write to +0x434, write to +0x674, gmd access)
#      are highest priority.
#
# Output is ranked by likelihood and shows:
#   - The function entry + name
#   - All its +0x434 / +0x674 hits with disasm
#   - Any gmd::sInstance loads inside the function
#   - Any known accessor calls
#
# Rationale: the badge populator MUST read from a struct that
# ultimately roots at GameDataMgr (since the badge bitfield gets
# serialized to the save), so it must have a sInstance load
# somewhere in its body (or in one of its callees, but that's a
# follow-up search).

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function


# ===== Offsets we're looking for =====
SESSION_OFFSETS = [
    (0x434, "equip_badge_id (session struct)"),
    (0x674, "badge_id_array  (session struct sead::Buffer<int>)"),
    (0x67C, "badge_id_array+8 (sead::Buffer size field)"),
]

# ===== gmd::GameDataMgr::sInstance NSO offset =====
GMD_SINSTANCE_NSO_OFFSET = 0x0363F0F0

# ===== Known GameDataMgr accessor functions (relative to NSO base) =====
# A function calling any of these is touching GameDataMgr-owned data.
GMD_ACCESSORS = {
    0x0012AE94: "Container-A READER (gmd, out_ptr, hash)",
    0x0049F648: "Container-A WRITER (gmd, value, hash) ★ grant primitive",
    0x003838AC: "Sub-bool READER (sub_obj, out_byte, hash)",
    0x003D3FB0: "Stage-info → course-index TRANSLATOR",
    0x003D4110: "Murmur3-32 hash function",
    0x0059F894: "GameData accessor opener (called by M1 SetCourseClearFlag hook)",
    0x005E93FC: "(suspected flag writer — M1 hook chain) — UNCONFIRMED",
}


# ===== Mnemonics we'll inspect for offset-immediate uses =====
DISP_MNEMONICS = (
    "ldr", "ldrb", "ldrh", "ldrsb", "ldrsh", "ldrsw",
    "str", "strb", "strh",
    "ldp", "stp",
)


# ===== Limits to avoid console flood =====
MAX_HITS_PER_OFFSET = 64
MAX_FUNCTIONS_TO_REPORT = 32


def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


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


def _fn_name(ins_addr):
    fn_mgr = currentProgram.getFunctionManager()
    fn = fn_mgr.getFunctionContaining(ins_addr)
    if fn is None:
        return None
    return fn


def _scan_disp_offset(target_off):
    """Iterate all instructions, yield those whose last 1-2 operands have
    immediate displacement equal to `target_off`."""
    listing = currentProgram.getListing()
    hits = []
    for ins in listing.getInstructions(True):
        if monitor.isCancelled():
            break
        mnem = ins.getMnemonicString().lower()
        if mnem not in DISP_MNEMONICS:
            continue
        nops = ins.getNumOperands()
        for op_idx in (nops - 1, nops - 2):
            if op_idx < 0:
                continue
            imm = _imm_at_op(ins, op_idx)
            if imm is None:
                continue
            if imm == target_off:
                hits.append(ins)
                break
        if len(hits) >= MAX_HITS_PER_OFFSET:
            return hits
    return hits


def _classify_disp_hit(ins):
    """Return 'WRITE', 'READ', or 'UNKNOWN' for the instruction."""
    mnem = ins.getMnemonicString().lower()
    if mnem.startswith("str") or mnem == "stp":
        return "WRITE"
    if mnem.startswith("ldr") or mnem == "ldp":
        return "READ"
    return "UNKNOWN"


def _find_gmd_sinstance_xrefs():
    """Return set of (fn_entry_offset) for every function that has an
    xref to gmd::GameDataMgr::sInstance."""
    main_base = currentProgram.getImageBase().getOffset()
    sinst_addr = _addr(main_base + GMD_SINSTANCE_NSO_OFFSET)
    ref_mgr = currentProgram.getReferenceManager()
    fn_mgr = currentProgram.getFunctionManager()
    refs = list(ref_mgr.getReferencesTo(sinst_addr))
    fns = set()
    for r in refs:
        src = r.getFromAddress()
        fn = fn_mgr.getFunctionContaining(src)
        if fn is not None:
            fns.add(fn.getEntryPoint().getOffset())
    return fns


def _find_accessor_callers():
    """Return dict { caller_fn_entry: [(accessor_name, callsite_addr)] }
    for every function that calls a known GameDataMgr accessor."""
    main_base = currentProgram.getImageBase().getOffset()
    ref_mgr = currentProgram.getReferenceManager()
    fn_mgr = currentProgram.getFunctionManager()
    by_fn = {}
    for off, name in GMD_ACCESSORS.items():
        target_addr = _addr(main_base + off)
        refs = list(ref_mgr.getReferencesTo(target_addr))
        for r in refs:
            rt = r.getReferenceType()
            if hasattr(rt, "isCall") and not rt.isCall():
                # Skip non-call references (data refs etc.)
                continue
            src = r.getFromAddress()
            fn = fn_mgr.getFunctionContaining(src)
            if fn is None:
                continue
            entry = fn.getEntryPoint().getOffset()
            by_fn.setdefault(entry, []).append((name, src.getOffset()))
    return by_fn


def main():
    print("SMBW Archipelago: find_session_struct_populator.py")
    print("  M3.2 sprint run #6 — intersection of offset hits + gmd access")
    print("")

    main_base = currentProgram.getImageBase().getOffset()

    # 1. Build the set of functions that touch gmd::sInstance directly.
    print("Indexing gmd::sInstance xrefs ...")
    gmd_direct_fns = _find_gmd_sinstance_xrefs()
    print("  %d functions directly access gmd::sInstance" %
          len(gmd_direct_fns))

    # 2. Build the set of functions that call any known accessor.
    print("Indexing known accessor callers ...")
    accessor_callers = _find_accessor_callers()
    print("  %d functions call a known GMD accessor" %
          len(accessor_callers))

    # 3. Scan the offset hits and bucket by containing function.
    # buckets: { fn_entry: { offset: [insns] } }
    buckets = {}
    fn_mgr = currentProgram.getFunctionManager()

    for off, label in SESSION_OFFSETS:
        print("Scanning .text for displacement %d (0x%x) ..." % (off, off))
        hits = _scan_disp_offset(off)
        print("  %d hits" % len(hits))
        for ins in hits:
            fn = fn_mgr.getFunctionContaining(ins.getAddress())
            if fn is None:
                continue
            entry = fn.getEntryPoint().getOffset()
            buckets.setdefault(entry, {}).setdefault(off, []).append(ins)

    if not buckets:
        print("")
        print("No hits — extend the scan.  The offsets may be built via")
        print("split-immediate ADD (e.g., `add x_, x_, #0x4, lsl #8;")
        print("add x_, x_, #0x34`) rather than ldr/str displacement.")
        return

    # 4. Score each function.  Highest priority: contains all 3 offsets
    # AND has gmd access.  Lower: subset of these.
    print("")
    print("=" * 72)
    print("RANKED POPULATOR CANDIDATES")
    print("=" * 72)
    print("")

    def score(entry):
        """Higher = more likely to be the populator."""
        s = 0
        offsets_present = set(buckets[entry].keys())
        s += len(offsets_present) * 100  # each offset hit adds 100
        if entry in gmd_direct_fns:
            s += 1000  # direct gmd::sInstance access is a strong signal
        if entry in accessor_callers:
            s += 500   # accessor call is medium signal
        # Has BOTH a WRITE to 0x434 AND a WRITE to 0x674 = very strong
        wrote_434 = any(_classify_disp_hit(ins) == "WRITE"
                        for ins in buckets[entry].get(0x434, []))
        wrote_674 = any(_classify_disp_hit(ins) == "WRITE"
                        for ins in buckets[entry].get(0x674, []))
        if wrote_434 and wrote_674:
            s += 2000
        elif wrote_434 or wrote_674:
            s += 500
        return s

    ranked = sorted(buckets.keys(), key=lambda e: (-score(e), e))

    shown = 0
    for entry in ranked:
        if shown >= MAX_FUNCTIONS_TO_REPORT:
            print("")
            print("... (%d more not shown)" % (len(ranked) - shown))
            break
        shown += 1
        fn = fn_mgr.getFunctionAt(_addr(entry))
        fn_name = fn.getName() if fn else "FUN_%x" % entry
        sc = score(entry)
        print("")
        print("[score=%d] %s @ 0x%x" % (sc, fn_name, entry))

        # Offset hits
        for off, _label in SESSION_OFFSETS:
            insns = buckets[entry].get(off, [])
            if not insns:
                continue
            for ins in insns:
                cls = _classify_disp_hit(ins)
                print("    %s  %s   %s   (+0x%x)" % (
                    cls, ins.getAddress(), ins,
                    ins.getAddress().getOffset() - entry))

        # GMD access summary
        if entry in gmd_direct_fns:
            print("    >>> directly loads gmd::sInstance")
        if entry in accessor_callers:
            for name, callsite in accessor_callers[entry]:
                print("    >>> calls 0x%x %s   (+0x%x)" % (
                    callsite, name, callsite - entry))

    print("")
    print("=" * 72)
    print("HOW TO READ THIS")
    print("=" * 72)
    print("")
    print("Top of the list = most likely badge populator.")
    print("")
    print("Score = (each-offset-bucket * 100) + (gmd-direct * 1000) +")
    print("        (accessor-call * 500) + (writes-to-both-434-and-674 * 2000)")
    print("")
    print("What to look for in the top candidates:")
    print("  - WRITES to 0x434 = equip_badge_id setter")
    print("  - WRITES to 0x674 + 0x67C = sead::Buffer (ptr, size) write")
    print("    for the owned-badge array")
    print("  - A call to an accessor or a direct sInstance load nearby")
    print("    in the disasm = where the bitfield gets read from live state")
    print("")
    print("If the top candidate has all three (write to 0x434, write to")
    print("0x674, and gmd access), decompile it next.  The function's")
    print("body will contain `ldr x_, [x_live_bm, #N]` where x_live_bm")
    print("is the live BadgeMgr address — that's the grant target.")
    print("")
    print("If no function has BOTH offset writes, the equip_badge_id and")
    print("badge_id_array might be populated by DIFFERENT functions —")
    print("look at the top of each offset's individual list.")
    print("")
    print("Done.")


main()
