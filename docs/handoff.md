# SMBW Archipelago — handoff doc

> Last updated: 2026-05-26 — **AP-authoritative Wonder Seed gate
> override wired end-to-end (M3.3 production path)**.  The earlier
> "0x8c20ccb7 is the lifetime Wonder Seed counter" note was wrong;
> `0x8c20ccb7` is one of **five mirror hashes of the per-current-world
> Wonder Seed count**, not a lifetime total.  See the
> [smbwap-wonder-seed-gate-solved](C:\Users\maxwe\.claude\projects\C--Users-maxwe-Documents-smwonder-archipelago\memory\smbwap_wonder_seed_gate_solved.md)
> memory and `docs/static-analysis-findings.md` "2026-05-26 ✅ HYPOTHESIS
> CONFIRMED" for the full discovery trail.
>
> **The gate problem**: ``FUN_71001787b40`` @ NSO ``+0x1787b40`` is the
> Wonder Seed gate predicate.  It reads container-A hash ``0x390eb960``
> for the *current world's* seed count and compares against a per-gate
> threshold.  Five hashes mirror that count and update in lockstep on
> every seed-count change:
>
>   - ``0x21f89ab1``
>   - ``0x8c20ccb7``  (the value formerly assumed to be "lifetime" --
>                     actually per-current-world; resets on world
>                     transitions to whatever the per-course bitfield
>                     recompute produces)
>   - ``0xeeff353b``
>   - ``0x390eb960``  (the one the gate predicate reads)
>   - ``0xa0e5f253``
>
> **Live-validated 2026-05-26**: Switch-side
> ``probe::pushWonderSeedOverride(value)`` writes ``value`` to all 5 via
> the container-A counter writer (``FUN_710049F648``).  With value=99
> hard-coded, a W3 gate that previously denied entry on the test save
> (1 actual W3 seed) opened on the next attempt and the in-game UI
> counter showed 99.  Confirms AP can be the sole authority over Wonder
> Seed gating without touching per-course persistent storage.
>
> **The production wiring (shipped 2026-05-26)** — same
> idempotent-absolute-overwrite pattern as badges, plumbed
> through:
>   1. New wire message ``SetWonderSeedCountsMsg { counts: [u32; 8] }``
>      ([wire.py](../apworld/smbw_archipelago/client/wire.py),
>      ``WireSetWonderSeedCounts`` in
>      [ApProtocol.hpp](../switch-mod/src/program/ap/ApProtocol.hpp))
>      indexed by AP item bucket (W1=0..W6=5, Petal Isles=6, Special=7).
>   2. [wonder_seed_table.py](../apworld/smbw_archipelago/client/wonder_seed_table.py)
>      maps the 8 Wonder Seed AP item names to their bucket indices.
>   3. [``SMBWContext._recompute_wonder_seed_counts``](../apworld/smbw_archipelago/client/context.py)
>      walks ``items_received`` and returns ``list[int]`` of length 8.
>   4. [``LanServer.send_set_wonder_seed_counts``](../apworld/smbw_archipelago/client/lan_server.py)
>      pushes the array on the same three triggers as badges:
>      every ``ReceivedItems``, every Switch ``HelloMsg``, every ~2 s
>      tick (``_badge_sync_loop``).
>   5. Switch worker thread routes the message via
>      [ApClient.cpp](../switch-mod/src/program/ap/ApClient.cpp)
>      ``InboundKind::SetWonderSeedCounts`` to the inbound ring;
>      [drainInbound](../switch-mod/src/program/ap/ApFrameBridge.cpp)
>      caches the 8 values into ``g_wonder_seed_counts[8]`` (atomic).
>   6. [main.cpp](../switch-mod/src/program/main.cpp) NerveActivateOnce
>      tick (~2 s cadence under normal play) replaces the iteration-5
>      hard-coded ``pushWonderSeedOverride(99)`` smoke with: read
>      container-A hash ``0x9f5ead3c`` (live current-world index, 1..8),
>      map to bucket index (0..7), call
>      ``probe::pushWonderSeedOverride(getWonderSeedCount(bucket))``.
>      Values outside [1, 8] preserve the game's natural state.
>
> AP becomes the sole authority over Wonder Seed gating: any in-game
> pickup (Wonder phase grab, flag-pole goal seed, 10-coin reward) and
> the natural world-transition recompute both get clobbered back to AP's
> view within ~2 s.  Idempotent absolute-overwrite, no double-counting.
>
> **Per-world bucket convention** (encoded both in
> ``wonder_seed_table.py`` and the Switch's ``world_val -> bucket``
> map): W1=1→0, W2=2→1, ..., W6=6→5, Petal Isles=7→6, Special=8→7.
> The W2 and W3 values are observed live; W1/W4-W6 follow the natural
> 1-indexed sequence; Petal Isles and Special at 7/8 are **tentative**
> -- if the live values turn out to be something else, fix the map in
> [main.cpp](../switch-mod/src/program/main.cpp) without touching the
> bridge-side table.
>
> **Scope caveat — gate override only, not per-course storage**:
> ``pushWonderSeedOverride`` makes gates pass for the *current world*
> based on AP's count, but does NOT write the per-course Wonder Seed
> bitfield in persistent storage (file offset ``0x3AF8 + 4*course_index``
> -- writer not yet RE'd; see the
> [smbwap-wonder-seed-counter-candidate](C:\Users\maxwe\.claude\projects\C--Users-maxwe-Documents-smwonder-archipelago\memory\smbwap_wonder_seed_counter_candidate.md)
> memory for the open discovery work).  As long as the tick is running,
> the game won't notice; if the player saves+quits and AP is offline on
> next load, per-course completion markers and the lifetime counter
> revert to the genuine in-game state.  This is fine for the gate-pass
> goal; it's the open follow-up for proper persistence.

> **2026-05-25 layout change** — the headless `bridge/` package and the
> Manual-template `manual_smbwonder_zim/` apworld were replaced by a
> first-class **SMBWonder** apworld + Kivy client at
> [apworld/smbw_archipelago/](../apworld/smbw_archipelago/).  Archipelago
> Launcher now exposes a "SMBW Client" button.  Every `bridge/*.py`
> path below has moved to `apworld/smbw_archipelago/client/*.py`;
> `python -m bridge ...` is replaced by
> `python -m apworld.smbw_archipelago.client.main ...` (or the Launcher
> button).  See CLAUDE.md "Launching the SMBW Client" for the new flow.

Last updated: 2026-05-25 — **M3.7 shipped** (game-completion goal hook
wired Switch ↔ bridge ↔ AP).  RE'd the
`SetFlagEndDispMsgFirstVisitedWorldAfterClearedLastBoss` Nerve at NSO
`+0x15b77a8` (vtable `+0x3363330`, slot 8) statically by walking the
single ADRP+ADD that computes the string at NSO `+0x295d801` ->
getter at NSO `+0x15b7790` -> R_AARCH64_RELATIVE entry pointing the
vtable slot 0 at it -> vtable slot 8 = execute.  Zero direct BL
callers and not in `FUN_7100559f7c` xref list -> one-shot Nerve (same
flavor as M1.3 SetCourseClearFlagToGameData).  Bridge processor emits
`GoalCompleted` deduped via `BridgeState.mark_goal_complete()`;
`SMBWContext.handle_goal_completed` sends
`StatusUpdate(ClientStatus.CLIENT_GOAL=30)`.  255 apworld tests green.
Live-validation pending the player actually beating final Bowser.

