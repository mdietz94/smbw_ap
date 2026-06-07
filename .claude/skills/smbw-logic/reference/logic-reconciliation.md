# SMBW Archipelago — logic reconciliation record

The apworld logic was diffed across multiple rounds against the
community-maintained logic PDF. After applying user-confirmed filters
(Wonder Effects always granted, all power-ups always granted, bridge-coin
roadblocks always in-logic via single-coin grinding), the actionable list
shrank from ~170 raw rows to the set captured below. This is a **current-state**
record — it describes how the logic stands now, not the order it got there.

---

## Badge logic — the rule and the three walls

**Rule:** *a level that **grants** a badge **requires** that badge in logic.* AP
is the sole badge authority (the in-game grant is reverted by the forced-death /
M5 path), so the badge must come from AP before you can clear the level that
would have handed it to you. **Exception:** badges handed over in the
**overworld** rather than inside the course — only **Sensor** (given before W5
Upshroom Downshroom), which stays ungated (PR #116). All 23 badge-granting
levels carry their badge requirement at the **location** layer.

**Progression walls.** Wonder Seeds are pool items and AP fill never strands a
required item behind a badge gate it can't open first, so a badge level that is
an **optional side spur** is already safe (all 18 badge-challenge "I/II"
levels). A badge level that is a **forced progression wall** (must be cleared to
advance the world) is *not* safe with only a location requirement — the
seeds-only region graph can't see the wall, so fill can bury the badge in a
later world and softlock the seed (observed: Parachute Cap placed in W6 while the
Pipe-Rock Badge House blocked all of W1). These three walls are therefore gated
at the **region** layer in `regions.json` and pinned by
`tests/test_data_validation.py::test_progression_wall_badges_gate_regions`:

| Region gate | Wall (level) | Grants |
|---|---|---|
| `W1 3 Seeds` += `\|Parachute Cap Badge\|` | Badge House in Pipe-Rock Plateau | Parachute Cap |
| `W1 10 Seeds` += `\|Auto Super Mushroom Badge\|` | Wiggler Race Mountaineering! | Auto Super Mushroom |
| `W3 4 Seeds` += `\|Crouching High Jump Badge\|` | POOF! Crouching High Jump I | Crouching High Jump |

Verified against the Super Mario Wiki: Mountaineering's vanilla unlock set
(*Swamp Pipe Crawl, Angry Spikes, Bulrush Express, Wall-Climb Jump I, Pipe-Rock
Plateau Palace, KO Pipe-Rock Rumble*) is exactly the `W1 10 Seeds` region, and
Crouching High Jump I is *"the only Badge Challenge required to complete the
game"* (it unlocks The Midway Trial → … → Royal Seed Mansion). There are **no
other Badge House levels** in the game. **Do not re-strip these** — they were
once removed as "spurious" under the older "badges auto-present" assumption,
which caused the softlock.

Verified: standard-mode (`open_world=0`) generation fills and is beatable across
25 seeds; full apworld logic suite green.

---

## Region-gate facts (regions.json)

- `W1 3 Seeds` / `W1 10 Seeds` — gated on Parachute Cap / Auto Super Mushroom
  (the badge walls above), in addition to the Wonder-Seed tolls.
- `W3 Start`: `|Petal Isles Wonder Seed:8|` only (the Hoppycat / Anglefish
  trials are **not** behind a Dolphin Kick branch).
- `W3 4 Seeds` — gated on Crouching High Jump (the badge wall above) plus
  `|W3 Wonder Seed:4|`.
- `World Bowser`: `|@Royal Seed:6|` only (Bowser unlock is 6 Royal Seeds; the
  old `W4/W5` Wonder-Seed extras were wrong). Direct `W6 Start → World Bowser`
  edge exists.
- W6 palace is the **25-seed** gate: the palace location group lives in
  `W6 25 Seeds` (there is no `W6 15 Seeds` region).
- `PI 5 Seeds` and `PI 8 Seeds` exist and home the PI-seed-gated levels
  (Downpour Uproar / Wiggler Race Swimming / Dolphin Kick II → 5; Jewel-Block
  Cave / Gnawsher Lair / Maw-Maw Mouthful / Muncher Fields / Wiggler Race
  Spelunking / Petal Isles Poplin Shop 2 / KO Petal Meddle / Petal Isles Flying
  Battleship / Boosting Spin Jump I / Way of the Goomba → 8).
- `W6 Post-Spring`: no Spring Feet requirement (Solar Roller needs only Jet Run
  + Invisibility).
- `W1 Post-Bulrush Express`: `|Elephant Fruit| OR |Drill Mushroom|` (Drill is a
  valid alternative to Elephant).

## Structural

- `W6 25 Seeds` requires `|W6 Wonder Seed:25|`.
- `Post-Badge` (Badge Marathon roadblock) requires all-seed-counts + Royal
  Seeds: `|W1 Wonder Seed:14| AND |W2 Wonder Seed:14| AND |W3 Wonder Seed:10|
  AND |W4 Wonder Seed:15| AND |W5 Wonder Seed:11| AND |W6 Wonder Seed:25| AND
  |Petal Isles Wonder Seed:15| AND |Special World Wonder Seed:16| AND
  |@Royal Seed:6|`.

## Check-kind behavior

- **KO Arenas** (W1 Pipe-Rock Rumble, W2 Fluff-Puff Kerfuff, W4 Sunbaked
  Skirmish, W5 Fungi Funk, W6 Magma Flare-Up, PI Petal Meddle) have no real
  exit — clearing the arena *is* the Wonder Seed grab. Each has only the
  per-arena Wonder Seed check plus its 3× 10-Coin checks (no Normal-Exit check).
- **Secret-flag tops**: topping either the normal-exit or secret-exit flagpole
  fires the same per-course `TOP_OF_FLAG` check (no separate
  `TOP_OF_SECRET_FLAG` kind).

---

## What was NOT changed (per user filters)

- ~60 Wonder-flower disagreements (Wonder Effects always granted).
- ~40 power-up gates on 10-Coins (power-ups always granted; `Mushroom+` / `Star`
  aren't items, so they're moot).
- 50-Flower-Coin "bridge" roadblocks (always in-logic via single-coin grinding).
- Hidden Character Block locations — skipped per user.

## Notes / known limitations

- **Top of Fake Flag (Wubba Ruins)**: a separate "Top of Fake Flagpole" check
  the PDF lists (rewards from Add ! Blocks badge or any Yoshi). Modeling it would
  need a new `CheckKind.TOP_OF_FAKE_FLAG` + processor support for
  `goal_id == 2 && touch_goal_top`. Not added.
- **"Way of the Goomba" ← Wiggler Race - Spelunking prerequisite**: the PDF says
  Way of the Goomba is unlocked by *clearing* Wiggler Race - Spelunking!, not
  just by reaching the 8-PI-seed gate. Approximated by co-locating both in
  `PI 8 Seeds`. Exact modeling would need a custom rule hook or a synthetic
  "Spelunking Cleared" item.
- **Confirmed secret exits**: `W3: Royal Seed Mansion - Secret Exit` and
  `W5: Operation Poplin Rescue - Secret Exit` are real in-game secret exits (each
  unlocks a Special World level). Correctly modeled — keep.
- **FFP Cabin "13+ characters"**: those character blocks (14 total) belong to
  *Search Party - Puzzling Park*, not Fluff-Puff Peaks Cabin. Falls under the
  skipped "Hidden Character Blocks" category.
- **Captain Toad cross-world locations**: skipped per user.

## General audit follow-up

The progression-wall fix was scoped to *badge* levels. The same softlock class
can exist for any **non-badge forced level** that blocks the only path but is
modeled as seeds-only. A general wall audit (re-derive each world's forced path
from the game/PDF, diff against the seed-toll region graph) is the recommended
next deep pass.

---

## Sources

- [How to Reach Castle Bowser — Nintendo Supply](https://nintendosupply.com/articles/how-to-reach-bowsers-castle-in-super-mario-bros-wonder)
- [Royal Seed — Super Mario Wiki](https://www.mariowiki.com/Royal_Seed)
- [List of All Badge Challenges — Game8](https://game8.co/games/Super-Mario-Bros-Wonder/archives/432051)
- [Royal Seed Mansion (secret exit) — Super Mario Wiki](https://www.mariowiki.com/Royal_Seed_Mansion)
- [Operation Poplin Rescue (secret exit) — Super Mario Wiki](https://www.mariowiki.com/Operation_Poplin_Rescue)
- [Search Party Puzzling Park — Super Mario Wiki](https://www.mariowiki.com/Search_Party_Puzzling_Park)
- [Wiggler Race Mountaineering! — Super Mario Wiki](https://www.mariowiki.com/Wiggler_Race_Mountaineering!) (wall: unlocks the W1 10-Seeds cluster + palace)
- [POOF! Badge Challenge Crouching High Jump I — Super Mario Wiki](https://www.mariowiki.com/POOF!_Badge_Challenge_Crouching_High_Jump_I) ("the only Badge Challenge course required to complete the game")
- [Badge House in Pipe-Rock Plateau — Super Mario Wiki](https://www.mariowiki.com/Badge_House_in_Pipe-Rock_Plateau) (only Badge House level in the game)
