# -*- coding: utf-8 -*-
# Ghidra script for the SMBW Archipelago project -- M4 location_table
# offline coverage expansion (Phase 2, after dump_course_name_hashes.py
# falsified the Murmur3-of-CourseN hypothesis).
#
# What we know now:
#   - PlayReport's `stage_key` is NOT Murmur3-32-seed-0 of any string
#     in the FUN_71003D4110 lookup table (those are "Course1"..."Course80"
#     + "Invalid" slot placeholders).
#   - We have 3 known stage_keys captured live:
#       W1-1                     = 0xAF11F7FC
#       W1-2                     = 0x0DD5004B
#       Pipe-Rock Plateau Palace = 0x8995D757
#
# Hypothesis 2: stage_keys are precomputed (maybe by an offline tool,
# maybe by a hash function we haven't identified) and stored in a
# CourseInfo struct array in .rodata.  If so, all 81-ish stage_keys
# live next to each other at a fixed stride.
#
# This script:
#   1. Scans every initialized non-executable memory block in the
#      Ghidra program (i.e. .rodata) for each of the 3 known stage_keys
#      encoded as a little-endian 4-byte sequence.
#   2. Reports every hit (address, enclosing block, surrounding 64 bytes
#      as hex+ASCII).
#   3. If 2+ hits share a block, computes the stride between hits and
#      suggests "this is an array entry of size <stride>".
#   4. If a stride is found, walks the array forward+backward up to
#      MAX_TABLE_ENTRIES, prints what looks like each entry's
#      stage_key + adjacent fields (helps spot world_no / course_no).
#
# Output is enough to write the CourseInfo struct definition in a
# follow-up dump script, which then emits the full (stage_key,
# world_no, course_no) tuple for all 81 courses -- offline,
# without Ryujinx.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function


# ---------------------------------------------------------------------------
# Configuration

# Stage keys captured live in PlayReport fixtures.
KNOWN_STAGE_KEYS = [
    (0xAF11F7FC, "W1-1  Welcome to the Flower Kingdom!"),
    (0x0DD5004B, "W1-2  Piranha Plants on Parade"),
    (0x8995D757, "Pipe-Rock Plateau Palace"),
]

# Don't scan executable blocks (.text) -- 32-bit literals in arm64
# instructions are rare but they exist and would just be noise.
# Don't scan uninitialized blocks (.bss).
SKIP_EXECUTABLE = True
SKIP_UNINITIALIZED = True

# Bytes of context to dump around each hit.  Two cacheline widths is
# enough to see ~5 struct fields on either side of the stage_key
# at any reasonable struct size (4, 8, 16, 32, 64 bytes).
CONTEXT_BEFORE = 32
CONTEXT_AFTER = 32

# When walking a discovered stride, how far to go.
MAX_TABLE_ENTRIES = 96  # > 81 to catch the "Invalid"-style end sentinel


# ---------------------------------------------------------------------------
# Helpers

def _addr(n):
    return currentProgram.getAddressFactory().getAddress("%x" % n)


def _read_byte_safe(memory, addr):
    try:
        return memory.getByte(addr) & 0xFF
    except Exception:
        return None


def _read_u32_le(memory, addr):
    try:
        b0 = memory.getByte(addr.add(0)) & 0xFF
        b1 = memory.getByte(addr.add(1)) & 0xFF
        b2 = memory.getByte(addr.add(2)) & 0xFF
        b3 = memory.getByte(addr.add(3)) & 0xFF
        return b0 | (b1 << 8) | (b2 << 16) | (b3 << 24)
    except Exception:
        return None


def _scan_block_for_u32_le(block, needle_u32):
    """Find every occurrence of `needle_u32` as a 4-byte LE sequence in
    `block`.  Returns a list of absolute integer addresses."""
    pattern = bytearray([
        needle_u32 & 0xFF,
        (needle_u32 >> 8) & 0xFF,
        (needle_u32 >> 16) & 0xFF,
        (needle_u32 >> 24) & 0xFF,
    ])
    n = len(pattern)
    hits = []
    memory = currentProgram.getMemory()
    start = block.getStart().getOffset()
    size = block.getSize()
    CHUNK = 65536
    overlap = n - 1
    pos = 0
    # Read in chunks with a tail-overlap so a match straddling a
    # chunk boundary isn't missed.
    while pos < size:
        if monitor.isCancelled():
            return hits
        chunk_len = min(CHUNK + overlap, size - pos)
        bs = bytearray()
        try:
            for i in range(chunk_len):
                bs.append(memory.getByte(_addr(start + pos + i)) & 0xFF)
        except Exception:
            # Likely hit a non-initialized hole inside an otherwise
            # initialized block; advance and continue.
            pos += CHUNK
            continue
        idx = 0
        while True:
            found = bs.find(pattern, idx)
            if found == -1:
                break
            hits.append(start + pos + found)
            idx = found + 1
        pos += CHUNK
    return hits


