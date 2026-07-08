"""Generate the character-block sanity tables (offline RomFS datamining).

Inputs (offline, no Ghidra / no runtime):

  * ``charblock_table.json`` -- the 166 ``ObjectBlockClarityCharacter``
    instances extracted from the RomFS BancMapUnit files (generator:
    ``smbw_re_tmp/charblock_table.py``).  Fields per instance:
    ``file`` (e.g. ``Course005_Main``), ``hash`` (banc u32), ``chara``
    (PlayerCharaType 0-11), ``contents``, ``pos`` [x, y, z].

Crosswalk (the key insight that ties the offline banc data to the
client's runtime course identity):

    stage_key = murmur3_x86_32(
        "Work/Stage/StageParam/<CourseNNN>_Course.game__stage__StageParam.gyml",
        seed=0)

This is the SAME u32 the game ships in every PlayReport ``stage_info``
(verified against location_table's Course001 -> 0xAF11F7FC and
Course002 -> 0x0DD67B0B).  So a banc ``file`` like ``Course005_Main``
strips to ``Course005`` and hashes to the runtime stage_key directly --
no WorldMapInfo lookup needed, though WorldMapInfo00N.json corroborates
which CourseNNN exist.

Outputs (both checked in -- treated like the other generated tables):

  * ``apworld/smbw_archipelago/client/char_block_table.json`` -- the
    client lookup table: a list of {stage_key, chara, name} rows, one per
    (course, charaType) check.  Loaded zip-safe via pkgutil.get_data.

  * ``apworld/smbw_archipelago/data/_character_block_locations.json`` --
    the apworld location rows (name/region/category/requires) to splice
    into ``locations.json``.  This script does NOT mutate locations.json
    directly; it emits the rows and a small splice helper runs separately
    (see ``--splice``).

v1 collapse: (course, charaType) is unique everywhere EXCEPT Course450
(14 blocks / 5 distinct chars), Course451, Course453, Course850.  For v1
we emit ONE check per (course, charaType) -- collapsing those few
multi-block-same-char courses to a single location.  This drops 166
instances to 154 locations.  Position is retained per-row for a future
multi-block disambiguation pass (the 4 collapsed courses).

Run:  python scripts/romfs/build_charblock_table.py
      python scripts/romfs/build_charblock_table.py --splice   # also rewrites locations.json
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

try:
    import mmh3  # type: ignore
except ImportError:
    mmh3 = None


# PlayerCharaType 0-11 -> AP character name.  Roster order, confirmed
# against the live ``chara_type_array`` PlayReport field (course_in /
# course_result both carry it; a PALACE_COURSE_RESULT fixture decoded
# chara_type 6).  The 12 playable characters in roster order:
CHARA_NAMES: list[str] = [
    "Mario",          # 0
    "Luigi",          # 1
    "Peach",          # 2
    "Daisy",          # 3
    "Yellow Toad",    # 4
    "Blue Toad",      # 5
    "Toadette",       # 6
    "Yoshi",          # 7
    "Red Yoshi",      # 8
    "Yellow Yoshi",   # 9
    "Light-Blue Yoshi",  # 10
    "Nabbit",         # 11
]
# NOTE: PROVISIONAL roster-order mapping.  The enum->name assignment is
# the working hypothesis from the task RE notes; only the *array shape*
# (one chara_type int per local player, values 0-11) is confirmed from
# live PlayReport captures.  If a live capture later disproves a specific
# index, fix it HERE and in client/char_block_table.py's mirror, then
# regenerate -- the AP location ids are keyed by (stage_key, chara) so a
# name change does not renumber anything.


def _here() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _repo_root() -> str:
    # scripts/romfs/ -> repo root is two levels up.
    return os.path.abspath(os.path.join(_here(), "..", ".."))


def _stage_key(course: str) -> int:
    """murmur3_x86_32 of the StageParam path for ``CourseNNN``."""
    if mmh3 is None:
        raise SystemExit(
            "mmh3 is required: pip install mmh3 (the murmur3-x86_32 seed-0 "
            "hash the game uses for stage keys)")
    path = (f"Work/Stage/StageParam/{course}_Course."
            f"game__stage__StageParam.gyml")
    return mmh3.hash(path, 0, signed=False)


def _course_of(file_name: str) -> str:
    # "Course005_Main" / "Course005_Sub2" -> "Course005"
    return file_name.split("_", 1)[0]


def load_charblocks(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_rows(
    charblocks: list[dict],
    stage_key_to_name: dict[int, str],
) -> list[dict]:
    """Collapse to one row per (stage_key, chara).  Returns rows sorted by
    (stage_key, chara) for a stable, diff-friendly output."""
    # Group instances by (course, chara); keep the first position seen for
    # the future multi-block pass.
    grouped: dict[tuple[str, int], dict] = {}
    for inst in charblocks:
        course = _course_of(inst["file"])
        chara = int(inst["chara"])
        key = (course, chara)
        if key not in grouped:
            grouped[key] = {
                "course": course,
                "chara": chara,
                "positions": [],
                "n_instances": 0,
            }
        grouped[key]["positions"].append(inst.get("pos"))
        grouped[key]["n_instances"] += 1

    rows: list[dict] = []
    unknown_courses: set[str] = set()
    for (course, chara), g in grouped.items():
        sk = _stage_key(course)
        disp = stage_key_to_name.get(sk)
        if disp is None:
            unknown_courses.add(course)
            # Fall back to the raw CourseNNN so the row is still emitted;
            # the operator can fix the display name once the course shows
            # up in location_table.  Should be empty in practice.
            disp = course
        chara_name = (CHARA_NAMES[chara] if 0 <= chara < len(CHARA_NAMES)
                      else f"Chara{chara}")
        rows.append({
            "stage_key": sk,
            "chara": chara,
            "course": course,
            "course_name": disp,
            "name": f"{disp} - {chara_name} Block",
            "n_instances": g["n_instances"],
        })

    if unknown_courses:
        print(f"WARNING: {len(unknown_courses)} charblock course(s) had no "
              f"location_table display name (used raw CourseNNN): "
              f"{sorted(unknown_courses)}", file=sys.stderr)

    rows.sort(key=lambda r: (r["stage_key"], r["chara"]))
    return rows


def parse_location_table_names(path: str) -> dict[int, str]:
    """Extract stage_key -> 'W1: Course Name' from location_table.py's
    ``_STAGE_* = 0x... # World: Display Name`` constant block."""
    import re
    out: dict[int, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        txt = fh.read()
    pat = re.compile(
        r"_STAGE_[A-Z0-9_]+: Final\[int\] = (0x[0-9A-Fa-f]+)\s+# (.+)")
    for m in pat.finditer(txt):
        hexv = int(m.group(1), 16)
        out[hexv] = m.group(2).strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--charblocks",
        default=r"C:\Users\maxwe\Documents\smbw_re_tmp\charblock_table.json",
        help="path to the extracted charblock_table.json")
    ap.add_argument(
        "--splice", action="store_true",
        help="also rewrite apworld locations.json with the new rows")
    args = ap.parse_args()

    root = _repo_root()
    loc_table_py = os.path.join(
        root, "apworld", "smbw_archipelago", "client", "location_table.py")
    stage_key_to_name = parse_location_table_names(loc_table_py)

    charblocks = load_charblocks(args.charblocks)
    rows = build_rows(charblocks, stage_key_to_name)

    print(f"{len(charblocks)} instances -> {len(rows)} (course, chara) rows")

    # 1) client table.
    client_rows = [
        {"stage_key": r["stage_key"], "chara": r["chara"], "name": r["name"]}
        for r in rows
    ]
    client_out = os.path.join(
        root, "apworld", "smbw_archipelago", "client", "char_block_table.json")
    with open(client_out, "w", encoding="utf-8") as fh:
        json.dump(client_rows, fh, indent=1)
        fh.write("\n")
    print(f"wrote {client_out}")

    # 2) apworld location rows.
    loc_rows = []
    for r in rows:
        # Region + requires: same as the course's existing checks (course
        # entry).  We don't know the exact region string per course offline,
        # so the splice step (below) copies the region from an existing
        # location that shares the course_name prefix.  Here we emit a
        # placeholder the splice fills in.
        loc_rows.append({
            "name": r["name"],
            "course_name": r["course_name"],
            "category": ["Character Block"],
            "requires": [],
        })
    data_out = os.path.join(
        root, "apworld", "smbw_archipelago", "data",
        "_character_block_locations.json")
    with open(data_out, "w", encoding="utf-8") as fh:
        json.dump(loc_rows, fh, indent=2)
        fh.write("\n")
    print(f"wrote {data_out}")

    if args.splice:
        splice_locations(root, rows)

    return 0