Last updated: 2026-05-25 — **M3.3 + M3.3b + M3.8 all shipped** (M3.8
end-to-end live-validated: latched `live_base=0x20a1f27030`, `synthKill`
wrote HP=0 at +0x38, Mario died on first frame).  Production path is
AP Bounce -> `SMBWContext.on_deathlink` -> `LanServer.send_kill` ->
Switch `drainInbound` -> `probe::synthKill` (no constexpr gates).
Container-A counter writer (`probe::grantContainerACounter`) shipped in
[switch-mod/src/program/main.cpp](../switch-mod/src/program/main.cpp) and
live-validated with `flower_coin` (6 → 99 at file offset `0x0894`).
Container-B bool writer (`probe::grantContainerBBool` → NSO `+0x0049EA24`)
shipped in the same file and live-validated 2026-05-25 with all 8
documented bool hashes — save-diff confirmed 6 byte flips at expected
pair-region offsets (W2 @ 0x0064, W3 @ 0x0384, W4 @ 0x01F4, W5 @ 0x036C,
W6 @ 0x00BC, COMPLETE_GAME @ 0x0044); W1 and INTRO were idempotent no-ops
because they were already `0x01` in the test save.  Wire schema +
`GrantHashKeyedMsg` reused as-is; `drainInbound()` branches on
`isBoolHash()` to route bool hashes to container-B and counters to
container-A.  Bridge cleanup landed: `royal_seed_table.py` flipped
`source="live"`, `ap_client._handle_received_items` WARN replaced with
INFO.  203 bridge tests pass.  See also: M4 LAN bridge end-to-end
(2026-05-24) — the shipped foundation this session built on.

This is the "next session, hi me again" doc. Read it first.

## M3.3 + M3.3b both shipped (2026-05-25)

End-to-end paths exist for **container-A counter** grants via
`FUN_710049F648` at NSO `+0x0049F648` AND **container-B bool** grants
via `FUN_710049EA24` at NSO `+0x0049EA24`.  Live smoke tests proved
both: `flower_coin` 6 → 99 (counter), 6/8 expected bool hashes flipped
in the save (the other 2 were already set, idempotent).

- **Switch primitive**: `probe::grantContainerACounter(hash, value)` in
  [switch-mod/src/program/main.cpp](../switch-mod/src/program/main.cpp).
  Reuses the existing `probe::gmdSingleton()` helper from M3.2; calls the
  writer via a function-pointer cast at NSO+0x49F648.
- **Wire protocol**: new `grant_hash_keyed { hash: u32, value: u32 }` message
  type.  Mirrors the existing `grant_badge` plumbing on both sides
  ([bridge/wire.py](../bridge/wire.py),
  [ApProtocol.{hpp,cpp}](../switch-mod/src/program/ap/ApProtocol.hpp),
  [ApFrameBridge.{hpp,cpp}](../switch-mod/src/program/ap/ApFrameBridge.hpp),
  [ApClient.cpp](../switch-mod/src/program/ap/ApClient.cpp)).
- **AP routing**: [bridge/royal_seed_table.py](../bridge/royal_seed_table.py)
  maps the 6 AP item names (`"W1 Royal Seed"` .. `"W6 Royal Seed"`) to their
  MemetendoYT-verified hash keys; [ap_client.py](../bridge/ap_client.py)
  routes them via `lan_server.send_grant_hash_keyed(hash, 1)`.

### M3.3 ✅ — counter writer works

Smoke test (2026-05-25): `probe::grantContainerACounter(0xf4ee6827, 99)`
fired from first `NerveActivateOnce` callback.  Save + quit, then
`python scripts/savediff.py game_data.sav.pre game_data.sav` shows:

```
[pair  269 @ 0x0890]  key=0xf4ee6827           6 → 99          (change (+93))
```

`regular_coin` should work via the same primitive (same writer, same
container, u8 truncation pattern documented in static-analysis-findings).

### M3.3b ✅ — Royal Seed bool writer shipped

`probe::grantContainerBBool(hash, value)` calls `FUN_710049EA24` at NSO
`+0x0049EA24` — the high-level bool writer wrapper which checks the
gmd+0x68 init/lock and delegates to `FUN_7101F263FC(gmd+8, value & 1, hash)`
(the deferred-write bool setter for the gmd+8 substruct).  Smoke test
2026-05-25 wrote all 8 documented bool hashes; trampoline log confirmed
`substruct = gmd + 8` exactly as documented in
[static-analysis-findings.md](static-analysis-findings.md) line 3215.

**Lessons-learned**: the bool writer was already statically identified
in sprint 2 — `FUN_710049EA24` shows up in the GameDataMgr xref table
(line 317) with 14 callers, and the delegate `FUN_7101F263FC` was
explicitly tagged as the "deferred-write bool WRITER for gmd+8 substruct"
(line 3327).  An existing `GmdBoolWriter` probe trampoline at NSO
`+0x01F263FC` (installed during M3.2 badge investigation) provided free
observability for the smoke test.  The M3.3b work was ~90% reading our
own notes, ~10% writing the primitive that mirrored `grantContainerACounter`.

**Save-diff results** (2026-05-25, mid-game profile):

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

**Dispatch** (`ApFrameBridge.cpp` `drainInbound`): `isBoolHash(h)` checks
the 8-hash whitelist in `ApFrameBridge.hpp`; bool hashes route to
`grantContainerBBool`, counters to `grantContainerACounter`.

**Bridge cleanup landed**: `royal_seed_table.py` `source` flipped to
`"live"`, docstring updated; `ap_client._handle_received_items` WARN
replaced with INFO matching the badge branch pattern.

## M3.8 DeathLink — shipped end-to-end 2026-05-25

Plan: [`.claude/plans/let-s-work-on-m3-8-cosmic-hickey.md`](../.claude/plans/let-s-work-on-m3-8-cosmic-hickey.md).
Live-validated end-to-end on Ryujinx 2026-05-25 09:00 (run log
`Ryujinx_1.3.3_2026-05-25_09-00-34.log`): latched
`live_base=0x20a1f27030`, `probe::synthKill` wrote int16 `0` at
`+0x38`, Mario died on the first frame.

### Production wire path

**Outbound (Switch death -> AP Bounce)**:
1. `NerveActivateOnce::Callback` filters the noisy `vt_off=0x33fd9a8`
   scene-transition Nerve: only `*(u32)(nerve+0x18) == 0x04` (Mario
   death) enqueues `NerveKind::DeathDetected`.  Controlled exit
   (`== 0x84`), and any unobserved sibling enums, are logged as
   `SCENE_TRANSITION non-death (state=0xN)` and dropped.
2. `probe::g_synthetic_death_this_frame` consumes-once via `exchange`
   to suppress the echo when the death we'd just detected was caused
   by our own inbound `synthKill`.
3. `enqueueNerveFire(DeathDetected, seq)` -> outbound ring ->
   `ApClient` worker -> JSON `{"t":"nerve","kind":"death_detected",...}`
   -> bridge's `LanServer._dispatch_line`.
4. `processor._handle_nerve_fire(DEATH_DETECTED)` -> `state.bump_death_count()`
   + emits `DeathReported(seq)`.
5. `LanServer._run_processor` -> `on_death_reported` -> bound to
   `SMBWContext.handle_death_reported` in `bridge.__main__`.
6. `handle_death_reported` calls `CommonContext.send_death("mario_died")`
   iff `self.deathlink_enabled` (set from `slot_data["death_link"]`
   on `Connected`).
7. CommonClient writes the Bounce: `{"cmd":"Bounce","tags":["DeathLink"],"data":{...}}`.

