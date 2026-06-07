#!/usr/bin/env python3
"""Generate PopTracker logic for the SMBW_Tracker pack from the apworld logic.

The apworld (apworld/smbw_archipelago/data/{regions,locations,items}.json) is the
single source of truth for SMBW Archipelago access logic.  The community tracker
at https://github.com/LuckwurstJoe/SMBW_Tracker ships every location/item/map but
no access logic -- every check shows reachable from the start.

This script translates the apworld's region graph + `requires` strings into
PopTracker access rules and writes them into a checkout of the tracker:

  * `scripts/logic/smbw_generated_logic.lua` -- one Lua function per region
    implementing AP reachability (full(R) = R.requires AND OR(full(parents))),
    one rule per AP location id, and a `smbw_loc(id)` dispatcher.  Counted/seed
    gates compile to `HAS(code, n, n)`; the `|@Royal Seed:N|` category compiles
    to a `smbw_royal(n)` sum helper.
  * `items/logic_item.json` -- hidden toggle items for the button / Wonder-Effect
    / Wonder-Flower / power-up codes the apworld gates on.  The tracker already
    maps these in autotracking/item_mapping.lua but never defined item objects
    for them, so `HAS()` would read 0 forever without this file.
  * each `locations/*.json` section gets `"access_rules": ["$smbw_loc|<apid>"]`
    injected (surgical text insert, minimal diff).
  * `scripts/init.lua` gains a `require` for the generated logic; the two import
    helpers gain the new logic/items files.

The bridge between the two repos is the AP id:
  location id = starting_index + 500 + (index in locations.json)
  item id     = starting_index + (index in items.json)
which is exactly how the tracker's autotracking/{location,item}_mapping.lua keys
its tables, so paths/codes line up 1:1.

Usage:
    py -3 scripts/generate_tracker_logic.py <path-to-SMBW_Tracker-checkout> \
          [<path-to-apworld-data-dir>]

The apworld data dir defaults to this repo's apworld/smbw_archipelago/data when
the script is run from the SMBW Archipelago repo; pass it explicitly when running
this from a tracker checkout (a copy of this script ships under the tracker's
tools/ for transparency).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    import json5  # tolerant parser: the tracker's location JSON uses trailing commas
except ImportError:
    sys.exit("This script needs json5:  py -3 -m pip install json5")

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DATA = REPO / "apworld" / "smbw_archipelago" / "data"

# starting_index for game "SMBWonder" / creator "Zim" (see Game.py).  Hardcoded
# so this script needs no Archipelago on sys.path; asserted against the tracker.
STARTING_INDEX = 14887804000
LOC_BASE = STARTING_INDEX + 500


# --------------------------------------------------------------------------- #
# Load apworld data + build the id <-> tracker-code/path bridges
# --------------------------------------------------------------------------- #
def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def parse_item_mapping(tracker: Path) -> dict[int, str]:
    """ap item id -> first tracker code."""
    out: dict[int, str] = {}
    text = (tracker / "scripts" / "autotracking" / "item_mapping.lua").read_text(encoding="utf-8")
    for m in re.finditer(r'\[(\d+)\]\s*=\s*\{\{"([^"]+)"', text):
        out[int(m.group(1))] = m.group(2)
    return out


def parse_location_mapping(tracker: Path) -> dict[int, str]:
    """ap location id -> "World/Level/Section" path (the leading @ stripped)."""
    out: dict[int, str] = {}
    text = (tracker / "scripts" / "autotracking" / "location_mapping.lua").read_text(encoding="utf-8")
    for m in re.finditer(r'\[(\d+)\]\s*=\s*\{"@([^"]+)"\}', text):
        out[int(m.group(1))] = m.group(2)
    return out


# --------------------------------------------------------------------------- #
# requires-string -> Lua expression compiler
# --------------------------------------------------------------------------- #
ROYAL_CODES = ["w1royalseed", "w2royalseed", "w3royalseed", "w4royalseed", "w5royalseed", "w6royalseed"]

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<lparen>\() |
        (?P<rparen>\)) |
        (?P<and>\bAND\b) |
        (?P<or>\bOR\b) |
        (?P<opt>\{OptOne\(\s*(?P<optbody>\|[^|]+\|)\s*\)\}) |
        (?P<item>\|[^|]+\|)
    )""",
    re.IGNORECASE | re.VERBOSE,
)


