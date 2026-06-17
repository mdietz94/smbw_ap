# M4 — LAN bridge + AP client runbook

> **2026-05-25 layout change** — `bridge/` and `manual_smbwonder_zim/`
> were deleted.  Replace any `python -m bridge --slot=...` here with
> `python -m apworld.smbw_archipelago.client.main --connect=... --name=...`
> (or click "SMBW Client" in Archipelago Launcher for the Kivy UI).
> Test seeds now need to target game `SMBWonder` instead of
> `Manual_SMBWonder_Zim`.

End-to-end smoke test for the M4 bidirectional MVP.  Validates:

- **Outbound**: Wonder Seed + Course Clear on Switch → client → AP `LocationChecks`.
- **Inbound**: AP server `/send <slot> Spring Feet Badge` → client → Switch → badge appears live in equip menu.

## Prereqs

- **Switch (Ryujinx)**: SMBW v1.0.0 mounted, modded with this subsdk.  Do NOT
  apply the v1.0.1 update — hook offsets are pinned to 1.0.0.
- **Archipelago**: the vendored checkout at `vendor/Archipelago/` (git submodule).
  Expose this repo's apworld via a junction at
  `vendor/Archipelago/custom_worlds/smbw_archipelago` →
  `apworld/smbw_archipelago` (run `python scripts/install_smbw_apworld.py`,
  or use the `/setup` wizard's Junction phase).
- **A generated test seed**: one slot named e.g. `MarioTest` for game `SMBWonder`,
  with **1 Spring Feet Badge in starting inventory** so the AP server has one
  to send immediately.
- **Same LAN**: bridge PC + Switch on the same subnet, OR Ryujinx on the bridge
  PC (loopback works fine via the discovery loopback probe).

## Build + deploy the Switch mod

```pwsh
# Reconfigure ONCE after adding new files under switch-mod/src/.
& "C:\Program Files\CMake\bin\cmake.exe" `
    -S "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod" `
    -B "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod\build" `
    -G Ninja

# Then build incrementally:
& "C:\Program Files\CMake\bin\cmake.exe" --build `
    "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod\build"

# Deploy to Ryujinx mods dir:
$dst = "$env:APPDATA\Ryujinx\mods\contents\010015100b514000\smbwap\exefs"
Copy-Item "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod\build\subsdk9" `
          -Destination $dst -Force
Copy-Item "C:\Users\maxwe\Documents\smwonder_archipelago\switch-mod\build\subsdk9.npdm" `
          -Destination "$dst\main.npdm" -Force
```

If discovery doesn't find the bridge automatically (Switch on a different
subnet than `192.168.1.0/24`), rebuild with an explicit seed. The seed only
needs to be *some* address on the right /24. Easiest paths:

- **In SMBW Client:** `/setup_ip 192.168.7.42` — opens the setup wizard with
  the "Bridge IP" field prefilled; click Run. (Plain `/setup` exposes the same
  field, blank by default.)
- **Headless:** `python -m apworld.smbw_archipelago._setup.wizard_cli \
  --phases build,deploy --deploy-target ryujinx --bridge-host 192.168.7.42`

Both forward `-DBRIDGE_HOST_STRING` to cmake and force a reconfigure. The raw
cmake form still works too:

```pwsh
& "C:\Program Files\CMake\bin\cmake.exe" `
    -S ... -B ... -G Ninja `
    -DBRIDGE_HOST_STRING="192.168.7.42"
```

## Start the AP server

```pwsh
cd C:\Users\maxwe\Documents\smo_archipelago\vendor\Archipelago
python Launcher.py
# Host -> select the generated .archipelago file.
# Note the port it binds (default 38281).
```

## Start the bridge

```pwsh
cd C:\Users\maxwe\Documents\smwonder_archipelago
python -m bridge --ap-host=localhost --ap-port=38281 --slot=MarioTest --log-level=DEBUG
```

Expected startup lines:

