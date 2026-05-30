# -*- coding: utf-8 -*-
# Ghidra script for SMBW Archipelago — dump the 81 course-name strings
# from FUN_71003D4110 (Murmur3 course-name -> course-index lookup).
#
# Why: the per-course Wonder Seed bitfield (container-C hash 0x60458608)
# is indexed by `course_index`, where each course has a specific bit
# position assigned by the order of its name in the hardcoded table that
# FUN_71003D4110 iterates.  AP's bridge currently packs WS items into a
# naive "world W gets bits [W*16, W*16+15]" scheme, which doesn't match
# the real course-index assignments -- so bits we set get stripped by
# the save serializer (CLAUDE.md "save serializer filters out bit
# positions that don't correspond to real badges in the registry"
# applies equally to seeds).
#
# Dumping the 81 strings in their hardcoded order gives us:
#   course_index 0 = <name_0>  (== bit position 0 in 0x60458608)
#   course_index 1 = <name_1>
#   ...
#   course_index 80 = <name_80>
#
# Bridge can then map AP items like "W6 Wonder Seed" to the bit
# position(s) of W6 courses by parsing the world prefix in the strings.
#
# This script tries TWO discovery patterns since we don't know how the
# strings are laid out a priori:
#
#   (1) INLINE references.  Each course name might be referenced by a
#       separate adrp+add or pre-resolved data reference inside the
#       function.  We harvest all data refs from every instruction in
#       the function, try to read each target as a C string, dedupe by
#       target address preserving first-seen order.  This is what
#       Ghidra's auto-analyzer should have already resolved for the
#       common adrp+add pattern.
#
#   (2) STRING TABLE.  The function might just compute a base address
#       (one adrp+add) for a contiguous array of (const char*) and then
#       iterate through it.  We try each unique data-ref target as a
#       potential table base: read qwords from it, treat each as a
#       pointer, dereference and check if it points to a C string.
#       If we find >= 5 consecutive valid strings, it's plausibly a
#       table.
#
# Also dumps FUN_71003D3FB0 (sibling stage-info hash -> course-index
# translator) for cross-reference -- its body shows how the result
# gets used and may reveal additional structure.
#
# Defensive against Ghidra's known FunctionDB.getBody() bug (throws
# IndexOutOfBoundsException on malformed prototypes -- see
# find_hash_immediate_loads.py for context): walks instructions from
# entry to next-fn-entry instead of calling getBody().  Bare except
# catches Java Throwables that Python `except Exception` may miss.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function


# Targets: (nso_offset, role).
TARGETS = [
    (0x003D4110, "course-name -> course-index (Murmur3, 81 entries)"),
    (0x003D3FB0, "stage-info hash -> course-index (sibling)"),
]

# Walk limits.
MAX_INSNS_PER_FN = 65536
MAX_STR_LEN = 256
TABLE_MAX_ENTRIES = 128
TABLE_MIN_ENTRIES_FOR_REPORT = 5


def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % (n & 0xFFFFFFFFFFFFFFFF))


def _read_c_string(addr):
    """Read a NUL-terminated printable-ASCII string from program memory at
    `addr`.  Returns the string (length >= 2) or None if the byte stream
    isn't a printable C string.
    """
    if addr is None:
        return None
    try:
        mem = currentProgram.getMemory()
        buf = []
        cur = addr
        for _ in range(MAX_STR_LEN):
            try:
                b = mem.getByte(cur) & 0xFF
            except:
                return None
            if b == 0:
                break
            if b < 0x20 or b > 0x7E:
                # Hit a non-printable byte -- not a clean ASCII string.
                # (Could be UTF-8; SMBW course names appear ASCII per
                # docs/static-analysis-findings.md examples like
                # "Savanna", "Nettai", "Himitu".)
                return None
            buf.append(chr(b))
            try:
                cur = cur.add(1)
            except:
                return None
        if len(buf) < 2:
            return None
        return "".join(buf)
    except:
        return None


def _scan_fn_for_data_refs(fn_entry, next_entry):
    """Walk instructions and harvest every data reference + its target's
    string content if any.  Returns (hits, n_insns) where hits is a list
    of (ins_addr_str, target_addr_str, string_content).
    """
    listing = currentProgram.getListing()
    hits = []
    n = 0
    ins = None
    try:
        ins = listing.getInstructionAt(fn_entry)
    except:
        return ([], 0)
    while ins is not None and n < MAX_INSNS_PER_FN:
        if monitor.isCancelled():
            break
        try:
            addr = ins.getAddress()
        except:
            break
        if next_entry is not None:
            try:
                if addr.compareTo(next_entry) >= 0:
                    break
            except:
                pass
        n += 1

        try:
            refs = ins.getReferencesFrom()
        except:
            refs = []
        for r in refs:
            try:
                # Only consider data references (skip flow/call/jump).
                if r.getReferenceType().isFlow():
                    continue
            except:
                pass
            try:
                tgt = r.getToAddress()
            except:
                tgt = None
            if tgt is None:
                continue
            s = _read_c_string(tgt)
            if s is not None:
                hits.append((str(addr), str(tgt), s))

        try:
            ins = listing.getInstructionAfter(addr)
        except:
            ins = None
    return (hits, n)


def _scan_table_at(base_addr):
    """Treat `base_addr` as the start of an array of `const char*`.  Read
    qwords until we hit a value that doesn't dereference to a printable
    string (or we hit max).  Returns list of (entry_addr_str, string).
    """
    results = []
    try:
        mem = currentProgram.getMemory()
        af = currentProgram.getAddressFactory()
        cur = base_addr
        for _ in range(TABLE_MAX_ENTRIES):
            if monitor.isCancelled():
                break
            try:
                qword = mem.getLong(cur) & 0xFFFFFFFFFFFFFFFF
            except:
                break
            if qword == 0:
                break
            try:
                ptr_addr = af.getAddress("%x" % qword)
            except:
                break
            s = _read_c_string(ptr_addr)
            if s is None:
                break
            results.append((str(cur), s))
            try:
                cur = cur.add(8)
            except:
                break
    except:
        pass
    return results


