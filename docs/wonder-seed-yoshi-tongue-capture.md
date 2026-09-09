# Wonder Seed lost when Yoshi eats it — capture + validation protocol

**Status**: open spike, code landed on branch
`claude/wonder-seed-yoshi-tongue-check-ch4gvn` (bridge-side only, no
switch-mod change). Needs a live capture on Ryujinx to settle one
unproven field semantic; the agent prompt at the bottom is copy-pasteable
into a session on a machine that can build/deploy/play.

## The bug

A player reports: grabbing a Wonder Seed with **Yoshi's tongue** awards
the seed in-game, but the AP `- Wonder Seed` check never sends.

That is consistent with how detection works today. The mid-course Wonder
Seed check rides one signal only: the `NerveActivateOnce` filter on
vtable `+0x3345728` (`switch-mod/src/main.cpp`, `kVtableOff_WonderSeedAwarded`)
→ `WONDER_SEED_AWARDED` → `processor._handle_nerve_fire`. That Nerve is
the unnamed sibling on the **player pickup** path (its name was never
resolved — `docs/handoff.md`, "String-search → vtable approach"). A
tongue-eat pickup is a different actor path and plausibly never activates
it.

## The fix on this branch

`processor._emit_wonder_seed_fallback` — a redundant end-of-course emit,
the same shape as the existing `_emit_course_clear_badge` robustness
layer. On a `course_result` clear it emits `CheckKind.WONDER_SEED` for
the course when:

- `total_get_finish_seed_count >= 1`, **and**
- `total_wonder_count >= 1`, **and**
- the stage has a `WONDER_SEED` row in `location_table`
  (`has_wonder_seed_location`).

`BridgeState.emit_check` dedups on `(kind, stage_key, sub_key)`, so when
the nerve *did* fire this is a no-op — no double-send, and no need to
detect "the nerve was missing".

It also logs one INFO line per course clear regardless of outcome, which
is the capture instrument:

```
wonder-seed tally at stage_key=0xAF11F7FC: finish_seed=1 wonder=1 flower=2 new_flower=0 (nerve already emitted: True)
```

Toggle: `_WONDER_SEED_FALLBACK_ENABLED` in `processor.py` (set `False` to
keep the telemetry line but suppress the emit).

## Why it needs a live capture

Two things are unproven.

**1. Does the tongue path even bump the tally?** If the game increments
`total_get_finish_seed_count` at the same code point the Nerve rides, the
field will read 0 on a tongue grab and this fallback fixes nothing — then
it's an RE job (plan B below).

**2. Does the field mean "collected this run" or "already owned"?** The
existing corpus can't separate the two: every capture with the field set
also completed a Wonder phase on that run. Field values across the
fixtures in `client/tests/test_play_report.py`:

| fixture | `total_get_finish_seed_count` | `total_wonder_count` | `get_flower_count` | `new_flower_count` |
|---|---|---|---|---|
| W1-1 clear, Wonder phase done | 1 | 1 | 2 | 0 |
| W1-2 secret exit, Wonder phase done | 1 | 1 | 3 | 1 |
| W1 palace clear | 1 | 5 | 1 | 1 |
| pause-quit (`course_result=3`) | 0 | 0 | 1 | 0 |
| Break Time! (no Wonder phase) | 0 | 0 | 1 | 0 |

If it means "owned", a re-clear of an already-seeded course would send a
Wonder Seed check the player never earned this session. The
`total_wonder_count >= 1` conjunct is the cheap guard against that
reading; run C below is what actually settles it.

## Capture matrix

Four course runs, one client log. Read the `wonder-seed tally` line for
each, plus whether a `wonder_seed_awarded` nerve line appears.

| # | Run | Expect if the fallback works |
|---|---|---|
| **A** control | Mario grabs the Wonder Seed normally, clear the course | nerve fires; `finish_seed=1 wonder>=1`; `nerve already emitted: True` |
| **B** the bug | Yoshi **tongue-grabs** the Wonder Seed, clear the course | nerve line ABSENT; `finish_seed=1 wonder>=1`; `course_result → wonder_seed ... fallback rescued the check` |
| **C** false-positive control | Re-enter a course whose Wonder Seed you already own, do **not** touch the Wonder Flower, clear it | `finish_seed=0` (proves per-run semantics). A `1` here means "already owned" and the fallback must be re-gated |
| **D** partial | Touch the Wonder Flower, then die / skip the seed, then clear | `finish_seed=0 wonder>=1` |

