"""Look up the equip-badge identity hashes in the save's second table.

If badge name-hashes appear as keys in the (key, offset) second table,
we can read off internal names for them via the string blob without
needing more captures.
"""

from __future__ import annotations

import struct
import sys

SAVE = "C:/Users/maxwe/Desktop/badge_change/0/game_data.sav"

KNOWN_HASHES = {
    0xe41b1aba: "Wall-Climb Jump",
    0xb77086e2: "Auto Super Mushroom",
    0x76219b3b: "(unknown — adjacent slot before equip slot)",
    0x7e3d1e46: "(null sentinel — repeated everywhere)",
}


def find_hash_locations(buf: bytes, h: int) -> list[int]:
    """Return file offsets where the 4-byte LE encoding of h appears."""
    needle = struct.pack("<I", h)
    locs = []
    start = 0
    while True:
        i = buf.find(needle, start)
        if i < 0:
            break
        locs.append(i)
        start = i + 1
    return locs


def get_cstring(buf: bytes, off: int, max_len: int = 256) -> str:
    end = buf.find(b"\x00", off, off + max_len)
    if end < 0:
        end = off + max_len
    try:
        return buf[off:end].decode("utf-8", errors="replace")
    except Exception:
        return repr(buf[off:end])


def main() -> None:
    with open(SAVE, "rb") as f:
        buf = f.read()

    print(f"Save size: {len(buf)} bytes")
    print(f"String blob starts at 0xbf0 = {0xbf0}.\n")

    # First: scan the second region (0x428..0xbf0) for these hashes as keys.
    print("=== Searching second region 0x428..0xbf0 for known hashes as keys ===")
    for h, name in KNOWN_HASHES.items():
        found = False
        for off in range(0x428, 0xbf0 - 7, 8):
            k = struct.unpack_from("<I", buf, off)[0]
            v = struct.unpack_from("<I", buf, off + 4)[0]
            if k == h:
                print(f"  0x{h:08x} ({name}):  found at pair @ 0x{off:04x}, value={v}")
                # If value looks like a string-blob offset, decode the string.
                if 0xbf0 <= v < len(buf):
                    s = get_cstring(buf, v)
                    print(f"      blob[0x{v:x}] = {s!r}")
                found = True
                break
        if not found:
            print(f"  0x{h:08x} ({name}):  not found as a key in second region")
    print()

    # Now: scan the WHOLE file for these hashes (as 4-byte LE values).
    print("=== Locations of each hash anywhere in the file ===")
    for h, name in KNOWN_HASHES.items():
        locs = find_hash_locations(buf, h)
        # Filter out the equip slot we already know about.
        print(f"  0x{h:08x} ({name}):  {len(locs)} occurrence(s)")
        for off in locs[:10]:
            print(f"      @ 0x{off:04x}")
    print()

    # Walk the second region and decode each (key, blob_offset) pair into
    # (key, string) to find badge name candidates.
    print("=== Walking second region — first 60 entries with their target string ===")
    for i, off in enumerate(range(0x428, 0xbf0 - 7, 8)):
        if i >= 60:
            break
        k = struct.unpack_from("<I", buf, off)[0]
        v = struct.unpack_from("<I", buf, off + 4)[0]
        if 0xbf0 <= v < len(buf):
            s = get_cstring(buf, v, 64)
            if s and any(c.isalpha() for c in s):
                print(f"  [{i:3d}] @ 0x{off:04x}  key=0x{k:08x}  blob[0x{v:x}] = {s!r}")


if __name__ == "__main__":
    main()
