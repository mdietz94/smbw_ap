"""Brute-force search for the badge bitfield's hash key.

The M3.3 grant primitive `FUN_710049F648(gmd, value, hash)` writes
container-A counter fields keyed by 32-bit hashes.  Per save-diff
findings, the badge ownership bitfield (u64 at file offset 0x0EA0) is
NOT in the pair region — implying it's stored separately.  BUT it's
worth ruling out the cheap hypothesis first: maybe badges ARE in
container A under a hash we haven't seen yet.

This script:
  1. Computes Murmur3-32 (seed 0) — the hash function confirmed in
     FUN_71003D4110 — for ~60 candidate badge-related field name strings.
  2. Cross-references against:
     (a) The 8 MemetendoYT-confirmed keys (negative control — should
         not match)
     (b) The unknown 32-bit hashes harvested from walk_hash_writer_xrefs.py
         output (the 18+ shared keys between Container-A R/W).  If ANY
         of these match, that's likely the badge bitfield's hash key.
     (c) Hashes appearing in the user's actual save file pair region
         (via savediff --summary output) — gives us a third corroboration.

If we get a hit, granting badges becomes a one-line addition to the
M3.3 grant primitive.  If not, badges genuinely live outside container A
and we need the Ghidra `find_badge_writer_path.py` results.

Run as: python scripts/brute_badge_field_hashes.py
        [--save PATH/game_data.sav]
"""

from __future__ import annotations

import argparse
import struct
import sys
from typing import Iterable


# ===== Murmur3-32 implementation (seed 0) =====
# Verified against canonical test vectors:
#   ""               -> 0x00000000
#   "a"              -> 0x3c2569b2
#   "hello"          -> 0x248bfa47
#   "Hello, world!"  -> 0xc0363e43
#
# This matches the algorithm decompiled from FUN_71003D4110 (course-name
# hash function): c1=0xcc9e2d51, c2=0x1b873593, rotate-15, mix=0xe6546b64,
# r2=13, mul=5+0xe6546b64, finalizers 0x85ebca6b / 0xc2b2ae35.


def murmur3_32(data: bytes, seed: int = 0) -> int:
    """Compute MurmurHash3 x86_32 of `data` with `seed`."""
    c1 = 0xcc9e2d51
    c2 = 0x1b873593
    h = seed & 0xFFFFFFFF
    length = len(data)
    nblocks = length // 4

    for i in range(nblocks):
        k = struct.unpack_from("<I", data, i * 4)[0]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
        h = ((h << 13) | (h >> 19)) & 0xFFFFFFFF
        h = (h * 5 + 0xe6546b64) & 0xFFFFFFFF

    # Tail
    tail_start = nblocks * 4
    k1 = 0
    rem = length - tail_start
    if rem >= 3:
        k1 ^= data[tail_start + 2] << 16
    if rem >= 2:
        k1 ^= data[tail_start + 1] << 8
    if rem >= 1:
        k1 ^= data[tail_start + 0]
        k1 = (k1 * c1) & 0xFFFFFFFF
        k1 = ((k1 << 15) | (k1 >> 17)) & 0xFFFFFFFF
        k1 = (k1 * c2) & 0xFFFFFFFF
        h ^= k1

    # Finalize
    h ^= length
    h ^= (h >> 16)
    h = (h * 0x85ebca6b) & 0xFFFFFFFF
    h ^= (h >> 13)
    h = (h * 0xc2b2ae35) & 0xFFFFFFFF
    h ^= (h >> 16)
    return h


# ===== Self-test against canonical vectors =====
def _self_test() -> None:
    cases = [
        (b"", 0, 0x00000000),
        (b"a", 0, 0x3c2569b2),
        (b"hello", 0, 0x248bfa47),
        (b"Hello, world!", 0, 0xc0363e43),
    ]
    for data, seed, expected in cases:
        got = murmur3_32(data, seed)
        if got != expected:
            print(f"  FAIL: murmur3({data!r}, seed={seed}) "
                  f"= 0x{got:08x}, expected 0x{expected:08x}",
                  file=sys.stderr)
            sys.exit(1)
    print(f"  Murmur3-32 self-test: OK ({len(cases)} canonical vectors)")


