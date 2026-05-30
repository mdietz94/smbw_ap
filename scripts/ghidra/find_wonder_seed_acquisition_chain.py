# -*- coding: utf-8 -*-
# Ghidra script for SMBW Archipelago — Wonder Seed RE re-open (2026-05-28).
#
# Trace forward from the WonderSeedAwarded Nerve vtable to identify the
# call chain that culminates in container writes.  Goal: find the natural
# game write path for a Wonder Seed grab so we can either hook it directly
# or learn what hash/offset it stores into.
#
# Background.  In M1 we identified vtable offset NSO+0x3345728 as the
# WonderSeedAwarded Nerve (see switch-mod/src/main.cpp:116).  This Nerve
# is dispatched via the shared "Nerve one-shot activate" helper at
# FUN_7100559f7c (NSO +0x559f7c) -- our NerveActivateOnce trampoline
# already filters by nerve[0]==0x3345728 to surface fires.
#
# The vtable layout (slot 8 = execute) is what we want here.  Reading
# slot 8 from NSO+0x3345728+8*8 = NSO+0x3345768 gives the execute
# function pointer.  From there we forward-trace N levels of `bl`/`blr`
# to identify every container-* writer (FUN_710049F648 / FUN_710049EA24
# / FUN_7101F2B354 / FUN_71005E93FC / FUN_7101F263FC / etc.) that the
# execute path calls -- transitively.
#
# Output:
#
#   ===== Wonder Seed acquisition chain (max depth N) =====
#   FUN_7100559f7c                                   (NerveActivateOnce helper)
#     -> FUN_XYZ                                     (execute slot 8 dispatcher)
#       -> FUN_XYZ                                   (...)
#         -> FUN_710049EA24                          ** container-B wrapper
#         -> FUN_710049F648                          ** container-A counter
#         -> FUN_7101F2B354                          ** per-course bitfield
#         -> ... other game functions ...
#
# Hits on the starred writers are key evidence: if the WonderSeedAwarded
# execute path calls FUN_710049EA24 with hash 0x60458608 + a bit_index,
# the in-game grant pipeline is fully characterized and we can mirror it
# bit-for-bit from AP.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

# NSO offsets.
VTABLE_WONDER_SEED_AWARDED = 0x03345728
VTABLE_SLOT_SIZE = 8
VTABLE_EXECUTE_SLOT = 8

# Known container/writer offsets (highlight when reached).
WRITER_TARGETS = {
    0x00049F648: ("FUN_710049F648",
                  "container-A counter writer (3-arg)"),
    0x00049EA24: ("FUN_710049EA24",
                  "container-B wrapper (4-arg overload candidate)"),
    0x001F263FC: ("FUN_7101F263FC",
                  "container-B bool delegate (3-arg)"),
    0x001F2B354: ("FUN_7101F2B354",
                  "per-course u32 bitfield writer (4-arg)"),
    0x0005E93FC: ("FUN_71005E93FC",
                  "container-B inner delegate"),
    0x000124134: ("FUN_7100124134",
                  "per-course bit reader (filter relevant during write?)"),
}

# Depth limit for the BFS.  4-5 is typically enough to bridge a Nerve
# execute -> game-logic -> primitive call.  10 catches deeper chains
# without running away.
MAX_DEPTH = 6

# Per-depth fan-out cap.  Some inner game functions call dozens of
# helpers; cap to keep the output manageable.  We always include
# WRITER_TARGETS regardless of cap.
PER_NODE_CALL_CAP = 32


def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def _read_qword_at(off_nso):
    """Read a qword from program memory at NSO+off_nso.  Returns int or None."""
    image_base = currentProgram.getImageBase()
    target = image_base.add(off_nso)
    try:
        # Ghidra's currentProgram.getMemory().getLong returns a Java long.
        return currentProgram.getMemory().getLong(target) & 0xFFFFFFFFFFFFFFFF
    except Exception:
        return None


