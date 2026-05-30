# -*- coding: utf-8 -*-
# Ghidra script for SMBW Archipelago — Wonder Seed RE re-open (2026-05-28).
#
# Find every function that loads a given 32-bit literal as an immediate.
# Seeded with the hash candidates surfaced by the gate-RE work + 2026-05-26
# static-analysis sprint:
#
#   0x60458608 — per-course Wonder Seed bitfield (container D)  ★ primary
#   0x580b7eb4 — adjacent per-course course-state bitfield
#   0x21f89ab1 ─┐
#   0x8c20ccb7 ├─ 5-hash per-current-world Wonder Seed mirror group
#   0xeeff353b │   (gate predicate reads 0x390eb960; the other 4 update
#   0x390eb960 │    in lockstep on world transitions)
#   0xa0e5f253 ─┘
#   0x9f5ead3c — "current player world index" lookup hash
#
# Why this script (vs. walk_hash_writer_xrefs.py): that script harvests
# every CALL TO a known accessor and reverse-walks the hash constant.
# This script does the inverse — given a literal, find every function
# that materializes it, including functions that do NOT call any known
# accessor (e.g., inlined ARM exclusive-monitor write loops, custom
# popcount paths).  Combined the two cover both directions.
#
# IMPLEMENTATION NOTE (2026-05-29 rewrite): the original version called
# `getFunctionContaining(addr)` for every instruction, which (a) is
# O(text * log(fns)) and slow, and (b) hits a Ghidra bug in
# FunctionDB.getBody for at least one function in this binary
# (HomogeneousAggregate.filter throws IndexOutOfBoundsException on a
# malformed parameter prototype).  Both issues are eliminated by
# iterating FUNCTIONS first and then walking each function's body
# instructions -- no per-insn fn lookup, and we never hit the buggy
# fn at all.  Orphan code outside defined functions is skipped; that's
# fine because every target (FUN_710049EA24, FUN_7100124134, etc.) is
# a defined function.
#
# Output:
#
#   ===== 0x60458608 (per-course Wonder Seed bitfield) =====
#     loaded by FUN_71001a2b3c@7101a2b3c0  via movz w2, ...; movk w2, ...
#     loaded by FUN_710049ea24@710049ea24  via mov w2, ...
#     ...
#
# At the end, an inter-literal correlation table — functions that load
# MULTIPLE seeded literals are very likely the popcount / aggregator
# routines we have been chasing.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function


# Hash literals to hunt.  Add new ones as the RE work surfaces them.
TARGETS = [
    (0x60458608, "per-course Wonder Seed bitfield (container D)"),
    (0x580b7eb4, "adjacent per-course course-state bitfield"),
    (0x21f89ab1, "WS mirror hash #1 (in-course HUD)"),
    (0x8c20ccb7, "WS mirror hash #2 (BISECT-C safe)"),
    (0xeeff353b, "WS mirror hash #3 (animation source)"),
    (0x390eb960, "WS gate-predicate hash (FUN_71001787b40)"),
    (0xa0e5f253, "WS mirror hash #5 (animation target)"),
    (0x9f5ead3c, "current player world index"),
    (0xed817774, "session+0x546 touch_goal_top_result"),
    (0xf79bcbb0, "session+0x478 goal_id (last goal reached)"),
    (0xdf82e9ab, "SetCourseClearFlagToGameData write hash"),
    (0x105df820, "badge owned bitfield (container C)"),
    (0x6d1b5c25, "badge UI slot bitmap (container C aux)"),
]


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
    s = str(ins).lower()
    i = s.find("lsl #")
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


def _w_to_x(reg):
    if not reg:
        return None
    n = reg.lower()
    if n.startswith("w") and n[1:].isdigit():
        return "x" + n[1:]
    return n


def _scan_function(fn_entry, fn_label, next_entry, target_set, hits):
    """Walk instructions starting at `fn_entry` and stopping just before
    `next_entry` (or end of listing).  AVOIDS calling `fn.getBody()`
    entirely -- that method triggers Ghidra's calling-convention
    analyzer (FunctionDB.loadVariables -> HomogeneousAggregate.filter),
    which throws java.lang.IndexOutOfBoundsException on at least one
    function in the SMBW NSO whose prototype can't be resolved.  Jython/
    PyGhidra also fails to surface that Throwable through Python's
    `except Exception`, so we sidestep the call rather than catch it.

    Pending per-register state is local to this function -- resets when
    we cross into a new function because each scan call starts fresh.
    """
    listing = currentProgram.getListing()
    pending = {}
    n_insns = 0

    ins = listing.getInstructionAt(fn_entry)
    while ins is not None:
        if monitor.isCancelled():
            return ("cancelled", n_insns)
        addr = ins.getAddress()
        # Stop when we cross into the next function (or off the end).
        if next_entry is not None and addr.compareTo(next_entry) >= 0:
            break
        n_insns += 1

        try:
            mnem = ins.getMnemonicString().lower()
            dst = _w_to_x(_dest_reg(ins))
        except:
            ins = listing.getInstructionAfter(addr)
            continue
        if dst is None:
            ins = listing.getInstructionAfter(addr)
            continue

        if mnem in ("mov", "movz"):
            imm = _imm_at_op(ins, 1)
            if imm is not None:
                shift = _lsl_shift_from_insn(ins)
                val = (imm << shift) & 0xFFFFFFFF
                pending[dst] = val
                if val in target_set:
                    hits[val].append((str(addr), fn_label, str(ins)))
            else:
                # mov w?, w? -- register copy.
                src_reg = None
                try:
                    objs = ins.getOpObjects(1)
                    for o in objs:
                        if o is None:
                            continue
                        if o.__class__.__name__ == "Register":
                            src_reg = _w_to_x(o.getName().lower())
                            break
                except:
                    pass
                if src_reg is not None and src_reg in pending:
                    pending[dst] = pending[src_reg]
                else:
                    pending.pop(dst, None)
        elif mnem == "movk":
            imm = _imm_at_op(ins, 1)
            if imm is not None:
                shift = _lsl_shift_from_insn(ins)
                contrib = (imm << shift) & 0xFFFFFFFF
                base = pending.get(dst, 0)
                mask = ~((0xFFFF) << shift) & 0xFFFFFFFF
                val = (base & mask) | contrib
                pending[dst] = val
                if val in target_set:
                    hits[val].append((str(addr), fn_label, str(ins)))
        elif mnem == "movn":
            imm = _imm_at_op(ins, 1)
            if imm is not None:
                shift = _lsl_shift_from_insn(ins)
                val = (~(imm << shift)) & 0xFFFFFFFF
                pending[dst] = val
                if val in target_set:
                    hits[val].append((str(addr), fn_label, str(ins)))
        else:
            pending.pop(dst, None)

        ins = listing.getInstructionAfter(addr)

    return ("ok", n_insns)


