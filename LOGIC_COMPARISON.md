# SMBW Archipelago Logic Comparison — Resolution Summary

This was a multi-round diff against the community-maintained logic PDF.  After applying user-confirmed filters (Wonder Effects always granted, all power-ups always granted, ~~badges always available within their own badge-challenge levels~~ — *see the 2026-06-06 banner below; this filter was reversed*, bridge-coin roadblocks always in-logic), the actionable list shrank from ~170 raw rows to ~25 real fixes — all of which are now applied.

---

> ## ⚠️ 2026-06-06 — Badge philosophy reversed + progression-wall gates added
>
> **Old rule (rounds 1–N):** *"badges are auto-present within their own
> badge-challenge level"* → a level was **never** gated on the badge it teaches.
> Under that rule we stripped several `|… Badge|` requirements as "spurious"
> (see the annotated entries below).
>
> **New rule:** **a level that *grants* a badge requires that badge in logic.**
> AP is the sole badge authority (M5 suppresses the in-game grant), so you must
> receive the badge from AP *before* you can clear the level that would have
> handed it to you. *Exception:* badges handed over in the **overworld** rather
> than inside the course — only **Sensor** (given before W5 Upshroom Downshroom),
> which therefore stays ungated (see PR #116). Every one of the 23
> badge-granting levels already carries its badge requirement at the
> *location* layer; that part needed no change.
>
> **Why a *location* requirement isn't always enough — the softlock.** Wonder
> Seeds are **pool items**, and AP's fill never strands a required item behind a
> badge gate it can't open first — so a badge-granting level that is an
> *optional side spur* is already safe (all 18 "I/II" badge-challenge levels).
> The danger is a badge-granting level that is a **progression wall**: a node you
> must clear to physically advance the world. The seeds-only region graph can't
> see those walls, so fill happily buried **Parachute Cap in W6** while the
> Pipe-Rock Badge House blocked all of W1 — an unwinnable seed.
>
> **The three progression walls (verified vs Super Mario Wiki) — now gated at
> the region layer in `regions.json`, pinned by
> `tests/test_data_validation.py::test_progression_wall_badges_gate_regions`:**
>
> | Wall (level) | World | Grants | Region gate added |
> |---|---|---|---|
> | Badge House in Pipe-Rock Plateau | W1 | Parachute Cap | `W1 3 Seeds` += `|Parachute Cap Badge|` |
> | Wiggler Race Mountaineering! | W1 | Auto Super Mushroom | `W1 10 Seeds` += `|Auto Super Mushroom Badge|` |
> | POOF! Badge Challenge Crouching High Jump I | W3 | Crouching High Jump | `W3 4 Seeds` += `|Crouching High Jump Badge|` |
>
> Mountaineering's vanilla unlock set (*Swamp Pipe Crawl, Angry Spikes, Bulrush
> Express, Wall-Climb Jump I, Pipe-Rock Plateau Palace, KO Pipe-Rock Rumble*)
> exactly equals the `W1 10 Seeds` region, and Crouching High Jump I is the
> Mario-Wiki-confirmed *"only Badge Challenge required to complete the game"*
> (it unlocks The Midway Trial → … → Royal Seed Mansion in `W3`). All other
> badge-granting levels are optional side spurs (fill-safe) — there are **no
> other Badge House levels** in the game.
>
> Verified: standard-mode (`open_world=0`) generation fills and is beatable
> across 25 seeds; full apworld test suite green.

---

## What was fixed

### Missing locations (2 new entries, both wired through the in-game hook)
~~6 KO Arena "Normal Exit" / Level-Clear entries~~ — **reverted 2026-05-29**. KO Arenas (W1 Pipe-Rock Rumble, W2 Fluff-Puff Kerfuff, W4 Sunbaked Skirmish, W5 Fungi Funk, W6 Magma Flare-Up, PI Petal Meddle) don't have a real exit — clearing the arena is the Wonder Seed grab itself. Only the per-arena Wonder Seed (and the existing 3× 10-Coin) checks remain.

7 "Top of Secret Flag" entries (W1 Piranha Plants, Bulrush Coming Through, Bulrush Express; W2 Outmaway Valley; W4 Shova Mansion; W6 Where the Rrrumbas Rule, Hot-Hot Hot) were briefly added as a separate `CheckKind.TOP_OF_SECRET_FLAG`, then **collapsed back into `TOP_OF_FLAG`** — topping either the normal-exit or secret-exit flagpole now fires the same per-course `TOP_OF_FLAG` check.

