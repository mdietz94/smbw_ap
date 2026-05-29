# -*- coding: utf-8 -*-
# Ghidra script -- SMBW Archipelago, resolve the current-world container-D
# WRITER addressing (2026-05-29).
#
# Empirics: calling FUN_7101F2B354(gmd, value=0x7, hash, course) while in W1
# bumped W1's count by popcount(0x7)=3 and persisted -- but hit the CURRENT
# world (W1), not the hash's world, and immediate read-back via
# FUN_71000e258c (primary container gmd+0x800) showed 0 (writer touches the
# gmd+0x788 / current-stage container).
#
# FUN_7101f2b354(gmd, value, k) -> FUN_7101f2b0e4(gmd+0x788, value, k):
#   if (k == 0) return;
#   ctx = FUN_71006b61d0(*(gmd+0x7a0), *(gmd+0x7ac));   // current-stage ctx
#   if (ctx) { store{value, k} via FUN_7101f2b140(gmd+0x788, {value,k}); }
#
# To SET (not just add) the current world's count to N, I need to know how
# FUN_7101f2b140 stores/keys the entry (overwrite vs OR vs append) and what
# `k` selects.  Decompile the inner chain.
#
# OUTPUT -> OUTPUT_PATH inside the repo.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

OUTPUT_PATH = (
    r"C:\Users\maxwe\Documents\smwonder_archipelago\.claude\worktrees"
    r"\bridge-cse_01KtYB9wuC3NNZhW48vZ89Zd\scripts\ghidra"
    r"\out_pcwriter_inner.txt")

TARGETS = [
    (0x001f2b140, "FUN_7101f2b140", "the actual store (gmd+0x788, &{value,k})"),
    (0x0006b61d0, "FUN_71006b61d0", "current-stage container lookup "
     "(*(gmd+0x7a0), *(gmd+0x7ac), [hash])"),
    (0x001f2af84, "FUN_7101f2af84", "callee of FUN_7101f2b354"),
    (0x0006b622c, "FUN_71006b622c", "writer used at end of world-entry "
     "FUN_71006b5d6c (gmd+0x788, &{1,hash})"),
    (0x001f2b1f0, "FUN_7101f2b1f0", "container-D lock check (gmd+0x7e8)"),
    (0x001f2b0e4, "FUN_7101f2b0e4", "writer middle (re-include for context)"),
]

EXPAND_CALLEES = [
    (0x001f2b140, "FUN_7101f2b140"),
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


def _scan_called_functions(fn):
    listing = currentProgram.getListing()
    fn_mgr = currentProgram.getFunctionManager()
    try:
        entry = fn.getEntryPoint()
    except:
        return []
    next_entry = None
    try:
        nxt_fn = fn_mgr.getFunctionAfter(entry)
        if nxt_fn is not None:
            next_entry = nxt_fn.getEntryPoint()
    except:
        pass
    out = []
    ins = listing.getInstructionAt(entry)
    steps = 0
    while ins is not None and steps < 16384:
        if monitor.isCancelled():
            break
        addr = ins.getAddress()
        if next_entry is not None and addr.compareTo(next_entry) >= 0:
            break
        steps += 1
        try:
            mn = ins.getMnemonicString().lower()
        except:
            ins = listing.getInstructionAfter(addr)
            continue
        if mn in ("bl", "blr", "b"):
            try:
                refs = ins.getReferencesFrom()
            except:
                refs = []
            for r in refs:
                try:
                    is_call = r.getReferenceType().isCall()
                except:
                    is_call = False
                if not is_call and mn != "b":
                    continue
                try:
                    callee = fn_mgr.getFunctionAt(r.getToAddress())
                except:
                    callee = None
                if callee is not None:
                    out.append((addr, callee))
        ins = listing.getInstructionAfter(addr)
    return out


def _get_fn(fn_mgr, ent):
    fn = fn_mgr.getFunctionAt(_addr(ent))
    if fn is None:
        fn = fn_mgr.getFunctionContaining(_addr(ent))
    return fn


def run():
    emit("SMBW Archipelago: decompile_pcwriter_inner.py")
    fn_mgr = currentProgram.getFunctionManager()
    image_base = currentProgram.getImageBase().getOffset()
    emit("  image base: 0x%x" % image_base)
    emit("")

    work = []
    seen = set()

    def add_work(ent, nm, role):
        if ent in seen:
            return
        seen.add(ent)
        work.append((ent, nm, role))

    for off, nm, role in TARGETS:
        add_work(image_base + off, nm, role)
    for off, nm in EXPAND_CALLEES:
        fn = _get_fn(fn_mgr, image_base + off)
        if fn is None:
            continue
        for site, callee in _scan_called_functions(fn):
            try:
                cent = callee.getEntryPoint().getOffset()
            except:
                continue
            add_work(cent, callee.getName(), "callee of %s" % nm)

    bodies = []
    for ent, nm, role in work:
        if monitor.isCancelled():
            break
        fn = _get_fn(fn_mgr, ent)
        if fn is None:
            emit("===== 0x%x %s [NO FUNCTION] =====" % (ent, nm))
            continue
        emit("===== 0x%x %s =====" % (ent, nm))
        emit("  role: %s" % role)
        callees = _scan_called_functions(fn)
        s2 = set()
        dd = []
        for site, callee in callees:
            cn = callee.getName()
            if cn in s2:
                continue
            s2.add(cn)
            dd.append((site, callee))
        if dd:
            emit("  callees:")
            for site, callee in dd[:16]:
                emit("    %s -> %s @ %s"
                     % (site, callee.getName(), callee.getEntryPoint()))
        c = _decompile(fn)
        if c is None:
            emit("  (decompile failed)")
            emit("")
            continue
        bodies.append((nm, str(fn.getEntryPoint()), c))
        emit("  decompile: %d chars (at end)" % len(c))
        emit("")

    emit("")
    emit("===== decompile bodies =====")
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
