r"""Incremental builder for the apworld -> SMBW internal_id badge mapping.

Diffs the "badges ever equipped" bitfield at file offset 0x0c58 of
game_data.sav across multiple captures. Each newly-cleared bit = the
SMBW internal_id of the badge equipped in that capture.

Usage:
    # First, drop save folders on your Desktop with descriptive names,
    # one per badge equipped. e.g.:
    #   C:\Users\maxwe\Desktop\equip-coin_reward\
    #   C:\Users\maxwe\Desktop\equip-parachute_cap\
    #
    # Then run with a baseline + a list of (path, badge_name) pairs:
    python scripts\badge_map_builder.py

The script is configured below. Edit CAPTURES and BASELINE to point at
your save folders.

The "baseline" should be the EARLIEST save in the chain (i.e. the one
with the FEWEST bits cleared in the 0x0c58 bitfield). Each subsequent
capture should have been recorded immediately after equipping a new
(never-before-equipped) badge.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

# === EDIT ME ===
# Baseline = state BEFORE any equip swaps. Newly-cleared bits are
# measured relative to this.
BASELINE = "C:/Users/maxwe/Desktop/post-badge/0/game_data.sav"

# List of (label, save_path) pairs in the order they were captured.
# Each label is the badge name that was newly equipped between the
# prior capture and this one.
CAPTURES: list[tuple[str, str]] = [
    ("Auto Super Mushroom Badge",
     "C:/Users/maxwe/Desktop/badge_change/0/game_data.sav"),
    # Add more here as you do captures, e.g.:
    # ("Coin Reward Badge",
    #  "C:/Users/maxwe/Desktop/equip-coin_reward/0/game_data.sav"),
    # ("Parachute Cap Badge",
    #  "C:/Users/maxwe/Desktop/equip-parachute_cap/0/game_data.sav"),
    # ...
]

# Region of interest: the "badges ever equipped" bitfield at 0x0c58.
# Empirically 12 bytes (u96) — the all-1s run from 0x0c58 to 0x0c63;
# the u32 at 0x0c64 holds an unrelated value (0x0000000f) and is NOT
# part of the bitfield.
BITFIELD_OFFSET = 0x0c58
BITFIELD_BYTES = 12


def load_bitfield(path: str) -> int:
    """Read the 'badges ever equipped' bitfield as a u128 integer."""
    with open(path, "rb") as f:
        f.seek(BITFIELD_OFFSET)
        chunk = f.read(BITFIELD_BYTES)
    # Read as little-endian u128 so bit 0 = LSB of byte 0.
    val = 0
    for i, b in enumerate(chunk):
        val |= b << (i * 8)
    return val


def cleared_bits(mask: int, all_set: int) -> list[int]:
    """Return positions of bits cleared (= 0) in mask within all_set."""
    diff = (~mask) & all_set
    out = []
    pos = 0
    while diff:
        if diff & 1:
            out.append(pos)
        diff >>= 1
        pos += 1
    return out


def main() -> None:
    all_set = (1 << (BITFIELD_BYTES * 8)) - 1

    print(f"Bitfield region: file offset 0x{BITFIELD_OFFSET:04x}, "
          f"{BITFIELD_BYTES} bytes (u{BITFIELD_BYTES * 8})")
    print()

    print(f"=== Baseline: {BASELINE} ===")
    if not Path(BASELINE).exists():
        print(f"  ERROR: baseline file not found")
        sys.exit(1)
    base_mask = load_bitfield(BASELINE)
    base_cleared = cleared_bits(base_mask, all_set)
    print(f"  cleared bits in baseline: {base_cleared} "
          f"({len(base_cleared)} badges ever equipped before this run)")
    print()

    if not CAPTURES:
        print("(no captures configured — edit CAPTURES at the top of the file)")
        return

    print("=== Walking captures ===")
    mapping: dict[str, int] = {}
    prev_cleared = set(base_cleared)
    prev_label = "(baseline)"
    for label, path in CAPTURES:
        if not Path(path).exists():
            print(f"  SKIP {label}: file not found at {path}")
            continue
        mask = load_bitfield(path)
        cur_cleared = set(cleared_bits(mask, all_set))
        new_bits = cur_cleared - prev_cleared
        lost_bits = prev_cleared - cur_cleared
        if lost_bits:
            print(f"  WARNING: bits became un-cleared between {prev_label} "
                  f"and {label}: {sorted(lost_bits)}. The bitfield is supposed "
                  f"to be set-once; investigate.")
        if not new_bits:
            print(f"  {label}: no new bit cleared (already in 'ever equipped' "
                  f"set, or capture is identical to prior)")
        elif len(new_bits) == 1:
            bit = next(iter(new_bits))
            print(f"  {label}: bit {bit} newly cleared -> SMBW internal_id = {bit}")
            mapping[label] = bit
        else:
            print(f"  {label}: MULTIPLE bits newly cleared: {sorted(new_bits)}. "
                  f"Ambiguous — equipped more than one new badge between "
                  f"captures, or there's collateral state change.")
            for b in sorted(new_bits):
                mapping[f"{label} (candidate)"] = b
        prev_cleared = cur_cleared
        prev_label = label

    print()
    print("=== confirmed mapping (apworld badge name -> SMBW internal_id) ===")
    if not mapping:
        print("  (none yet)")
    else:
        for label, internal_id in mapping.items():
            print(f"    {label!r}: {internal_id},")


if __name__ == "__main__":
    main()