class Compiler:
    def __init__(self, name2code: dict[str, str], name2count: dict[str, int]):
        self.name2code = name2code
        self.name2count = name2count
        self.used_codes: set[str] = set()

    # -- atom helpers ------------------------------------------------------- #
    def _item_atom(self, body: str, optional: bool) -> str:
        """Compile a `|...|` body to a Lua sub-expression (a HAS/smbw_royal call
        or the literal `true`)."""
        inner = body.strip().strip("|")
        is_cat = inner.startswith("@")
        inner = inner.lstrip("@")
        if ":" in inner:
            iname, cnt = inner.split(":", 1)
            iname, cnt = iname.strip(), cnt.strip()
        else:
            iname, cnt = inner.strip(), "1"

        if is_cat:
            if iname != "Royal Seed":
                raise ValueError(f"Unsupported category in requires: {iname!r}")
            for c in ROYAL_CODES:
                self.used_codes.add(c)
            return f"smbw_royal({int(cnt)})"

        code = self.name2code[iname]
        n = int(cnt)
        if optional:
            # OptOne clamps the count to how many exist in the pool; 0 -> always true.
            n = min(n, self.name2count.get(iname, 0))
            if n <= 0:
                return "true"
        self.used_codes.add(code)
        if n <= 1:
            return f'HAS("{code}")'
        # amountInLogic == amount keeps the check in-logic (NORMAL), not a seq-break.
        return f'HAS("{code}", {n}, {n})'

    # -- recursive-descent over AND/OR/parens ------------------------------- #
    def compile(self, expr: str) -> str:
        if not expr or (isinstance(expr, list) and not expr):
            return "true"
        # Tolerate a benign data typo: a `{OptOne(...)` that lost its leading `{`
        # (Rules.py's loose regex ignores it; a strict parser must not choke).
        expr = re.sub(r"(?<!\{)\bOptOne\(", "{OptOne(", expr)
        toks = self._tokenize(expr)
        self._pos = 0
        self._toks = toks
        node = self._parse_or()
        if self._pos != len(toks):
            raise ValueError(f"Trailing tokens in requires: {expr!r}")
        return self._emit(node)

    def _tokenize(self, expr: str):
        toks = []
        pos = 0
        while pos < len(expr):
            if expr[pos].isspace():
                pos += 1
                continue
            m = _TOKEN.match(expr, pos)
            if not m:
                raise ValueError(f"Cannot tokenize {expr[pos:]!r} in {expr!r}")
            if m.group("lparen"):
                toks.append(("(", None))
            elif m.group("rparen"):
                toks.append((")", None))
            elif m.group("and"):
                toks.append(("AND", None))
            elif m.group("or"):
                toks.append(("OR", None))
            elif m.group("opt"):
                toks.append(("ATOM", self._item_atom(m.group("optbody"), optional=True)))
            elif m.group("item"):
                toks.append(("ATOM", self._item_atom(m.group("item"), optional=False)))
            pos = m.end()
        return toks

    def _peek(self):
        return self._toks[self._pos] if self._pos < len(self._toks) else (None, None)

    def _parse_or(self):
        nodes = [self._parse_and()]
        while self._peek()[0] == "OR":
            self._pos += 1
            nodes.append(self._parse_and())
        return ("OR", nodes) if len(nodes) > 1 else nodes[0]

    def _parse_and(self):
        nodes = [self._parse_atom()]
        while self._peek()[0] == "AND":
            self._pos += 1
            nodes.append(self._parse_atom())
        return ("AND", nodes) if len(nodes) > 1 else nodes[0]

    def _parse_atom(self):
        kind, val = self._peek()
        if kind == "(":
            self._pos += 1
            node = self._parse_or()
            if self._peek()[0] != ")":
                raise ValueError("Unbalanced parens")
            self._pos += 1
            return node
        if kind == "ATOM":
            self._pos += 1
            return ("ATOM", val)
        raise ValueError(f"Unexpected token {kind!r}")

    def _emit(self, node) -> str:
        kind = node[0]
        if kind == "ATOM":
            return node[1]
        args = [self._emit(n) for n in node[1]]
        # collapse trivially-true args
        args = [a for a in args if a != "true"]
        if not args:
            return "true"
        if len(args) == 1:
            return args[0]
        fn = "ALL" if kind == "AND" else "ANY"
        return f"{fn}({', '.join(args)})"


