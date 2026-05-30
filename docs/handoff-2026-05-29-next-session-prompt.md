# Copy-paste this prompt to start the next session

```
I'm continuing the SMBW Archipelago Wonder Seed RE work on the
bridge-cse worktree.  Before doing anything else, read these in
order:

1. docs/handoff-2026-05-29-ws-persistence.md (end-of-day handoff
   from the previous session — comprehensive technical context)
2. CLAUDE.md (project orientation; "Daily dev loop" still
   authoritative for build/deploy)
3. The memory files in your local memory dir, especially
   `feedback_delegate_noisy_ops.md` (preference: delegate large
   tooling output like submodule init, full builds, big log
   dumps to subagents to keep main context clean)

TL;DR of where we are: AP-authoritative Wonder Seed bitfield +
counter writes both work and are crash-free.  Disk persistence
confirmed for the low 64 bits of the container-C bitfield at hash
0x60458608.  Bridge replay covers the high 64 bits.  Counter
ownership (the bridge's hypothesis-driven counter write at the 5
mirror hashes) works.  Live-validated with 12-14 W1 seeds, no
crashes.

But we paused before shipping the bridge bit-packing fix
("GAME_ORDER_LAYOUT proposal" in the handoff) because three open
questions came up that should be answered first:

**Q1: Why does the 80-entry course table only cover ~60% of the
game's 131 levels?**  Where are the ~50 side courses tracked?
Probably a different container-C hash or a container-D layout.

**Q2: Where is the per-seed (which specific seeds in this course)
bitmask?**  Hash 0x60458608 is BINARY per course.  But the
in-game UI shows individual seed icons per course (top-of-flag,
secret exit, wonder phase, badge challenge).  The granular
per-seed state is stored elsewhere.  STRONG candidate: container
D at gmd+0x788, hash-keyed (hash, course_index) -> u32 bitmask
with each bit = one exit type.  The Wonder Seed variant hash is
unknown.  We captured 8 candidate hashes via observability already
— see "Captured per-course hashes from observability" in the
handoff doc.

**Q3: Does data[2..3] of 0x60458608 actually persist for VALID
course indices?**  We saw bit 80 not persist, but bit 80 is the
"Invalid" sentinel per the course table.  Bits 64-79 are valid
indices.  Untested whether they persist.  This is the simplest
question to answer first (set bit 70, save, restart, check).

Please:

1. Read the handoff doc.
2. Start with Q3 since it's the cheapest test and gates the
   bit-packing decision.  Suggest a way to do this without
   requiring the user to play the game.  The simplest approach is
   probably a one-shot probe wired to fire ONCE on first
   SetCourseClearFlagExecute (like the smoke test we removed),
   that calls setContainerCBit(0x60458608, 70, true), then asks
   the user to save+quit+reload and share the new log.
3. Then tackle Q1 and Q2.  For Q2 specifically, the 8 captured
   D_write hashes are the highest-value lead — they were written
   by the game itself during real gameplay and probably include
   the Wonder Seed bitmask hash.  Strategies for identifying
   which one:
   - Cross-reference each hash against decompiled callers (look
     for ones called from the Wonder Seed acquisition Nerve
     path) — use the existing scripts/ghidra/find_hash_immediate_loads.py
     and decompile_container_chain.py
   - The 4 hashes that paired (one wrote value=1 immediately
     followed by another that wrote value=0) suggest "set + clear"
     semantics — possibly a goal_id + bitfield pair where the
     value=0 hash is the "is reached" flag and value=1 is the
     bitmask write.  Examine the bit positions in the value=1
     writes — if they're sparse bits at specific offsets, that
     hints at exit-type semantics.

Working dir is the worktree at
C:\Users\maxwe\Documents\smwonder_archipelago\.claude\worktrees\bridge-cse_01KtYB9wuC3NNZhW48vZ89Zd
— most things have been built and deployed there already, but
specific paths are in the handoff doc.

Auto mode is fine; bias toward acting without clarifying questions
unless genuinely blocked.
```

---

## Why this prompt rather than just summarizing

The next session needs full architectural context to make good
decisions — but the answer to Q3 doesn't require any of it (just a
one-line probe + a reload).  The prompt is structured so the new
agent:

1. Loads the comprehensive handoff doc into context once
2. Identifies the cheapest test (Q3) and runs it before doing
   anything else
3. Uses Q3's outcome to decide the bit-packing strategy
4. Tackles the deeper RE work (Q1, Q2) using the captured 8 D_write
   hashes as the highest-value lead

The prompt deliberately:
- Names the user preference about delegating noisy ops (memory
  files reinforce this but a prompt mention ensures it's noticed
  immediately)
- Gives the agent specific scripts to use rather than asking it
  to discover them
- Tells the agent the working dir explicitly (worktree, not main
  repo)
- Says "auto mode is fine" so the agent doesn't burn turns asking
  clarifying questions for things the handoff doc covers