Use a scratch AP room, and pick a course whose Wonder Seed location is
still unchecked so run B's check is visible as a new send.

## Capture status

**Not yet captured.** The rig below was stood up and verified working
end-to-end on 2026-09-09 (Ryujinx booted, mod loaded, bridge connected,
grants flowing both directions), but no course was ever played in that
session -- the emulator sat at boot for ~90 minutes and was then closed.
The client log recorded only the ~2 s inbound tick traffic
(`itemgate` / `charagate` / `badgeshop`), zero `course_result`, zero
`wonder-seed tally`, zero nerve fires. The matrix below therefore still
reads "Expect", not observed values.

Anyone resuming: the rig section is the time-saver -- it took most of a
session to get a working Archipelago environment on this machine, and
none of that work is in the capture matrix.

## Capture rig -- verified working

Standing this up hit three environment blockers that are not obvious and
cost real time. All three are environment-level, not branch-level.

1. **`scripts/run_client.py` is stale and cannot launch the client.** It
   imports `worlds.smbw_archipelago`, but since the apworld rename
   (`339e6c7` / `2b88e9f`) the installed package is `worlds.smbwonder`.
   The dev shim is broken on master, independent of this branch. Until it
   is fixed, launch through the Archipelago Launcher, or use a shim that
   imports `worlds.smbwonder.client.main`. Note `launch(*launch_args)`
   parses **only** its explicit arguments -- it ignores `sys.argv`, so a
   shim must forward `sys.argv[1:]` or the client silently starts
   unconnected (`AP=None name=None`).

2. **Python dependency pins.** Archipelago 0.6.7 needs
   `websockets==13.1`; with `websockets>=14` the AP handshake dies in
   `MultiServer.send_msgs` with
   `AttributeError: 'ServerConnection' object has no attribute 'open'`.
   It also needs `pkg_resources`, i.e. `setuptools<81` (setuptools 84
   removed it). Plus AP's base requirements minus Kivy
   (`platformdirs` in particular, or the AP handshake fails in
   `Utils.get_unique_identifier`).

3. **`Generate.py` blocks invisibly on missing requirements.** It calls
   `input()` to confirm installing them, so with stdin closed it either
   hangs at ~0% CPU or dies with `EOFError`. Set
   `SKIP_REQUIREMENTS_UPDATE=1` for every AP entry point. Also: the game
   name in the player YAML is `Super Mario Bros Wonder` -- no period.

Working invocations (Windows, from the repo root):

```bash
# apworld -> vendor/Archipelago/custom_worlds/smbwonder.apworld
python scripts/install_apworld.py

# generate a scratch seed (Players/*.yaml, game: Super Mario Bros Wonder)
cd vendor/Archipelago && SKIP_REQUIREMENTS_UPDATE=1     python Generate.py --player_files_path Players --outputpath output

# server, then client (client needs vendor/Archipelago on sys.path first)
SKIP_REQUIREMENTS_UPDATE=1 python MultiServer.py --port 38281 output/AP_<seed>.zip
SKIP_REQUIREMENTS_UPDATE=1 python <shim>.py --nogui --connect=localhost:38281 --name=Mario
```

The client is fine headless (`--nogui`); the capture instrument is the
log file, not the GUI. Kivy is only needed for the window.

**Character gating blocks run B.** A fresh AP room starts with
`unlocked_charas mask=0x1` (Mario only), so Yoshi is not selectable and
the tongue-grab cannot be performed. Send a Yoshi first
(`/send <slot> Green Yoshi`); confirm `[charagate] unlocked mask 0x000 ->
0x101` in the Ryujinx log before starting run B.