def _decompile(fn):
    try:
        from ghidra.app.decompiler import DecompInterface
        from ghidra.util.task import ConsoleTaskMonitor
    except:
        return None
    ifc = DecompInterface()
    try:
        ifc.openProgram(currentProgram)
        result = ifc.decompileFunction(fn, 120, ConsoleTaskMonitor())
        if result is None:
            return None
        df = result.getDecompiledFunction()
        if df is None:
            return None
        return df.getC()
    except:
        return None
    finally:
        try:
            ifc.dispose()
        except:
            pass


def _resolve_fn(fn_mgr, nso_off):
    image_base = currentProgram.getImageBase().getOffset()
    ent = image_base + nso_off
    try:
        fn = fn_mgr.getFunctionAt(_addr(ent))
    except:
        fn = None
    if fn is None:
        try:
            fn = fn_mgr.getFunctionContaining(_addr(ent))
        except:
            fn = None
    return fn, ent


def _next_fn_entry(fn_mgr, fn):
    try:
        nxt = fn_mgr.getFunctionAfter(fn.getEntryPoint())
        if nxt is not None:
            return nxt.getEntryPoint()
    except:
        pass
    return None


def main():
    print("SMBW Archipelago: dump_course_name_table.py")
    image_base = currentProgram.getImageBase().getOffset()
    print("  image base: 0x%x" % image_base)
    print("")

    fn_mgr = currentProgram.getFunctionManager()
    decompile_text = []

    for nso_off, role in TARGETS:
        if monitor.isCancelled():
            break

        fn, ent_offset = _resolve_fn(fn_mgr, nso_off)
        if fn is None:
            print("===== FUN_%x (%s) =====  [no function defined]" % (ent_offset, role))
            print("")
            continue

        print("===== %s @ %s =====" % (fn.getName(), fn.getEntryPoint()))
        print("  role: %s" % role)
        print("  NSO offset: 0x%x" % nso_off)

        next_entry = _next_fn_entry(fn_mgr, fn)

        hits, n_insns = _scan_fn_for_data_refs(fn.getEntryPoint(), next_entry)
        print("  walked %d instructions; %d data-refs resolved to C strings"
              % (n_insns, len(hits)))

        # Dedupe by target address, preserve first-seen instruction order.
        seen_tgt = set()
        inline = []
        for ins_addr, tgt, s in hits:
            if tgt in seen_tgt:
                continue
            seen_tgt.add(tgt)
            inline.append((ins_addr, tgt, s))

        if inline:
            print("")
            print("  INLINE strings (deduped, ordered by first ins):")
            print("  idx | ins addr   | string addr   | content")
            for i, (ins_addr, tgt, s) in enumerate(inline):
                disp = s if len(s) <= 60 else (s[:57] + "...")
                print("  %3d | %-10s | %-13s | %s" % (i, ins_addr, tgt, disp))

        # Pattern (2): treat each unique data-ref target as a potential
        # string TABLE base.
        print("")
        print("  Potential string-table interpretation of each data-ref:")
        reported = 0
        for ins_addr, tgt, _ in inline:
            entries = _scan_table_at(_addr(int(tgt, 16)))
            if len(entries) >= TABLE_MIN_ENTRIES_FOR_REPORT:
                reported += 1
                print("    Potential TABLE @ %s (referenced by ins %s):"
                      " %d strings"
                      % (tgt, ins_addr, len(entries)))
                preview = min(len(entries), 12)
                for j, (entry_addr, s) in enumerate(entries[:preview]):
                    disp = s if len(s) <= 60 else (s[:57] + "...")
                    print("      [%3d] %s -> %s" % (j, entry_addr, disp))
                if len(entries) > preview:
                    print("      ... (%d more)" % (len(entries) - preview))
        if reported == 0:
            print("    (no candidate target dereferences as a pointer array)")

        # Decompile body for fallback.
        c = _decompile(fn)
        if c:
            decompile_text.append((fn.getName(), str(fn.getEntryPoint()), c))
        print("")

    # Print decompile bodies at the end so they don't bury the
    # per-function summaries.
    if decompile_text:
        print("============================================================")
        print("===== decompile bodies =====")
        print("============================================================")
        for name, ep, c in decompile_text:
            print("")
            print("----- %s @ %s -----" % (name, ep))
            for line in c.split("\n"):
                print("  %s" % line)

    print("")
    print("Done.")
    print("")
    print("Next steps:")
    print("  - If the INLINE list above contains ~81 plausible course")
    print("    names, that ordering IS the course_index assignment.")
    print("    The position in the list (0..80) == bit position in the")
    print("    per-course WS bitfield at container-C hash 0x60458608.")
    print("  - If a potential TABLE candidate has ~81 entries, those are")
    print("    the course names indexed by course_index directly --")
    print("    cleaner ground truth.")
    print("  - Names probably look like 'W1_Course_01_...' or Japanese")
    print("    codenames ('Savanna' = W1, 'Nettai' = ?, 'Himitu' = Special)")
    print("    per FUN_7100743D10 decompile findings (2026-05-29).")
    print("  - Group resolved names by world prefix, then update")
    print("    apworld/smbw_archipelago/client/context.py's")
    print("    _recompute_wonder_seed_bits() to assign AP items to the")
    print("    SPECIFIC bit positions of that world's known courses.")


main()
