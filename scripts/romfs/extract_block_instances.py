"""Extract every *block* actor placement from the RomFS BancMapUnit stage data.

This replaces the lost `smbw_re_tmp/charblock_table.py` scratch script that
originally produced `client/char_block_table.json`.  That script was never
version-controlled and is gone; regenerating the character-block table was
therefore impossible from the repo alone, and the banc `chara` -> roster enum
mapping could not be re-verified.  Hence this file.

Scope is deliberately WIDER than character blocks: it emits *all* block-family
actors, because the character-block hit detection on the Switch discriminates
by AINB graph LAYOUT (`delta = node - [node+0x28]`, 0x17d0 assumed to mean
`ObjectBlockClarityCharacter`), not by actor identity.  Knowing which other
block actors exist -- and crucially which courses contain a plain
`BlockClarity` but NO `ObjectBlockClarityCharacter` -- is what makes the
false-positive test conclusive.

Input:  <romfs>/BancMapUnit/*.bcett.byml.zs   (zstd-compressed BYML)
Output: JSON list of rows:
    {file, stage, gyaml, hash, chara, translate:[x,y,z], layer, name}
  `chara` is the banc `Dynamic.PlayerCharaType` (S32) when present, else None.

Usage:
    python scripts/romfs/extract_block_instances.py \
        --romfs C:\\Users\\maxwe\\Documents\\smbw_re_tmp\\romfs \
        --out   C:\\Users\\maxwe\\Documents\\smbw_re_tmp\\block_instances.json

    # just the summary, no file written
    python scripts/romfs/extract_block_instances.py --romfs <...> --summary
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from byml_parse import Byml  # noqa: E402

try:
    import zstandard
except ImportError:  # pragma: no cover
    sys.exit("need `pip install zstandard`")


# An actor is "block-family" if its gyaml contains Block.  Kept broad on
# purpose -- the point is to discover what exists, not to prejudge it.
BLOCK_RE = re.compile(r"Block", re.IGNORECASE)

# The two actors the character-block feature cares about.
CHAR_BLOCK = "ObjectBlockClarityCharacter"
PLAIN_CLARITY = "BlockClarity"

STAGE_RE = re.compile(r"^(Course\d+|[A-Za-z]+\d*)_")


def read_banc(path: Path) -> dict:
    raw = zstandard.ZstdDecompressor().decompress(
        path.read_bytes(), max_output_size=256 * 1024 * 1024)
    return Byml(raw).parse()


def stage_of(filename: str) -> str:
    """'Course001_Main.bcett.byml.zs' -> 'Course001'."""
    m = STAGE_RE.match(filename)
    return m.group(1) if m else filename.split(".")[0]


def collect(romfs: Path) -> list[dict]:
    banc_dir = romfs / "BancMapUnit"
    if not banc_dir.is_dir():
        sys.exit(f"no BancMapUnit under {romfs}")
    rows: list[dict] = []
    files = sorted(banc_dir.glob("*.bcett.byml.zs"))
    for i, path in enumerate(files, 1):
        try:
            doc = read_banc(path)
        except Exception as exc:  # a malformed/oddball file must not abort
            print(f"  !! {path.name}: {exc}", file=sys.stderr)
            continue
        stage = stage_of(path.name)
        for actor in doc.get("Actors", []) or []:
            gyaml = actor.get("Gyaml") or ""
            if not BLOCK_RE.search(gyaml):
                continue
            dyn = actor.get("Dynamic") or {}
            chara = dyn.get("PlayerCharaType")
            rows.append({
                "file": path.name,
                "stage": stage,
                "gyaml": gyaml,
                "hash": actor.get("Hash"),
                "chara": chara if isinstance(chara, int) else None,
                "translate": actor.get("Translate"),
                "layer": actor.get("Layer"),
                "name": actor.get("Name"),
            })
        if i % 100 == 0:
            print(f"  ...{i}/{len(files)} files", file=sys.stderr)
    return rows


def summarize(rows: list[dict]) -> None:
    by_gyaml = collections.Counter(r["gyaml"] for r in rows)
    print(f"\n=== block-family actors ({len(rows)} placements, "
          f"{len(by_gyaml)} distinct gyaml) ===")
    for name, n in by_gyaml.most_common():
        mark = ""
        if name == CHAR_BLOCK:
            mark = "   <== the character block (emits an AP check)"
        elif name == PLAIN_CLARITY:
            mark = "   <== plain hidden block (same 'Clarity' family)"
        print(f"  {n:5}  {name}{mark}")

    # Per-stage presence of the two Clarity actors -- the false-positive matrix.
    stages = collections.defaultdict(lambda: {"char": 0, "plain": 0})
    for r in rows:
        if r["gyaml"] == CHAR_BLOCK:
            stages[r["stage"]]["char"] += 1
        elif r["gyaml"] == PLAIN_CLARITY:
            stages[r["stage"]]["plain"] += 1
    only_plain = sorted(s for s, c in stages.items()
                        if c["plain"] and not c["char"])
    both = sorted(s for s, c in stages.items() if c["plain"] and c["char"])
    only_char = sorted(s for s, c in stages.items()
                       if c["char"] and not c["plain"])
    print(f"\n=== Clarity-actor stage matrix ===")
    print(f"  plain BlockClarity ONLY (no char block): {len(only_plain)} stages")
    print(f"  both:                                    {len(both)} stages")
    print(f"  char block only:                         {len(only_char)} stages")
    print(f"\n  *** FALSE-POSITIVE TEST STAGES (plain hidden block, no "
          f"character block) ***")
    print("  " + ", ".join(only_plain))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--romfs", required=True, type=Path,
                   help="extracted RomFS root (contains BancMapUnit/)")
    p.add_argument("--out", type=Path, help="write the JSON rows here")
    p.add_argument("--summary", action="store_true",
                   help="print the summary (implied when --out is omitted)")
    a = p.parse_args(argv)

    rows = collect(a.romfs)
    if a.out:
        a.out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(f"OK: wrote {a.out} ({len(rows)} block placements)")
    if a.summary or not a.out:
        summarize(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
