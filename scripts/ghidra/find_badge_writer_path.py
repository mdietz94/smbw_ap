# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — sprint 2 (badges).
#
# M3.2 badge-grant RE — combined-strategy script. The badge bitfield
# lives in the save's TRAILING region (file offset 0x0EA0, u64), which
# is NOT hash-keyed under GameDataMgr's container A or B (those serialize
# the pair region 0x028..0xBF0).  So the M3.3 grant primitive
# `FUN_710049F648(gmd, value, hash)` does NOT cover badges.  We need
# to find the live-state writer for the badge bitfield directly.
#
# Sprint 1 (string-grep) found a test harness FUN_7101b1fb6c with three
# candidate functions but no real grant API.  This script reframes the
# search using sprint-2-style dataflow techniques, combining four
# independent angles to triangulate the badge state struct:
#
#   Phase A — file-offset displacement scan
#     Find every `ldr/str ..., [Xn, #0x0EA0]` and every `mov ..., #0x0EA0`
#     in .text.  The enclosing function is either (1) the save serializer
#     for the trailing region, (2) the save deserializer, or (3) a
#     direct accessor of the badge bitfield.  Sprint-2's writeup
#     identifies (1)+(2) as a "chokepoint hook" — once we have one,
#     we can read its source register chain to find the LIVE buffer
#     address from the save-OUT buffer copy.
#
#   Phase B — PlayReport equip_badge_id backward walk
#     The game emits `equip_badge_id=[N]` in course_in PlayReports.
#     That means somewhere it does `ldr w_, [x_, #M]; ...; bl Add(...)`
#     where the load source is the live equipped-badge field.  The
#     OWNED bitfield is almost certainly a sibling field a few offsets
#     away in the same struct.  This phase locates the load site.
#
#   Phase C — PlayReport badge_id_array iteration scan
#     `badge_id_array` is an *array* in course_in / course_result
#     PlayReports — a list of owned badge IDs.  The builder code must
#     iterate the live bitfield, count set bits, emit one entry per
#     bit.  Look for `clz` / `ctz` / `rbit` / `cnt` / `ubfx`-style
#     instructions near a PlayReport::Add call site — those are
#     bit-iteration patterns.  The base register being iterated is the
#     badge bitfield's live address.
#
#   Phase D — cheat-DB badge-EFFECT anchor analysis
#     The HamletDuFromage cheat at NSO +0x00306AEC `MOV W20,#1` forces
#     the Magnet Coin badge effect active.  The un-cheated code at that
#     site loads W20 from somewhere — almost certainly the "is this
#     badge currently equipped + owned + active" check that reads from
#     the owned bitfield.  Dump that load instruction.
#
# After this script runs:
#   - Cross-reference the function names appearing in MULTIPLE phases:
#     a function in Phase A + Phase B is almost certainly the save
#     serializer.  A function in Phase B + Phase D is the badge-effect
#     gating code.  The grant-writer is the function that produces the
#     value read by Phase D (one xref back along the data flow).
#   - Functions with HIGH xref counts AND a 0x0EA0 reference are
#     likely the centralized BadgeMgr / BadgeData methods.
#
# This script does NOT modify the program.  It's read-only, output to
# the Ghidra Console.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function


# ===== Phase A: 0x0EA0 displacement / immediate scan =====
BADGE_BITFIELD_FILE_OFFSET = 0x0EA0

# Disp loads that could carry the 0x0EA0 immediate.
DISP_MNEMONICS = (
    "ldr", "ldrb", "ldrh", "ldrsb", "ldrsh", "ldrsw",
    "str", "strb", "strh",
    "ldp", "stp",
)
IMM_LOAD_MNEMONICS = ("mov", "movz", "movk", "movn")


# ===== Phase B/C: PlayReport field-name strings (M2.4 corpus) =====
BADGE_PR_FIELDS = [
    "equip_badge_id",   # course_in: array of currently-equipped badge IDs
    "badge_id_array",   # course_result: array of owned badge IDs at clear
]


# ===== Phase D: cheat-DB badge-effect anchors =====
# These are NSO addresses of HamletDuFromage cheat patches that force
# specific badge effects active.  Their un-cheated form READS from the
# live badge-state — the read instruction tells us where badge data
# is in memory.
CHEAT_DB_BADGE_EFFECT_ANCHORS = [
    (0x00306AEC, "Magnet Coin badge effect (MOV W20,#1)"),
    (0x00306B90, "Magnet Coin badge effect (MOV W21,#1)"),
    (0x01751DB8, "Wall-Jump badge (MOV W1,#7)"),
    (0x0033186C, "Squat-Jump High badge"),
]


