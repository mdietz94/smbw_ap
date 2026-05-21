# Ghidra script for the SMBW Archipelago project — M3.2 badge RE.
#
# Finds xrefs to two SMBW debug-name strings that we believe correspond
# to in-binary function names:
#     GiveBadgeIdOnCourseClear
#     UnlockBadgeIdOnCourseClear
#
# For each xref, prints the source address, reference type, containing
# function, and the first 8 instructions of that function — enough to
# tell the user whether the prologue is trampolineable by exlaunch's
# And64InlineHook (no PC-relative loads in the first 5 instructions),
# and which xref site corresponds to the actual badge-grant impl.
#
# How to run:
#   1. Open the SMBW NSO in Ghidra with the Adubbz Switch loader.
#   2. Run auto-analysis at least to "Function Start Analyzer" depth so
#      function boundaries exist.
#   3. Apply the NN SDK symbol map (per docs/handoff.md "Tools used").
#   4. Window -> Script Manager -> File menu -> New Script (Jython /
#      Python).  Save this file's contents under any name.
#   5. Hit the green Run button.
#
# Output goes to the Ghidra Console (Window -> Console).
#
# Python 2 / Jython AND Python 3 / PyGhidra compatible.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

TARGETS = [
    "GiveBadgeIdOnCourseClear",
    "UnlockBadgeIdOnCourseClear",
]

# How many instructions of the function prologue to dump. 8 is enough
# to see whether the first 5 (And64InlineHook's relocation window)
# contain any PC-relative loads (adrp / ldr literal / b / bl), which
# is the failure mode that bit Wonder Seed slot 8 in M1.
PROLOGUE_INSTRUCTION_COUNT = 8


def find_string_address(needle):
    """Search memory for a null-terminated occurrence of `needle`.
    Returns the first match Address or None.
    """
    pattern = bytearray()
    for c in needle:
        pattern.append(ord(c))
    pattern.append(0)  # null terminator
    memory = currentProgram.getMemory()
    start = memory.getMinAddress()
    # findBytes(start, byte[], mask, forward, monitor) -> Address | None
    return memory.findBytes(start, bytes(pattern), None, True, monitor)


def dump_function_prologue(fn):
    listing = currentProgram.getListing()
    ins = listing.getInstructionAt(fn.getEntryPoint())
    count = 0
    while ins is not None and count < PROLOGUE_INSTRUCTION_COUNT:
        # Instruction.__str__ yields "addr  mnemonic operands".
        print("    %s" % ins)
        ins = ins.getNext()
        count += 1


def process_target(needle):
    print("")
    print("===== %s =====" % needle)
    addr = find_string_address(needle)
    if addr is None:
        print("  ERROR: string not found in memory.")
        print("  - Confirm the binary loaded matches v1.0.0 (BID CD6E42AEE7934F4D).")
        print("  - Try `Search -> For Strings...` manually to verify the binary")
        print("    contains the string at all.")
        return
    print("  string found at: %s" % addr)

    ref_mgr = currentProgram.getReferenceManager()
    fn_mgr = currentProgram.getFunctionManager()

    refs = list(ref_mgr.getReferencesTo(addr))
    if not refs:
        print("  no xrefs found.")
        print("  - Right-click the address in the Listing window and choose")
        print("    Data -> string, then run auto-analyzer again, then re-run")
        print("    this script.  Until the address is typed as a string,")
        print("    Ghidra won't promote ADRP+ADD pairs that load it into")
        print("    proper data references.")
        return

    print("  %d xref(s):" % len(refs))
    for ref in refs:
        src = ref.getFromAddress()
        ref_type = ref.getReferenceType().getName()
        fn = fn_mgr.getFunctionContaining(src)
        if fn is None:
            print("  - xref at %s (%s): NOT in a function — likely a data" %
                  (src, ref_type))
            print("    table entry.  Inspect the surrounding bytes to see if")
            print("    this is a (string -> function-impl) table.")
            continue
        fn_start = fn.getEntryPoint()
        print("  - xref at %s (%s) in function %s (entry %s):" %
              (src, ref_type, fn.getName(), fn_start))
        dump_function_prologue(fn)


def main():
    print("SMBW Archipelago: find_badge_functions.py")
    print("Looking for %d target string(s)..." % len(TARGETS))
    for needle in TARGETS:
        process_target(needle)
    print("")
    print("Done.")


main()
