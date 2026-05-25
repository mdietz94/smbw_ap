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

### M2.2 — 10-coin pickups (306 checks) — biggest remaining bucket

Each level has up to 3 "10 Coins" (`ObjectBigTenLuckyCoin` placement actor — confirmed in M1.2). 306 AP checks across the manual (102 courses × 3 coins, plus 1 stray duplicate); the single biggest piece of the surface.

**Status (2026-05-25)** — ✅ **scoped + bridge implementation shipped; no new Switch-side hook needed**. See [docs/m2.2-runbook.md](m2.2-runbook.md) for the full plan.

The `course_result` PlayReport (M2.4 corpus) already carries `big_flower_coin_course_in` and `big_flower_coin_course_out` as `bool[3]` — per-instance state of the 3 "Big Flower Coin" placements at course entry vs exit. Diff = newly-collected this run. Implementation shipped: `CheckKind.TEN_COIN` in [bridge/protocol.py](../bridge/protocol.py); `_emit_ten_coin_checks` helper in [bridge/processor.py](../bridge/processor.py) extending `_handle_course_result`; dedup-key extended with sub_key in [bridge/state.py](../bridge/state.py); `_TEN_COIN_TABLE` for W1-1 and W1-2 in [bridge/location_table.py](../bridge/location_table.py); 21 new tests across `test_processor.py` and `test_tables.py` (224 bridge tests pass).  The original Ghidra Nerve-hunt + counter-write-hook paths are deferred indefinitely as fallback (only revisited if the diff-semantics empirical verification fails — see runbook).

**One blocking risk**: all 4 existing `course_result` fixtures have `_in == _out`. The diff interpretation is inferred from naming, unproven empirically. One capture with `_in[N]=False, _out[N]=True` would settle it. Captures are otherwise free side-effects of normal play.

**Routing backlog**: only 2 of 102 non-palace courses have stage_key → name entries in `_TEN_COIN_TABLE`.  The 100 remaining are an incremental fill-out (one `course_in` PlayReport capture per course, same backlog as the existing CheckKinds).

**Historical Ghidra-hunt notes (deferred — kept for the fallback path)**:

