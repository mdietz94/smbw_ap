# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — M3.2 sprint run #10.
#
# Multi-target version of walk_object_accessor_hashes.py.  Given a list
# of CANDIDATE accessor function NSO offsets, walks every xref to each
# one, reconstructs the hash constant passed in w2, and emits a per-
# accessor summary that flags:
#
#   - which hashes are KNOWN (cross-referenced against the 23-entry
#     corpus from runs #8 and the original sprint 2)
#   - which hashes are UNKNOWN (new keyspace — interesting!)
#   - which callsites have a "u64 footprint" nearby (str x_ / ldr x_
#     within the same arg-prep window, indicating 8-byte payload)
#   - which callsites have a "bit-iter footprint" nearby (rbit / clz /
#     tbnz / repeated lsr, indicating bitfield decode)
#
# Inputs are the high-frequency UNKNOWN direct-call targets identified
# in find_gamedatamgr_xrefs.py's dispatch-kind tally — the function
# addresses that the binary calls many times against gmd::sInstance
# but that we haven't yet characterized.
#
# Hypothesis: the badge ownership u64 bitfield is accessed through a
# u64-typed sibling of either the container-A reader/writer family or
# the run-#8 enum writer family.  Adjacent function addresses
# (e.g. 0x710049ea24 sitting 0x1224 bytes before writer 0x710049f648)
# are prime suspects.
#
# Output structure:
#   For each candidate accessor:
#     - total xref count
#     - per-hash callsite count
#     - known/unknown bucket counts
#     - aggregate u64-footprint and bit-iter-footprint hit counts
#   Then a cross-cutting summary: candidates ranked by "most likely to
#   be the badge u64 accessor" (heavy weight on unknown hashes + u64
#   footprint + bit-iter).

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function


# ---------------------------------------------------------------------------
# Candidate accessor NSO offsets (from run #10 dispatch-kind tally).
# Format: (nso_offset, frequency_in_dispatch_tally, note)
# Sorted roughly by leverage.
# ---------------------------------------------------------------------------
CANDIDATES = [
    # Tier 1 — very high frequency, unknown
    (0x00124134, 42, "adjacent to container-A reader 0x12ae94"),
    (0x00472be4, 41, "standalone, very common"),
    (0x0049ea24, 14, "ADJACENT to container-A writer 0x49f648 — prime sibling suspect"),
    (0x0049ea78,  5, "second sibling adjacent to writer"),
    (0x00387a84,  6, "adjacent to enum writer 0x3877c4"),

    # Tier 2 — moderate frequency, unknown, or doc-mentioned
    (0x00221128, 18, "paired with 0x221278; unknown region"),
    (0x00221278,  8, "sister of 0x221128"),
    (0x00370264, 16, "near sub-bool reader 0x3838ac"),
    (0x003703e4,  2, "sister of 0x370264"),
    (0x000e258c, 14, "low-region accessor, very early in NSO"),
    (0x00533fe4, 13, "mid-region accessor"),
    (0x000ed07c,  8, "low-region accessor"),
    (0x005c1a18,  7, "mid-region"),
    (0x005e2528,  7, "CLOSE to FUN_71005E93FC — doc-flagged container-B writer candidate"),
    (0x005046dc,  9, "mid-region"),
    (0x006650ec,  7, "mid-region"),
    (0x006ad0e0,  6, "mid-region"),
    (0x009b4400,  6, "mid-region"),
    (0x0059f894,  1, "★ explicit container-B writer candidate from sprint 2 doc"),

    # Tier 3 — 0x7101f2XXXX cluster: siblings of FUN_7101F27B78 object-getter
    (0x01f2b354,  5, "0x7101f2 cluster — sibling of object-getter"),
    (0x01f290cc,  4, "0x7101f2 cluster"),
    (0x01f2b584,  4, "0x7101f2 cluster"),
]

# ---------------------------------------------------------------------------
# Known hash corpus (runs #1-#9 + sprint 2).  Used to bucket each
# accessor's hash list into known/unknown.
# ---------------------------------------------------------------------------
KNOWN_HASHES = {
    # Sprint 2 — container A, save-fields
    0xf4ee6827: "flower_coin",
    0x17f0bb21: "regular_coin",
    0x55815859: "GRAND_SEED_WORLD1",
    0x49abba86: "GRAND_SEED_WORLD2",
    0xb550d8d6: "GRAND_SEED_WORLD3",
    0x1dcf7f6e: "GRAND_SEED_WORLD4",
    0x0d5a3e00: "GRAND_SEED_WORLD5",
    0xd4660d2b: "GRAND_SEED_WORLD6",
    0x5d3ec9b4: "COMPLETE_GAME",
    0x89f1cc52: "INTRO_CUTSCENE_COMPLETED",
    0xdf82e9ab: "course-clear lookup",
    0xed817774: "touch_goal_top_result bool",
    0xf79bcbb0: "current course goal_id counter",
    # Run #8 — per-player session populator hashes
    0x2d8c6ec0: "per-player session field",
    0x5f07db24: "per-player session field",
    0xe82403c2: "per-player session field",
    0xdd62141a: "per-player session field",
    0xf0d05e3a: "per-player session field (course-info input)",
    0x6ba8ad3d: "badge_id_array[0] (P1 equipped)",
    0x1415f836: "badge_id_array[1] (P2 equipped)",
    0xb1ae38a4: "badge_id_array[2] (P3 equipped)",
    # Run #8 — course-event reporter
    0x3b241921: "difficulty enum",
    0x1b56d494: "result state enum",
    0x5bf37fa9: "current course (Course1..81 string)",
    0xb8c53575: "course state enum",
    0xf20e6a36: "course-derived counter",
}