# ===== Bit-iteration mnemonics for Phase C =====
# When code iterates an N-bit bitfield to enumerate set bits, it typically
# uses one of these patterns:
#   clz / clz x_, x_     — count-leading-zeros for "find next set bit"
#   rbit                  — reverse-bits (paired with clz for ctz)
#   ubfx / sbfx           — extract a single bit (loop over indices)
#   cnt                   — popcount (less common; usually in SIMD)
BIT_ITER_MNEMONICS = ("clz", "rbit", "ubfx", "sbfx", "cnt", "tbz", "tbnz")


# ===== Walk limits to avoid console flood =====
MAX_PHASE_A_HITS    = 64
MAX_PR_XREFS        = 16
BACKWALK_INSNS      = 12
FORWALK_INSNS       = 12
WINDOW_INSNS_BEFORE = 6
WINDOW_INSNS_AFTER  = 12


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


def _fn_label(addr_or_ins):
    """Return 'fn_name @ entry+offset' for the function containing an
    address (or instruction).  Accepts either a Ghidra Address or
    Instruction.  (Note: both classes have a getAddress() method but
    Address.getAddress(int) takes an index arg — use getMnemonicString
    to discriminate.)"""
    fn_mgr = currentProgram.getFunctionManager()
    if hasattr(addr_or_ins, "getMnemonicString"):
        # It's an Instruction.
        a = addr_or_ins.getAddress()
    else:
        # It's an Address (or compatible).
        a = addr_or_ins
    fn = fn_mgr.getFunctionContaining(a)
    if fn is None:
        return "(not in a function)"
    delta = a.getOffset() - fn.getEntryPoint().getOffset()
    return "%s @ %s (+0x%x)" % (
        fn.getName(), fn.getEntryPoint(), delta)


def _instructions_iter():
    """Yield all instructions in the .text section, forward."""
    return currentProgram.getListing().getInstructions(True)


# ============================================================
# Phase A — 0x0EA0 displacement / immediate scan
# ============================================================

def phase_a_offset_scan():
    print("")
    print("=" * 68)
    print("PHASE A — scan for displacement uses of 0x%x (badge bitfield)" %
          BADGE_BITFIELD_FILE_OFFSET)
    print("=" * 68)

    disp_hits = []
    imm_hits = []
    seen_fns = {}  # fn_name -> hit count

    for ins in _instructions_iter():
        if monitor.isCancelled():
            break
        mnem = ins.getMnemonicString().lower()
        nops = ins.getNumOperands()
        # Displacement form: e.g. ldr w_, [x_, #0xEA0]
        if mnem in DISP_MNEMONICS:
            for op_idx in (nops - 1, nops - 2):
                if op_idx < 0:
                    continue
                imm = _imm_at_op(ins, op_idx)
                if imm == BADGE_BITFIELD_FILE_OFFSET:
                    disp_hits.append(ins)
                    break
        # Immediate form: mov/movz x_, #0xEA0 (built into a register
        # for later add-base)
        elif mnem in IMM_LOAD_MNEMONICS:
            imm = _imm_at_op(ins, 1)
            if imm == BADGE_BITFIELD_FILE_OFFSET:
                imm_hits.append(ins)

        if len(disp_hits) + len(imm_hits) >= MAX_PHASE_A_HITS:
            break

    print("  ldr/str disp matches:  %d" % len(disp_hits))
    for ins in disp_hits:
        label = _fn_label(ins)
        print("    %s   %s   in %s" % (ins.getAddress(), ins, label))
        # Track per-fn count for the "high-traffic chokepoint" summary
        fn_mgr = currentProgram.getFunctionManager()
        fn = fn_mgr.getFunctionContaining(ins.getAddress())
        if fn is not None:
            seen_fns[fn.getName()] = seen_fns.get(fn.getName(), 0) + 1

    print("")
    print("  mov/movz imm matches:  %d" % len(imm_hits))
    for ins in imm_hits:
        print("    %s   %s   in %s" % (
            ins.getAddress(), ins, _fn_label(ins)))
        fn_mgr = currentProgram.getFunctionManager()
        fn = fn_mgr.getFunctionContaining(ins.getAddress())
        if fn is not None:
            seen_fns[fn.getName()] = seen_fns.get(fn.getName(), 0) + 1

    if seen_fns:
        print("")
        print("  Functions with 0x%x hits (sorted by count desc):" %
              BADGE_BITFIELD_FILE_OFFSET)
        for fn_name, n in sorted(
                seen_fns.items(), key=lambda kv: (-kv[1], kv[0])):
            print("    %2d  %s" % (n, fn_name))
        print("")
        print("  >>> The HIGH-count function is the save serializer or")
        print("      deserializer (it touches many trailing-region offsets).")
        print("      The LOW-count (1-2) functions are direct accessors of")
        print("      the badge bitfield — those are grant-writer candidates.")

    return seen_fns