# ===== Verified hash keys (from MemetendoYT / save-diff) =====
KNOWN_HASHES = {
    0xf4ee6827: "flower_coin",
    0x17f0bb21: "regular_coin",
    0x55815859: "GRAND_SEED_WORLD1",
    0x49abba86: "GRAND_SEED_WORLD2",
    0xb550d8d6: "GRAND_SEED_WORLD3",
    0x1dcf7f6e: "GRAND_SEED_WORLD4",
    0x0d5a3e00: "GRAND_SEED_WORLD5",
    0xd4660d2b: "GRAND_SEED_WORLD6",
    0x5d3ec9b4: "COMPLETE_GAME",
    0x89f1cc52: "INTRO_CUTSCENE_COMPLETED",
    0xdf82e9ab: "course-clear (lookup key)",
}


# ===== Unknown hashes harvested from sprint-2 walk_hash_writer_xrefs.py =====
# These appear as `mov w_, #imm` immediates at GameDataMgr xref sites
# but don't match MemetendoYT's 8 keys.  Source: docs/static-analysis-findings.md
# "Additional hash keys discovered (unknown semantics)" + the post-fix
# 32-bit-hash table.  If any matches our brute-force, we've named one.
UNKNOWN_HASHES_FROM_GHIDRA: list[tuple[int, str]] = [
    (0x9f5ead3c, "container-A reader x3 + container-B writer (HEAVY USE)"),
    (0xed18dcfe, "container-A reader x2"),
    (0x390eb960, "container-A reader"),
    (0x85da9000, "container-A reader"),
    (0xd6f631af, "container-A reader x2"),
    (0xd1f27fc9, "container-A reader"),
    (0xc1a2a9a6, "container-A reader"),
    (0xecd38c6a, "FUN_7100472BE4 accessor"),
    (0xaae9c08e, "FUN_7100472BE4 accessor"),
    (0x42ffdf00, "FUN_7100472BE4 accessor"),
    (0xa530167a, "FUN_7100221128 accessor"),
    (0xecea4196, "FUN_7100221128 accessor"),
    (0x60e24c90, "FUN_7100221128 accessor"),
    (0xcd0b87d1, "FUN_71009B4400 sub-call"),
    (0xf9f53617, "FUN_71009B4400 sub-call"),
    (0xe05b4f08, "bool getter (FUN_71003838AC)"),
    (0xacd56da2, "bool getter (FUN_71003838AC)"),
    (0x9a1e0d84, "bool getter (FUN_71003838AC) x2"),
    (0xb472b9bd, "FUN_7100124134 accessor"),
    (0x41e51faf, "FUN_7100124134 accessor"),
    (0x4f1c1277, "tail-call"),
    (0xab5acd0d, "FUN_7100533FE4 x10 (single hot key)"),
    # Discovered 2026-05-24 in FUN_7101a5d9a0 (M3.2 sprint byproduct):
    (0xed817774, "bool getter at FUN_7101a5d9a0 -> session+0x546 (touch_goal_top_result)"),
    (0xf79bcbb0, "counter reader at FUN_7101a5d9a0 -> session+0x478 (goal_id)"),
    # === Run #11 (2026-05-24) — multi-accessor hash harvest ===
    # ★★ Cross-validated u64 candidates: appear in BOTH FUN_7100124134
    # (adjacent to container-A reader) AND FUN_710049ea24 (adjacent to
    # container-A writer).  If these two are a u64-typed reader/writer
    # pair, one of these 4 hashes is plausibly the badge ownership
    # bitfield.  HIGHEST PRIORITY.
    (0x0d5de3d5, "★★ R+W cross-validated u64 candidate (124134 + 49ea24)"),
    (0x1faf41e5, "★★ R+W cross-validated u64 candidate (124134 + 49ea24)"),
    (0x9fd4fe00, "★★ R+W cross-validated u64 candidate (124134 + 49ea24)"),
    (0xe237fbc6, "★★ R+W cross-validated u64 candidate (124134 + 49ea24) — 3x calls"),
    # ★ High-bit-iter accessor FUN_7100221128 — bit_iter=55 (highest).
    # Different keyspace from 124134/49ea24 — possibly per-course
    # progress bitfields or a separate badge-style family.
    (0xd32edb2d, "★ FUN_7100221128 — 3x calls, high-bit-iter accessor"),
    (0x1e8cb632, "★ FUN_7100221128 — 2x calls"),
    (0xcde23083, "★ FUN_7100221128 — 2x calls"),
    (0x4216df00, "FUN_7100221128 — 1x call"),
    (0xff884ba0, "FUN_7100221128 — 1x call"),
    (0xf5f72b63, "FUN_7100221128 — 1x call"),
    # ★ FUN_7100124134 high-frequency keys (NOT seen in 49ea24)
    (0xa1f6cd1a, "★ FUN_7100124134 — 3x calls"),
    (0x105df820, "FUN_7100124134 — 2x calls"),
    (0x6ee76f8c, "FUN_7100124134 — 2x calls"),
    (0xe48a1168, "FUN_7100124134 — 2x calls"),
    # FUN_7100472be4 (very common standalone primitive)
    (0x638a4ca3, "FUN_7100472be4 — 5x calls (hot key)"),
    (0x14b48618, "FUN_7100472be4 — 4x calls"),
    (0x9d265434, "FUN_7100472be4 — 3x calls"),
    (0xdd6601ec, "FUN_7100472be4 — 2x calls"),
    # Other clusters
    (0x43ce533e, "FUN_71005c1a18 — 7x calls (hot key)"),
    (0x33bf655f, "FUN_71005c1a18 — 4x calls"),
    (0x435db7a3, "FUN_71005c1a18 — 3x calls"),
    (0xa287568b, "FUN_7101f290cc — 3x calls (object-getter cluster)"),
    (0x7940dc77, "FUN_7100370264+703e4 cross-validated"),
    (0xdcf45353, "FUN_7100370264+703e4 cross-validated"),
    (0xeeff353b, "FUN_71005e2528 — 2x (container-B-suspect accessor)"),
]


