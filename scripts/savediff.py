#!/usr/bin/env python3
"""Save-buffer diff tool for the M3 grant save-diff sprint.

Companion to docs/save-diff-grants.md and docs/save-diff-findings-2026-05-22.md.
Given two SMBW save buffers
(`%APPDATA%\\Ryujinx\\bis\\user\\save\\0000000000000002\\<user>\\game_data.sav`)
captured before and after acquiring exactly one in-game item, print
the per-(hash, value) pair changes that the acquisition produced.

The SMBW save format we mapped (as of 2026-05-22):

    0x00..0x28   header
        +0x00   u32 magic        = 0x01020304 (little-endian `04 03 02 01`)
        +0x04   u32 version      = 1
        +0x08   u32 length_field = 0xbf0
        +0x0c..+0x27 padding (zeros)

    0x28..0xbf0  pair region — 375 entries of (u32 hash_key, u32 value).
                 Originally believed to be a "first table" of counter pairs
                 (0x28..0x428) followed by a "second table" of blob-offset
                 pairs (0x428..0xbf0). The 2026-05-22 capture analysis
                 showed the 0x428..0xbf0 region is actually MIXED — some
                 subsections hold counter pairs (e.g. flower_coin at pair
                 141 / file offset 0x890, total_play_time_sec at pair 144
                 / file offset 0x8a8), some hold blob-offset pairs,
                 separated by (key=0, val=N) sentinels.

                 The diff tool now scans the FULL pair region (377 pairs).
                 For each changed pair it prints the (key, before, after)
                 so the user can decide whether the change is a counter
                 delta or a blob-offset shift caused by string additions.

    0xbf0..EOF   trailing region. Mostly structured per-course / per-badge
                 records with embedded strings — not a flat string blob.
                 Single-byte and short-run changes here usually map to
                 game-state flags (badge ownership, ten-coin bits, per-world
                 wonder-seed bitmaps, etc.).

Usage:
    python scripts/savediff.py before.sav after.sav
    python scripts/savediff.py before.sav after.sav --mask-noise
    python scripts/savediff.py --summary single.sav
"""

from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass
from typing import Iterator


SMBW_SAVE_MAGIC = 0x01020304
HEADER_BYTES = 0x28

# The full (u32 key, u32 value) pair region. Originally split into a
# "first table" (0x28..0x428, 128 entries) and a "second table"
# (0x428..0xbf0). The 2026-05-22 capture work showed both regions hold
# the same (key, value) format with mixed semantics, so we treat the
# whole span uniformly.
PAIR_REGION_OFFSET = 0x28
PAIR_REGION_END_HINT = 0xbf0
PAIR_REGION_COUNT = (PAIR_REGION_END_HINT - PAIR_REGION_OFFSET) // 8  # 375

# Back-compat aliases for callers / tests that referenced the old names.
FIRST_TABLE_OFFSET = PAIR_REGION_OFFSET
FIRST_TABLE_END_HINT = PAIR_REGION_END_HINT
FIRST_TABLE_COUNT = PAIR_REGION_COUNT

# Empirical save-metadata noise byte ranges from the 2026-05-22 capture
# corpus. These bytes change on every save regardless of in-game state,
# so --mask-noise suppresses them. Each entry is (offset, length).
KNOWN_METADATA_NOISE: list[tuple[int, int]] = [
    (0x16b8, 4),   # 4-byte field that randomizes on every save (hash/salt).
    (0x50c8, 2),   # u16 counter that monotonically increases across saves.
]


@dataclass(frozen=True)
class PairChange:
    pair_index: int        # 0-based within the pair region
    offset: int            # byte offset in the save file
    key: int               # u32 hash key (typically unchanged between captures)
    before_value: int
    after_value: int
    key_changed: bool = False  # true if the hash key itself changed (table re-ordered)

    @property
    def delta(self) -> int:
        return self.after_value - self.before_value

    def classify(self) -> str:
        if self.key_changed:
            return "key changed (table re-ordered)"
        b, a = self.before_value, self.after_value
        if b == 0 and a != 0:
            if a & (a - 1) == 0:
                bit = a.bit_length() - 1
                return f"first-acquire / bit {bit} set"
            return f"first-acquire (0 → {a})"
        if a == b + 1:
            return "increment by 1"
        if a == b - 1:
            return "decrement by 1"
        if (b ^ a).bit_count() == 1:
            bit = (b ^ a).bit_length() - 1
            old = "set" if b & (1 << bit) else "clear"
            new = "set" if a & (1 << bit) else "clear"
            return f"bit {bit} flip ({old} → {new})"
        return f"change ({a - b:+d})"


@dataclass(frozen=True)
class RegionChange:
    """A changed byte run that isn't aligned to a pair-region pair."""
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


def iter_pair_region(buf: bytes) -> Iterator[tuple[int, int, int]]:
    """Yield (pair_index, key, value) for each (u32, u32) pair in the
    full pair region (0x28..0xbf0)."""
    end = min(len(buf), PAIR_REGION_END_HINT)
    pair_index = 0
    for off in range(PAIR_REGION_OFFSET, end - 7, 8):
        key, val = struct.unpack_from("<II", buf, off)
        yield pair_index, key, val
        pair_index += 1


# Back-compat alias for the old name.
iter_first_table_pairs = iter_pair_region


