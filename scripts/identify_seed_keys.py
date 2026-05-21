#!/usr/bin/env python3
"""Offline analysis tool for M3.3.

We captured a set of (key, value) pairs from the wonder-seed-counter
getter at runtime.  The keys are 32-bit hashes of internal stat names.
If we can identify the hash function the game uses, we can enumerate
the names → keys directly (without more in-game probes).

Strategy: take the known-meaning anchor "flower_coin"-family stat
(returned value 148 matches PlayReport `flower_coin_course_out: 148`)
and try common hash functions on candidate name strings against the
observed key.  If any function matches, that's the function and we
can hash any other candidate to find its key.
"""

from __future__ import annotations

import zlib


# Observed (key, value) pairs from the in-game probe.  Values cross-
# referenced against the M2.4 PlayReport corpus where possible.
KEY_TO_VALUE: dict[int, int] = {
    0xf4ee6827: 148,        # likely flower_coin (matches PlayReport)
    0x17f0bb21: 26,         # likely total_play_time_sec (W1-1 had 26s)
    0x74f3a647: 1,
    0xf4d9942a: 2,
    0xe37debfe: 0,
    0x9f5ead3c: 2,
    0xd6b9ce86: 2,
    0x390eb960: 0,
    0x3db2938d: 0,
    0xf0d15a94: -1,
    0xd6f631af: None,       # garbage values — invalid key for this table
}


# Stat-name candidates harvested from the M2.4 PlayReport corpus
# (test_play_report.py field names) plus likely wonder-seed variants.
CANDIDATES = [
    # PlayReport top-level fields we've seen
    "savedata_id", "play_mode", "scene_type", "system_report_tag",
    "total_play_time", "total_play_time_sec", "current_play_time_sec",
    "stage_key", "world_no", "course_no", "world_kind",
    "course_in_utc", "course_result", "hana_race_result", "goal_id",
    "remote_encounter_count", "ghost_encounter_count",
    "get_yellow_coin", "get_lucky_coin",
    "yellow_coin_course_in", "yellow_coin_course_out",
    "flower_coin_course_in", "flower_coin_course_out",
    "big_flower_coin_course_in", "big_flower_coin_course_out",
    "arena_score_enter", "arena_score_result",
    "touch_goal_top_enter", "touch_goal_top_result",
    "new_flower_count", "get_flower_count",
    "world_wonder_flower", "world_mother_seed",
    "last_put_panel_id", "start_mmp", "result_mmp",
    "friend_race_member", "friend_race_result",
    "room_member_enter", "room_member_max", "last_ctrl_by_stcik",
    "challenge_count", "total_wonder_count", "max_wonder_count",
    "total_get_finish_seed_count", "net_mode", "badge_id_array",
    "player_rest_course_in", "player_rest_course_out",
    "total_1up_count", "local_player_num",
    "ctrl_style_array", "chara_type_array",
    "self_shabon_count_array", "miss_shabon_count_array",
    "dead_count_array", "direct_dead_count_array",
    "equip_badge_id", "recom_badge_id",
    "recom_badge_result", "pre_recom_badge_id",
    "wonder_coin", "wonder_seed", "lucky_coin", "yellow_coin",
    "flower_coin", "dash_ctrl_type", "squat_ctrl_type",
    "swing_ctrl_type", "telop_disp_type", "flower_setting_type",
    "flower_voice_region", "hdvibration_level_type",
    "option_badge_select", "disp_lang",
    # PlayReport nested struct field names
    "rescue", "item_bln", "emote", "ctrl_guide", "online_guide",
    "net_conn", "get_medal_array",
    # Wonder-seed-specific candidates
    "wonder_seed_count", "wonder_seed_total",
    "WonderSeedCount", "WonderSeedTotal",
    "PossessedWonderSeed", "PossessedWonderSeedCount",
    "wonder_seed_collected", "wonder_seed_collected_count",
    "GlobalWonderSeed", "WonderSeed",
    # Save data field names that might be present
    "savedata", "save_data", "save",
    "BootupTimeUs", "Region", "Language",
    # Things we suspected might be in the hash table
    "GameOption", "WorldOption", "PlayerOption",
]


def fnv1a_32(s: str) -> int:
    h = 0x811C9DC5
    for b in s.encode("ascii"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def fnv1_32(s: str) -> int:
    h = 0x811C9DC5
    for b in s.encode("ascii"):
        h = (h * 0x01000193) & 0xFFFFFFFF
        h ^= b
    return h


def djb2(s: str) -> int:
    h = 5381
    for b in s.encode("ascii"):
        h = (((h << 5) + h) + b) & 0xFFFFFFFF
    return h


def sdbm(s: str) -> int:
    h = 0
    for b in s.encode("ascii"):
        h = (b + (h << 6) + (h << 16) - h) & 0xFFFFFFFF
    return h


def crc32(s: str) -> int:
    return zlib.crc32(s.encode("ascii"))


# Nintendo's nn::util::HashFunctions::CalcStringHash32 is documented
# in some places as either CRC32 or a specific FNV variant; both
# already in our list.

HASHES = {
    "CRC32":          crc32,
    "FNV-1a-32":      fnv1a_32,
    "FNV-1-32":       fnv1_32,
    "DJB2":           djb2,
    "SDBM":           sdbm,
}


def main() -> None:
    observed_keys = set(KEY_TO_VALUE.keys())
    print(f"Trying {len(CANDIDATES)} candidate strings against "
          f"{len(observed_keys)} observed keys using {len(HASHES)} hash functions.\n")
    any_match = False
    for hname, hfn in HASHES.items():
        matches = []
        for cand in CANDIDATES:
            h = hfn(cand)
            if h in observed_keys:
                matches.append((cand, h, KEY_TO_VALUE[h]))
        if matches:
            any_match = True
            print(f"=== {hname} matches ===")
            for cand, h, val in matches:
                print(f"  {cand!r:40s} -> 0x{h:08x}  (observed value: {val})")
            print()
    if not any_match:
        print("No matches — the game uses a hash function we haven't tried,")
        print("or the candidate strings don't include the right names.")
        print()
        print("Showing what each function produces for a few key candidates")
        print("so we can eyeball-spot patterns:")
        for hname, hfn in HASHES.items():
            print(f"  {hname}:")
            for cand in ["world_wonder_flower", "flower_coin", "lucky_coin",
                         "wonder_seed", "wonder_coin"]:
                print(f"    {cand!r:30s} -> 0x{hfn(cand):08x}")


if __name__ == "__main__":
    main()
