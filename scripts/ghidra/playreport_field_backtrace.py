# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project — sprint 2, Phase 5.2.
#
# Walks every known PlayReport field name (from M2.4 corpus) and
# back-traces the value that gets passed to PlayReport::Add at each
# call site.  The value is loaded from `[x_base, #offset]` — meaning
# `x_base` at that moment holds (a pointer into) the live game-state
# struct.  Recording each field's `(load_offset, base_reg)` reveals
# the live struct's layout for that subsystem.
#
# Example output line:
#   field='world_wonder_flower'  callsite=0x710xxxxxxx  load=ldr w8,[x22,#0xC8]
#       in fn FUN_710xxxxxxx (likely 'collect Wonder Phase report builder')
#
# Cross-reference with:
#   - The cheat DB's `STR W10,[X22,#0xC8]` at NSO +0x49253C (flower_coin
#     writer).  Identical (x22, #0xC8) implies the field name is
#     `world_wonder_flower` and x22 is the same live-state base.
#   - The save-file offsets in docs/save-diff-findings.md.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

# PlayReport field names harvested from M2.4 corpus
# (apworld/smbw_archipelago/client/tests/test_play_report.py).
# Each is a key-string the game passes to PlayReport::Add.  Each appears
# in .rodata as a null-terminated ASCII string and has 1+ xref from
# the .text where the Add() call is made.
PR_FIELDS = [
    # course_in / course_result fields:
    "stage_key", "world_no", "course_no",
    "lucky_coin", "world_wonder_flower",
    "equip_badge_id",
    "local_player_rest",
    "goal_id", "course_result",
    "touch_goal_top_enter", "touch_goal_top_result",
    "world_mother_seed",
    "total_get_finish_seed_count",
    "badge_id_array",
    # world_activity / world_result fields:
    "stage_info", "wonder_seed", "wonder_coin",
    "next_stage_info", "stage_type", "world_kind",
    "transition_type",
    # koopajr_result fields:
    "battle_result", "koopajr_total_time",
    "koopajr_final_stage", "koopajr_challenge_count",
    "koopajr_step_info",
    # global / boot:
    "savedata_id", "play_mode", "scene_type",
    "total_play_time_sec",
    "flower_coin_course_out",
    "arena_score_enter", "last_put_panel_id",
]

# How far back to walk from the PlayReport::Add call to find the
# instruction that loaded the value into the value-register (typically
# w1, w2, or x1/x2 depending on the Add overload signature).
BACKWALK_INSNS = 12

# Max occurrences of each field-name string to investigate (some keys
# may be referenced from many call sites — e.g. stage_key is used in
# multiple PlayReport types).
MAX_XREFS_PER_FIELD = 8


def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def _find_strings_with_content(content):
    """Linear .rodata scan for a null-terminated string equal to `content`.
    Returns list of addresses.  Ghidra's defined-strings table is more
    efficient when available, but a manual scan works on partially-
    analyzed binaries."""
    memory = currentProgram.getMemory()
    target_bytes = bytearray(content.encode("ascii") + b"\x00")
    n = len(target_bytes)
    found = []

    # Iterate over loaded memory blocks containing initialized data.
    for block in memory.getBlocks():
        if not block.isInitialized():
            continue
        if not block.isRead():
            continue
        # Skip the code section if we can identify it; Nintendo NSOs
        # split into .text / .rodata / .data — checking 'isExecute'
        # is a coarse filter.
        if block.isExecute():
            continue
        start = block.getStart().getOffset()
        size = block.getSize()
        # Read in chunks to avoid pulling whole megabytes at once.
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
            # Find every occurrence within this chunk.
            idx = 0
            while True:
                pos = bs.find(target_bytes, idx)
                if pos == -1:
                    break
                # Must be preceded by 0 or block start to count as a true
                # null-terminated-string match (defensive — we don't want
                # to match a substring of a larger word).
                if pos == 0 or bs[pos - 1] == 0:
                    found.append(start + chunk_start + pos)
                idx = pos + 1
                if len(found) > 32:
                    return found
    return found


