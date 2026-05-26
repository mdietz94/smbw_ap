# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — Wonder Seed gate RE
# (2026-05-26 plan: Phase 3 corroborator).
#
# Specialized adaptation of playreport_field_backtrace.py focused on
# seed-related PlayReport fields, with one additional pass to filter
# OUT the known PlayReport-builder chain so non-telemetry readers of
# the same value-loads surface independently.
#
# The hypothesis: the in-game gate-check function reads "how many
# Wonder Seeds have I collected in world X?" from the same backing
# storage as the PlayReport builder.  If we identify the offset/base
# used in the PlayReport builder's value-load (e.g. `ldr w8, [x22,
# #0xAB]`), then any OTHER function in the binary that performs the
# same load is consuming the same field — and at least one of those
# is the gate check.
#
# Output format:
#
#   Phase 1 — backtrace each seed field:
#     field='total_get_finish_seed_count'  load=ldr w8,[x22,#0xAB]
#         in fn FUN_7101a5d93c (PlayReport builder)
#
#   Phase 2 — for each unique (base_reg, offset) seen in phase 1,
#   scan the whole binary for matching loads.  Print each, with the
#   enclosing function.  Exclude the PlayReport-builder chain
#   (FUN_7101a5d93c and known descendants) so non-telemetry consumers
#   stand out.
#
# A function appearing in Phase 2 but NOT in the PlayReport chain is
# a CANDIDATE GATE READER for that seed field.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

# Seed-related PlayReport fields (subset of PR_FIELDS in the parent
# script).  These are the only fields whose values can plausibly be
# the gate input.
SEED_FIELDS = [
    "total_get_finish_seed_count",  # primary — sum across the session
    "wonder_seed",                  # world_activity / world_result
    "world_mother_seed",            # palace WIN cross-check
    "world_wonder_flower",          # purple-coin cousin (not a seed,
                                    # but lives in the same struct
                                    # region per cheat DB)
]

# PlayReport-builder functions known from the M2.4 corpus / sprint 2
# decompile.  Any value-load that resolves to one of these is
# TELEMETRY, not a gate.  Functions that share the same load idiom but
# AREN'T in this set are gate-reader candidates.
PLAYREPORT_BUILDERS = {
    0x7101a5d93c: "course_result builder",
    0x7101a5d9a0: "course_result extension (session struct populator)",
    0x7101a5de58: "session-struct field loader",
    0x7101a5ea50: "PlayReport::Add wrapper",
}

# How far back to walk from the PlayReport::Add call to find the
# instruction that loaded the value into the value-register.
BACKWALK_INSNS = 12

# Max xrefs per field-name string to investigate.
MAX_XREFS_PER_FIELD = 8

# Phase-2 scan cap (per (base_reg, offset) pattern).
MAX_PHASE2_HITS_PER_PATTERN = 64


def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def _find_strings_with_content(content):
    memory = currentProgram.getMemory()
    target_bytes = bytearray(content.encode("ascii") + b"\x00")
    n = len(target_bytes)
    found = []
    for block in memory.getBlocks():
        if not block.isInitialized():
            continue
        if not block.isRead():
            continue
        if block.isExecute():
            continue
        start = block.getStart().getOffset()
        size = block.getSize()
        CHUNK = 65536
        for chunk_start in range(0, size, CHUNK):
            if monitor.isCancelled():
                return found
            sz = min(CHUNK + n, size - chunk_start)
            try:
                bs = bytearray()
                for i in range(sz):
                    bs.append(memory.getByte(_addr(start + chunk_start + i)) & 0xff)
            except Exception:
                continue
            idx = 0
            while True:
                pos = bs.find(target_bytes, idx)
                if pos == -1:
                    break
                if pos == 0 or bs[pos - 1] == 0:
                    found.append(start + chunk_start + pos)
                idx = pos + 1
                if len(found) > 32:
                    return found
    return found


def _trace_value_load(call_site, value_reg):
    listing = currentProgram.getListing()
    cur = listing.getInstructionBefore(call_site)
    walked = 0
    while cur is not None and walked < BACKWALK_INSNS:
        mnem = cur.getMnemonicString().lower()
        if cur.getNumOperands() > 0:
            try:
                objs = cur.getOpObjects(0)
            except Exception:
                objs = []
            for o in objs:
                if o is None:
                    continue
                if o.__class__.__name__ == "Register":
                    rn = o.getName().lower()
                    if rn == value_reg or (
                        rn.startswith(("w", "x")) and value_reg.startswith(("w", "x"))
                        and rn[1:] == value_reg[1:]
                    ):
                        return (cur, "%s %s" % (mnem, cur))
        cur = cur.getPrevious()
        walked += 1
    return (None, "no setter within %d insns" % BACKWALK_INSNS)


