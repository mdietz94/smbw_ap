# -*- coding: utf-8 -*-
# Host-side Python (NOT a Ghidra script — runs under stock CPython).
#
# SMBW Archipelago — Wonder Seed RE re-open (2026-05-28).
#
# Murmur3-32 brute force over plausible internal field names for the
# unknown Wonder Seed-related hash keys.  Mirrors the structure of
# scripts/ghidra/brute_badge_field_hashes.py (which targeted the badge
# hashes).  Badge brute force found ZERO hits, confirming Nintendo uses
# pre-computed or non-MurmurHash3 names for badges; this run repeats the
# experiment for seed hashes and ALSO tries a few common alternative
# hash functions (FNV-1a, CRC-32) so we don't relitigate Murmur3-only
# coverage.
#
# Hashes to crack:
#
#   0x60458608  per-course Wonder Seed bitfield (container D)
#   0x580b7eb4  adjacent per-course course-state bitfield
#   0x21f89ab1  WS mirror hash #1 (in-course HUD)
#   0x8c20ccb7  WS mirror hash #2 (BISECT-C safe)
#   0xeeff353b  WS mirror hash #3 (animation source)
#   0x390eb960  WS gate-predicate hash
#   0xa0e5f253  WS mirror hash #5 (animation target)
#   0x9f5ead3c  current player world index
#   0xed817774  session+0x546 touch_goal_top_result
#   0xf79bcbb0  session+0x478 goal_id (last goal reached)
#
# Even if NONE crack, the negative result is still useful — it tells us
# the names are not MurmurHash3 of any of these dictionary words (which
# rules out a class of approaches and pushes us toward static decompile
# of FUN_71003D4110 / FUN_71003D3FB0 to extract the name strings
# directly from the binary).
#
# Run:  python scripts/ghidra/brute_seed_field_hashes.py

from __future__ import print_function

import itertools
import struct
import sys
import zlib


TARGETS = {
    0x60458608: "per-course Wonder Seed bitfield",
    0x580b7eb4: "adjacent per-course course-state bitfield",
    0x21f89ab1: "WS mirror hash #1 (in-course HUD)",
    0x8c20ccb7: "WS mirror hash #2 (BISECT-C safe)",
    0xeeff353b: "WS mirror hash #3 (animation source)",
    0x390eb960: "WS gate-predicate hash",
    0xa0e5f253: "WS mirror hash #5 (animation target)",
    0x9f5ead3c: "current player world index",
    0xed817774: "session+0x546 touch_goal_top_result",
    0xf79bcbb0: "session+0x478 goal_id (last goal reached)",
}


# --- Word lists -----------------------------------------------------------

# Wonder-Seed-specific English seeds.
SEED_PREFIXES = [
    "", "wonder", "Wonder", "WONDER", "seed", "Seed", "SEED",
    "wonder_seed", "WonderSeed", "wonderSeed", "wonderseed",
    "wonder_seed_get", "wonder_seed_owned", "wonder_seed_collected",
    "wonder_seed_acquired", "wonder_seed_bit", "wonder_seed_bitfield",
    "WonderSeed_get", "WonderSeed_owned", "WonderSeedFlags",
    "wonder_seed_flag", "wonder_flag", "wonder_flags",
    "miracle", "miracle_seed", "miracleSeed", "MiracleSeed",
    "wonder_count", "wonder_seed_count", "WonderCount",
    "wonder_total", "wonder_seed_total",
]

# Japanese-romaji and SMBW-internal codename candidates.
JP_PREFIXES = [
    "fushigi", "fushigi_seed", "fushigiSeed", "fushigi_no_tane",
    "fushigi_tane", "fushiginotane", "fushigi_no_mi", "fushigi_mi",
    "tane", "seed_tane", "no_tane", "nin_tane",
    "kakera",  # "fragment" — Royal Seed used "kakera" internally
    "wandar", "wonderlandSeed", "secred", "Secred",  # internal name
    "secred_seed", "secredSeed",
]

# Per-course / course-state seeds.
COURSE_PREFIXES = [
    "course", "Course", "COURSE", "stage", "Stage", "STAGE",
    "course_clear", "CourseClear", "courseClear",
    "course_seed", "CourseSeed", "course_wonder",
    "stage_clear", "stage_seed", "stage_wonder",
    "level", "Level", "level_clear",
    "course_state", "CourseState", "course_status", "CourseStatus",
    "course_progress", "CourseProgress",
    "course_flag", "CourseFlag", "course_flags",
    "course_bit", "CourseBit", "course_bitfield", "courseBitfield",
    "goal", "Goal", "goal_id",
    "goal_top", "GoalTop", "goal_top_result", "GoalTopResult",
    "goal_reach", "GoalReach", "goal_count",
    "touch_goal", "touchGoal", "touch_goal_top",
    "flagpole", "Flagpole", "flag_pole",
    "exit", "Exit", "exit_type",
]

# World seeds (for gate/mirror hashes).
WORLD_PREFIXES = [
    "world", "World", "WORLD",
    "world_index", "world_no", "world_id", "WorldId",
    "current_world", "currentWorld", "CurrentWorld",
    "player_world", "playerWorld",
    "active_world", "activeWorld",
    "world_seed_count", "worldSeedCount",
    "world_wonder_count", "world_wonder_seed_count",
    "world_clear_count",
    "kingdom", "Kingdom",  # nintendo loves this term
]

