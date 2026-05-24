# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — sprint 2, Phase 1+5
# helper (new lead discovered 2026-05-24).
#
# The switch-mod/syms/100/gmd/GameDataMgr.sym file contains exactly one
# symbol:
#
#     gmd::GameDataMgr::sInstance = __main_start + 0x0363f0f0;
#
# This is the singleton pointer for the game's GameDataMgr — almost
# certainly the master save-data state holder.  Dereferencing
# `*(uint64_t*)(0x7100000000 + 0x0363f0f0)` at runtime yields the heap
# address of the live GameDataMgr object, which holds (or points to)
# the entire save-state struct.
#
# This single anchor replaces the entire Cheat Engine pointer-chain
# workflow from docs/runtime-address-backtrace-plan.md.
#
# This script walks every xref to NSO +0x0363f0f0 in the binary, and
# for each one tries to identify what method of GameDataMgr is being
# invoked.  The common AArch64 pattern for `singleton->method(arg)` is:
#
#     adrp x0, page                ; load high bits of sInstance addr
#     ldr  x0, [x0, #offset]       ; load sInstance pointer ← THIS xref
#     ldr  x1, [x0]                ; load vtable pointer (if virtual call)
#     ldr  xN, [x1, #vtable_slot]  ; load method address
#     blr  xN                      ; call
#
# OR direct (non-virtual) calls:
#
#     adrp x0, ...                 ; sInstance load
#     ldr  x0, [x0, #...]
#     bl   <method_addr>
#
# For each xref the script tries to identify:
#   - Whether the immediate following block does a vtable dispatch
#     (and which slot)
#   - Whether it does a direct bl
#   - The enclosing function (the consumer of GameDataMgr)
#
# Output groups by enclosing function, since one consumer usually
# performs multiple GameDataMgr calls in sequence (e.g., a save-out
# serializer reads many fields).

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

GAMEDATAMGR_SINSTANCE_OFFSET = 0x0363F0F0

# How far forward to look after a sInstance load.
FORWARD_INSNS = 8


def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def _label_for(addr_int):
    fn_mgr = currentProgram.getFunctionManager()
    try:
        a = _addr(addr_int)
    except Exception:
        return ""
    fn = fn_mgr.getFunctionContaining(a)
    if fn is None:
        return ""
    if fn.getEntryPoint().getOffset() == addr_int:
        return "fn:%s" % fn.getName()
    return "in-fn:%s+0x%x" % (fn.getName(),
                              addr_int - fn.getEntryPoint().getOffset())


def _emit_window(start_ins, count, mark_at=None):
    listing = currentProgram.getListing()
    cur = start_ins
    n = 0
    while cur is not None and n < count:
        marker = "==> " if (mark_at is not None and
                            cur.getAddress().getOffset() == mark_at) else "    "
        mnem = cur.getMnemonicString().lower()
        note = ""
        if mnem == "bl" and cur.getNumOperands() >= 1:
            try:
                target = cur.getAddress(0)
                if target is not None:
                    tlabel = _label_for(target.getOffset())
                    if tlabel:
                        note = "    ; -> %s" % tlabel
            except Exception:
                pass
        elif mnem in ("ldr", "ldrb", "ldrh"):
            refs = list(cur.getReferencesFrom())
            for r in refs:
                try:
                    tlabel = _label_for(r.getToAddress().getOffset())
                    if tlabel:
                        note = "    ; ref -> %s" % tlabel
                        break
                except Exception:
                    pass
        print("%s %s%s" % (marker, cur, note))
        cur = cur.getNext()
        n += 1


