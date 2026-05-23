# Save-diff capture analysis — 2026-05-22

Analysis of six consecutive save captures the user made to identify how
SMBW's `game_data.sav` encodes badges, wonder seeds, ten-coins, and
flower coins.

## Captures

All under `C:\Users\maxwe\Desktop\`. Profile `0/` and `1/` are
byte-identical in every capture, so only profile 0 was analyzed.

| # | Folder | Action taken since prior |
|---|---|---|
| 1 | `pre-badge` | baseline |
| 2 | `post-badge` | bought the coin badge |
| 3 | `badge_change` | equipped the coin badge |
| 4 | `plus_one_wonder_seed` | got W1 wonder seed for 100 purple coins |
| 5 | `plus_one_more_wonder_seed_world_2` | got a free W2 wonder seed |
| 6 | `dolphin kick 1 completion...` | cleared Dolphin Kick 1 (+1 seed, +3 ten-coins, equipped Dolphin Kick badge) |

**Caveat:** captures 3 → 4 (`badge_change` → `plus_one_wonder_seed`)
are **byte-identical**. Either the user did not actually save between
those two states, or the +1 wonder seed in W1 did not trigger a save.
Capture 5 contains the changes from both #4 *and* #5's events.

## Headline findings

### 1. The "second-table" region (0x428..0xbf0) is **NOT just string-blob offsets**

This contradicts the prior handoff in
[docs/save-diff-grants.md](save-diff-grants.md#format-we-mapped-on-2026-05-21).
The region is a mix of subsections separated by `(key=0, value=N)`
sentinels. Some subsections hold blob-offset pairs (values
monotonically increase), but at least one subsection holds **counter
pairs identical in semantics to the first table**.

Confirmed counters in the 2T region (profile 0, pre-badge values).
Pair indices below are **full-pair-region indices** from `PAIR_REGION_OFFSET`
(0x28), matching the updated savediff tool's output. Within the old
"2T subsection" they're pairs 141 and 144 respectively.

| Pair (full) | File offset | Hash key | Meaning | Source |
|---:|---|---|---|---|
| 269 | `0x0890` | `0xf4ee6827` | `flower_coin` (purple coin count) | M3.3 probe + this analysis |
| 272 | `0x08a8` | `0x17f0bb21` | `total_play_time_sec` | M3.3 probe + this analysis |

`flower_coin` was tracked across captures:

| Capture | Value | Δ | Event |
|---|---:|---:|---|
| pre-badge | 148 | — | baseline |
| post-badge | 118 | −30 | coin badge purchase = **30 flower coins** |
| badge_change | 118 | 0 | (no purchase) |
| plus_one_wonder_seed | 118 | 0 | (no save) |
| plus_one_more_wonder_seed_world_2 | 18 | −100 | W1 100-coin wonder seed cost **100** |
| dolphin_kick | 53 | +35 | Dolphin Kick 1 course rewarded **35 flower coins** |

The savediff tool currently *excludes* the 2T region from key/value
interpretation; it should be updated to scan the full 0x28..0xbf0
range. The "string-blob offset" subsection can be detected
post-hoc by checking whether values rise monotonically.

### 2. Ten-coin acquisition is a 3-bit-per-course bitfield

Capture 6 collected 3 ten-coins in Dolphin Kick 1. Exactly one byte
went `0x00 → 0x07` (binary `00000111`) at file offset `0x3f68`. The
three bits are almost certainly the three ten-coin slots of that
course; the byte is the Dolphin Kick 1 record.

### 3. Coin-badge ownership is two small flags in a per-badge record

Capture 1 → 2 (badge purchase) flipped these bytes (no first-table
changes at all):

| Offset | Before → After | Hypothesis |
|---|---|---|
| `0x0ea1` | `00 → 02` | "owned" flag in a per-badge record |
| `0x0f3c` | `00 → 04` | secondary flag in same record set |
| `0x0f49` (bit 9 of u32 @ `0x0f48`) | `ff → fd` | "still available to buy" bitmask — bit cleared on purchase |

In capture 6 (Dolphin Kick badge acquired as course reward), three
*different* bytes at `0x0cbf`, `0x0daf`, `0x0ea3` each went `0x00 →
0x20` (bit 5). These are spaced ~240 bytes apart, suggesting per-record
"newly acquired / unviewed" flags in three 240-byte records (badge
entry + two other related records).

### 4. Coin-badge equip flips bit 6 of byte `0x0c5d`

Capture 2 → 3 (badge_change = equipping the coin badge) flipped
exactly one game-state bit in the 0x0c50 region:

```
post-badge:   ff ff ff ff  f3 ff ff ff  ...
badge_change: ff ff ff ff  f3 bf ff ff  ...
                            ↑0x0c5d (bit 6 cleared)
