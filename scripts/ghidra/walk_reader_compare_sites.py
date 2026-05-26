# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — Wonder Seed gate RE
# (2026-05-26 plan: Phase 2, the workhorse).
#
# For each GameDataMgr READER accessor:
#   FUN_710012AE94 — container-A reader, signature (gmd, u32* out, u32 hash)
#   FUN_71003838AC — sub-bool reader,    signature (sub, u8*  out, u32 hash)
#   FUN_7100124134 — candidate per-bit reader (42 xrefs)
#
# walk every call site, do BOTH:
#   (a) BACKWARD: reconstruct the 32-bit hash constant the caller passed
#       (same logic as walk_hash_writer_xrefs.py — copied + minimally
#       adapted).
#   (b) FORWARD: scan up to FORWARD_INSNS instructions for an immediate
#       compare against any value in the regions.json threshold corpus
#       (typically cmp/subs/cbz/cbnz against the value just read back
#       from the out-pointer).  Score the call site by:
#         + threshold-corpus hit
#         + close proximity (<= 8 insns after the bl)
#         + an intervening `ldr w?, [...]` (the out-pointer dereference)
#         + the comparison preceding a conditional branch (b.cc / b.eq /
#           cbz / cbnz / tbz / tbnz) within 4 insns of the cmp.
#
# Rationale: in SMBW, the in-game "do you have enough Wonder Seeds to
# enter X?" check is almost certainly shaped like:
#
#     mov  w2, #<hash-of-W?-subtotal>
#     add  x1, sp, #<slot>                ; out-pointer
#     bl   FUN_710012AE94                 ; or sibling reader
#     ldr  w8, [sp, #<slot>]              ; load out-pointer'd value
#     cmp  w8, #<threshold>               ; <-- THE GATE COMPARE
#     b.lo Llocked                        ; or cbz/cbnz/tbz/tbnz
#
# If we surface that exact pattern at any call site, the enclosing
# function is a high-confidence gate-check candidate.
#
# The threshold corpus is derived live from
# apworld/smbw_archipelago/data/regions.json (the project's source of
# truth for per-world Wonder Seed gating).  Hardcoded here as of
# 2026-05-26: {2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 14, 15, 16, 25}.
# If new region requirements are added, regenerate from regions.json:
#
#     python -c "import json,re,collections; d=json.load(open(
#       'apworld/smbw_archipelago/data/regions.json'));
#       pat=re.compile(r'\\|([^|:]+):(\\d+)\\|'); s=set();
#       [s.add(int(n)) for b in d.values() for _,n in
#        pat.findall(b.get('requires','') if isinstance(b.get('requires'),str)
#        else '') if 'Wonder Seed' in _]; print(sorted(s))"
#
# Output is one ranked line per call site.  The top entries are the
# candidate gate-check functions; cross-reference with
# find_gate_strings.py for string-adjacency evidence.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

# Reader accessors — same NSO addresses as walk_hash_writer_xrefs.py.
# (signature_hint informs which arg-register typically holds the hash.)
READERS = [
    (0x710012AE94, "x2", "container-A reader (66 xrefs)  — (gmd, u32* out, u32 hash)"),
    (0x71003838AC, "x2", "sub-bool reader (38 xrefs)     — (sub, u8*  out, u32 hash)"),
    (0x7100124134, "x2", "candidate reader B (42 xrefs)  — signature TBD"),
    (0x7100472BE4, "x2", "candidate reader C (41 xrefs)  — signature TBD"),
    (0x71003D3FB0, "x2", "sibling accessor (uses [+0x260] not [+0xe0])"),
]

# Regions.json Wonder-Seed threshold corpus (2026-05-26).
# If any of these immediates appears in a cmp shortly after a reader
# call, that's strong evidence the call site is a gate check.
SEED_THRESHOLDS = (2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 14, 15, 16, 25)

