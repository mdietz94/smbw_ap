"""One-off analysis of the 2026-05-22 badge capture.

Cross-references the user's reported state (5 lives, 26 coins, 16 wonder
seeds, 1 royal seed, flower coins -30) against the save buffer to
identify hash keys in the second counter table at file offsets
0x428..0xbf0.
"""

from __future__ import annotations

import struct
import sys

PRE = "C:/Users/maxwe/Desktop/pre-badge/0/game_data.sav"
POST = "C:/Users/maxwe/Desktop/post-badge/0/game_data.sav"

# Region to scan as (u32 key, u32 value) pairs.
REGION_START = 0x428
REGION_END = 0xbf0

# Known keys from prior analysis.
KNOWN_KEYS = {
    0xf4ee6827: "flower_coin",      # M3.3 probe value 148 == this save's
    0x17f0bb21: "regular_coin?",    # M3.3 thought play_time but user has 26 coins
}

# Target values to hunt for.
TARGETS_PRE = {
    "5 lives": 5,
    "26 coins": 26,
    "16 wonder seeds": 16,
    "1 royal seed": 1,
    "flower coins (pre)": 148,
}


def pairs(buf: bytes, start: int, end: int):
    for off in range(start, end - 7, 8):
        k, v = struct.unpack_from("<II", buf, off)
        yield off, k, v


def main() -> None:
    with open(PRE, "rb") as f:
        pre = f.read()
    with open(POST, "rb") as f:
        post = f.read()

    pair_count = (REGION_END - REGION_START) // 8
    print(f"Scanning region 0x{REGION_START:04x}..0x{REGION_END:04x} "
          f"({pair_count} pairs of (u32 key, u32 value)).")
    print()

    # --- Pass 1: changes in this region ---
    print("=== changed pairs in region 0x428..0xbf0 ===")
    any_change = False
    for (off_a, k_a, v_a), (off_b, k_b, v_b) in zip(
            pairs(pre, REGION_START, REGION_END),
            pairs(post, REGION_START, REGION_END)):
        if k_a != k_b or v_a != v_b:
            any_change = True
            label = KNOWN_KEYS.get(k_a, "?")
            print(f"  @ 0x{off_a:04x}  key=0x{k_a:08x} ({label})  "
                  f"{v_a} -> {v_b}  (delta {v_b - v_a:+d})")
    if not any_change:
        print("  (none)")
    print()

    # --- Pass 2: value-to-key lookup for user-reported state ---
    print("=== candidate keys by user-reported pre-acquire values ===")
    by_value: dict[int, list[tuple[int, int]]] = {}
    for off, k, v in pairs(pre, REGION_START, REGION_END):
        by_value.setdefault(v, []).append((off, k))

    for label, val in TARGETS_PRE.items():
        hits = by_value.get(val, [])
        # Filter near-noise: 0 and -1 values are too generic.
        if val in (0, 0xffffffff):
            print(f"  {label} (value={val}): skipping (too common)")
            continue
        print(f"  {label} (value={val}):  {len(hits)} candidates")
        for off, k in hits[:20]:
            known = KNOWN_KEYS.get(k, "")
            tag = f" [{known}]" if known else ""
            print(f"      @ 0x{off:04x}  key=0x{k:08x}{tag}")
    print()

    # --- Pass 3: sentinel-aware boundary check ---
    # Look at pair indices 128, 129, ... to recheck where (key, value)
    # interpretation breaks down.
    print("=== first 8 pairs at pair_index 128.. (post header re-check) ===")
    base = 0x28
    for i in range(128, 138):
        off = base + i * 8
        k, v = struct.unpack_from("<II", pre, off)
        print(f"  pair {i}  @ 0x{off:04x}  key=0x{k:08x}  value={v} (0x{v:x})")


if __name__ == "__main__":
    main()