**Run C may not be constructible as written.** The client pushes
`set_wonder_seeds_absolute` as AP-authoritative state every ~2 s, derived
from Wonder Seed items *received*, not locations *checked*. So after run
A the game's per-course "seed owned" state can be zeroed back out, and a
re-entry would report `finish_seed=0` because the seed is not owned --
which looks like the per-run answer but proves nothing. Before trusting a
run C result, confirm the game still considers that course's seed owned
at re-entry (the AP-granted per-world Wonder Seed counts are the lever).

## Reading the results

- **B shows `finish_seed=1`** → the fallback is the fix. Confirm the AP
  server received the `- Wonder Seed` location for that course, then this
  branch is ready to merge (record the capture in this file).
- **B shows `finish_seed=0`** → PlayReport is a dead end; the field is
  bumped on the same path as the Nerve. Go to plan B.
- **C shows `finish_seed=1`** → "already owned" semantics; the fallback
  as written would over-send. Do **not** merge as-is: either drop it, or
  re-gate it (e.g. require `new_flower_count >= 1`, or diff a
  Switch-side per-course seed bit across the run) and re-run the matrix.

## Plan B — if the PlayReport is a dead end

Hook the **save-flag write**, not the pickup: whatever route awards the
seed, the per-course seed bit/count write happens.

- Observability trampolines from the persistence spike already exist:
  `switch-mod/src/probe/SeedTrace.cpp`; the Container-D per-course
  Wonder-Seed writer (hash `0x60458608`, save offset `0x3AF8 + 4*course_idx`)
  is §12 "Active RE" in the RE map.
- Secondary candidate, from §14 (the pipeline we already hook for
  power-up negation): `PlayerRequestItemGet` execute at `+0x1529fd8` ORs
  `1 << *(req+0x20)` into the pending byte at `player+0x86b`; the
  `canGetItemType` reader is `+0x182c8bc`. Seed-related runtime item bits
  are 11 (WonderFlower) / 13 (GroundSead) — the mask `0x2840` the
  `+0x182c8f0` seed special-case tests.

Do **not** try to detect the seed from a count delta
(`world_wonder_flower`, container-A seed counters): the AP-authoritative
`pushWonderSeedOverride` tick clobbers those every ~2 s by design.

---

## Agent prompt (copy-paste)

```
Repo: smbw_ap. Branch to test: claude/wonder-seed-yoshi-tongue-check-ch4gvn
(bridge-side only — no switch-mod rebuild strictly required, but rebuild
+ deploy if your Ryujinx mod is stale).

Read docs/wonder-seed-yoshi-tongue-capture.md first — it has the full
context, the capture matrix, and the decision table. Use the
smbw-build-deploy skill for the build/deploy/run/log-tail commands.

Your job:

1. Check out the branch, run the client test suite (pytest from the repo
   root) and confirm it's green.
2. Launch the SMBW Client + Ryujinx against a scratch AP room, and play
   the four runs in the capture matrix (A control, B Yoshi tongue-grab,
   C already-owned seed without touching the Wonder Flower, D wonder
   phase entered but seed not taken). Pick courses whose Wonder Seed AP
   location is still unchecked.
3. For each run, capture the client log's "wonder-seed tally at
   stage_key=..." line, whether a wonder_seed_awarded nerve line
   appeared, and whether "course_result → wonder_seed" fired. Also note
   whether the AP server actually received the location for run B.
4. Fill the results into the capture matrix table in
   docs/wonder-seed-yoshi-tongue-capture.md (replace "Expect" with what
   you observed, keep the raw log lines in a fenced block), and follow
   the "Reading the results" section:
     - B finish_seed=1 → the fix works; say so and leave the branch ready
       to merge.
     - B finish_seed=0 → PlayReport dead end; write up the plan-B RE
       entry points and stop (do not start the RE in the same session).
     - C finish_seed=1 → "already owned" semantics; the fallback
       over-sends. Re-gate or disable it (_WONDER_SEED_FALLBACK_ENABLED)
       and re-run the matrix.
5. Commit the doc update (and any re-gating) to the same branch and push.

Constraints: don't merge to master, don't change switch-mod hooks in this
session, and don't chase the RE if the capture says PlayReport works.
```
