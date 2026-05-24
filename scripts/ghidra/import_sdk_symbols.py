# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — sprint 2 setup.
#
# Imports the Nintendo SDK + game-side symbol maps in
# `switch-mod/syms/100/*.sym` as Ghidra labels (and functions, where
# applicable). The previous static-analysis sprint never did this —
# decompiled output called everything FUN_71xxxxx even though we had
# 27,000+ named symbols sitting on disk.
#
# Format of each .sym file (one symbol per line):
#
#     _ZN2nn4init11s_AllocatorE = __main_start + 0x035acda8;
#     _ZN2nn5async16ResumeBlockerOneD0Ev = __main_start + 0x00000000009dd224;
#     OR
#     _ZN2nn2fs8OpenFileEPNS0_10FileHandleEPKci = __sdk_start + 0x000000000010ad4c;
#
# Lines are either `name = base + 0xOFFSET;` (apply) or `name = 0;`
# (skip — symbol intentionally unmapped) or blank / /* comment */.
#
# Address bases (in Ghidra at the loaded NSO):
#   __main_start: 0x7100000000  (the game NSO base)
#   __sdk_start:  resolves at runtime via nn::ro::LookupSymbol; the
#                 SDK module is loaded by the kernel before the game
#                 starts.  In our STATIC Ghidra view of `main.nso`,
#                 SDK symbols are NOT in the binary — they are
#                 dynamic-link references resolved at load time.
#                 So we SKIP `__sdk_start` lines: they have no static
#                 address to label in main.nso.  (If you ever load
#                 the SDK NSO separately in Ghidra, re-run this
#                 script in that program to apply the SDK syms there.)
#
# Run from Ghidra Script Manager (see scripts/ghidra/README.md).
# Idempotent: re-running just refreshes labels.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

import os

# Path to the .sym files.  Adjust if you move them.
# (The script discovers files by walking this root.)
SYMS_ROOT = r"C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod\syms\100"

# Ghidra's loaded NSO base.  Confirm with currentProgram.getImageBase().
DEFAULT_MAIN_BASE = 0x7100000000

# If a name matches a CXX mangling and ENDS in something function-like
# (not a pure typeinfo / vtable), promote it to a function.  Conservative:
# don't try to demangle; just create the label and let Ghidra's analysis
# do the rest.

# Skip these prefixes — they are typeinfo / vtable / RTTI strings that
# Ghidra applies elsewhere and labelling them clutters the symbol tree.
# (Comment out if you actually want them.)
SKIP_PREFIXES = (
    # "_ZTS",   # typeinfo strings (Itanium)
    # "_ZTI",   # typeinfo objects
    # "_ZTV",   # vtables
)

# Symbol source priority — higher value wins when two files name the
# same address.  All our .sym files are USER_DEFINED so this only
# matters within Ghidra's own conflict resolution.
SOURCE = None  # set in main() from ghidra.program.model.symbol.SourceType


def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def _ensure_function(a, name):
    """Create a function at `a` if it looks like code, label otherwise."""
    fn_mgr = currentProgram.getFunctionManager()
    existing = fn_mgr.getFunctionAt(a)
    if existing is not None:
        # Already a function — just rename via the symbol table.
        return existing
    listing = currentProgram.getListing()
    # If there's an instruction at this address, treat as function.
    if listing.getInstructionAt(a) is not None:
        try:
            fn = fn_mgr.createFunction(name, a, None, SOURCE)
            return fn
        except Exception:
            return None
    return None


def _apply_label(a, name):
    """Apply `name` as a primary label at `a`, replacing any auto label."""
    sym_tbl = currentProgram.getSymbolTable()
    try:
        sym = sym_tbl.createLabel(a, name, SOURCE)
        if sym is not None:
            sym.setPrimary()
            return True
    except Exception:
        # Most often: duplicate name in same namespace.  Acceptable.
        pass
    return False


