# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — M3.3 step 2.
#
# Quick prologue dump of FUN_710012ae94 (the wonder-seed-counter
# getter we identified in M3.3 step 1).  Two things we need:
#
#   1. Trampoline safety: no `adrp`/`ldr literal`/`b` in the first
#      5 instructions of the prologue, else And64InlineHook will
#      corrupt when we hook function entry (the Wonder Seed slot 8
#      failure mode from M1).
#   2. Calling convention: how does x11 (the index multiplier we
#      saw in the body) get set up from the function args?  That
#      tells us which arg register holds the world index.
#
# Run, paste the output back, and we'll write the runtime hook
# directly.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

FN_ENTRY = 0x710012ae94
INSN_COUNT = 30


def addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def main():
    print("SMBW Archipelago: dump_wonder_seed_fn_prologue.py")
    listing = currentProgram.getListing()
    fn_mgr = currentProgram.getFunctionManager()
    a = addr(FN_ENTRY)
    fn = fn_mgr.getFunctionContaining(a)
    if fn is None:
        print("WARN: 0x%x not inside any analyzed function." % FN_ENTRY)
    else:
        print("Function: %s @ %s" % (fn.getName(), fn.getEntryPoint()))
    print("")
    print("===== first %d instructions =====" % INSN_COUNT)
    ins = listing.getInstructionAt(a)
    count = 0
    while ins is not None and count < INSN_COUNT:
        print("    %s" % ins)
        ins = ins.getNext()
        count += 1
    print("")
    print("Done.")


main()
