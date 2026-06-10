# Badge-gated course entry — feasibility findings + implementation (2026-06-10)

## The ask

Gate a course's **entry** on possessing a specific badge, for **arbitrary**
courses. The user's concrete idea: when the player lacks the required badge,
make that course's Wonder-Seed entry requirement too high to enter, and relax it
once the badge is acquired.

## Feasibility verdict

| Layer | Per-course badge gate to arbitrary courses? | Why |
|---|---|---|
| **Live game (runtime)** | **No** | The per-course unlock threshold is static BYML, not a runtime-writable gmd field. |
| **Randomizer logic (apworld)** | **Yes** | Badge → course requirement is already the established mechanism; made configurable for arbitrary courses. |

**Net: PARTIAL — and the partial that's achievable is exactly the part that
matters for a randomizer.** The literal "inflate the per-course Wonder-Seed
requirement at runtime" lever does **not** exist. The closest feasible (and
randomizer-correct) design is to enforce the badge→course constraint in the
multiworld **logic**, which is what was implemented.

## Why the live per-course lever doesn't exist

Two distinct threshold mechanisms live in the world-map data, and **both are
static RomFS/BYML**, not runtime gmd fields:

- **Per-course** — `WorldMapInfo00N.json` `CourseTable[].CourseOpenCondition`.
  Inspecting the extracted RomFS (`C:\Users\maxwe\Documents\smbw_re_tmp\`), the
  condition types are `ClearLinkedCourse` / `ClearAppointedCourse` /
  `GoldenPropellerFlower` / `PowerJewelNum` (and `None`). There is **no
  `WonderSeedNum` per-course field on W1** and, crucially, no gmd hash backing
  any of these — they are baked descriptor fields with no static xref to a
  writable container key.
- **Per-area gate** — `WorldMapInfo00N.json`
  `GateTable[].GateOpenCondition.NeedNumOfWonderSeed` (e.g. Petal Isles gates at
  5/8/10/12/15). Also static BYML.

The only runtime lever the mod has over Wonder-Seed gating is the
**AP-authoritative Wonder-Seed count** we feed the game via
`probe::pushWonderSeedOverride` / `SetWonderSeedCountsMsg`. That predicate
(`WonderSeedRouteGatePredicate`, NSO `+0x1787b40`, reading container-A hash
`0x390eb960`) **recomputes from the count on each map load** and gates at
**whole-world / area** granularity. So:

- We can make a whole world's gates **pass** by inflating its count (shipped).
- The inverse — withholding the count to keep gates **closed** — would lock an
  entire **area**, not a single course. That is the granularity wall. There is
  no per-course count to inflate/withhold.

(Cross-checked against `smbw-re-map.md` §5 / §11: the route gate is count-driven
and recomputed; the FlowerLock route lock list is a BYML `"FlowerLock"` param,
"not an addressable gmd hash".)

## What was implemented (logic side)

The randomizer-correct way to express "you can't enter course X until you have
badge B" is to make badge B a **logical requirement** of every check in course
X. AP fill then never expects the player to enter/clear that course before the
badge item is collected — exactly the user's intent for a randomizer. This
mechanism already exists in the apworld (99 locations + 3 region walls gate on
badges); this change makes it **configurable for arbitrary courses**.

New option **`badge_gated_courses`** (`OptionDict`, in `hooks/Options.py`):

```yaml
badge_gated_courses:
  "W6: Hot-Hot Hot!": "Spring Feet Badge"
  "W2: Outmaway Valley": ["Dolphin Kick Badge", "Floating High Jump Badge"]
```

- **Key** = the course name = the location-name prefix before `" - "` (e.g.
  `"W1: Piranha Plants on Parade"`, not `"... - Normal Exit"`). One key gates
  the whole course (every check: Normal/Secret Exit, Wonder Seed, Top of Flag,
  10-Coins).
- **Value** = a single badge item name, or a list (a list means **all** of them
  are required — AND).
- Applied at gen time in `after_set_rules` (`hooks/World.py
  ::apply_badge_gated_courses`) via `add_rule`, which ANDs the badge requirement
  onto each of the course's locations.
- Unknown course or badge names abort generation with a clear `ValidationError`
  (typos caught immediately).

### Why this is fill-safe for arbitrary courses

Badge items are `progression: true` and already in the pool, so
`distribute_items_restrictive` places the badge somewhere reachable **before**
it needs the gated course. A gated course that is *also* a forced progression
wall whose seeds are required is already separately covered by the region-layer
walls in `regions.json` (the documented Parachute/Auto-Mushroom/Crouching-High-
Jump walls). Layering a user gate on top only adds an extra progression item the
fill must satisfy — it cannot strand a required item behind a gate it can't open
first (this is the same invariant the existing badge gating relies on).

## Files changed

- `apworld/smbw_archipelago/hooks/Options.py` — new `BadgeGatedCourses`
  `OptionDict` + registration.
- `apworld/smbw_archipelago/hooks/World.py` — `_course_prefix`,
  `apply_badge_gated_courses`, and a call in `after_set_rules`.
- `apworld/smbw_archipelago/tests/test_badge_gated_courses.py` — new (7 tests).
- `docs/badge-gated-entry-findings-2026-06-10.md` — this doc.

No switch-mod / client change was needed: AP is already the sole badge
authority (in-game grants are reverted by the badge-sync / M5 path), so a
logic-level requirement is sufficient and there is no live game-side change to
test.

## Tests

- New: `tests/test_badge_gated_courses.py` — 7 tests (block-without /
  open-with-badge, ungated-course-untouched, multi-badge AND, fill+beatable,
  empty-noop, unknown-course-aborts, unknown-badge-aborts). All pass.
- Logic suite (`test_data_validation.py` + `test_open_world_gen.py`): 18 pass,
  unchanged.
- Full apworld suite: **725 passed, 2 skipped**.

## Limitations / no live test needed

- **No single-course *live* entry lock exists.** This option is logic-only; it
  governs what AP fill expects, not an in-game door. A player ignoring logic
  could still physically walk into the course in-game (the game has no
  per-course badge door to install). For randomizer correctness — "the seed
  never *requires* you to enter before you have the badge" — this is complete.
- If a true in-game block were ever wanted, the only available lever is the
  whole-world Wonder-Seed count (area granularity), which cannot target one
  course. That remains infeasible per the RomFS/RE evidence above.