# ============================================================
# Phase B+C — PlayReport field-name backwalk
# ============================================================

def _find_strings_with_content(content):
    """Linear scan for null-terminated `content` in initialized non-
    executable memory blocks (i.e. .rodata).  Returns a list of
    addresses (as Python ints)."""
    memory = currentProgram.getMemory()
    target = bytearray(content.encode("ascii") + b"\x00")
    n = len(target)
    found = []
    for block in memory.getBlocks():
        if not block.isInitialized() or not block.isRead():
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
                    bs.append(memory.getByte(
                        _addr(start + chunk_start + i)) & 0xff)
            except Exception:
                continue
            idx = 0
            while True:
                pos = bs.find(target, idx)
                if pos == -1:
                    break
                # Must be preceded by \0 or block start.
                if pos == 0 or bs[pos - 1] == 0:
                    found.append(start + chunk_start + pos)
                idx = pos + 1
                if len(found) > 32:
                    return found
    return found


def _dump_window(site, before=WINDOW_INSNS_BEFORE,
                 after=WINDOW_INSNS_AFTER, marker="  ==> "):
    """Print disasm context around `site` (an Address)."""
    listing = currentProgram.getListing()
    pre = []
    cur = listing.getInstructionBefore(site)
    while cur is not None and len(pre) < before:
        pre.append(cur)
        cur = cur.getPrevious()
    pre.reverse()
    at = listing.getInstructionAt(site) or listing.getInstructionContaining(site)
    post = []
    cur = at.getNext() if at is not None else None
    while cur is not None and len(post) < after:
        post.append(cur)
        cur = cur.getNext()
    for ins in pre:
        print("       %s" % ins)
    if at is not None:
        print("%s%s" % (marker, at))
    else:
        print("%s(no instruction at site)" % marker)
    for ins in post:
        # Highlight bit-iteration patterns inline (Phase C signal).
        mnem = ins.getMnemonicString().lower()
        bit_note = "  <-- BIT-ITER" if mnem in BIT_ITER_MNEMONICS else ""
        print("       %s%s" % (ins, bit_note))


def phase_bc_playreport_backwalk():
    print("")
    print("=" * 68)
    print("PHASE B/C — PlayReport equip_badge_id + badge_id_array backwalk")
    print("=" * 68)

    ref_mgr = currentProgram.getReferenceManager()

    for field in BADGE_PR_FIELDS:
        print("")
        print("===== field: '%s' =====" % field)
        str_addrs = _find_strings_with_content(field)
        if not str_addrs:
            print("  (string not found — confirm M2.4 PlayReport corpus")
            print("   matches this binary version)")
            continue
        print("  string at %d address(es): %s" %
              (len(str_addrs), [hex(a) for a in str_addrs[:4]]))

        for sa_int in str_addrs:
            sa = _addr(sa_int)
            refs = list(ref_mgr.getReferencesTo(sa))
            shown = 0
            for r in refs:
                if shown >= MAX_PR_XREFS:
                    break
                shown += 1
                src = r.getFromAddress()
                print("")
                print("  --- xref at %s in %s ---" %
                      (src, _fn_label(src)))
                _dump_window(src)


# ============================================================
# Phase D — cheat-DB badge-effect anchors
# ============================================================

