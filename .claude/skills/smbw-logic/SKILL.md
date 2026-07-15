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

## Badge logic — two layers (per-check + progression walls)

Badges gate logic in two independent ways. **The full rules, the structural
set, the Yoshi bypass, the open-coin exceptions, and the All-Power-Up-badge
rule all live in [`reference/logic-reconciliation.md`](reference/logic-reconciliation.md)
— read it before touching any badge gate.** The load-bearing summary:

**(A) Per-check (location layer).** A badge challenge "*X I/II*" course is judged
check-by-check, not gated wholesale:
- **10-Coins → require the badge** (safe default), *except* a vetted open set
  (`_OPEN_COIN_LEVELS`, currently **Parachute Cap I** — coins doable with
  nothing). Pinned by `test_badge_challenge_coins_require_their_badge` +
  `test_open_coin_levels_stay_open`.
- **Normal Exit / Top of Flag → require the badge only for *structural*
  courses** (`_STRUCTURAL_BADGE_LEVELS`: Wall-Climb Jump, Grappling Vine,
  Boosting Spin Jump, Floating High Jump, Crouching High Jump, **Dolphin Kick**,
  **Jet Run** — I & II). Others (Spring Feet, Invisibility, Parachute Cap) stay
  open. Pinned by `test_structural_badge_levels_gate_completion` /
  `test_nonstructural_badge_completion_stays_open`.
- **Yoshi bypass:** a Yoshi clears the *climb/float* structural courses, so
  Wall-Climb Jump I/II and Floating High Jump I carry `... OR |@Yoshi:1|` on
  every check (`Yoshi` category = the four Yoshis, not Nabbit).

**(B) Progression walls (region layer) — the softlock gotcha.** Because Wonder
Seeds are pool items, fill never strands a required item behind a badge gate it
can't open first, so an **optional side-spur** badge level is already safe. The
danger is a badge level that is a **forced wall** (you must clear it to advance
the world): the seeds-only region graph can't see the wall, so without an
**explicit badge `requires` on the region transition**, fill can bury the badge
in a later world → **unwinnable seed**.

**Only one real wall — gated in `regions.json`, pinned by
`tests/test_data_validation.py::test_progression_wall_badges_gate_regions`:**

| Region gate | Wall (level) | Badge |
|---|---|---|
| `W3 4 Seeds` | POOF! Crouching High Jump I | Crouching High Jump |

Two former "walls" — **Parachute Cap** (Badge House @ `W1 3 Seeds`) and **Auto
Super Mushroom** (Wiggler Race Mountaineering! @ `W1 10 Seeds`) — were **removed**
(player-confirmed): the granting level needs no badge to clear and nothing else
forces the badge, so they were never real walls (the same test pins their
*removal*). Mountaineering!'s Normal Exit is likewise open now. Badges handed
over in the **overworld** (only **Sensor**) stay ungated.

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
