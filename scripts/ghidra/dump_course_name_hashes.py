# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project -- M4 location_table
# offline coverage expansion.
#
# Hypothesis: every course's PlayReport `stage_key` is
# Murmur3-32(internal_course_name, seed=0).  The FUN_71003D4110
# decompile (sprint 2, 2026-05-24, docs/static-analysis-findings.md)
# confirmed:
#
#   - Murmur3-32 with seed 0 is the algorithm (constants 0xcc9e2d51,
#     0x1b873593, 0xe6546b64, 0x85ebca6b, 0xc2b2ae35).
#   - The function iterates an 81-entry pointer table at NSO
#     +0x0034DEC90 (Ghidra auto-label PTR_s_Course1_71034dec90)
#     holding pointers to 81 course-name strings.
#
# What's NOT yet confirmed: that the `stage_key` field in PlayReport
# payloads is the SAME hash produced over the SAME table.  This script
# tests that by hashing every entry in the table and matching against
# the 3 stage_keys we've already captured live (W1-1, W1-2, Pipe-Rock
# Palace).  A 3/3 match confirms the hypothesis and gives us all 81
# stage_keys offline -- ~20 minutes of playtest avoided per the
# location_table.py coverage sweep planned for M4.
#
# Output: per-entry (index, string_addr, name, hash) table + a
# verification section that cross-checks the 3 known stage_keys.
#
# Tweak TABLE_NSO_OFFSET / EXPECTED_COUNT at the top if Ghidra's symbol
# tree shows a different anchor than the doc.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function


# ---------------------------------------------------------------------------
# Configuration

# NSO offset of the pointer table.  Absolute address 0x71034DEC90
# minus the NSO base 0x7100000000.
TABLE_NSO_OFFSET = 0x0034DEC90

# Expected entry count from the FUN_71003D4110 decompile.
EXPECTED_COUNT = 81

# Stage keys we've already captured live in PlayReport fixtures
# (bridge/location_table.py).  These are the unsigned-32-bit
# interpretation of the values; PlayReport encodes them as 0xD3 s64
# because W1-1's hash 0xAF11F7FC doesn't fit in positive s32.
KNOWN_STAGE_KEYS = {
    0xAF11F7FC: "W1-1  Welcome to the Flower Kingdom!",
    0x0DD5004B: "W1-2  Piranha Plants on Parade",
    0x8995D757: "Pipe-Rock Plateau Palace",
}

# Soft cap on bytes per string -- if any string exceeds this we bail
# rather than scribble across .rodata when the table layout differs
# from expectations.
MAX_STRING_LEN = 64


# ---------------------------------------------------------------------------
# Murmur3-32 (seed=0 fixed-seed variant -- matches the decompiled
# constants in FUN_71003D4110).

def murmur3_32(data, seed=0):
    """Standard Murmur3-32 over byte buffer `data` (bytearray).
    Returns unsigned 32-bit int.

    Reference vectors (used in _self_test):
       ""       -> 0x00000000
       "a"      -> 0x3c2569b2
       "hello"  -> 0x248bfa47
       "Hello, world!" -> 0xc0363e43
    """
    if not isinstance(data, bytearray):
        data = bytearray(data)
    c1 = 0xCC9E2D51
    c2 = 0x1B873593
    h = seed & 0xFFFFFFFF
    length = len(data)
    nblocks = length // 4

    for i in range(nblocks):
        b0 = data[i * 4]     & 0xFF
        b1 = data[i * 4 + 1] & 0xFF
        b2 = data[i * 4 + 2] & 0xFF
        b3 = data[i * 4 + 3] & 0xFF
        k = b0 | (b1 << 8) | (b2 << 16) | (b3 << 24)
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
        h = ((h << 13) | (h >> 19)) & 0xFFFFFFFF
        h = (h * 5 + 0xE6546B64) & 0xFFFFFFFF

    tail = nblocks * 4
    rem = length & 3
    k1 = 0
    if rem == 3:
        k1 = ((data[tail + 2] & 0xFF) << 16) | \
             ((data[tail + 1] & 0xFF) << 8)  | \
              (data[tail]     & 0xFF)
    elif rem == 2:
        k1 = ((data[tail + 1] & 0xFF) << 8)  | \
              (data[tail]     & 0xFF)
    elif rem == 1:
        k1 = data[tail] & 0xFF
    if rem != 0:
        k1 = (k1 * c1) & 0xFFFFFFFF
        k1 = ((k1 << 15) | (k1 >> 17)) & 0xFFFFFFFF
        k1 = (k1 * c2) & 0xFFFFFFFF
        h ^= k1

    h ^= length & 0xFFFFFFFF
    h ^= (h >> 16)
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= (h >> 13)
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= (h >> 16)
    return h & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Memory helpers

