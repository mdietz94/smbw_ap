# Royal Seed check-loss — RE plan

**The bug.** When AP grants a `W<N> Royal Seed` item, the bridge writes
the container-B bool via `probe::grantContainerBBool(hash, 1)`
(NSO+0x0049EA24).  The world-map UI then shows the world's palace as
"cleared" -- so the player has no in-game reason to re-enter the
palace, and the natural `PALACE_CLEAR` PlayReport that fires the
matching AP `- Royal Seed` location *never arrives*.  Result: the AP
location stays unchecked even though the player has the seed.

**The short-term unblock** (this PR).  Mirror the badge auto-resolve
precedent: in `SMBWContext._handle_received_items`, when a Royal Seed
item is received, emit a `CheckEmitted(PALACE_CLEAR, palace_stage_key)`
right then so the bridge sends the matching `LocationCheck` at
item-grant time.  Idempotent via `BridgeState.emit_check`; non-seed
items unaffected.  Code: `apworld/smbw_archipelago/client/context.py`
+ `apworld/smbw_archipelago/client/royal_seed_table.py`
(`palace_stage_key_for_item`).

This unblocks gameplay but is a workaround -- the player never *plays*
the palace.  We want a path where setting the seed bool from AP doesn't
also short-circuit the natural clear, so the player can still play the
palace, clear it, and have the check fire from the actual PlayReport.

## Hypotheses for why the natural path fails

Likely root cause is one of these (ranked by prior probability).  All
of them produce the same observable outcome -- no `PALACE_CLEAR`
PlayReport after AP grant -- and the RE work is to figure out which.

1. **Map-UI gate ("Palace is already cleared, you can't re-enter")**.
   The world-map node for the palace reads the seed bool directly; when
   set, the node either won't accept input or routes the player past
   the palace entirely.  Most likely given the in-game UX semantics.
   Test: load a save where the W1 seed bool is 1 but no other progress;
   try to enter Pipe-Rock Plateau Palace from the world map.  If the
   node is dead, we have our answer.

2. **PALACE_CLEAR PlayReport gated on first-clear flag**.  The
   `koopajr_result` / `course_result` PlayReport path may itself check
   "was this the first time you cleared this palace this save?" against
   a per-course flag distinct from the world-Royal-Seed bool.  If AP
   sets the seed bool but *not* the per-course first-clear flag,
   re-entry would clear the palace but the PlayReport's discriminator
   would suppress emission.  See M2.4 discriminator notes in
   `docs/static-analysis-findings.md`.  Less likely -- the WONDER_SEED
   pickup path uses *the seed itself* as the dedup, not a per-pickup
   flag, but palaces may differ.

3. **Palace entry triggers a different cleared cutscene that doesn't
   emit `koopajr_result`**.  If the seed bool is already set, the
   palace might short-circuit the boss fight (skip Bowser Jr. fight,
   just show the seed-already-grabbed cutscene) and emit some
   "revisit" PlayReport room we haven't classified.  Test: capture
   PlayReport stream during a forced re-entry (use AP to grant W1
   seed, then enter palace anyway via debug menu / Wonder Token bypass
   if there is one).

