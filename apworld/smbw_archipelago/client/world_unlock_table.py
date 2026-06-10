"""World/course unlock state for open-world mode (2026-06).

Originally derived from a fresh-vs-100%-save diff (scripts/savediff.py,
2026-06-03): every pair-region hash that changed 0 -> 1 between a fresh
SMBW v1.0.0 save and a 100% completion save.

CATEGORY SPLIT (2026-06-09).  Audited every hash against the RomFS
GameDataList.Product.100 schema (smbw_re_tmp tooling): all but 2 are
**Bool**-category (container-B, gmd+8 substruct); the 2 are
**Int**-category (container-A).  The writers are NOT interchangeable:

  - probe::grantContainerACounter silently no-ops on Bool-category
    hashes (the container-A lookup misses), so the old single-list
    dispatch never landed the bools.
  - probe::grantContainerBBool null-derefs on Int-category hashes (the
    gmd+8 lookup misses).  The historical "grantContainerBBool crashed
    the drain worker on first boot" was this table's Int hashes going
    through the bool writer -- not a timing problem.

So the table is split by category and the wire message carries the two
lists separately ("hashes" = Int -> grantContainerACounter, "bool_hashes"
= Bool -> grantContainerBBool).

CURATED ALLOWLIST (2026-06-09, same day).  When the 84 bools finally
LANDED for the first time (the category split fixed the silent no-op),
closed worlds started showing unlocked courses -- e.g. W6 levels in a
seed where W6 was not open.  Naming the hashes (murmur3 brute-match of
NSO+RomFS identifier strings against the GameDataList schema) showed the
fresh->100% diff mixes three very different kinds of state:

  1. World-map obstacle/unlock state we DO want (the Bowser approach).
  2. Per-world progress structs W_{Savanna,Yama,Wa,Sabaku,Kin,Nettai,
     Naka,Castle,Himitu} -- members like IsEndFirstVisitDemo /
     IsOpenWorldName / IsGetMotherSeed.  Setting these reveals a world's
     course nodes EVEN FOR WORLDS CLOSED IN THE SEED (the W6 bug), and
     marks per-world story beats done.
  3. Completion records: *_FINISH_A break-time events, Motherseed0xFinish,
     CaptainKinopio_*_Finish, WorldMapKinKinNPC*_1UP ("1UP already
     taken"), IsDoneStaffCredits, etc.  In a randomizer these HIDE live
     content (events/rewards present as already-done).

Only class 1 (plus harmless one-time-dialog suppressors) is sent now; the
full drop list is preserved below for traceability.  Open worlds get
their course nodes from probe::applyOpenWorldEntry's gmd+0x80 per-world
record fill, NOT from the per-world W_* flags, so dropping class 2 does
not regress open-world reachability (it was the never-landing of these
bools that kept pre-split builds working at all).

EXCLUDED from this table (AP or game owns these):
  - 6 Royal Seed hashes (AP-authoritative via SetRoyalSeedsAbsolute)
  - COMPLETE_GAME 0x5d3ec9b4 (game-owned, gates credits/ending)
  - Stat counters with non-bool values (flower_coin, play_time, etc.)

The six WorldMapCloudPackunVanishInfo.IsVanish* bools and
WorldMapKoopaCastleEntranceDemoInfo.IsAppear are ALSO force-granted
switch-side in probe::applyOpenWorldEntry (SeedTrace.cpp); the
double-grant is idempotent and the switch-side grant must stay (it
covers sessions where this message is dropped).
"""

from __future__ import annotations

from typing import Final