# Broader corpus of "small positive immediates that look like
# thresholds" — used for the medium-confidence bucket.  Excludes 0/1
# (cbz/cbnz/boolean) and very common indices (would yield mostly noise).
BROAD_THRESHOLDS = tuple(range(2, 32))

# How far to walk backward for the hash key.
BACKWALK_INSNS = 20
# How far to walk forward for the compare.
FORWARD_INSNS = 24
# How many insns after the cmp to scan for a conditional branch.
BRANCH_WINDOW = 4

# Mnemonics that produce a comparison against an immediate.
CMP_MNEMONICS = ("cmp", "subs", "cmn", "adds")
# Conditional-branch / test-and-branch mnemonics that consume flags or
# a register-equals-zero condition.
BRANCH_MNEMONICS = (
    "b.eq", "b.ne", "b.cs", "b.hs", "b.cc", "b.lo",
    "b.mi", "b.pl", "b.vs", "b.vc", "b.hi", "b.ls",
    "b.ge", "b.lt", "b.gt", "b.le",
    "cbz", "cbnz", "tbz", "tbnz",
)
# Mnemonics that load a value into a register (the out-pointer load).
LOAD_MNEMONICS = ("ldr", "ldrb", "ldrh", "ldrsb", "ldrsh", "ldrsw", "ldp")

# Known-key cross-reference; lifted from walk_hash_writer_xrefs.py for
# annotation convenience.
KNOWN_KEYS = {
    0xf4ee6827: "flower_coin (PURPLE_COINS, u16)",
    0x17f0bb21: "regular_coin (COINS, u8)",
    0x55815859: "GRAND_SEED_WORLD1",
    0x49abba86: "GRAND_SEED_WORLD2",
    0xb550d8d6: "GRAND_SEED_WORLD3",
    0x1dcf7f6e: "GRAND_SEED_WORLD4",
    0x0d5a3e00: "GRAND_SEED_WORLD5",
    0xd4660d2b: "GRAND_SEED_WORLD6",
    0x5d3ec9b4: "COMPLETE_GAME",
    0x89f1cc52: "INTRO_CUTSCENE_COMPLETED",
    0xdf82e9ab: "SetCourseClearFlagToGameData",
    0x8c20ccb7: "lifetime Wonder Seed counter (recomputed; not gate-trustworthy)",
    0xb9bd745d: "AP-grant queue (NOT per-course storage)",
    0xed817774: "touch_goal_top_result (container-B bool)",
    0xf79bcbb0: "goal_id (container-A counter)",
    0x105df820: "badge_owned bitfield (container-C u64)",
    0x6d1b5c25: "badge UI-slot bitmap (container-C aux)",
}


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


def _lsl_shift_from_insn(ins):
    s = str(ins)
    low = s.lower()
    i = low.find("lsl #")
    if i < 0:
        return 0
    rest = s[i + 5:].strip()
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
    if not reg_name:
        return None
    n = reg_name.lower()
    if n.startswith("w") and n[1:].isdigit():
        return "x" + n[1:]
    return n