def main():
    print("SMBW Archipelago: find_hash_immediate_loads.py")
    print("  Scanning .text for the following 32-bit literals:")
    for k, label in TARGETS:
        print("    0x%08x  -- %s" % (k, label))
    print("")

    target_set = set(t for t, _ in TARGETS)
    hits = {t: [] for t, _ in TARGETS}

    fn_mgr = currentProgram.getFunctionManager()

    # Pre-collect (entry_addr, name) pairs ordered by entry.  We pair each
    # function's entry with the NEXT function's entry so the scanner knows
    # when to stop -- avoids calling fn.getBody(), which is the buggy path
    # that triggered the IndexOutOfBoundsException.
    fn_records = []  # list of (entry_addr, label)
    for fn in fn_mgr.getFunctions(True):
        if monitor.isCancelled():
            break
        try:
            ent = fn.getEntryPoint()
            label = "%s@%s" % (fn.getName(), ent)
        except:
            continue
        fn_records.append((ent, label))
    # getFunctions(True) returns address-ordered already, but sort to be
    # paranoid (cheap on 140k entries).
    fn_records.sort(key=lambda r: r[0].getOffset())
    print("  collected %d function entries" % len(fn_records))

    n_fns = 0
    n_ok = 0
    n_failed = 0
    n_insns_total = 0
    failures = []  # (fn_label, reason)

    for i, (ent, label) in enumerate(fn_records):
        if monitor.isCancelled():
            print("  (cancelled)")
            break
        n_fns += 1
        if (n_fns & 0x3FF) == 0:
            print("  ... scanned %d/%d functions (%d insns)"
                  % (n_fns, len(fn_records), n_insns_total))
        next_entry = fn_records[i + 1][0] if i + 1 < len(fn_records) else None
        try:
            status, payload = _scan_function(ent, label, next_entry,
                                              target_set, hits)
        except:
            # Bare except: catches Java Throwables that Python `except
            # Exception` doesn't (Jython/PyGhidra surface Java errors
            # outside Python's exception hierarchy in some cases).
            n_failed += 1
            failures.append((label, "raised (suppressed)"))
            continue
        if status == "ok":
            n_ok += 1
            n_insns_total += payload
        elif status == "cancelled":
            n_insns_total += payload
            break
        else:
            n_failed += 1
            failures.append((label, status + ": " + str(payload)))

    print("")
    print("  scan complete: %d functions visited, %d ok, %d failed"
          % (n_fns, n_ok, n_failed))
    print("  total instructions walked: %d" % n_insns_total)
    if failures:
        print("  (first 5 fn failures:)")
        for fn_label, reason in failures[:5]:
            print("    %s  -- %s" % (fn_label, reason))
    print("")

    fn_to_keys = {}
    for k, label in TARGETS:
        print("===== 0x%08x (%s) =====" % (k, label))
        rows = hits[k]
        if not rows:
            print("  (no immediate loads of this literal found)")
            print("")
            continue
        by_fn = {}
        for addr_s, fn_label, insn_s in rows:
            by_fn.setdefault(fn_label, []).append((addr_s, insn_s))
        for fn_label in sorted(by_fn.keys()):
            sites = by_fn[fn_label]
            print("  %s  (%d load site%s)" %
                  (fn_label, len(sites), "" if len(sites) == 1 else "s"))
            for addr_s, insn_s in sites[:4]:
                print("    %s  %s" % (addr_s, insn_s))
            if len(sites) > 4:
                print("    ... (%d more)" % (len(sites) - 4))
            fn_to_keys.setdefault(fn_label, set()).add(k)
        print("")

    multi = sorted(
        [(fn, keys) for fn, keys in fn_to_keys.items() if len(keys) >= 2],
        key=lambda kv: -len(kv[1]))
    print("===== correlation: functions loading >= 2 seeded literals =====")
    if not multi:
        print("  (none)")
    for fn_label, keys in multi:
        keys_s = ", ".join("0x%08x" % k for k in sorted(keys))
        print("  %s  [%d literals] %s" % (fn_label, len(keys), keys_s))
    print("")
    print("Done.")
    print("")
    print("Next steps:")
    print("  - The 0x60458608 reader is presumed to be FUN_7100124134; the")
    print("    writer is the open question.  Cross-check the loaders here")
    print("    against the call sites that route into FUN_710049EA24 or")
    print("    FUN_7101F2B354.  Any function that loads 0x60458608 AND calls")
    print("    one of those is a writer candidate.")
    print("  - Functions loading both 0x60458608 and a mirror hash")
    print("    (0x390eb960 etc.) are the popcount/aggregator that converts")
    print("    per-course bits into per-world counts on world transitions.")


main()
