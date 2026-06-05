# RE: world-intro "FirstVisitDemo" — why fast-travel leaves routes blocked (2026-06-05)

Recovered from RE subagent `ad53287723ffa3c64` (interrupted by a 529 outage
mid-run; this is the reconstructed report). Read-only Ghidra investigation,
`main.nso`, image base `0x7100000000`.

## The problem
In open-world mode the player **fast-travels into a world** instead of walking in
from Petal Isles (PI). On a world's **first** entry, a one-time **world-intro
demo** normally plays that **moves the "Poplin" world-map NPCs and clears the
obstacle that blocks the onward route**. Fast-travel skips it, so the route stays
blocked even after clearing the required courses. (Game-over reloads the world map
and the route opens — i.e. the un-block is re-evaluated on world-map (re)load.)

## What the demo is (confirmed)
- The world-intro is a **`FirstVisitDemo`** event — a **world-map course-point
  demo condition**. The condition string `"FirstVisitDemo"` parses to **enum
  `0xb`** in `FUN_71009281bc` (string→enum parser for course-point demo-trigger
  conditions). The trigger condition + the demo content are **BYML/RomFS data**,
  not NSO code.
- The moving NPCs are **`WorldMapNpc`** actors (Poplins, plus Prince/Florian,
  KoopaJr, Kameck). The demo drives them via **`WorldMapNpcEventRailMoveParam`**
  (rail-move animation) and the route blocker is a **`WorldMapNpcObstacleEventParam`**
  actor — i.e. **route un-blocking is a side effect of the demo moving NPCs along
  rails and removing the obstacle actor.** Behavior is selected by
  `WorldMapNpcDemoSelector` / `GetWorldMapNpcDemoBehaviorType`, gated by
  condition-match IDs `id-cmc-popLinkWitness` / `id-cmc-popLinkRandom`.

## The completion flag (partially pinned)
- Completion is recorded in a gmd flag via accessors **`GetFlagEndFirstVisitWorldDemo`
  / `SetFlagEndFirstVisitWorldDemo`** (name strings @ `0x7102908ffc` / `0x7102915601`).
- Strongly looks **per-world / array-indexed**: sibling names
  `FirstVisitGameDataName` (`0x71028bc47f`), `FirstVisitGameDataArrayNo`
  (`0x71028dca62`), `FirstVisitSaveId` (`0x7102965951`), `FirstVisitEventName`
  (`0x710295f3b1`), `WorldMapFirstVisitWorldNo` (`0x710291dd2a`),
  `GetWorldMapFirstVisitDemoWorldNoFromCoursePointInfo` (`0x71028e1996`),
  `GetWorldMapCoursePointFirstVisitEventInfo` (`0x710292f95c`).
- **The gmd hash for the flag was NOT pinned.** These are reflection/serialization
  name strings (in a string table, no adjacent hash); the live access is by a
  name-hash computed at registration. Known sibling: `INTRO_CUTSCENE_COMPLETED`
  hash `0x89f1cc52` (read via the bool getter `FUN_71003838ac`). Next step to get
  the hash: recover the GameData name-hash algorithm (then hash the per-world flag
  name), or set a live breakpoint on `FUN_71003838ac` during a real first-visit.

## Node-visibility byte (separate, already handled)
`FUN_7100935c80` (node-visible predicate) reads `actor+0x8d` (per-world
visited/initialized byte, set on normal first visit; stays 0 after fast-travel)
plus a gmd bool on hash `0xb003b5f0`. Our `courseVisibleHook` already force-trues
this, so **node visibility is NOT the blocker** — the route obstacle is.

## Bottom line / recommendation
- **The route un-block is a side effect of a BYML/RomFS-driven demo animation**
  (NPC rail-move + obstacle-actor removal). Faking it purely via gmd writes is
  uncertain: setting `EndFirstVisitWorldDemo` likely makes the game **skip** the
  demo, and whether the world then loads in the post-demo (obstacle-removed) state
  is **untested** (and we don't have the hash yet).
- ⟹ The robust fix is to **let the player WALK into each world from PI** so the
  game plays its own `FirstVisitDemo` naturally. This argues for the "**unlock all
  of PI**" approach over fast-travel-into-world. Caveat: PI's own inter-world
  routes may gate on the same demo/Wonder-Seed mechanism — verify PI routes are
  openable (Wonder-Seed counts + Royal Seeds we already control vs. another
  FirstVisitDemo/BYML wall) before committing.
- If we must keep fast-travel: the experiment worth one test is to **set the
  per-world `EndFirstVisitWorldDemo` flag and reload the world map** and see if the
  obstacle is gone — but the hash must be recovered first.

## Concrete anchors for follow-up
- Parser `FUN_71009281bc` (FirstVisitDemo→0xb). Node predicate `FUN_7100935c80`.
- Bool getter `FUN_71003838ac(gmd, &out, hash)`; INTRO hash `0x89f1cc52`.
- Name strings table around `0x71028bc47f … 0x710295f3b1`, `FirstVisitDemo`
  `0x7103485c02`. WorldMapNpc gparam names around `0x71028a2642 … 0x7102952168`.