def splice_locations(root: str, rows: list[dict]) -> None:
    """Append Character Block locations to locations.json, deriving each
    row's ``region`` from an existing location sharing the same course-name
    prefix.  Idempotent: removes any prior Character Block rows first so a
    re-run doesn't duplicate.  Appends at the END so existing location ids
    (assigned sequentially in Locations.py) stay stable."""
    loc_json = os.path.join(
        root, "apworld", "smbw_archipelago", "data", "locations.json")
    with open(loc_json, "r", encoding="utf-8") as fh:
        locs = json.load(fh)

    # Drop existing Character Block rows (idempotent re-run).
    locs = [l for l in locs
            if "Character Block" not in l.get("category", [])]

    # Build course_name -> region by scanning existing rows.  Course name
    # is the part before " - " in a location name.
    course_region: dict[str, str] = {}
    for l in locs:
        name = l.get("name", "")
        if " - " in name and l.get("region"):
            prefix = name.rsplit(" - ", 1)[0]
            course_region.setdefault(prefix, l["region"])

    appended = 0
    missing_region: list[str] = []
    for r in rows:
        region = course_region.get(r["course_name"])
        if region is None:
            missing_region.append(r["course_name"])
            region = "Manual"  # safe fallback; still reachable
        locs.append({
            "name": r["name"],
            "region": region,
            "category": ["Character Block"],
            "requires": [],
        })
        appended += 1

    with open(loc_json, "w", encoding="utf-8") as fh:
        json.dump(locs, fh, indent=2)
        fh.write("\n")
    print(f"spliced {appended} Character Block rows into {loc_json}")
    if missing_region:
        uniq = sorted(set(missing_region))
        print(f"WARNING: {len(uniq)} course(s) had no region match "
              f"(used 'Manual'): {uniq}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
