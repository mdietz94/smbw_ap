# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — sprint 2, Phase 1.2.
#
# Walks every xref to each of the top GameDataMgr accessor functions
# discovered via find_gamedatamgr_xrefs.py, and extracts the hash-key
# constant passed to each call.  Cross-references against MemetendoYT's
# 8 verified pair-region keys.
#
# Pre-run (run-once) prerequisite: `find_gamedatamgr_xrefs.py` to
# identify the accessor function addresses below.  The defaults are
# from the 2026-05-24 first run; update if a future run reveals more.
#
# Calling convention observation (from sample xrefs):
#   - Hash constant lives in w2 for accessors like 0x710012ae94 (reader)
#     and 0x71003838ac (bool getter).
#   - Hash constant lives in w0 for callers that re-route through
#     accessor's tail (rare).
# So the script walks the backward window and accepts hash constants
# loaded into ANY w-register, then disambiguates by which register the
# call site actually uses.  AArch64 loads
# w0 with a constant via one of these patterns:
#
#     mov w0, #imm16              ; small constants 0..0xFFFF
#     movz w0, #imm16             ; zero-extended
#     movz w0, #imm_hi, lsl #16 / movk w0, #imm_lo
#     movn w0, #imm16             ; bitwise-NOT (signed-like negatives)
#     adrp x0, page / add x0, x0, #imm12  (string-pointer arg, NOT a hash)
#     mov w0, wN  (after some other reg got the constant)
#
# We resolve the simple cases statically.  For the register-aliasing
# case we follow up to 4 levels of mov w0, wN; mov wN, #imm.
#
# Output is one row per xref:
#
#     <call_site>  hash=0x<key>  in <fn_name @ entry>  [match=<known>]
#
# At the end, cross-reference the harvested keys against MemetendoYT's
# 8 verified keys (table embedded below).  Any key found 1:1 is a
# confirmed grant target.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

# Discovered GameDataMgr accessor functions, in descending xref order
# (per the 2026-05-24 find_gamedatamgr_xrefs.py output).  Each entry:
# (full_NSO_address, suspected_role)
# Comment out / add new entries based on subsequent decompilation.
ACCESSORS = [
    (0x710012AE94, "reader (66 xrefs)  — primary container Get (hash in w2)"),
    (0x710049F648, "candidate writer/setter A (46 xrefs)"),
    (0x7100124134, "candidate accessor B (42 xrefs)"),
    (0x7100472BE4, "candidate accessor C (41 xrefs)"),
    (0x71003838AC, "bool getter (38 xrefs)  — handles INTRO_CUTSCENE_COMPLETED"),
    (0x7100221128, "candidate accessor D (18 xrefs)"),
    (0x7100370264, "candidate accessor E (16 xrefs)"),
    (0x71000E258C, "candidate accessor F (14 xrefs)"),
    (0x710049EA24, "candidate accessor G (14 xrefs)"),
    (0x7100533FE4, "candidate accessor H (13 xrefs)"),
    (0x71003877C4, "candidate accessor I (10 xrefs)"),
    (0x710059F894, "GameData accessor opener (called by SetCourseClearFlag)"),
    (0x71003D3FB0, "sibling container accessor (uses [+0x260] not [+0xe0])"),
]

# Maximum instructions to walk backward from each call site.
BACKWALK_INSNS = 20

# Cross-reference against MemetendoYT's 8 verified pair-region hash
# keys (see docs/save-diff-findings.md "External corroboration" and
# docs/save-diff-findings.md "Pair-key sanity check").
KNOWN_KEYS = {
    0xf4ee6827: "flower_coin (PURPLE_COINS_PATTERN, u16)",
    0x17f0bb21: "regular_coin (COINS_PATTERN, u8)",
    0x55815859: "GRAND_SEED_WORLD1 (Royal Seed W1)",
    0x49abba86: "GRAND_SEED_WORLD2 (Royal Seed W2)",
    0xb550d8d6: "GRAND_SEED_WORLD3 (Royal Seed W3)",
    0x1dcf7f6e: "GRAND_SEED_WORLD4 (Royal Seed W4)",
    0x0d5a3e00: "GRAND_SEED_WORLD5 (Royal Seed W5)",
    0xd4660d2b: "GRAND_SEED_WORLD6 (Royal Seed W6)",
    0x5d3ec9b4: "COMPLETE_GAME (game-complete flag)",
    0x89f1cc52: "INTRO_CUTSCENE_COMPLETED",
    # Bonus: the hash from SetCourseClearFlagToGameData itself (known).
    0xdf82e9ab: "SetCourseClearFlagToGameData (course-clear write hash)",
}