```
bridge   INFO  using Archipelago at C:\...\vendor\Archipelago
bridge.lan   INFO  listening on ('0.0.0.0', 17777)
bridge.disc  INFO  listening on UDP 0.0.0.0:17776 (replies advertise TCP 192.168.1.42:17777)
bridge.ap    INFO  AP connected: slot=MarioTest player=1 seed=<seed>
bridge       INFO  bridge up: AP=localhost:38281 slot=MarioTest LAN=0.0.0.0:17777 discovery=17776
```

`bridge.ap INFO DataPackage cached: NNN locations, NNN items for Manual_SMBWonder_Zim`
should fire shortly after Connected — that loads the name → AP-id map.

## Start the game + tail the Switch log

```pwsh
$latest = Get-ChildItem "C:\Users\maxwe\Desktop\Switch\ryujinx-1.3.3\Logs\Ryujinx_*.log" |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content -Wait $latest.FullName | Select-String '\[smbwap'
```

Within ~10 seconds expect:

```
[smbwap inf] [net] socket init rc=0x0 pool=786432 bytes
[smbwap inf] [net] nn::nifm::Initialize
[smbwap inf] [net] network available: YES
[smbwap inf] [net] worker thread started
[smbwap inf] [worker] entered connect loop
[smbwap inf] [disc] resolved via loopback -> 192.168.1.42:17777
[smbwap inf] [conn] TCP connect OK -> 192.168.1.42:17777
[smbwap inf] [conn] state=hello
[smbwap inf] [conn] HELLO sent (NN bytes)
[smbwap inf] [conn] state=ready
[smbwap inf] [conn] HELLO acked: ok=true bridge_ver=bridge-m4-dev wire_ver=1
```

And on the bridge side:

```
bridge.lan   INFO  switch connected from ('127.0.0.1', 49152)
bridge.lan   INFO  switch ('127.0.0.1', 49152) hello: mod_ver=smbwap-m4 game_ver=smbw-1.0.0 pid=1
```

## Inbound smoke test — AP → Switch → live grant

In the AP server console:

```
/send MarioTest Spring Feet Badge
```

Bridge logs:

```
bridge.ap    INFO  item received: 'Spring Feet Badge' (id=NNN) -> grant_badge internal_id=4
bridge.lan   INFO  -> grant_badge internal_id=4
```

Switch logs:

```
[smbwap inf] [grant] received GrantBadge(id=4), enqueued
[smbwap inf] [grant] drained GrantBadge(id=4) -> grantBadgeBit returned true
[smbwap inf] GrantBadge(4): addr=0x... word=0 mask=00000010 before=00000000 after=00000010
```

In-game: pause Mario → open the badge equip menu → **Spring Feet Badge is
selectable** (was greyed/missing before).  No save+reload required.

## Outbound smoke test — Course Clear

Load any W1 course in-game (W1-1 Welcome to the Flower Kingdom or W1-2 Piranha
Plants on Parade are in the location table by default).  Reach the flagpole.
Touch it.

Switch logs (existing M1 lines + new M4 enqueue lines):

```
[smbwap inf] COURSE_CLEARED: nerve=0x... (fire #1)
[smbwap inf] prepo.ipc.save this=... room=course_in pay=... size=... flags=0x0
[smbwap inf] prepo.ipc.save this=... room=course_result pay=... size=... flags=0x0
```

Bridge logs:

```
bridge.lan   DEBUG play_report: room=course_in payload_bytes=NNN
bridge.proc  INFO  course_in: now in stage_key=2937190396 (world 1, course 1)
bridge.lan   DEBUG play_report: room=course_result payload_bytes=NNN
bridge.proc  INFO  course_result -> normal_exit at stage_key=2937190396
bridge.ap    INFO  -> AP LocationChecks 'W1: Welcome to the Flower Kingdom! - Normal Exit' (id=NNN)
```

AP server console:

```
(MarioTest) sent W1: Welcome to the Flower Kingdom! - Normal Exit
```

## Outbound smoke test — Wonder Seed

Enter W1-1.  Touch the Wonder Flower.  Complete the Wonder Phase.  Grab the
Wonder Seed at the end.

Switch:

