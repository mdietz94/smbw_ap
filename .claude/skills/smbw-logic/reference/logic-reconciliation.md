# SMBW Archipelago — logic reconciliation record

The apworld logic was diffed across multiple rounds against the
community-maintained logic PDF. After applying user-confirmed filters
(Wonder Effects always granted, all power-ups always granted, bridge-coin
roadblocks always in-logic via single-coin grinding), the actionable list
shrank from ~170 raw rows to the set captured below. This is a **current-state**
record — it describes how the logic stands now, not the order it got there.

---

## Badge logic — the rule and the three walls

**Rule (badge challenges are PER-CHECK).** A Badge Challenge course (the
self-themed *"X I"/"X II"* levels) is *not* uniformly gated on its badge — the
badge is practice content, and most courses are **completable without it**. So
each check is judged on its own (maintainer scope, PR #137 + follow-up player
reports):

- **10-Coin checks → require the badge, except a vetted open set.** The
  badge-themed coins are the badge-practice collectibles; player-confirmed
  (*"Dolphin Kick I is completable without the badge, but all the 10 coins
  require it"*). This is also the safe default — leaving them open let fill bury
  a needed Wonder Seed behind a badge the player never had (the reported
  softlock). **Exception — `_OPEN_COIN_LEVELS`:** **Parachute Cap I** — all 10
  coins are player-confirmed obtainable with **nothing** (no badge, no Yoshi), so
  its coins are open. Pinned by `test_badge_challenge_coins_require_their_badge`
  (respects the exemption) + `test_open_coin_levels_stay_open`.
- **Normal Exit / Top of Flag → require the badge ONLY where it is
  *structurally* required to reach the goal** (maintainer-confirmed). The
  structural courses are **Wall-Climb Jump**, **Grappling Vine**, **Boosting
  Spin Jump**, **Floating High Jump**, **Crouching High Jump**, **Dolphin Kick**,
  and **Jet Run** (both I & II of each) — you can't climb / swing / spin / float
  / crouch-jump / dolphin-kick / jet-dash to the exit without the ability
  (Dolphin Kick I/II + Jet Run I player-confirmed; Jet Run II gated by extension
  for safety). The remaining courses — **Spring Feet, Invisibility, Parachute
  Cap** (I & II) — are completable without the badge, so their completion checks
  stay **open**. Pinned by `test_structural_badge_levels_gate_completion` /
  `test_nonstructural_badge_completion_stays_open`
  (`_STRUCTURAL_BADGE_LEVELS` is the source of truth).

> If a course currently in the "open" set is later reported as uncompletable
> without its badge, add it to `_STRUCTURAL_BADGE_LEVELS` and gate its Normal
> Exit / Top of Flag.

**Yoshi bypasses the movement badge.** A Yoshi (any of the four; category
`Yoshi`) can climb/float through a structural challenge without the badge, so
**Wall-Climb Jump I/II** and **Floating High Jump I** carry `... OR |@Yoshi:1|`
on **every** check (completion + coins) — player-confirmed. The badge token is
kept alongside (the pin tests only require its presence), and the `|@Yoshi:1|`
category counts the four Yoshi items (Green/Red/Light-Blue/Yellow) but **not**
Nabbit. Add the same `OR |@Yoshi:1|` if more Yoshi-clearable challenges surface.

**NON-challenge badge levels — mostly require the badge** (these *grant* the
badge and AP is the sole authority): **Badge House**/Parachute Cap, **Ninji Jump
Party**/Rhythm Jump, **WONDER?**/Sound Off?, plus the overworld **Sensor**
exception (ungated, PR #116). **Mountaineering!** (grants Auto Super Mushroom) is
**open** — the Wiggler race needs no badge to clear, so its Normal Exit no longer
requires ASM (player-confirmed "should be in logic"). The ASM badge stays a
progression pool item but now gates nothing.

**Progression walls — a *separate*, region-layer concern.** Beyond the
per-check rule above, a region-layer badge gate is correct **only when a level
you MUST clear to advance the world also REQUIRES that badge to clear it.** The
seeds-only region graph can't see such a wall, so without the explicit gate fill
can bury the badge in a later world and softlock the seed. Pinned by
`tests/test_data_validation.py::test_progression_wall_badges_gate_regions`:

| Region gate | Wall (level) | Why it qualifies |
|---|---|---|
| `W3 4 Seeds` += `\|Crouching High Jump Badge\|` | POOF! Crouching High Jump I | **Structural** challenge course (can't reach goal without the badge) **and** forced — wiki: *"the only Badge Challenge required to complete the game"* (unlocks the W3 Royal Seed Mansion path). |

**Two former "walls" that were NOT real (removed):**

- **`W1 3 Seeds` / Parachute Cap** (Badge House in Pipe-Rock Plateau) — *removed,
  player-confirmed you don't need Parachute Cap to progress W1.* The Badge House
  just hands the badge over; clearing it needs no badge, and no other forced W1
  level needs the parachute. The old "Parachute Cap landed in W6 blocked all of
  W1" diagnosis was wrong — W1 Wonder Seeds come from mainline levels that don't
  need it.
- **`W1 10 Seeds` / Auto Super Mushroom** (Wiggler Race Mountaineering!) —
  *removed, player-confirmed.* Same pattern: a race grants the badge but needs
  no badge to clear, and nothing else requires ASM.

The distinction: Crouching High Jump I is a *badge challenge course* that is
itself unclearable without its badge; the Badge House / Wiggler Race merely
*grant* a badge and are clearable without it. The badge each grants is still
required at its own location checks (Badge House Normal Exit, the Parachute Cap
/ Crouching High Jump challenge courses) — fill-safe side spurs.

**Open-world exception (the remaining region walls only).** Open-world mode opens
every course node from the start, so the *forced-wall* premise doesn't hold there
— the world-map Wonder-Seed bar is the only gate, and a player with enough Wonder
Seeds reaches the next section without the badge (player-reported: "second
section of W1 accessible with enough seeds but not in logic"). So in open-world
the **region**-layer badge half of the remaining gates is dropped (the
Wonder-Seed toll stays); the **location**-layer badge requirement on the
challenge courses is kept (the badge is still needed to clear the course
itself). Implemented in
`open_world.strip_badge_requirement` / `badge_wall_open_world_requires`, swapped
in by `hooks/World.before_set_rules` alongside the existing `W{n} Start`/
`World Bowser` neutralization. Standard mode is unchanged.

Verified: standard-mode (`open_world=0`) generation fills and is beatable across
30 seeds; open-world keeps the seed toll while dropping the badge wall; full
apworld logic suite green.

---

## Region-gate facts (regions.json)

- `W1 3 Seeds` / `W1 10 Seeds` — Wonder-Seed toll only (`|W1 Wonder Seed:3|` /
  `|W1 Wonder Seed:10|`); the Parachute Cap and Auto Super Mushroom walls were
  both removed (not real walls — see Progression walls above).
- `W3 Start`: `|Petal Isles Wonder Seed:8|` only (the Hoppycat / Anglefish
  trials are **not** behind a Dolphin Kick branch).
- `W3: The Anglefish Trial` 10-Coins: **#3** is the Elephant-Fruit coin (its
  requirement was previously on **#1** — swapped to match the game,
  player-reported). #1 / #2 are open (they only ever gated on a button, now
  removed — see Buttons removed below).
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
  **World-progress gates (added):** Petal Isles is the hub — its islands open
  by clearing the prior world's **final palace level**, *not* by collecting PI
  Wonder Seeds. The PI-depth spur branches off the **pre-W2** hub node, so gating
  it only on `|Petal Isles Wonder Seed:N|` put all ~58 PI-depth checks in logic
  with zero World 2 progress (fidelity bug + fill could bury a progression item
  there).
  ⚠️ **Do NOT gate on the world's Royal Seed.** Royal Seeds are AP **pool items**
  placed anywhere, so `|W2 Royal Seed|` only means "AP granted it", not "you
  cleared World 2" — it doesn't model the in-game unlock (an earlier pass used
  the Royal Seed and was wrong). The correct gate is the *condition to play that
  world's final level*: in the seed-toll model that's the Wonder-Seed count to
  reach the palace region, plus any wall on the way. Each region's `requires`:
  - `PI 5 Seeds`: `|W2 Wonder Seed:14| AND |Petal Isles Wonder Seed:5|` — Wiggler
    Race Swimming! unlocks after clearing **Fluff-Puff Peaks Palace**, which lives
    in `W2 14 Seeds` (toll `|W2 Wonder Seed:14|`).
  - `PI 8 Seeds`: `|W3 Wonder Seed:10| AND |Crouching High Jump Badge| AND
    |Petal Isles Wonder Seed:8|` — Jewel-Block Cave unlocks after the **Shining
    Falls Royal Seed Mansion**, which lives in `W3 10 Seeds` (toll
    `|W3 Wonder Seed:10|`); reaching it must clear the W3 wall POOF! Crouching
    High Jump I, so the badge is part of "can play W3's finale".
  A region's `requires` is the access rule for the **checks inside it** (each
  location's rule = its own region's requires), so these tokens keep the PI-depth
  checks out of logic until you could actually play the gating world's finale,
  even though the regions have no outgoing edges. Pinned by
  `tests/test_data_validation.py::test_petal_isles_depth_requires_world_completion`.
  Sources: MarioWiki Wiggler Race Swimming!, Jewel-Block Cave, Royal Seed
  Mansion, Petal Isles Flying Battleship pages.
