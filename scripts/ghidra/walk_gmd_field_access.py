# -*- coding: utf-8 -*-
# Ghidra script for SMBW Archipelago — Wonder Seed RE re-open (2026-05-28).
#
# Classify every dereference of the GameDataMgr singleton by the gmd+0xXX
# offset that gets accessed.  Discovers unmapped containers.
#
# Background.  Our static-analysis sprint 2 (docs/static-analysis-findings.md)
# mapped substructs at gmd+0x08 (container B bool substruct), gmd+0x70..0x8c
# (container C buckets), gmd+0xC8 (container A substruct), gmd+0xf8 (qA dirty
# ring), gmd+0x128 (qA secondary), gmd+0x68 (container B init/lock).  But the
# struct is large (>0x300 bytes; queue layout extends to gmd+0x160), and the
# Wonder Seed RE work strongly suggests at least one more substruct (the
# per-course bitfield writer routes through gmd+? in FUN_710049EA24 vs
# FUN_7101F2B354 -- which offset?).
#
# This script:
#   1. Locates every load from the sInstance qword anchor (NSO +0x363F0F0).
#   2. For each such load (`ldr xN, [<adrp>+ <add lo12>]` then
#      `ldr xN, [xN]`), follows xN forward N instructions and records every
#      `ldr/str/add xM, [xN, #0xYY]` pattern that uses xN as base.
#   3. Aggregates per (offset, mnemonic) pair across the whole binary, so
#      we get a histogram of "this offset is read X times / written Y times".
#
# Output is a single table:
#
#   gmd+0x008   read=42   write=8     ldrB(40) strB(6) addB(...)
#   gmd+0x070   read=15   write=3     ldrB(15) strB(3)
#   ...
#   gmd+0x???   read=N    write=M     (NEW -- not in documented list)
#
# Any "NEW" offset is an unmapped substruct.  The two top candidates for
# the missing per-course Wonder Seed bitfield (container D) would show up
# here as offsets that get accessed during FUN_7101F2B354 / FUN_7100124134
# execution.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function


# Anchor: gmd::GameDataMgr::sInstance, the qword at NSO +0x363F0F0.
S_INSTANCE_NSO_OFF = 0x0363F0F0

# How many instructions to walk forward from an sInstance dereference
# before giving up on it as a base register.  20 = roughly 1 basic block;
# enough to capture multiple gmd-substruct accesses without false positives
# from register reuse.
FORWARD_INSNS = 20

# Documented substructs (highlight when output matches).
DOCUMENTED = {
    0x008: "container B substruct (gmd+0x08)",
    0x040: "container B dirty queue head",
    0x030: "container B dirty queue cap",
    0x068: "container B init/lock",
    0x070: "container C buckets begin",
    0x078: "container C typed sub-obj region",
    0x080: "container C anchor",
    0x0C8: "container A substruct base",
    0x0F0: "container A dirty queue cap",
    0x0F8: "container A dirty queue ring",
    0x100: "container A dirty queue head",
    0x128: "container A secondary substruct",
    0x150: "container A secondary cap (substruct+0x28)",
    0x160: "container A secondary head (substruct+0x38)",
}


def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def _displacement_at_op(ins, op_idx):
    """Return the immediate displacement at operand op_idx, or None.
    Ghidra typically encodes `ldr xN, [xM, #0xYY]` with the displacement
    in operand 1's Scalar component."""
    try:
        objs = ins.getOpObjects(op_idx)
    except Exception:
        return None
    for o in objs:
        if o is None:
            continue
        if o.__class__.__name__ == "Scalar":
            try:
                return o.getValue()
            except Exception:
                pass
    return None


def _dest_reg(ins):
    try:
        objs = ins.getOpObjects(0)
    except Exception:
        return None
    for o in objs:
        if o is None:
            continue
        if o.__class__.__name__ == "Register":
            return o.getName().lower()
    return None


def _base_reg_of_memop(ins, op_idx=1):
    """For ldr/str, op 1 is `[xN, #imm]` and op 0 is the dst/src register.
    Return the base register name (e.g. 'x8') from the memop, or None."""
    try:
        objs = ins.getOpObjects(op_idx)
    except Exception:
        return None
    for o in objs:
        if o is None:
            continue
        if o.__class__.__name__ == "Register":
            return o.getName().lower()
    return None


