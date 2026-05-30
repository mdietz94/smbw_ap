# -*- coding: utf-8 -*-
# Ghidra script for SMBW Archipelago — read the 81-entry course-name table
# directly from its known address.
#
# Discovery context: dump_course_name_table.py's decompile of
# FUN_71003D4110 revealed the loop:
#
#   uVar7 = 0;
#   do {
#       pbVar3 = (&PTR_s_Course1_71034dec90)[uVar7];   // table @ +0x34dec90
#       ...Murmur3 hash...
#       if (hash == param_1) { *param_2 = uVar7; return 1; }
#       uVar7 = uVar7 + 1;
#   } while (uVar7 != 0x51);                          // 0x51 == 81 entries
#
# So the table base is NSO+0x34dec90 (runtime 0x71034dec90), with 81 qword
# entries each pointing to a course-name C string.  Loop index == course
# bit_index in the per-course Wonder Seed bitfield at container-C hash
# 0x60458608.
#
# This script does ONLY ONE thing: read 81 qwords at the known table
# address, dereference each as a C string, dump the result.  Output is
# the ground-truth course_index -> name mapping we need to fix the
# bridge bit-packing.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function


# Table base discovered from FUN_71003D4110 decompile (2026-05-29).
TABLE_BASE_NSO_OFFSET = 0x034dec90
TABLE_ENTRIES = 81
MAX_STR_LEN = 256


def _read_c_string(addr):
    if addr is None:
        return None
    try:
        mem = currentProgram.getMemory()
        buf = []
        cur = addr
        for _ in range(MAX_STR_LEN):
            try:
                b = mem.getByte(cur) & 0xFF
            except:
                return None
            if b == 0:
                break
            buf.append(chr(b) if 0x20 <= b <= 0x7E else ("\\x%02x" % b))
            try:
                cur = cur.add(1)
            except:
                return None
        if not buf:
            return None
        return "".join(buf)
    except:
        return None


def main():
    image_base = currentProgram.getImageBase().getOffset()
    af = currentProgram.getAddressFactory()
    mem = currentProgram.getMemory()

    table_runtime = image_base + TABLE_BASE_NSO_OFFSET
    base_addr = af.getAddress("%x" % table_runtime)

    print("SMBW Archipelago: dump_course_table_direct.py")
    print("  image base       : 0x%x" % image_base)
    print("  table NSO offset : 0x%x" % TABLE_BASE_NSO_OFFSET)
    print("  table runtime    : %s" % base_addr)
    print("  entries          : %d" % TABLE_ENTRIES)
    print("")

    rows = []
    for i in range(TABLE_ENTRIES):
        if monitor.isCancelled():
            break
        try:
            entry_addr = base_addr.add(i * 8)
            ptr_value = mem.getLong(entry_addr) & 0xFFFFFFFFFFFFFFFF
            ptr_addr = af.getAddress("%x" % ptr_value)
            s = _read_c_string(ptr_addr)
            rows.append((i, str(entry_addr), str(ptr_addr), s))
        except Exception as e:
            rows.append((i, "?", "?", "(error: %s)" % e))

    # Group rows by inferred world prefix from the name.  We don't know
    # the prefix scheme yet -- print first to see what the names look like.
    print("  Course table (course_index -> name):")
    print("  idx | table entry @  | string @       | content")
    for i, ea, pa, s in rows:
        disp = s if s is not None else "(unreadable)"
        # Truncate very long names for readability.
        if len(disp) > 80:
            disp = disp[:77] + "..."
        print("  %3d | %-14s | %-14s | %s" % (i, ea, pa, disp))

    # Best-effort world bucketing using common prefixes / Japanese
    # codename keywords (Savanna=W1, Nettai=tropical, Himitu=Special)
    # documented in FUN_7100743D10 decompile.
    print("")
    print("  ===== world-prefix tally =====")
    print("  (heuristic; revise once names are inspected)")
    BUCKETS = [
        ("W1_or_Savanna", ("W1", "Savanna", "Wakuwaku")),
        ("W2_or_Petal",   ("W2", "Petal",   "Hanabira", "Sango", "Onsen")),
        ("W3",            ("W3",)),
        ("W4_or_Nettai",  ("W4", "Nettai",  "Tropical")),
        ("W5",            ("W5",)),
        ("W6",            ("W6",)),
        ("Castle_Bowser", ("Castle", "Boss", "Last", "Final", "Bowser")),
        ("Special_Himitu", ("Himitu", "Secret", "Special", "S_")),
        ("Course_num",    ("Course",)),
        ("Other",         (),),
    ]
    counts = {label: 0 for label, _ in BUCKETS}
    for i, _, _, s in rows:
        if s is None:
            counts.setdefault("Unreadable", 0)
            counts["Unreadable"] += 1
            continue
        matched = False
        for label, prefixes in BUCKETS:
            if not prefixes:
                continue
            for p in prefixes:
                if p in s:
                    counts[label] += 1
                    matched = True
                    break
            if matched:
                break
        if not matched:
            counts["Other"] += 1
    for label, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        if c > 0:
            print("    %-20s : %d" % (label, c))

    print("")
    print("Done.")
    print("")
    print("Next steps:")
    print("  - Inspect the names.  If they're 'Course1'..'Course81' with")
    print("    no world info embedded, we'll need a separate world->index")
    print("    table (look for adjacent .rodata or a sibling lookup fn).")
    print("  - If names embed worlds (e.g. 'W1_Welcome'), group by prefix")
    print("    and feed into apworld/.../context.py")
    print("    _recompute_wonder_seed_bits() as a hardcoded mapping.")


main()