def _detect_dispatch_kind(xref_site_int):
    """Look at the 8 instructions following the sInstance load and try
    to classify:
      - 'vtable[N]': loads [x?, #slot] then blr — indirect call
      - 'direct:<addr>': bl to a fixed address
      - 'no-call': sInstance is used as data, not invoked
    """
    listing = currentProgram.getListing()
    cur = listing.getInstructionAfter(_addr(xref_site_int))
    n = 0
    saw_ldr_offset = None
    while cur is not None and n < FORWARD_INSNS:
        mnem = cur.getMnemonicString().lower()
        if mnem == "bl":
            try:
                target = cur.getAddress(0)
                if target is not None:
                    return "direct:0x%x" % target.getOffset()
            except Exception:
                return "direct:?"
        if mnem == "blr":
            if saw_ldr_offset is not None:
                return "vtable[0x%x]" % saw_ldr_offset
            return "indirect"
        if mnem == "ldr":
            # operand 1 is the [base, #offset] memory operand; check for
            # a scalar at op 1.
            try:
                if cur.getNumOperands() >= 2:
                    objs = cur.getOpObjects(cur.getNumOperands() - 1)
                    for o in objs:
                        if o is None:
                            continue
                        if o.__class__.__name__ == "Scalar":
                            saw_ldr_offset = (o.getUnsignedValue()
                                              & 0xFFFFFFFFFFFFFFFF)
                            break
            except Exception:
                pass
        cur = cur.getNext()
        n += 1
    return "no-call"


def main():
    print("SMBW Archipelago: find_gamedatamgr_xrefs.py")
    main_base = currentProgram.getImageBase().getOffset()
    target_int = main_base + GAMEDATAMGR_SINSTANCE_OFFSET
    print("  GameDataMgr::sInstance @ 0x%x  (main_base + 0x%x)" %
          (target_int, GAMEDATAMGR_SINSTANCE_OFFSET))

    ref_mgr = currentProgram.getReferenceManager()
    refs = list(ref_mgr.getReferencesTo(_addr(target_int)))
    print("  %d xrefs total" % len(refs))
    print("")

    fn_mgr = currentProgram.getFunctionManager()

    # Group xrefs by enclosing function so consumers are visible.
    groups = {}
    for r in refs:
        src = r.getFromAddress()
        fn = fn_mgr.getFunctionContaining(src)
        key = ("%s @ %s" % (fn.getName(), fn.getEntryPoint())
               if fn is not None else "(no enclosing fn)")
        groups.setdefault(key, []).append(r)

    # Sort groups by xref count desc (heavy consumers first).
    sorted_groups = sorted(groups.items(),
                           key=lambda kv: -len(kv[1]))

    # Also tally dispatch kinds.
    kind_tally = {}

    for group_label, group_refs in sorted_groups:
        print("===== %s (%d xref(s)) =====" %
              (group_label, len(group_refs)))
        for r in group_refs:
            src = r.getFromAddress()
            kind = _detect_dispatch_kind(src.getOffset())
            kind_tally[kind] = kind_tally.get(kind, 0) + 1
            print("  xref %s  [%s]  ref-type=%s" %
                  (src, kind, r.getReferenceType().getName()))
            # Print a small disasm window for the first xref of each group
            # so the analyst can read the actual pattern.
            if r is group_refs[0]:
                listing = currentProgram.getListing()
                # 2 before + 6 after
                before_ins = listing.getInstructionBefore(src)
                if before_ins is not None:
                    bb_prev = listing.getInstructionBefore(before_ins.getAddress())
                    if bb_prev is not None:
                        before_ins = bb_prev
                start = before_ins if before_ins is not None else listing.getInstructionAt(src)
                _emit_window(start, 8, mark_at=src.getOffset())
                print("")

    print("===== dispatch-kind tally =====")
    for k, n in sorted(kind_tally.items(), key=lambda kv: -kv[1]):
        print("  %-30s %d" % (k, n))
    print("")
    print("How to read the output:")
    print("  - 'direct:0x...' = a non-virtual method call.  The address")
    print("    points to a specific GameDataMgr method we can hook by")
    print("    InstallAtOffset.  Note the address — that's our hook target.")
    print("  - 'vtable[0x...]' = virtual call through a vtable slot.  To")
    print("    identify the concrete method, find the GameDataMgr vtable")
    print("    (read 8 bytes at offset 0 of any *constructed* sInstance,")
    print("    or look for a vtable symbol in main.sym).")
    print("  - 'no-call' = sInstance loaded but never called.  Likely a")
    print("    pointer comparison or NULL check.  Less interesting.")
    print("  - Heavy consumers (functions with many xrefs) are usually")
    print("    the save serializer/deserializer — high-value hook targets")
    print("    since they touch every save field in sequence.")
    print("")
    print("Next: feed the 'direct:0x...' addresses into walk_hash_writer_xrefs.py-")
    print("style backwalks to find what hash keys / arguments they're being called with.")


main()