BACKWALK_INSNS = 16

IMM_BUILD_MNEMONICS = ("mov", "movz", "movk")

# u64 footprint markers — 8-byte memory ops that suggest u64-typed
# value flow either into (write) or out of (read) the accessor.
U64_MNEMONICS = ("ldr",  "str")  # we filter by x_ register below
# Bit-iter markers around the callsite.
BIT_ITER_MNEMONICS = ("clz", "rbit", "tbz", "tbnz")

# How far around each callsite to scan for u64/bit-iter footprints.
FOOTPRINT_RADIUS = 16


# ---------------------------------------------------------------------------

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
    if name is None:
        return None
    n = name.lower()
    if n.startswith(("w", "x")) and n[1:].isdigit():
        return n[1:]
    return n


def _is_x_reg(name):
    """True if name is xN (64-bit), not wN (32-bit)."""
    if name is None:
        return False
    n = name.lower()
    return n.startswith("x") and len(n) > 1 and n[1:].isdigit()


def _trace_hash_in_w2(call_site_addr):
    """Walk backward; first-hit-wins; stop at prior bl.
    (Same logic as the patched walk_object_accessor_hashes.py.)"""
    listing = currentProgram.getListing()
    cur = listing.getInstructionBefore(call_site_addr)
    walked = 0
    low_half = None
    high_half = None
    while cur is not None and walked < BACKWALK_INSNS:
        mnem = cur.getMnemonicString().lower()
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


def _detect_u64_and_bititer(call_site_addr, radius=FOOTPRINT_RADIUS):
    """Scan +/- `radius` instructions around the call site for:
      - u64-footprint: ldr/str on an xN register (8-byte mem op)
      - bit-iter:     rbit, clz, tbz/tbnz
    Returns (u64_hits, bititer_hits, sample_mnemonics)."""
    listing = currentProgram.getListing()
    u64_hits = 0
    bit_hits = 0
    sample = []

    def _scan(start_ins, direction):
        nonlocal_u64 = [0]
        nonlocal_bit = [0]
        nonlocal_smp = []
        cur = start_ins
        walked = 0
        while cur is not None and walked < radius:
            mnem = cur.getMnemonicString().lower()
            if mnem in BIT_ITER_MNEMONICS:
                nonlocal_bit[0] += 1
                if len(nonlocal_smp) < 4:
                    nonlocal_smp.append(mnem)
            elif mnem in U64_MNEMONICS:
                # Check if first operand is an x-register (64-bit).
                first_reg = _reg_at_op(cur, 0)
                if _is_x_reg(first_reg):
                    nonlocal_u64[0] += 1
                    if len(nonlocal_smp) < 4:
                        nonlocal_smp.append(mnem + ":" + first_reg)
            if direction == "fwd":
                cur = cur.getNext()
            else:
                cur = cur.getPrevious()
            walked += 1
        return nonlocal_u64[0], nonlocal_bit[0], nonlocal_smp

    fwd_u64, fwd_bit, fwd_smp = _scan(
        listing.getInstructionAfter(call_site_addr), "fwd")
    back_u64, back_bit, back_smp = _scan(
        listing.getInstructionBefore(call_site_addr), "back")

    u64_hits = fwd_u64 + back_u64
    bit_hits = fwd_bit + back_bit
    sample = fwd_smp + back_smp
    return u64_hits, bit_hits, sample[:6]


