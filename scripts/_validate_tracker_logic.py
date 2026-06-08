"""Cross-check generated tracker Lua against an independent Python port of the
apworld Rules.py evaluator, over many random inventories.  Throwaway harness."""
import json, re, random, sys
from pathlib import Path
from lupa import LuaRuntime

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "apworld" / "smbw_archipelago" / "data"
TRK = Path(sys.argv[1])

items = json.load(open(DATA / "items.json"))
locations = json.load(open(DATA / "locations.json"))
regions = json.load(open(DATA / "regions.json"))
STARTING_INDEX = 14887804000

# id->code/path bridges (same as generator)
im = {}
for m in re.finditer(r'\[(\d+)\]\s*=\s*\{\{"([^"]+)"', (TRK / "scripts/autotracking/item_mapping.lua").read_text()):
    im[int(m.group(1))] = m.group(2)
lm = {}
for m in re.finditer(r'\[(\d+)\]\s*=\s*\{"@([^"]+)"\}', (TRK / "scripts/autotracking/location_mapping.lua").read_text()):
    lm[int(m.group(1))] = m.group(2)

name2code = {it["name"]: im[STARTING_INDEX + i] for i, it in enumerate(items) if STARTING_INDEX + i in im}
code2name = {c: n for n, c in name2code.items()}
name2count = {it["name"]: int(it.get("count", 1)) for it in items}
ROYAL = ["W1 Royal Seed", "W2 Royal Seed", "W3 Royal Seed", "W4 Royal Seed", "W5 Royal Seed", "W6 Royal Seed"]
# Buttons / Wonder Effects / Wonder Flower are granted at start: the tracker
# never gates on them, so the reference model must treat them as always held.
ALWAYS_AVAILABLE = {it["name"] for it in items
                    if set(it.get("category", [])) & {"Button", "Wonder Effect", "Wonder Flower"}}


# ---- independent Python port of Rules.py checkRequireStringForArea ---------- #
def pyeval(req, inv_names):
    """inv_names: dict name->count. Returns bool. Mirrors Rules.py."""
    if not req:
        return True
    s = req
    # {OptOne(|item|)} -> |item:clamped|
    def opt_sub(m):
        body = m.group(1).strip().strip("|")
        cat = body.startswith("@")
        body = body.lstrip("@")
        if ":" in body:
            nm, cnt = body.split(":", 1)
            nm, cnt = nm.strip(), cnt.strip()
        else:
            nm, cnt = body.strip(), "1"
        if cat:
            return f"|@{nm}:{cnt}|"
        n = min(int(cnt), name2count.get(nm, 0))
        return f"|{nm}:{n}|"
    s = re.sub(r"\{OptOne\(\s*(\|[^|]+\|)\s*\)\}", opt_sub, s)
    # |item:n| / |@cat:n| -> 1/0
    def item_sub(m):
        tok = m.group(0)
        inner = tok.strip("|")
        cat = inner.startswith("@")
        inner = inner.lstrip("@")
        if ":" in inner:
            nm, cnt = inner.split(":", 1)
            nm, cnt = nm.strip(), int(cnt.strip())
        else:
            nm, cnt = inner.strip(), 1
        if cat:
            if nm == "Royal Seed":
                total = sum(inv_names.get(r, 0) for r in ROYAL)
            else:
                raise ValueError(nm)
            return "1" if total >= cnt else "0"
        return "1" if inv_names.get(nm, 0) >= cnt else "0"
    s = re.sub(r"\|[^|]+\|", item_sub, s)
    s = re.sub(r"\s?\bAND\b\s?", "&", s, flags=re.IGNORECASE)
    s = re.sub(r"\s?\bOR\b\s?", "|", s, flags=re.IGNORECASE)
    # eval boolean with & | ! and parens over 0/1
    s = s.replace("&", " and ").replace("|", " or ").replace("!", " not ")
    s = re.sub(r"\b1\b", "True", s).replace("0", "False")
    try:
        return bool(eval(s))
    except Exception as e:
        raise ValueError(f"pyeval failed for {req!r} -> {s!r}: {e}")


# region full() in python
parents = {r: [] for r in regions}
for src, info in regions.items():
    for dst in info.get("connects_to", []):
        if dst in parents:
            parents[dst].append(src)

def full(region, inv, memo):
    if region in memo:
        return memo[region]
    info = regions[region]
    if not pyeval(info.get("requires", "") or "", inv):
        memo[region] = False
        return False
    if info.get("starting"):
        memo[region] = True
        return True
    reach = any(full(p, inv, memo) for p in parents[region]) if parents[region] else False
    memo[region] = reach
    return reach

def py_loc_access(loc, inv):
    memo = {}
    region = loc.get("region")
    if region in regions and not full(region, inv, memo):
        return False
    return pyeval(loc.get("requires", "") or "", inv)


# ---- open-world reference model (mirrors open_world.py) --------------------- #
GOAL = "BC: Bowser's Rage Stage - Royal Seed"

def world_of_region(r):
    if len(r) >= 3 and r[0] == "W" and r[1].isdigit() and r[2] == " ":
        return int(r[1])
    return None

def is_hub(r):
    return r.startswith("PI ") or r in ("Pre-W4 Special", "Special End", "Post-Badge")

def is_world_start(r):
    return bool(re.fullmatch(r"W\d Start", r))