def _parse_load_load_idiom(ins):
    """If `ins` is `ldr Wn, [Xm, #imm]` (or similar), return
    (base_reg, offset_int).  Otherwise None.

    This is intentionally syntactic — uses the instruction's string
    representation to find the `[XN, #imm]` shape.  Works for
    ldr/ldrb/ldrh/ldrsb/ldrsh/ldrsw and str variants too."""
    if ins is None:
        return None
    s = str(ins)
    # Find the `[xN,#0x...]` or `[xN, #N]` segment.
    lb = s.find("[")
    rb = s.find("]")
    if lb < 0 or rb < 0 or rb <= lb:
        return None
    body = s[lb + 1:rb].strip()
    # Expect "xN" or "wN" optionally followed by ", #N".
    parts = body.split(",")
    base = parts[0].strip().lower()
    if not (base.startswith("x") or base.startswith("w") or base == "sp"):
        return None
    off = 0
    for p in parts[1:]:
        p = p.strip()
        if p.startswith("#"):
            try:
                off = int(p[1:], 0)
            except ValueError:
                return None
            break
    return (base, off)


def _analyze_xref(field_name, xref):
    listing = currentProgram.getListing()
    site = xref.getFromAddress()
    fn_mgr = currentProgram.getFunctionManager()
    fn = fn_mgr.getFunctionContaining(site)
    fn_label = ("%s @ %s" % (fn.getName(), fn.getEntryPoint())
                if fn is not None else "(no enclosing fn)")

    # Walk forward to the PlayReport::Add bl.
    cur = listing.getInstructionAt(site)
    forward = 0
    found_call = None
    while cur is not None and forward < 8:
        mnem = cur.getMnemonicString().lower()
        if mnem == "bl":
            try:
                tgt = cur.getAddress(0)
                if tgt is not None:
                    fn_at_tgt = fn_mgr.getFunctionAt(tgt)
                    if fn_at_tgt is not None:
                        name = fn_at_tgt.getName()
                        if "Add" in name or "PlayReport" in name:
                            found_call = (cur, fn_at_tgt)
                            break
                    if found_call is None:
                        found_call = (cur, fn_at_tgt)
            except Exception:
                pass
        cur = cur.getNext()
        forward += 1

    if found_call is None:
        return (field_name, str(site), fn_label, None, None, "no bl forward")

    call_ins, _ = found_call
    for value_reg in ("x2", "w2", "x1", "w1"):
        load_ins, load_src = _trace_value_load(
            call_ins.getAddress(), value_reg)
        if load_ins is not None:
            load_idiom = _parse_load_load_idiom(load_ins)
            return (
                field_name,
                str(call_ins.getAddress()),
                fn_label,
                "value(%s): %s" % (value_reg, load_src),
                load_idiom,
                None,
            )
    return (field_name, str(call_ins.getAddress()), fn_label,
            None, None, "no value load found")


def phase1(field_name):
    """Backtrace one PlayReport field.  Returns list of (base, offset)
    pairs harvested from value-loads."""
    print("")
    print("===== Phase 1: '%s' =====" % field_name)
    str_addrs = _find_strings_with_content(field_name)
    if not str_addrs:
        print("  (string not in .rodata)")
        return []
    print("  string occurrences: %d" % len(str_addrs))

    ref_mgr = currentProgram.getReferenceManager()
    patterns = []  # (base, off, fn_label)
    for sa_int in str_addrs:
        sa = _addr(sa_int)
        refs = list(ref_mgr.getReferencesTo(sa))
        for r in refs[:MAX_XREFS_PER_FIELD]:
            if monitor.isCancelled():
                return patterns
            field, call_site, fn_label, value_info, load_idiom, err = \
                _analyze_xref(field_name, r)
            if value_info:
                pat_str = (" pat=%s+#0x%x" % load_idiom) if load_idiom else ""
                print("  xref %s -> %s  callsite=%s  in %s%s" %
                      (r.getFromAddress(), value_info, call_site,
                       fn_label, pat_str))
                if load_idiom is not None:
                    patterns.append((load_idiom[0], load_idiom[1], fn_label))
            else:
                print("  xref %s  callsite=%s  in %s  [%s]" %
                      (r.getFromAddress(), call_site, fn_label, err))
    return patterns