def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def _imm_at_op(ins, op_idx):
    """Return the integer immediate at operand `op_idx` of `ins`,
    or None if the operand is not a recognized immediate.
    Handles signed/unsigned encodings."""
    try:
        objs = ins.getOpObjects(op_idx)
    except Exception:
        return None
    for o in objs:
        # Ghidra wraps immediates as Scalar.
        if o is None:
            continue
        cls = o.__class__.__name__
        if cls == "Scalar":
            try:
                return o.getUnsignedValue() & 0xFFFFFFFFFFFFFFFF
            except Exception:
                try:
                    return o.getValue() & 0xFFFFFFFFFFFFFFFF
                except Exception:
                    return None
    return None


def _lsl_shift_from_insn(ins):
    """Parse `LSL #N` out of the instruction's string representation.

    Ghidra's `movk w2, #0xf4ee, LSL #16` encodes the shift inside the
    operand object, NOT as a separate operand (getOpObjects(2) returns
    nothing useful).  But the textual representation always includes
    `LSL #N` when N > 0.  Returns 0 if no shift is present.
    """
    s = str(ins)
    low = s.lower()
    i = low.find("lsl #")
    if i < 0:
        return 0
    rest = s[i + 5:].strip()
    # Extract digits (decimal or 0x-prefixed).
    num = ""
    for ch in rest:
        if ch in "0123456789abcdefxABCDEFX":
            num += ch
        else:
            break
    if not num:
        return 0
    try:
        return int(num, 0)
    except ValueError:
        return 0


def _dest_reg_name(ins):
    """Mnemonics like mov/movz/movk/movn write to operand 0."""
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


def _src_reg_name(ins, op_idx):
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


def _w_to_x(reg_name):
    """Normalize wN <-> xN for the same physical register."""
    if not reg_name:
        return None
    n = reg_name.lower()
    if n.startswith("w") and n[1:].isdigit():
        return "x" + n[1:]
    return n


def reconstruct_reg_at(call_site, initial_reg="x0"):
    """Walk backward from `call_site` and reconstruct the value loaded
    into `initial_reg`.  Returns (value, source_insn_str) or (None, reason).

    Limitations: handles mov/movz/movk/movn immediate chains and
    one level of mov w?, wN aliasing.  Does NOT follow loads from
    memory, function calls, or arithmetic.
    """
    listing = currentProgram.getListing()
    # Track which physical register currently holds the constant we're
    # interested in.  Initially: x0 or whatever the caller passes.
    target_reg = initial_reg
    # For movz/movk pairs, accumulate.
    pending_value = None
    last_insn_str = None

    cur = listing.getInstructionBefore(call_site)
    walked = 0
    while cur is not None and walked < BACKWALK_INSNS:
        mnem = cur.getMnemonicString().lower()
        dst = _w_to_x(_dest_reg_name(cur))
        # If this instruction writes to our target register, inspect it.
        if dst == target_reg:
            if mnem == "mov":
                # mov w0, #imm  OR  mov w0, wN.
                # AArch64 quirk: `mov w?, #imm` with imm<=0xFFFF is encoded
                # the same as `movz` (the assembler picks the shorter
                # mnemonic).  So if we have a pending_value from a
                # preceding `movk w?, #..., LSL #16`, this `mov` is
                # actually the low-half of a 32-bit constant build:
                #     mov  w2, #0x6827           (low half — appears in code)
                #     movk w2, #0xf4ee, LSL #16  (high half)
                # When walking BACKWARD we encounter movk first
                # (setting pending_value=0xf4ee0000), then `mov` which
                # holds 0x6827.  OR the two halves to get the full key.
                imm = _imm_at_op(cur, 1)
                if imm is not None:
                    shift = _lsl_shift_from_insn(cur)
                    val = (imm << shift) & 0xFFFFFFFF
                    if pending_value is not None:
                        val |= pending_value
                    last_insn_str = "%s @ %s" % (cur, cur.getAddress())
                    return (val, last_insn_str)
                src = _w_to_x(_src_reg_name(cur, 1))
                if src and src != target_reg:
                    # Chase that register backwards next.  Keep
                    # pending_value (low half belongs to the chain).
                    target_reg = src
                    cur = cur.getPrevious()
                    walked += 1
                    continue
                return (None, "mov %s unhandled at %s" % (target_reg, cur.getAddress()))
            elif mnem in ("movz",):
                imm = _imm_at_op(cur, 1)
                if imm is None:
                    return (None, "movz no imm at %s" % cur.getAddress())
                shift = _lsl_shift_from_insn(cur)
                val = (imm << shift) & 0xFFFFFFFF
                if pending_value is not None:
                    val |= pending_value
                last_insn_str = "%s @ %s" % (cur, cur.getAddress())
                return (val, last_insn_str)
            elif mnem in ("movk",):
                imm = _imm_at_op(cur, 1)
                if imm is None:
                    return (None, "movk no imm at %s" % cur.getAddress())
                shift = _lsl_shift_from_insn(cur)
                shifted = (imm << shift) & 0xFFFFFFFF
                pending_value = (pending_value or 0) | shifted
                # Keep walking; expect a preceding movz to provide the
                # other half.
                cur = cur.getPrevious()
                walked += 1
                continue
            elif mnem in ("movn",):
                imm = _imm_at_op(cur, 1)
                if imm is None:
                    return (None, "movn no imm at %s" % cur.getAddress())
                shift = _lsl_shift_from_insn(cur)
                val = (~(imm << shift)) & 0xFFFFFFFF
                last_insn_str = "%s @ %s" % (cur, cur.getAddress())
                return (val, last_insn_str)
            elif mnem in ("adrp", "ldr", "ldrb", "ldrh"):
                return (None, "w0 set via %s (memory load) at %s" %
                        (mnem, cur.getAddress()))
            elif mnem in ("add", "sub", "or", "and", "eor"):
                return (None, "w0 set via %s (arithmetic) at %s" %
                        (mnem, cur.getAddress()))
            elif mnem == "bl":
                return (None, "w0 set by function call at %s" %
                        cur.getAddress())
            else:
                return (None, "w0 set via %s at %s (unhandled)" %
                        (mnem, cur.getAddress()))
        cur = cur.getPrevious()
        walked += 1
    return (None, "no w0 setter within %d insns" % BACKWALK_INSNS)


