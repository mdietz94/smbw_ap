# SMBW Archipelago — milestones plan

Living roadmap. M1 closed 2026-05-20. M2 starts next session.

For project orientation, build/deploy, and what we learned in M1, read `handoff.md` first.

## M1 — feasibility (✅ DONE)

**Goal**: prove the integration is mechanically possible by hooking the two riskiest gameplay events (Wonder Seed pickup, Goal flag-touch) end-to-end on a working Switch subsdk.

**Result**: both hooks land cleanly, fire only on the right events, and validate across multiple levels.

| Hook | Mechanism | AP check coverage |
| --- | --- | --: |
| `WONDER_SEED_AWARDED` | filter on shared Nerve-activate helper `FUN_7100559f7c` at NSO `+0x559f7c`, match `vt_off=0x3345728` | 124 |
| `COURSE_CLEARED` | direct trampoline on `SetCourseClearFlagToGameData` Nerve execute at NSO `+0x1bf28cc` | 206 (Goal Normal/Secret/Fake/Top-of-Flag = 199, plus Royal Seed palace clears = 7, all lumped) |
| **Total** | | **330 / 663 = 49.8%** |

Royal Seed palace clears were validated in M1's post-session test: `COURSE_CLEARED` fires when Koopa Jr is defeated and the Royal Seed appears. Palaces are courses, so they go through the same save-data clear-flag write. Sub-type splitting (palace clear vs normal-flag clear) still requires reading additional state at the hook callsite — same M2.4 problem as Normal vs Secret vs Top-of-Flag.

Negative validation passed for both: neither fires on menu-quit, death, world-map travel, damage taking, power-up pickup, or any non-event noise during 12+ minutes of gameplay. Decisively scoped.

Other useful Nerve vtable offsets observed during testing (logged but not currently target-filtered): see `handoff.md` Section "The two working hooks → currently filtered" and "Other vtables observed firing".

## M2 — fill out the outgoing AP-check surface

Cover the remaining 340 AP checks. Same pattern each time: search Ghidra strings, find the Nerve vtable, check whether its execute is in `FUN_7100559f7c`'s xref list, hook accordingly (filter the existing helper, or trampoline directly).

### M2.1 — Royal Seed (7 checks) — ✅ **already covered**

Confirmed post-M1: `COURSE_CLEARED` fires on palace boss clears (validated when Pipe-Rock Plateau Palace was beaten and the Royal Seed appeared). Palaces are courses; same save-data flag write fires.

The tagging problem (palace clear vs normal-level clear) is the same M2.4 distinguisher problem — both flavors come through one hook. Solve once, get both Royal Seed identity AND Goal exit-type splitting in the same pass.

### M2.2 — 10-coin pickups (305 checks) — biggest remaining bucket

Each level has up to 3 "10 Coins" (`ObjectBigTenLuckyCoin` placement actor — confirmed in M1.2). 305 AP checks across the manual; the single biggest piece of the surface.

**Ghidra search list** (in priority order):

- `RequestEventGetBigTenLuckyCoin` / `RequestEventGetTenCoin` / `RequestEventGetCoin`
- `BigTenLuckyCoinGet` / `TenCoinGet`
- `OnTenCoinPickup` / `PickupTenCoin`
- Generic: search strings for `LuckyCoin`, `BigCoin`, `CoinTen`, `Coin10`

Likely outcome: a `RequestEventGet*Coin` Nerve exists. Find vtable, identify whether slot 8 is in `FUN_7100559f7c`'s xref list. If yes, add `vt_off=0xXXXXX` to `NerveActivateOnce`'s filter. Likely the *cleanest* hook in the entire project — coin pickups are discrete one-frame events, no ambiguity.

**Identity question**: 10-coin AP checks are per-instance (`W1: Welcome to the Flower Kingdom - 10 Coin #1` vs `#2` vs `#3`). We need to extract the placement hash from the Nerve at fire time to identify which coin was collected. Same identity-extraction problem as Wonder Seed (which we deferred); coin volume makes it more pressing here. Read fields at `nerve+0x??` (probably `+0xd8` or similar — see `FUN_7100299488`'s `param_2+0xd8+0x5c` pattern from M1's dispatcher).