def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def _read_pointer_le(addr):
    """Read a 64-bit little-endian pointer (ARM64) one byte at a time.
    Matches the byte-iteration pattern used elsewhere in this folder."""
    memory = currentProgram.getMemory()
    val = 0
    for i in range(8):
        b = memory.getByte(addr.add(i)) & 0xFF
        val |= b << (8 * i)
    return val


def _read_cstring(addr, max_len=MAX_STRING_LEN):
    """Read a NUL-terminated string at `addr`.

    Returns (bytearray_without_terminator, found_terminator_bool).
    If the string is longer than max_len we return what we read and
    terminator_bool=False so the caller can flag it as suspect."""
    memory = currentProgram.getMemory()
    bs = bytearray()
    for i in range(max_len + 1):
        b = memory.getByte(addr.add(i)) & 0xFF
        if b == 0:
            return bs, True
        bs.append(b)
    return bs, False


# ---------------------------------------------------------------------------
# Sanity check the Murmur3 implementation against canonical vectors.

def _self_test():
    cases = [
        (b"",                0x00000000),
        (b"a",               0x3C2569B2),
        (b"hello",           0x248BFA47),
        (b"Hello, world!",   0xC0363E43),
    ]
    ok = True
    for data, want in cases:
        got = murmur3_32(bytearray(data))
        marker = "OK" if got == want else "FAIL"
        if got != want:
            ok = False
        print("  self-test  Murmur3(%r) = 0x%08x   want 0x%08x   [%s]" %
              (bytes(data), got, want, marker))
    return ok


# ---------------------------------------------------------------------------

