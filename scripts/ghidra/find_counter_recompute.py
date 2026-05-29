# -*- coding: utf-8 -*-
# Ghidra script -- SMBW Archipelago, find the per-world Wonder Seed COUNT
# recompute (the source of truth for the gate + UI count).
#
# The gate predicate FUN_71001787B40 reads container-A hash 0x390eb960.
# That counter (and 4 lockstep mirrors) resets to 0 on every world
# transition and is RECOMPUTED on world entry.  The recompute function is
# the source of truth for "how many wonder seeds does this world show".
#
# CENTRAL QUESTION for AP count-consistency: does the recompute POPCOUNT
# the per-course bitfield 0x60458608 over the world's course range, or
# does it read a separate stored count?
#   * popcount-in-range  -> AP must set exactly N bits in each world's
#     actual course-index range, and they must persist (Q3).
#   * separate counter    -> AP can set the counter + any N bits anywhere.
#
# This script scans for immediate loads of the 5 mirror hashes, decompiles
# every loading function, and writes them to a file.  The recompute is the
# loader that WRITES these hashes (calls the container-A writer
# FUN_710049F648 @ +0x49F648) -- read its body to see what it counts.
#
# OUTPUT -> OUTPUT_PATH inside the repo.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

OUTPUT_PATH = (
    r"C:\Users\maxwe\Documents\smwonder_archipelago\.claude\worktrees"
    r"\bridge-cse_01KtYB9wuC3NNZhW48vZ89Zd\scripts\ghidra"
    r"\out_counter_recompute.txt")

TARGETS = [
    (0x390eb960, "WS gate-predicate counter (container-A)"),
    (0x21f89ab1, "WS mirror #1 (in-course HUD)"),
    (0x8c20ccb7, "WS mirror #2"),
    (0xeeff353b, "WS mirror #3 (animation source)"),
    (0xa0e5f253, "WS mirror #5 (animation target)"),
    (0x74f3a647, "course-count counter (read by FUN_71001e4ae0)"),
    (0x9f5ead3c, "current player world index"),
]

# Container-A WRITER (counter SET) and READER (counter GET) NSO offsets,
# so we can tag each loader as writer/reader.
CONTAINER_A_WRITER = 0x00049F648
CONTAINER_A_READER = 0x00012AE94

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
    emit("SMBW Archipelago: find_counter_recompute.py")
    fn_mgr = currentProgram.getFunctionManager()
    image_base = currentProgram.getImageBase().getOffset()
    writer_addr = "%x" % (image_base + CONTAINER_A_WRITER)
    reader_addr = "%x" % (image_base + CONTAINER_A_READER)
    emit("  image base: 0x%x" % image_base)
    emit("  container-A WRITER @ %s, READER @ %s" % (writer_addr, reader_addr))
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

    emit("")
    emit("===== loaders per hash =====")
    loader_entries = {}  # ent_off -> fn
    for k, lbl in TARGETS:
        emit("")
        emit("--- 0x%08x (%s): %d loader fn(s) ---"
             % (k, lbl, len(set(r[1] for r in hits[k]))))
        for fn_label in sorted(set(r[1] for r in hits[k])):
            emit("    %s" % fn_label)

    # Decompile every unique loader. Tag whether it calls the container-A
    # writer (=> a recompute/setter) by scanning its callees.
    emit("")
    emit("============================================================")
    emit("===== decompiled loaders (recompute candidates) =====")
    emit("============================================================")
    done = set()
    for k, lbl in TARGETS:
        for addr_s, fn_label, insn_s in hits[k]:
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
            # crude writer/reader tag: does body reference the writer addr?
            body_txt = _decompile(fn)
            tag = ""
            if body_txt:
                if writer_addr in body_txt or "49f648" in body_txt.lower():
                    tag = "  [CALLS CONTAINER-A WRITER -> RECOMPUTE]"
                elif reader_addr in body_txt or "12ae94" in body_txt.lower():
                    tag = "  [reader]"
            emit("")
            emit("----- %s @ %s%s -----"
                 % (fn.getName(), fn.getEntryPoint(), tag))
            if body_txt is None:
                emit("  (decompile failed)")
                continue
            for line in body_txt.split("\n"):
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
