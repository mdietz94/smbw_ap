"""World/course unlock state for open-world mode (2026-06).

Derived from a fresh-vs-100%-save diff (scripts/savediff.py) captured
2026-06-03.  All hashes changed 0 -> 1 in the pair-region between a
fresh SMBW v1.0.0 save (only W1 unlocked) and a 100% completion save.
These are saved GameData fields representing world-discovered and
course-visible-on-map state that the game populates as the player
progresses through the overworld.

CATEGORY SPLIT (2026-06-09).  Audited every hash against the RomFS
GameDataList.Product.100 schema (smbw_re_tmp tooling): 84 of the 86 are
**Bool**-category (container-B, gmd+8 substruct) and only 2 are
**Int**-category (container-A).  The writers are NOT interchangeable:

  - probe::grantContainerACounter silently no-ops on Bool-category
    hashes (the container-A lookup misses), so the old single-list
    dispatch never landed the 84 bools.
  - probe::grantContainerBBool null-derefs on Int-category hashes (the
    gmd+8 lookup misses).  The historical "grantContainerBBool crashed
    the drain worker on first boot" was this table's TWO Int hashes
    going through the bool writer -- not a timing problem.  (Proof: the
    six IsVanish* bools below are force-granted via grantContainerBBool
    in probe::applyOpenWorldEntry without issue.)

So the table is split by category and the wire message carries the two
lists separately ("hashes" = Int -> grantContainerACounter, "bool_hashes"
= Bool -> grantContainerBBool).

EXCLUDED from this table (AP or game owns these):
  - 6 Royal Seed hashes (AP-authoritative via SetRoyalSeedsAbsolute)
  - COMPLETE_GAME 0x5d3ec9b4 (game-owned, gates credits/ending)
  - Stat counters with non-bool values (flower_coin, play_time, etc.)

The course NODE VISIBILITY (teleport list, world-map drawing) is handled
separately by probe::applyOpenWorldEntry() (SeedTrace.cpp), which fills
the gmd+0x80 per-world records and force-grants the six
WorldMapCloudPackunVanishInfo.IsVanish* bools (0xc687fb5f 0xcff5f3d2
0x048bc39c 0x1677f038 0x95539ec5 0x7f6e8a47).  Those six also appear in
the Bool list below; the double-grant is idempotent and the switch-side
force-grant must stay regardless (it covers sessions where this message
is dropped).
"""

from __future__ import annotations

from typing import Final

