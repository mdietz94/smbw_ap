---
name: smbw-logic
description: >-
  Edit and reason about the SMBW Archipelago apworld LOGIC — the item/location/
  region data tables and access rules that AP fill uses to place items and prove
  a seed beatable. Use when changing a region gate or location `requires`, wiring
  badge/seed/power-up gating, adding a check, touching Rules.py / Regions.py /
  Options.py / DataValidation.py or data/{items,locations,regions,game}.json,
  reconciling against the community logic PDF, debugging a generation failure
  (FillError) or "why was item X placed so late / can I even reach it" softlock,
  or running the generation + beatability tests. Bundles the logic-reconciliation
  record and the progression-wall softlock rule. Triggers: "add a region gate",
  "edit the logic", "regions.json", "locations.json", "why placed in W6",
  "softlock", "FillError", "is this beatable", "badge gating", "logic PDF",
  "DataValidation", "Wonder Seed count gate".
---

# SMBW Archipelago — apworld logic

The apworld is a **Manual-style** AP world: the logic is data, not code. Four
tables under `apworld/smbw_archipelago/data/` drive everything; `Rules.py`
compiles the `requires` strings into AP access rules and `DataValidation.py`
runs at generation time.

| File | Holds |
|---|---|
| `items.json` | every item, its `count`, `category`, and `progression` flag |
| `locations.json` | every check: `name`, `region`, `category`, `requires` |
| `regions.json` | the region graph: `connects_to` + per-region `requires` |
| `game.json` | game name / metadata |

`open_world.py` rewrites the region graph at gen time for open-world mode (it
severs cross-world edges and rebuilds Manual→`W{n} Start` entries; **intra-world
edges and their `requires` are preserved**, so logic walls carry over).

## The progression model: Wonder-Seed tolls

Each world is a linear chain of regions gated on **Wonder-Seed counts**, e.g.
`W1 Start → W1 3 Seeds → W1 10 Seeds → W1 14 Seeds → (exit to PI/next world)`.
The Royal-Seed palaces sit at the end of each world; the goal (`World Bowser`)
needs `|@Royal Seed:6|`. **Wonder Seeds are pool items** (`W1 Wonder Seed`
×35, etc.) — AP scatters them across reachable locations; the `|W1 Wonder
Seed:10|` gate just means "received 10 copies." This is an **abstraction of the
minimap**: it assumes "enough seeds + the right movement ⇒ you can reach the
palace." `requires` syntax: `|Item|`, `|Item:N|` (count), `|@Category:N|`,
`AND`/`OR`/parens, and `{OptOne(|X|)}` (movement/effects treated as
expected-available helpers). See `Rules.py` for the exact compile.

## ⚠️ The load-bearing gotcha: badge **progression walls**

**Rule (current):** *a level that **grants** a badge **requires** that badge in
logic.* AP is the sole badge authority — the in-game grant is reverted by the
forced-death / M5 path — so you must receive the badge from AP before clearing
the level that would have handed it to you. *Exception:* badges handed over in
the **overworld** (only **Sensor**, given before W5 Upshroom Downshroom) stay
ungated. This supersedes an older "badges are auto-present in their own challenge
level" assumption — **do not re-strip these requirements.**

**Why a `requires` on the location isn't always enough.** Because Wonder Seeds
are pool items, AP fill never strands a required item behind a badge gate it
can't open first — so a badge level that is an **optional side spur** is already
safe (all 18 badge-challenge "I/II" levels). The danger is a badge level that is
a **forced progression wall** (you must clear it to physically advance the
world). The seeds-only region graph can't see walls, so without an **explicit
badge requirement on the region transition**, fill happily buries the badge in a
later world → **unwinnable seed** (this is exactly how Parachute Cap once landed
in W6 while the Pipe-Rock Badge House blocked all of W1).

**The three known walls — gated in `regions.json`, pinned by
`tests/test_data_validation.py::test_progression_wall_badges_gate_regions`:**

| Region gate | Wall (level) | Badge |
|---|---|---|
| `W1 3 Seeds` | Badge House in Pipe-Rock Plateau | Parachute Cap |
| `W1 10 Seeds` | Wiggler Race Mountaineering! | Auto Super Mushroom |
| `W3 4 Seeds` | POOF! Crouching High Jump I | Crouching High Jump |

There are **no other Badge House levels** in the game; every other badge-granting
level is an optional side spur (fill-safe). Full detail + sources:
[`reference/logic-reconciliation.md`](reference/logic-reconciliation.md).

## Adding or changing a gate — the decision

1. **A check needs an item to be done?** Put the item in that location's
   `requires` (location layer). Fill handles it; no region change.
2. **A whole region is unreachable until you have an item / clear a forced
   level?** Gate the **region transition** (`requires` on the downstream region).
   Use this for any **forced wall** — including a non-badge level that blocks the
   only path (the same softlock class could exist for non-badge forced levels;
   audit when in doubt).
3. **Any item named in a `requires` must be `progression: true`** in items.json
   or generation aborts (`DataValidation` enforces this; there's a test).

## Validate & test

```bash
# JSON sanity
py -3 -c "import json; json.load(open('apworld/smbw_archipelago/data/regions.json'))"

# logic suite (DataValidation + open-world gen + the wall pin test)
py -3 -m pytest apworld/smbw_archipelago/tests/test_data_validation.py \
                apworld/smbw_archipelago/tests/test_open_world_gen.py -q
```

`test_open_world_gen.py` includes `test_open_world_off_still_solvable`
(standard-mode `fill=True` + `can_beat_game()`) and
`test_solvable_across_counts` (open-world). For belt-and-suspenders after a gate
change, fill+beat many standard seeds (a `FillError` or `can_beat_game()==False`
is the softlock signal):

```python
# needs vendor/Archipelago on sys.path (git submodule update --init vendor/Archipelago)
from test.general import setup_multiworld, gen_steps
from Fill import distribute_items_restrictive
from apworld.smbw_archipelago import SMBWonderWorld
for seed in range(1000, 1025):
    mw = setup_multiworld(SMBWonderWorld, gen_steps, seed=seed, options={"open_world": 0})
    distribute_items_restrictive(mw)
    assert mw.can_beat_game(), seed
```

To inspect *where* a specific item landed, iterate `mw.get_locations(1)` after
`distribute_items_restrictive` and match `loc.item.name`.

## Related skills

- **smbw-save-data** — the runtime side: how a granted item actually writes to
  game state (the badge requirement here exists because AP, not the game, is the
  badge authority).
- **smbw-reverse-engineering** — finding the hooks/Nerves behind grants & checks.