def _w_to_x(reg):
    if not reg:
        return None
    n = reg.lower()
    if n.startswith("w") and n[1:].isdigit():
        return "x" + n[1:]
    return n


def _find_sInstance_dereferences():
    """Return a list of (addr, base_reg) tuples — each is a point in the
    listing where some xN register holds (sInstance loaded as a pointer).

    We look for the `adrp + ldr` pattern that loads sInstance's address,
    followed by a second `ldr` that dereferences it.  After the second ldr,
    the destination register holds the live GameDataMgr* pointer.
    """
    s_inst_addr = currentProgram.getImageBase().add(S_INSTANCE_NSO_OFF)
    ref_mgr = currentProgram.getReferenceManager()
    refs_to_anchor = list(ref_mgr.getReferencesTo(s_inst_addr))

    deref_sites = []
    listing = currentProgram.getListing()
    for r in refs_to_anchor:
        if monitor.isCancelled():
            break
        src = r.getFromAddress()
        # The adrp/add that materializes &sInstance is what xrefs to the
        # anchor.  We then want to find a following `ldr xN, [xM]` that
        # dereferences it.
        cur = listing.getInstructionAt(src)
        if cur is None:
            continue
        # The dest reg of the adrp/add will become the base for a later ldr.
        carry_reg = _w_to_x(_dest_reg(cur))
        if carry_reg is None:
            continue
        # Walk forward up to 8 instructions looking for `ldr xN, [carry_reg]`
        # or `ldr xN, [carry_reg, #imm]` where imm is small (sometimes the
        # anchor is loaded with an add+ldr sequence that splits page-offset
        # and dereference).
        nxt = listing.getInstructionAfter(src)
        steps = 0
        while nxt is not None and steps < 8:
            steps += 1
            mn = nxt.getMnemonicString().lower()
            if mn == "ldr":
                base = _w_to_x(_base_reg_of_memop(nxt, 1))
                if base == carry_reg:
                    # Found the dereference.  The dst register now holds gmd.
                    gmd_reg = _w_to_x(_dest_reg(nxt))
                    if gmd_reg is not None:
                        deref_sites.append((nxt.getAddress(), gmd_reg))
                    break
            # If carry_reg gets clobbered before we find the deref, give up.
            dst = _w_to_x(_dest_reg(nxt))
            if dst == carry_reg and mn != "ldr":
                break
            nxt = listing.getInstructionAfter(nxt.getAddress())
    return deref_sites


def _accumulate_gmd_field_access(deref_sites):
    """For each (deref_site, gmd_reg), walk forward FORWARD_INSNS and
    accumulate every `(ldr|str|ldrb|strb|ldrh|strh) Reg, [gmd_reg, #imm]`
    along with its function context.
    """
    listing = currentProgram.getListing()
    fn_mgr = currentProgram.getFunctionManager()

    # offset -> {mnem -> count}
    hist = {}
    # offset -> set of (fn_name, fn_entry)
    fn_index = {}

    for site_addr, gmd_reg in deref_sites:
        if monitor.isCancelled():
            break
        # Defensive: Ghidra's getFunctionContaining can throw
        # IndexOutOfBoundsException on functions with malformed parameter
        # prototypes (HomogeneousAggregate.filter bug, observed 2026-05-29).
        try:
            fn = fn_mgr.getFunctionContaining(site_addr)
        except Exception:
            fn = None
        try:
            fn_label = (fn.getName() + "@" + str(fn.getEntryPoint())
                        if fn is not None else "(no fn)")
        except Exception:
            fn_label = "(fn-lookup-failed)"
        cur = listing.getInstructionAfter(site_addr)
        steps = 0
        cur_base_reg = gmd_reg
        while cur is not None and steps < FORWARD_INSNS:
            steps += 1
            mn = cur.getMnemonicString().lower()
            if mn in ("ldr", "ldrb", "ldrh", "ldrsh", "ldrsw",
                      "str", "strb", "strh"):
                base = _w_to_x(_base_reg_of_memop(cur, 1))
                if base == cur_base_reg:
                    disp = _displacement_at_op(cur, 1)
                    if disp is None:
                        disp = 0
                    disp = disp & 0xFFFF  # gmd struct is < 64 KiB
                    by_mn = hist.setdefault(disp, {})
                    by_mn[mn] = by_mn.get(mn, 0) + 1
                    fn_index.setdefault(disp, set()).add(fn_label)
            # If our base register gets clobbered by something else, stop
            # tracking through this site (e.g., a function call clobbers
            # x0-x18).
            dst = _w_to_x(_dest_reg(cur))
            if dst == cur_base_reg and mn != "ldr":
                # A non-load that writes to gmd_reg invalidates the tracking.
                # (A second ldr through gmd_reg is fine -- still gmd.)
                break
            if mn == "bl":
                # BL clobbers x0-x18 in AAPCS.  If our gmd_reg is in that
                # range, stop tracking.
                try:
                    n = int(cur_base_reg[1:])
                    if n <= 18:
                        break
                except (ValueError, IndexError):
                    pass
            cur = listing.getInstructionAfter(cur.getAddress())

    return hist, fn_index


