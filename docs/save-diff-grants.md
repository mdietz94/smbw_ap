# Save-diff handoff: M3 incoming item grants

**Status: planning, no implementation yet.** When you sit down to crack
M3 grants (badges, Wonder Seeds, Royal Seeds) by save-diff instead of
RE, start here.

## Why this doc exists

We spent 2026-05-20 → 2026-05-21 trying to find named API entry points
for badge / Wonder Seed / Royal Seed grants by reverse-engineering the
SMBW NSO in Ghidra. Eleven scripts later
([scripts/ghidra/](../scripts/ghidra/)), the conclusion was:

| Item | Why static RE failed |
|---|---|
| Badges (M3.2) | All ~200 badge-related strings are labels for UI / log / state-machine code, not keys into a grant code path. The actual badge-add operation is either compiled-inline-no-name or routed via vtables / function-pointer tables that Ghidra's xref pass can't follow. |
| Wonder Seeds (M3.3) | The HamletDuFromage "[seed]" cheat anchor at NSO +0x12AF6C is a generic counter getter (`FUN_710012ae94`) keyed by 32-bit hashes of internal stat names. Hash function isn't any of CRC32/FNV/DJB2/SDBM/Murmur3 we tried — Nintendo uses something custom. Without the hash function, we can't enumerate which hash key represents wonder seeds vs flower coins vs play time etc. |
| Royal Seeds (M3.3b) | Same family as Wonder Seeds; same blocker. |

Save-diff bypasses the function-finding problem entirely by going
straight to "what bytes change when the player acquires the item, and
how do I write those bytes when AP grants the item?"

See the deeper finds + the dead-end stories in [milestones.md M3.2 /
M3.3](milestones.md) — they're worth skimming so you don't repeat the
same Ghidra paths.

## The strategy

Per item type we want to grant:

1. **Capture** the save buffer twice — before and after acquiring one
   instance of the item in-game.
2. **Diff** the two buffers byte-by-byte. The differing region is
   where that item's ownership / count is stored.
3. **Identify** the field (single byte? bit in a bitfield? uint32
   counter?) and its offset in the save buffer.
4. **Locate** the corresponding live address: SMBW loads the save into
   memory at boot; the live copy is at a constant offset from a known
   anchor (e.g. the `container` at `0x20d3da07a8` we already captured
   for M3.3 — almost certainly part of the same per-player state).
5. **Write** the same byte/bit/counter change in our subsdk when AP
   sends a grant.

The Ryujinx setup makes step 1 easy (we don't need an Atmosphere SD
extraction or anything else exotic). Steps 2-4 are scripted in Python
on the PC. Step 5 is a 5-line addition to `main.cpp`.

## Step 1 — capturing the save buffer

### Ryujinx (development target)

SMBW's title ID is `010015100B514000`. Ryujinx stores user saves under:

```
%APPDATA%\Ryujinx\bis\user\save\<save_uid>\<user_uid>\<title_id_hex_segments>
```

The exact `<save_uid>` is allocated by Ryujinx on first save creation
and varies. To find it:

1. Open Ryujinx → right-click the SMBW game in the library → **Open
   Save Directory**. That opens an Explorer window pointing directly at
   the SMBW save root.
2. Inside, you'll see files like `userdata.bin` (name varies — SMBW's
   own choice). That's the save buffer.
3. Note the absolute path; the capture script will read it directly.