# Bool-category (container-B) hashes, written value=1 via
# probe::grantContainerBBool.  Curated allowlist -- every entry named via
# the GameDataList schema + murmur3(name) (see module docstring).
WORLD_UNLOCK_BOOL_HASHES: Final[tuple[int, ...]] = (
    # --- the Bowser approach (cloud piranhas + castle entry) ---
    0x95539ec5,  # pair  11  WorldMapCloudPackunVanishInfo.IsVanishKin
    0xcff5f3d2,  # pair  34  WorldMapCloudPackunVanishInfo.IsVanishYama
    0xc687fb5f,  # pair  42  WorldMapCloudPackunVanishInfo.IsVanishSavanna
    0x1677f038,  # pair  46  WorldMapCloudPackunVanishInfo.IsVanishSabaku
    0x7f6e8a47,  # pair  50  WorldMapCloudPackunVanishInfo.IsVanishNettai
    0x048bc39c,  # pair 124  WorldMapCloudPackunVanishInfo.IsVanishWa
    0xc06bd61e,  # pair  73  WorldMapKoopaCastleEntranceDemoInfo.IsAppear
    0x3a2abdf6,  # pair  71  WorldMapKoopaCastleBurnerFlag
    # --- one-time story-demo "already played" flags (no checks behind) ---
    0xccd62ea8,  # pair  21  AfterCourseClearDemoInfo.FirstStageDemoFlag
    0xb927a134,  # pair  45  AfterCourseClearDemoInfo.FlowerWorldLastStageDemoFlag
    0x5ea220cb,  # pair 123  AfterCourseClearDemoInfo.HakkunStageDemoFlag
    # --- one-time dialog suppressors ---
    0xbde59f10,  # pair  88  IsEndDisplayCanUnlockFirstRaceMsg
    0x7e6ac630,  # pair 119  IsEndDisplayDialogAppearUltraChampionsShip
    # --- world-map gates ---
    0xa25422c1,  # pair 116  IsOpenSabakuFence (W4 desert fence)
)

# 2 Int-category (container-A) hashes.  Written with value=1 via
# probe::grantContainerACounter (the only two entries the pre-split
# dispatch ever actually landed).
WORLD_UNLOCK_INT_HASHES: Final[tuple[int, ...]] = (
    0x5ac1e406,  # pair 264
    # Recovered 2026-06-08 from the W3-end cutscene diff (absent from the
    # fresh->100% diff because it is 0 at both endpoints).
    0x20fced8b,  # pair 270  WorldMapKoopaCastleEmissionLv (castle glow level)
)

# Full sent set, Int first then Bool -- kept for traceability/tests.  Do
# NOT feed this combined tuple to a single writer; route each sub-list
# through its category's writer (see module docstring).
WORLD_UNLOCK_HASHES: Final[tuple[int, ...]] = (
    WORLD_UNLOCK_INT_HASHES + WORLD_UNLOCK_BOOL_HASHES
)

