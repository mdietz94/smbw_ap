# -*- coding: utf-8 -*-
# Ghidra script -- SMBW Archipelago, resolve the 0x60458608 WRITE path.
#
# FUN_7101c41f20 (the world-map "commit cache to save" flush) calls:
#     FUN_710049ea24(gmd, value, 0x60458608, course_index)
# i.e. a 4-ARG call that writes the per-course Wonder Seed bit through
# the game's own deferred-write path.  A prior session decompiled
# FUN_710049ea24 and concluded it was "strictly 3-arg, masks value & 1,
# ignores w3" -- which would make this call nonsensical.  Resolve the
# contradiction by decompiling the full chain with callee expansion.
#
# If FUN_710049ea24 really consumes the 4th arg (course_index) for the
# per-course bitfield, then the CORRECT primitive for AP-authoritative
# persistence is to call it the same way the game does -- guaranteeing
# the write goes through the dirty-queue and survives save, instead of
# poking live RAM via setContainerCBit (which may not be flushed).
#
# OUTPUT -> OUTPUT_PATH inside the repo.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

OUTPUT_PATH = (
    r"C:\Users\maxwe\Documents\smwonder_archipelago\.claude\worktrees"
    r"\bridge-cse_01KtYB9wuC3NNZhW48vZ89Zd\scripts\ghidra"
    r"\out_writepath.txt")

TARGETS = [
    (0x00049EA24, "FUN_710049EA24", "container-B wrapper -- the WRITER the "
     "game uses for 0x60458608 (gmd, value, hash, course_index?)"),
    (0x0005E93FC, "FUN_71005E93FC", "container-B inner delegate"),
    (0x001F263FC, "FUN_7101F263FC", "deferred-write bool setter on gmd+8 "
     "dirty queue"),
    (0x0096E25C, "FUN_710096E25C", "called immediately before the writer in "
     "the FUN_7101c41f20 flush loop"),
    (0x001F2B0E4, "FUN_7101F2B0E4", "container-D inner writer (gmd+0x788)"),
    (0x001F2B1F0, "FUN_7101F2B1F0", "container-D lock check (gmd+0x7e8)"),
]

# Expand direct callees of the main writer so the whole delegate tree
# comes back in one paste.
EXPAND_CALLEES = [
    (0x00049EA24, "FUN_710049EA24"),
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
    emit("SMBW Archipelago: decompile_writepath.py")
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
            emit("  [expand] %s NO FUNCTION" % nm)
            continue
        added = 0
        for site, callee in _scan_called_functions(fn):
            try:
                cent = callee.getEntryPoint().getOffset()
            except:
                continue
            if cent in seen:
                continue
            add_work(cent, callee.getName(),
                     "callee of %s @ %s" % (nm, site))
            added += 1
        emit("  [expand] %s -> %d new callee(s)" % (nm, added))
    emit("  total to decompile: %d" % len(work))
    emit("")

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
        dd = []
        s2 = set()
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