### Region-gate bugs (regions.json)
- `W1 3 Seeds`: dropped spurious `|Parachute Cap Badge|` (was gating 5 normal levels behind a badge they don't need). **↳ REVERSED 2026-06-06 (see banner): the Pipe-Rock Badge House IS a progression wall; `|Parachute Cap Badge|` was re-added to `W1 3 Seeds`, and `|Auto Super Mushroom Badge|` (Mountaineering wall) added to `W1 10 Seeds`.**
- `W3 Start`: simplified to just `|Petal Isles Wonder Seed:8|` (dropped the Dolphin Kick Badge branch that gated the Hoppycat / Anglefish trials).
- `W3 4 Seeds`: dropped spurious `|Crouching High Jump Badge|` (was gating Midway / Sharp / Sugarstar Trials). **↳ REVERSED 2026-06-06 (see banner): POOF! Crouching High Jump I is the one mandatory badge challenge — it gates The Midway Trial → Royal Seed Mansion; `|Crouching High Jump Badge|` was re-added to `W3 4 Seeds`.**
- W6 Palace location group moved from `W6 15 Seeds` to `W6 25 Seeds` (PDF: palace is the 25-seed gate, not 15).
- `World Bowser`: simplified to `|@Royal Seed:6|` (verified online: Bowser unlock is 6 Royal Seeds only; the old `|W4 Wonder Seed:15| AND |W5 Wonder Seed:11|` extras were wrong).  Added direct `W6 Start → World Bowser` connection.
- `W6 15 Seeds` region removed (no longer used after palace move).
- Added `PI 5 Seeds` and `PI 8 Seeds` regions and re-homed the PI-seed-gated levels (Downpour Uproar / Wiggler Race Swimming / Dolphin Kick II → 5; Jewel-Block Cave / Gnawsher Lair / Maw-Maw Mouthful / Muncher Fields / Wiggler Race Spelunking / Petal Isles Poplin Shop 2 / KO Petal Meddle / Petal Isles Flying Battleship / Boosting Spin Jump I → 8; Way of the Goomba → 8).
- `W6 Post-Spring`: dropped spurious `|Spring Feet Badge|` (Solar Roller needs only Jet Run + Invisibility per PDF).
- `W1 Post-Bulrush Express`: added the Drill alternative to Elephant (`|Elephant Fruit| OR |Drill Mushroom|`) to match PDF.

### Structural bugs
- `W6 25 Seeds` requires: rewritten from a malformed paren-unbalanced AND/OR string to just `|W6 Wonder Seed:25|`.
- `Post-Badge` requires: rewritten to model the Badge Marathon roadblock as all-seed-counts + Royal Seeds: `|W1 Wonder Seed:14| AND |W2 Wonder Seed:14| AND |W3 Wonder Seed:10| AND |W4 Wonder Seed:15| AND |W5 Wonder Seed:11| AND |W6 Wonder Seed:25| AND |Petal Isles Wonder Seed:15| AND |Special World Wonder Seed:16| AND |@Royal Seed:6|`.

### Test updates
3 processor tests updated to reflect the dual-emit behavior (`SECRET_EXIT` + `TOP_OF_FLAG` for secret-flag tops).  All 411 client tests pass.

---

## What was NOT changed (per user filters)

- ~60 Wonder-flower disagreements (Wonder Effects always granted)
- ~40 power-up gates on 10-Coins (power-ups always granted; `Mushroom+` and `Star` don't exist as items but are moot)
- ~~Badges within badge-challenge levels (auto-present)~~ — **superseded 2026-06-06 (see top banner): a level that grants a badge now requires it; the three progression-wall levels are gated at the region layer.**
- 50-Flower-Coin "bridge" roadblocks (always in-logic via single-coin grinding)
- Hidden Character Block locations — skipped per user

---

## Notes / known limitations

- **Top of Fake Flag (Wubba Ruins)**: the Wubba Ruins level has a separate "Top of Fake Flagpole" check that the PDF lists (rewards from Add ! Blocks badge or any Yoshi).  Mirroring the secret-flag work would need a new `CheckKind.TOP_OF_FAKE_FLAG` and processor-side support for `goal_id == 2 && touch_goal_top`.  Not added — user only requested Top-of-Secret-Flag fixes.
- **"Way of the Goomba" Wiggler Race - Spelunking prerequisite**: PDF says Way of the Goomba is unlocked specifically by *clearing* Wiggler Race - Spelunking!, not just by reaching the 8-PI-seed gate.  Our model approximates this by co-locating both in `PI 8 Seeds`.  Modeling the level-clear prerequisite exactly would need either a custom rule hook or a synthetic "Spelunking Cleared" item that the location yields and the gate checks.
- **Confirmed secret exits**: both `W3: Royal Seed Mansion - Secret Exit` and `W5: Operation Poplin Rescue - Secret Exit` are real in-game secret exits (each unlocks a Special World level).  They are correctly modeled — keep as-is.
- **FFP Cabin "13+ characters"**: those character blocks (4 Peach + 5 Nabbit + 3 Yellow Yoshi + 1 Yellow Toad + 1 Luigi = 14 total) belong to *Search Party - Puzzling Park*, not Fluff-Puff Peaks Cabin (verified via Mario Wiki).  Falls into the skipped "Hidden Character Blocks" category.
- **Captain Toad cross-world locations**: skipped per user.

---

## Sources

- [How to Reach Castle Bowser — Nintendo Supply](https://nintendosupply.com/articles/how-to-reach-bowsers-castle-in-super-mario-bros-wonder)
- [Royal Seed — Super Mario Wiki](https://www.mariowiki.com/Royal_Seed)
- [List of All Badge Challenges — Game8](https://game8.co/games/Super-Mario-Bros-Wonder/archives/432051)
- [Royal Seed Mansion (secret exit) — Super Mario Wiki](https://www.mariowiki.com/Royal_Seed_Mansion)
- [Operation Poplin Rescue (secret exit) — Super Mario Wiki](https://www.mariowiki.com/Operation_Poplin_Rescue)
- [Search Party Puzzling Park — Super Mario Wiki](https://www.mariowiki.com/Search_Party_Puzzling_Park)
- [Wiggler Race Mountaineering! — Super Mario Wiki](https://www.mariowiki.com/Wiggler_Race_Mountaineering!) (progression-wall verification: unlocks the W1 10-Seeds cluster + palace)
- [POOF! Badge Challenge Crouching High Jump I — Super Mario Wiki](https://www.mariowiki.com/POOF!_Badge_Challenge_Crouching_High_Jump_I) ("the only Badge Challenge course required to complete the game")
- [Badge House in Pipe-Rock Plateau — Super Mario Wiki](https://www.mariowiki.com/Badge_House_in_Pipe-Rock_Plateau) (only Badge House level in the game)