def phase_d_cheat_anchors():
    print("")
    print("=" * 68)
    print("PHASE D — cheat-DB badge-effect anchors (un-cheated load sources)")
    print("=" * 68)

    main_base = currentProgram.getImageBase().getOffset()

    for nso_off, label in CHEAT_DB_BADGE_EFFECT_ANCHORS:
        print("")
        print("===== %s @ NSO +0x%x =====" % (label, nso_off))
        site = _addr(main_base + nso_off)
        listing = currentProgram.getListing()
        cheat_ins = listing.getInstructionAt(site)
        if cheat_ins is None:
            print("  (no instruction at %s — confirm offset matches v1.0.0)" %
                  site)
            continue
        print("  cheat replaces:       %s" % cheat_ins)
        print("  containing function:  %s" % _fn_label(site))
        print("")
        print("  context (-%d / +%d insns):" %
              (WINDOW_INSNS_BEFORE, WINDOW_INSNS_AFTER))
        _dump_window(site)

        # The un-cheated instruction sets W_/X_ from somewhere.  If
        # the destination register comes from a LDR earlier in the
        # function, that LDR is the badge-state read.  Backward-walk:
        cheat_mnem = cheat_ins.getMnemonicString().lower()
        if cheat_mnem in ("mov", "movz", "movk"):
            try:
                dest_objs = cheat_ins.getOpObjects(0)
                dest_reg = None
                for o in dest_objs:
                    if o is not None and o.__class__.__name__ == "Register":
                        dest_reg = o.getName().lower()
                        break
                if dest_reg:
                    print("")
                    print("  Backward dataflow for destination %s:" % dest_reg)
                    cur = cheat_ins.getPrevious()
                    walked = 0
                    while cur is not None and walked < BACKWALK_INSNS:
                        nmnem = cur.getMnemonicString().lower()
                        if nmnem in DISP_MNEMONICS:
                            # Check if dest matches
                            try:
                                cobjs = cur.getOpObjects(0)
                                for o in cobjs:
                                    if (o is not None
                                            and o.__class__.__name__ == "Register"):
                                        rn = o.getName().lower()
                                        # Normalize w/x prefix comparison
                                        if rn[1:] == dest_reg[1:]:
                                            print("    pre-cheat load:  %s   (%s)" %
                                                  (cur, _fn_label(cur)))
                                            break
                            except Exception:
                                pass
                        cur = cur.getPrevious()
                        walked += 1
            except Exception as e:
                print("  (backward-walk failed: %s)" % e)


# ============================================================
# main
# ============================================================

def main():
    print("SMBW Archipelago: find_badge_writer_path.py")
    print("  M3.2 badge grant — combined-strategy discovery")
    print("")

    phase_a_fns = phase_a_offset_scan()
    phase_bc_playreport_backwalk()
    phase_d_cheat_anchors()

    print("")
    print("=" * 68)
    print("CROSS-REFERENCE INSTRUCTIONS")
    print("=" * 68)
    print("")
    print("1. The HIGHEST-COUNT function in Phase A is the save serializer.")
    print("   Open it in the decompiler and look at the load source for the")
    print("   instruction with displacement 0x%x — its base register is" %
          BADGE_BITFIELD_FILE_OFFSET)
    print("   (live_state_pointer + struct_offset).  Trace backwards to find")
    print("   where that base register is loaded from.  It will be either:")
    print("     (a) sInstance->[+M] — confirms badges are in a GameDataMgr")
    print("         sub-object at offset M.  Use this offset directly.")
    print("     (b) [func_arg] — confirms badges live in a struct passed to")
    print("         this function.  Trace the caller chain.")
    print("")
    print("2. Phase B's equip_badge_id load reveals the per-player badge")
    print("   state struct.  Compare the base register and offset against")
    print("   Phase A's badge-bitfield load.  Likely same base register,")
    print("   different offset (equipped_id ≠ owned_bitfield).")
    print("")
    print("3. Phase C's bit-iteration (clz/rbit/ubfx) near a PlayReport::Add")
    print("   call for badge_id_array confirms the bitfield encoding.")
    print("   The load source is the badge ownership u64.")
    print("")
    print("4. Phase D's pre-cheat load reveals where the BADGE EFFECT engine")
    print("   reads from.  This is downstream of ownership (effect = owned")
    print("   AND equipped AND active) — but the read source can be traced")
    print("   back to the ownership bitfield via the gating function.")
    print("")
    print("5. The intersection of Phase A's accessor functions with Phase B's")
    print("   PlayReport-building functions tells us whether badge state is")
    print("   in the same struct as other PlayReport-emitted fields.  If")
    print("   YES: badge state lives in the per-course session struct, and")
    print("   the grant writer is per-session.  If NO: it's in a persistent")
    print("   struct on GameDataMgr.")
    print("")
    print("Done.")


main()