def _dump_context(addr_int, before=CONTEXT_BEFORE, after=CONTEXT_AFTER):
    """Return a list of strings, one per 16-byte row, around addr_int.
    Marks the row containing addr_int with `*`."""
    memory = currentProgram.getMemory()
    start = (addr_int - before) & ~0xF       # 16-byte align down
    end = addr_int + 4 + after
    rows = []
    cur = start
    while cur < end:
        row_bytes = []
        for i in range(16):
            b = _read_byte_safe(memory, _addr(cur + i))
            row_bytes.append(b)
        hex_parts = []
        ascii_parts = []
        for i, b in enumerate(row_bytes):
            if b is None:
                hex_parts.append("??")
                ascii_parts.append(".")
            else:
                hex_parts.append("%02x" % b)
                ascii_parts.append(chr(b) if 32 <= b < 127 else ".")
        marker = "*" if cur <= addr_int < cur + 16 else " "
        # Add a caret under the matched 4 bytes within the marked row.
        line = "%s 0x%010x  %s  |%s|" % (
            marker, cur, " ".join(hex_parts), "".join(ascii_parts))
        rows.append(line)
        cur += 16
    return rows


def _looks_like_world_course_pair(memory, addr_int):
    """If the bytes immediately before/after the stage_key look like
    plausible (world_no, course_no, ...) ints, report them.  This is
    a pure-heuristic helper; the real struct layout will become
    obvious once we see the dumps."""
    # Try interpreting the 8 bytes before the stage_key as two u32s,
    # and the 8 bytes after as two more.  Print any value <= 32 since
    # world_no <= 7 and course_no <= ~20 in SMBW.
    out = {}
    for off in (-12, -8, -4, 4, 8, 12):
        v = _read_u32_le(memory, _addr(addr_int + off))
        if v is None:
            continue
        out["+%d" % off if off > 0 else "%d" % off] = v
    plausible = [(k, v) for k, v in out.items()
                 if v is not None and v <= 32]
    return out, plausible


def _walk_stride_around(stride, anchor_addr_int, count_each_dir):
    """Walk count_each_dir entries forward and backward at `stride`
    from `anchor_addr_int`, reading the 4-byte u32 at each entry's
    stage_key offset (== anchor offset since we're aligned).  Returns
    list of (entry_index, addr_int, u32).  entry_index is 0 at anchor,
    positive forward, negative backward."""
    memory = currentProgram.getMemory()
    out = []
    for i in range(-count_each_dir, count_each_dir + 1):
        a = anchor_addr_int + i * stride
        v = _read_u32_le(memory, _addr(a))
        out.append((i, a, v))
    return out


# ---------------------------------------------------------------------------
# Main