```
[smbwap inf] WONDER_SEED_AWARDED: nerve=0x... (fire #N)
```

Bridge:

```
bridge.proc  INFO  course_in: now in stage_key=2937190396 ...   (set earlier)
bridge.lan   DEBUG nerve: kind=wonder_seed_awarded seq=N
bridge.ap    INFO  -> AP LocationChecks 'W1: Welcome to the Flower Kingdom! - Wonder Seed' (id=NNN)
```

## Reconnect resilience

Ctrl-C the bridge.  Switch logs:

```
[smbwap inf] [conn] Recv failed errno=...
[smbwap inf] [conn] peer closed
[smbwap inf] [conn] state=disconnected
[smbwap inf] [conn] peer closed; reconnecting in 1000 ms
[smbwap inf] [conn] state=connecting
[smbwap inf] [disc] no UDP reply (loopback + sweep); caller retries with backoff
```

Restart the bridge.  Switch should re-establish without restart:

```
[smbwap inf] [disc] resolved via loopback -> 192.168.1.42:17777
[smbwap inf] [conn] TCP connect OK
[smbwap inf] [conn] state=ready
```

Re-enter an already-cleared course and touch the flag.  Bridge dedups via
`BridgeState.emit_check`:

```
bridge.proc  ...  (no log -- emit_check returned False)
```

(No `-> AP LocationChecks` line means the dedup worked; nothing was sent.)

## Common failure modes

- **`socket init rc=0x*non-zero*`** in Switch log: nn::socket pool too small
  or unaligned, OR the game already initialized sockets first.  M4 installs
  before Orig in `GameFrameworkInitialize::Callback` so this shouldn't happen;
  if it does, add a no-op trampoline on `nn::socket::Initialize` (smo's
  pattern, deferred from M4).

- **`[disc] no UDP reply`** repeating forever: bridge's UDP responder didn't
  bind (port conflict) OR Switch's `BRIDGE_HOST_STRING` seed is on a different
  subnet than the bridge.  Bridge log shows the bound port; rebuild with
  `-DBRIDGE_HOST_STRING="<your-subnet-anything>"` if needed.

- **`-> AP LocationChecks` never fires after `course_result`**: the AP
  location table doesn't have an entry for that (CheckKind, stage_key).  Bridge
  logs `INFO no AP location name for kind=normal_exit stage_key=...` -- add the
  course to `bridge/location_table.py`.

- **`item received: 'X Badge' -> ... no internal_id`**: badge isn't in
  `bridge/badge_table.py`.  Look up the bit position via M3.2 capture work and
  add it.

- **Switch HELLO acked but no badge appears after `/send`**: drainInbound only
  runs from `NerveActivateOnce::Callback` and `SetCourseClearFlagExecute::Callback`
  -- if the player is on the title screen with no nerves firing, grants queue
  in the inbound ring.  Loading a save fires many nerves immediately and drains
  the backlog.  This is the M4 piggyback-drain trade-off; M4.5 adds a true
  per-frame hook.

## What's deferred (not in M4)

- DeathLink (schema slot reserved via `NerveKind.DEATH_DETECTED` but unwired).
- Snapshot/replay on Switch reconnect (`BridgeState.emit_check` dedup is
  enough for AP; badges persist in the live gmd container across saves).
- Wonder Flower / 10-coin Wonder Seed differentiation.
- Power-up / character grants (M5+).
- ✅ Hash-keyed counter grants (M3.3) — primitive shipped 2026-05-25;
  smoke-test recipe below.  Coin/Wonder-Seed counter denomination
  routing in AP is still future work.
- ❌ Royal Seed bool grants (M3.3b) — container-A writer no-ops on
  bool slots; bridge plumbing wired but Switch-side container-B writer
  pending (see [docs/handoff.md](handoff.md) "M3.3b live-falsified").
- Multi-Switch support (M4 rejects second connection).
- `host.yaml` integration (M4 uses CLI args only).
- Kivy GUI (headless for M4).
- True per-frame drain hook (piggyback on existing nerve hooks).


## M3.3 counter-writer smoke test (2026-05-25, validated)

