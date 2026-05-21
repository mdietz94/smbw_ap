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

Captured live 2026-05-20 (W1-1 Welcome to the Flower Kingdom playthrough):
- `world_activity` (239 bytes, 10 fields) — fires when stepping onto a course tile.
- `world_result` (1059 bytes, 26 fields) — fires on overworld→course transition; carries `next_stage_info.stage_key` (destination identifier).
- **`course_result`** (1577 bytes, 57 fields) — **fires ~8 ms after the M1 `COURSE_CLEARED` nerve**. Carries `stage_info.stage_key` (cleared course), `touch_goal_top_{enter,result}` (the M2.5 Top-of-Flag distinguisher!), `goal_id`, `badge_id_array`, `total_get_finish_seed_count`, all coin counts.

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
| Top of Flag (89) | `goal_id == 0` AND `touch_goal_top_result == True` | ✅ W1-1 capture |
| Normal Exit (96) | `goal_id == 0` AND `touch_goal_top_result == False` | partial — no non-top-touch normal-exit capture yet, but logic follows from the W1-1 evidence |
| Secret Exit (9) | `goal_id == 1` (regardless of `touch_goal_top_*`) | ✅ W1-2 capture |
| Fake Exit (5) | `goal_id == 2` (guessed) | TBD — capture a Fake Exit (5 courses have one) |
| Palace Clear (7) | `room_name == "koopajr_result"` | known from pre-M2.4 RE; not yet captured live |

W1-2 capture: `goal_id=1, touch_goal_top_result=True`. Confirms `goal_id` is the primary discriminator and `touch_goal_top_*` is orthogonal (a secret-exit pole can also be top-touched). The mapping logic is locked into [bridge/test_play_report.py](bridge/test_play_report.py) `TestM25ExitTypeMapping`.

**First check**: if M2.4's PlayReport hook lands and the post-clear PlayReport carries an `exit_type` / `clear_type` / `goal_kind` field, the entire problem is solved at zero cost. The koopajr_result report's `battle_result` field is precedent for engine-side enum tagging of result types.

**Fallback if not**: at the `SetCourseClearFlagExecute` callback, dump additional fields from the `nerve` struct. Candidates by analogy to wondar's `ActorPlacementInfo`:

- `nerve + 0x10`: actor-archive-name pointer for the goal we touched (would be `"ObjectGoalPole"`, `"ObjectGoalPoleFort"`, etc. — distinguishes Normal vs Fort/Secret)
- `nerve + 0x40` or `+0x68`: state index from `FUN_7101bf28cc` body (we saw `x0 = sub x29, #0x8`; that local is used in `FUN_710059f894` and `FUN_71003d3fb0` — probably contains the exit-id)
- Mario's Y position at clear time (for Top-of-Flag): the player actor is reachable from a global GameFramework singleton

Test approach: enter a level with both a Normal Exit and a Secret Exit (Piranha Plants on Parade, W1-2). Take each exit in separate runs. Dump `nerve+0x00..0xC0` at each fire. The differing bytes tell us where the exit-id lives.

### M2.6 — Wonder Seed placement-hash identity (124 checks)