def diff_pairs(before: bytes, after: bytes) -> list[PairChange]:
    changes: list[PairChange] = []
    bi = iter_pair_region(before)
    ai = iter_pair_region(after)
    for (bidx, bkey, bval), (aidx, akey, aval) in zip(bi, ai):
        if bidx != aidx:
            raise AssertionError("pair index drift")
        off = PAIR_REGION_OFFSET + bidx * 8
        if bkey != akey:
            changes.append(PairChange(bidx, off, bkey, bval, aval, key_changed=True))
            continue
        if bval != aval:
            changes.append(PairChange(bidx, off, bkey, bval, aval))
    return changes


def _is_masked(i: int, mask: list[tuple[int, int]]) -> bool:
    for off, length in mask:
        if off <= i < off + length:
            return True
    return False


def diff_outside_pairs(
    before: bytes,
    after: bytes,
    mask: list[tuple[int, int]] | None = None,
) -> list[RegionChange]:
    """Find changed byte runs outside the pair region. If `mask` is
    provided, byte offsets covered by any (off, length) entry are
    treated as unchanged."""
    regions: list[RegionChange] = []
    if len(before) != len(after):
        return [RegionChange(0, before, after)]
    n = len(before)
    end_of_pairs = min(n, PAIR_REGION_END_HINT)
    in_run = False
    run_start = 0
    mask = mask or []
    for i in range(n):
        if PAIR_REGION_OFFSET <= i < end_of_pairs:
            if in_run:
                regions.append(RegionChange(run_start,
                                            before[run_start:i],
                                            after[run_start:i]))
                in_run = False
            continue
        differs = before[i] != after[i]
        if differs and _is_masked(i, mask):
            differs = False
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


def _ascii(b: bytes) -> str:
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b)


def format_region_change(
    r: RegionChange,
    before: bytes | None = None,
    after: bytes | None = None,
    context: int = 16,
) -> str:
    """Format a trailing-region change, optionally with `context` bytes
    of surrounding context from the full before/after buffers."""
    bhex = r.before.hex(" ")
    ahex = r.after.hex(" ")
    head = (f"  [region @ 0x{r.offset:04x}  len={len(r.before)}]\n"
            f"      before: {bhex}\n"
            f"      after:  {ahex}")
    if before is None or after is None or context <= 0:
        return head
    lo = max(0, r.offset - context)
    hi = min(len(before), r.offset + len(r.before) + context)
    pre_ctx = before[lo:hi]
    post_ctx = after[lo:hi]
    # Underline the changed bytes with `^` in the ASCII view.
    marker = bytearray(b" " * (hi - lo))
    for i in range(r.offset, r.offset + len(r.before)):
        if lo <= i < hi:
            marker[i - lo] = ord("^")
    return (head + "\n"
            f"      ctx @ 0x{lo:04x}..0x{hi-1:04x}:\n"
            f"        before: {pre_ctx.hex(' ')}  {_ascii(pre_ctx)}\n"
            f"        after:  {post_ctx.hex(' ')}  {_ascii(post_ctx)}\n"
            f"        marker: {' '.join('  ' if m == ord(' ') else '^^' for m in marker)}")


def print_diff(before_path: str, after_path: str, mask_noise: bool = False) -> None:
    with open(before_path, "rb") as f:
        before = f.read()
    with open(after_path, "rb") as f:
        after = f.read()

    print(f"== savediff: {before_path} vs {after_path} ==")
    bmagic, bver, blen = parse_header(before)
    amagic, aver, alen = parse_header(after)
    print(f"   before: {len(before)} bytes, magic=0x{bmagic:08x}, version={bver}, length_field=0x{blen:x}")
    print(f"   after:  {len(after)} bytes, magic=0x{amagic:08x}, version={aver}, length_field=0x{alen:x}")
    if mask_noise:
        spans = ", ".join(f"0x{o:04x}+{l}" for o, l in KNOWN_METADATA_NOISE)
        print(f"   --mask-noise: suppressing {spans}")
    print()

    pair_changes = diff_pairs(before, after)
    print("== pair-region (hash, value) changes ==")
    if pair_changes:
        for c in pair_changes:
            print(format_pair_change(c))
    else:
        print("  (none)")
    print()

    mask = KNOWN_METADATA_NOISE if mask_noise else None
    region_changes = diff_outside_pairs(before, after, mask=mask)
    print(f"== trailing-region changes ({len(region_changes)} run(s)) ==")
    if region_changes:
        for r in region_changes:
            print(format_region_change(r, before=before, after=after))
    else:
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
    print("== pair-region non-zero entries ==")
    nz = 0
    last_idx = -1
    for idx, key, val in iter_pair_region(buf):
        last_idx = idx
        if val != 0 or key != 0:
            nz += 1
            print(f"  [pair {idx:4d} @ 0x{PAIR_REGION_OFFSET + idx*8:04x}]  key=0x{key:08x}  value={val}")
    print(f"\n{nz} non-zero entries (of {last_idx + 1} total).")


def main() -> None:
    # Output uses '→' which cp1252 can't encode; force UTF-8 on Windows.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("before", help="before-acquire save file (or single file with --summary)")
    ap.add_argument("after", nargs="?", help="after-acquire save file")
    ap.add_argument("--summary", action="store_true",
                    help="dump non-zero pair-region entries from a single save")
    ap.add_argument("--mask-noise", action="store_true",
                    help="suppress known save-metadata noise byte ranges "
                         "(see KNOWN_METADATA_NOISE)")
    args = ap.parse_args()

    if args.summary:
        print_summary(args.before)
    else:
        if not args.after:
            ap.error("two save files required (or --summary with one)")
        print_diff(args.before, args.after, mask_noise=args.mask_noise)


if __name__ == "__main__":
    main()
