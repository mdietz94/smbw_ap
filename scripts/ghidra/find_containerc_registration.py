# -*- coding: utf-8 -*-
# Ghidra script for SMBW Archipelago -- Wonder Seed persistence Q3 (2026-05-29).
#
# QUESTION: does container-C hash 0x60458608 (per-course Wonder Seed bit)
# persist >64 bits, or is it a u64 with data[2..3] mirroring data[0..1]
# like the badge bitfield 0x105df820?
#
# The reader FUN_7100124134 bounds bit_index against a VIRTUAL bit-count
# (vtable slot 0x20) on the typed object, and serialization width is set
# when the object is constructed.  Both come from the REGISTRATION site --
# the code that allocates the container-C typed object and inserts it
# under its hash.
#
# This script:
#   1. Scans every function for instructions that materialize the 32-bit
#      immediates 0x60458608 (Wonder Seed) and 0x105df820 (badge,
#      known-u64 reference).  (movz/movk/movn/mov tracking, same engine
#      as find_hash_immediate_loads.py.)
#   2. Decompiles EVERY function that loads either literal and writes the
#      bodies to a file.  The registration site is the loader that is NOT
#      the known reader (FUN_7100124134) -- it will show the alloc + the
#      typed-object construction (vtable store + bit-count field), which
#      tells us the declared width directly.
#
# Compare the WS registration against the badge registration: if both
# construct the same class with the same size param, WS is a u64 mirror
# (Q3 = data[2..3] does NOT persist).  If WS gets a larger size, it is a
# genuine wide bitfield (Q3 = high bits persist).
#
# OUTPUT: written to OUTPUT_PATH inside the repo (the driving agent reads
# it directly).

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

OUTPUT_PATH = (
    r"C:\Users\maxwe\Documents\smwonder_archipelago\.claude\worktrees"
    r"\bridge-cse_01KtYB9wuC3NNZhW48vZ89Zd\scripts\ghidra"
    r"\out_containerc_registration.txt")

# Known reader -- we already have it; flag loaders that ARE this fn so we
# can ignore them and zero in on the registration/constructor.
KNOWN_READER_NSO = 0x000124134  # FUN_7100124134

TARGETS = [
    (0x60458608, "per-course Wonder Seed bitfield (container C)"),
    (0x105df820, "badge owned bitfield (container C, known u64 @ file 0x0EA0)"),
]

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
    n_insns = 0
    ins = listing.getInstructionAt(fn_entry)
    while ins is not None:
        if monitor.isCancelled():
            return n_insns
        addr = ins.getAddress()
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
                src_reg = None
                try:
                    for o in ins.getOpObjects(1):
                        if o is not None and o.__class__.__name__ == "Register":
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
    return n_insns


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
    emit("SMBW Archipelago: find_containerc_registration.py")
    fn_mgr = currentProgram.getFunctionManager()
    image_base = currentProgram.getImageBase().getOffset()
    known_reader = image_base + KNOWN_READER_NSO
    emit("  image base: 0x%x" % image_base)
    emit("  output: %s" % OUTPUT_PATH)
    emit("  scanning for immediates:")
    for k, lbl in TARGETS:
        emit("    0x%08x  %s" % (k, lbl))
    emit("")

    target_set = set(t for t, _ in TARGETS)
    hits = {t: [] for t, _ in TARGETS}

    fn_records = []
    for fn in fn_mgr.getFunctions(True):
        if monitor.isCancelled():
            break
        try:
            ent = fn.getEntryPoint()
            label = "%s@%s" % (fn.getName(), ent)
        except:
            continue
        fn_records.append((ent, label))
    fn_records.sort(key=lambda r: r[0].getOffset())
    emit("  collected %d function entries" % len(fn_records))

    n_fns = 0
    for i, (ent, label) in enumerate(fn_records):
        if monitor.isCancelled():
            emit("  (cancelled)")
            break
        n_fns += 1
        if (n_fns & 0x1FFF) == 0:
            emit("  ... scanned %d/%d functions" % (n_fns, len(fn_records)))
        next_entry = fn_records[i + 1][0] if i + 1 < len(fn_records) else None
        try:
            _scan_function(ent, next_entry, target_set, hits, label)
        except:
            continue

    emit("")
    emit("===== immediate-load hits =====")
    loader_fns = {}  # entry_offset -> (fn, label)
    for k, lbl in TARGETS:
        emit("")
        emit("--- 0x%08x (%s) ---" % (k, lbl))
        rows = hits[k]
        if not rows:
            emit("  (none)")
            continue
        by_fn = {}
        for addr_s, fn_label, insn_s in rows:
            by_fn.setdefault(fn_label, []).append((addr_s, insn_s))
        for fn_label in sorted(by_fn.keys()):
            is_reader = ("%s" % known_reader) in fn_label or "124134" in fn_label
            tag = "  [KNOWN READER]" if is_reader else ""
            emit("  %s  (%d site(s))%s" % (fn_label, len(by_fn[fn_label]), tag))
            for addr_s, insn_s in by_fn[fn_label]:
                emit("      %s  %s" % (addr_s, insn_s))

    # Decompile every unique loader function (skip the known reader to
    # save space -- we already have it).
    emit("")
    emit("============================================================")
    emit("===== decompiled loader functions =====")
    emit("============================================================")
    decompiled = set()
    for k, lbl in TARGETS:
        for addr_s, fn_label, insn_s in hits[k]:
            # resolve the function at the load address
            try:
                ld_addr = currentProgram.getAddressFactory().getAddress(
                    addr_s)
                fn = fn_mgr.getFunctionContaining(ld_addr)
            except:
                fn = None
            if fn is None:
                continue
            ent_off = fn.getEntryPoint().getOffset()
            if ent_off in decompiled:
                continue
            decompiled.add(ent_off)
            nm = fn.getName()
            if "124134" in nm:
                emit("")
                emit("----- %s (known reader, skipped) -----" % nm)
                continue
            emit("")
            emit("----- %s @ %s  (loads literal) -----"
                 % (nm, fn.getEntryPoint()))
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
        print("WARN: could not open output file: %s" % e)
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