def phase2(patterns_by_field):
    """For each unique (base, off) pair, scan the whole binary for
    matching loads.  Filter out the PlayReport builder chain so
    non-telemetry consumers (gate-reader candidates) stand out."""
    print("")
    print("===== Phase 2: non-PlayReport consumers of seed-field loads =====")
    print("")

    fn_mgr = currentProgram.getFunctionManager()
    listing = currentProgram.getListing()

    # Build the union of unique (base, off) pairs to scan for.
    pairs = {}
    for field, plist in patterns_by_field.items():
        for base, off, _ in plist:
            pairs.setdefault((base, off), set()).add(field)
    if not pairs:
        print("  (no load patterns harvested in Phase 1)")
        return

    main_base = currentProgram.getImageBase().getOffset()
    pr_builder_addrs = set(PLAYREPORT_BUILDERS.keys())

    for (base, off), fields in sorted(pairs.items(), key=lambda kv: kv[0]):
        if monitor.isCancelled():
            break
        print("--- pattern: [%s, #0x%x]  (fields: %s) ---"
              % (base, off, ", ".join(sorted(fields))))
        # Scan all instructions for matching loads.
        hits = []
        inst_iter = listing.getInstructions(True)
        for ins in inst_iter:
            if monitor.isCancelled():
                break
            mnem = ins.getMnemonicString().lower()
            if mnem not in ("ldr", "ldrb", "ldrh", "ldrsb", "ldrsh",
                            "ldrsw", "str", "strb", "strh"):
                continue
            idiom = _parse_load_load_idiom(ins)
            if idiom is None:
                continue
            if idiom[0] != base or idiom[1] != off:
                continue
            fn = fn_mgr.getFunctionContaining(ins.getAddress())
            hits.append((ins, fn))
            if len(hits) >= MAX_PHASE2_HITS_PER_PATTERN:
                break

        # Separate into PlayReport-chain vs other.
        pr_chain = []
        other = []
        for ins, fn in hits:
            if fn is None:
                other.append((ins, fn))
                continue
            entry_off = fn.getEntryPoint().getOffset()
            # Normalize to NSO-relative (PLAYREPORT_BUILDERS values are
            # already NSO-relative full addresses).
            if entry_off in pr_builder_addrs:
                pr_chain.append((ins, fn))
            else:
                other.append((ins, fn))

        print("  total matches: %d  (PlayReport-chain: %d, other: %d)" %
              (len(hits), len(pr_chain), len(other)))
        # Show 'other' (candidate gate readers) first.
        for ins, fn in other[:20]:
            label = ("%s@%s" % (fn.getName(), fn.getEntryPoint())
                     if fn is not None else "(no fn)")
            print("  GATE-CANDIDATE  %s  %s  in %s" %
                  (ins.getAddress(), ins, label))
        if len(other) > 20:
            print("  ... (%d more non-PR loads not shown)" % (len(other) - 20))
        # Brief PR-chain list.
        for ins, fn in pr_chain[:5]:
            label = ("%s@%s" % (fn.getName(), fn.getEntryPoint())
                     if fn is not None else "(no fn)")
            print("  pr-chain        %s  %s  in %s" %
                  (ins.getAddress(), ins, label))
        print("")


def main():
    print("SMBW Archipelago: playreport_field_backtrace_seed.py")
    print("  (Wonder Seed gate-check RE — Phase 3 corroborator)")
    print("")

    patterns_by_field = {}
    for f in SEED_FIELDS:
        if monitor.isCancelled():
            break
        patterns_by_field[f] = phase1(f)

    phase2(patterns_by_field)

    print("Done.")
    print("")
    print("How to read this output:")
    print("  - For each (base_reg, offset) load pattern surfaced in Phase 1,")
    print("    Phase 2 lists every other instruction in the binary using the")
    print("    SAME load.  Entries tagged GATE-CANDIDATE are non-telemetry")
    print("    consumers — at least one of them is the gate-check reader.")
    print("  - Cross-reference with walk_reader_compare_sites.py:")
    print("    a function appearing in BOTH outputs is a top-tier candidate.")
    print("  - Limitation: this only catches direct memory loads with the")
    print("    same base register.  If the gate path computes the offset")
    print("    differently (e.g. via a different live-state pointer), this")
    print("    will miss it.  In that case fall back to the reader+cmp")
    print("    walker (Phase 2 of the plan).")


main()