# 84 Bool-category (container-B) hashes from the fresh->100% save diff
# (2026-06-03).  All written with value=1 via probe::grantContainerBBool.
# Save-pair indices included for traceability.  Excludes: 6 Royal Seeds
# (AP-owned), COMPLETE_GAME (game-owned), stat counters (pairs
# 266/269/270/271/272/287/289, non-bool values), and the 2 Int-category
# hashes (see WORLD_UNLOCK_INT_HASHES).
WORLD_UNLOCK_BOOL_HASHES: Final[tuple[int, ...]] = (
    0x3da75ac4,  # pair   0
    0x66500164,  # pair   4
    0x6324a3f0,  # pair   5
    0x884d2433,  # pair   6
    0xfecc126b,  # pair   8
    0xbc1e74fa,  # pair   9
    0x9ceeac14,  # pair  10
    0x95539ec5,  # pair  11  (IsVanish*, also force-granted switch-side)
    0xe5cdcbd6,  # pair  12
    0x0f469c6e,  # pair  13
    0x017e885e,  # pair  14
    0xe54c7dc0,  # pair  16
    0x5714d62f,  # pair  17
    0x336d9b3d,  # pair  20
    0xccd62ea8,  # pair  21
    0xdc43cd9e,  # pair  25
    0x4116be7b,  # pair  26
    0x1bc6545d,  # pair  27
    0x4e4223d7,  # pair  28
    0xdaeb15b2,  # pair  29
    0xf574d756,  # pair  30
    0x4624d954,  # pair  31
    0xea690e38,  # pair  33
    0xcff5f3d2,  # pair  34  (IsVanish*, also force-granted switch-side)
    0x52536691,  # pair  35
    0x904be1e8,  # pair  39
    0xa6e6ce4b,  # pair  41
    0xc687fb5f,  # pair  42  (IsVanish*, also force-granted switch-side)
    0xc2da9082,  # pair  43
    0xb927a134,  # pair  45
    0x1677f038,  # pair  46  (IsVanish*, also force-granted switch-side)
    0x9b046eae,  # pair  47
    0xde6f7bf2,  # pair  48
    0x7f6e8a47,  # pair  50  (IsVanish*, also force-granted switch-side)
    0x5ca69b2b,  # pair  51
    0x466dae6b,  # pair  52
    0xb89fb7c2,  # pair  54
    0x221bb0e6,  # pair  55
    0x0df2e5e9,  # pair  56
    0x11b460d1,  # pair  58
    0x85a5d6fc,  # pair  60
    0x63dac5fb,  # pair  61
    0x5acdeded,  # pair  62
    0x5fab7f7d,  # pair  64
    0x7d6c5ec2,  # pair  65
    0x9d7052ad,  # pair  66
    0x81d2bffc,  # pair  68
    0x103eebd9,  # pair  70
    0x3a2abdf6,  # pair  71
    0xc06bd61e,  # pair  73
    0x44c540f1,  # pair  77
    0x7fff318b,  # pair  78
    0xee6b32b0,  # pair  79
    0x7746394c,  # pair  80
    0xfc951a99,  # pair  81
    0xeb5da755,  # pair  82
    0xcd4ec26a,  # pair  83
    0x9e77fa3b,  # pair  84
    0x2ffa30f5,  # pair  86
    0x48cc4fee,  # pair  87
    0xbde59f10,  # pair  88
    0x27f84ee6,  # pair  89
    0xdf9a528a,  # pair  90
    0x44c77ad2,  # pair  91
    0x09735895,  # pair  92
    0x9cf94a91,  # pair  94
    0xdde8a00c,  # pair  96
    0x43318285,  # pair  97
    0xbd222b7c,  # pair  99
    0xa6ce080b,  # pair 100
    0x601ad072,  # pair 108
    0x6a7aa43e,  # pair 110
    0xf55a3b3d,  # pair 111
    0x0e5c2d3d,  # pair 113
    0x7ae70f64,  # pair 114
    0xa25422c1,  # pair 116
    0x246bc983,  # pair 118
    0x7e6ac630,  # pair 119
    0xf53c6d43,  # pair 120
    0xf54c6140,  # pair 122
    0x5ea220cb,  # pair 123
    0x048bc39c,  # pair 124  (IsVanish*, also force-granted switch-side)
    0xa45048b6,  # pair 125
    0xbf40af4f,  # pair 127
)

# 2 Int-category (container-A) hashes.  Written with value=1 via
# probe::grantContainerACounter (the only two entries the pre-split
# dispatch ever actually landed).
WORLD_UNLOCK_INT_HASHES: Final[tuple[int, ...]] = (
    0x5ac1e406,  # pair 264
    # --- transient world-reveal flag (NOT from the fresh->100% diff) ---
    # The fresh->100% diff structurally misses flags that are 0 at BOTH
    # endpoints (set during a reveal, cleared by completion).  Recovered
    # 2026-06-08 from the W3-end "unlocks W4/5/6" cutscene diff
    # ("w3 end" -> "w3-post cutscene"): the only cutscene flag not already
    # in this table.  NOTE: this flag is part of the reveal footprint but is
    # NOT what draws the PI->W4/5/6 road -- that is three container-C bitfields
    # set in probe::applyOpenWorldEntry (SeedTrace.cpp).  Kept here as genuine
    # world-reveal state.  See docs/grand-propeller-flower-reveal-re-2026-06-08.md.
    0x20fced8b,  # pair 270 (w3-end cutscene), transient W4/5/6-reveal flag
)

# Full set, Int first then Bool -- kept for traceability/tests.  Do NOT
# feed this combined tuple to a single writer; route each sub-list
# through its category's writer (see module docstring).
WORLD_UNLOCK_HASHES: Final[tuple[int, ...]] = (
    WORLD_UNLOCK_INT_HASHES + WORLD_UNLOCK_BOOL_HASHES
)