# --------------------------------------------------------------------------- #
# Region full() topological emit
# --------------------------------------------------------------------------- #
def region_func_name(region: str) -> str:
    return "R_" + re.sub(r"[^0-9A-Za-z]+", "_", region).strip("_")


def topo_order(regions: dict) -> list[str]:
    parents: dict[str, list[str]] = {r: [] for r in regions}
    for src, info in regions.items():
        for dst in info.get("connects_to", []):
            if dst in parents:
                parents[dst].append(src)
    order, seen = [], set()

    def visit(r):
        if r in seen:
            return
        seen.add(r)
        for p in parents[r]:
            visit(p)
        order.append(r)

    for r in regions:
        visit(r)
    return order, parents


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    tracker = Path(sys.argv[1]).resolve()
    if not (tracker / "manifest.json").exists():
        sys.exit(f"Not a tracker checkout: {tracker}")
    data = Path(sys.argv[2]).resolve() if len(sys.argv) == 3 else DEFAULT_DATA
    if not (data / "regions.json").exists():
        sys.exit(f"apworld data dir not found: {data}\n{__doc__}")

    items = load_json(data / "items.json")
    locations = load_json(data / "locations.json")
    regions = load_json(data / "regions.json")

    item_map = parse_item_mapping(tracker)
    loc_map = parse_location_mapping(tracker)

    # sanity: our computed ids line up with the tracker's mapping tables
    assert item_map.get(STARTING_INDEX) is not None, "item id base mismatch"
    assert LOC_BASE in loc_map, "loc id base mismatch (starting_index drift?)"

    name2code = {it["name"]: item_map[STARTING_INDEX + i]
                 for i, it in enumerate(items) if STARTING_INDEX + i in item_map}
    name2count = {it["name"]: int(it.get("count", 1)) for it in items}
    code2type: dict[str, str] = {}
    for i, it in enumerate(items):
        apid = STARTING_INDEX + i
        if apid in item_map:
            code2type[item_map[apid]] = "consumable" if name2count[it["name"]] > 1 else "toggle"

    comp = Compiler(name2code, name2count)

    # -- compile region full() expressions -------------------------------- #
    order, parents = topo_order(regions)
    region_expr: dict[str, str] = {}
    for region in order:
        info = regions[region]
        req_expr = comp.compile(info.get("requires", "") or "")
        ps = parents[region]
        if info.get("starting"):
            reach = "true"
        elif not ps:
            reach = "ACCESS_NONE"   # orphan, unreachable in AP
        elif len(ps) == 1:
            reach = f"{region_func_name(ps[0])}()"
        else:
            reach = "ANY(" + ", ".join(f"{region_func_name(p)}()" for p in ps) + ")"
        parts = [p for p in (req_expr, reach) if p != "true"]
        if not parts:
            body = "ACCESS_NORMAL"          # starting region, no requirement
        elif len(parts) == 1:
            body = parts[0]                  # already a HAS/region-call/ANY/ALL -> returns a level
        else:
            body = "ALL(" + ", ".join(parts) + ")"
        region_expr[region] = body

    # -- compile per-location rules --------------------------------------- #
    loc_rules: dict[int, str] = {}   # apid -> lua expr
    skipped_no_path = []
    for idx, loc in enumerate(locations):
        apid = LOC_BASE + idx
        if apid not in loc_map:
            skipped_no_path.append(loc["name"])
            continue
        region = loc.get("region")
        rexpr = region_func_name(region) + "()" if region in regions else "ACCESS_NORMAL"
        lexpr = comp.compile(loc.get("requires", "") or "")
        if lexpr == "true":
            body = rexpr
        else:
            body = f"ALL({rexpr}, {lexpr})"
        loc_rules[apid] = body

    write_lua(tracker, regions, order, region_expr, loc_rules)
    write_logic_items(tracker, comp.used_codes, code2type)
    inject_access_rules(tracker, loc_map, loc_rules)
    patch_imports(tracker)

    print(f"  regions compiled : {len(region_expr)}")
    print(f"  location rules   : {len(loc_rules)}")
    print(f"  logic-only items : {sum(1 for c in comp.used_codes)} candidates")
    if skipped_no_path:
        print(f"  skipped (no tracker path): {len(skipped_no_path)} -> {skipped_no_path}")