def _get_function_at(off_nso):
    if off_nso is None:
        return None
    image_base = currentProgram.getImageBase().getOffset()
    addr = _addr(image_base + off_nso)
    fm = currentProgram.getFunctionManager()
    fn = fm.getFunctionAt(addr)
    if fn is None:
        fn = fm.getFunctionContaining(addr)
    return fn


def _callees_of(fn):
    """Return ordered list of (call_site_addr, callee_fn) within fn.

    Avoids calling `fn.getBody()` -- it triggers Ghidra's calling-
    convention analyzer (FunctionDB.loadVariables ->
    HomogeneousAggregate.filter) which throws
    java.lang.IndexOutOfBoundsException on at least one fn in the
    SMBW NSO whose prototype can't be resolved.  Jython's `except
    Exception` DOES NOT catch that Java Throwable, so we sidestep
    the call: walk forward from the entry point until the next fn
    entry (or a hard step cap).
    """
    listing = currentProgram.getListing()
    fm = currentProgram.getFunctionManager()
    try:
        entry = fn.getEntryPoint()
    except:
        return []
    # Find the next function's entry by asking the manager for the
    # following defined function in address order.
    next_entry = None
    try:
        nxt_fn = fm.getFunctionAfter(entry)
        if nxt_fn is not None:
            next_entry = nxt_fn.getEntryPoint()
    except:
        pass

    out = []
    ins = listing.getInstructionAt(entry)
    # Hard cap on instructions walked per function -- bounds runaway
    # cost on huge fns (also a safety belt if next_entry lookup failed).
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
        if mn in ("bl", "blr"):
            try:
                refs = ins.getReferencesFrom()
            except:
                refs = []
            for r in refs:
                try:
                    if not r.getReferenceType().isCall():
                        continue
                    callee_addr = r.getToAddress()
                    callee = fm.getFunctionAt(callee_addr)
                    if callee is None:
                        callee = fm.getFunctionContaining(callee_addr)
                except:
                    callee = None
                if callee is not None:
                    out.append((addr, callee))
        ins = listing.getInstructionAfter(addr)
    return out


def _callee_to_nso_off(callee_fn):
    image_base = currentProgram.getImageBase().getOffset()
    ep = callee_fn.getEntryPoint().getOffset()
    return (ep - image_base) & 0xFFFFFFFFFFFFFFFF


