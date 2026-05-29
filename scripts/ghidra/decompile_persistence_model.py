# -*- coding: utf-8 -*-
# Ghidra script -- SMBW Archipelago, the persistent per-world seed store.
#
# FUN_71006b5d6c (world-entry handler) reads the persistent per-world seed
# bitmasks from container-D (gmd+0x788) using two hashes from a per-world
# descriptor lVar9 (regular @ +0x10, special @ +0x14) at an index, copies
# them into the current-world working bitfields 0x1faf41e5 / 0xb9bd745d,
# computes the base 0x21f89ab1 via FUN_710048332c, then recomputes.
#
# To let AP write that persistent store directly (zero-window persistence)
# we need:
#   (a) the per-world (regular_hash, special_hash, index) descriptor source
#       -- comes from FUN_71000d33e0(&iter) over per-world seed-info entries.
#   (b) the base-count semantics -- FUN_710048332c.
#
# This script decompiles the descriptor/base chain AND dumps the rodata
# region around the hash registry table at 0x71029f0bd0 (found earlier to
# contain captured D_write hashes) so we can read the per-world layout.
#
# OUTPUT -> OUTPUT_PATH inside the repo.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

OUTPUT_PATH = (
    r"C:\Users\maxwe\Documents\smwonder_archipelago\.claude\worktrees"
    r"\bridge-cse_01KtYB9wuC3NNZhW48vZ89Zd\scripts\ghidra"
    r"\out_persistence_model.txt")

TARGETS = [
    (0x0006b5d6c, "FUN_71006b5d6c", "world-entry handler: loads container-D "
     "per-world seed bitmasks -> current-world bitfields -> base -> recompute"),
    (0x00048332c, "FUN_710048332c", "base-count computer (writes 0x21f89ab1)"),
    (0x0000d33e0, "FUN_71000d33e0", "descriptor accessor (returns lVar9 = "
     "per-world seed-info entry with hashes at +0x10/+0x14)"),
    (0x00021fda0, "FUN_710021fda0", "descriptor/iterator builder"),
    (0x000584ba0, "FUN_7100584ba0", "descriptor/iterator builder variant"),
    (0x0000e258c, "FUN_71000e258c", "container-D u32 reader (gmd+0x788)"),
    (0x000d3524,  "FUN_71000d3524", "iterator helper (used in base calc)"),
    (0x000d3640,  "FUN_71000d3640", "iterator helper"),
    (0x000d3830,  "FUN_71000d3830", "iterator helper"),
]

# rodata data dump range (the hash-registry table region).
DATA_DUMP_RANGES = [
    (0x71029f0a00, 0x71029f1600),
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


def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


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


def _get_fn(fn_mgr, ent):
    fn = fn_mgr.getFunctionAt(_addr(ent))
    if fn is None:
        fn = fn_mgr.getFunctionContaining(_addr(ent))
    return fn


def _read_u8(off):
    try:
        return getByte(_addr(off)) & 0xFF
    except Exception:
        return None


def _dump_data(start, end):
    emit("  data dump 0x%x .. 0x%x (u32 LE, 4/line):" % (start, end))
    a = start
    while a < end:
        words = []
        for w in range(4):
            base = a + w * 4
            bs = [_read_u8(base + i) for i in range(4)]
            if None in bs:
                words.append("????????")
            else:
                words.append("%08x" % (bs[0] | (bs[1] << 8)
                                       | (bs[2] << 16) | (bs[3] << 24)))
        emit("    0x%x: %s" % (a, "  ".join(words)))
        a += 16


def run():
    emit("SMBW Archipelago: decompile_persistence_model.py")
    fn_mgr = currentProgram.getFunctionManager()
    image_base = currentProgram.getImageBase().getOffset()
    emit("  image base: 0x%x" % image_base)
    emit("")

    bodies = []
    for off, nm, role in TARGETS:
        if monitor.isCancelled():
            break
        fn = _get_fn(fn_mgr, image_base + off)
        if fn is None:
            emit("===== %s [NO FUNCTION @ 0x%x] =====" % (nm, image_base + off))
            continue
        emit("===== %s @ %s =====" % (nm, fn.getEntryPoint()))
        emit("  role: %s" % role)
        c = _decompile(fn)
        if c is None:
            emit("  (decompile failed)")
            emit("")
            continue
        bodies.append((nm, str(fn.getEntryPoint()), c))
        emit("  decompile: %d chars (at end)" % len(c))
        emit("")

    emit("")
    emit("============================================================")
    emit("===== data dumps =====")
    emit("============================================================")
    for s, e in DATA_DUMP_RANGES:
        emit("")
        _dump_data(s, e)

    emit("")
    emit("============================================================")
    emit("===== decompile bodies =====")
    emit("============================================================")
    for nm, ent, c in bodies:
        emit("")
        emit("----- %s @ %s -----" % (nm, ent))
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