# ===== Candidate badge field name strings =====
# A wide net.  Two encodings each: original case + UPPER_SNAKE
# (matching the convention of GRAND_SEED_WORLD%d and INTRO_CUTSCENE_COMPLETED).
def _build_candidate_names() -> list[str]:
    bases = [
        # Direct ownership terms
        "badge", "badges", "have_badge", "have_badges",
        "owned_badge", "owned_badges", "got_badge", "got_badges",
        "unlocked_badge", "unlocked_badges",
        "badge_flags", "badge_flag", "badge_bitmap", "badge_mask",
        "badge_owned", "badge_owned_flags", "badge_owned_bitmap",
        "badge_have", "badge_have_flags", "badge_inventory",
        "badge_inv", "badge_get", "badge_unlock", "badge_unlocked",
        "badge_acquired", "BadgeAcquired",
        # Plural variants
        "badges_owned", "badges_have", "badges_unlocked", "badges_get",
        "all_badge", "all_badges",
        # Likely SMBW internal codenames (Japanese / engine vocab)
        "headgear",          # SMBW's internal Japanese term for badges
        "kazari", "kazari_flag",  # 飾り — Japanese for "decoration"
        "shoulder",          # alt internal term sometimes
        "memorial",          # "memorial badge" appears in some docs
        "wappen",            # German for "badge" (Japanese loanword used)
        "trophy", "trophies",
        # Game-design terms
        "ability", "abilities", "ability_flag", "ability_flags",
        "perk", "perks", "perk_flag", "perk_flags",
        "skill", "skill_flags",
        "gear", "gear_flags",
        "item", "items", "item_flag", "item_flags",
        "equip", "equips",
        # Save-data conventions matching the prefix style of the
        # confirmed hashes (e.g., GRAND_SEED_*, COMPLETE_*, INTRO_*)
        "have_badge_flags", "get_badge_flags",
        "BADGE_BITFIELD", "BADGE_OWNED", "BADGE_OWNED_FLAGS",
        "HAVE_BADGE_FLAGS", "HAVE_BADGE_BITMAP",
        "EQUIP_BADGE_AVAILABLE", "BADGE_AVAILABLE_FLAGS",
        # Per the MemetendoYT pattern of WORLD-suffixed keys
        "badge_world", "BADGE_LIST",
    ]
    out: list[str] = []
    seen: set[str] = set()
    for b in bases:
        for variant in (b, b.upper(), b.lower(), b.title().replace("_", "")):
            if variant not in seen:
                seen.add(variant)
                out.append(variant)
    # Add numbered variants (one slot per badge — like GRAND_SEED_WORLD1)
    for prefix in ("BADGE", "HAVE_BADGE", "OWNED_BADGE", "BADGE_FLAG"):
        for i in range(0, 64):
            v = f"{prefix}_{i}"
            if v not in seen:
                seen.add(v)
                out.append(v)
        for i in range(0, 64):
            v = f"{prefix}{i}"
            if v not in seen:
                seen.add(v)
                out.append(v)
    return out