Alternative path (when Ryujinx isn't running): grep `%APPDATA%\Ryujinx\bis\user\save\`
for directories whose `Common/SaveDataMeta` shows title id
`010015100B514000`.

### Real hardware (later)

When we move to Atmosphere CFW, the save lives at
`/atmosphere/contents/010015100B514000/save/...` on the SD card.
Easier to extract via JKSV or similar. Out of scope for first
implementation.

## Step 2 — capture protocol per grant type

For each grant type, do the following dance:

### Badges

1. Boot the game in Ryujinx with the existing subsdk attached (so
   PlayReports + nerve hooks fire — useful for cross-referencing).
2. Make a note of which badges you currently own (the badge select
   screen shows your collection).
3. Play to the in-game **save** point. (Press the in-game pause →
   save, OR clear a course which auto-saves.) **Take care that no
   other state changes between this save and the next** — don't
   collect coins, don't move on the world map, etc.
4. Quit to the home menu so the save flushes to disk.
5. **Copy `userdata.bin` to `before_badge.bin`** (preserve the
   original).
6. Resume the game. **Acquire exactly one new badge**: a Badge
   Challenge clear, a Badge Shop purchase, or a flower-pickup that
   awards a badge. Avoid acquiring anything else.
7. Save again (in-game pause → save), quit to home menu, flush.
8. Copy `userdata.bin` to `after_badge.bin`.
9. Diff (next step).

### Wonder Seeds

1. Same preamble, but the in-between action is **clear a course that
   awards a Wonder Phase seed** (any normal course you haven't cleared
   yet — the Wonder Flower mid-course gives you one when you complete
   the Wonder Phase, and the flagpole always gives a "finish" seed).
2. Compare `before_seed.bin` vs `after_seed.bin`. Expect changes in:
   - A per-course "seed collected" bit/byte (boolean).
   - The world-wide wonder seed counter (uint8 or uint16; increments
     by 1 per seed collected).
   - Possibly a timestamp or "last collected" field.

### Royal Seeds

1. Acquire ONLY a Royal Seed (beat a Palace boss). Avoid any other
   Wonder Phase grabs during the run.
2. Same diff procedure.
3. Expect 1 bit change for the per-palace flag + an increment to the
   Royal Seed counter (uint8, 0..7).

## Step 3 — analyzing the diff

We don't have the script written yet, but here's what it should do:

```python
# bridge/scripts/savediff.py  (TODO — write this)

def diff_saves(before: bytes, after: bytes) -> list[Change]:
    """Return per-offset changes.

    For each contiguous run of changed bytes, return a Change with:
      - offset (int)
      - length (int)
      - before_bytes (bytes)
      - after_bytes  (bytes)
      - heuristic_kind (bit | byte_inc | u16_inc | u32_inc | other)
    """
    ...
```

Expected output for a clean badge capture:

```
=== changed regions ===
  offset 0x12340  length 1   before=0x00  after=0x02  (bit set: position 1)
  offset 0x12348  length 4   before=00 ... after=01 ... (u32 inc by 1: badges_total)
  offset 0x1c000  length 8   before=<old timestamp> after=<new timestamp> — ignore
```

The badge bitfield is the first change (one bit set). The total
counter increment is the second. The timestamp is noise.

**Heuristic helpers the diff script should include:**

- **Single-bit changes**: highlight when only one bit differs in a
  byte — likely a per-flag bit in a collection bitfield. Print the bit
  index (0-7) for easy mapping.
- **Counter increments**: detect when after = before + 1 (u8, u16,
  u32, u64 widths). Likely a "total acquired" counter.
- **Timestamp filter**: changes in fields that look like Unix
  timestamps (large numbers near `time.time()`) are usually irrelevant
  game-state noise. Filter or de-emphasize.
- **Multiple captures comparison**: if you do 2-3 captures of the
  same action (clear 2 different courses for Wonder Seeds), diff the
  diffs — the *common* changes across both are the real grant
  semantics; the *differing* changes are per-course identifiers.

Save files are likely either plain or trivially obfuscated. If they
look like random noise (high entropy throughout, no obvious zero
regions or repeated patterns), they're encrypted and we'll need to
find the key. SMBW likely uses Nintendo's SaveDataMeta format which
is documented in libnx — probably plaintext for the application save
buffer.

## Step 4 — locating the live address

Once we know the byte offset in the save buffer, we need its address
in the running game's memory.

Two scenarios:

**(a) Single saved-state container in memory.** SMBW loads the save
into a single struct in heap memory. The container's address is
constant within a session (e.g., `0x20d3da07a8` from our M3.3 probe).
The save bytes start at some constant offset within that container.

To find it:

1. Pick a unique byte sequence from the save (e.g., the `savedata_id`
   UUID string `b813e675-eb254c8a-a3e0d052-df1afad0` we already know
   from PlayReports — 35 bytes, almost certainly unique).
2. Add a one-shot scanner hook to the subsdk that searches the heap
   for that byte sequence on first run, logs the address.
3. The save-buffer-in-memory's offset = `<found address> -
   (offset_of_savedata_id_field_in_save_buffer)`.
4. From there, our grant code does `*(found_address +
   badge_bitfield_offset) |= (1 << badge_bit)`.

**(b) Save is decoded into per-field globals.** If SMBW doesn't keep
the raw save buffer in memory and instead unpacks into fields scattered
across various objects, we'd need to find each per-field address
separately. More work; only do if (a) fails.

The M3.3 probe already captured a stable container address; that's
the most likely anchor.

## Step 5 — write the grant code

For each grant type, the subsdk code is small:

```cpp
// In main.cpp, alongside existing hooks.

// Captured at runtime via the save-data anchor hook.
static uintptr_t s_save_buffer_in_memory = 0;

void GrantBadge(int badge_id) {
    if (!s_save_buffer_in_memory) return;
    uint8_t* bitfield = reinterpret_cast<uint8_t*>(
        s_save_buffer_in_memory + BADGE_BITFIELD_OFFSET);
    bitfield[badge_id / 8] |= (1 << (badge_id % 8));
    // Also bump the "total acquired" counter so the UI shows the right number.
    uint32_t* total = reinterpret_cast<uint32_t*>(
        s_save_buffer_in_memory + BADGE_TOTAL_COUNTER_OFFSET);
    *total += 1;
    // Optional: flag the save as dirty so it persists.
    // TBD: find what the game does on a normal badge acquire — likely
    // it sets a "needs save" flag or calls a save-dirty function.
}
```

Wired to bridge events via the existing `bridge/protocol.py` —
extend `CheckKind` with item-grant message variants and dispatch in
`bridge/processor.py` (TBD when the LAN socket lands in M4).

## Pitfalls

- **Save encryption**. If SMBW encrypts user save data, the byte-level
  diff will show noise instead of clean changes. Check by inspecting
  `userdata.bin` in a hex editor first — clean saves have obvious
  zero regions and ASCII strings; encrypted saves look uniformly
  random.
- **Save data layout changes between sessions**. If SMBW serializes
  with non-deterministic ordering (e.g. a hash map), the bytes might
  shuffle between saves even without any grant. Mitigate by doing
  multiple before+before captures with no in-between action and
  diffing — if they differ, we have ordering noise; ignore those
  regions.
- **The grant function does more than write the byte**. UI updates,
  achievement triggers, save-flag flipping. Writing the byte directly
  might leave the UI showing the old value until something forces a
  refresh. Workaround for UI: write at a moment when the UI is
  guaranteed to refresh shortly (e.g. on course-clear, on overworld
  load). Workaround for save: also write the "save dirty" flag (if we
  can find it).
- **Multi-byte changes that look like a write but are actually a
  recalc**. Some derived fields (totals, percentages, etc.) get
  recomputed every save based on other fields. We probably want to
  write the *primary* field and let the derivations happen naturally
  on the next in-game tick.
- **Per-instance hash collisions**. If badges share a bitfield with
  other achievements, setting an unowned-badge bit might also affect
  unrelated state. Inspect the change region's size against the known
  badge count (24) to estimate.

## What's already in the repo to help

- [scripts/identify_seed_keys.py](../scripts/identify_seed_keys.py):
  the hash-reversing attempt for the wonder seed counter. Useful
  template for any future hash-identification work.
- [scripts/ghidra/](../scripts/ghidra/): the 11 Ghidra scripts that
  characterized the badge / wonder seed / hash table structures.
  Don't repeat these — they're the dead-end evidence.
- [bridge/play_report.py](../bridge/play_report.py) + the M2.4
  PlayReport corpus: gives us reference values to cross-check against
  save bytes (the `savedata_id` UUID is the strongest anchor, and
  things like `world_wonder_flower` give us "current count" values to
  look for as u8/u16 values in the save buffer).

## Recommended order

1. Capture badge before / after — badges are the simplest binary
   bitfield, easiest to interpret.
2. Then wonder seeds — slightly more complex (per-course flag +
   per-world counter, possibly).
3. Then royal seeds — same family as wonder seeds, should be quick.
4. Build the runtime address-anchor hook (the `savedata_id` scanner).
5. Wire up the per-grant code in `main.cpp`.
6. Test end-to-end with one hardcoded grant at boot before doing the
   full AP wiring via the bridge.

Should take a single focused session per item (~30 min for capture +
diff + identification, then code).