# ---------------------------------------------------------------------------
# DROPPED from the original fresh->100% diff (2026-06-09) -- NOT sent.
# Per-world progress (reveals closed worlds' courses -- the "W6 levels
# unlocked" bug) and completion records (hide live events/rewards in a
# randomizer).  Kept here, named where resolved, so nobody re-adds them
# wholesale from the old diff.  W_* member flags include
# IsEndFirstVisitDemo / IsOpenWorldName / IsGetMotherSeed per world.
WORLD_UNLOCK_DROPPED_HASHES: Final[tuple[int, ...]] = (
    0x3da75ac4,  # pair   0  W_Naka.<member>
    0x66500164,  # pair   4  TOGETOGE_FINISH_A
    0x6324a3f0,  # pair   5  YORIDORIMIDORI_FINISH_A
    0x884d2433,  # pair   6  W_Castle.<member>
    0xfecc126b,  # pair   8  W_Nettai.<member>
    0xbc1e74fa,  # pair   9  Motherseed03Finish
    0x9ceeac14,  # pair  10  W_Yama.<member>
    0xe5cdcbd6,  # pair  12  W_Naka.<member>
    0x0f469c6e,  # pair  13  W_Sabaku.<member>
    0x017e885e,  # pair  14  W_Naka.<member>
    0xe54c7dc0,  # pair  16  WorldMapKinKinNPC4_1UP
    0x5714d62f,  # pair  17  W_Wa.<member>
    0x336d9b3d,  # pair  20  W_Wa.<member>
    0xdc43cd9e,  # pair  25  Motherseed04Finish
    0x4116be7b,  # pair  26  Motherseed05Finish
    0x1bc6545d,  # pair  27  W_Nettai.<member>
    0x4e4223d7,  # pair  28  W_Wa.<member>
    0xdaeb15b2,  # pair  29  W_Yama.<member>
    0xf574d756,  # pair  30  Motherseed01Finish
    0x4624d954,  # pair  31  W_Himitu.<member>
    0xea690e38,  # pair  33  MIDTEST_FINISH_A
    0x52536691,  # pair  35  W_Kin.<member>
    0x904be1e8,  # pair  39  HOSHIZORA_FINISH_A
    0xa6e6ce4b,  # pair  41  W_Kin.<member>
    0xc2da9082,  # pair  43  (unnamed, not in any GameData struct)
    0x9b046eae,  # pair  47  W_Kin.<member>
    0xde6f7bf2,  # pair  48  W_Nettai.<member>
    0x5ca69b2b,  # pair  51  W_Wa.<member>
    0x466dae6b,  # pair  52  Motherseed02Finish
    0xb89fb7c2,  # pair  54  CaptainKinopio_Shoppai_Finish
    0x221bb0e6,  # pair  55  IsDoneStaffCredits
    0x0df2e5e9,  # pair  56  W_Himitu.<member>
    0x11b460d1,  # pair  58  HIDEN_FINISH_A
    0x85a5d6fc,  # pair  60  W_Himitu.<member>
    0x63dac5fb,  # pair  61  W_Wa.<member>
    0x5acdeded,  # pair  62  W_Yama.<member>
    0x5fab7f7d,  # pair  64  W_Yama.<member>
    0x7d6c5ec2,  # pair  65  W_Nettai.<member>
    0x9d7052ad,  # pair  66  W_Nettai.<member>
    0x81d2bffc,  # pair  68  W_Castle.<member>
    0x103eebd9,  # pair  70  RadioTowerFirstEncount
    0x44c540f1,  # pair  77  W_Castle.<member>
    0x7fff318b,  # pair  78  W_Himitu.<member>
    0xee6b32b0,  # pair  79  W_Naka.<member>
    0x7746394c,  # pair  80  AIMY_FINISH_A
    0xfc951a99,  # pair  81  W_Savanna.<member>
    0xeb5da755,  # pair  82  W_Yama.<member>
    0xcd4ec26a,  # pair  83  Masterhanapio_clear
    0x9e77fa3b,  # pair  84  W_Sabaku.<member>
    0x2ffa30f5,  # pair  86  W_Wa.<member>
    0x48cc4fee,  # pair  87  CaptainKinopio_Mannakai_Finish
    0x27f84ee6,  # pair  89  W_Sabaku.<member>
    0xdf9a528a,  # pair  90  W_Savanna.<member>
    0x44c77ad2,  # pair  91  W_Kin.<member>
    0x09735895,  # pair  92  CaptainKinopio_KinKin_Finish
    0x9cf94a91,  # pair  94  W_Yama.<member>
    0xdde8a00c,  # pair  96  Motherseed00Finish
    0x43318285,  # pair  97  W_Savanna.<member>
    0xbd222b7c,  # pair  99  W_Kin.<member>
    0xa6ce080b,  # pair 100  W_Sabaku.<member>
    0x601ad072,  # pair 108  W_Naka.<member>
    0x6a7aa43e,  # pair 110  W_Nettai.<member>
    0xf55a3b3d,  # pair 111  Ogon_flowerroad
    0x0e5c2d3d,  # pair 113  W_Castle.<member>
    0x7ae70f64,  # pair 114  W_Savanna.IsEndFirstVisitDemo
    0x246bc983,  # pair 118  WorldMapKinKinNPC1_1UP
    0xf53c6d43,  # pair 120  W_Sabaku.<member>
    0xf54c6140,  # pair 122  WorldMapKinKinNPC2_1UP
    0xa45048b6,  # pair 125  W_Kin.<member>
    0xbf40af4f,  # pair 127  W_Sabaku.<member>
)