def reconstruct_reg_at(call_site, initial_reg="x2"):
    """Backward-walk hash reconstruction.  Same logic as
    walk_hash_writer_xrefs.py — see that script for the full
    documentation of the AArch64 immediate-load idioms handled."""
    listing = currentProgram.getListing()
    target_reg = initial_reg
    pending_value = None
    last_insn_str = None

    cur = listing.getInstructionBefore(call_site)
    walked = 0
    while cur is not None and walked < BACKWALK_INSNS:
        mnem = cur.getMnemonicString().lower()
        dst = _w_to_x(_dest_reg_name(cur))
        if dst == target_reg:
            if mnem == "mov":
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
                    target_reg = src
                    cur = cur.getPrevious()
                    walked += 1
                    continue
                return (None, "mov %s unhandled at %s" % (target_reg, cur.getAddress()))
            elif mnem == "movz":
                imm = _imm_at_op(cur, 1)
                if imm is None:
                    return (None, "movz no imm at %s" % cur.getAddress())
                shift = _lsl_shift_from_insn(cur)
                val = (imm << shift) & 0xFFFFFFFF
                if pending_value is not None:
                    val |= pending_value
                return (val, "%s @ %s" % (cur, cur.getAddress()))
            elif mnem == "movk":
                imm = _imm_at_op(cur, 1)
                if imm is None:
                    return (None, "movk no imm at %s" % cur.getAddress())
                shift = _lsl_shift_from_insn(cur)
                shifted = (imm << shift) & 0xFFFFFFFF
                pending_value = (pending_value or 0) | shifted
                cur = cur.getPrevious()
                walked += 1
                continue
            elif mnem == "movn":
                imm = _imm_at_op(cur, 1)
                if imm is None:
                    return (None, "movn no imm at %s" % cur.getAddress())
                shift = _lsl_shift_from_insn(cur)
                val = (~(imm << shift)) & 0xFFFFFFFF
                return (val, "%s @ %s" % (cur, cur.getAddress()))
            elif mnem in ("adrp", "ldr", "ldrb", "ldrh"):
                return (None, "set via %s at %s" % (mnem, cur.getAddress()))
            elif mnem in ("add", "sub", "or", "and", "eor"):
                return (None, "set via %s at %s" % (mnem, cur.getAddress()))
            elif mnem == "bl":
                return (None, "set by bl at %s" % cur.getAddress())
            else:
                return (None, "set via %s at %s (unhandled)" % (mnem, cur.getAddress()))
        cur = cur.getPrevious()
        walked += 1
    return (None, "no setter within %d insns" % BACKWALK_INSNS)


def _harvest_hash(call_site, primary_reg):
    """Try the canonical hash register first; fall back to alternatives.
    Returns (val, reg_used) or (None, None)."""
    for reg in (primary_reg, "x2", "x0", "x1", "x3"):
        val, _ = reconstruct_reg_at(call_site, initial_reg=reg)
        if val is not None and val > 0xFFFF:
            return (val, reg)
    # Last-ditch: accept a small constant from any reg.
    for reg in (primary_reg, "x2", "x0", "x1", "x3"):
        val, _ = reconstruct_reg_at(call_site, initial_reg=reg)
        if val is not None:
            return (val, reg)
    return (None, None)