def _walk_one_accessor(accessor_addr_int, role_hint):
    main_base = currentProgram.getImageBase().getOffset()
    addr_int = accessor_addr_int
    if addr_int < main_base:
        addr_int = main_base + accessor_addr_int

    fn_mgr = currentProgram.getFunctionManager()
    accessor = fn_mgr.getFunctionAt(_addr(addr_int))
    if accessor is None:
        accessor = fn_mgr.getFunctionContaining(_addr(addr_int))
    if accessor is None:
        return None, []

    ref_mgr = currentProgram.getReferenceManager()
    refs = list(ref_mgr.getReferencesTo(accessor.getEntryPoint()))
    call_refs = [r for r in refs if r.getReferenceType().isCall()]

    harvest = []  # (call_site, hash_or_None, reason_or_insn, fn)
    for r in call_refs:
        src = r.getFromAddress()
        fn = fn_mgr.getFunctionContaining(src)
        # The hash key may be passed in any of w0/w1/w2/w3 depending on
        # accessor signature.  Try each.  Prefer a value that looks like
        # a 32-bit Nintendo hash (high bits set); fall back to small ints.
        best_full = (None, None)    # value with >16 bits set
        best_small = (None, None)   # any value found
        for reg in ("x2", "x0", "x1", "x3"):
            val, why = reconstruct_reg_at(src, initial_reg=reg)
            if val is None:
                continue
            entry = (val, "%s: %s" % (reg, why))
            # A 32-bit hash key always has high bits set (per MemetendoYT's
            # 8 keys, all of which are > 0x0d000000).  If the value fits
            # in 16 bits, it's likely a small constant (count, flag) — not
            # a hash key.  Prefer 32-bit candidates.
            if val > 0xFFFF:
                if best_full[0] is None:
                    best_full = entry
                # If a later register yields a 32-bit value too, that's
                # also fine — but bias toward x2 (the canonical hash arg).
                break
            elif best_small[0] is None:
                best_small = entry
        chosen = best_full if best_full[0] is not None else best_small
        if chosen[0] is None:
            harvest.append((src, None, "no setter found", fn))
        else:
            harvest.append((src, chosen[0], chosen[1], fn))

    return accessor, harvest