# --------------------------------------------------------------------------- #
# Emitters
# --------------------------------------------------------------------------- #
def write_lua(tracker, regions, order, region_expr, loc_rules):
    L = []
    L.append("-- AUTO-GENERATED by scripts/generate_tracker_logic.py -- do not edit by hand.")
    L.append("-- Source of truth: SMBW Archipelago apworld data/{regions,locations,items}.json")
    L.append("-- Implements AP reachability: full(R) = R.requires AND OR(full(parents)).")
    L.append("")
    L.append("local GEN = 0")
    L.append("local RCACHE = {}")
    L.append('ScriptHost:AddWatchForCode("smbw_logic_invalidate", "*", function() GEN = GEN + 1 end)')
    L.append("")
    L.append("-- |@Royal Seed:n| category gate -- sum of the six world Royal Seeds.")
    L.append("function smbw_royal(n)")
    L.append("    local c = " + "\n        + ".join(f'Tracker:ProviderCountForCode("{c}")' for c in ROYAL_CODES))
    L.append("    if c >= tonumber(n) then return ACCESS_NORMAL end")
    L.append("    return ACCESS_NONE")
    L.append("end")
    L.append("")
    L.append("-- Region reachability (topologically ordered: parents before children).")
    for region in order:
        fn = region_func_name(region)
        body = region_expr[region]
        if body in ("ACCESS_NORMAL", "ACCESS_NONE"):
            L.append(f"local function {fn}() return {body} end")
        else:
            L.append(f"local function {fn}()")
            L.append(f'    local hit = RCACHE["{fn}"]')
            L.append("    if hit and hit.gen == GEN then return hit.v end")
            L.append(f"    local v = {body}")
            L.append(f'    RCACHE["{fn}"] = {{gen = GEN, v = v}}')
            L.append("    return v")
            L.append("end")
    L.append("")
    L.append("-- Per-AP-location access rules.")
    L.append("local LOC = {}")
    for apid in sorted(loc_rules):
        L.append(f'LOC["{apid}"] = function() return {loc_rules[apid]} end')
    L.append("")
    L.append("function smbw_loc(id)")
    L.append("    local f = LOC[id]")
    L.append("    if f then return f() end")
    L.append("    return ACCESS_NORMAL")
    L.append("end")
    L.append("")
    out = tracker / "scripts" / "logic" / "smbw_generated_logic.lua"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(tracker)} ({len(L)} lines)")