def main():
    print("SMBW Archipelago: find_wonder_seed_acquisition_chain.py")
    print("  Vtable address NSO+0x%x  (WonderSeedAwarded Nerve)"
          % VTABLE_WONDER_SEED_AWARDED)
    print("  Max BFS depth: %d" % MAX_DEPTH)
    print("")

    # Locate the vtable's execute slot.
    execute_slot_nso = (VTABLE_WONDER_SEED_AWARDED
                        + VTABLE_SLOT_SIZE * VTABLE_EXECUTE_SLOT)
    raw = _read_qword_at(execute_slot_nso)
    if raw is None:
        print("  Failed to read execute slot.")
        return
    # The qword stored at the slot is the absolute address of the execute
    # function in the loaded NSO address space.  Convert back to NSO offset
    # by subtracting the image base.  Ghidra's image base differs from the
    # runtime 0x7100000000 base used in CLAUDE.md notes -- we want the
    # OFFSET within the program, which is `raw - imageBase`.
    image_base = currentProgram.getImageBase().getOffset()
    execute_addr = raw
    # In some Switch loaders, vtables store the raw runtime address
    # (0x7100xxxxxxxx).  Normalize by stripping the runtime bias if the
    # value is unreasonably large.
    if execute_addr > image_base + 0x80000000:
        # Runtime-biased; subtract the known runtime base 0x7100000000
        # (CLAUDE.md "Address space layout").
        runtime_bias = 0x7100000000
        execute_addr = execute_addr - runtime_bias + image_base
    execute_off = execute_addr - image_base
    print("  Vtable slot 8 (execute) = NSO+0x%x  -> 0x%x" %
          (execute_slot_nso, execute_off))
    print("")

    root_fn = _get_function_at(execute_off)
    if root_fn is None:
        print("  Execute function not defined at NSO+0x%x" % execute_off)
        print("  Make sure the binary is fully analyzed before running.")
        return
    print("  Root: %s @ %s" % (root_fn.getName(), root_fn.getEntryPoint()))
    print("")

    # BFS.
    visited = set()
    visited.add(_callee_to_nso_off(root_fn))

    # frontier: list of (fn, depth, parent_chain_str)
    frontier = [(root_fn, 0, root_fn.getName())]
    writer_hits = []  # (writer_off, chain_str)

    output_lines = []
    output_lines.append("%s%s  (root)" %
                        ("  " * 0, root_fn.getName()))

    while frontier:
        if monitor.isCancelled():
            break
        cur, depth, chain = frontier.pop(0)
        if depth >= MAX_DEPTH:
            continue
        callees = _callees_of(cur)
        # Prioritize writer-target callees -- always include regardless
        # of cap.
        nso_offs = [(site, callee, _callee_to_nso_off(callee))
                    for site, callee in callees]
        priority = [t for t in nso_offs if t[2] in WRITER_TARGETS]
        rest = [t for t in nso_offs if t[2] not in WRITER_TARGETS]
        bundle = priority + rest[:max(0, PER_NODE_CALL_CAP - len(priority))]

        for site, callee, off in bundle:
            if off in visited:
                continue
            visited.add(off)
            try:
                cname = str(callee.getName())
            except:
                cname = "?"
            try:
                cep = str(callee.getEntryPoint())
            except:
                cep = "?"
            next_chain = chain + " -> " + cname
            indent = "  " * (depth + 1)
            tag = ""
            if off in WRITER_TARGETS:
                _, role = WRITER_TARGETS[off]
                tag = "  ** " + role
                writer_hits.append((off, next_chain))
            try:
                output_lines.append(
                    indent + cname + " @ " + cep + tag)
            except:
                # Defensive: any Jython 2 ASCII/unicode mismatch in
                # callee names (rare but happens with C++ demangled
                # symbols containing operator overloads).
                output_lines.append(indent + "<unprintable>" + tag)
            frontier.append((callee, depth + 1, next_chain))

    print("===== Wonder Seed acquisition tree =====")
    for line in output_lines[:300]:
        print(line)
    if len(output_lines) > 300:
        print("  ... (%d more lines truncated)" % (len(output_lines) - 300))
    print("")

    print("===== Writer-target hits =====")
    if not writer_hits:
        print("  None within depth %d.  Increase MAX_DEPTH or examine"
              " whether the Nerve's execute body is short and delegates"
              " to a vtable-dispatched submethod (vtable slot reads"
              " masquerade as data-flow, BFS skips them)." % MAX_DEPTH)
    else:
        for off, chain in writer_hits:
            name, role = WRITER_TARGETS[off]
            print("  %s (%s)" % (name, role))
            print("    via: %s" % chain)
    print("")
    print("Done.")
    print("")
    print("Next steps:")
    print("  - If FUN_710049EA24 is hit: SeedTrace's ContainerBWrapper4Arg")
    print("    trampoline will surface the live (hash, value, w3) tuple on")
    print("    Mario's seed grab.  Run the test in-game to confirm.")
    print("  - If FUN_7101F2B354 is hit: SeedTrace's PerCourseBitfield-")
    print("    Writer trampoline gives the (hash, course, value) tuple.")
    print("  - If neither is hit, the game uses a different storage path")
    print("    we haven't decompiled.  Widen MAX_DEPTH or hook the deepest")
    print("    untraversed function in the tree as the next observability")
    print("    target.")


main()