def main():
    print("SMBW Archipelago: dump_course_name_hashes.py")
    print("")

    if not _self_test():
        print("")
        print("  ! Murmur3 self-test FAILED -- algorithm implementation is broken.")
        print("  ! Cannot trust any output below; fix murmur3_32 first.")
        return
    print("  Murmur3-32 self-test passed.")
    print("")

    main_base = currentProgram.getImageBase().getOffset()
    table_addr_int = main_base + TABLE_NSO_OFFSET
    table_addr = _addr(table_addr_int)
    print("  image base:                0x%x" % main_base)
    print("  pointer-table NSO offset:  +0x%x" % TABLE_NSO_OFFSET)
    print("  pointer-table absolute:    0x%x" % table_addr_int)
    print("  reading %d pointer entries (8 bytes each)..." % EXPECTED_COUNT)
    print("")

    results = []
    for idx in range(EXPECTED_COUNT):
        if monitor.isCancelled():
            print("  (cancelled by user)")
            break
        entry_addr = table_addr.add(idx * 8)
        try:
            str_ptr = _read_pointer_le(entry_addr)
        except Exception as e:
            print("  ! entry %d: pointer read FAILED at %s (%s); stopping." %
                  (idx, entry_addr, e))
            break
        if str_ptr == 0:
            print("  ! entry %d: pointer is NULL; stopping (table may be shorter "
                  "than expected)." % idx)
            break
        # Use Ghidra's actual memory map for the bound check -- the
        # NSO image extends past the .text/.rodata ranges noted in
        # CLAUDE.md (vtables sit at 0x71034BXXXX+, and the string
        # table may be packed right before/after its pointer table).
        try:
            ptr_addr = _addr(str_ptr)
            in_image = currentProgram.getMemory().contains(ptr_addr)
        except Exception:
            in_image = False
        if not in_image:
            print("  ! entry %d: pointer 0x%x is OUTSIDE the loaded NSO "
                  "image; table base is probably wrong, stopping."
                  % (idx, str_ptr))
            break
        try:
            bs, terminated = _read_cstring(_addr(str_ptr))
        except Exception as e:
            print("  ! entry %d: string read FAILED at 0x%x (%s); skipping."
                  % (idx, str_ptr, e))
            continue
        if not terminated:
            print("  ! entry %d: string at 0x%x exceeded MAX_STRING_LEN=%d "
                  "without a NUL -- table layout likely wrong, stopping."
                  % (idx, str_ptr, MAX_STRING_LEN))
            break
        try:
            display = bs.decode("ascii")
        except Exception:
            # Non-ASCII (could be shift-JIS or UTF-8 with high bytes);
            # show repr so we don't crash the loop.
            display = repr(bytes(bs))
        h = murmur3_32(bs)
        results.append((idx, str_ptr, display, h))

    print("")
    print("=" * 78)
    print("RESULTS  (%d / %d entries decoded)" % (len(results), EXPECTED_COUNT))
    print("=" * 78)
    print("%-4s %-12s  %-40s %-12s %s" %
          ("idx", "string_addr", "course_name", "hash_hex", "marker"))
    print("-" * 78)
    matched = {}
    for idx, ptr, name, h in results:
        marker = ""
        if h in KNOWN_STAGE_KEYS:
            marker = "  <- " + KNOWN_STAGE_KEYS[h]
            matched[h] = (idx, name)
        # Clip very long display strings so the table stays readable.
        disp = name if len(name) <= 38 else name[:35] + "..."
        print("%-4d 0x%010x  %-40s 0x%08x%s" %
              (idx, ptr, disp, h, marker))

    print("")
    print("=" * 78)
    print("STAGE-KEY VERIFICATION")
    print("=" * 78)
    for want_hash, label in sorted(KNOWN_STAGE_KEYS.items()):
        if want_hash in matched:
            idx, name = matched[want_hash]
            print("  0x%08x  (%s)" % (want_hash, label))
            print("                -> idx %d, name %r   MATCH" % (idx, name))
        else:
            print("  0x%08x  (%s)" % (want_hash, label))
            print("                -> NOT FOUND in the %d-entry table"
                  % len(results))

    print("")
    if len(matched) == len(KNOWN_STAGE_KEYS):
        print("=" * 78)
        print("ALL %d known stage_keys matched." % len(KNOWN_STAGE_KEYS))
        print("=" * 78)
        print("Hypothesis CONFIRMED:")
        print("  stage_key = Murmur3-32(internal_course_name, seed=0)")
        print("")
        print("Next step: copy the (course_name -> hash_hex) mapping above")
        print("into bridge/location_table.py.  Map each internal name to")
        print("the human-readable AP location name in")
        print("manual_smbwonder_zim/data/locations.json by world/course")
        print("ordering -- ~5 minutes of desk work, no Ryujinx required.")
    elif len(matched) > 0:
        print("=" * 78)
        print("PARTIAL MATCH (%d / %d)." % (len(matched), len(KNOWN_STAGE_KEYS)))
        print("=" * 78)
        print("Possible causes:")
        print("  - Seed is not 0 (try non-zero seeds; check FUN_71003D4110")
        print("    decompile for the literal `seed` value passed in).")
        print("  - Some stage types (e.g. palaces) live in a separate table")
        print("    or use a different hash function.")
        print("  - Off-by-one in TABLE_NSO_OFFSET (try +/- 8).")
    else:
        print("=" * 78)
        print("NO MATCHES.")
        print("=" * 78)
        print("The 3 known stage_keys are not Murmur3-32-seed-0 of any string")
        print("in this table.  Either:")
        print("  - TABLE_NSO_OFFSET (0x%x) is wrong -- look up" % TABLE_NSO_OFFSET)
        print("    PTR_s_Course1_71034dec90 in Ghidra's symbol tree and")
        print("    update TABLE_NSO_OFFSET to (its_addr - image_base).")
        print("  - The hash uses a non-zero seed (recheck FUN_71003D4110).")
        print("  - stage_key is NOT Murmur3 of an internal name -- fall back")
        print("    to find_stage_key_table.py or the playtest sweep.")


main()
