# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — Wonder Seed gate RE
# (2026-05-26 iteration 6).
#
# Iteration 5 confirmed the world-map dispatch builds a list, not a
# gate.  This iteration runs TWO orthogonal probes:
#
#   PART A — Decompile the GateOpen-string-holding functions
#     FUN_7101abd140 and FUN_7101abd3c8 (xref 'GateOpen' string at
#     0x71034d832f, 8 total xrefs).  These might be the literal
#     gate-handling code we're after.
#
#   PART B — Nerve search via name-getter pattern
#     SMBW Nerves carry their event name as a string returned by
#     vtable slot 0.  The getter is small:
#         adrp x0, <string_page>
#         add  x0, x0, #<string_offset>
#         ret
#     We scan .rodata for gate-relevant strings, find every xref
#     site, check if it's inside such a getter function, and if so
#     trace BACK to vtable callers.  Hits in the vtable regions
#     0x710334XXXX / 0x71033fXXXX / 0x71034BXXXX are Nerve vtables.
#     Slot 8 of a Nerve vtable is the execute function — that's a
#     gate-check candidate to hook with the M1 NerveActivateOnce
#     filter pattern.
#
# Output to file at OUTPUT_FILE_PATH.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

import os

OUTPUT_FILE_PATH = r"C:\Users\maxwe\Documents\smwonder_archipelago\.claude\worktrees\funny-lehmann-06e394\scripts\ghidra\dump_v5_output.txt"

# PART A — functions to decompile (GateOpen string holders).
PART_A_FUNCTIONS = [
    (0x7101abd140, "*** GateOpen string xref'er"),
    (0x7101abd3c8, "*** GateOpen string xref'er"),
]

# PART B — string fragments to search for in .rodata.  Hits inside small
# string-getter functions (adrp+add+ret pattern) are Nerve name getters.
NERVE_STRING_SEEDS = [
    # Gate / lock terms
    "GateOpen",
    "GateClose",
    "OpenGate",
    "Gate",
    "Lock",
    "Unlock",
    "Locked",
    "Unlocked",
    "TryOpen",
    # Course / level entry
    "EnterStage",
    "EnterCourse",
    "TryEnter",
    "RequestEnter",
    "RequestEnterCourse",
    "RequestEnterStage",
    "OnEnter",
    "SelectCourse",
    "SelectStage",
    "OpenCourse",
    "OpenStage",
    "CourseStart",
    "StageStart",
    "CourseSelect",
    # World transition
    "WorldMove",
    "WorldChange",
    "ChangeWorld",
    "RequestWorld",
    "MoveToWorld",
    "TravelTo",
    "FastTravel",
    # Palace / boss
    "Palace",
    "PalaceEnter",
    "PalaceBoss",
    "PalaceStart",
    "Castle",
    "CastleEnter",
    "Bowser",
    "BowserBattle",
    "BowserStart",
    "FinalBoss",
    "BossBattle",
    "BossStart",
    "BossDoor",
    # Specific to seed gates
    "RequireSeed",
    "SeedRequire",
    "SeedGate",
    "NeedSeed",
    "WonderSeed",
    "RoyalSeed",
    "GrandSeed",
    "MotherSeed",
    "FlowerGate",
]

# Vtable regions per CLAUDE.md (Nerves observed here).
VTABLE_REGIONS = [
    (0x7103340000, 0x7103500000),  # widen — Nerves observed at 33XXXX and 34BXXX
]

# Max instructions in a "name getter" (adrp + add + ret = 3 insns typically).
NAME_GETTER_MAX_INSNS = 5


# ---- shared helpers (same as v4) ----

_out_lines = []


def out(s=""):
    _out_lines.append(s)
    print(s)


def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def classify_caller(src_addr, fn_mgr):
    src_off = src_addr.getOffset()
    fn = fn_mgr.getFunctionContaining(src_addr)
    if fn is not None:
        return ("fn", "%s @ %s" % (fn.getName(), fn.getEntryPoint()))
    for lo, hi in VTABLE_REGIONS:
        if lo <= src_off < hi:
            return ("vtable", "VTABLE @ 0x%x" % src_off)
    listing = currentProgram.getListing()
    ins = listing.getInstructionAt(src_addr)
    if ins is not None:
        return ("orphan", "orphan code @ 0x%x  %s" % (src_off, ins))
    return ("data", "data ref @ 0x%x" % src_off)


def get_callers(fn):
    ref_mgr = currentProgram.getReferenceManager()
    fn_mgr = currentProgram.getFunctionManager()
    refs = list(ref_mgr.getReferencesTo(fn.getEntryPoint()))
    out_refs = []
    for r in refs:
        src = r.getFromAddress()
        ref_type = str(r.getReferenceType())
        kind, label = classify_caller(src, fn_mgr)
        out_refs.append((kind, label, src, ref_type))
    return out_refs


