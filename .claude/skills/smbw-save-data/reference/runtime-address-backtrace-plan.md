# Runtime-address backtrace plan

> 📋 **Append-only research log — superseded.** The `gmd::GameDataMgr::sInstance`
> anchor (NSO `+0x0363F0F0`) obviated this Cheat-Engine backtrace workflow.
> Current confirmed facts:
> [smbw-re-map.md](../../smbw-reverse-engineering/reference/smbw-re-map.md). Kept
> for the live-address discovery template if a non-GameDataMgr grant ever needs it.

**Goal**: For each category of grantable trailing-region item (badges,
per-course clears, etc.), identify the **live runtime memory address**
the game's UI/state reads from. Derive a **stable pointer chain** from
a known anchor (NSO base or similar) that the subsdk can reproduce on
any future boot.

**Why we need this**: The buffer we found via the savedata_id UUID
scan turned out to be a save-OUT staging buffer (only exists during/
after save serialization, doesn't drive UI state, writes don't grant
anything). The live state lives elsewhere — almost certainly in a
struct populated at save-LOAD time. Cheat Engine's "Find what writes
this address" feature lets us catch the game serializing from live to
save-out and trace back to the live source.

**Why this is high-leverage**: Many trailing-region offsets (badges,
several per-course array families, lives, etc.) probably live in the
same parent struct in memory. **One successful backtrace may give us
the base pointer for the entire trailing region in one shot.**

## Working anchor — current state of knowledge

| Item | File offset | Last known value | Notes |
|---|---|---|---|
| BC Parachute Cap I clear flag | `0x4438` | 1 | Best probe target — single u32, well-isolated |
| Badge ownership u64 | `0x0EA0` | `0x0000400C00000200` | Independent verification target |
| Normal-course slot 6 clear | `0x4408` | 1 | Adjacent struct (likely same parent) |
| Lives | `0x167C` | (whatever user has) | Standalone, useful independent check |

The save-out buffer's base address changes per session. After any
in-game Save, scan for UUID `75 E6 13 B8 8A 4C 25 EB 52 D0 E0 A3 D0
FA 1A DF` (Array of byte) to relocate it. The buffer disappears
between saves.

## Phase 1: Proof-of-concept backtrace (BC clear flag)

### 1.1 Prepare

1. In Ryujinx, get to the world map on profile 0.
2. Click **Save** in-game menu (populates the save-out buffer).
3. In Cheat Engine (attached to Ryujinx.exe), scan UUID, get a match
   address. Compute `BASE = uuid_match - 0x50B8`.
4. Add a watch:
   - Address: `BASE + 0x4438`
   - Type: 4 Bytes
   - Confirm value = **1**.

### 1.2 Arm the write breakpoint

1. Right-click the BC-clear watch → **Find out what writes this
   address**.
2. Cheat Engine opens a debug window and sets a hardware breakpoint.
   (One of 4 available — keep this in mind, only one breakpoint at a
   time during backtracing.)

### 1.3 Trigger the write

1. In-game, click **Save** again.
2. Cheat Engine catches the write instruction. The debug window
   populates with at least one entry. Click **Show disassembler** to
   inspect.

### 1.4 Identify the source