def open_full(region, inv_open, active, memo):
    """Intra-world reachability: world roots are reachable iff active; only
    same-world parent edges survive (open_world.py severs the rest)."""
    if region in memo:
        return memo[region]
    info = regions[region]
    if not pyeval(info.get("requires", "") or "", inv_open):
        memo[region] = False
        return False
    if is_world_start(region):
        memo[region] = world_of_region(region) in active
        return memo[region]
    w = world_of_region(region)
    intra = [p for p in parents[region] if world_of_region(p) == w]
    res = any(open_full(p, inv_open, active, memo) for p in intra) if intra else False
    memo[region] = res
    return res

def py_open_access(loc, inv, active, P):
    region = loc.get("region")
    name = loc["name"]
    inv_open = dict(inv)
    for nm in ALWAYS_AVAILABLE | {"Petal Isles Wonder Seed", "Special World Wonder Seed"}:
        inv_open[nm] = name2count.get(nm, 1)   # precollected / always-granted
    if region == "World Bowser":
        if name != GOAL:
            return False                        # stripped in open-world
        royals = sum(1 for n in active if inv.get(f"W{n} Royal Seed", 0) > 0)
        if royals < P:
            return False
        return pyeval(loc.get("requires", "") or "", inv_open)
    if is_hub(region):
        return False                            # hub stripped
    w = world_of_region(region)
    if w is not None and w not in active:
        return False
    if region in regions and not open_full(region, inv_open, active, {}):
        return False
    return pyeval(loc.get("requires", "") or "", inv_open)


# ---- lua side -------------------------------------------------------------- #
lua = LuaRuntime(unpack_returned_tuples=True)
INV = {}  # code -> count, shared with lua via closure
lua.execute("""
AccessibilityLevel = {None=0, Partial=1, Inspect=3, SequenceBreak=5, Normal=6, Cleared=7}
ScriptHost = { AddWatchForCode = function(...) end }
""")
lua.globals().pycount = lambda code: INV.get(code, 0)
lua.execute("""
Tracker = { ProviderCountForCode = function(self, code) return pycount(code) end }
""")
# load logic_helper (defines ACCESS_*, ALL, ANY, HAS)
lua.execute((TRK / "scripts/logic/logic_helper.lua").read_text())
# load generated
lua.execute((TRK / "scripts/logic/smbw_generated_logic.lua").read_text())
smbw_loc = lua.globals().smbw_loc
ACCESS_NORMAL = lua.globals().ACCESS_NORMAL

def lua_loc_access(apid, inv_codes):
    INV.clear()
    for c, v in inv_codes.items():
        INV[c] = v
    # bump GEN so cache invalidates each call
    lua.execute("if smbw_logic_invalidate then end")
    return int(smbw_loc(str(apid))) >= int(ACCESS_NORMAL)

# Since RCACHE keys on GEN and GEN only bumps via watch callback (not called here),
# force fresh eval by reloading generated module's GEN each iteration:
# simpler: monkeypatch by setting GEN via a global bump function.
lua.execute("function __bumpgen() end")


def random_inv():
    inv_names = {}
    inv_codes = {}
    for it in items:
        nm = it["name"]
        mx = name2count[nm]
        if nm in ALWAYS_AVAILABLE:
            c = mx  # granted at start -> always held
        else:
            # bias: often 0, sometimes full/partial
            r = random.random()
            if r < 0.45:
                c = 0
            elif mx == 1:
                c = 1
            else:
                c = random.randint(1, mx)
        if c:
            inv_names[nm] = c
            inv_codes[name2code[nm]] = c
    return inv_names, inv_codes


def main():
    # GEN cache problem: generated lua caches on GEN which never changes here, so
    # region values would be stale across inventories. Reload the generated chunk
    # each inventory to reset RCACHE. (Cheap enough for validation.)
    gen_src = (TRK / "scripts/logic/smbw_generated_logic.lua").read_text()
    N = 200
    mism = 0
    NORMAL = int(lua.globals().ACCESS_NORMAL)
    apids = [STARTING_INDEX + 500 + i for i in range(len(locations)) if (STARTING_INDEX + 500 + i) in lm]
    idx_by_apid = {STARTING_INDEX + 500 + i: i for i in range(len(locations))}
    random.seed(1234)

    def run_scenario(inv_codes, slot_data_lua, py_access):
        nonlocal mism
        INV.clear(); INV.update(inv_codes)
        lua.globals().SLOT_DATA = slot_data_lua
        lua.execute(gen_src)  # fresh RCACHE + re-reads SLOT_DATA at call time
        sl = lua.globals().smbw_loc
        for apid in apids:
            loc = locations[idx_by_apid[apid]]
            py = py_access(loc)
            lv = int(sl(str(apid))) >= NORMAL
            if py != lv:
                mism += 1
                if mism <= 20:
                    print(f"MISMATCH apid={apid} {loc['name']!r} py={py} lua={lv}")
                    print(f"   region={loc.get('region')} requires={loc.get('requires')!r}")

    checks = 0
    for t in range(N):
        inv_names, inv_codes = random_inv()
        # standard mode
        run_scenario(inv_codes, None, lambda loc: py_loc_access(loc, inv_names))
        checks += len(apids)
        # open-world mode: random active set + palace threshold
        k = random.randint(1, 6)
        active = sorted(random.sample(range(1, 7), k))
        P = random.randint(1, k)
        sd = lua.table_from({"open_world": 1,
                             "open_world_active": lua.table_from(active),
                             "palaces_required": P})
        run_scenario(inv_codes, sd, lambda loc, a=active, p=P: py_open_access(loc, inv_names, a, p))
        checks += len(apids)
    print(f"checked {checks} evals across standard + open-world; mismatches={mism}")


if __name__ == "__main__":
    main()