**Inbound (AP Bounce -> Switch death)**:
1. AP server delivers `Bounce` to our DeathLink-tagged slot.
2. CommonContext routes to `SMBWContext.on_deathlink(data)`.
3. Override forwards to `LanServer.send_kill(source, cause)` unless
   `source == self.player_names[self.slot]` (belt-and-braces self-ping
   guard over CommonClient's timestamp check).
4. `LanServer._writer_loop` ships `KillMsg(source, cause)` as
   `{"t":"kill","source":"...","cause":"..."}` to the Switch.
5. Switch `ApClient::handleLine` decodes via `parseKill` into
   `WireKill` and pushes onto the inbound SPSC ring.
6. `ApFrameBridge::drainInbound`'s `case InboundKind::Kill` calls
   `probe::synthKill()` from the game thread.
7. `probe::synthKill` sets `g_synthetic_death_this_frame=true` then
   writes `int16(0)` to `g_live_base + 0x38`.  `g_live_base` is
   captured by `PlayerTickLatch` (trampoline hook on `FUN_7100273868`
   entry at NSO `+0x273868`), which walks
   `param_1 -> +0x10 -> +0x208 -> ver-conditional +0 or +0x118 -> HP
   struct` and latches the result once.
8. The very next tick of `FUN_7100273868` re-reads HP at +0x38, sees
   `<= 0`, and takes the `b.le LAB_710027593c` death branch.  Mario
   dies on screen; the loop-guard atomic from step 7 suppresses the
   outbound echo when `0x33fd9a8` fires as a consequence.

### Test coverage

240 bridge tests green (224 prior + 16 new across `test_wire`,
`test_processor`, `test_lan_server`, `test_ap_client_deathlink`):
- `TestRoundTrip.test_kill_*` (4): KillMsg encode/decode + truncation.
- `TestDecodeErrors.test_kill_*` (3): missing/non-string field
  rejections.
- `TestDeathTracking.test_death_detected_*` (2): processor emits
  `DeathReported`, never a `CheckEmitted`.
- `TestKillOutbound`, `TestDeathReportedDispatch` (3): LAN-server
  round-trip + death-route through processor.
- `TestApClientDeathLink` (9): on_deathlink forwarding, self-source
  drop, slot_data wiring, `handle_death_reported -> send_death` gating.

### Untested scenarios (low-risk follow-ups)

- World-map travel, palace boss clear, pause-quit, and file-select
  scene transitions were NOT in the 5-event observation pass.  If any
  fire `0x33fd9a8` with yet another `+0x18` enum, the whitelist
  conservatively drops them and the dump logs the new state -- extend
  the filter if observed in normal play.
- `PlayerTickLatch` captures once per session.  Cross-life persistence
  is fine because Mario's HP struct address can change but the latch
  re-runs from a fresh session.  An adversarial Switch reboot mid-AP-
  session would currently leave `g_live_base = 0` until the level is
  re-entered; an inbound DeathLink in that window is dropped with a
  `synthKill: no live_base latched yet` log line.

### M3.3 verification recipe

Reproducing the smoke-test win (for future regression checks):

1. Temporarily re-add the smoke block to `NerveActivateOnce::Callback`
   right after `drainInbound()`:
   ```cpp
   static std::atomic_flag s_smoke_fired = ATOMIC_FLAG_INIT;
   if (!s_smoke_fired.test_and_set()) {
       probe::grantContainerACounter(0xf4ee6827, 99);
   }
   ```
2. Snapshot save, build + deploy, enter any course, save, quit.
3. `python scripts/savediff.py <pre>.sav <post>.sav` → expect pair 269's
   value to flip from `current → 99`.

## M4 follow-ups

End-to-end smoke test on 2026-05-24 validated the full pipeline (AP server
`/send Spring Feet Badge` → bridge → Switch worker → container-C bitfield
→ badge appears live in equip menu).  Two production gaps surfaced during
play; **both are now closed for badges** by the M4 follow-up #2 work
shipped 2026-05-25:

1. ✅ **Badge grants now survive save/reload** (was: open).  The bridge
   pushes a `SetBadgesAbsoluteMsg(bits=mask)` to the Switch on every AP
   `ReceivedItems`, on every Switch `HelloMsg` (replay-on-reconnect),
   and on a ~2 s periodic tick.  The Switch's
   `probe::setBadgeBitfieldAbsolute` overwrites the entire container-C
   bitfield to that exact set.  Idempotent by construction; subsumes
   the originally-planned M4.5 replay-on-HelloMsg pattern.

   ⚠️ **Container-A grants (Royal Seeds, coins, COMPLETE_GAME,
   INTRO_CUTSCENE) still don't survive save/reload.**  The
   `FUN_710049F648` writer queues to a dirty buffer at `gmd->[+0xf8]`
   that flushes on next save; load-fresh-save reverts.  A parallel
   container-A replay design (same pattern as `SetBadgesAbsolute`)
   needs to land before those grants are durable.  Currently the
   bridge issues `GrantHashKeyedMsg` per item on `ReceivedItems` only
   — no tick, no HelloMsg replay.
2. ✅ **In-game badge acquisition is now AP-authoritative** (was: open).
   The ~2 s tick overwrite cycle reverts any badge the player picks up
   in-game (Poplin shop, badge house, badge medley, badge challenges,
   badge tutorial) to AP's known set within seconds.  No call-site
   suppression was needed — the absolute-overwrite makes per-path
   suppression unnecessary.  Same pattern will apply to power-ups /
   characters once M5 wires them up.

   ⚠️ Side effect: a player who buys a badge from a Poplin shop sees
   it disappear from their inventory ~2 s later (and they're still
   short the coins).  This is intentional — AP is the sole authority.
   The accompanying shop-coin debit / shop-purchase-as-LocationCheck
   work is M5 (see milestones.md).

## TL;DR — where you are right now

**M1 done. M2.4 now spec-complete on the Switch side via the IPC-layer pattern.**

- **M1**: Both critical-path hooks work end-to-end. `WONDER_SEED_AWARDED` fires on every Wonder Seed grab; `COURSE_CLEARED` fires on every successful flagpole touch *and* on Royal Seed palace clears. **330 of 663 AP checks covered (49.8%)**.
- **M2.4 (real hardware)**: After a multi-step bisect on 2026-05-20, the working pattern is:
  1. Hook `nn::prepo::PlayReport::SetEventId` to capture the **room name** (event id) — safe.
  2. Hook the IPC-client `CmifProxyImpl<IPrepoService>::_nn_sf_sync_SaveReport{,WithUser}` to capture the **already-serialized payload bytes** — safe.
  3. The decoder runs in the **PC-side Python bridge** (M4), not on Switch — bytes ship over the wire.

  **Critical**: hooking PlayReport class members `Save()`/`Save(Uid&)`/any `Add(...)` overload triggers a delayed SDK abort on a *different thread* depending on which validator notices first (ModuleSystemWorker1, gmd::SaveDataMgr observed; see "What didn't work"). The IPC client layer below the PlayReport class is below those validators and works cleanly.

  **What we captured on 2026-05-20 to prove the path**:
  - `room=course_in pay=0x... size=355 flags=0x0` from entering Bulrush Coming Through
  - First 64 bytes of the payload, e.g. `de 00 0f ab 73 61 76 65 64 61 74 61 5f 69 64 d9 23 62 38 31 33 65 36 37 35...`
  - Ryujinx's prepo decoder confirmed the same buffer represents: `stage_info.stage_key=2308078743, world_no=1, course_no=30, lucky_coin=135, world_wonder_flower=14, equip_badge_id=[34]`, etc.

**Status snapshot (2026-05-20 end)**:

| Surface | Coverage | Notes |
|---|---|---|
| M1 — Wonder Seed nerve + Course Clear nerve | ✅ 330 AP checks | nerve hooks fire reliably across all tested scenarios |
| M2.4 — PlayReport capture (real-hardware path) | ✅ done | SetEventId + IPC SaveReport hooks; Python decoder lives in [bridge/play_report.py](bridge/play_report.py); 87 tests pass against 9 live fixtures |
| M2.5 — Exit-type discrimination | ✅ 199/199 structurally classifiable | mapping table in `TestM25ExitTypeMapping`; only Fake Exit `goal_id` value (guessed 2) lacks live capture; palace WIN+LOSS both captured |

**Status update (2026-05-21)**:

- ✅ **M2.6** done — bridge skeleton + course correlation; 106 Python tests passing (`bridge/`).
- ❌ **M3.2 + M3.3 + M3.3b grant-function RE failed.** 11 Ghidra scripts plus a runtime probe characterized the badge system as "no exposed grant API" (label strings only — UI / state-machines / log) and the wonder-seed system as a generic counter getter keyed by 32-bit hashes of internal stat names where SMBW uses a custom hash function none of CRC32 / FNV / DJB2 / SDBM / Murmur3 reproduce. **All three are now deferred to a save-diff sprint** ([save-diff-grants.md](save-diff-grants.md)).

**Status update (2026-05-22)** — first badge capture round complete (⚠️ findings later proved to be save-file-editor capability only, NOT a live-grant path — see 2026-05-24 status above):

- Captured pre/post for "buy Coin Reward Badge from Poplin Shop" (-30 flower coins) and pre/post for "swap equipped badge Wall-Climb → Auto Super Mushroom". Both diffs in [docs/save-diff-findings.md](save-diff-findings.md).
- **File-offset for badge ownership identified**: bit `internal_id` in the u64 at file offset `0x0ea0` of `game_data.sav`. ⚠️ The corresponding in-memory address (found later via UUID scan) turned out to be the save-OUT staging buffer; writes there don't affect live gameplay. Still useful as the byte-level verification target after a successful live grant.
- **Hash key for flower coins identified**: `0xf4ee6827`. Static analysis on 2026-05-24 confirmed this writes via `FUN_710049F648` (container-A writer) and produces a real live change.
- 4 of 24 badge mappings confirmed: Coin Reward → internal 9, Auto Super Mushroom → internal 46, Parachute & Wall-Climb at {34, 35}. **No grant function for badges has been found** — the trailing-region bitfield write only modifies the save-out buffer. M3.2 needs separate static RE (or a different approach) for live grants.
- M3.3 corpus correction: key `0x17f0bb21` is `regular_coin_count`, not `play_time_sec`.
- New tools: [scripts/badge_map_builder.py](../scripts/badge_map_builder.py) (incremental apworld → SMBW internal_id table builder), [scripts/find_equip_hashes.py](../scripts/find_equip_hashes.py) (one-off hash lookup), [scripts/analyze_badge_capture.py](../scripts/analyze_badge_capture.py) (one-off region cross-reference).

**Status update (2026-05-21 PM)** — kicked off the save-diff sprint:

- Reverted the M3.3 runtime probe in `switch-mod/src/program/main.cpp` (no longer producing useful data; freed 80 LoC + 1 hook install).
- Located the SMBW save: `%APPDATA%\Ryujinx\bis\user\save\0000000000000002\<user>\game_data.sav` (21,876 bytes; plaintext; 87.6% zero bytes; magic `04 03 02 01`).
- Characterized the save: first 0x400 bytes after the header are **128 entries of (u32 hash_key, u32 value)** — the SAME hash-keyed counter table the M3.3 probe was reading in memory. The diff yields ready-to-use 32-bit hash keys with zero hash-function work needed. Full layout in [save-diff-grants.md "Format we mapped on 2026-05-21"](save-diff-grants.md#format-we-mapped-on-2026-05-21).
- Built [scripts/savediff.py](../scripts/savediff.py) + [scripts/test_savediff.py](../scripts/test_savediff.py) — diff tool with classification (`first-acquire`, `increment by 1`, `bit N flip`, generic `change`) and a `--summary` mode for single-save inspection. 13 unit tests pass.
- Whole test suite now 119 OK (106 bridge + 13 savediff).

**Status update (2026-05-24)** — **save-diff was a dead-end for live grants; static-analysis sprint 2 succeeded** (full details in [docs/static-analysis-findings.md](static-analysis-findings.md)):

★ **CRITICAL — what the save-diff work actually produced.** The buffer we located via the `savedata_id` UUID scan (2026-05-23) turned out to be the **save-OUT staging buffer**: it only exists during/after save serialization, the game populates it FROM the live state, and writes into it are discarded the moment the game refreshes from live state. **Writing to those offsets does NOT change live gameplay** (badge ownership doesn't appear, course-clear flags don't register, etc.). What we have from the save-diff sprint is a **save-file editor capability** — useful as a verification tool (we can predict and confirm the bytes the game writes on save) but NOT a grant mechanism. See [docs/runtime-address-backtrace-plan.md](runtime-address-backtrace-plan.md) which documents this discovery and outlines the Cheat-Engine-based path that the sprint-2 static analysis ultimately obviated.

- ★ **The previous M3 dead-end was wrong.** A second static-analysis pass (with imported sym files, dataflow-anchored xref harvesting, and cross-reference against MemetendoYT + HamletDuFromage cheat DB) identified the entire GameDataMgr API surface — **the ONLY live-grant path we've found**. Plan + scripts in [scripts/ghidra/](../scripts/ghidra/) (sprint-2 inventory in [scripts/ghidra/README.md](../scripts/ghidra/README.md)).
- ★ **The grant function is `FUN_710049F648`** — Container A counter writer. Signature `(GameDataMgr*, uint32_t value, uint32_t hash)`. Lock-free + thread-safe (ARM exclusive-monitor atomics on the dirty queue). Deferred-write (queues to `[gmd + 0xf8]` ring buffer; drains at next save). Confirmed via decompile + 3 confirmed flower_coin call sites.
- ★ **`gmd::GameDataMgr::sInstance` @ NSO `+0x0363F0F0`** — singleton root pointer. Was sitting in [switch-mod/syms/100/gmd/GameDataMgr.sym](../switch-mod/syms/100/gmd/GameDataMgr.sym) the whole time; the previous sprint never grep'd the sym files. One dereference replaces the entire Cheat Engine pointer-scan workflow from [docs/runtime-address-backtrace-plan.md](runtime-address-backtrace-plan.md).
- ★ **MemetendoYT 8 keys cross-verified live in code** — flower_coin (`0xf4ee6827`), regular_coin (`0x17f0bb21`), course-clear (`0xdf82e9ab`), INTRO (`0x89f1cc52`), COMPLETE_GAME (`0x5d3ec9b4`) all appear at GameDataMgr xrefs with the expected accessor mapping.
- **`FUN_71003D4110` is Murmur3-32** (seed 0) over the 81 hardcoded course-name strings. Identified via textbook constant signature (`0xcc9e2d51` / `0x1b873593` / `0xe6546b64` / `0x85ebca6b` / `0xc2b2ae35`).
- **M3.3 + M3.3b are now ONE smoke test away from being wireable.** Flower coins, regular coins, all 6 Royal Seeds, COMPLETE_GAME, INTRO are all grantable via `FUN_710049F648` if the Royal Seed theory holds (same writer truncates u32 → u8 internally for typed slots).
- ★ Three corrections to prior assumptions: `FUN_71003D3FB0` is NOT a writer (it's a stage-info → course-index translator); `FUN_71003838AC` is the bool READER, not setter; the previous CLAUDE.md comment "FUN_71003D3FB0 = write field by hash" was a wrong guess.

**Status snapshot (2026-05-24 end)**:

| Surface | Coverage | Status |
|---|---|---|
| M1 — Wonder Seed nerve + Course Clear nerve | ✅ 330 AP checks | shipped |
| M2.4 — PlayReport capture | ✅ done | shipped |
| M2.5 — Exit-type discrimination | ✅ 199/199 classifiable | shipped |
| M2.6 — bridge skeleton + course correlation | ✅ 106 tests | shipped |
| Save-diff sprint — badge mapping + per-course array offsets | ✅ done | shipped (4/24 badges; MemetendoYT W1 offsets validated) |
| **Static-analysis sprint 2 — GameDataMgr API** | ✅ **decompiled** | **ready to wire** |
| **M3.3 — container-A counter grants in subsdk** | ✅ **shipped** | `grantContainerACounter` live (flower_coin 6→99) |
| **M3.3b — container-B bool grants in subsdk** | ✅ **shipped** | `grantContainerBBool` live (8 hashes; 6 byte-flip diffs confirmed) |
| M4 — LAN socket | ✅ shipped | bridge ↔ Switch end-to-end |
| **M3.8 — DeathLink (both halves)** | ✅ **shipped** | nerve discriminator + PlayerTickLatch + synthKill live |
| **M4.5 — save-survival replay-on-HelloMsg** | ✅ **shipped** | Royal Seeds replayed on every Switch HelloMsg; badges already covered by M4 follow-up #2 |

**Next session priorities** (revised 2026-05-25 after M3.3 + M3.3b + M4.5 shipped):

### Just landed — M4.5 + M3.3b boot-smoke cleanup

Bridge-side `SMBWContext._collect_royal_seed_grants` →
`LanServer._push_royal_seeds_now` re-emits one `GrantHashKeyedMsg` per
received Royal Seed on every Switch `HelloMsg`, mirroring the M4
follow-up #2 badge replay.  Together with the absolute-overwrite badge
sync (HelloMsg + 2 s tick), all currently-AP-exposed deferred-write
grants survive Switch reboots, Ryujinx restarts, and save/reload
cycles.  Container-A counters (flower_coin, regular_coin) and the two
container-B completion bools (COMPLETE_GAME, INTRO) are NOT replayed
because they're not AP items today; the same provider/replay shape
can be extended generically if they ever are.

The M3.3b boot smoke in `NerveActivateOnce::Callback` was already
removed when M3.3b was merged.  The M3.8 `kEnableSynthKillSmokeTest`
block (Validation harness that auto-killed Mario on first level entry)
landed with the original M3.8 commit as constexpr-false dead code; a
follow-up PR removes it entirely.

### Priority 1 — Deferred items pending future sessions

- **M2.2 10-coin** (306 checks) — ✅ **bridge implementation shipped 2026-05-25**; see [docs/m2.2-runbook.md](m2.2-runbook.md).  No Ghidra needed — `course_result` PlayReport's `big_flower_coin_course_{in,out}` bool[3] gives both detection AND per-instance identity for free.  ~30 LoC across [bridge/{protocol,state,processor,location_table}.py](../bridge/processor.py) + 21 new tests (224 total pass).  Two open items: (a) the diff-semantics interpretation is unproven by existing fixtures, **needs one empirical capture** where a 10-coin is collected within a single run; (b) `_TEN_COIN_TABLE` has 2 of 102 non-palace courses mapped, the rest fill in incrementally per playthrough.
- **M3.2 badge follow-ups** — UI-slot mask hash `0x6d1b5c25` write may be
  needed for newly-granted badges to appear in the equip UI.
- ~~**M3.1 power-up grant**~~ — **deferred to M7** (2026-05-25).  AP
  server now precollects all 4 Power-Ups via `starting_items` in
  [apworld/smbw_archipelago/data/game.json](../apworld/smbw_archipelago/data/game.json);
  player begins every seed with Elephant / Fire / Bubble / Drill in
  hand and no AP grant ever fires for them.  RE notes preserved in
  milestones.md "M3.1 — power-up grant" for when the work resumes.
- ~~**M3.4 character roster unlock**~~ — **deferred to M7**
  (2026-05-25).  AP server precollects all 12 Character items via the
  same `starting_items` block; full roster unlocked from the start.
  Save-data bitfield discovery (memory-diff on a character unlock) is
  the entry point when revisited.
- **Per-course flag writers** — for course-clear precise classification.
- **M5 — in-game grant suppression** (shop / badge house must defer to AP).
- **M6 — real Switch + Atmosphere CFW deployment**.
- **M7 — production polish**.

## Project layout

```
C:\Users\maxwe\Documents\smwonder_archipelago\
├── docs\
│   ├── handoff.md          ← you are here
│   └── milestones.md       ← read this next for the M2+ plan
├── manual_smbwonder_zim\   ← the existing Archipelago Manual world (Python apworld)
│                             we'll convert this to an integrated apworld
│                             once the Switch mod talks to a host service
└── switch-mod\             ← the Switch subsdk (forked from fruityloops1/wondar)
    ├── CMakeLists.txt        — modified: -fpermissive, symlink shim for include/
    ├── cmake\toolchain.cmake — devkitA64 cross-compile
    ├── include\, lib\        — wondar's vendored headers + sead/imgui/NN SDK submodules
    └── src\
        ├── program\
        │   ├── main.cpp          ← all our hook installs and callbacks live here
        │   ├── util\Log.hpp      ← ported from smo_archipelago — svcOutputDebugString
        │   ├── util\Log.cpp        sink, level prefixes, no thread_local
        │   ├── util\TargetActorProbe.{hpp,cpp}  ← legacy probe, currently a stub
        │   └── pe\               — wondar's debug UI (mostly disabled in our build)
        └── lib\                  — wondar's inlined exlaunch source
```

Original wondar (upstream): https://github.com/fruityloops1/wondar
Our fork: https://github.com/mdietz94/wondar (uncommitted local changes on top)
The plan document the project started from: `C:\Users\maxwe\.claude\plans\rustling-strolling-marble.md`

## Build + deploy (the daily dev loop)

```pwsh
$env:DEVKITPRO = "C:\devkitPro"
$env:PATH = "C:\devkitPro\msys2\usr\bin;" + $env:PATH

# build
& "C:\Program Files\CMake\bin\cmake.exe" --build `
    "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod\build"

# deploy
$dst = "$env:APPDATA\Ryujinx\mods\contents\010015100b514000\smbwap\exefs"
Copy-Item "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod\build\subsdk9" `
          -Destination $dst -Force
Copy-Item "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod\build\subsdk9.npdm" `
          -Destination "$dst\main.npdm" -Force
```

**First-time configure** (only needed once, or after blowing away `build/`):

```pwsh
& "C:\Program Files\CMake\bin\cmake.exe" `
    -S "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod" `
    -B "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod\build" `
    -G Ninja `
    -DCMAKE_TOOLCHAIN_FILE="C:/Users/maxwe/Documents/smwonder_archipelago/switch-mod/cmake/toolchain.cmake"
```

If the build fails with a *symlink* error (`sead/container/seadPtrArray.h: No such file or directory`), the CMake-side shim that materializes the broken POSIX symlinks didn't run. Re-run cmake configure; the shim copies `lib/sead/include`, `lib/NintendoSDK/include/{nn,nvn,vapours}` into `build/symlink-shims/` and adds it to the include path. See `CMakeLists.txt` around the `_SYMLINK_SHIM_DIR` block.

## Tailing the in-game log

Ryujinx writes `svcOutputDebugString` to its file logs at:

```
C:\Users\maxwe\Desktop\Switch\ryujinx-1.3.3\Logs\Ryujinx_1.3.3_<timestamp>.log
```

Our log lines are prefixed `[smbwap inf]` / `[smbwap dbg]` etc. and tagged `KernelSvc OutputDebugString` by Ryujinx. To filter live during play:

```pwsh
$latest = Get-ChildItem "C:\Users\maxwe\Desktop\Switch\ryujinx-1.3.3\Logs\Ryujinx_*.log" |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content -Wait $latest.FullName | Select-String '\[smbwap'
```

Logs are written with embedded NUL bytes (Ryujinx quirk) so for offline parsing, strip them first:

```bash
tr -d '\0' < "<log>" > /tmp/log_clean.txt
grep '\[smbwap' /tmp/log_clean.txt
```

## Game version + dump artifacts

- **Target version: SMBW v1.0.0** (BID `CD6E42AEE7934F4D`). Internal codename: `Secred.nss`.
- **NSP** at `C:\Users\maxwe\Desktop\Roms\Switch\Super Mario Bros. Wonder\Super Mario Bros. Wonder [010015100B514000][v0][Base].nsp`.
- **Extracted `main.nso`** at `C:\Users\maxwe\Desktop\Roms\Switch\Super Mario Bros. Wonder\main.nso` (26.7 MB).
- Title key (extracted from the bundled `.tik` at offset 0x180): `56c80b14d4923b7ece12c6c45e25e86b`.
- Tool used: `C:\Users\maxwe\Desktop\Switch\hactool.exe`. Keys at `C:\Users\maxwe\.switch\prod.keys`.
- **Do not apply the v1.0.1 update** in Ryujinx — every offset in our mod is pinned to v1.0.0.

## The two working hooks

Both live in `src\program\main.cpp`. NSO base when loaded is `0x7100000000`.

### 1. `NerveActivateOnce` at NSO `+0x559f7c`

Traps `FUN_7100559f7c` — a shared "one-shot Nerve activate" helper with 19 xrefs. Used by many Nerve classes' execute slots (`if (flag == 0) FUN_7100559f7c(this);`). At callback entry, we read `*nerve` (the vtable pointer) and compute its NSO offset (= `vtable - GetTargetStart()`) for filtering by class identity.

**Currently filtered**:

| `vt_off` | Event | Status |
| --- | --- | --- |
| `0x3345728` | **Wonder Seed pickup** (fires WONDER_SEED_AWARDED) | ✅ |
| `0x33fd690` | RequestEventCourseExitByAreaTag (looked promising; never fires on flag touch) | inactive |
| `0x3345bc0`, `0x3345cf8`, `0x3345e30` | RequestEventGoal{Base, GateFinish, TreasureChest} (passive registrations; never fire on touch) | inactive |

**Other vtables observed firing during gameplay** (kept for reference, not target-filtered):

| `vt_off` | What it fires on | Notes |
| --- | --- | --- |
| `0x33fd9a8` | Mario death; ~50ms after Wonder Seed grab; world map travel | "scene transition" |
| `0x33fd870` | Damage; power-up pickup (sometimes) | "player state animation" |
| `0x3346330` | Wonder Flower touched (Wonder phase start) | useful for `wonder_flower_rando` AP opt-in |
| `0x33fd4c8` | Menu exit (quit-to-map) | distinct from level-clear path |
| `0x33fd738` | World map travel (W1→W2 transition) | overworld nav event |

### 2. `SetCourseClearFlagExecute` at NSO `+0x1bf28cc`

Direct trampoline on `FUN_7101bf28cc`, which is slot 8 (execute) of the **`SetCourseClearFlagToGameData` Nerve** (vtable at NSO `+0x34b14e8`).

The function name says it all: it writes the "course cleared" flag to GameData (save data). Body shape:

```c
bl FUN_710059f894          ; open GameData accessor
mov w0, #0xdf82e9ab          ; hash of save-data field name (probably "is_clear" or similar)
bl FUN_71003d3fb0            ; write field by hash
bl FUN_71005e93fc            ; check success
tbz w0, #0x0, fail_path
```

Validated negative:
- Menu quit: silent (different nerve, `vt_off=0x33fd4c8`).
- Death + game over: silent.
- World map travel: silent.

Validated positive: fires exactly once on flag-touch level clear.

**Important nuance**: this covers all *valid* clear types (Normal Exit, Secret Exit, Fake Exit, Top-of-Flag, and likely palace boss clears too). It does NOT, by itself, *distinguish* between them. The 199 Goal-family AP checks split into 4 sub-types in the manual:

| Sub-type | AP checks | Distinguisher TBD |
| --- | --: | --- |
| Normal Exit | 96 | (default, no special property) |
| Top of Flag | 89 | read Mario's Y at clear time |
| Secret Exit | 9 | read the goal pole actor's exit-id field |
| Fake Exit | 5 | same — fake-flag flag |

For M1's feasibility check we just needed "any valid clear" to fire reliably. For M2 splitting comes next — we read additional state at the hook callsite.

## M2.4 working pattern: SetEventId + IPC SaveReport

Real-hardware compatible payload extraction. Two hooks total, both `InstallAtSymbol`.

### Hook A — `nn::prepo::PlayReport::SetEventId`

Symbol: `_ZN2nn5prepo10PlayReport10SetEventIdEPKc` (sdk +0x3a81a0).
Signature: `Result SetEventId(this, const char* event_id)`.

Logs: `prepo.set_event this=<ptr> event=<room_name>`.

Fires whenever the game sets the room name on a PlayReport (with-event-id ctor calls this internally; the no-arg ctor leaves it for the game to call after). The hook on the with-event-id ctor (`_ZN2nn5prepo10PlayReportC2EPKc`) *also* installed; harmless duplication — in practice the game uses the no-arg ctor + SetEventId path, so the ctor hook rarely fires.

### Hook B — `CmifProxyImpl<IPrepoService>::_nn_sf_sync_SaveReport{,WithUser}`

Symbols (long; verbatim from sdk.sym):
```
_ZN2nn2sf4cmif6client6detail13CmifProxyImpl...E22_nn_sf_sync_SaveReportERKNS0_7InArrayIcEERKNS0_8InBufferEm
                                                  (sdk +0x3a9f8c)
_ZN2nn2sf4cmif6client6detail13CmifProxyImpl...E30_nn_sf_sync_SaveReportWithUserERKNS_7account3UidERKNS0_7InArrayIcEERKNS0_8InBufferEm
                                                  (sdk +0x3a9fac)
```

Signatures (after `this`):
- `SaveReport(InArray<char>& room, InBuffer& payload, ulong flags)`
- `SaveReportWithUser(Uid& uid, InArray<char>& room, InBuffer& payload, ulong flags)`

`InArray<char>` and `InBuffer` are 16-byte `{ptr, size}` structs passed by const reference. `Uid` is a 16-byte `{u64[2]}` also by const reference. The Add-overload Old/Old2 variants exist (`SaveReportOld`, `SaveReportOld2`, `SaveReportWithUserOld`, `SaveReportWithUserOld2`) but aren't used by the current build — observed `prepo.set_event` always pairs with one of the two non-Old methods. If a session shows `set_event` without a paired IPC line, add the Old variants.

Logs (per call):
```
prepo.ipc.save     this=0x.. room=<name> pay=0x.. size=<n> flags=0x..
prepo.ipc.save_uid this=0x.. uid=0x.. room=<name> pay=0x.. size=<n> flags=0x..
prepo.ipc.bytes(64/<n>): <hex>...
```

The hex line is the first 64 bytes of the serialized payload. Decoded shape is the PlayReport CBOR-ish format documented below.

### What we did NOT hook on PlayReport (and why)

Hooking *any* of the PlayReport class member functions besides ctor and SetEventId triggered a delayed guest abort:

| Hooks installed | Abort thread | Abort site | Time after last save |
|---|---|---|---|
| All 10 (ctor, Save × 2, Add × 5, Struct::Add × 2) | `ModuleSystemWorker1` | `nn::account::ShowUserCreator` (sdk +0x198284) | ~6 s |
| ctor + SetEventId + Save + Save(Uid&) | `gmd::SaveDataMgr` | nnSdk +0xa4264 | ~5 s |
| ctor + SetEventId only | (none — clean) | — | — |
| ctor + SetEventId + IPC SaveReport{,WithUser} | (none — clean) | — | — |

The crash relocates to whichever validator subsystem notices the inconsistency first. The IPC client layer below the PlayReport class is below those validators — that's why hooking *there* is safe even though hooking the public API isn't.

### PlayReport payload format

Reverse-engineered 2026-05-20 from three full payloads (world_activity / world_result / course_result, all from W1-1). Decoder is in [bridge/play_report.py](bridge/play_report.py); 44 tests pass including end-to-end decoding of all three live captures.

```
Header (3 bytes):
    0xDE                      magic
    u16 BE                    entry count

Body: `entry_count` flat (key, value) pairs.

Opcodes (LIVE = observed in captured bytes; otherwise GUESSED):
    0x00..0x7F                inline uint 0..127             [LIVE]
                              (Nintendo extends CBOR's 0..23 range —
                              0x18..0x1B are NOT reserved here)
    0x80..0x8F                open struct: N=op&0xF entries
                              follow as (key, value) pairs   [LIVE]
    0x90..0x9F                open array: N=op&0xF values    [LIVE]
    0xA0..0xBF                short text string, len = op & 0x1F  [LIVE]
    0xC2                      false                          [LIVE]
    0xC3                      true                           [LIVE]
    0xCC + u8                 uint 128..255                  [LIVE]
    0xCD + u16 BE             uint 256..65535                [GUESSED]
    0xCE + u32 BE             uint                           [LIVE]
    0xCF + u64 BE             uint                           [GUESSED]
    0xD0 + s8                 signed int -128..127           [GUESSED]
    0xD1 + s16 BE             signed int -32768..32767       [GUESSED]
    0xD2 + s32 BE             signed int                     [LIVE — W1-2
                              stage_key = 232160011]
    0xD3 + s64 BE             signed int                     [LIVE — W1-1
                              stage_key = 2937190396 = 0xAF11F7FC, doesn't
                              fit positive s32 so encoder picks s64]
                              Used by Struct::Add(long) — encoder picks
                              smallest signed width that fits.
                              (Top-level PlayReport::Add uses the unsigned
                              0xCC..0xCF path instead.)
    0xD7 + u8 + u64 BE        Any64BitId: 1-byte TypeCode + 8-byte u64 Value;
                              decoded as
                              {"TypeCode": int, "Value": int} [LIVE]
    0xD9 + u8 + N chars       medium text string, 0..255 ch  [LIVE]
    0xFF                      literal -1                     [LIVE]

Unmapped (no live capture — decoder raises DecodeError):
    - floats (single / double / half)
    - negatives other than -1 (maybe inline range in 0xE0..0xFE?)
    - structs / arrays with >15 entries (extension opcode TBD)
    - strings >255 chars (likely 0xD8 + u16 + chars)
```

Important nuances:
- 0x80-0x8F (structs) vs 0x90-0x9F (arrays) are explicitly different opener ranges — the decoder picks dict vs list by the opener nibble, not by peeking at children.
- The encoder uses different opcodes for the same magnitude depending on which `Add` overload was called: top-level `PlayReport::Add(long)` minimizes *unsigned* width (`cc`/`ce`); `Struct::Add(long)` minimizes *signed* width (`d0`/`d1`/`d2`/`d3`). Compare W1-1 stage_key=2937190396 (high bit set as s32 → bumps to s64 → `d3`) vs W1-2 stage_key=232160011 (fits positive s32 → `d2`). Both decode to plain Python ints.
- `arena_score_enter = 4294967295` and `last_put_panel_id = -1` are both "all-ones" semantically but encode differently — the former is `ce ff ff ff ff` (genuine u32 max), the latter is `ff` (the -1 short form). The C++ caller's signedness flows through.

Test fixtures and assertion sets live in [bridge/test_play_report.py](bridge/test_play_report.py). Iterate by playing through new scenarios (secret exit, palace clear, item pickup), pasting the new `prepo.ipc.bytes(...)` lines into a fixture, and adding assertions.

### Known room names so far (corpus grows organically)

| Room name | When it fires | Notable fields |
|---|---|---|
| `bootup_time` | application launch | `BootupTimeUs` |
| `erepo_region` | boot | `Region`, `Language`, `StandardTimeName`, `UtcOffsetSeconds` |
| `erepo_time`, `erepo_playstyle`, `erepo_network_status`, `erepo_active_beacon` | boot | SDK telemetry, skip |
| `game_option` | settings change / boot finalize | `savedata_id`, `play_mode`, `scene_type`, control-type arrays |
| `world_activity` | world-map activity update | `stage_info.{stage_key, world_no}`, `wonder_seed`, `wonder_coin` |
| `world_result` | world-map → course transition | `stage_info` (source), **`next_stage_info.{stage_key, course_id, stage_type, world_no, world_kind}`** (destination) |
| `course_in` | course actually loading | **`stage_info.{stage_key, world_no, course_no}`**, `local_player_rest`, `lucky_coin`, `world_wonder_flower`, `equip_badge_id[]` |
| **`course_result`** | **course CLEARED — fires ~8 ms after M1 `COURSE_CLEARED` nerve** | **`stage_info.{stage_key, world_no, course_no}` identifies the cleared course; `goal_id` (0=normal pole, 1=secret exit, 2=fake exit guessed); `touch_goal_top_{enter,result}` (bool, distinguishes Top of Flag from Normal Exit when `goal_id=0`); `course_result` (1=clear); `badge_id_array`; `total_get_finish_seed_count`; all flower-coin / yellow-coin counts** |
| `koopajr_result` | palace boss fight result (win OR loss) | `stage_info` (identifies the palace), **`battle_result`** (True=won/Royal Seed earned, False=died), `koopajr_final_stage`, **`koopajr_step_info`** (array of per-phase structs with damage count + time), `koopajr_total_time`, `koopajr_challenge_count`, `badge_id_array`. **An AP Royal Seed check fires only when `battle_result == True`.** ✅ Both loss AND win captured (Pipe-Rock Plateau Palace, stage_key=2308078743). ⚠ A palace WIN *also* emits a concurrent `course_result` ~1 ms before — the bridge must prefer this `koopajr_result` over that companion. |

The `course_result` discovery (2026-05-20) closes the M2.5 distinguisher question: every clear-state field we need (Top of Flag, goal identity, coin counts, badges held) is in the payload. Three live fixtures now lock in the exit-type mapping table — see [bridge/test_play_report.py](bridge/test_play_report.py) `COURSE_RESULT` (W1-1 Top of Flag, `goal_id=0`), `W1_2_COURSE_RESULT_SECRET` (W1-2 Secret Exit, `goal_id=1`), and `KOOPAJR_RESULT_LOSS` (Pipe-Rock Plateau Palace loss, room `koopajr_result` + `battle_result=False`), plus `TestM25ExitTypeMapping` for the discrimination logic. 199 of 199 goal-clear *and* palace AP checks are now classifiable structurally; only the Fake Exit `goal_id` value (guessed `2`) and a palace WIN capture (to confirm `battle_result=True` in the same shape) remain as nice-to-have empirical confirmations.

## What didn't work (don't repeat these)

Saving these so the next attempt doesn't burn time re-deriving them.

1. **Hooking ActorBase::ctor at NSO `+0x231204`** (wondar's existing offset). Fires only 3-5 times early in boot for *system* actors. Game-level actors (Mario, enemies, items, goal poles) construct through a different path we never identified. The `s_ActorList` in wondar's ActorBrowser is *not* populated by Goal/WonderSeed instances.
2. **Hash-based actor-class registry path through `FUN_7100362920`**. Too many indirection layers (class registry → per-class instance pool → instance descriptor with `+0x178` index field → ??? → actor). After 30 min of decompilation, never reached a vtable. Abandoned.
3. **Hooking `FUN_7101562fb4`** (Wonder Seed Nerve's slot-8 execute) directly. Crashed level loads ~2.4s after first fire. Likely a trampoline relocation issue (some instruction pattern in its first 16 bytes that exlaunch's And64InlineHook doesn't handle). **Avoid hooking Wonder Seed slot 8 directly.** Use the shared helper at `FUN_7100559f7c` and filter by vtable — that's what works.
4. **Hooking `FUN_7100299488`** (the per-actor level-load dispatcher with the hardcoded `"ObjectGoalPole"` check). Fires at level *load*, not on touch — it's a level-init registration handler, not an event dispatcher. The post-match callees `FUN_7100299738` (sead::ActorReference ctor) and `FUN_7100299800` (ActorReference swap) just stash a weak-ref to the goal actor for later use.
5. **String-search → vtable approach for `RequestEventGetFinishWonderSeed`** (NSO offset `0x33457b8`). The vtable exists, the class exists, but the Nerve *never activates during gameplay*. It's a passive registration that the engine has wired but never ticks. The actual Wonder Seed pickup goes through a sibling Nerve at `0x3345728` whose name we never resolved (slot 0 tail-calls into `FUN_71015636cc`; never confirmed what name it returns). Naming was a red herring — go by empirical firing, not by string.
6. **Hooking the 4 `RequestEventGoal*` and `RequestEventCourseExitByAreaTag` Nerves**. All passive registrations. None fire on flag-touch despite their slot-8 execute methods being in `FUN_7100559f7c`'s xref list. The goal-clear path bypasses Nerve tick entirely and writes save data directly through `SetCourseClearFlagToGameData`.
7. **Hooking `nn::prepo::PlayReport::Save()`, `Save(Uid&)`, or any `Add(...)` overload**. Installs cleanly, fires correctly, but triggers a delayed guest abort 5-6 seconds later on a *different SDK thread* (ModuleSystemWorker1, then gmd::SaveDataMgr depending on which validator notices). The SDK has cross-subsystem audits that detect PlayReport-state inconsistency introduced by the trampoline; the abort surfaces wherever the next prepo-touching subsystem runs. **Workaround**: drop below the PlayReport class to the IPC client (`CmifProxyImpl<IPrepoService>::_nn_sf_sync_SaveReport{,WithUser}`) which sees the already-serialized payload. See "M2.4 working pattern" above.

## Critical engine knowledge

Nintendo's Nerve system in SMBW has **two distinct flavors** that need different hook strategies:

- **Active Nerves**: tick every frame while live. Execute method has shape `if (flag == 0) FUN_7100559f7c(this); FUN_7100005390(this+0x68);`. The first call does the one-shot work, the second advances state. Examples: Wonder Seed pickup, Wonder phase start, scene transitions, damage/power-up animations. **Hook strategy**: trampoline on `FUN_7100559f7c`, filter by vtable.
- **One-shot dispatch Nerves**: their class exists but `execute` is called explicitly by other code at a specific moment (not per-frame). Execute method does its work inline. Examples: `SetCourseClearFlagToGameData`. **Hook strategy**: trampoline directly on the execute function. Validate by checking the prologue is clean (no `adrp`/`ldr literal` in first 5 instructions, since `And64InlineHook` has a fixed-size relocation buffer).

Recipe to find any new Nerve hook target:

1. Search Ghidra strings for the event name (e.g., `RequestEventXxx`, `SetXxx`, `GetXxx`).
2. Find the getter that returns the string. The getter's address appears as **slot 0** of a vtable somewhere in `0x710334XXXX` or `0x71034BXXXX` (the Nerve vtable region) or `0x71033fXXXX` (a different family).
3. Read the vtable layout. Shared base method slots are `LAB_71014498**` and `LAB_7101e078e4`. Event-specific overrides are slots 7, 8, and a few high ones.
4. **Slot 8 is execute.** Check whether slot 8's address is in `FUN_7100559f7c`'s 19-entry xref list:
   - **Yes** → it's an active Nerve. Use `NerveActivateOnce` and add the vtable offset to the filter.
   - **No** → it's a one-shot. Peek the execute function's prologue. If clean, hook directly.

Address space layout in the loaded NSO:

- Base: `0x7100000000` (loaded by Ryujinx; `exl::util::modules::GetTargetStart()` returns this).
- `.text` code: roughly `0x7100000000`–`0x7102800000`.
- `.rodata` strings: `0x71028XXXXX`–`0x71029XXXXX` typically.
- Vtables: `0x710334XXXX`, `0x71033fXXXX`, `0x71034BXXXX` (Nerve regions we've seen).
- Itanium-style typeinfo: `0x71000ac930` is a function that appears in every Nerve vtable's `-8` slot (probably a generic destructor or dispatch helper, not std::type_info).

## Tools used

- **Ghidra 11.3 or 11.4** + **Adubbz Switch Loader 1.7.0** (`File → Install Extensions`).
- **JDK 21** required by Ghidra 11.x.
- `wondar\syms\100\sdk.sym` is the NN SDK symbol map. Apply it as Ghidra labels via a Jython script (~20 lines: parse each `name = __main_start + 0xOFFSET;` line and apply to base + offset). Hugely speeds up navigation once `nn::`, `sead::`, etc. names appear in the listing.

## Things to test next session (priority order)

1. ✅ ~~Boss clear test~~ — done post-M1. `COURSE_CLEARED` fires on Royal Seed palace clears. +7 AP checks for free.
2. ✅ ~~Wire PlayReport hooks~~ — done. Initial naive approach (all 10 class-member hooks) crashed; final pattern is ctor + SetEventId + IPC SaveReport{,WithUser}. See "M2.4 working pattern".
3. **Expand the room-name corpus**: play through a normal level clear, a secret exit (W1-2 Piranha Plants on Parade), and a palace clear (Pipe-Rock Plateau Palace). Each adds one room name + field map to the M2.4 spec.
4. **Build the Python decoder** (~50 LoC, CBOR-ish format documented above). Test against the captured `course_in` 355-byte payload — we have it in the 2026-05-20 20:52 log if needed.
5. **10-coin Nerve hunt**: search Ghidra strings for `RequestEventGetBigTenLuckyCoin`, `BigTenLuckyCoin`, `TenCoin`. Find vtable, identify hook approach (shared helper vs direct execute). 305 AP checks, biggest remaining bucket.
6. **Goal exit-type distinguisher**: confirm whether `course_in` or the (yet-unobserved) course-clear report carries `exit_type` / `goal_kind`. If so, M2.5 is solved for free. If not, fall back to dumping `nerve+0x40/+0x68/+0x90` at `SetCourseClearFlagExecute` callback entry.
7. **LAN socket**: re-enable the commented `nn::socket::Initialize` block in `main.cpp` and add a simple TCP outbound to your PC. SMO's pattern is in `smo_archipelago\switch-mod\src\ap\ApClient.cpp`. This is the moment the captured IPC bytes ship over the wire to the Python bridge.

See `milestones.md` for the full M2+ plan.

## State of the codebase

`switch-mod/` is a fork of `mdietz94/wondar` (its own git repo). Local diffs from upstream now living on top:

- `CMakeLists.txt`: `-fpermissive` (libstdc++15 `std::construct_at` const fix from smo_archipelago); symlink-shim block to materialize broken POSIX symlinks on Windows checkouts.
- `src/program/main.cpp`: hooks added — `NerveActivateOnce`, `SetCourseClearFlagExecute` (M1); `PlayReportCtor`, `PlayReportSetEventId`, `PrepoIpcSaveReport`, `PrepoIpcSaveReportWithUser` (M2.4). Crashy hooks left as definitions only, install lines commented out with explanation (PlayReport::Save{,Uid&}, all PlayReport::Add overloads, Struct::Add overloads). `nvnImGui` install disabled; wondar's hardcoded `RwPages` SDK patch at `+0x399790` disabled.
- `src/program/util/Log.hpp`/`Log.cpp` (new): smbwap kernel-debug logger, ported from smo_archipelago.
- `src/program/util/TargetActorProbe.hpp`/`TargetActorProbe.cpp` (new): legacy actor-vtable runtime-discovery probe, currently a stub.
- `src/program/pe/DbgGui/Windows/ActorBrowser.cpp`: lightly modified to call into the probe stub.

The outer repo (`smwonder_archipelago/`) is a separate git repo holding `docs/`, `manual_smbwonder_zim/`, and `CLAUDE.md`. `switch-mod/` is excluded via outer `.gitignore` for now — it's tracked by its own repo and may be promoted to a submodule once pushed.

Decide before upstreaming: the symlink-shim and `-fpermissive` fixes are general Windows-build-fixes worth a PR to wondar; the prepo hooks are SMBW-specific and stay private.