# ===== Save-file pair-region hash extraction =====
def extract_save_hashes(path: str) -> set[int]:
    """Walk the pair region of the save file and return every 32-bit
    key found.  Useful as a third corroboration target."""
    with open(path, "rb") as f:
        buf = f.read()
    keys: set[int] = set()
    # Pair region: 0x0028..0x0BF0, 8-byte stride (4 key, 4 value)
    for off in range(0x0028, 0x0BF0 - 7, 8):
        k = struct.unpack_from("<I", buf, off)[0]
        if k != 0:
            keys.add(k)
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Brute-force badge field name hashes (Murmur3-32 seed 0)")
    parser.add_argument(
        "--save", help="Path to game_data.sav for cross-reference",
        default=None)
    args = parser.parse_args()

    print("== Murmur3-32 self-test ==")
    _self_test()

    print("")
    print("== Loading targets ==")
    ghidra_unknowns = {h: label for h, label in UNKNOWN_HASHES_FROM_GHIDRA}
    print(f"  {len(KNOWN_HASHES)} known/named hashes (negative control)")
    print(f"  {len(ghidra_unknowns)} unknown hashes from Ghidra harvest")

    save_keys: set[int] = set()
    if args.save:
        try:
            save_keys = extract_save_hashes(args.save)
            print(f"  {len(save_keys)} pair-region keys in {args.save}")
            # Subtract known so we see only candidates
            unnamed_save = save_keys - set(KNOWN_HASHES) - set(ghidra_unknowns)
            print(f"  ({len(unnamed_save)} of those are NOT in known or "
                  f"Ghidra unknown sets — those are PURE new candidates)")
        except Exception as e:
            print(f"  (skipping save read: {e})")
            save_keys = set()

    print("")
    print("== Computing Murmur3-32 of candidate names ==")
    candidates = _build_candidate_names()
    print(f"  {len(candidates)} candidate strings")

    hits: list[tuple[str, int, str]] = []  # (name, hash, where)
    for name in candidates:
        h = murmur3_32(name.encode("ascii"), seed=0)
        if h in KNOWN_HASHES:
            # Sanity check — none of our candidates should be one of the 8.
            hits.append((name, h, f"OVERLAP with known {KNOWN_HASHES[h]}"))
        elif h in ghidra_unknowns:
            hits.append((name, h, f"GHIDRA UNKNOWN: {ghidra_unknowns[h]}"))
        elif h in save_keys:
            hits.append((name, h, "SAVE PAIR KEY (unnamed)"))

    if not hits:
        print("")
        print("== Result: NO HITS ==")
        print("  No candidate badge field name reproduces any Ghidra-")
        print("  observed or save-file hash.  This means either:")
        print("    (a) Badge field name is in Japanese / encoded / not")
        print("        in our candidate list — extend the list and rerun.")
        print("    (b) Badge field name uses a DIFFERENT hash function")
        print("        (Murmur3 is confirmed for course names but field")
        print("         names may differ).")
        print("    (c) Badges genuinely aren't in container A — the live")
        print("        bitfield is stored in a separate struct that")
        print("        GameDataMgr serializes to the trailing region.")
        print("        Proceed with find_badge_writer_path.py.")
    else:
        print("")
        print(f"== Result: {len(hits)} HIT(S) ==")
        for name, h, where in hits:
            print(f"  '{name}'  ->  0x{h:08x}  [{where}]")
        print("")
        print("If a hit lands in 'GHIDRA UNKNOWN' or 'SAVE PAIR KEY',")
        print("that's a strong candidate for the badge bitfield hash.")
        print("Cross-check by reading the value at the corresponding save")
        print("pair offset and confirming it matches a known badge")
        print("ownership state (e.g., u64 with bits at 9, 34, 35, 45, 46")
        print("for the user's known badge set).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