def _walk_one(target_int, note, freq):
    ref_mgr = currentProgram.getReferenceManager()
    fn_mgr = currentProgram.getFunctionManager()
    refs = list(ref_mgr.getReferencesTo(_addr(target_int)))
    call_refs = []
    for r in refs:
        rt = r.getReferenceType()
        if hasattr(rt, "isCall") and rt.isCall():
            call_refs.append(r)
    if not call_refs:
        call_refs = refs

    hash_counts = {}
    u64_total = 0
    bit_total = 0
    callsite_rows = []
    for r in call_refs:
        src = r.getFromAddress()
        h = _trace_hash_in_w2(src)
        u64, bit, smp = _detect_u64_and_bititer(src)
        u64_total += u64
        bit_total += bit
        hash_counts[h] = hash_counts.get(h, 0) + 1
        fn = fn_mgr.getFunctionContaining(src)
        fn_label = fn.getName() if fn is not None else "?"
        callsite_rows.append((src, h, u64, bit, smp, fn_label))

    return {
        "target_int": target_int,
        "note": note,
        "tally_freq": freq,
        "total_xrefs": len(call_refs),
        "hash_counts": hash_counts,
        "u64_total": u64_total,
        "bit_total": bit_total,
        "callsite_rows": callsite_rows,
    }


def _classify_accessor(result):
    """Rough 'how likely is this the badge u64 accessor' score.

    Heuristics:
      +N for each UNKNOWN hash (new keyspace)
      +N for each u64 footprint hit
      +N for each bit-iter hit
      penalty if ALL hashes are known (boring — same fields, different view)
    """
    unknown_hashes = sum(1 for h in result["hash_counts"]
                        if h is not None and h not in KNOWN_HASHES)
    known_hashes = sum(1 for h in result["hash_counts"]
                      if h is not None and h in KNOWN_HASHES)
    unresolved = result["hash_counts"].get(None, 0)
    score = 0
    score += 8 * unknown_hashes
    score += 1 * result["u64_total"]
    score += 4 * result["bit_total"]
    if known_hashes > 0 and unknown_hashes == 0:
        score -= 5  # all-known = probably a sibling view of same data
    return score, unknown_hashes, known_hashes, unresolved


def main():
    print("SMBW Archipelago: walk_multi_accessor_hashes.py")
    print("  Walking xrefs to %d candidate accessors" % len(CANDIDATES))
    print("")

    main_base = currentProgram.getImageBase().getOffset()

    results = []
    for nso_off, freq, note in CANDIDATES:
        target_int = main_base + nso_off
        try:
            r = _walk_one(target_int, note, freq)
            results.append(r)
        except Exception as e:
            print("  ! error walking 0x%x: %s" % (target_int, e))

    # Per-accessor detailed dump
    print("=" * 78)
    print("PER-ACCESSOR HASH BREAKDOWN")
    print("=" * 78)
    for r in results:
        score, unk, kno, unr = _classify_accessor(r)
        print("")
        print("--- FUN_%x (tally:%d  walked-xrefs:%d  note:%s) ---" %
              (r["target_int"], r["tally_freq"], r["total_xrefs"], r["note"]))
        print("    score:%d  unknown-hashes:%d  known-hashes:%d  unresolved:%d  "
              "u64-footprint:%d  bit-iter:%d" %
              (score, unk, kno, unr, r["u64_total"], r["bit_total"]))
        sorted_hashes = sorted(r["hash_counts"].items(),
                               key=lambda kv: (-kv[1],
                                               kv[0] if kv[0] is not None else 0))
        for h, count in sorted_hashes:
            if h is None:
                label = "(unresolved)"
            elif h in KNOWN_HASHES:
                label = "0x%08x  KNOWN  [%s]" % (h, KNOWN_HASHES[h])
            else:
                label = "0x%08x  UNKNOWN" % h
            print("      %3dx  %s" % (count, label))

    # Cross-cutting ranking
    print("")
    print("=" * 78)
    print("RANKED BY BADGE-U64-LIKELIHOOD SCORE")
    print("=" * 78)
    ranked = sorted(results,
                    key=lambda r: -_classify_accessor(r)[0])
    print("")
    print("%-22s %6s %6s %6s %6s %6s   %s" %
          ("function", "score", "unk", "kno", "u64", "bitit", "note"))
    print("-" * 78)
    for r in ranked:
        score, unk, kno, unr = _classify_accessor(r)
        print("FUN_%x  %6d %6d %6d %6d %6d   %s" %
              (r["target_int"], score, unk, kno,
               r["u64_total"], r["bit_total"], r["note"]))

    # Unknown-hash corpus across ALL candidates
    print("")
    print("=" * 78)
    print("ALL UNKNOWN HASHES SEEN (aggregated across candidates)")
    print("=" * 78)
    aggregate = {}
    for r in results:
        for h, count in r["hash_counts"].items():
            if h is None or h in KNOWN_HASHES:
                continue
            entry = aggregate.setdefault(h, {"total": 0, "accessors": []})
            entry["total"] += count
            entry["accessors"].append(("0x%x" % r["target_int"], count))
    sorted_unk = sorted(aggregate.items(), key=lambda kv: -kv[1]["total"])
    for h, info in sorted_unk:
        accs = ", ".join("%s(%d)" % a for a in info["accessors"])
        print("  0x%08x  total:%-3d  via: %s" % (h, info["total"], accs))

    print("")
    print("Done.")


main()