Same identity-extraction problem as 10-coins. Wonder Seed AP checks are per-level (`W1: Welcome to the Flower Kingdom - Wonder Seed`, etc.), and the actor placement hashes from level-load tell us which seed is which (we logged them in M1.2 — e.g. W1-1's seed is hash `48c0584d409801d6`).

At `WONDER_SEED_AWARDED` fire time, read the nerve's `+0x??` offset to extract the placement hash, match against a `hash → AP-location-id` table. Defer to M2 for now; the M1 hook fires correctly without identity, which is enough for "feasibility proven."

## M3 — incoming AP-item application (game ← AP)

Outgoing checks (M2) are only half the integration. AP also sends *items* to the player: power-ups, badges, characters, Wonder Effects, etc. The mod must apply them in-game.

### M3.1 — power-up grant (4 items: Elephant, Fire, Bubble, Drill)

The HamletDuFromage cheat DB gave us:

- NSO `+0x198B50` is the power-up state load site
- NSO `+0x020128F4` is the power-up state field
- Values: `2=Fire, 3=Elephant, 5=Small, 6=Drill, 8=Tall, 9=Pink/Bubble` (cross-confirmed with wondar's partly-RE'd `ItemGetType` enum in `include/game/actor/component/ItemGetRef.h`: `Super=1, Elephant=3, Drill=6, Bubble=9`)

**Approach**: rather than poke memory raw, find the *apply-powerup* function the engine calls when the player picks one up. Hook it once for read (confirm signature, find arg ordering), then call it from our code with the AP-granted type. This ensures animations, sound effects, and any side-effects (e.g., size box change) run correctly.

The event Nerve `vt_off=0x33fd870` fires on damage *and* power-up pickup (we observed this in M1 testing). Worth peeking that Nerve's vtable to see if it's a `RequestEventApplyPowerUp` family member.

### M3.2 — badge unlock (24 items, inverse of M2.3)

Whatever writes the badge-unlocked bitfield in save data is our entry point. Same function found in M2.3 (`GiveBadgeIdOnCourseClear`) might also be callable for arbitrary IDs from our subsdk.

### M3.3 — Wonder Seed counter increment (per-world)

The cheat DB's `[seed]` cheat at `+0x12AF6C` is a *read* of the per-world Wonder Seed counter (fed back as a forced value of 100 by the cheat). The corresponding *write* (the increment function) is what we'd want to call to credit the player with a seed from AP. Find it by searching xrefs of the field address.

### M3.4 — character roster unlock (12 items)

Save data bitfield for which characters are available in file-select. Find via memory diff (unlock a new character via gameplay, diff save state before/after).

### M3.5 — Wonder Flower / Wonder Effect suppression (opt-in)

Per the manual's `wonder_flower_rando` and `wonder_effect_rando` yaml flags:

- **Wonder Flower**: when AP hasn't granted the `Wonder Flower` item, suppress the actor's spawn or `onTouch`. Hook the actor's init or interaction virtual.
- **Wonder Effect** (15 effect types): when AP hasn't granted the level's native effect type, refuse to start the Wonder phase. We have a candidate: `vt_off=0x3346330` fires on Wonder Flower touch (observed in M1 testing) — peek that Nerve and find what *starts* the effect, then we early-out if the effect type isn't AP-granted.

### M3.6 — Button-input suppression (4 items, opt-in)

For `button_shuffle` yaml flag — locks Y, ZL/Down, R, Up button capability until AP grants the item. Hook player-input polling (likely the `PlayerControlRef` component referenced in wondar's `include/game/actor/component/PlayerControlRef.h`). Per-button gates inside the input read; if not AP-granted, force-zero the bit before the game sees it.

### M3.7 — game-completion goal hook

Detect "all-clear" / final Bowser defeat → fire AP `goal complete`. The strings dump turned up `GameClear` (`0x710348e884`) and `SetFlagEndDispMsgFirstVisitedWorldAfterClearedLastBoss` (`0x710295d801`). The latter is the most specific signal we'll ever get — that flag is set exactly once per save, on first time defeating final Bowser. Find its setter and hook.

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

1. **`SetCourseClearFlagToGameData` fires on palace clears as well as level clears** — actually a feature, not a risk; we just tag the fire with course-id. (Tested in M2.1.)
2. **10-coin Nerve doesn't exist** — fallback to coin-counter-write hook with value filter. Already known-good per cheat DB.
3. **Goal exit-type distinguisher data isn't in the Nerve struct** — fall back to reading Mario's player actor + the active goal-pole actor at clear time. Slower hook but works.
4. **TCP on Switch hits firewall / NAT issues** — SMO already solved this with LAN-direct connection; should port without trouble.
5. **v1.0.0-only support becomes a sticking point for users on v1.0.1** — port hooks via BinDiff/Diaphora when there's user demand; ~1 day per version-bump per hook.

## Recommended pacing for next 2-3 sessions

- **Session 2** (next): M2.1 + M2.2 + M2.3 + M2.4. ~80% of remaining AP-outgoing surface in one sitting if Ghidra goes smoothly.
- **Session 3**: M3.1 + M3.2 + M3.7. Incoming hooks for power-up, badge, goal. Validate game-modification doesn't break boot.
- **Session 4**: M4.1 + M4.2. LAN socket + Python host. End-to-end demo: AP client logs Wonder Seed pickup from in-game.