def main():
    print("SMBW Archipelago: walk_gmd_field_access.py")
    print("  Anchor: gmd::sInstance @ NSO +0x%x" % S_INSTANCE_NSO_OFF)
    print("  Forward-walk window: %d instructions" % FORWARD_INSNS)
    print("")

    deref_sites = _find_sInstance_dereferences()
    print("  Found %d sInstance dereference sites." % len(deref_sites))
    print("")

    hist, fn_index = _accumulate_gmd_field_access(deref_sites)
    if not hist:
        print("  No gmd-relative field accesses found.")
        return

    print("===== gmd field access histogram =====")
    print("  (offset, read=ldr+ldrb+ldrh, write=str+strb+strh)")
    print("")
    for off in sorted(hist.keys()):
        per = hist[off]
        reads = (per.get("ldr", 0) + per.get("ldrb", 0)
                 + per.get("ldrh", 0) + per.get("ldrsh", 0)
                 + per.get("ldrsw", 0))
        writes = per.get("str", 0) + per.get("strb", 0) + per.get("strh", 0)
        n_fn = len(fn_index.get(off, set()))
        label = DOCUMENTED.get(off, "")
        tag = "  <-- DOCUMENTED" if label else "  <-- NEW"
        details = " ".join("%s(%d)" % (m, c) for m, c in sorted(per.items()))
        print("  gmd+0x%04x  read=%-4d write=%-4d fns=%-3d  %s%s"
              % (off, reads, writes, n_fn, details, tag))
        if label:
            print("              -- %s" % label)
    print("")

    # Highlight unmapped offsets with the most action — these are the
    # next-RE candidates.
    print("===== top unmapped offsets (read+write desc) =====")
    unmapped = []
    for off, per in hist.items():
        if off in DOCUMENTED:
            continue
        total = sum(per.values())
        unmapped.append((off, total, per))
    unmapped.sort(key=lambda r: -r[1])
    for off, total, per in unmapped[:20]:
        per_s = ", ".join("%s=%d" % (m, c) for m, c in sorted(per.items()))
        n_fn = len(fn_index.get(off, set()))
        print("  gmd+0x%04x  total=%d  fns=%d  (%s)"
              % (off, total, n_fn, per_s))
        # Sample 3 callers for context.
        sample = sorted(fn_index.get(off, set()))[:3]
        for fn in sample:
            print("      %s" % fn)
    print("")
    print("Done.")
    print("")
    print("Next steps:")
    print("  - Any unmapped offset with >5 read accesses is a candidate for a")
    print("    new substruct (container D / E).  Decompile a sample function")
    print("    that touches it to identify the role.")
    print("  - Cross-check: if FUN_7101F2B354 and FUN_710049EA24 both")
    print("    dereference the same gmd+0xXX, that offset is where the")
    print("    per-course bitfield storage lives.")


main()