def get_decompile_text(fn, timeout_sec=60):
    try:
        from ghidra.app.decompiler import DecompInterface
        from ghidra.util.task import ConsoleTaskMonitor
    except Exception as e:
        return ["[decompile import failed: %s]" % e]
    di = DecompInterface()
    di.openProgram(currentProgram)
    try:
        res = di.decompileFunction(fn, timeout_sec, ConsoleTaskMonitor())
        if res is None or not res.decompileCompleted():
            err = res.getErrorMessage() if res else "(no result)"
            return ["[decompile failed: %s]" % err]
        decomp = res.getDecompiledFunction()
        if decomp is None:
            return ["[no decompiled output]"]
        return decomp.getC().splitlines()
    finally:
        di.dispose()


def dump_function(off_full, label):
    out("")
    out("================================================================")
    out("=== FUNCTION 0x%x  --  %s" % (off_full, label))
    out("================================================================")
    fn_mgr = currentProgram.getFunctionManager()
    a = _addr(off_full)
    fn = fn_mgr.getFunctionAt(a)
    if fn is None:
        fn = fn_mgr.getFunctionContaining(a)
    if fn is None:
        out("  [no function at 0x%x]" % off_full)
        return
    out("  Function: %s @ %s" % (fn.getName(), fn.getEntryPoint()))
    out("  Signature: %s" % fn.getSignature())
    out("  Body size: %d bytes" % fn.getBody().getNumAddresses())

    out("")
    out("  -- Direct callers --")
    callers = get_callers(fn)
    out("  Total refs: %d" % len(callers))
    for k, lab, src, rt in callers[:30]:
        out("    [%s][%s]  %s" % (k, rt, lab))
    if len(callers) > 30:
        out("    ... (%d more not shown)" % (len(callers) - 30))

    out("")
    out("  -- Decompile --")
    for ln in get_decompile_text(fn):
        out("    %s" % ln)


# ---- Nerve name-getter detection ----


def find_string_addresses(needle):
    """Find every null-terminated occurrence of `needle` in initialized
    non-executable memory.  Returns list of integer addresses."""
    pattern = bytearray()
    for c in needle:
        pattern.append(ord(c))
    pattern.append(0)
    memory = currentProgram.getMemory()
    start = memory.getMinAddress()
    found = []
    max_per_term = 64
    while start is not None and len(found) < max_per_term:
        try:
            f = memory.findBytes(start, bytes(pattern), None, True, monitor)
        except Exception:
            break
        if f is None:
            break
        block = memory.getBlock(f)
        if block is not None and block.isExecute():
            start = f.add(1)
            continue
        found.append(f.getOffset())
        start = f.add(len(pattern))
    return found


def is_name_getter(fn):
    """Heuristic: a name getter is a very small function that just does
    adrp + add + ret to return a string pointer.  Some have a single
    extra mov insn.  Returns True/False."""
    listing = currentProgram.getListing()
    cur = listing.getInstructionAt(fn.getEntryPoint())
    insns = []
    while cur is not None and len(insns) < NAME_GETTER_MAX_INSNS:
        insns.append(cur)
        m = cur.getMnemonicString().lower()
        if m == "ret":
            break
        cur = cur.getNext()
    if not insns:
        return False
    mnemonics = [i.getMnemonicString().lower() for i in insns]
    if "ret" not in mnemonics:
        return False
    # Must contain an adrp.
    if "adrp" not in mnemonics:
        return False
    # Body size should be small.
    if fn.getBody().getNumAddresses() > 32:
        return False
    return True


def find_string_xref_owner_fns(string_addr, fn_mgr, ref_mgr):
    """For a string address, return the set of function entries whose
    body contains an xref to that string."""
    refs = list(ref_mgr.getReferencesTo(_addr(string_addr)))
    out_fns = set()
    for r in refs:
        src = r.getFromAddress()
        fn = fn_mgr.getFunctionContaining(src)
        if fn is not None:
            out_fns.add((fn.getEntryPoint().getOffset(), fn))
    return out_fns


def find_vtable_refs_to(fn_entry, ref_mgr):
    """Return refs to `fn_entry` whose source address lies in a vtable
    region."""
    refs = list(ref_mgr.getReferencesTo(fn_entry))
    out_refs = []
    for r in refs:
        src = r.getFromAddress().getOffset()
        for lo, hi in VTABLE_REGIONS:
            if lo <= src < hi:
                out_refs.append(r)
                break
    return out_refs


