#!/usr/bin/env python3
"""Save-buffer diff tool for the M3 grant save-diff sprint.

Companion to docs/save-diff-grants.md. Given two SMBW save buffers
(`%APPDATA%\\Ryujinx\\bis\\user\\save\\0000000000000002\\<user>\\game_data.sav`)
captured before and after acquiring exactly one in-game item, print
the per-(hash, value) pair changes that the acquisition produced.

The SMBW save format we observed in profile 0 on 2026-05-21:

    0x00..0x28  header
        +0x00   u32 magic        = 0x01020304 (little-endian `04 03 02 01`)
        +0x04   u32 version      = 1
        +0x08   u32 length_field = 0xbf0
        +0x0c..+0x27 padding (zeros)

    0x28..0x428    first counter table — 128 pairs of (u32 hash_key, u32 value)
                   This is the same hash-keyed counter container that
                   FUN_710012ae94 reads at runtime (M3.3 probe).
                   Entry 128 (at 0x428) is `(0, 1)` in observed saves —
                   either a sentinel or unused slot; diff treats it as
                   past-the-end.
    0x428..0xbf0   second table — pairs of (u32 hash_key, u32 offset)
                   where offset points into the string blob below.
                   "Values" here are monotonically increasing string-blob
                   offsets, NOT counter values; the diff tool clips here
                   so adding a string between saves doesn't false-positive.
    0xbf0..EOF    string blob.

The first-table key space is exactly what we need for the grant code:
when we diff before/after, the changed keys are usable directly with
the runtime hash table — no need to crack Nintendo's hash function.

Usage:
    python scripts/savediff.py before.sav after.sav
    python scripts/savediff.py --summary single.sav
"""

from __future__ import annotations

import argparse
import struct
from dataclasses import dataclass
from typing import Iterator


SMBW_SAVE_MAGIC = 0x01020304
HEADER_BYTES = 0x28

# Empirical: 128 entries of (u32 key, u32 value) starting at 0x28, ending
# at 0x428. Past that, pair-value semantics change to string-blob offsets,
# so the diff tool's "interpret as key/value" mode applies only here.
FIRST_TABLE_OFFSET = 0x28
FIRST_TABLE_COUNT = 128
FIRST_TABLE_END_HINT = FIRST_TABLE_OFFSET + FIRST_TABLE_COUNT * 8  # 0x428


@dataclass(frozen=True)
class PairChange:
    pair_index: int        # 0-based within the first table
    offset: int            # byte offset in the save file
    key: int               # u32 hash key (unchanged between before/after)
    before_value: int
    after_value: int

    @property
    def delta(self) -> int:
        return self.after_value - self.before_value

    def classify(self) -> str:
        b, a = self.before_value, self.after_value
        if b == 0 and a != 0:
            # Possibly a bit-set in a bitfield-stored-as-u32, or a counter
            # going from 0 to N. Bit-set fits when a has popcount 1.
            if a & (a - 1) == 0:
                bit = a.bit_length() - 1
                return f"first-acquire / bit {bit} set"
            return f"first-acquire (0 → {a})"
        if a == b + 1:
            return "increment by 1"
        if a == b - 1:
            return "decrement by 1"
        if (b ^ a).bit_count() == 1:
            # Single-bit toggle in a bitfield.
            bit = (b ^ a).bit_length() - 1
            old = "set" if b & (1 << bit) else "clear"
            new = "set" if a & (1 << bit) else "clear"
            return f"bit {bit} flip ({old} → {new})"
        return f"change ({a - b:+d})"


@dataclass(frozen=True)
class RegionChange:
    """A changed byte run that isn't aligned to a first-table pair."""
    offset: int
    before: bytes
    after: bytes


def parse_header(buf: bytes) -> tuple[int, int, int]:
    if len(buf) < HEADER_BYTES:
        raise ValueError(f"save too short ({len(buf)} bytes < {HEADER_BYTES})")
    magic, version, length_field = struct.unpack_from("<III", buf, 0)
    if magic != SMBW_SAVE_MAGIC:
        raise ValueError(f"bad magic 0x{magic:08x}, expected 0x{SMBW_SAVE_MAGIC:08x}")
    return magic, version, length_field


def iter_first_table_pairs(buf: bytes) -> Iterator[tuple[int, int, int]]:
    """Yield (pair_index, key, value) for each (u32, u32) pair in the first table."""
    end = min(len(buf), FIRST_TABLE_END_HINT)
    pair_index = 0
    for off in range(FIRST_TABLE_OFFSET, end - 7, 8):
        key, val = struct.unpack_from("<II", buf, off)
        yield pair_index, key, val
        pair_index += 1


