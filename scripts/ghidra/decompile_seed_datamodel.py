# -*- coding: utf-8 -*-
# Ghidra script for SMBW Archipelago -- Wonder Seed persistence (2026-05-29).
#
# Goal: map the per-course save-data model end-to-end so we can answer:
#
#   Q3  Is container-C hash 0x60458608 a genuine >64-bit field, or a
#       u64 with data[2..3] mirroring data[0..1] (like badges)?  The
#       READER FUN_7100124134 indexes data[bit>>5] up to bit 127, but
#       that proves nothing about persisted WIDTH.  We need (a) the
#       typed-object's declared bit-count and (b) what the populator
#       does with high bits.
#
#   Q2  Where is the per-SEED (which exit-types collected per course)
#       bitmask?  Strong candidate: container-D (gmd+0x788), a
#       (hash, course_index) -> u32 store.  Decompile its reader/writer
#       and the WonderSeedAwarded acquisition path to find which hash
#       the game writes when a seed is grabbed.
#
#   Q1  Where are the ~50 side courses tracked (table only has 80
#       entries)?  The cache-rebuild orchestrator FUN_710066DEEC calls
#       ~6 populators; decompiling ALL of them shows every container the
#       per-course UI cache pulls from -- side-course storage included.
#
# OUTPUT: all console text is ALSO written to OUTPUT_PATH (a file inside
# the repo) so nothing is lost to console scrollback truncation.  The
# driving agent reads that file directly.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

OUTPUT_PATH = (
    r"C:\Users\maxwe\Documents\smwonder_archipelago\.claude\worktrees"
    r"\bridge-cse_01KtYB9wuC3NNZhW48vZ89Zd\scripts\ghidra"
    r"\out_decompile_seed_datamodel.txt")

# (nso_offset, name_hint, role)
TARGETS = [
    (0x000124134, "FUN_7100124134",
     "container-C/D BIT READER (gmd, out*, hash, bit_index) -- Q3: look for "
     "the typed-obj layout + any bit_count bounds check"),
    (0x0000E258C, "FUN_71000E258C",
     "container-D u32 READER (gmd, out*, hash, course_index) -- Q2"),
    (0x001F2B354, "FUN_7101F2B354",
     "container-D u32 WRITER (gmd, value, hash, course_index) -- Q2: which "
     "gmd+0xXX substruct (expect gmd+0x788)"),
    (0x00066DEEC, "FUN_710066DEEC",
     "per-course cache-rebuild ORCHESTRATOR -- calls ~6 populators (Q1/Q2)"),
    (0x00066E548, "FUN_710066E548",
     "populator: reads 0x60458608 (persistent) + 0x580b7eb4 into 208B "
     "records -- Q1/Q2/Q3"),
    (0x001E02188, "FUN_7101E02188",
     "WonderSeedAwarded Nerve execute -- root of the seed-grab write path "
     "(Q2)"),
    (0x00081D220, "FUN_710081D220",
     "3-hop parent of the per-course reader -- read-modify-write candidate "
     "(Q2)"),
    (0x00743D10, "FUN_7100743D10",
     "loads 0x580b7eb4 + WS mirror -- per-course->mirror bridge"),
]

# Orchestrators whose DIRECT callees we also decompile (deduped against
# the fixed TARGETS so we never decompile the same fn twice).
EXPAND_CALLEES = [
    (0x00066DEEC, "FUN_710066DEEC"),
    (0x00066E548, "FUN_710066E548"),
]

COMMENT_TAG = "SEED-DATAMODEL: "

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


def _plate(addr, text):
    listing = currentProgram.getListing()
    existing = listing.getComment(0, addr)
    if existing is None:
        existing = ""
    payload = COMMENT_TAG + text
    if payload in existing:
        return
    new = existing + ("\n" if existing else "") + payload
    listing.setComment(addr, 0, new)


def _scan_called_functions(fn):
    """(call_site_addr, callee_fn) for every call inside fn.  Walks entry
    to next-fn-entry to avoid fn.getBody() (buggy on this binary)."""
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
    MAX_INSNS = 16384
    while ins is not None and steps < MAX_INSNS:
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
                    callee_addr = r.getToAddress()
                    callee = fn_mgr.getFunctionAt(callee_addr)
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
    emit("SMBW Archipelago: decompile_seed_datamodel.py")
    fn_mgr = currentProgram.getFunctionManager()
    image_base = currentProgram.getImageBase().getOffset()
    emit("  image base: 0x%x" % image_base)
    emit("  output file: %s" % OUTPUT_PATH)
    emit("")

    work = []
    seen_entries = set()

    def add_work(ent, name_hint, role):
        if ent in seen_entries:
            return
        seen_entries.add(ent)
        work.append((ent, name_hint, role))

    for nso_off, name_hint, role in TARGETS:
        add_work(image_base + nso_off, name_hint, role)

    for nso_off, name_hint in EXPAND_CALLEES:
        ent = image_base + nso_off
        fn = _get_fn(fn_mgr, ent)
        if fn is None:
            emit("  [expand] %s NO FUNCTION" % name_hint)
            continue
        callees = _scan_called_functions(fn)
        added = 0
        for site, callee in callees:
            try:
                cent = callee.getEntryPoint().getOffset()
            except:
                continue
            if cent in seen_entries:
                continue
            add_work(cent, callee.getName(),
                     "callee of %s @ %s" % (name_hint, site))
            added += 1
        emit("  [expand] %s -> %d new callee(s) queued" % (name_hint, added))
    emit("  total functions to decompile: %d" % len(work))
    emit("")

    decompile_text = []
    for ent, name_hint, role in work:
        if monitor.isCancelled():
            break
        fn = _get_fn(fn_mgr, ent)
        if fn is None:
            emit("===== 0x%x %s =====  [NO FUNCTION]" % (ent, name_hint))
            continue
        emit("===== 0x%x %s =====" % (ent, name_hint))
        emit("  role: %s" % role)
        emit("  ghidra name: %s @ %s" % (fn.getName(), fn.getEntryPoint()))

        _plate(fn.getEntryPoint(), "%s -- %s" % (name_hint, role))

        callees = _scan_called_functions(fn)
        seen = set()
        deduped = []
        for site, callee in callees:
            cname = callee.getName()
            if cname in seen:
                continue
            seen.add(cname)
            deduped.append((site, callee))
        if deduped:
            emit("  callees (deduped, first 16):")
            for site, callee in deduped[:16]:
                emit("    %s -> %s @ %s"
                     % (site, callee.getName(), callee.getEntryPoint()))

        c = _decompile(fn)
        if c is None:
            emit("  (decompile failed)")
            emit("")
            continue
        decompile_text.append((name_hint, str(fn.getEntryPoint()), c))
        emit("  decompile: %d chars (printed at end)" % len(c))
        emit("")

    emit("")
    emit("============================================================")
    emit("===== decompile bodies =====")
    emit("============================================================")
    for name_hint, ent, c in decompile_text:
        emit("")
        emit("----- %s @ %s -----" % (name_hint, ent))
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