def nerve_search():
    out("")
    out("################################################################")
    out("####  PART B: Nerve search via string-getter pattern  ##########")
    out("################################################################")
    out("  Strategy: scan .rodata for gate-relevant strings, find xref")
    out("  sites in small adrp+add+ret functions (Nerve name getters),")
    out("  then trace BACK to vtable callers.  Vtable hits = Nerves.")
    out("")

    fn_mgr = currentProgram.getFunctionManager()
    ref_mgr = currentProgram.getReferenceManager()
    listing = currentProgram.getListing()

    found_nerves = []  # (string_text, string_addr, getter_fn, vtable_addrs)

    for term in NERVE_STRING_SEEDS:
        if monitor.isCancelled():
            break
        addrs = find_string_addresses(term)
        if not addrs:
            continue
        for sa in addrs:
            # Verify this is the exact string we want (avoid substring matches).
            # The string at sa should be the terminator-anchored version of `term`.
            mem = currentProgram.getMemory()
            try:
                actual_bs = bytearray()
                for i in range(len(term) + 1):
                    actual_bs.append(mem.getByte(_addr(sa + i)) & 0xff)
            except Exception:
                continue
            if actual_bs != bytearray(term.encode("ascii") + b"\x00"):
                continue
            # Only count if the preceding byte is null (true string boundary).
            try:
                prev = mem.getByte(_addr(sa - 1)) & 0xff
            except Exception:
                prev = 0
            if prev != 0:
                # Could still be valid (string at very start of region), but
                # let's filter out substring matches.
                # (Note: this filter loses some hits but reduces noise.)
                pass

            # Find functions xref'ing this string.
            owner_fns = find_string_xref_owner_fns(sa, fn_mgr, ref_mgr)
            for entry_off, fn in owner_fns:
                if not is_name_getter(fn):
                    continue
                # Found a name getter — check for vtable refs.
                vt_refs = find_vtable_refs_to(fn.getEntryPoint(), ref_mgr)
                if not vt_refs:
                    # Still record getter functions w/o vtable refs (might
                    # be missing analysis).
                    found_nerves.append((term, sa, fn, []))
                    continue
                vt_addrs = [r.getFromAddress().getOffset() for r in vt_refs]
                found_nerves.append((term, sa, fn, vt_addrs))

    if not found_nerves:
        out("  No name-getter-shaped functions matched any term.")
        return

    # Group by getter function.
    by_getter = {}
    for term, sa, fn, vt_addrs in found_nerves:
        key = fn.getEntryPoint().getOffset()
        if key not in by_getter:
            by_getter[key] = {
                "fn": fn,
                "terms": set(),
                "vt_addrs": set(),
            }
        by_getter[key]["terms"].add(term)
        for v in vt_addrs:
            by_getter[key]["vt_addrs"].add(v)

    out("  Found %d name-getter-shaped functions matching gate-relevant terms." % len(by_getter))
    out("")

    # Sort by vtable-ref count desc (most-vtabled first = most-likely Nerves).
    rows = sorted(by_getter.values(),
                  key=lambda r: (-len(r["vt_addrs"]), r["fn"].getEntryPoint().getOffset()))
    for r in rows:
        fn = r["fn"]
        terms = sorted(r["terms"])
        vt_addrs = sorted(r["vt_addrs"])
        out("")
        out("  ----------------------------------------------------------------")
        out("  Name getter: %s @ %s" % (fn.getName(), fn.getEntryPoint()))
        out("  Returns one of: %s" % ", ".join(repr(t) for t in terms))
        out("  Vtable refs (%d):" % len(vt_addrs))
        for v in vt_addrs:
            # For each vtable hit, slot 8 (execute, +0x40 bytes) is the
            # candidate Nerve execute.  Read the qword at v+0x40.
            slot0_off = v  # the getter is at slot 0
            slot8_off = v + 0x40  # 8 qwords = 64 bytes later
            try:
                mem = currentProgram.getMemory()
                qword = 0
                for i in range(8):
                    qword |= (mem.getByte(_addr(slot8_off + i)) & 0xff) << (8 * i)
                # Slot 8 should be a function pointer.
                out("    VTABLE @ 0x%x  slot 0 -> getter (us);  slot 8 -> 0x%x" %
                    (slot0_off, qword))
            except Exception as e:
                out("    VTABLE @ 0x%x  slot 8 read failed: %s" % (slot0_off, e))
        # Disassemble the first 4 insns of the getter to confirm string.
        out("  Getter body:")
        cur = listing.getInstructionAt(fn.getEntryPoint())
        for _ in range(NAME_GETTER_MAX_INSNS):
            if cur is None:
                break
            try:
                bs = cur.getBytes()
                bs_hex = " ".join("%02x" % (b & 0xff) for b in bs)
            except Exception:
                bs_hex = "?"
            out("    %s   %-20s  %s" % (cur.getAddress(), bs_hex, cur))
            if cur.getMnemonicString().lower() == "ret":
                break
            cur = cur.getNext()


def main():
    out("SMBW Archipelago: dump_gate_candidates_v5.py")
    out("  (Wonder Seed gate-check RE — iteration 6)")
    out("  Parts: A (GateOpen functions) + B (Nerve name-getter search)")
    out("  Output: %s" % OUTPUT_FILE_PATH)

    out("")
    out("################################################################")
    out("####  PART A: GateOpen string xref'ers  ########################")
    out("################################################################")
    for off, label in PART_A_FUNCTIONS:
        if monitor.isCancelled():
            break
        dump_function(off, label)

    nerve_search()

    out("")
    out("Done.")

    try:
        d = os.path.dirname(OUTPUT_FILE_PATH)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        f = open(OUTPUT_FILE_PATH, "w")
        try:
            for ln in _out_lines:
                f.write(ln)
                f.write("\n")
        finally:
            f.close()
        print("")
        print("[file] Wrote %d lines to %s" % (len(_out_lines), OUTPUT_FILE_PATH))
    except Exception as e:
        print("[file] FAILED to write %s: %s" % (OUTPUT_FILE_PATH, e))


main()