def diff_pairs(before: bytes, after: bytes) -> list[PairChange]:
    changes: list[PairChange] = []
    bi = iter_first_table_pairs(before)
    ai = iter_first_table_pairs(after)
    for (bidx, bkey, bval), (aidx, akey, aval) in zip(bi, ai):
        if bidx != aidx:
            raise AssertionError("pair index drift")
        if bkey != akey:
            # Key changed too — table re-ordered between saves. Flag it.
            changes.append(PairChange(bidx, FIRST_TABLE_OFFSET + bidx * 8,
                                       bkey, bval, aval))
            continue
        if bval != aval:
            changes.append(PairChange(bidx, FIRST_TABLE_OFFSET + bidx * 8,
                                       bkey, bval, aval))
    return changes


def diff_outside_pairs(before: bytes, after: bytes) -> list[RegionChange]:
    """Find changed byte runs outside the first table."""
    regions: list[RegionChange] = []
    if len(before) != len(after):
        return [RegionChange(0, before, after)]
    n = len(before)
    end_of_first_table = min(n, FIRST_TABLE_END_HINT)
    in_run = False
    run_start = 0
    for i in range(n):
        if FIRST_TABLE_OFFSET <= i < end_of_first_table:
            # Handled by diff_pairs.
            if in_run:
                regions.append(RegionChange(run_start,
                                            before[run_start:i],
                                            after[run_start:i]))
                in_run = False
            continue
        differs = before[i] != after[i]
        if differs and not in_run:
            run_start = i
            in_run = True
        elif not differs and in_run:
            regions.append(RegionChange(run_start,
                                        before[run_start:i],
                                        after[run_start:i]))
            in_run = False
    if in_run:
        regions.append(RegionChange(run_start, before[run_start:], after[run_start:]))
    return regions


def format_pair_change(c: PairChange) -> str:
    return (f"  [pair {c.pair_index:4d} @ 0x{c.offset:04x}]  "
            f"key=0x{c.key:08x}  {c.before_value:>10} → {c.after_value:<10}  "
            f"({c.classify()})")


def format_region_change(r: RegionChange) -> str:
    bhex = r.before.hex(" ")
    ahex = r.after.hex(" ")
    return (f"  [region @ 0x{r.offset:04x}  len={len(r.before)}]\n"
            f"      before: {bhex}\n"
            f"      after:  {ahex}")


def print_diff(before_path: str, after_path: str) -> None:
    with open(before_path, "rb") as f:
        before = f.read()
    with open(after_path, "rb") as f:
        after = f.read()

    print(f"== savediff: {before_path} vs {after_path} ==")
    bmagic, bver, blen = parse_header(before)
    amagic, aver, alen = parse_header(after)
    print(f"   before: {len(before)} bytes, magic=0x{bmagic:08x}, version={bver}, length_field=0x{blen:x}")
    print(f"   after:  {len(after)} bytes, magic=0x{amagic:08x}, version={aver}, length_field=0x{alen:x}")
    print()

    pair_changes = diff_pairs(before, after)
    if pair_changes:
        print("== first-table (hash, value) changes ==")
        for c in pair_changes:
            print(format_pair_change(c))
    else:
        print("== first-table (hash, value) changes ==")
        print("  (none)")
    print()

    region_changes = diff_outside_pairs(before, after)
    if region_changes:
        print(f"== other changed regions ({len(region_changes)} run(s)) ==")
        for r in region_changes:
            print(format_region_change(r))
    else:
        print("== other changed regions ==")
        print("  (none)")
    print()

    if pair_changes:
        print("== keys-summary (paste candidates for identify_seed_keys.py) ==")
        for c in pair_changes:
            print(f"    0x{c.key:08x}: {c.after_value},   # was {c.before_value}, {c.classify()}")


def print_summary(path: str) -> None:
    with open(path, "rb") as f:
        buf = f.read()
    magic, version, length = parse_header(buf)
    print(f"== savediff --summary: {path} ==")
    print(f"   {len(buf)} bytes, magic=0x{magic:08x}, version={version}, length_field=0x{length:x}")
    print()
    print("== first-table non-zero entries ==")
    nz = 0
    for idx, key, val in iter_first_table_pairs(buf):
        if val != 0:
            nz += 1
            print(f"  [pair {idx:4d}]  key=0x{key:08x}  value={val}")
    print(f"\n{nz} non-zero entries (of {idx + 1} total).")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("before", help="before-acquire save file (or single file with --summary)")
    ap.add_argument("after", nargs="?", help="after-acquire save file")
    ap.add_argument("--summary", action="store_true",
                    help="dump non-zero first-table entries from a single save")
    args = ap.parse_args()

    if args.summary:
        print_summary(args.before)
    else:
        if not args.after:
            ap.error("two save files required (or --summary with one)")
        print_diff(args.before, args.after)


if __name__ == "__main__":
    main()
