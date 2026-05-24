# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — M3.2 sprint run #9.
#
# Walks every xref to FUN_7101F27B78, the newly-discovered "object-
# pointer getter" GameDataMgr accessor.  Signature (confirmed at 8
# call sites in FUN_7101c62368):
#
#     FUN_7101F27B78(GameDataMgr* gmd, void** out_obj_ptr, uint32_t hash)
#
# Unlike sprint-2 accessors that return primitive values, this one
# returns a sub-object pointer (probably a `gmd::GameDataField*` or
# similar wrapper).  Callers then pass the returned pointer to a
# typed extractor (e.g., FUN_7101c626e0) to read the actual value.
#
# Hypothesis: the badge ownership bitfield (u64) is held as a
# GameDataField hash-keyed under SOME unknown hash and accessed via
# this accessor.  Scanning every callsite for the `hash` constant
# (passed in w2) will produce a list including the badge ownership
# hash.  We then identify it among the candidates by:
#
#   - Its call site does u64-shaped extraction (look for str/ldr
#     x_reg patterns after the typed-extractor call), OR
#   - Its enclosing function does bit-iteration (rbit/clz/tbnz on
#     the extracted value), OR
#   - Cross-reference with known-named hashes (badge-shop /
#     badge-equip context strings nearby)
#
# Output: per-callsite hash + enclosing function + post-call extractor
# function (heuristic: the first BL after the accessor call).

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function


# NSO offset of FUN_7101F27B78 (the object-pointer getter).
OBJECT_ACCESSOR_NSO_OFFSET = 0x01F27B78

# How far backward to walk from each call site to find the mov/movz/movk
# that builds the hash constant in w2.  AArch64 32-bit constants:
#   mov  w2, #LOW16          ; MOVZ encoding
#   movk w2, #HIGH16, LSL 16 ; sets upper 16 bits
# So we need to track both halves.
BACKWALK_INSNS = 16

# How far forward to walk to find the typed-extractor (first BL after
# the accessor returns).
FORWALK_INSNS = 8

# Mnemonics that build constants into registers.
IMM_BUILD_MNEMONICS = ("mov", "movz", "movk")

# Bit-iteration patterns that indicate u64 bitfield processing.
BIT_ITER_MNEMONICS = ("clz", "rbit", "ubfx", "sbfx", "cnt",
                      "tbz", "tbnz", "lsr", "asr")

# Limit per-bucket output.
MAX_CALLSITES = 256


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


def _reg_at_op(ins, op_idx):
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


def _normalize_reg(name):
    """Treat w2 / x2 as the same register identifier '2'."""
    if name is None:
        return None
    n = name.lower()
    if n.startswith(("w", "x")) and n[1:].isdigit():
        return n[1:]
    return n


def _trace_hash_in_w2(call_site_addr):
    """Walk backward from call site, accumulating mov/movz/movk into w2/x2.
    Returns the reconstructed 32-bit hash or None.

    Fix (run #9 post-mortem): the original implementation kept walking
    past prior `bl` boundaries, and unconditionally OVERWROTE low_half
    / high_half on every match.  Combined with how the v1.0.0 binary
    schedules adjacent FUN_7101F27B78 callsites (the LOW16 of slot
    i+1 is hoisted before the LOW16 of slot i in the prologue setup
    region), this produced hybrid hashes of the form
        upper16[slot_i] | lower16[slot_{i+1}]
    across every interior callsite in FUN_7101c623ec.  Fix:
      - Stop at any prior `bl` (we've crossed into a previous
        callsite's argument setup region).
      - Closest-to-bl wins for each half (assign only if None).
      - Early-exit once both halves are known.
      - For mov / movz, also honor the LSL #16 modifier (movz w2,
        #X, LSL 16 is a valid alias for "set high half, clear low").
    """
    listing = currentProgram.getListing()
    cur = listing.getInstructionBefore(call_site_addr)
    walked = 0
    low_half = None
    high_half = None
    while cur is not None and walked < BACKWALK_INSNS:
        mnem = cur.getMnemonicString().lower()
        # Crossed into a prior callsite's arg setup — stop.
        if mnem == "bl":
            break
        if mnem in IMM_BUILD_MNEMONICS:
            dest = _normalize_reg(_reg_at_op(cur, 0))
            if dest == "2":
                imm = _imm_at_op(cur, 1)
                if imm is not None:
                    s = str(cur).lower()
                    is_high = ("lsl #0x10" in s or "lsl #16" in s)
                    if is_high:
                        if high_half is None:
                            high_half = imm & 0xFFFF
                    else:
                        if low_half is None:
                            low_half = imm & 0xFFFF
        if low_half is not None and high_half is not None:
            break
        cur = cur.getPrevious()
        walked += 1
    if low_half is None and high_half is None:
        return None
    return ((high_half or 0) << 16) | (low_half or 0)


def _find_post_extractor(call_site_addr):
    """Forward-walk from call site looking for the next BL — that's
    the typed extractor that consumes the returned object pointer.
    Returns the target address or None."""
    listing = currentProgram.getListing()
    cur = listing.getInstructionAfter(call_site_addr)
    walked = 0
    while cur is not None and walked < FORWALK_INSNS:
        mnem = cur.getMnemonicString().lower()
        if mnem == "bl":
            try:
                tgt = cur.getAddress(0)
                if tgt is not None:
                    return tgt.getOffset()
            except Exception:
                pass
        cur = cur.getNext()
        walked += 1
    return None