Fallback if no `RequestEvent*` Nerve exists: hook the coin counter write at NSO `+0x49253C` (from HamletDuFromage's cheat DB). It's not actor-specific — fires for *all* coin types — but we can filter to 10-coin via the value being added (10-coins increment by 10, regular coins by 1).

### M2.3 — Badge unlocks (24 checks)

The Ghidra strings dump from M1's `Cleared` search turned up:

- `GiveBadgeIdOnCourseClear` (NSO string at `0x7102903f19`)
- `UnlockBadgeIdOnCourseClear` (NSO string at `0x710291dc73`)

These are probably **function symbol names** (the names leaked into the binary somehow, possibly via reflection / debug-symbol metadata). Search for xrefs:

- If they're function names, they're our hook targets directly. Each takes a badge ID arg.
- If they're field names in a config struct, find which function reads them and hooks at that level.

Each badge unlock event sends one of the 24 `<Badge> Obtained` AP checks. The badge ID maps 1:1 to the AP item.

### M2.4 — PlayReport hook for course-identification (huge unlock)

**Status (2026-05-20 evening)**: ✅ **DONE — Switch-side capture + Python decoder + W1-1 corpus**.

Working hook set (locked in after a 4-step bisect):

1. `nn::prepo::PlayReport::SetEventId` (sdk +0x3a81a0) — captures the room name.
2. `CmifProxyImpl<IPrepoService>::_nn_sf_sync_SaveReport` (sdk +0x3a9f8c) and `..._SaveReportWithUser` (sdk +0x3a9fac) — capture the already-serialized payload bytes (now chunked across multiple `prepo.ipc.bytes(start..end/total)` log lines, 128 bytes/line).

PERMANENTLY off-limits (per bisect): hooking any of `PlayReport::Save()`, `Save(Uid&)`, the 8 `PlayReport::Add(...)` overloads, or the 2 `Struct::Add` overloads. Each triggers a delayed audit-thread abort 5-6 seconds later on whichever SDK validator thread next touches the prepo subsystem (observed: `ModuleSystemWorker1`, `gmd::SaveDataMgr`).

Captured live 2026-05-20 (fixtures in [bridge/test_play_report.py](bridge/test_play_report.py)):

| Fixture | Bytes | Scenario |
|---|--:|---|
| `WORLD_ACTIVITY` | 239 | W1-1 — stepping onto the course tile |
| `WORLD_RESULT` | 1059 | W1-1 overworld→course (intra-world) |
| `WORLD_RESULT_W1_TO_W2` | 1059 | W1→W2 inter-world transition (first `world_no=2`, `stage_type=2`, `transition_type=0`) |
| `COURSE_RESULT` | 1577 | W1-1 Top of Flag clear (`goal_id=0, touch_goal_top=True`) |
| `W1_2_COURSE_IN` | 351 | entering W1-2 |
| `W1_2_COURSE_RESULT_SECRET` | 1575 | W1-2 Secret Exit clear (`goal_id=1`) |
| `PALACE_COURSE_RESULT` | 1579 | the *concurrent* course_result for a palace WIN (`world_mother_seed=True`) — first live `0xCD + u16` (total_play_time_sec=266) |
| `KOOPAJR_RESULT_LOSS` | 499 | Pipe-Rock Plateau Palace LOSS to Bowser Jr (`battle_result=False`) |
| `KOOPAJR_RESULT_WIN` | 499 | the WIN companion to PALACE_COURSE_RESULT (`battle_result=True`) — also the first array-of-structs in the corpus (`koopajr_step_info`) |

Format (CBOR-ish with Nintendo extensions): 3-byte header (`0xDE` + big-endian u16 entry count), then flat key-value pairs. Strings use 0xA0-0xBF (short) / 0xD9 (medium). Top-level uints use 0x00-0x7F inline / 0xCC + u8 / 0xCE + u32 (unsigned). Nested Struct ints use signed 0xD0-0xD3 (s8/s16/s32/s64, smallest-fit). Structs are 0x80-0x8F, arrays are 0x90-0x9F. Booleans are 0xC2/0xC3 (NOT CBOR-standard 0xF4/0xF5). Any64BitId tagged values use `0xD7 + u8 TypeCode + u64`. Negative -1 has its own short form `0xFF`.

Decoder + 44 tests in [bridge/play_report.py](bridge/play_report.py) and [bridge/test_play_report.py](bridge/test_play_report.py). The three live W1-1 payloads decode end-to-end and assert against Ryujinx's reference output. See [docs/handoff.md](docs/handoff.md) for the full opcode table and remaining unmapped types (floats, multi-byte negatives, larger containers).

Discovered post-M1: the game emits structured telemetry via `nn::prepo::PlayReport`. Each event carries rich state. The `koopajr_result` report we observed contained:

```json
"stage_info": {
    "stage_key": 2308078743,   // unique 32-bit ID for this course
    "world_kind": 0,
    "world_no": 1,
    "course_no": 30
},
"battle_result": False,
"badge_id_array": [34],
"koopajr_total_time": 40,
"play_mode": 1,
...
```

`stage_key` is the **unique 32-bit identifier per course** — exactly the AP-location-mapping key we need. Maps 1:1 to courses in the manual world.

**Hook**: symbol-based on `nn::prepo::PlayReport::Save` (or whichever finalize-call commits the report). Same pattern wondar already uses for `nn::fs::OpenFile` — `InstallAtSymbol("_ZN2nn...")`. Stable across builds, no offset hunting. The Nintendo SDK symbol is in wondar's `syms/100/sdk.sym`.

**At hook entry**: the PlayReport struct holds key/value pairs. Walk it, extract the Room name + the structured payload. Filter by Room:

- `koopajr_result` → boss/palace clear (Royal Seed identification)
- `course_result` / similar → normal level clear (Goal Normal/Secret/Fake/Top-of-Flag identification)
- `game_option` / `erepo_*` → boring telemetry, skip
- TBD what others fire — exhaustive list to be gathered by enabling a survey-log filter

**Limitation**: PlayReports fire at transitions / results, not per-pickup. So:
- ✅ Course identification (Goal + Royal Seed → which course was cleared)
- ✅ End-of-level summaries (badges held, total time, etc.)
- ❌ Per-pickup events (Wonder Seed grab, 10-coin grab during gameplay) — too granular for PlayReport. Keep the Nerve-hook approach for those.

The combination — Nerve hooks for in-level events, PlayReport hook for level identification — gives clean orthogonal coverage with minimal overlap.

### M2.5 — Goal exit-type distinguisher

`COURSE_CLEARED` fires on all valid clears but doesn't yet distinguish Normal Exit (96) / Secret Exit (9) / Fake Exit (5) / Top of Flag (89) / Royal Seed palace clear (7). For AP completeness we need that split.

**Status (2026-05-20)**: ✅ **DONE for 194 of 199 checks; awaiting Fake Exit + palace capture for the remaining 12.** Empirically derived mapping table from W1-1 (Normal Top-of-Flag) + W1-2 (Secret Exit) captures:

| AP sub-type | Discriminator | Empirical evidence |
|---|---|---|
| Top of Flag (89) | `goal_id == 0` AND `touch_goal_top_result == True` AND `world_mother_seed == False` | ✅ W1-1 |
| Normal Exit (96) | `goal_id == 0` AND `touch_goal_top_result == False` AND `world_mother_seed == False` | partial — logic-derived from W1-1 (need a non-top-touch normal-exit capture) |
| Secret Exit (9) | `goal_id == 1` AND `world_mother_seed == False` | ✅ W1-2 |
| Fake Exit (5) | `goal_id == 2` (guessed) AND `world_mother_seed == False` | TBD |
| Palace Clear (7) | `room_name == "koopajr_result"` AND `battle_result == True` | ✅ both Pipe-Rock Plateau Palace LOSS *and* WIN captured 2026-05-20 |

⚠ **Important nuance** (locked in 2026-05-20 after the palace WIN capture): a palace WIN emits BOTH a `course_result` AND a `koopajr_result` ~1 ms apart for the *same* clear event. The course_result's `goal_id=0, touch_goal_top_result=False` would naively misclassify the palace win as Normal Exit. Two safe handlings:

1. **Priority rule (preferred)**: if `koopajr_result` fires within ~50 ms of `COURSE_CLEARED`, use it. Ignore the concurrent `course_result` for AP routing.
2. **Defensive flag**: `course_result.world_mother_seed == True` distinguishes palace clears from normal level clears (always False in W1-1 / W1-2; True in the palace WIN). Use as a cross-check.

The discriminator table above adds `world_mother_seed == False` to every non-palace row so even a bridge using just `course_result` will route correctly.

W1-2 capture: `goal_id=1, touch_goal_top_result=True`. Confirms `goal_id` is the primary discriminator and `touch_goal_top_*` is orthogonal (a secret-exit pole can also be top-touched). The mapping logic is locked into [bridge/test_play_report.py](bridge/test_play_report.py) `TestM25ExitTypeMapping`.

The pre-M2.4 fallback plan (dumping `SetCourseClearFlagExecute`'s nerve struct fields) is now obsolete — PlayReport delivers everything we needed. Kept for history in git only.

### M2.6 — Wonder Seed per-level identification (124 checks)

The M1 `WONDER_SEED_AWARDED` nerve fires reliably on every Wonder Seed grab but doesn't carry an in-band identifier of *which* seed. AP checks are per-course (and some courses have 2+ Wonder Phase seeds), so the bridge needs a way to attribute each fire to a specific AP location.

**Preferred approach — course correlation in the bridge (unlocked by M2.4 PlayReports)**:

1. Bridge subscribes to `course_in` PlayReport events. When one fires it sets `current_course = stage_info.stage_key`.
2. Bridge subscribes to `WONDER_SEED_AWARDED` (Switch nerve hook).
3. On each WONDER_SEED_AWARDED fire, attribute it to `current_course`.
4. AP location table maps `stage_key → location_id`. Bridge fires the AP check.

This covers the **~70 courses that have exactly one Wonder Phase seed** with no Switch-side code changes. The bridge already needs to track `current_course` for other reasons (e.g. routing per-course events).

**Fallback for courses with multiple Wonder Phase seeds**:

If a single course has N>1 Wonder Phase seeds and we need to distinguish *which* one was grabbed, fall through to the placement-hash approach the original M2.6 plan envisioned:

1. At `WONDER_SEED_AWARDED` callback entry, read the nerve struct to find the actor pointer (likely at some offset N — same pattern as M1's `GoalDispatcher` exploration which found `param_2+0xd8` → actor info → `+0x5c` for the name).
2. The actor has a placement hash stored in its `ActorPlacementInfo` — wondar's existing `include/game/actor/ActorPlacementInfo.h` describes the layout; `mHash` is at a known offset.
3. Read it, log alongside the nerve fire.
4. Map `(stage_key, placement_hash) → AP location` in the bridge.

This requires Ghidra work to find the actor-pointer offset on the Wonder Seed nerve struct. Defer to a follow-up — handle simple courses first, see how many multi-seed courses actually exist in the manual location table.

**Definition of done**: 124 of 124 Wonder Seed AP checks correctly routed to their per-course (or per-instance) AP location. First milestone: get all single-seed courses working via course correlation.

## M3 — incoming AP-item application (game ← AP)

Outgoing checks (M2) are only half the integration. AP also sends *items* to the player: Wonder Seeds, Royal Seeds, badges, power-ups, characters, Wonder Effects, etc. The mod must apply them in-game.

**MVP item set for first usable bridge** (per 2026-05-20 scope decision — 10-coins deferred):

| Section | Item count | Status |
|---|--:|---|
| M3.2 Badge unlock | 24 | function names known; need Ghidra + symbol lookup |
| M3.3 Wonder Seed grant | 124 | counter address known; need the increment-function RE |
| M3.3b Royal Seed grant | 7 (new section) | TBD — likely same family as Wonder Seed but per-palace |
| M3.8 DeathLink trigger | 1 (bidirectional event) | TBD — companion to M3.8 detection |

M3.1 (power-ups), M3.4 (characters), M3.5 (Wonder Flower suppression), M3.6 (button suppression), M3.7 (goal hook) are deferred until the MVP set ships.

### M3.1 — power-up grant (4 items: Elephant, Fire, Bubble, Drill) — DEFERRED

The HamletDuFromage cheat DB gave us:

- NSO `+0x198B50` is the power-up state load site
- NSO `+0x020128F4` is the power-up state field
- Values: `2=Fire, 3=Elephant, 5=Small, 6=Drill, 8=Tall, 9=Pink/Bubble` (cross-confirmed with wondar's partly-RE'd `ItemGetType` enum in `include/game/actor/component/ItemGetRef.h`: `Super=1, Elephant=3, Drill=6, Bubble=9`)

**Approach**: rather than poke memory raw, find the *apply-powerup* function the engine calls when the player picks one up. Hook it once for read (confirm signature, find arg ordering), then call it from our code with the AP-granted type. This ensures animations, sound effects, and any side-effects (e.g., size box change) run correctly.

The event Nerve `vt_off=0x33fd870` fires on damage *and* power-up pickup (we observed this in M1 testing). Worth peeking that Nerve's vtable to see if it's a `RequestEventApplyPowerUp` family member.

### M3.2 — badge unlock (24 items, inverse of M2.3) — DEFERRED (save-diff path)

**Status (2026-05-21)**: 7 Ghidra scripts ([scripts/ghidra/find_badge_*.py](../scripts/ghidra/) and [scripts/ghidra/inspect_badge_*.py](../scripts/ghidra/)) failed to surface a usable grant function via string-based RE. Findings:

- `GiveBadgeIdOnCourseClear` and `UnlockBadgeIdOnCourseClear` resolve to **course-config property getters** (FUN_7101a592fc / FUN_7101b204a8), not action functions — they read the badge_id associated with a course but don't grant it.
- `FUN_7101b1fb6c` (the function that references both strings) is a **test harness** that exercises the getters and reports results via vtable callbacks.
- The four acquisition-context strings (`BadgeShop`, `BadgeChallenge`, `BadgeHouse`, `BadgeMedley`) appear only in **struct initializers, string-compare loops, and debug log formatters** — no shared grant helper is called.
- The `BadgeFlower` actor class-name string has exactly one xref in the binary, in a 6.7 KB-stack dispatcher that uses the string as one struct field among many — not a class-name getter.
- No `Add*` / `Grant*` / `Acquire*` / `*::Add` named functions exist in the badge family.

**Conclusion**: the badge-add operation is either (a) compiled as a direct bit-write inside a larger function with no exposed string name, or (b) routed through vtable indirection that's invisible to Ghidra's static xref pass. Either way, string-RE has bottomed out.

**Path forward — save-diff**: full handoff is in [docs/save-diff-grants.md](save-diff-grants.md). Same diff procedure as M3.3 / M3.3b — they're now all one batched workstream.

Defer until after the M4 LAN bridge ships; the bridge can run with outgoing-checks-only for now, and we revisit grants together when there's a focused session for save-data capture.

### M3.3 — Wonder Seed grant (124 items) — DEFERRED (save-diff path)

**Status (2026-05-21)**: 4 Ghidra scripts + a runtime probe + a hash-reversing attempt failed to produce a usable grant entry point. Findings:

- NSO `+0x12AF6C` (HamletDuFromage's "[seed]" cheat anchor) is *not* a data field — it's an `ldr w8, [x8]` instruction inside `FUN_710012ae94`, a **generic counter getter keyed by 32-bit hashes of internal stat names**.
- The function reads from a hash table at `container[+0xe0]` (bucket array, size at `+0xec` = 140 buckets), with per-entry data at `container[+0xd8]` (40-byte stride, value at `+0x1c`).
- The runtime probe captured the container address as `0x20d3da07a8` (constant per session). Confirmed identifications by cross-referencing PlayReport values: `key=0xf4ee6827 → value=148` matches `flower_coin_course_out`; `key=0x17f0bb21 → value=26` matches `total_play_time_sec`.
- **The wonder-seed-specific key wasn't surfaced** in any captured session — and even if it were, granting requires identifying the *writer* in the same hash table, which has no obvious string anchor.
- Hash-reversing ([scripts/identify_seed_keys.py](../scripts/identify_seed_keys.py)) tried CRC32 / FNV-1 / FNV-1a / DJB2 / SDBM against ~100 candidate stat names — no matches. SMBW uses a custom hash (likely a Nintendo-specific Murmur variant or an internal one).

**Path forward — save-diff** ([docs/save-diff-grants.md](save-diff-grants.md)): capture save buffer before/after one wonder-seed acquisition, diff the bytes, identify the per-course flag + per-world counter, write the same change in our subsdk when AP sends a grant.

### M3.3b — Royal Seed grant (7 items) — DEFERRED (same path)

Same family as M3.3 — the per-palace flag + counter should appear as 1 bit change + a u8 increment in the same save-diff procedure. Likely surfaced in the same diff pass since palace clears already produce a `world_mother_seed=True` indicator. See [docs/save-diff-grants.md](save-diff-grants.md).

### M3.4 — character roster unlock (12 items) — DEFERRED

Save data bitfield for which characters are available in file-select. Find via memory diff (unlock a new character via gameplay, diff save state before/after).

### M3.5 — Wonder Flower / Wonder Effect suppression (opt-in)

Per the manual's `wonder_flower_rando` and `wonder_effect_rando` yaml flags:

- **Wonder Flower**: when AP hasn't granted the `Wonder Flower` item, suppress the actor's spawn or `onTouch`. Hook the actor's init or interaction virtual.
- **Wonder Effect** (15 effect types): when AP hasn't granted the level's native effect type, refuse to start the Wonder phase. We have a candidate: `vt_off=0x3346330` fires on Wonder Flower touch (observed in M1 testing) — peek that Nerve and find what *starts* the effect, then we early-out if the effect type isn't AP-granted.

### M3.6 — Button-input suppression (4 items, opt-in)

For `button_shuffle` yaml flag — locks Y, ZL/Down, R, Up button capability until AP grants the item. Hook player-input polling (likely the `PlayerControlRef` component referenced in wondar's `include/game/actor/component/PlayerControlRef.h`). Per-button gates inside the input read; if not AP-granted, force-zero the bit before the game sees it.

### M3.7 — game-completion goal hook

Detect "all-clear" / final Bowser defeat → fire AP `goal complete`. The strings dump turned up `GameClear` (`0x710348e884`) and `SetFlagEndDispMsgFirstVisitedWorldAfterClearedLastBoss` (`0x710295d801`). The latter is the most specific signal we'll ever get — that flag is set exactly once per save, on first time defeating final Bowser. Find its setter and hook.

### M3.8 — DeathLink (bidirectional — MVP)

DeathLink is an Archipelago feature: when one player dies, every other player connected with DeathLink enabled also dies. Two halves, both required:

**Detection (outgoing) — straightforward**:

The M1 nerve survey already identified `vt_off=0x33fd9a8` as "Mario death; ~50ms after Wonder Seed grab; world map travel" — i.e. a generic "scene transition" nerve that includes death as one of its triggers. To turn this into a clean death signal:

1. Extend `NerveActivateOnce` to also log on `vt_off=0x33fd9a8`.
2. Cross-check the in-context fires: not every 0x33fd9a8 fire is a death (post-seed cleanup and world map travel also fire it). Need a secondary discriminator — likely the previous Wonder Seed grab clears nicely, but world-map travel must be filtered. Compare against a known death (e.g. fall into pit) vs a known scene transition (clear flag) and diff the nerve state.
3. Fallback: find a more specific death-only nerve via Ghidra string search for `Dead`, `PlayerDead`, `RequestEventDead`, etc.
4. Emit `DEATH_DETECTED` log line. Bridge sends `DeathLink` AP event with the player's slot data.

**Triggering (incoming) — needs more research**:

When AP sends a DeathLink event to this slot, the Switch mod must kill Mario. Three candidate approaches:

1. **Call the death-handling function directly**. Find via xrefs from the death-only nerve (whatever the slot-8 execute calls when triggered).
2. **Damage Mario with overflow**. The player-damage function (callable from the AP grant path) with a damage amount exceeding current HP forces a death. Likely callable from outside a combat context too.
3. **Write the player's HP field to 0** and force a state-check tick. Fragile — might leave the player frozen rather than dying cleanly.

(1) is cleanest; (2) is a fallback if (1) requires hard-to-reach state.

**Edge cases the bridge must handle**:
- Don't fire DeathLink when the player is dying *because* a DeathLink event arrived (would create a loop).
- Suppress DeathLink during cutscenes / file-select / paused / overworld (player isn't in a deathable state). The bridge's "current course" tracker (M2.6 prereq) already gives us this signal.
- DeathLink during a palace fight should still register but ideally not kill mid-fight — defer / queue to after the fight ends, or send anyway and let the player retry.

## M4 — LAN protocol + host client

The Switch mod needs to talk to a PC-side service that bridges to the Archipelago server. Mirror smo_archipelago's design.

### M4.1 — Switch-side TCP client

Enable the commented-out `nn::socket::Initialize` block in `main.cpp`. Add a worker thread that maintains a TCP connection to `BRIDGE_HOST:BRIDGE_PORT` (CMake-baked, like SMO's setup). Sends one JSON line per check event:

```json
{"event":"wonder_seed","level_id":"W1-1","placement_hash":"48c0584d409801d6"}
{"event":"course_cleared","level_id":"W1-1","exit_type":"normal"}
```

Receives item-grant lines from PC:

```json
{"action":"grant_powerup","type":"elephant"}
{"action":"grant_badge","badge_id":"parachute_cap"}
```

Use the same lock-free atomic-spinlock ring-buffer pattern the SMO logger uses for SD writes (`smo_archipelago/switch-mod/src/util/Log.cpp` `SMOAP_DEBUG_SD_LOG` block). No `std::mutex`, no `thread_local`.

### M4.2 — Host service (Python)

PC-side service:

- Listens on the TCP port for Switch events.
- Maintains a state table (`current_course`, `seeds_collected`, etc.).
- Forwards to an Archipelago client via the AP network protocol.
- Receives AP item-grants → relays to Switch.

Mirror smo_archipelago's `apworld/` and `scripts/` layout. Re-uses Archipelago's Python SDK.

### M4.3 — Setup wizard (defer)

SMO has a `/setup` slash command in their AP client that handles first-time prereq checks + bridge-IP configuration + Switch-mod build. We can defer this until the rest works manually.

## M5 — Convert manual_smbwonder_zim to integrated apworld

The existing `manual_smbwonder_zim` is an Archipelago Manual world (player ticks checkboxes by hand in the AP client). With M2 + M3 + M4 working, we replace the manual ticking with auto-detected events from the Switch mod.

Concrete work:

- Replace the per-check `requires: []` boolean checks with Switch-event subscriptions.
- Add a `setup_*.md` doc for the user (mirror `smo_archipelago/docs/first-time-setup.md`).
- Pre-release the apworld for one beta tester before bundling at scale.

## M6 — Hardware Switch support

Move from Ryujinx-only to also working on a real modded Switch under Atmosphere CFW. This is mostly a deployment story:

- Path changes: `Ryujinx/mods/contents/<TID>` → `/atmosphere/contents/<TID>/exefs`.
- Restore the `SMOAP_DEBUG_SD_LOG`-equivalent ring-buffer/SD-card fallback for crash diagnostics (currently disabled in our Log.cpp; the SMO version of this pattern is the reference).
- Restore the RSTB (Resource Size Table) update if we ever do romfs replacement.
- Validate `svcOutputDebugString` is non-fatal on retail Switch (it routes to `lm` service; binlog visibility is "spotty" per SMO doc comment — not a problem for production, just for our diagnostic visibility).

## M7 — UX polish

Lowest priority but customer-facing. Mirror SMO's Cappy-speech-bubble pattern for AP notifications:

- "Connected to Archipelago" toast when the LAN bridge handshakes.
- "Got Fire Flower from P3" toasts as items arrive.
- "Disconnected from Archipelago" + replay-from-buffer when the LAN drops mid-session.

wondar's existing ImGui overlay is currently disabled (it crashed on this NSO build). Path forward: either re-enable with caution (find what specifically crashed and fix), or build a native-UI toast system using the game's own message rendering (more work but bug-free).

## Risk register

Things that could re-block the project, in rough probability order:

1. **`SetCourseClearFlagToGameData` fires on palace clears as well as level clears** — actually a feature, not a risk; we tag the fire with course-id and the per-palace `koopajr_result` PlayReport disambiguates. (Tested + done in M2.1 / M2.4 / M2.5.)
2. **PlayReport class-member hooks crash on SDK validation** — discovered + worked around in the M2.4 bisect by hooking the IPC client layer instead. Documented + permanently in the "don't try" list.
3. **10-coin Nerve doesn't exist** — fallback to coin-counter-write hook with value filter. Already known-good per cheat DB. Deferred per 2026-05-20 scope decision.
4. **Multiple Wonder Phase seeds per course** — M2.6 fallback is the placement-hash read approach; not blocking the MVP.
5. **`GiveBadgeIdOnCourseClear` requires course-clear context to call** — would need a deeper "AddBadgeToCollection" function. Probably solvable by reading what the original function calls internally.
6. **No public Royal Seed grant function** — fall back to writing the save bit directly. Less clean but works.
7. **DeathLink incoming trigger has no clean public entry** — may need to write HP=0 and force a tick. Fragile-looking but the SMO project's DeathLink ships on a similar pattern (we have its source for reference).
8. **TCP on Switch hits firewall / NAT issues** — SMO already solved this with LAN-direct connection; should port without trouble.
9. **v1.0.0-only support becomes a sticking point for users on v1.0.1** — port hooks via BinDiff/Diaphora when there's user demand; ~1 day per version-bump per hook.

## Recommended pacing (revised 2026-05-20)

History (closed):
- ✅ Session 1: M1 — Wonder Seed + Course Clear nerves.
- ✅ Session 2: M2.4 + M2.5 — PlayReport IPC capture, Python decoder, full corpus of 9 live fixtures across W1-1/W1-2/Palace clears + W1→W2 transition.

Plan (next):
History (closed since 2026-05-20):
- ✅ Session 3: M2.6 bridge skeleton — state + protocol + processor, 106 tests passing.
- ❌ Sessions 4 + 5: M3.2 + M3.3 + M3.3b Ghidra attempts. 11 scripts deep, no usable grant API. **Pivoted** to save-diff per [save-diff-grants.md](save-diff-grants.md) for all three items.

Forward plan (revised 2026-05-21):
- **Session 6 (next)**: M3.8 DeathLink detection — extend `NerveActivateOnce` to filter on `vt_off=0x33fd9a8`, find a discriminator for actual deaths vs the noise sources (post-seed cleanup / world-map travel). Pure Switch-mod work, no RE dead-ends.
- **Session 7**: M4.1 + M4.2 — LAN socket from Switch mod ↔ Python bridge. End-to-end demo of the outgoing surface: AP client logs Wonder Seed pickups, course clears, palace clears, deaths — all flowing from game → Switch mod → TCP → bridge → AP. **Outgoing-only MVP ships here**; grants are still TBD.
- **Session 8**: save-diff sprint per [save-diff-grants.md](save-diff-grants.md). Capture badge before/after, wonder seed before/after, royal seed before/after. Diff, identify offsets, build the runtime address anchor in the subsdk, wire up the three grant functions.
- **Session 9**: DeathLink triggering (the "kill Mario from AP" half) — Ghidra to find the death-application function, or a fallback like HP=0 write.

Deferred indefinitely until after the MVP demo:
- M2.2 (10-coin nerve hunt) — 305 outgoing checks, biggest unrouted bucket.
- M3.1 (power-up grant), M3.4 (characters), M3.5 (Wonder Flower / Effect suppression), M3.6 (button suppression), M3.7 (goal hook).
- M5 (replace manual_smbwonder_zim with integrated apworld).
- M6 (hardware Switch), M7 (UX polish).