# Common suffixes.
SUFFIXES = [
    "",
    "Count", "_count", "_total", "Total",
    "Flag", "_flag", "Flags", "_flags",
    "Bit", "_bit", "Bits", "_bits", "Bitfield", "_bitfield",
    "Mask", "_mask",
    "Array", "_array",
    "Id", "_id", "Index", "_index", "Idx", "_idx",
    "Value", "_value", "Val", "_val",
    "State", "_state", "Status", "_status",
    "Owned", "_owned", "Get", "Set",
    "Save", "_save", "Data", "_data",
    "Progress", "_progress",
    "Result", "_result",
]


# --- Hash functions -------------------------------------------------------

def murmur3_32(key, seed=0):
    """Murmur3 32-bit hash, x86 variant.  Matches Nintendo's documented
    Murmur3-32 usage for the course-name -> index lookup (FUN_71003D4110)."""
    if not isinstance(key, (bytes, bytearray)):
        key = key.encode("utf-8")
    length = len(key)
    h1 = seed & 0xFFFFFFFF
    c1 = 0xCC9E2D51
    c2 = 0x1B873593
    rounded = (length // 4) * 4
    for i in range(0, rounded, 4):
        k1 = struct.unpack("<I", key[i:i + 4])[0]
        k1 = (k1 * c1) & 0xFFFFFFFF
        k1 = ((k1 << 15) | (k1 >> 17)) & 0xFFFFFFFF
        k1 = (k1 * c2) & 0xFFFFFFFF
        h1 ^= k1
        h1 = ((h1 << 13) | (h1 >> 19)) & 0xFFFFFFFF
        h1 = (h1 * 5 + 0xE6546B64) & 0xFFFFFFFF
    tail_index = rounded
    k1 = 0
    tail_size = length & 3
    if tail_size >= 3:
        k1 ^= key[tail_index + 2] << 16
    if tail_size >= 2:
        k1 ^= key[tail_index + 1] << 8
    if tail_size >= 1:
        k1 ^= key[tail_index]
        k1 = (k1 * c1) & 0xFFFFFFFF
        k1 = ((k1 << 15) | (k1 >> 17)) & 0xFFFFFFFF
        k1 = (k1 * c2) & 0xFFFFFFFF
        h1 ^= k1
    h1 ^= length
    h1 ^= h1 >> 16
    h1 = (h1 * 0x85EBCA6B) & 0xFFFFFFFF
    h1 ^= h1 >> 13
    h1 = (h1 * 0xC2B2AE35) & 0xFFFFFFFF
    h1 ^= h1 >> 16
    return h1


def fnv1a_32(key):
    if not isinstance(key, (bytes, bytearray)):
        key = key.encode("utf-8")
    h = 0x811C9DC5
    for b in key:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def crc32(key):
    if not isinstance(key, (bytes, bytearray)):
        key = key.encode("utf-8")
    return zlib.crc32(key) & 0xFFFFFFFF


HASH_FUNCS = [
    ("murmur3_seed=0", lambda k: murmur3_32(k, 0)),
    ("murmur3_seed=1", lambda k: murmur3_32(k, 1)),
    ("murmur3_seed=0xdeadbeef", lambda k: murmur3_32(k, 0xDEADBEEF)),
    ("fnv1a_32", fnv1a_32),
    ("crc32", crc32),
]


def _iter_candidates():
    bases = []
    bases.extend(SEED_PREFIXES)
    bases.extend(JP_PREFIXES)
    bases.extend(COURSE_PREFIXES)
    bases.extend(WORLD_PREFIXES)
    for base in bases:
        for suf in SUFFIXES:
            cand = base + suf
            if cand:
                yield cand
    # Cross-products of common compound names.
    for prefix, mid in itertools.product(
            ["wonder", "miracle", "fushigi"],
            ["seed", "tane", "kakera"]):
        for suf in SUFFIXES:
            yield "%s_%s%s" % (prefix, mid, suf)
            yield "%s%s%s" % (prefix.capitalize(), mid.capitalize(), suf)
    # Per-world enumerations (mirror hashes might be name + world_id).
    for world in range(1, 10):
        for base in ("wonder_seed", "WonderSeed", "wonder_seed_count"):
            yield "%s_world%d" % (base, world)
            yield "%s_W%d" % (base, world)
            yield "%s%d" % (base, world)


def main():
    target_set = set(TARGETS.keys())
    hits = []
    n = 0
    for cand in _iter_candidates():
        n += 1
        for hname, hfn in HASH_FUNCS:
            try:
                h = hfn(cand)
            except Exception:
                continue
            if h in target_set:
                hits.append((cand, hname, h, TARGETS[h]))
                print("MATCH  %-40s  via %-20s  -> 0x%08x  (%s)"
                      % (cand, hname, h, TARGETS[h]))
    print("")
    print("Tested %d candidates over %d hash functions." % (n, len(HASH_FUNCS)))
    print("Total hits: %d" % len(hits))
    if hits:
        print("")
        print("Confirmed field names:")
        for cand, hname, h, role in hits:
            print("  %-40s  %-25s  0x%08x  (%s)"
                  % (cand, hname, h, role))
    else:
        print("")
        print("Zero hits.  Conclusions:")
        print("  - The seed field names are NOT in any dictionary candidate")
        print("    tested here under MurmurHash3/FNV-1a/CRC-32.")
        print("  - Either the names use Japanese codepoints we didn't try,")
        print("    or the hash function is different (suspect: Nintendo's")
        print("    internal `n3ds`-era pre-computed table, in which case")
        print("    the names exist nowhere in the binary).")
        print("  - Next: decompile FUN_71003D4110 (course-name hash lookup)")
        print("    to extract the 81 hardcoded course strings; these are")
        print("    the closest available 'real names' the game uses.")


if __name__ == "__main__":
    main()