def write_logic_items(tracker, used_codes, code2type):
    """Define toggle/consumable item objects for codes the rules read but the
    tracker never declared (buttons, Wonder Effects, Wonder Flower, lowercase
    power-up aliases).  Without these, ProviderCountForCode() is always 0."""
    defined = set()
    for f in (tracker / "items").glob("*.json"):
        if f.name == "logic_item.json":
            continue
        for it in json5.loads(f.read_text(encoding="utf-8")):
            for part in re.split(r"[|,]", str(it.get("codes", ""))):
                if part.strip():
                    defined.add(part.strip())
    missing = sorted(c for c in used_codes if c not in defined)
    objs = []
    for code in missing:
        name = code.replace("-", " ").replace("/", " ").title() + " (logic)"
        objs.append({
            "name": name,
            "type": code2type.get(code, "toggle"),
            "allow_disabled": True,
            "img": "images/various/fake.png",
            "codes": code,
        })
    out = tracker / "items" / "logic_item.json"
    out.write_text(json.dumps(objs, indent=4) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(tracker)} ({len(objs)} logic-only items)")


def inject_access_rules(tracker, loc_map, loc_rules):
    """Surgically insert `"access_rules": ["$smbw_loc|<id>"]` after each section's
    name line, scoped per (world, level, section) path -- minimal diff, original
    formatting preserved."""
    # (world, level, section) -> apid, for the locations we have rules for
    want: dict[tuple, int] = {}
    for apid in loc_rules:
        seg = loc_map[apid].split("/")
        if len(seg) >= 3:                       # only section-level paths are injectable
            want[(seg[0], seg[1], seg[2])] = apid

    injected = 0
    files_touched = 0
    for f in sorted((tracker / "locations").glob("*.json")):
        if "unused" in f.name:                  # the *_with_unused_locations variant is not imported
            continue
        text = f.read_text(encoding="utf-8")
        data = json5.loads(text)
        # document order of sections in this file
        doc = [(grp.get("name"), lvl.get("name"), sec.get("name"))
               for grp in data
               for lvl in grp.get("children", [])
               for sec in lvl.get("sections", [])]

        scan = 0
        file_hits = 0
        for world, level, section in doc:
            apid = want.get((world, level, section))
            if apid is None:
                continue
            # forward-scanning match keeps duplicate section names (e.g. "Wonder
            # Seed") aligned to the right level, since doc order == text order.
            pat = re.compile(r'([ \t]*)"name"\s*:\s*"' + re.escape(section) + r'"\s*,[^\n]*\n')
            m = pat.search(text, scan)
            if not m:
                continue
            indent = m.group(1)
            insert = f'{indent}"access_rules": ["$smbw_loc|{apid}"],\n'
            ins_at = m.end()
            text = text[:ins_at] + insert + text[ins_at:]
            scan = ins_at + len(insert)
            file_hits += 1
        if file_hits:
            f.write_text(text, encoding="utf-8")
            injected += file_hits
            files_touched += 1
    print(f"injected access_rules into {injected} sections across {files_touched} files")


def patch_imports(tracker):
    # init.lua: require the generated logic after base_logic
    init = tracker / "scripts" / "init.lua"
    t = init.read_text(encoding="utf-8")
    if "smbw_generated_logic" not in t:
        t = t.replace(
            'require("scripts/logic/base_logic")',
            'require("scripts/logic/base_logic")\nrequire("scripts/logic/smbw_generated_logic")',
        )
        init.write_text(t, encoding="utf-8")
        print("patched scripts/init.lua (require smbw_generated_logic)")
    # items_import.lua: add logic_item.json
    imp = tracker / "scripts" / "items_import.lua"
    t = imp.read_text(encoding="utf-8")
    if "logic_item.json" not in t:
        t = t.rstrip("\n") + '\nTracker:AddItems("items/logic_item.json")\n'
        imp.write_text(t, encoding="utf-8")
        print("patched scripts/items_import.lua (AddItems logic_item.json)")


if __name__ == "__main__":
    main()