```

`0x0c58..0x0c63` looks like a "badge not equipped" bitmask (1 = not
equipped, 0 = equipped). Bit `(0x0c5d − 0x0c58)*8 + 6 = 46` is the
coin badge's slot.

Notably, **equipping the Dolphin Kick badge in capture 6 did NOT
change this byte**. Possibilities:
- One-time "first-ever badge equipped" flag (tutorial-style).
- The Dolphin Kick badge replaced the coin badge in the *same* slot
  (so a bit was cleared *and* set, net no change to the byte).
- Equipped-slot state is elsewhere; this byte tracks something more
  specific (e.g., "badge has been equipped at least once").

This needs one more capture to disambiguate — a "swap equipped badge
without acquiring anything else" save would clarify.

### 5. Per-world / per-event flag bytes

Each wonder-seed acquisition flipped multiple `0x00 → 0x01` bytes at
scattered offsets:

| Capture transition (event) | Single-bit flips |
|---|---|
| ws1 → ws2 (W1 100-coin seed *and* W2 free seed) | `0x1c5c`, `0x3480`, `0x4e54` |
| ws2 → dk (W4 dolphin-kick seed) | `0x1cd0`, `0x4ec8`, plus more |

`0x1cd0 − 0x1c5c = 0x74` (116 bytes) and `0x4ec8 − 0x4e54 = 0x74`. So
the two regions `~0x1c5c` and `~0x4e54` each look like an array of
116-byte per-world (or per-course) records, with a flag at a fixed
offset within each record that flips on wonder-seed acquisition. The
`0x3480` change in ws1→ws2 doesn't fit the 0x74 stride and may be a
different array.

Capture 6 also flipped `0x0ff9`, `0x1221`, `0x4ec8` (single bits), and
the per-world counter pattern continued — but disentangling
course-clear vs. wonder-seed-acquisition vs. badge-acquisition would
need more single-event captures.

### 6. Bitfield change at `0x0cec` and `0x0ff5`

In ws1 → ws2, two non-trivial bit changes:

| Offset | Before → After | Bits set |
|---|---|---|
| `0x0cec` | `0x80 → 0x82` | bit 1 added (bit 7 already set) |
| `0x0ff5` | `0x00 → 0x08` | bit 3 set |

These look like **per-world wonder-seed bitmaps**. If each world has a
byte (or larger) marking which seeds in that world have been
collected, then `0x0cec` and `0x0ff5` are two such bytes (different
worlds). Bit positions within the byte map to per-seed indices.

### 7. Save-metadata fields that change every save

These two regions change on essentially every save (not tied to any
in-game event):

| Offset | Type | Observation |
|---|---|---|
| `0x16b8..0x16bb` | u32-ish (looks random) | Different in every save; probably a session/save hash or per-save salt |
| `0x50c8..0x50c9` | u16-ish | Monotonically increasing: `0x11e0, 0x1201, 0x178f, 0x178f (no save), 0x19d1, 0x1a59`. Likely a frame/time counter accumulated at save time. |

Useful for filtering noise out of future diffs — savediff should
optionally mask these.

## Diff tool defects discovered (FIXED 2026-05-22)

[`scripts/savediff.py`](../scripts/savediff.py) had three problems
exposed by this corpus, all fixed in the same session:

1. **It skipped the 2T region.** The handoff doc claimed 0x428..0xbf0
   was only string-blob offsets, but pairs in this range include
   counter pairs (flower_coin at pair 269, total_play_time_sec at
   pair 272). The tool now scans the full 0x28..0xbf0 as (key, value)
   pairs.

2. **Trailing-region output had no per-byte context.** The tool now
   prints 16 bytes of hex+ASCII context before/after each change,
   with a `^^` marker line underneath the changed bytes.

3. **No way to filter save-metadata noise.** Bytes at 0x16b8..0x16bb
   (random hash) and 0x50c8..0x50c9 (monotonic counter) change on
   every save regardless of in-game state. New `--mask-noise` flag
   suppresses these.

## New hash keys discovered (additions for `KEY_TO_VALUE`)

Confirmed in this corpus:

```python
0xf4ee6827: "flower_coin",            # was already known from M3.3
0x17f0bb21: "total_play_time_sec",    # was already known from M3.3
```

Candidates worth identifying via further single-event captures (these
have value=0 in pair indices 135-150, the same subsection as
flower_coin and play_time):

```
0xe53c17b8  0x5ac1e406  0x93af7dfc  0x1e0a2a4e  0x03745f51  0x6af4303e
0x20fced8b  0xb47087e6  0xb994f3f9  0xc89f302b  0xc050ac61  0x3db2938d
0x7ca196b7
```

One of these is plausibly `wonder_seed_count_lifetime` or similar —
but it didn't tick in our captures, so the corpus needs an event that
clearly increments it.

## Recommendations / next steps

1. **Update [docs/save-diff-grants.md](save-diff-grants.md)** with the
   corrected layout: 0x428..0xbf0 is mixed subsections (offsets +
   counters), not pure offsets.
2. **Fix savediff.py** to (a) parse the full first+second table as
   key/value pairs, (b) annotate trailing changes with hex context.
3. **Single-event captures** still needed to pin down:
   - Pure equip-swap (no acquisition) — distinguishes equipped-slot
     tracking from one-time-flag.
   - Pure course-clear (no badge/seed reward) — separates
     course-clear-flag offsets from acquisition flags.
   - Pure +1 ten-coin (one coin only) — confirms 3-bit packing at
     `0x3f68`-class offsets.
   - Royal seed (palace boss clear) — to find royal-seed counter.
   - Add a "no-action" save (save → reload → save with no input) to
     identify exactly which bytes are pure save-metadata noise.
4. **Grant code for `flower_coin`** is now trivial — write `value` at
   2T offset `0x890 + 4` in the save buffer, or, in memory, write the
   hash-table entry keyed by `0xf4ee6827`. This is a direct M3.2-style
   incoming-grant capability (e.g. "AP awards flower coins").

## Recap: what this analysis unlocked

- **flower_coin** counter is fully diff-identified — both its
  file-offset location and its hash key. AP can already award flower
  coins by writing this value (pending the runtime anchor work in
  Step 4 of save-diff-grants.md).
- **Ten-coin acquisition** has a known encoding (3-bit per course).
  Per-course offset map needs to be built up via more single-course
  captures.
- **Coin-badge ownership** has known encoding (byte flag at 0x0ea1
  region + bitmap at 0x0f48). Per-badge offset map needs to be built
  up — but the structure is now visible.
- **Wonder-seed acquisition** flips multiple bytes (per-world bitmap
  + per-world counter + scattered flags). Still needs disambiguation
  via single-seed captures.
- **The 2T region's true structure** is mapped enough to know it
  contains the kind of counters M3.3 needs.
