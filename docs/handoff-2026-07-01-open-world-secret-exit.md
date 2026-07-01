# Open-world secret-exit unlock — SHIPPED 2026-07-01

**Status: DONE, live-confirmed.** [PR #158](https://github.com/mdietz94/smbw_ap/pull/158).

## Problem

In **open-world mode**, the "replay" secret-exit courses — **Operation Poplin
Rescue** (W5, `Course551`) and **Royal Seed Mansion** (W3, `Course531`) — never
presented their secret path, so the `SECRET_EXIT` AP checks (and the downstream
Special-World "Dangerous Donut Ride" checks) were unreachable. Player-reported:
beat the W5 palace in open-world, re-entered for the secret exit, no secret path.

## Root cause (datamined, offline)

From `BancMapUnit/Course551_Course.bcett.byml` (via the RomFS tooling, no Ghidra):
the course has **two `ObjectAreaGoal`s** — normal (`GoalID 0`, always present) and
secret (`GoalID 1`, `InLinks:{Create:1}` → spawned on demand). Two
`BoolGameDataTag`s read the transient bool **`IsInClearedCourse`** (murmur
`0xbef2db36`, category `Bool`, **`SaveFileIndex -1`**, latched at actor-init since
`IsCheckEachFrame:false`); when true they **`Create` the secret goal** and
**`Delete` the 12 `ObjectBlockHardBreakableStone`** blocking the secret route (and
swap the Poplin dialogue to its replay version). It is the *only* GameData gate in
the level. Vanilla sets it true on re-entry of a cleared course; open-world bypasses
the linear world-clear flow that would, so it stays false and the secret path never
appears. **Not a route wall** (the W5 palace gates on the normal exit `GoalID 0`),
so no fill/softlock risk.

Bonus datamining artifacts (reusable): `RSDB/StageInfo.Product.100` row `__RowId`
**is** the PlayReport `stage_key` (→ Operation Poplin Rescue = `Course551`, Royal
Seed Mansion = `Course531`); `CourseInfo.CourseId` = `0xdf82e9ab` (Enum, per-world
`Course1..Course80`); world-file map via `kWorldValToBucket` (WMI006 = W5 Fungi
Mines, WMI004 = W3). See memory `smbwap-secret-exit-isinclearedcourse`.

## Fix

**Bridge** (`apworld/smbw_archipelago/client/`): new `SetForceClearedCourses` wire
message + `force_cleared_table.py`. A course is forced iff it has no `NORMAL_EXIT`
location (→ always) else its `NORMAL_EXIT` location has been checked (so the player
plays the normal exit first). Both current courses award a Royal Seed on the normal
goal, so both resolve to "always". Synced on Connected / ReceivedItems / HelloMsg /
periodic tick, mirroring the routable-worlds mask.

**Applies in both open-world AND standard mode (revised 2026-07-01).** Originally
gated to open-world, then broadened: it's *necessary* in open-world (the synthesized
access flow never arms the flag), and in standard mode it's redundant-but-safe (the
game most likely sets `IsInClearedCourse` itself on replay of a cleared course, so
force-writing true when already true is a no-op). We could **not** RE-confirm which
persistent field the game reads to set the flag — the hash is mov/movk-materialized
+ data-driven through the GameData manifest, so it's un-searchable with the current
Ghidra bridge (no constant/byte/instruction search; `decompile`/`search_instructions`
time out; the flag name is not a string in the NSO). That leaves open the (unlikely
but unruled-out) possibility that an AP-authoritative overwrite in standard mode
(Wonder Seed counts / per-course bitfield, which the bridge clobbers every tick)
clears the flag's source. Since forcing is safe-if-unnecessary and a fix if it
isn't, we force in both. **Standard-mode caveat:** these two search-party courses
now show their secret path from *first* entry (blocks removed, secret goal present),
not only on post-world-clear replay — consistent with the "always secret for
no-NORMAL_EXIT courses" gating intent, and not a softlock (the normal goal is still
present for the Royal Seed).

**Switch** (`switch-mod/src/`): at `SceneTransition` (course-load) read
`(world_val 0x9f5ead3c, CourseInfo.CourseId 0xdf82e9ab)` and, for a matching flagged
course, write `IsInClearedCourse` via `probe::grantContainerBBool(0xbef2db36, 1)`
**before** the level's `BoolGameDataTag` latches it — spawning the secret goal and
removing the wall blocks. Course identity table `kForceClearedCourses` in `main.cpp`:
Operation Poplin Rescue = `(world_val 6, CourseId 0xcd7c09bb)`, Royal Seed Mansion =
`(world_val 4, CourseId 0x37d76dc1)`.

**Gotcha caught in live test:** a Bridge→Switch message needs wiring at **three**
subsdk sites — `ApProtocol::decodeInbound`, `ApClient::handleLine` (the
easy-to-miss forward-to-ring `switch`), and `ApFrameBridge::drainInbound`. Missing
`handleLine` decoded the message then silently dropped it (no error, no cache log).
See memory `smbwap-inbound-wire-three-sites`.

## Relationship to the Royal-Seed check-loss spike

Distinct problem, same "replay a cleared course" family. This fix handles the
**open-world secret-exit spawn**; the broader Royal-Seed **check-loss / gate-entry**
work (letting the player replay a palace so the Royal Seed clears naturally instead
of via bridge auto-resolve) — see [royal-seed-check-loss-re-findings.md](royal-seed-check-loss-re-findings.md)
and [royal-seed-gate-entry-design.md](royal-seed-gate-entry-design.md) — is
**still open** and unaffected.