def _forward_compare_scan(call_site):
    """Walk forward from `call_site` (the bl instruction).  Returns a
    list of dicts describing each cmp-like instruction found within
    FORWARD_INSNS, annotated with its immediate value, whether it's
    in the seed-threshold or broad threshold corpus, whether a
    conditional branch follows within BRANCH_WINDOW insns, and whether
    an intervening load was seen.

    Each hit:
      {
        "cmp_addr":   <Address>,
        "cmp_text":   <str>,
        "cmp_imm":    <int>,
        "in_seed":    <bool>,
        "in_broad":   <bool>,
        "had_load":   <bool>,
        "branch":     <branch insn str or None>,
        "distance":   <int>,
      }
    """
    listing = currentProgram.getListing()
    cur = listing.getInstructionAfter(call_site)
    walked = 0
    had_load = False
    hits = []
    while cur is not None and walked < FORWARD_INSNS:
        mnem = cur.getMnemonicString().lower()

        if mnem in LOAD_MNEMONICS:
            had_load = True

        # Bail if we hit another bl (probably out of the relevant block).
        if mnem == "bl" and walked > 0:
            break
        # Bail at unconditional branches (different basic block).
        if mnem in ("b", "ret", "br"):
            break

        if mnem in CMP_MNEMONICS:
            imm = _imm_at_op(cur, 1)
            if imm is None:
                # Some cmp variants have the immediate at operand 2
                # (e.g. cmp w0, w1, lsl #N), but those compare regs.
                imm = _imm_at_op(cur, 2)
            if imm is not None:
                # Look ahead for a conditional branch consuming the flags.
                branch_str = None
                look = listing.getInstructionAfter(cur.getAddress())
                ahead = 0
                while look is not None and ahead < BRANCH_WINDOW:
                    bmnem = look.getMnemonicString().lower()
                    if bmnem in BRANCH_MNEMONICS:
                        branch_str = "%s %s" % (bmnem, look)
                        break
                    if bmnem in CMP_MNEMONICS:
                        break  # flags clobbered; no branch consumed THIS cmp
                    look = listing.getInstructionAfter(look.getAddress())
                    ahead += 1
                hits.append({
                    "cmp_addr": cur.getAddress(),
                    "cmp_text": str(cur),
                    "cmp_imm": imm & 0xFFFFFFFFFFFFFFFF,
                    "in_seed": (imm & 0xFFFFFFFF) in SEED_THRESHOLDS,
                    "in_broad": (imm & 0xFFFFFFFF) in BROAD_THRESHOLDS,
                    "had_load": had_load,
                    "branch": branch_str,
                    "distance": walked + 1,
                })

        # Also check cbz/cbnz/tbz/tbnz — they have a register operand
        # (and tbz/tbnz a bit index immediate).  Treat tbz/tbnz against
        # a small bit index as a probable per-bit gate.
        if mnem in ("cbz", "cbnz"):
            hits.append({
                "cmp_addr": cur.getAddress(),
                "cmp_text": str(cur),
                "cmp_imm": 0,
                "in_seed": False,
                "in_broad": False,
                "had_load": had_load,
                "branch": "%s (self)" % mnem,
                "distance": walked + 1,
                "is_cbz": True,
            })
        if mnem in ("tbz", "tbnz"):
            imm = _imm_at_op(cur, 1)
            hits.append({
                "cmp_addr": cur.getAddress(),
                "cmp_text": str(cur),
                "cmp_imm": imm if imm is not None else -1,
                "in_seed": False,
                "in_broad": False,
                "had_load": had_load,
                "branch": "%s (self)" % mnem,
                "distance": walked + 1,
                "is_tbz": True,
            })

        cur = listing.getInstructionAfter(cur.getAddress())
        walked += 1
    return hits


def _score_call_site(hash_val, forward_hits):
    """Score a call site by how 'gate-shaped' the post-call pattern is.

    Scoring rubric (higher = stronger gate candidate):
      +50 : a cmp immediate matches the seed-threshold corpus
      +20 : a cmp immediate matches the broad small-int corpus
            (but not the seed corpus)
      +15 : an `ldr` was seen between the bl and the cmp (out-ptr deref)
      +10 : a conditional branch follows the cmp within BRANCH_WINDOW
      + 5 : cmp distance from bl <= 8 insns (tight pattern)
      +10 : harvested hash is one of the seed-related KNOWN_KEYS that
            we expect to be a per-world subtotal (currently empty — see
            below for the hypothesis)
      - 5 : harvested hash is 0x8c20ccb7 (the lifetime counter — known
            stale; not a gate input candidate)
      - 5 : harvested hash is 0xb9bd745d (AP-grant queue — known
            irrelevant for gating)

    Note: we deliberately do NOT pre-tag any hash as 'seed-related'
    beyond the lifetime/queue exclusions, because the gate's per-world
    subtotal hashes are unknown going in.  The script's job is to
    SURFACE candidates so a human can name them.
    """
    if not forward_hits:
        return 0, []
    best = 0
    reasons = []
    for h in forward_hits:
        score = 0
        if h.get("in_seed"):
            score += 50
            reasons.append("imm=%d in seed-corpus" % h["cmp_imm"])
        elif h.get("in_broad") and not h.get("is_cbz") and not h.get("is_tbz"):
            score += 20
            reasons.append("imm=%d in broad-corpus" % h["cmp_imm"])
        if h.get("had_load"):
            score += 15
            reasons.append("ldr seen pre-cmp")
        if h.get("branch"):
            score += 10
            reasons.append("branch=%s" % h["branch"])
        if h.get("distance", 99) <= 8:
            score += 5
            reasons.append("near (d=%d)" % h["distance"])
        if score > best:
            best = score
    if hash_val == 0x8c20ccb7:
        best -= 5
        reasons.append("hash is lifetime counter (stale)")
    if hash_val == 0xb9bd745d:
        best -= 5
        reasons.append("hash is AP-grant queue")
    return best, reasons