4. **`SetCourseClearFlagToGameData` nerve gates on "is course already
   cleared"**.  The M1 hook at NSO+0x1bf28cc trampolines this nerve's
   slot-8 execute; the nerve's body may early-out when the course's
   clear flag is already 1, suppressing both the Nerve fire (so our
   COURSE_CLEARED hook doesn't fire either) and the `koopajr_result`
   PlayReport emission downstream.  Easy to confirm via Ghidra static
   analysis of NSO+0x1bf28cc.

## Investigation plan

### Phase 0 (zero-cost): confirm the bug is real

Before anything else, make sure the bug is what we think it is.

- Start a fresh save (or one where no Royal Seeds are obtained yet).
- Connect to AP and grant `W1 Royal Seed` via the `/grant` debug verb
  or by routing the item from another slot's seed.
- Observe the world map: does the palace look cleared?
- Try to enter the palace.  Three possible outcomes:
  - **Node refuses input** → hypothesis 1 confirmed.
  - **Palace loads, boss fight skipped** → hypothesis 3.
  - **Palace loads, boss fight plays, clear PlayReport emits** →
    bug is elsewhere (maybe just dedup in `BridgeState`).  Check
    `[smbwap` log in Ryujinx for a `COURSE_CLEARED` /
    `koopajr_result` line; if it's there but the AP `LocationCheck`
    didn't fire, the bug is in the bridge dedup or location_table,
    not the in-game path.

### Phase 1: static analysis on the world-map gate

Most likely outcome of phase 0 is hypothesis 1.  To confirm, find the
function that reads the seed bool to decide whether the world-map node
is interactive.

- In Ghidra, xref the W1 seed bool hash `0x55815859` as a 32-bit
  constant in `.text`.  We already know the writer at NSO+0x0049EA24
  and the reader at NSO+0x71003838AC; look for *other* readers.
- For each non-writer xref, decompile the containing function.  Look
  for one whose call sites are in the world-map UI module -- pattern
  match on strings like `WorldMap`, `StageSelect`, the world's
  `RequestEvent*` getters, palace-node ID strings.
- If found: that's the gate.  Note its NSO offset for phase 3.

### Phase 2: dynamic capture during re-entry attempt

- Add a temporary observability hook on `FUN_71003838AC` (the
  sub-bool reader from CLAUDE.md) that logs `(reader_pc, hash,
  out_value)` for every read.  This catches *every* place the game
  checks the seed bool -- world-map UI, palace entry, PlayReport
  emission decisions.
- Reproduce the phase 0 scenario.  Cross-reference reader PCs against
  what we found statically in phase 1.

### Phase 3: design the fix

Outcomes shape the fix:

- **If the world map gates on the seed bool**: we likely need to
  decouple "world is unlocked" from "palace is cleared".  Options:
  (a) leave the seed bool alone and grant via a different field that
  doesn't gate the map (look for a separate "world unlocked" flag);
  (b) grant the seed bool *but* also blank the palace's clear flag so
  the world map shows the palace as still-clearable; (c) hook the
  world-map gate function and force it to return "enterable" for the
  palace until our own clear check fires.
- **If the PlayReport itself is gated on a first-clear flag**: we
  need to find that flag and grant only the seed bool but not the
  first-clear flag.  Likely a per-course u8 / bit somewhere in
  container-A or container-B; the M3 grant primitives apply.
- **If the Nerve early-exits on already-cleared**: we may need a
  second hook closer to the Nerve's data fetch to spoof "course not
  yet cleared" only on first re-entry, then back off.  Complex.

In all three cases, the workaround in this PR remains as a safety net
-- the natural fix is additive, not a replacement.  We'll only remove
the workaround after Phase 3 ships and a multi-week dogfood confirms
the natural path is reliable.

## Open questions

- Does the W3 "Royal Seed Mansion" behave differently from the other
  five palaces?  It's the odd-one-out structurally (a mansion, a secret
  exit, no Bowser Jr. boss) and the PlayReport room mix may differ.
- Is the goal Royal Seed (`PI: Bowser's Rage Stage - Royal Seed`)
  affected by this same gate?  The Bowser Rage stage isn't paired with
  an AP "Royal Seed" item so the gate never trips -- but if a future
  rando mode adds it, the same fix would need to apply.
- Does setting the bool to 0 then back to 1 reset the world-map gate?
  If yes, the fix could be even simpler: don't set the bool from AP
  at all until the player has played the palace.  But that would
  require detecting "player wants to play this palace" before granting,
  which is harder than the current absolute-overwrite invariant.

## Pointers

- `apworld/smbw_archipelago/client/context.py` — the short-term fix.
- `apworld/smbw_archipelago/client/royal_seed_table.py` — table maps
  AP item names to (hash, palace_stage_key, mask bit).
- `apworld/smbw_archipelago/client/location_table.py` — the 6
  `(PALACE_CLEAR, _STAGE_*_PALACE)` entries.
- `switch-mod/src/probe/` — `grantContainerBBool` lives here.
- CLAUDE.md "GameDataMgr (gmd::) save-data API" — NSO offsets for the
  container-B bool reader/writer pair.
- `docs/static-analysis-findings.md` — M2.4/M2.5 PlayReport
  discriminator table; the palace WIN path is the
  `koopajr_result`/`course_result` doublet.