- Search list: `RequestEventGetBigTenLuckyCoin` / `RequestEventGetTenCoin` / `BigTenLuckyCoinGet` / `LuckyCoin` / `BigCoin` / `CoinTen` / `Coin10`.
- Likely outcome: a `RequestEventGet*Coin` Nerve. If slot 8 is in `FUN_7100559f7c`'s xref list, add `vt_off=0xXXXXX` to `NerveActivateOnce`'s filter.
- Identity question (per-instance #1/#2/#3) would have required reading the placement hash from the actor pointer at the nerve's `+0x??` offset.
- Fallback to that fallback: hook the coin-counter write at NSO `+0x49253C` (HamletDuFromage cheat DB) with a +10 value-filter to distinguish from regular coins.

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
| M3.2 Badge unlock | 24 | ✅ primitive shipped 2026-05-24 (Spring Feet validated end-to-end); badge_table holds 1 live + 2 save-diff entries |
| M3.3 Wonder Seed grant | 124 | ✅ container-A counter primitive shipped 2026-05-25 (`probe::grantContainerACounter`); flower_coin live-validated.  Per-seed Wonder Seed routing deferred (no AP item maps to per-seed grants; route via M2.6 course-correlation already covers per-course AP locations) |
| M3.3b Royal Seed grant | 7 (new section) | ✅ shipped 2026-05-25 — `probe::grantContainerBBool` calls `FUN_710049EA24` (NSO +0x0049EA24); save-diff confirmed 6 byte flips, W1+INTRO idempotent (already set) |
| M3.8 DeathLink trigger | 1 (bidirectional event) | TBD — companion to M3.8 detection |

M3.1 (power-ups), M3.4 (characters), M3.5 (Wonder Flower suppression), M3.6 (button suppression), M3.7 (goal hook) are deferred until the MVP set ships.

### M3.1 — power-up grant (4 items: Elephant, Fire, Bubble, Drill) — DEFERRED

The HamletDuFromage cheat DB gave us:

- NSO `+0x198B50` is the power-up state load site
- NSO `+0x020128F4` is the power-up state field
- Values: `2=Fire, 3=Elephant, 5=Small, 6=Drill, 8=Tall, 9=Pink/Bubble` (cross-confirmed with wondar's partly-RE'd `ItemGetType` enum in `include/game/actor/component/ItemGetRef.h`: `Super=1, Elephant=3, Drill=6, Bubble=9`)

**Approach**: rather than poke memory raw, find the *apply-powerup* function the engine calls when the player picks one up. Hook it once for read (confirm signature, find arg ordering), then call it from our code with the AP-granted type. This ensures animations, sound effects, and any side-effects (e.g., size box change) run correctly.

The event Nerve `vt_off=0x33fd870` fires on damage *and* power-up pickup (we observed this in M1 testing). Worth peeking that Nerve's vtable to see if it's a `RequestEventApplyPowerUp` family member.

### M3.2 — badge unlock (24 items, inverse of M2.3) — DEFERRED (separate path from M3.3 hash table)

**Status (2026-05-21)** — first attempt: 7 Ghidra scripts ([scripts/ghidra/find_badge_*.py](../scripts/ghidra/) and [scripts/ghidra/inspect_badge_*.py](../scripts/ghidra/)) failed to surface a usable grant function via string-based RE. Findings:

- `GiveBadgeIdOnCourseClear` and `UnlockBadgeIdOnCourseClear` resolve to **course-config property getters** (FUN_7101a592fc / FUN_7101b204a8), not action functions.
- `FUN_7101b1fb6c` is a **test harness**, not a real API.
- The acquisition-context strings (`BadgeShop`, `BadgeChallenge`, `BadgeHouse`, `BadgeMedley`) are label-uses only — no shared grant helper found.
- No `Add*` / `Grant*` / `Acquire*` / `*::Add` named functions exist in the badge family.

**Status (2026-05-22)** — save-diff identified the **save-file** byte-level write target: bit `internal_id` in the u64 at file offset `0x0EA0` of `game_data.sav`. 4/24 internal_id mappings confirmed (Coin Reward=9, Auto Super Mushroom=46, Parachute & Wall-Climb at {34,35}).

**Status (2026-05-23 → reframed 2026-05-24)** — ⚠️ the runtime memory anchor we located via the `savedata_id` UUID scan turned out to be the **save-OUT staging buffer**, not the live state. Writes to `live_state_base + 0x0EA0` are overwritten on the next save event (the game repopulates the buffer FROM live state on every serialization) and do NOT grant a badge in-game. The save-diff work yielded a **save-file editor capability** (offline modification of `game_data.sav`) but **NOT a live-grant mechanism**. See [docs/runtime-address-backtrace-plan.md](runtime-address-backtrace-plan.md) which documents the discovery.

**Status (2026-05-24)** — the M3.3 sprint 2 success **does NOT extend to M3.2**. Badges aren't in container A (the hash-keyed counter container that `FUN_710049F648` writes); they live in a distinct bitfield somewhere in `GameDataMgr` or one of its sub-structs.

**Path forward — find the LIVE badge bitfield location and/or grant function**:

1. **Find the badge grant function statically.** Run [scripts/ghidra/find_offset_constant_xrefs.py](../scripts/ghidra/find_offset_constant_xrefs.py) (Phase 2.1 of [docs/static-analysis-findings.md](static-analysis-findings.md)) — it scans `.text` for immediate-displacement uses of `0x0EA0`. The script is written but never executed. Most hits will be in the save serializer/deserializer; the gameplay-time writer (the actual badge-add function) is the target. Failing that, search for ARM64 `orr w?, w?, #(1 << N)` patterns near the bitfield base.
2. **Identify the LIVE badge bitfield offset within GameDataMgr.** The badge bitfield in memory probably lives at some `gmd::GameDataMgr::sInstance + <offset>` or `gmd::GameDataMgr::sInstance->[<sub_struct_ptr>]->[<offset>]`. The save serializer (which we can find via approach #1 since it must reference `0x0EA0` as a destination offset) will reveal both the source (live address) and destination (save-out buffer address). The source register at the relevant `ldr` or `memcpy` gives us the live base.

Defer until after the M3.3 smoke test lands; revisit when the LAN bridge ships and we want the badge-grant feature.

### M3.3 — Wonder Seed grant (124 items) — ★ STATIC RE SUCCEEDED (2026-05-24)

**Status (2026-05-21)** — first attempt: 4 Ghidra scripts + a runtime probe + a hash-reversing attempt failed to produce a usable grant entry point. Hash-reversing tried CRC32 / FNV-1/1a / DJB2 / SDBM against ~100 candidate stat names — no matches. Declared dead-end; pivoted to save-diff.

**Status (2026-05-24)** — second attempt **SUCCEEDED**. Full details in [docs/static-analysis-findings.md](static-analysis-findings.md).

The breakthrough was: (a) importing **all** of `switch-mod/syms/100/*.sym` (not just sdk.sym), which surfaced `gmd::GameDataMgr::sInstance @ NSO +0x0363F0F0` — an anchor the first attempt completely missed; (b) **dataflow-anchored xref harvesting** instead of string-grep, walking from the singleton outward to enumerate all 10+ accessor functions of the GameDataMgr API; (c) **cross-validation** against MemetendoYT's 8 verified hash keys to identify which accessor writes which kind of value.

Key results:

- **The grant primitive is `FUN_710049F648`**: `void (GameDataMgr*, uint32_t value, uint32_t hash)`. Lock-free, thread-safe (ARM exclusive-monitor atomics on the dirty queue), deferred-write (queues to ring buffer at `gmd->[+0xf8]`, drains on next save).
- **The singleton** is at NSO `+0x0363F0F0`. One dereference at runtime gives the live `GameDataMgr*`.
- **Confirmed via 3 flower_coin call sites** in the binary. Hash `0xf4ee6827` is passed in `w2`, value in `w1`, `this` in `x0`. Decompiled signature locked in.
- **The hash function** for course names is **Murmur3-32 with seed 0** (recovered statically from `FUN_71003D4110`, which iterates 81 hardcoded course strings). Textbook constants `0xcc9e2d51` / `0x1b873593` / `0xe6546b64` / `0x85ebca6b` / `0xc2b2ae35`. The first-attempt hash-reversing tried Murmur3 but apparently with the wrong seed/strings; the static decompile is definitive.
- **The field-name hashes** (MemetendoYT's 8) DON'T come from Murmur3 of obvious English names — they may use internal/Japanese strings or precomputed constants. Not blocking; we have the hashes already.

**Wireable from `main.cpp` in ~20 lines of C++.** Smoke test plan: `GrantFlowerCoin(99)` at boot → save → quit → diff `game_data.sav` → expect `0x0894 = 0x63 0x00` (u16 LE = 99).

The save-diff work from 2026-05-22..23 remains valid as a **byte-level verification target** — after writing via the hash API, the resulting save file should match save-diff's predicted byte changes.

**Status (2026-05-25)** — ✅ **counter primitive shipped + live-validated** as `probe::grantContainerACounter(hash, value)` in [switch-mod/src/program/main.cpp](../switch-mod/src/program/main.cpp). Boot-time smoke test on first `NerveActivateOnce` fire wrote `flower_coin=99` (hash `0xf4ee6827`); savediff confirmed `0x0890`'s value flipped from 6 → 99. Wire schema gained `GrantHashKeyedMsg` (`bridge/wire.py`) and `WireGrantHashKeyed` (`switch-mod/src/program/ap/ApProtocol.{hpp,cpp}`); inbound drain dispatches via `ApFrameBridge.cpp`. Bridge tests: 203 pass. Wonder Seed item routing per-seed isn't wired here because no AP item in the manual world maps to it; M2.6 course-correlation already attributes each WONDER_SEED_AWARDED nerve fire to a per-course AP location.

### M3.3b — Royal Seed grant (7 items) — ✅ shipped 2026-05-25

The 6 GRAND_SEED_WORLD{1..6} hashes (verified by MemetendoYT) live in the same pair-region table as flower_coin in the save file.  The 2026-05-25 falsification proved they're bool-typed and silently no-op'd by the container-A writer; the container-B bool writer at NSO `+0x0049EA24` was needed.

**Status (2026-05-25)** — ✅ **live end-to-end**. `probe::grantContainerBBool(hash, value)` in [switch-mod/src/program/main.cpp](../switch-mod/src/program/main.cpp) calls `FUN_710049EA24` (high-level wrapper) which gates on the gmd+0x68 init/lock and delegates to `FUN_7101F263FC(gmd+8, value & 1, hash)` — the deferred-write bool setter for the gmd+8 substruct.  Boot-time smoke test wrote all 8 documented bool hashes (6 Royal Seeds + COMPLETE_GAME + INTRO).  Trampoline log confirmed `substruct = gmd + 8` exactly as documented.

**Save-diff results**: 6 expected byte flips at pair-region value offsets:

| Hash | Pair offset | Result |
|---|---|---|
| `0x55815859` W1 Royal Seed | `0x0354` | already 0x01 (idempotent ✓) |
| `0x49ABBA86` W2 Royal Seed | `0x0064` | `00 → 01` ✓ |
| `0xB550D8D6` W3 Royal Seed | `0x0384` | `00 → 01` ✓ |
| `0x1DCF7F6E` W4 Royal Seed | `0x01F4` | `00 → 01` ✓ |
| `0x0D5A3E00` W5 Royal Seed | `0x036C` | `00 → 01` ✓ |
| `0xD4660D2B` W6 Royal Seed | `0x00BC` | `00 → 01` ✓ |
| `0x5D3EC9B4` COMPLETE_GAME | `0x0044` | `00 → 01` ✓ |
| `0x89F1CC52` INTRO_CUTSCENE_COMPLETED | `0x012C` | already 0x01 (idempotent ✓) |

**Lessons-learned**: the bool writer was already statically identified in sprint 2 — `FUN_710049EA24` shows up in the GameDataMgr xref table with 14 callers, and the delegate `FUN_7101F263FC` was explicitly tagged as the "deferred-write bool WRITER for gmd+8 substruct" in [static-analysis-findings.md](static-analysis-findings.md) line 3215+3327.  An existing `GmdBoolWriter` probe trampoline at NSO `+0x01F263FC` (installed during M3.2 badge investigation) provided free observability for the smoke test.  Implementation was ~90% reading our own notes, ~10% writing the primitive that mirrored `grantContainerACounter`.

**Dispatch**: [ApFrameBridge.cpp](../switch-mod/src/program/ap/ApFrameBridge.cpp) `drainInbound()` branches on `isBoolHash(h)` (defined in `ApFrameBridge.hpp` with the 8-hash whitelist) — bool hashes route to `grantContainerBBool`, counters stay on `grantContainerACounter`.  Bridge cleanup landed: `royal_seed_table.py` `source` flipped to `"live"`, docstring updated; `ap_client._handle_received_items` WARN replaced with INFO.

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

### M4.5 — Grant persistence across save/reload (new, 2026-05-24; ✅ shipped 2026-05-25)

First M4 smoke test showed that the live writers (badges, container-A
counters, container-B bools) all write to deferred-write buffers or
non-persistent state that save/reload can revert.  The fix landed in
two passes:

- **Badges**: M4 follow-up #2 (commit
  [9a5716c](https://github.com/mdietz94/wondar/commit/9a5716c))
  switched to AP-authoritative absolute-overwrite via
  `SetBadgesAbsoluteMsg` on every `ReceivedItems`, every `HelloMsg`,
  and a 2 s periodic tick.  Subsumes "replay every badge grant" with
  a stronger guarantee (covers in-game pickups too).
- **Royal Seeds (container-B bools)**: M4.5 proper
  (`SMBWContext._collect_royal_seed_grants` →
  `LanServer._push_royal_seeds_now`) re-emits one
  `GrantHashKeyedMsg` per seed on every Switch `HelloMsg`.  No periodic
  tick — Royal Seeds have no in-game acquisition path that bypasses
  AP.  Idempotent at the Switch primitive level (`probe::grantContainerBBool`
  setting a bool to 1 when it's already 1 is a no-op).

Container-A counters (flower_coin, regular_coin) and the two
container-B completion bools (COMPLETE_GAME, INTRO) are NOT replayed
today because they're not AP items.  When/if they become AP items, the
same `_collect_*_grants` / `_push_*_now` provider shape extends
trivially.

## M5 — Convert manual_smbwonder_zim to integrated apworld + suppress in-game item acquisition

The existing `manual_smbwonder_zim` is an Archipelago Manual world (player ticks checkboxes by hand in the AP client). With M2 + M3 + M4 working, we replace the manual ticking with auto-detected events from the Switch mod.

**Blocker uncovered in M4 first-run (2026-05-24, ✅ closed 2026-05-25 for
badges)**: in-game badge acquisition (Poplin shop, badge house, badge
tutorial) bypassed AP.  Rather than RE'ing the per-path grant call sites
and suppressing each one, M4 follow-up #2 made the bridge the sole owner
of the badge bitfield: it overwrites the Switch's container-C bitfield
to AP's known mask on every `ReceivedItems`, every `HelloMsg`, and a
~2 s periodic tick (see [CLAUDE.md](../CLAUDE.md) "M4 follow-up #2"
section and the [bridge/lan_server.py](../bridge/lan_server.py)
`_badge_sync_loop`).  Any in-game pickup is reverted to AP's view within
seconds; AP is the sole authority.  **Same absolute-overwrite pattern
will be needed for Wonder Seeds, power-ups, characters once those
grants land** — the M4 follow-up #2 design is the template.

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

## Recommended pacing (revised 2026-05-25)

History (closed):
- ✅ Session 1: M1 — Wonder Seed + Course Clear nerves.
- ✅ Session 2: M2.4 + M2.5 — PlayReport IPC capture, Python decoder, full corpus of 9 live fixtures.
- ✅ Session 3: M2.6 bridge skeleton — state + protocol + processor, 106 tests passing.
- ❌ Sessions 4 + 5 (2026-05-20→21): M3.2 + M3.3 + M3.3b first-pass Ghidra attempts declared dead-end after 11 scripts. **Pivoted** to save-diff.
- ✅ Sessions 6 + 7 (2026-05-22→23): save-diff sprint. Byte-exact write targets identified for badges (`0x0EA0`), 16+ per-course u32 arrays, and pair-region keys. Cross-verified with MemetendoYT/SMBW-SaveGame-Editor.
- ✅ Session 8 (2026-05-24): **static-analysis sprint 2 — succeeded**. `FUN_710049F648(gmd, value, hash)` confirmed as the M3.3 grant primitive. 5 of 8 MemetendoYT keys located live in code. Murmur3-32 hash function recovered. Full details in [docs/static-analysis-findings.md](static-analysis-findings.md). 5 new Ghidra scripts in [scripts/ghidra/](../scripts/ghidra/), all sprint-2 tagged.
- ✅ Session 9 (2026-05-24): **M3.2 badge primitive + M4 LAN bridge** shipped end-to-end. `probe::grantBadgeBit` validated live with Spring Feet; bridge `LanServer` + `SMBWContext` route the AP `/send` flow.
- ✅/❌ Session 10 (2026-05-25): **M3.3 counter shipped + M3.3b Royal Seed falsified**. `probe::grantContainerACounter` (counter writer) live-validated with `flower_coin` (6 → 99 at file offset 0x0894).  Same primitive + bridge plumbing called for `GRAND_SEED_WORLD1` (hash 0x55815859) returned cleanly but produced no save-file change — container-A writer is typed and no-ops on bool slots.  Royal Seed routing kept wired on the bridge with a warning log; Switch-side needs container-B writer.  Wire schema gained `GrantHashKeyedMsg`; tests: 203 pass (181 + 22 new).
- ✅ Session 11 (2026-05-25, same day): **M3.3b shipped**. `probe::grantContainerBBool` wired around the static-analysis-findings.md-documented `FUN_710049EA24` (NSO +0x0049EA24); `ApFrameBridge.cpp` `drainInbound` branches on `isBoolHash()` to route bool hashes to container-B.  Boot-time 8-grant smoke validated end-to-end: save-diff showed 6 expected pair-region byte flips; W1 + INTRO were idempotent no-ops because already `0x01` in the test save.  Trampoline log confirmed `substruct = gmd + 8`.  Bridge cleanup: `royal_seed_table.py` `source` flipped to `"live"`, WARN replaced with INFO.  203 bridge tests still pass.

Forward plan (revised 2026-05-25):

- ✅ Session 12 (2026-05-25, same day): **M4.5 Royal Seed replay-on-HelloMsg shipped**.  Bridge-side `_collect_royal_seed_grants` + `_push_royal_seeds_now` re-emit one `GrantHashKeyedMsg` per received Royal Seed every Switch handshake; mirrors the M4 follow-up #2 badge replay.  M3.3b boot smoke was already removed when M3.3b merged — `main.cpp` now contains only the unrelated M3.8 synthKill smoke.  209 bridge tests pass (207 + 2 new HelloMsg-replay tests).
- **Session 13 (next)**: M3.8 DeathLink detection — extend `NerveActivateOnce` to filter on `vt_off=0x33fd9a8`, find a death-vs-noise discriminator. Switch-mod only, no RE dead-ends.
- **Session 14**: DeathLink triggering (the "kill Mario from AP" half) — verify the `LiveBaseLatch` cheat-anchor prologue in Ghidra, flip `kEnableLiveBaseLatch` to true, validate `probe::synthKill`.

Deferred indefinitely until after the MVP demo:
- M2.2 (10-coin) — ✅ scoped 2026-05-25, bridge-side implementation pending in [docs/m2.2-runbook.md](m2.2-runbook.md). No Ghidra work needed.
- M3.1 (power-up grant), M3.4 (characters), M3.5 (Wonder Flower / Effect suppression), M3.6 (button suppression), M3.7 (goal hook).
- M5 (replace manual_smbwonder_zim with integrated apworld).
- M6 (hardware Switch), M7 (UX polish).