def main():
    print("SMBW Archipelago: find_stage_key_table.py")
    print("")

    memory = currentProgram.getMemory()
    blocks = []
    for b in memory.getBlocks():
        if not b.isInitialized():
            if SKIP_UNINITIALIZED:
                continue
        if b.isExecute():
            if SKIP_EXECUTABLE:
                continue
        blocks.append(b)

    print("  scanning %d initialized non-executable memory blocks:" % len(blocks))
    for b in blocks:
        print("    %s  %s..%s  (size=0x%x)" % (
            b.getName(), b.getStart(), b.getEnd(), b.getSize()))
    print("")

    # ---- Phase 1: find every hit for each known stage_key.
    print("=" * 78)
    print("PHASE 1 -- raw hit locations")
    print("=" * 78)
    all_hits = {}  # u32 -> list of (addr_int, block_name)
    for needle, label in KNOWN_STAGE_KEYS:
        print("")
        print("--- 0x%08x  (%s) ---" % (needle, label))
        per_key_hits = []
        for block in blocks:
            if monitor.isCancelled():
                break
            block_hits = _scan_block_for_u32_le(block, needle)
            for h in block_hits:
                per_key_hits.append((h, block.getName()))
        all_hits[needle] = per_key_hits
        if not per_key_hits:
            print("    (no hits)")
            continue
        print("    %d hit(s):" % len(per_key_hits))
        for addr_int, blk_name in per_key_hits:
            print("      0x%010x  in block %r" % (addr_int, blk_name))

    # ---- Phase 2: per-hit context dumps + adjacent-field probe.
    print("")
    print("=" * 78)
    print("PHASE 2 -- context dumps around each hit")
    print("=" * 78)
    for needle, label in KNOWN_STAGE_KEYS:
        hits = all_hits.get(needle, [])
        if not hits:
            continue
        for addr_int, blk_name in hits:
            print("")
            print("--- 0x%08x at 0x%010x (block %r) ---" %
                  (needle, addr_int, blk_name))
            for row in _dump_context(addr_int):
                print(row)
            full, plausible = _looks_like_world_course_pair(memory, addr_int)
            print("    surrounding-u32 raw:  %s" %
                  ", ".join("%s=0x%08x" % (k, v) for k, v in sorted(full.items())))
            if plausible:
                print("    plausible small ints (<=32, candidate world/course_no):")
                for k, v in plausible:
                    print("      %s = %d" % (k, v))
            else:
                print("    (no plausibly-small neighboring u32s)")

    # ---- Phase 3: stride detection across hits.
    print("")
    print("=" * 78)
    print("PHASE 3 -- stride detection")
    print("=" * 78)

    # Collect all hits across all 3 stage_keys, sorted by address;
    # then group by enclosing block.
    by_block = {}
    for needle, _label in KNOWN_STAGE_KEYS:
        for addr_int, blk_name in all_hits.get(needle, []):
            by_block.setdefault(blk_name, []).append((addr_int, needle))

    stride_candidates = []
    for blk_name, entries in by_block.items():
        entries.sort()
        if len(entries) < 2:
            print("  block %r: only %d hit(s); cannot derive stride" %
                  (blk_name, len(entries)))
            continue
        deltas = []
        for i in range(len(entries) - 1):
            (a0, k0) = entries[i]
            (a1, k1) = entries[i + 1]
            deltas.append((a1 - a0, k0, a0, k1, a1))
        print("  block %r: %d hit(s); pairwise deltas:" %
              (blk_name, len(entries)))
        for d, k0, a0, k1, a1 in deltas:
            print("    delta=0x%-6x  (%d)   0x%08x@0x%010x -> 0x%08x@0x%010x"
                  % (d, d, k0, a0, k1, a1))
            stride_candidates.append((d, a0, a1, blk_name))

        # If multiple deltas are integer multiples of a common smaller
        # value, that smaller value is the stride.  Try the smallest
        # delta as the candidate stride and see if the others are
        # multiples.
        if deltas:
            smallest = min(d for d, _, _, _, _ in deltas)
            if all(d % smallest == 0 for d, _, _, _, _ in deltas):
                print("    => candidate stride: 0x%x (%d bytes)" %
                      (smallest, smallest))

    # ---- Phase 4: walk array at each stride candidate.
    if stride_candidates:
        print("")
        print("=" * 78)
        print("PHASE 4 -- walking strides")
        print("=" * 78)
        # Deduplicate strides; walk each unique one once, anchored at
        # the lowest known hit so we capture entries before AND after.
        seen_strides = set()
        for stride, a0, a1, blk_name in stride_candidates:
            if stride in seen_strides:
                continue
            seen_strides.add(stride)
            anchor = min(a0, a1)
            print("")
            print("--- stride=0x%x (%d), anchor=0x%010x ---" %
                  (stride, stride, anchor))
            walked = _walk_stride_around(
                stride, anchor, MAX_TABLE_ENTRIES // 2)
            # Trim leading/trailing entries that look like garbage
            # (huge values that don't pattern-match a hash and aren't 0).
            # Show all; mark known stage_keys.
            known_set = {k for k, _ in KNOWN_STAGE_KEYS}
            for i, a, v in walked:
                if v is None:
                    print("    [%+3d]  0x%010x  (read failed)" % (i, a))
                    continue
                tag = ""
                if v in known_set:
                    tag = "  <- KNOWN"
                print("    [%+3d]  0x%010x  u32=0x%08x%s" %
                      (i, a, v, tag))

    print("")
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    n_found = sum(1 for k, _ in KNOWN_STAGE_KEYS if all_hits.get(k))
    print("  known stage_keys with at least one hit: %d / %d" %
          (n_found, len(KNOWN_STAGE_KEYS)))
    if n_found == 0:
        print("")
        print("  None of the 3 known stage_keys appear as 4-byte LE sequences")
        print("  in .rodata.  Either:")
        print("    - stage_key is computed at runtime from other fields (not")
        print("      stored as a raw constant).")
        print("    - It's stored at a non-natural alignment we'd have to")
        print("      catch with a finer-grained scan.")
        print("    - It's stored byte-reversed (big-endian) or split into")
        print("      pieces.  (Try editing the script's _scan_block_for_u32_le")
        print("      pattern to BE bytes.)")
    elif n_found < len(KNOWN_STAGE_KEYS):
        print("")
        print("  Only some stage_keys hit.  Possible: palaces live in a")
        print("  separate table from courses, or the table has been")
        print("  split somehow.  Check Phase 2 context dumps for the ones")
        print("  that DID hit -- the surrounding bytes will tell us what")
        print("  the struct looks like, and we can write a follow-up dumper")
        print("  even with partial data.")
    else:
        print("")
        print("  All 3 known stage_keys located.  Cross-check the Phase 3")
        print("  stride suggestion and the Phase 4 walk -- if the walk shows")
        print("  a plausible run of hash-looking u32 values for ~80 entries,")
        print("  you have the table.  Use the surrounding-u32 dumps to find")
        print("  the world_no / course_no offsets, then write a dump script")
        print("  that emits the full (stage_key, world_no, course_no) tuple")
        print("  per entry.")


main()