Reproducing the live validation of `probe::grantContainerACounter` end-
to-end.  Same shape as the badge smoke test above.

### Phase A — temporarily re-add the smoke trigger

In [switch-mod/src/program/main.cpp](../switch-mod/src/program/main.cpp)'s
`NerveActivateOnce::Callback`, right after `drainInbound()`:

```cpp
static std::atomic_flag s_smoke_fired = ATOMIC_FLAG_INIT;
if (!s_smoke_fired.test_and_set()) {
    SMBWAP_LOG_INFO("M3.3 smoke: firing flower_coin=99");
    probe::grantContainerACounter(0xf4ee6827, 99);
}
```

(This block is NOT committed in tree.  It's a regression-test snippet.)

### Phase B — snapshot, build, deploy, play

1. **Snapshot** `%APPDATA%\Ryujinx\bis\user\save\0000000000000002\<user>\game_data.sav`
   to `game_data.sav.pre`.
2. **Build** (`cmake --build ...\switch-mod\build`) and **deploy** subsdk9
   to `$APPDATA\Ryujinx\mods\contents\010015100b514000\smbwap\exefs\`.
3. **Launch** SMBW, enter ANY course (W1-1 works); the smoke trigger fires
   on the first nerve activation.  Expect Switch log:
   ```
   [smbwap inf] M3.3 smoke: firing flower_coin=99
   [smbwap inf] gmd.A_writer hash=0xf4ee6827 value=99 gmd=0x...
   [smbwap inf] GrantHashKeyed: hash=0xf4ee6827 value=99 gmd=0x...
   ```
   (The `gmd.A_writer` line fires because our writer call recurses through
   the existing `GmdContainerAWriter` probe trampoline -- free
   observability.)
4. **Save** the game in-overworld.  The dirty buffer at `gmd->[+0xf8]`
   flushes to the persistent container at this save event.
5. **Quit Ryujinx**.
6. **Diff** with [scripts/savediff.py](../scripts/savediff.py):
   ```pwsh
   python scripts/savediff.py game_data.sav.pre game_data.sav
   ```
   Expect:
   ```
   [pair  269 @ 0x0890]  key=0xf4ee6827           <prior> → 99
   ```

### Phase C — remove the smoke block, redeploy

Once the diff matches, drop the smoke `if`-block from the callback so
fresh sessions don't mutate the save.  Rebuild + redeploy.

### Royal Seed end-to-end is NOT yet functional

The bridge plumbing (`royal_seed_table.py`, `send_grant_hash_keyed`,
`GrantHashKeyedMsg`, `drainInbound` dispatch) is correct and reusable,
but the Switch-side `probe::grantContainerACounter` silently no-ops on
the Royal Seed bool slots (live-falsified 2026-05-25 — the M3.3 smoke
test also called `grantContainerACounter(0x55815859, 1)` and observed
no save-file change at file offset `0x0354`).  Once the M3.3b
container-B writer ships, the bridge will route Royal Seeds end-to-end
with no bridge-side change required.

In the meantime, `/send MarioTest W1 Royal Seed` from AP:

- Bridge logs `WARNING` flagging the in-game no-op.
- Wire message still ships (`grant_hash_keyed hash=0x55815859 value=1`).
- Switch log shows `grantContainerACounter returned true` (the function
  returned cleanly; it just didn't change persistent state).
- AP server records the item as received.
- Save+quit+diff shows NO change at `0x0354`.

### Known caveat — save-survival (container-A grants)

Container-A grants do NOT survive save/reload.  The
`FUN_710049F648` writer queues to `gmd->[+0xf8]`; if the player loads
a fresh save before the next in-game save fires, the value reverts.
Same gap as the M3.2 badge primitive.  Mitigations:

- **For smoke testing**: always save explicitly after each `/send`.
- **For dogfooding**: M4.5 replay-on-`HelloMsg` is the durable fix and
  covers badges, container-A grants, and future container-B grants
  uniformly.  Tracked in [docs/handoff.md](handoff.md) "M4 follow-ups".