- `W6 Post-Spring`: no Spring Feet requirement (Solar Roller needs only Jet Run
  + Invisibility).
- `W1 Post-Bulrush Express`: `|Elephant Fruit| OR |Drill Mushroom|` (Drill is a
  valid alternative to Elephant).
- `W1: Bulrush Express - Secret Exit`: **no** `|Elephant Fruit|` requirement —
  the secret exit is reachable without it (player-confirmed). Only the button /
  Wonder-Flower helpers remain.
- `W1: Sproings in the Twilight Forest - 10 Coin #1`: **open** (`requires: []`) —
  player-confirmed obtainable with nothing; the old `|Elephant Fruit|` gate was
  wrong.
- `Pre-W4 Special` (holds **The Semifinal Test: Piranha Plant Reprise**, its only
  course): re-gated to just `|Special World Wonder Seed:6|` and re-anchored off
  the **`PI Pre-W2`** hub node (was hung off `PI Pre-W4`, i.e. behind the whole
  W3 chain + a large Wonder-Effect list). Player-confirmed: reachable with 6
  Special-World Wonder Seeds after clearing *Climb to the Beat*, well before W3's
  finale. It stays an `_EXTRA_HUB_REGIONS` member so open-world still strips it.
  The Special World is otherwise still modelled by placing each special course in
  a world-progression region (see the Notes limitation) — Piranha Plant Reprise
  is the one course re-pointed at its true Special-seed gate; a full
  Special-World remodel is a follow-up.

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
  **All four checks require `|@Power-Up:1|`** — the fight is out of logic
  without at least one Power-Up (player-reported: hard/unfair power-up-less).
  Pinned by `test_ko_arenas_require_a_powerup`. (Power-Ups became real pool
  progression items in M3.1 / `powerup_gating`, so "any power-up" is a real
  gate, not a no-op.)
