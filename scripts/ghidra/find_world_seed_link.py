# -*- coding: utf-8 -*-
# Ghidra script -- SMBW Archipelago, link the COUNT machinery to storage.
#
# The per-world seed count recompute (FUN_7100499f14) computes:
#     count = base(0x21f89ab1)
#           + popcount(container-C bitfield 0x1faf41e5, ~30 bits)
#           + popcount(container-C bitfield 0xb9bd745d, ~30 bits)
# and stores it to 0x8c20ccb7; the gate reads the animated 0x390eb960.
#
# DECISIVE QUESTION: are the two popcounted bitfields (0x1faf41e5,
# 0xb9bd745d) DERIVED from the global per-course bitfield 0x60458608 on
# world entry, or are they independent storage?
#   * derived  -> 0x60458608 is the master; writing it (current world's
#     slice) flows into the count and persists.
#   * independent -> count = base + these two; 0x60458608 is cosmetic.
#
# Method:
#   1. Scan all functions for immediate loads of the storage + count
#      hashes.
#   2. Print a correlation table: any function loading BOTH 0x60458608
#      and 0x1faf41e5/0xb9bd745d is the world-entry copy (the link).
#   3. Decompile every function that loads 0x1faf41e5 or 0xb9bd745d so we
#      can read the populator/consumer logic directly.
#
# OUTPUT -> OUTPUT_PATH inside the repo.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

OUTPUT_PATH = (
    r"C:\Users\maxwe\Documents\smwonder_archipelago\.claude\worktrees"
    r"\bridge-cse_01KtYB9wuC3NNZhW48vZ89Zd\scripts\ghidra"
    r"\out_world_seed_link.txt")

TARGETS = [
    (0x60458608, "global per-course WS bitfield (container C)"),
    (0x1faf41e5, "count popcount bitfield A (current world)"),
    (0xb9bd745d, "count popcount bitfield B (current world)"),
    (0x21f89ab1, "count base (container-A secondary)"),
    (0x8c20ccb7, "count result (true count)"),
    (0x390eb960, "gate animated count"),
    (0x580b7eb4, "per-course status enum"),
]

# Decompile every loader of these hashes (the interesting storage ones).
DECOMPILE_LOADERS_OF = set([0x1faf41e5, 0xb9bd745d, 0x21f89ab1])

_OUT = {"fh": None}


def emit(s=""):
    print(s)
    fh = _OUT["fh"]
    if fh is not None:
        try:
            fh.write(s)
            fh.write("\n")
        except Exception:
            pass


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


def _scan_function(fn_entry, next_entry, target_set, hits, fn_label):
    listing = currentProgram.getListing()
    pending = {}
    ins = listing.getInstructionAt(fn_entry)
    while ins is not None:
        if monitor.isCancelled():
            return
        addr = ins.getAddress()
        if next_entry is not None and addr.compareTo(next_entry) >= 0:
            break
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
                val = (imm << _lsl_shift_from_insn(ins)) & 0xFFFFFFFF
                pending[dst] = val
                if val in target_set:
                    hits[val].append((str(addr), fn_label, str(ins)))
            else:
                src = None
                try:
                    for o in ins.getOpObjects(1):
                        if o is not None and o.__class__.__name__ == "Register":
                            src = _w_to_x(o.getName().lower())
                            break
                except:
                    pass
                if src is not None and src in pending:
                    pending[dst] = pending[src]
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
                val = (~(imm << _lsl_shift_from_insn(ins))) & 0xFFFFFFFF
                pending[dst] = val
                if val in target_set:
                    hits[val].append((str(addr), fn_label, str(ins)))
        else:
            pending.pop(dst, None)
        ins = listing.getInstructionAfter(addr)


def _decompile(fn):
    from ghidra.app.decompiler import DecompInterface
    from ghidra.util.task import ConsoleTaskMonitor
    ifc = DecompInterface()
    ifc.openProgram(currentProgram)
    try:
        result = ifc.decompileFunction(fn, 120, ConsoleTaskMonitor())
        if result is None:
            return None
        df = result.getDecompiledFunction()
        if df is None:
            return None
        return df.getC()
    finally:
        ifc.dispose()


def run():
    emit("SMBW Archipelago: find_world_seed_link.py")
    fn_mgr = currentProgram.getFunctionManager()
    emit("  image base: 0x%x" % currentProgram.getImageBase().getOffset())
    emit("")

    target_set = set(t for t, _ in TARGETS)
    hits = {t: [] for t, _ in TARGETS}

    fn_records = []
    for fn in fn_mgr.getFunctions(True):
        if monitor.isCancelled():
            break
        try:
            ent = fn.getEntryPoint()
            fn_records.append((ent, "%s@%s" % (fn.getName(), ent)))
        except:
            continue
    fn_records.sort(key=lambda r: r[0].getOffset())
    emit("  scanning %d functions..." % len(fn_records))

    for i, (ent, label) in enumerate(fn_records):
        if monitor.isCancelled():
            break
        if (i & 0x3FFF) == 0 and i:
            emit("  ... %d/%d" % (i, len(fn_records)))
        nxt = fn_records[i + 1][0] if i + 1 < len(fn_records) else None
        try:
            _scan_function(ent, nxt, target_set, hits, label)
        except:
            continue

    # Correlation: fn -> set of hashes it loads.
    fn_to_hashes = {}
    for k, _ in TARGETS:
        for addr_s, fn_label, insn_s in hits[k]:
            fn_to_hashes.setdefault(fn_label, set()).add(k)

    emit("")
    emit("===== per-hash loader counts =====")
    for k, lbl in TARGETS:
        emit("  0x%08x %-42s : %d loader fn(s)"
             % (k, lbl, len(set(r[1] for r in hits[k]))))

    emit("")
    emit("===== correlation: functions loading >= 2 target hashes =====")
    multi = sorted([(fn, hs) for fn, hs in fn_to_hashes.items() if len(hs) >= 2],
                   key=lambda kv: -len(kv[1]))
    if not multi:
        emit("  (none)")
    for fn_label, hs in multi:
        emit("  %s" % fn_label)
        emit("      loads: %s" % ", ".join("0x%08x" % h for h in sorted(hs)))

    emit("")
    emit("============================================================")
    emit("===== decompiled loaders of the popcount bitfields + base =====")
    emit("============================================================")
    done = set()
    for k in sorted(DECOMPILE_LOADERS_OF):
        for addr_s, fn_label, insn_s in hits.get(k, []):
            try:
                ld = currentProgram.getAddressFactory().getAddress(addr_s)
                fn = fn_mgr.getFunctionContaining(ld)
            except:
                fn = None
            if fn is None:
                continue
            eo = fn.getEntryPoint().getOffset()
            if eo in done:
                continue
            done.add(eo)
            hs = fn_to_hashes.get(fn_label, set())
            emit("")
            emit("----- %s @ %s  (loads: %s) -----"
                 % (fn.getName(), fn.getEntryPoint(),
                    ", ".join("0x%08x" % h for h in sorted(hs))))
            c = _decompile(fn)
            if c is None:
                emit("  (decompile failed)")
                continue
            for line in c.split("\n"):
                emit("  %s" % line)
    emit("")
    emit("Done.")


def main():
    try:
        _OUT["fh"] = open(OUTPUT_PATH, "w")
    except Exception as e:
        print("WARN: could not open output: %s" % e)
        _OUT["fh"] = None
    try:
        run()
    finally:
        if _OUT["fh"] is not None:
            try:
                _OUT["fh"].flush()
                _OUT["fh"].close()
            except Exception:
                pass
            print("Wrote full output to: %s" % OUTPUT_PATH)


main()