def parse_sym_file(path, main_base):
    """Yield (name, address) for every applicable symbol in `path`.

    Skips:
      - blank / comment lines
      - `name = 0;` (intentionally unmapped)
      - `__sdk_start + ...` (resolved dynamically — not present in main.nso)
      - SKIP_PREFIXES names
    """
    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("ascii", "replace")
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("/*") or s.startswith("//"):
            continue
        if "=" not in s:
            continue
        # Strip trailing ';' and surrounding whitespace.
        if s.endswith(";"):
            s = s[:-1].strip()
        try:
            name_part, rhs = s.split("=", 1)
        except ValueError:
            continue
        name = name_part.strip()
        rhs = rhs.strip()
        if not name or not rhs:
            continue
        if name.startswith(SKIP_PREFIXES) and SKIP_PREFIXES:
            continue

        if rhs == "0":
            # Intentionally unmapped.
            continue

        if rhs.startswith("__sdk_start"):
            # SDK symbol — resolved at runtime, no static address in main.nso.
            continue

        if rhs.startswith("__main_start"):
            # Expect: __main_start + 0xOFFSET
            try:
                _, offs = rhs.split("+", 1)
            except ValueError:
                continue
            offs = offs.strip()
            try:
                off_int = int(offs, 0)
            except ValueError:
                continue
            yield (name, main_base + off_int)
        else:
            # Bare numeric: name = 0xABCD;
            try:
                yield (name, int(rhs, 0))
            except ValueError:
                continue


def main():
    global SOURCE
    # Defer the import so the script still parses if run outside Ghidra.
    from ghidra.program.model.symbol import SourceType
    SOURCE = SourceType.USER_DEFINED

    main_base = currentProgram.getImageBase().getOffset()
    if main_base != DEFAULT_MAIN_BASE:
        print("WARN: image base is 0x%x, expected 0x%x. Using actual." %
              (main_base, DEFAULT_MAIN_BASE))

    print("SMBW Archipelago: import_sdk_symbols.py")
    print("  symbol root: %s" % SYMS_ROOT)
    print("  main base:   0x%x" % main_base)

    if not os.path.isdir(SYMS_ROOT):
        print("ERROR: SYMS_ROOT not found.  Edit the constant at the top of")
        print("       this script to point at switch-mod/syms/100.")
        return

    sym_files = []
    for root, dirs, files in os.walk(SYMS_ROOT):
        for f in files:
            if f.endswith(".sym"):
                sym_files.append(os.path.join(root, f))
    sym_files.sort()

    total_applied = 0
    total_skipped = 0
    total_funcs = 0
    per_file = []

    for path in sym_files:
        rel = os.path.relpath(path, SYMS_ROOT)
        applied = 0
        funcs = 0
        skipped = 0
        for name, addr_int in parse_sym_file(path, main_base):
            # Reject obviously out-of-range addresses.
            if addr_int < main_base or addr_int > main_base + 0x10000000:
                skipped += 1
                continue
            try:
                a = _addr(addr_int)
            except Exception:
                skipped += 1
                continue
            ok = _apply_label(a, name)
            if ok:
                applied += 1
                # Attempt to promote to function for likely-function symbols.
                # Heuristic: names starting with `_Z` (mangled C++) at code
                # addresses, OR names without `__` (likely C functions).
                if name.startswith("_Z") or not name.startswith("__"):
                    fn = _ensure_function(a, name)
                    if fn is not None:
                        funcs += 1
            else:
                skipped += 1
        per_file.append((rel, applied, funcs, skipped))
        total_applied += applied
        total_funcs += funcs
        total_skipped += skipped

    print("")
    print("===== summary =====")
    for rel, applied, funcs, skipped in per_file:
        print("  %s: %d applied (%d functions), %d skipped" %
              (rel, applied, funcs, skipped))
    print("")
    print("TOTAL: %d applied, %d promoted to functions, %d skipped" %
          (total_applied, total_funcs, total_skipped))
    print("")
    print("If the totals look low (< 50), check that you opened main.nso")
    print("(BID CD6E42AEE7934F4D, v1.0.0) and that the syms path is correct.")
    print("Inspect symbol-tree pane: namespaces like `gmd::`, `sead::`,")
    print("`nn::` should now be populated.")
    print("")
    print("CRITICAL ANCHOR: gmd::GameDataMgr::sInstance @ __main_start + 0x0363f0f0")
    print("  Dereference *(qword*)(0x7100000000 + 0x0363f0f0) at runtime to")
    print("  get the live GameDataMgr singleton — the master save-state ptr.")


main()