Cheat Engine sees the x86_64 JIT-translated code (Ryujinx translates
the Switch's ARM64 to x86_64 internally). You'll typically see one of
these patterns:

**Pattern A — direct copy** (easiest):
```asm
mov eax, [rdi+1448]      ; loaded from live state at rdi+0x1448
mov [rsi+1448], eax      ; stored to save-out buffer at rsi+0x1448  <-- breakpoint here
```
→ The LOAD just above the breakpoint reads from `rdi + 0x1448`. RDI
at this moment holds the live struct base. **Live address = RDI +
0x4438** (RDI value visible in CE register pane).

**Pattern B — bulk memcpy** (common for arrays):
```asm
call memcpy              ; rdi = save-out dst, rsi = live src, rdx = size
```
→ RSI at function entry holds the live src pointer. The save-out
byte at `BASE + 0x4438` corresponds to `RSI + (some offset)`. To
identify the offset: it'll be the same as the destination offset
within the save-out struct.

**Pattern C — generic serializer with offset table**:
```asm
ldr   x2, [x1, #...]     ; load offset from table
str   ...                ; write
```
→ Less common but possible. Trace through manually.

**Recording**:
- Note the register holding the live struct pointer (RDI, RSI, or
  whatever).
- Note its value at the breakpoint moment.
- **Live BC clear address = register_value + 0x4438**.

### 1.5 Validate

1. Disarm the breakpoint (red X in debug window).
2. Add a new watch at the computed live address.
3. Confirm it reads as **1**.
4. Write **0** to it.
5. In-game, open the world map. **Does Parachute Cap I show as
   un-cleared?**
   - **Yes** → we found the live runtime address. ✓ Proceed to Phase 2.
   - **No** → there's another layer (live state is yet further upstream).
     Set "Find what writes" on this address and repeat. The runtime
     may have a cache between the "real" state and the save serializer.
6. Restore to **1** before doing anything else. **Do not save.**

## Phase 2: Map the cluster — is it one struct or many?

If Phase 1 found a live base pointer `LIVE_BASE` (where `LIVE_BASE +
0x4438` = live BC clear flag), test whether the **entire trailing
region** lives at the same offset:

| File offset | Live address (predicted) | Expected value | Test |
|---|---|---|---|
| `0x4408` | `LIVE_BASE + 0x4408` | 1 (normal-course slot 6 clear) | u32 read |
| `0x0EA0` | `LIVE_BASE + 0x0EA0` | `0x400C00000200` | u64 read |
| `0x167C` | `LIVE_BASE + 0x167C` | (user's current lives) | u8 read |
| `0x3390` | `LIVE_BASE + 0x3390` | 1 (BC GoalSeed slot 0) | u32 read |

**If all match**: the entire trailing region is one contiguous struct
in memory at `LIVE_BASE`. **One pointer chain covers all
trailing-region grants.** Skip Phase 4's per-category backtraces.

**If some match and some don't**: the live state is fragmented into
sub-structs. Re-run Phase 1 for the items that didn't match, treating
each as a separate cluster.

**If none match**: the BC clear flag lives in its own object, not a
shared trailing-region struct. Re-run Phase 1 per category.

Test the bidirectional UI for any newly-found live address (write,
check UI, restore).

## Phase 3: Derive a stable pointer chain

Cheat Engine addresses change each Ryujinx boot. To grant from the
subsdk we need a **chain of dereferences from a stable anchor** (the
loaded NSO base, or a Ryujinx static module address).

1. Right-click the validated live address (e.g., `LIVE_BASE`) →
   **Pointer scan for this address**.
2. In the pointer scan dialog:
   - **Max level**: start with 4 (= up to 4 dereferences). Increase
     if no results.
   - **Max offset**: 0x1000 (= each dereference's offset can be up to
     4 KB). Reasonable for game structs.
   - **Use static module addresses only**: checked. We want chains
     rooted in `Ryujinx.exe + offset` or similar — those are stable.
3. Cheat Engine takes 10–60 s and produces a chain list (could be
   thousands).
4. **Rescan for stability**: Save → close Ryujinx → reopen → load
   profile 0 → world map → click Save → re-scan UUID → re-find
   LIVE_BASE via the Phase 2 offset test. Then in Cheat Engine
   pointer-scan results, click **Rescan memory** → select the new
   pointer-scan file → set new live address. CE filters down to
   chains that still resolve.
5. Repeat the rescan once more (3 boots total) to filter to truly
   stable chains.
6. Pick the **shortest** chain with the **smallest offsets**.

Expected result: something like
```
Ryujinx.exe + 0x12AB000 → +0x18 → +0x40 → +0x0 → LIVE_BASE
```
or rooted in a Ryujinx module other than the main exe (which is fine
as long as it's stable).

### Cross-check: convert to NSO-relative if possible

If the chain anchor isn't NSO base but the subsdk needs NSO-relative,
do one extra check: find the live address's offset relative to the
known M3.3 container at guest `0x20D3DA07A8`. If LIVE_BASE is in the
same allocation, the relative offset from M3.3 container is a stable
anchor we already have from inside the subsdk.

Alternatively the subsdk can scan for a fixed pattern in the live
state struct itself (e.g., a unique magic byte sequence, or the badge
u64 value if it's distinctive enough) at boot time, same approach
that worked for finding the save-out buffer.

## Phase 4: Per-category backtraces (only if Phase 2 says multiple clusters)

Likely cluster boundaries based on file offsets — if Phase 2 reveals
fragmentation, each of these would need its own backtrace:

| Cluster | File offsets | Probe item |
|---|---|---|
| Palace / shared bitfield | `0x0CD3`, `0x0D3C` | Clapper at `0x0CD3` |
| Badges | `0x0EA0`, `0x16B8` | Badge u64 at `0x0EA0` |
| Lives | `0x167C` | Standalone u8 |
| Purple coin records | `0x1718..0x17B0` | Normal at `0x1718` |
| GoalSeed records | `0x3348..0x3408` | Normal at `0x3348` |
| Shop wonder seed | `0x3480` | Single u32 |
| Wonder Phase records | `0x3AF8` | Normal at `0x3AF8` |
| CourseClear records | `0x43F0..0x44B0` | Done in Phase 1 |
| BC active hash | `0x53E8..0x53F3` | u32 at `0x53E8` |

Repeat Phase 1 + 3 for one item per cluster. Each cluster gets its
own pointer chain (or shares with a sibling cluster if Phase 2 finds
they coalesce).

## Phase 5: Record findings

Add a new section to [docs/save-diff-findings.md](save-diff-findings.md)
mirroring the table below. Update as each cluster's chain is found.

```
| Cluster | Live address (this session) | Pointer chain | Verified ✓ |
|---|---|---|---|
| CourseClear records | (filled in) | (filled in) | (yes/no) |
| ... | | | |
```

After all clusters are mapped, draft the subsdk grant code as a
single new file `switch-mod/src/program/SaveGrants.{hpp,cpp}` that:

- On first frame (or on first save event), resolves each pointer
  chain and caches the live base pointers.
- Exposes per-category grant functions:
  `GrantBadge(int internal_id)`,
  `GrantCourseClear(int course_index, CourseType type)`, etc.
- Each grant function writes the appropriate byte(s) and triggers any
  needed save-dirty or UI-refresh side effects.

## Pitfalls / troubleshooting

- **Hardware breakpoints limited to 4.** Only run one "Find what
  writes" at a time. Cheat Engine will warn if you exceed.
- **Game writes via memcpy/memset for arrays.** When the breakpoint
  fires inside a memcpy, the source is in RSI (x86_64 convention).
  Cheat Engine's debug window shows the call site if you scroll the
  disassembler up.
- **Breakpoint catches multiple writes in rapid succession.** Normal
  for arrays — game writes each element. Look at the first hit after
  the Save click for the cleanest backtrace.
- **Source register holds an immediate (e.g., EAX = 1) rather than a
  load.** Game pre-loaded the value from somewhere. Scroll the
  disassembler up to find the prior LOAD that put 1 into EAX; that
  load's source is the live address.
- **Pointer scan returns zero stable chains.** Try increasing Max
  level (5–7). If still nothing, the chain may pass through Ryujinx's
  guest-memory page table — derive a chain rooted in the subsdk-
  visible NSO base instead, by finding the same struct via byte
  pattern scanning from inside the subsdk.
- **Live address moves WITHIN a single session.** Some game state
  reallocates on world/course transitions. After validating the
  pointer chain in Phase 3, test it across a course entry/exit
  before locking it in.
- **Writes to the live address work but the UI doesn't refresh.**
  Open and close a menu / change screens to force a UI redraw. If
  still stale, the UI has its own cache layer; not blocking for the
  AP grant (which only needs the save+state to update — UI will
  refresh on the next natural redraw).

## Estimated time per phase

| Phase | Time | Notes |
|---|---|---|
| 1 (POC backtrace) | 30 min | Includes first-time CE learning curve |
| 2 (cluster mapping) | 10 min | Just 4 watch reads + one UI test |
| 3 (pointer chain) | 30–60 min | Pointer scan + 2–3 rescans across boots |
| 4 (per-category, if needed) | 20 min × N | Mechanical after Phase 1 |
| 5 (record + draft subsdk) | 1–2 hours | Documentation + C++ |

**Best case (Phase 2 says all-one-struct)**: ~2 hours end-to-end, one
pointer chain unlocks everything.

**Realistic**: 4–6 hours if 2–3 clusters need separate backtraces.