def main():
    print("SMBW Archipelago: walk_hash_writer_xrefs.py")
    main_base = currentProgram.getImageBase().getOffset()
    print("  main_base: 0x%x" % main_base)
    print("  scanning %d accessor functions" % len(ACCESSORS))
    print("")

    # Combined per-key cross-reference: which accessors are seen with each key,
    # which callers use which (accessor, key) pair.
    key_to_callers = {}   # hash_key -> list of (accessor_addr_int, call_site, fn)
    accessor_summaries = []  # (addr, role, n_xrefs, n_resolved, n_matched)

    for accessor_addr_int, role in ACCESSORS:
        if monitor.isCancelled():
            break
        accessor, harvest = _walk_one_accessor(accessor_addr_int, role)
        if accessor is None:
            print("[skip] 0x%x  (no function defined)" % accessor_addr_int)
            continue
        n_resolved = sum(1 for _, v, _, _ in harvest if v is not None)
        n_matched = sum(1 for _, v, _, _ in harvest if v is not None and v in KNOWN_KEYS)
        accessor_summaries.append(
            (accessor_addr_int, role, len(harvest), n_resolved, n_matched))

        print("===== accessor 0x%x  (%s) =====" % (accessor_addr_int, role))
        print("  %s @ %s" % (accessor.getName(), accessor.getEntryPoint()))
        print("  xrefs=%d  resolved=%d  known-key-matches=%d" %
              (len(harvest), n_resolved, n_matched))
        # Sort the harvest: known-key matches first.
        def sort_key(row):
            src, val, why, fn = row
            is_known = 0 if (val is not None and val in KNOWN_KEYS) else 1
            return (is_known, src.getOffset() if src is not None else 0)
        harvest.sort(key=sort_key)
        # Show only matches + first 20 of the rest, to avoid console flood.
        printed = 0
        for src, val, why, fn in harvest:
            fn_label = ("%s@%s" % (fn.getName(), fn.getEntryPoint())
                        if fn is not None else "(no fn)")
            is_match = val is not None and val in KNOWN_KEYS
            if is_match:
                tag = "MATCH:%s" % KNOWN_KEYS[val]
            elif val is not None:
                tag = "unknown"
            else:
                tag = "unresolved"
                if printed >= 20 and not is_match:
                    continue
            if val is not None:
                print("  CALL %s  hash=0x%08x  in %s  [%s]" %
                      (src, val, fn_label, tag))
            else:
                print("  CALL %s  hash=?           in %s  [%s] (%s)" %
                      (src, fn_label, tag, why))
            printed += 1
            if val is not None:
                key_to_callers.setdefault(val, []).append(
                    (accessor_addr_int, str(src), fn_label))
        print("")

    # Per-accessor summary table.
    print("")
    print("===== accessor summary =====")
    print("  accessor          xrefs  resolved  matched  role")
    for a, role, n, r, m in accessor_summaries:
        print("  0x%x   %4d   %4d      %4d   %s" % (a, n, r, m, role))
    print("")

    # Known-key coverage across all accessors.
    print("===== MemetendoYT 8-key coverage (across all accessors) =====")
    for k, name in sorted(KNOWN_KEYS.items()):
        if k in key_to_callers:
            accs = sorted(set(a for a, _, _ in key_to_callers[k]))
            acc_str = ", ".join("0x%x" % a for a in accs)
            n_callsites = len(key_to_callers[k])
            print("  0x%08x  FOUND   accessors=[%s]  callsites=%d  -- %s" %
                  (k, acc_str, n_callsites, name))
        else:
            print("  0x%08x  MISSING                                 -- %s" %
                  (k, name))
    print("")

    # Detailed (accessor, key) pair list — readable getter/setter pairs.
    print("===== distinct hash keys discovered =====")
    print("  (sorted by callsite count desc)")
    by_count = sorted(key_to_callers.items(),
                      key=lambda kv: -len(kv[1]))
    for k, callers in by_count[:80]:
        accs = sorted(set(a for a, _, _ in callers))
        acc_str = ", ".join("0x%x" % a for a in accs)
        known = " <- %s" % KNOWN_KEYS[k] if k in KNOWN_KEYS else ""
        print("  0x%08x  callsites=%d  accessors=[%s]%s" %
              (k, len(callers), acc_str, known))
    if len(by_count) > 80:
        print("  ... (%d more keys not shown)" % (len(by_count) - 80))
    print("")
    print("Done.")
    print("")
    print("Next steps:")
    print("  - Sort accessors by 'known-key-matches' descending.  The top-3")
    print("    accessors are the highest-confidence Getter / Setter / Bool-")
    print("    Get/Set candidates for the M3.3b grant API.")
    print("  - For each (key, accessor) pair: the accessor with the most")
    print("    matches is likely the Get; the corresponding Set may be one")
    print("    of the other accessors used WITH THE SAME KEY.  If a key")
    print("    appears in only one accessor, that accessor probably handles")
    print("    both Get and Set (overloaded — distinguished by arg count).")


main()