def _trace_value_load(call_site, value_reg):
    """From `call_site` walk backward up to BACKWALK_INSNS instructions
    looking for an instruction that writes to `value_reg`.  Returns the
    instruction (or None) and a brief description of the load source."""
    listing = currentProgram.getListing()
    cur = listing.getInstructionBefore(call_site)
    walked = 0
    while cur is not None and walked < BACKWALK_INSNS:
        mnem = cur.getMnemonicString().lower()
        # Check whether this instruction writes to value_reg.
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
                        # Found a writer of the value register.
                        return (cur, "%s %s" % (mnem, cur))
        cur = cur.getPrevious()
        walked += 1
    return (None, "no setter within %d insns" % BACKWALK_INSNS)


def _analyze_xref(field_name, xref):
    """Walk forward from the xref site looking for the PlayReport::Add
    call (`bl <symbol>`).  Then back-trace the value-register load."""
    listing = currentProgram.getListing()
    site = xref.getFromAddress()
    fn_mgr = currentProgram.getFunctionManager()
    fn = fn_mgr.getFunctionContaining(site)
    fn_label = ("%s @ %s" % (fn.getName(), fn.getEntryPoint())
                if fn is not None else "(no enclosing fn)")

    # The xref is typically an `adrp` or `add` instruction that loads
    # the string address into a register (likely x1 or x2 depending on
    # the Add overload).  Walk FORWARD looking for the `bl` to Add.
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
                        # Heuristic: the target function name contains "Add"
                        # (PlayReport::Add) or its size/signature matches.
                        name = fn_at_tgt.getName()
                        if "Add" in name or "PlayReport" in name:
                            found_call = (cur, fn_at_tgt)
                            break
                    # Even without name resolution, the closest forward bl
                    # after a string-pointer load is usually the Add call.
                    if found_call is None:
                        found_call = (cur, fn_at_tgt)
            except Exception:
                pass
        cur = cur.getNext()
        forward += 1

    if found_call is None:
        return (field_name, str(site), fn_label, None, "no bl forward")

    call_ins, call_fn = found_call

    # The value register for PlayReport::Add overloads is typically x2
    # (3rd arg: this, key, value).  Try x2 first; fall back to w2.
    for value_reg in ("x2", "w2", "x1", "w1"):
        load_ins, load_src = _trace_value_load(
            call_ins.getAddress(), value_reg)
        if load_ins is not None:
            return (
                field_name,
                str(call_ins.getAddress()),
                fn_label,
                "value(%s): %s" % (value_reg, load_src),
                None,
            )
    return (field_name, str(call_ins.getAddress()), fn_label,
            None, "no value load found")


def process_field(field_name):
    print("")
    print("===== '%s' =====" % field_name)
    str_addrs = _find_strings_with_content(field_name)
    if not str_addrs:
        print("  (string not found in .rodata — possible if the name is")
        print("   built dynamically or split, or analysis hasn't loaded")
        print("   strings yet)")
        return
    print("  string occurrences: %d" % len(str_addrs))

    ref_mgr = currentProgram.getReferenceManager()
    total_xrefs = 0
    for sa_int in str_addrs:
        sa = _addr(sa_int)
        refs = list(ref_mgr.getReferencesTo(sa))
        for r in refs[:MAX_XREFS_PER_FIELD]:
            total_xrefs += 1
            field, call_site, fn_label, value_info, err = _analyze_xref(
                field_name, r)
            if value_info:
                print("  xref %s -> %s  callsite=%s  in %s" %
                      (r.getFromAddress(), value_info, call_site, fn_label))
            else:
                print("  xref %s  callsite=%s  in %s  [%s]" %
                      (r.getFromAddress(), call_site, fn_label, err))


def main():
    print("SMBW Archipelago: playreport_field_backtrace.py")
    print("  scanning %d PlayReport field names" % len(PR_FIELDS))
    print("  (this is slow; expect ~5-10 sec per field on first run)")

    for f in PR_FIELDS:
        if monitor.isCancelled():
            break
        process_field(f)

    print("")
    print("Done.")
    print("")
    print("Cross-reference guide:")
    print("  - Field 'world_wonder_flower' with load `[x22, #0xC8]` would")
    print("    confirm x22 is the live game-state base.  HamletDuFromage's")
    print("    cheat at NSO +0x49253C writes flower_coin via STR W10,[X22,#0xC8].")
    print("  - Each callsite is a hookable PlayReport builder.  Hooking it")
    print("    + logging the base register value reveals the live struct")
    print("    address at runtime — replaces the Cheat Engine workflow.")


main()