def _walk_one_reader(reader_addr_int, primary_reg, role_hint):
    main_base = currentProgram.getImageBase().getOffset()
    addr_int = reader_addr_int
    if addr_int < main_base:
        addr_int = main_base + reader_addr_int

    fn_mgr = currentProgram.getFunctionManager()
    reader = fn_mgr.getFunctionAt(_addr(addr_int))
    if reader is None:
        reader = fn_mgr.getFunctionContaining(_addr(addr_int))
    if reader is None:
        return None, []

    ref_mgr = currentProgram.getReferenceManager()
    refs = list(ref_mgr.getReferencesTo(reader.getEntryPoint()))
    call_refs = [r for r in refs if r.getReferenceType().isCall()]

    harvest = []
    for r in call_refs:
        if monitor.isCancelled():
            break
        src = r.getFromAddress()
        fn = fn_mgr.getFunctionContaining(src)
        hash_val, hash_reg = _harvest_hash(src, primary_reg)
        forward_hits = _forward_compare_scan(src)
        score, reasons = _score_call_site(hash_val, forward_hits)
        harvest.append({
            "call_site": src,
            "fn": fn,
            "hash": hash_val,
            "hash_reg": hash_reg,
            "forward_hits": forward_hits,
            "score": score,
            "reasons": reasons,
        })

    return reader, harvest


def _fmt_fn(fn):
    if fn is None:
        return "(no enclosing fn)"
    return "%s@%s" % (fn.getName(), fn.getEntryPoint())