- **All-Power-Up badges count as Power-Ups.** Equipping an *All &lt;X&gt; Power*
  badge keeps you in that form permanently, so the four badges (**All Elephant /
  Fire / Bubble / Drill Power**) are now `progression: true` and carry the
  `Power-Up` category **in addition to** `Badge` — that makes any of them satisfy
  `|@Power-Up:1|` (KO arenas). For a *specific* power-up gate, the matching badge
  is OR'd in at every site: `|Elephant Fruit|` → `(|Elephant Fruit| OR |All
  Elephant Power Badge|)`, and likewise Bubble/Drill (Fire has no bare-token
  gate). Applied to both `locations.json` and `regions.json` (player-requested).
- **Secret-flag tops**: topping either the normal-exit or secret-exit flagpole
  fires the same per-course `TOP_OF_FLAG` check (no separate
  `TOP_OF_SECRET_FLAG` kind).

---

## Buttons removed (`button_shuffle` dropped entirely)

The `button_shuffle` feature — four progression items **Y Button / R Button /
Up / ZL Button/Down** (the `Button` category, gated by the hidden-and-locked
`button_shuffle` option) — was **removed wholesale** (player-requested). It was
always a `_LockedOffToggle` (never in the pool), so with the option off every
`{OptOne(|<X> Button|)}` token already evaluated to TRUE (`|X:0|`). Removal makes
that permanent:

- **Items** (`items.json`): the four Button items deleted.
- **Category** (`categories.json`): `Button` entry deleted; **Options.py**:
  `button_shuffle` dropped from `_DEFERRED_OPTIONS` (the option is no longer
  generated).
- **Logic** (`locations.json` / `regions.json`): every `{OptOne(|Y Button|)}` /
  `{OptOne(|R Button|)}` / `{OptOne(|Up|)}` / `{OptOne(|ZL Button/Down|)}` token
  was substituted with TRUE and the boolean expression simplified (equal-
  precedence, left-associative `AND`/`OR`, matching `Rules.py`). A clause that
  collapsed to always-true became `""`. **No logic change for anyone playing the
  default** (button_shuffle was off ⇒ these were already TRUE); only the ability
  to turn the feature on is gone. Non-button `{OptOne(...)}` tokens (Wonder
  Effects / Wonder Flower / characters) are untouched.

---

## What was NOT changed (per user filters)

- ~60 Wonder-flower disagreements (Wonder Effects always granted).
- ~40 power-up gates on 10-Coins (power-ups *were* treated as always granted).
  ⚠️ **Now partially revisited:** Power-Ups are real pool items (M3.1 /
  `powerup_gating`), and the KO-arena ("Rumble") checks are now gated on
  `|@Power-Up:1|` (see Check-kind behavior). The remaining per-coin power-up
  gates the PDF lists are still NOT applied (would need per-coin power-up data);
  audit candidate if more "needed a power-up" reports come in.
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
  unlocks a Special World level). Correctly modeled — keep. `requires: []` is
  correct: no *item* gates them (the secret path spawns on replay of a cleared
  course, gated on the transient `IsInClearedCourse` flag, not on anything the
  player carries). In open-world that flag is never set naturally; the Switch
  force-writes it (in **both** modes as of 2026-07-01 — necessary in open-world,
  safe-if-redundant in standard) — see
  [docs/handoff-2026-07-01-open-world-secret-exit.md](../../../../docs/handoff-2026-07-01-open-world-secret-exit.md)
  (PR #158) and memory `smbwap-secret-exit-isinclearedcourse`. No logic change
  needed.
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