def _detect_bit_iter_nearby(call_site_addr, radius=32):
    """Scan +/- `radius` instructions around the call site for
    bit-iteration mnemonics that suggest u64 bitfield processing."""
    listing = currentProgram.getListing()
    found = []
    # Forward scan
    cur = listing.getInstructionAfter(call_site_addr)
    walked = 0
    while cur is not None and walked < radius:
        mnem = cur.getMnemonicString().lower()
        if mnem in BIT_ITER_MNEMONICS:
            found.append(mnem)
            if len(found) >= 4:
                break
        cur = cur.getNext()
        walked += 1
    # Backward scan
    cur = listing.getInstructionBefore(call_site_addr)
    walked = 0
    while cur is not None and walked < radius:
        mnem = cur.getMnemonicString().lower()
        if mnem in BIT_ITER_MNEMONICS:
            found.append(mnem)
            if len(found) >= 4:
                break
        cur = cur.getPrevious()
        walked += 1
    return found


def main():
    print("SMBW Archipelago: walk_object_accessor_hashes.py")
    print("  Walking xrefs to FUN_7101F27B78 (NEW object-pointer getter)")
    print("")

    main_base = currentProgram.getImageBase().getOffset()
    accessor_addr = _addr(main_base + OBJECT_ACCESSOR_NSO_OFFSET)
    print("  Target: %s (NSO +0x%x)" %
          (accessor_addr, OBJECT_ACCESSOR_NSO_OFFSET))

    ref_mgr = currentProgram.getReferenceManager()
    fn_mgr = currentProgram.getFunctionManager()
    refs = list(ref_mgr.getReferencesTo(accessor_addr))

    # Filter to call refs
    call_refs = []
    for r in refs:
        rt = r.getReferenceType()
        if hasattr(rt, "isCall") and rt.isCall():
            call_refs.append(r)
    if not call_refs:
        # Fallback: take all if call classification empty
        call_refs = refs
        print("  (no call-classified xrefs; using all xrefs)")
    print("  %d call site(s)" % len(call_refs))

    # Per-callsite analysis
    rows = []  # (hash, callsite_addr, enclosing_fn, post_extractor, bit_iter_hits)
    for r in call_refs[:MAX_CALLSITES]:
        src = r.getFromAddress()
        h = _trace_hash_in_w2(src)
        fn = fn_mgr.getFunctionContaining(src)
        fn_label = ("%s @ %s" % (fn.getName(), fn.getEntryPoint())
                    if fn is not None else "(no fn)")
        post = _find_post_extractor(src)
        post_label = ("FUN_%x" % post) if post is not None else "(none)"
        bit_iter = _detect_bit_iter_nearby(src)
        rows.append((h, src, fn_label, post_label, bit_iter))

    # Group by hash for the headline summary
    by_hash = {}
    for h, src, fn_label, post_label, bit_iter in rows:
        by_hash.setdefault(h, []).append((src, fn_label, post_label, bit_iter))

    # Output: per-hash listing
    print("")
    print("=" * 72)
    print("HASHES USED WITH FUN_7101F27B78 (sorted by frequency desc)")
    print("=" * 72)
    print("")

    # Known hashes for cross-reference
    KNOWN = {
        0x2d8c6ec0: "per-player session field (run #8)",
        0x5f07db24: "per-player session field (run #8)",
        0xe82403c2: "per-player session field (run #8)",
        0xdd62141a: "per-player session field (run #8)",
        0xf0d05e3a: "per-player session field — course-info input (run #8)",
        0x6ba8ad3d: "badge_id_array[0] P1 equipped (run #8)",
        0x1415f836: "badge_id_array[1] P2 equipped (run #8)",
        0xb1ae38a4: "badge_id_array[2] P3 equipped (run #8)",
    }

    sorted_hashes = sorted(by_hash.items(),
                           key=lambda kv: (-len(kv[1]),
                                           kv[0] if kv[0] is not None else 0))
    for h, sites in sorted_hashes:
        if h is None:
            label = "(unresolved hash — backwalk failed)"
        else:
            known = KNOWN.get(h, "")
            label = ("0x%08x" % h) + (" [%s]" % known if known else "")
        print("")
        print("--- %s  (%d callsite%s) ---" %
              (label, len(sites), "s" if len(sites) != 1 else ""))
        for src, fn_label, post_label, bit_iter in sites:
            bit_note = ""
            if bit_iter:
                bit_note = "  ★ BIT-ITER NEARBY: %s" % ",".join(bit_iter[:4])
            print("    %s   in %s   -> post-extract: %s%s" %
                  (src, fn_label, post_label, bit_note))

    # Headline summary
    print("")
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print("")
    print("Total call sites: %d" % len(rows))
    print("Distinct hashes:  %d" % len(by_hash))
    print("")
    print("HOW TO IDENTIFY THE BADGE OWNERSHIP HASH:")
    print("")
    print("1. Look for hashes with BIT-ITER NEARBY marker — those are")
    print("   the most likely owned-bitfield consumers (bit iteration")
    print("   over a u64).")
    print("")
    print("2. Look at the 'post-extract' column.  Each accessor call")
    print("   passes the returned obj pointer to a typed extractor.")
    print("   Decompiling one extractor function reveals whether it")
    print("   reads u32 (counter) or u64 (bitfield).  The badge-owned")
    print("   extractor reads u64.")
    print("")
    print("3. Cross-check known hashes (above) to rule out per-player")
    print("   session fields.  The badge ownership hash is NOT one of")
    print("   the 8 we already named.")
    print("")
    print("4. Hashes used in functions whose names suggest equip-menu /")
    print("   shop / badge-unlock context are prime suspects.")
    print("")
    print("Done.")


main()