def main():
    print("SMBW Archipelago: walk_reader_compare_sites.py")
    print("  (Wonder Seed gate-check RE — Phase 2 candidate ranking)")
    main_base = currentProgram.getImageBase().getOffset()
    print("  main_base: 0x%x" % main_base)
    print("  seed thresholds: %s" % (SEED_THRESHOLDS,))
    print("  scanning %d reader functions" % len(READERS))
    print("")

    # Per-reader aggregation.
    all_rows = []  # flat list across all readers, for global ranking.
    for reader_addr_int, primary_reg, role in READERS:
        if monitor.isCancelled():
            break
        reader, harvest = _walk_one_reader(reader_addr_int, primary_reg, role)
        if reader is None:
            print("[skip] 0x%x  (no function defined)" % reader_addr_int)
            continue

        print("===== reader 0x%x  (%s) =====" % (reader_addr_int, role))
        print("  %s @ %s" % (reader.getName(), reader.getEntryPoint()))
        print("  total xrefs: %d" % len(harvest))

        # Per-reader summary by score buckets.
        n_seed = sum(1 for h in harvest if h["score"] >= 50)
        n_broad = sum(1 for h in harvest
                      if 20 <= h["score"] < 50)
        n_other = sum(1 for h in harvest if 0 < h["score"] < 20)
        n_zero = sum(1 for h in harvest if h["score"] <= 0)
        print("  buckets: seed-threshold=%d  broad=%d  weak=%d  zero=%d"
              % (n_seed, n_broad, n_other, n_zero))

        # Show top 10 of this reader.
        harvest_sorted = sorted(harvest, key=lambda h: -h["score"])
        for row in harvest_sorted[:10]:
            hash_str = ("0x%08x" % row["hash"]) if row["hash"] is not None else "?"
            known = ""
            if row["hash"] is not None and row["hash"] in KNOWN_KEYS:
                known = "  <- %s" % KNOWN_KEYS[row["hash"]]
            print("  [score=%3d]  call %s  hash=%s (via %s)  in %s%s" %
                  (row["score"], row["call_site"], hash_str,
                   row["hash_reg"] or "?", _fmt_fn(row["fn"]), known))
            # Show the strongest forward hit inline.
            best_h = None
            for h in row["forward_hits"]:
                if best_h is None:
                    best_h = h
                elif (h.get("in_seed") and not best_h.get("in_seed")):
                    best_h = h
                elif (h.get("in_seed") == best_h.get("in_seed")
                      and h.get("had_load") and not best_h.get("had_load")):
                    best_h = h
            if best_h is not None:
                tag = "SEED" if best_h.get("in_seed") else (
                      "broad" if best_h.get("in_broad") else "other")
                print("              -> %s @ %s  [%s, d=%d, load=%s, br=%s]" %
                      (best_h["cmp_text"], best_h["cmp_addr"], tag,
                       best_h["distance"], best_h.get("had_load"),
                       best_h.get("branch")))
            if row["reasons"]:
                print("              reasons: %s" % "; ".join(row["reasons"]))

        all_rows.extend(harvest_sorted)
        print("")

    # Global ranking across all readers.
    print("===== GLOBAL TOP 30 (across all readers) =====")
    print("  These are the highest-scoring gate-check candidates.")
    print("  Manual review: decompile each fn and check whether the")
    print("  function shape matches 'returns whether gate is open'.")
    print("")
    all_rows.sort(key=lambda h: -h["score"])
    seen_fns = set()
    rank = 0
    for row in all_rows:
        if row["score"] <= 0:
            break
        fn_key = row["fn"].getEntryPoint().getOffset() if row["fn"] else 0
        if fn_key in seen_fns:
            continue  # dedupe by enclosing function — one row per fn
        seen_fns.add(fn_key)
        rank += 1
        if rank > 30:
            break
        hash_str = ("0x%08x" % row["hash"]) if row["hash"] is not None else "?"
        known = ""
        if row["hash"] is not None and row["hash"] in KNOWN_KEYS:
            known = "  [known: %s]" % KNOWN_KEYS[row["hash"]]
        print("  #%-2d  score=%-3d  fn=%s  call=%s  hash=%s%s" %
              (rank, row["score"], _fmt_fn(row["fn"]),
               row["call_site"], hash_str, known))
        best_h = None
        for h in row["forward_hits"]:
            if best_h is None:
                best_h = h
            elif h.get("in_seed") and not best_h.get("in_seed"):
                best_h = h
        if best_h is not None:
            tag = "SEED" if best_h.get("in_seed") else (
                  "broad" if best_h.get("in_broad") else "other")
            print("        -> %s @ %s  [%s, d=%d, load=%s]" %
                  (best_h["cmp_text"], best_h["cmp_addr"], tag,
                   best_h["distance"], best_h.get("had_load")))

    print("")
    print("Done.")
    print("")
    print("How to read this output:")
    print("  - GLOBAL TOP 30 ranks unique enclosing functions by gate-shape score.")
    print("  - Rows with [score>=50] have an immediate-cmp matching a regions.json")
    print("    Wonder Seed threshold within %d insns of the reader call." %
          FORWARD_INSNS)
    print("  - Rows with [score 20..49] have a small-int cmp but not in the seed")
    print("    corpus — still worth inspecting if the hash is unknown.")
    print("  - For each top candidate:")
    print("      1. Decompile the enclosing function in Ghidra.")
    print("      2. Check whether it returns a boolean (`return w0` with 0/1).")
    print("      3. Check direct callers — are any in Nerve vtables")
    print("         (0x710334XXXX / 0x71033fXXXX / 0x71034BXXXX)?")
    print("      4. Check prologue safety: paste first 5 insns; flag any")
    print("         adrp/ldr-literal/b/bl per CLAUDE.md gotcha #2.")
    print("      5. Cross-reference with find_gate_strings.py: is a")
    print("         gate-UI string near this fn?")
    print("  - Hashes are listed for downstream naming.  Seed-related per-world")
    print("    subtotal hashes are unknown going in; finding consistent hashes")
    print("    across multiple thresholds (e.g. one hash always compared to")
    print("    {3, 10, 14}) suggests the W1 subtotal key.")


main()
