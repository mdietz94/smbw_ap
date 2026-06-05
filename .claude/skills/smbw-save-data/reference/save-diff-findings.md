# Save-diff findings — growing notes

> 📋 **Append-only research log — not the source of current truth.** For
> present-tense confirmed facts (save offsets, badge map, hashes) see
> [smbw-re-map.md](../../smbw-reverse-engineering/reference/smbw-re-map.md). This
> file may contain superseded claims; read it for *how/why we found something*,
> the map for *what is true now*. Remember: these are save-OUT offsets, **not**
> live-writable.

Append-only log of what each capture told us. Capture protocol per item
is in [save-diff-grants.md](save-diff-grants.md).

---

⚠️ **What this work produced (read first, 2026-05-24 retrospective):**

This sprint mapped the on-disk `game_data.sav` format byte-by-byte
and located its in-memory mirror via the `savedata_id` UUID scan.
**The in-memory buffer turned out to be the save-OUT staging buffer**
— it exists transiently during/after save serialization, the game
populates it FROM live state on every save, and writes into it are
overwritten on the next save event. **It is NOT a viable target for
live grants** — see [docs/runtime-address-backtrace-plan.md](runtime-address-backtrace-plan.md)
for the discovery.

What this work IS useful for:

1. **Save-file editor capability** — offline modification of
   `game_data.sav` files works (the editor lives in
   [scripts/savediff.py](../scripts/savediff.py)).
2. **Byte-level verification target** — after a successful live
   grant via the GameDataMgr API ([docs/static-analysis-findings.md](static-analysis-findings.md)),
   we can predict and confirm the bytes that will appear in the
   saved file.
3. **Hash-key + offset corpus** — the 8 MemetendoYT pair-region keys
   (flower_coin, regular_coin, 6 Royal Seeds, COMPLETE_GAME, INTRO),
   the trailing-region per-course array offsets, and the badge
   bitfield at `0x0EA0` are all real semantic-byte mappings that
   inform what hash to call the GameDataMgr writer with, or what
   live offset to find for non-hash-keyed fields like badges.

The capture logs below remain valid as a save-format reference;
they just don't directly drive grant code anymore.

---

## 2026-05-22 — Badge capture #1: Coin Reward Badge (Poplin Shop, 30 flower coins)

User reported state at capture time: 5 lives, 26 regular coins, 16
wonder seeds, 1 royal seed. Purchased Coin Reward Badge from Poplin
Shop for 30 flower coins (chosen specifically so no level play would
contaminate the diff).

### Changed offsets (slot 0 game_data.sav; slot 1 mirrors slot 0 in
Ryujinx so the diffs are identical)

| Offset | Width | Pre | Post | Interpretation |
|---|---|---|---|---|
| `0x0894` | u32 | 148 (`0x94`) | 118 (`0x76`) | **`flower_coin` counter** — key `0xf4ee6827` at file offset `0x0890..0x0893` (second-region (key, value) pair). Delta -30 matches the badge price exactly. ✓ |
| `0x0ea0` | u32 | 0 | 512 (= bit 9) | **TBD** — possibly per-category badges bitfield, or unrelated state. Neighboring u32 at `0x0ea4` is `0x400c` (= bits 34/35/45/46 in a u64 reading) and didn't change. |
| `0x0f3c` | u32 | 0 | 4 (= bit 2) | **Most likely the master "badges owned" bitfield, indexed by SMBW internal badge_id (= apworld items.json badge order).** Coin Reward Badge is at index 2 in the apworld badge list (Parachute=0, Wall-Climb=1, Coin Reward=2). ★ Best grant-write target. |
| `0x0f48` | u64 | `0xffffffff_ffffffff` | `0xffffffff_fffffffd` | **Shop inventory mask**, bit 1 cleared = "shop slot 1 (the one Coin Reward sat in) is no longer for sale". Different bit position from the badge id, so this mask indexes shop slots, not badge ids. |
| `0x50c8` | u32 | `0x000011e0` (= 4576) | `0x00001201` (= 4609) | **Session noise** — sits immediately after the `savedata_id` UUID (`b813e675-eb254c8a-a3e0d052-df1afad0` at `0x50b8`); likely a session counter or play-seconds total. Ignore. |

Other save files: `game_account.sav`, `option_data.sav` unchanged.
`game_sub_data.sav` changed 8 bytes at `0x015c..0x016f` — a shifting
ring-buffer (old `0x015c` value reappears at `0x016c` post-save).
Standard metadata noise.

### Disproven prior annotation

The M3.3 probe's `identify_seed_keys.py` had `0x17f0bb21: 26  # likely
total_play_time_sec (W1-1 had 26s)`. The save shows this key at
`0x08a8` with value 26 — but the user has exactly 26 regular coins,
not 26s play time. **Corrected: `0x17f0bb21` is `regular_coin_count`,
not play_time_sec.** Update the corpus accordingly.

### User's pre-purchase badge ownership (revealed 2026-05-22)

User owned **3 badges** before buying Coin Reward: Parachute Cap
(apworld #0), Wall-Climb Jump (apworld #1, currently equipped),
Auto Super Mushroom (apworld #3).

This disambiguates the two candidate fields:

- **`0x0f3c` CANNOT be the master owned-badges bitfield** indexed by
  apworld order — pre-value 0 contradicts 3 pre-owned badges (which
  would require `0b1011 = 0x0B`). Most likely interpretation: it's a
  "shop-purchased badges, ever" bitfield. User had never bought from
  the shop before, so pre=0; now bit 2 set = Coin Reward apworld
  index 2 confirmed-purchased-from-shop.
- **`0x0ea0` IS the master owned-badges bitfield, indexed by sparse
  SMBW internal badge_ids**. Pre-existing 4 bits (positions 34, 35,
  45, 46 in the u64 interpretation) likely represent the 3 owned
  badges + one extra bit (possibly an "equipped flag" given user
  equips Wall-Climb, or a starter/built-in flag). The added bit 9 on
  purchase = Coin Reward's SMBW internal badge_id is **9**.

The PlayReport corpus's `equip_badge_id=[34]` is consistent — internal
ID 34 falls in the pre-existing bit positions, plausibly Wall-Climb
(the equipped badge).

### Implication for M3 grant code

To grant any AP badge, we need to know its SMBW internal badge_id (the
bit position in `0x0ea0`'s bitfield). We have ONE confirmed mapping
today (Coin Reward apworld #2 → internal 9). Building the full
24-entry table efficiently:

- 24 brute-force captures (1 per badge): slow but works.
- N captures + extrapolation: if internal-ID pattern is discoverable
  (e.g., monotonic with apworld order skipping some, or grouped by
  Action/Boost/Expert category, or matching SMBW's UI order), 4-5
  captures may suffice.
- Cheaper experiment first — **switch the equipped badge in-game (no
  shop purchase, no flower coins spent), save, capture**. Diff
  reveals the "equipped badge" field's location AND likely toggles
  bits in `0x0ea0` between the old and new equipped badges. That
  gives us TWO more internal_id mappings (Wall-Climb #1 and whichever
  badge gets equipped) in one capture.

### Next concrete capture proposal

In-game: equip Auto Super Mushroom instead of Wall-Climb Jump. Save
& quit. Copy `game_data.sav` to `equip_change.sav`. Diff vs
post-badge:

```pwsh
python scripts\savediff.py "post-badge\0\game_data.sav" "equip_change\0\game_data.sav"
```

Expected results:
- 1-2 changed bytes in some "equipped_badge_id" field (likely a u32
  storing the internal_id directly, similar to PlayReport's
  `equip_badge_id`)
- Possibly toggles in `0x0ea0` if it's structured per-character or
  per-equipped-slot
- No flower coin change, no shop change

## 2026-05-22 — Badge capture #2: equip swap Wall-Climb → Auto Super Mushroom

In-game switched equipped badge from Wall-Climb Jump to Auto Super
Mushroom (no flower coins spent, no shop interaction).

### Changed offsets (slot 0 game_data.sav, vs post-badge baseline)

| Offset | Width | Pre | Post | Interpretation |
|---|---|---|---|---|
| `0x0c5d` | u8 | `0xff` | `0xbf` | Single bit (6) cleared in a 16-byte sea-of-0xff bitfield starting at `0x0c58`. **"Badges ever equipped" tracker.** Bit position in u96 = 46 = SMBW internal_id of Auto Super Mushroom. Pre had bits 34, 35 cleared (Wall-Climb + Parachute Cap, the two badges previously equipped). |
| `0x16b8` | u32 (LE) | `0xe41b1aba` | `0xb77086e2` | **Currently-equipped badge identity hash.** Wall-Climb's name-hash → Auto Super Mushroom's name-hash. Surrounded by a sea of `0x7e3d1e46` null-sentinels (likely fixed-size array of equipped-badge slots for multiplayer or recent history). |
| `0x50c8` | u32 | `0x00001201` | `0x0000178f` | Same session-counter noise as previous captures. Ignore. |

**Crucially: `0x0ea0` did NOT change.** That confirms the master
"badges owned" bitfield at `0x0ea0` tracks pure ownership; equipped
status lives separately. Equipping a badge you already own doesn't
touch ownership state. ✓

### Confirmed apworld → SMBW internal_id mappings (4 of 24)

| apworld # | Badge | SMBW internal_id (bit in `0x0ea0`) | Equip name hash |
|---|---|---|---|
| #0 | Parachute Cap Badge | 34 or 35 | unknown |
| #1 | Wall-Climb Jump Badge | 34 or 35 | `0xe41b1aba` |
| #2 | Coin Reward Badge | **9** | unknown |
| #3 | Auto Super Mushroom Badge | **46** | `0xb77086e2` |

The internal_ids are sparse and non-monotonic (#2 → 9, #3 → 46), so
no simple offset extrapolation. Each badge needs an empirical capture
to identify its bit position.

### Lookup-shortcut attempt — failed

Checked whether equip identity hashes appear as keys in the second
region (file offsets `0x428..0xbf0`): they don't. The hashes are pure
identifiers, not lookup keys into the second table. Also, the second
region's "values" turned out to be single-byte records (the recurring
`0x51`, `0x64` threshold constants), not string-blob offsets — so the
"string blob" guess from yesterday was wrong; that region is more
structured records.

Implication: no way to extract all 24 badge mappings from this single
save. Need either ~20 more captures, or a runtime/Ghidra approach.

### Forward options for the remaining 20 internal_ids

1. **Cheap-iteration equip cycle** (~20 captures): in-game equip each
   unowned badge once, save, capture. Each capture reveals one new
   bit in `0x0c58` = that badge's internal_id. Cost: each capture is
   a save+quit+copy cycle, no flower coins. Slow but reliable.
2. **Buy-all-badges sweep**: spend flower coins to buy every shop
   badge over multiple shop refreshes. Each purchase reveals the
   bit in `0x0ea0`. Cost: ~flower coins per badge × 24. Combine with
   capture cycle.
3. **Heap scanner + write-all approach (MVP shortcut)**: instead of
   granting per badge, set ALL 64 bits in the in-memory `0x0ea0` u64
   at session start = "all badges available". This is an
   "all-or-nothing" grant that ignores AP's per-item progression but
   is unblocking for a first integration.
4. **Find the internal_id assignment in the binary via Ghidra**:
   surface a function that names badges or their bit positions. Two
   prior Ghidra attempts on the badge system dead-ended (see
   docs/milestones.md M3.2 status); revisit only as last resort.

### Decisions

- `0x0ea0` is locked in as the master "owned badges" u64 — confirmed
  by the no-change-on-equip-swap test.
- `0x16b8` is the equip slot; writing the name-hash there equips a
  badge (assuming it's already owned). Useful as a UX touch (auto-
  equip an AP-granted badge so the player notices), not as a grant
  mechanism.
- Going with **option 1 (cheap-iteration captures)** per user decision
  on 2026-05-22. Batch tool at
  [scripts/badge_map_builder.py](../scripts/badge_map_builder.py).

## Capture protocol for next sessions

### M3.2 — incremental badge mapping (continue from this session)

The user owns 4 badges as of this session. Equip-cycle reveals the
internal_id for badges not yet in the "ever equipped" cleared set
(currently bits 34, 35 are cleared = 2 prior equips). Plan:

1. **Equip Coin Reward → save → copy save folder** as
   `equip-coin_reward` on Desktop. Expected: bit 9 newly cleared in
   `0x0c58` (matches the bit 9 set in `0x0ea0` on purchase, confirming
   internal_id 9). If a DIFFERENT bit clears, the equip and ownership
   bitfields don't share the same internal_id space — important
   finding either way.
2. **Equip Parachute Cap → save → copy** as `equip-parachute_cap`.
   Expected: no new bit clears (already in pre-existing set 34/35).
   But `0x16b8` should change to Parachute Cap's name-hash. That hash
   becomes our second known equip-identity.
3. **Equip Wall-Climb → save → copy** as `equip-wall_climb`. Same
   expectation as #2; the unchanged 0x0c58 confirms both 34 and 35
   are the user's pre-equipped badges. `0x16b8` should return to
   `0xe41b1aba`.
4. **Going forward**, every time the player acquires a NEW badge
   (course reward, Wonder Phase, Badge Challenge clear, shop
   purchase), do: equip it once → save → copy. The diff vs prior
   capture reveals that badge's internal_id.

Add each capture's `(label, path)` to the `CAPTURES` list in
`scripts/badge_map_builder.py` and re-run.

### M3.3 — Wonder Seed capture (next session)

Capture protocol per [save-diff-grants.md](save-diff-grants.md) Step 2:

1. Save state in-game so nothing changes between captures (don't
   collect anything, don't move on the world map).
2. Quit → copy save folder to Desktop as `pre_seed`.
3. Resume. **Acquire one Wonder Seed** — easiest source is the
   "finish seed" you get just for touching the goal pole on a course
   you haven't cleared yet. Avoid Wonder Phase trigger (the
   mid-course flower) since that gives 1 seed AND completes the
   course — extra noise. Best target: clear a course you haven't
   cleared in W2 if W1 is done, or find one in W1 you skipped.
4. Save → quit → copy as `post_seed`.
5. Run `python scripts\savediff.py pre_seed\0\game_data.sav post_seed\0\game_data.sav`.

Expected changes (predicted from save layout):
- A new bit set in the first-table region (file offset 0x28..0x428)
  — one of the 128 (hash_key, value) pairs flips from `value=0` to
  `value=1`. This is the per-course "finish seed collected" flag.
- The wonder seed total counter increments somewhere — possibly
  another (key, value) pair updating from N to N+1.
- Possibly `0x0ea0` doesn't change (badges only), so wonder seeds
  use a different ownership mechanism.

### M3.3b — Royal Seed capture (next session)

Same as M3.3 but the in-between action is **beating a palace boss**
(Bowser Jr fight) to earn a Royal Seed. The user has 1 royal seed
currently, so any palace they haven't cleared will give #2.

Expected changes:
- A new bit set somewhere indicating "Palace N cleared".
- A counter increment from 1 to 2 in the royal seed total.
- Possibly the `koopajr_result` PlayReport mirror — but we already
  decode that in M2.4, so the save-diff side is just for grant
  identification.

### Confirmed wins from this capture

1. **`0xf4ee6827` (flower_coin) is grantable** via writing the runtime
   hash-table-backed counter (when we know the live address). Useful
   for M3 grant code: "Grant N flower coins" = increment the value
   tied to this key.
2. **`0x17f0bb21` (regular_coin) corpus correction**.
3. **24 of 24 badges in the apworld correspond 1:1 with SMBW's internal
   badge ordering** (assumed from the apworld author's likely habit of
   listing them in SMBW UI order; confirmed by the bit 2 ↔ Coin Reward
   = items.json index 2 match).
4. **Badge state lives in 3 byte clusters in the string-blob region**,
   not in the first counter table or the second (key, value/offset)
   table. The grant code needs to find these byte clusters at runtime
   via a save-buffer scanner.

### Decided in this session

- The save-diff approach works cleanly — plaintext, single discrete
  changes, no encryption.
- For badges specifically, **one more capture (any second badge from
  the shop) would lock in the `0x0ea0` vs `0x0f3c` question**.
  Different badges with known internal IDs are even more powerful —
  they show whether bit positions track item internal_id (preferred)
  or shop slot or some other index.

## 2026-05-23 — Wonder Seed capture #1: shop purchase (16 → 17 seeds, −100 flower coins)

User paid 100 flower coins (= "purple coins" in their wording) at the
Poplin Shop to buy a single Wonder Seed, taking the total from 16 → 17.
No course play, no badge interaction.

Run:

```pwsh
python scripts\savediff.py `
    "C:\Users\maxwe\Desktop\before-badge\0\game_data.sav" `
    "C:\Users\maxwe\Desktop\+1 wonder seed, -100 purple coins\0\game_data.sav" `
    --mask-noise
```

### Changed offsets (slot 0 game_data.sav)

| Offset | Width | Pre | Post | Interpretation |
|---|---|---|---|---|
| `0x0894` | u32 | 118 | 18 | **`flower_coin` counter** (pair-region key `0xf4ee6827`). Δ −100 matches the shop price exactly. ✓ Same key + offset as the badge-purchase capture confirmed it. |
| `0x3480` | u32 | 0 | 1 | **★ Best wonder-seed grant-write candidate.** Single bit 0 set in an otherwise-zero u32. Followed immediately by a `0x51 = 81` u32 at `0x3484` (one of the structured-record threshold constants from the post-`0xbf0` region). Most likely an "ever-purchased shop wonder-seed #N" flag in a per-shop-slot bitfield. |
| `0x0010..0x0011` | u16 | `00 00` | `0c 40` (LE = `0x400c` = 16396) | **Unexplained header-block init.** Bytes 0x10..0x1f were entirely zero pre-capture; post-capture they hold a u32 `0x400c` at 0x10 and a u32 `0x14` (= 20) at 0x1c. Header (0x00..0x28) was previously assumed to be magic+version+length+padding; this contradicts that. Possibly a per-profile "first shop interaction" timestamp/nonce, or wonder-seed-purchase activation block. Needs a second-purchase capture to distinguish "one-time init" from "tally". |
| `0x001c` | u32 | 0 | 20 | Same header-block init region (see above). |
| `0x1690..0x1693` | u32 | `0x64248334` | `0xfbd064be` | Sits 4 bytes before the 16-byte 0x43c14d27-sentinel array we identified previously around `0x16b8`. This `0x1690` slot is structurally identical (4-byte hash, surrounded by 0x43c14d27 nulls) — most likely **another equip/recent-action hash slot** that the game touches whenever any shop interaction happens. Treat as save-metadata noise of the same family as `0x16b8`. Should be added to `KNOWN_METADATA_NOISE` once one more capture confirms it changes on unrelated saves too. |
| `0x50c8` | u16 | (masked) | (masked) | Known session-counter noise. |

### What we did *not* see

- **No pair-region (key,value) change for the seed counter** beyond
  flower_coin. The "16 → 17 wonder seeds" delta is not stored as a u32
  counter in either the first or second pair region. This strongly
  supports the theory that **the displayed Wonder Seed total is a
  popcount over per-acquisition flag bits** scattered across the
  trailing region (the `0x3480..` structured-record area).
- **No change in `0x0ea0`** (the badge-ownership bitfield) — confirms
  that field is exclusively badges, not a generic "items owned" mask.

### Implication for M3.3 grant code

Granting a Wonder Seed will require:

1. **Knowing which bit to set** for each individual seed source — the
   game appears to track each (course, seed-type) and each
   (shop, slot) acquisition as its own bit/flag. We have ONE confirmed
   flag so far: `0x3480` bit 0 = "first Poplin Shop wonder-seed slot
   purchased".
2. **Possibly also writing a recalc trigger** if the displayed total
   doesn't refresh until the engine recounts on next save/load.

To build the full per-seed bit map we need more captures across all
acquisition types: shop purchases (different slots), normal course
finish-pole seeds, mid-course Wonder Phase seeds, secret-exit seeds,
Top-of-Flag seeds. Each one likely lands in its own slot in the
trailing region's structured records.

### Open questions

- Is `0x0010..0x001f`'s sudden non-zero block a **one-time init on
  first wonder-seed shop purchase ever**, or does it tally something
  cumulative? A second shop-purchase capture answers this in one shot.
- Is the `0x1690+4` hash slot reliably noise, or does it carry meaning
  (e.g., "hash of last shop transaction")? Same second-capture answers
  it.

### Next concrete capture proposal

**Clear an uncleared normal course's flagpole (no Wonder Flower grab)
to earn exactly one "finish seed".** Reasons in priority order:

1. Most common in-game seed acquisition — the AP grant code will need
   to mirror this path far more often than shop purchases.
2. Cleanest single-flag delta (no flower coin spend, no shop state, no
   Wonder Phase noise).
3. Discriminator: tells us whether course-earned seed flags live in
   the same `0x3480..` byte range as shop-purchased seed flags or in
   a separate per-course block. Either answer is actionable.
4. As a bonus, the `0x0010..` header-block question gets a partial
   answer — if those bytes do NOT change on a non-shop seed acquire,
   the block is shop-specific, not seed-specific.

Capture protocol:
1. In Ryujinx, save & quit cleanly. Copy save folder to Desktop as
   `before-finish-seed`.
2. Resume. Enter any course that's never been cleared. Run STRAIGHT
   to the flagpole — do NOT touch the mid-course Wonder Flower, do
   NOT collect 10-coins, do NOT pick up power-ups (or at least
   don't change your power-up state by entering with the same form
   you exit with).
3. Touch the flagpole at the bottom (not Top-of-Flag) for the
   plain "course clear seed".
4. The course auto-saves. Quit to home menu.
5. Copy save folder to Desktop as `after-finish-seed`.

Then I'll diff and we'll know the structure of normal-play seed
storage.

## 2026-05-23 — Wonder Seed capture #2: clear-an-uncleared-level + Top-of-Flag

User cleared one previously-uncleared course, grabbed Top of Flag
(always grants a Wonder Seed when clearing an uncleared course, per
the user). Also picked up ~10 yellow coins during the run. No badge
state changed, no flower coin spend.

Run:

```pwsh
python scripts\savediff.py `
    "C:\Users\maxwe\Desktop\post-destroying-boulder\0\game_data.sav" `
    "C:\Users\maxwe\Desktop\post-level-clear\0\game_data.sav" `
    --mask-noise
```

### Changed offsets (slot 0 game_data.sav)

| Offset | Width | Pre | Post | Interpretation |
|---|---|---|---|---|
| `0x08a8` | u32 | 26 | 33 | **`regular_coin_count` confirmed for the 3rd capture** (pair key `0x17f0bb21`). Δ +7 matches "~10 yellow coins" reasonably (user estimate was rough). |
| `0x0d3d` | u8 | `0x08` | `0x09` | Bit 0 set in a packed bitfield record. Context shows `XX 20 40` pattern adjacent. |
| `0x0f22` | u8 | `0x08` | `0x18` | Bit 4 set in another packed bitfield record. Same `XX 08 20 40` neighbourhood. |
| `0x1249` | u8 | `0x00` | `0x01` | Bit 0 set in yet a third packed bitfield record (different sub-region). |
| `0x3360` | u32 | 0 | 1 | **★ Per-course flag bit flipped from 0 → 1 in a u32-array of per-course booleans.** Surrounding entries are mostly `0x00000001` (cleared) or `0x00000000` (not cleared). |
| `0x4408` | u32 | 0 | 1 | **★ A second per-course flag bit flipped from 0 → 1 in a parallel u32 array** 0x10A8 bytes after `0x3360`. Identical structure. |

### What we did *not* see (very informative)

- **`0x3480` did NOT change** (the shop-purchased seed flag from
  capture #1). Confirms **shop-purchased seed flags and course-earned
  seed flags live in completely separate byte ranges**.
- **`0x0010..0x001f` did NOT change**. Confirms that header-block
  init was **shop-interaction-specific**, not wonder-seed-acquisition-
  specific. Mystery partially resolved — that block activates on
  first shop transaction ever, not on first seed of any kind.
- **`0x1690+4` did NOT change**. Confirms that hash slot is also
  shop-interaction-related noise, not a general save-touch counter.
  Promote it to `KNOWN_METADATA_NOISE` only after we see it stay
  unchanged across another non-shop capture.
- **`0x0ea0` (badge bitfield) did NOT change**. Confirms its
  badges-only scope yet again.
- **No pair-region entry tracks the wonder seed total**. Strongest
  confirmation yet: **the displayed Wonder Seed total is a popcount
  over distributed flag bits**, not a stored counter.

### Five bits changed for one level clear — what each likely is

For a single uncleared-level-clear-with-Top-of-Flag the game flipped
5 distinct bits. The most plausible mapping:

| Bit | Likely meaning |
|---|---|
| `0x3360` u32 0→1 | "Course N cleared" flag (per-course u32 array #1) |
| `0x4408` u32 0→1 | "Course N — finish-pole wonder seed obtained" flag (per-course u32 array #2) |
| `0x0d3d` bit 0 | One of: "Top of Flag obtained for course N" / "any flagpole-touch achievement for N" / world-map state update |
| `0x0f22` bit 4 | Same family as above — another per-course / per-world progression bit |
| `0x1249` bit 0 | Same family — third packed bitfield in a different sub-region |

Each of `0x3360` and `0x4408` is in an obvious **u32-per-course array**
where adjacent slots are 0 or 1 — easy to grant by computing the
course index and writing 1 to the right u32. The three byte-level
bitfield changes (`0x0d3d`, `0x0f22`, `0x1249`) are in **packed
per-course bit arrays** where each course occupies one bit position
across multi-byte records.

### Implication for M3.3 grant code

Granting a "level-completion wonder seed" almost certainly requires
writing all five bits to keep the UI / world map consistent. The
straightforward path:

1. Capture before+after for 2-3 more course clears to pin down which
   of the five bits is "course cleared" vs "wonder seed earned" vs
   "Top of Flag" — each subsequent capture should hit DIFFERENT
   u32-array slots (for `0x3360` and `0x4408` regions) and DIFFERENT
   bit positions (for the three packed bitfields) because each course
   has its own index.
2. Once we have 3 captures, the structure is unambiguous: stride
   between courses in each region is fixed, so we can extrapolate to
   all ~70 courses.

### Confirmations from this capture

1. **`0x17f0bb21` = `regular_coin_count`** confirmed for the third
   time. Locked in.
2. **`flower_coin` did not change** — confirms that key only updates
   on flower-coin transactions, not on regular-coin pickups.
3. **Shop seeds and course-clear seeds are stored in completely
   different regions** — grant code will need two separate code paths.
4. **The Wonder Seed total is computed, not stored.** AP grants must
   write to per-acquisition flag bits and trust the game to recount.

### Next concrete capture proposal

**Clear a SECOND uncleared course, this time WITHOUT touching Top of
Flag (touch the bottom of the flagpole, or jump on the pole low so
you don't trigger Top-of-Flag).** Reasons:

1. **Isolates Top-of-Flag bit**: any of the 5 bits that does NOT flip
   this time is a Top-of-Flag-specific bit. The bits that DO flip
   (at different positions/indices) are the "level cleared" and
   "completion seed" bits. This is the cleanest possible
   discriminator for the 5-bit cluster we just found.
2. **Reveals the per-course stride**: comparing this capture's
   `0x3360`-array slot index and `0x4408`-array slot index to the
   previous capture's gives us the byte stride between adjacent
   courses (probably 4 bytes per course given they're u32 arrays).
   That extrapolates to all ~70 courses immediately.
3. **Same per-course course-id mapping** as the packed bitfields at
   `0x0d3d`/`0x0f22`/`0x1249` — bit positions / record indices
   should match the array slot index, giving us a unified per-course
   index across all five regions.

Capture protocol:
1. In Ryujinx, save & quit. Copy save folder to Desktop as
   `before-no-top-of-flag`.
2. Resume. Enter a DIFFERENT uncleared course (any one is fine).
   Run to flagpole. **Touch the bottom half of the flagpole — do
   NOT jump high enough to grab Top of Flag.** Goal: ordinary
   flagpole clear, awarded 1 finish-seed but no Top-of-Flag seed.
3. Avoid 10-coins, avoid Wonder Flower, avoid power-up changes
   if possible (mid-run coin pickups are tolerable noise; the
   pair-region change at `0x08a8` will just be a delta).
4. Auto-save fires on clear; quit to home menu.
5. Copy save folder to Desktop as `after-no-top-of-flag`.

If by accident Top-of-Flag fires, no problem — we'll still get the
per-course stride from comparing array slot positions. We just lose
the Top-of-Flag isolation; we can recover it on a third capture.

## 2026-05-23 — Wonder Seed capture #3: Badge Challenge "Parachute Cap 1" first clear, normal flag pole

User cleared the Badge Challenge "Parachute Cap 1" with a normal
flag-pole touch (no Top-of-Flag). BC was previously uncleared, so
this granted a "first clear" wonder seed. ~1 yellow coin gained
during the run.

Run:

```pwsh
python scripts\savediff.py `
    "C:\Users\maxwe\Desktop\pre_clear\0\game_data.sav" `
    "C:\Users\maxwe\Desktop\post_clear\0\game_data.sav" `
    --mask-noise
```

### Changed offsets (slot 0 game_data.sav)

| Offset | Width | Pre | Post | Interpretation |
|---|---|---|---|---|
| `0x08a8` | u32 | 34 | 35 | `regular_coin_count` (key `0x17f0bb21`) Δ +1. Matches one coin picked up in the BC. |
| `0x0cc0` | u32 | 4 (`0x4`) | 12 (`0xc`) | **Bit 3 set in a packed u32 bitfield** in the `0x0cb0..` structured-record region. Differs from capture #2's bit locations — likely a **Badge Challenge-specific** bitfield (capture #2 didn't touch this region at all). |
| `0x0d3e` | u8 | `0x20` | `0x30` | **Bit 20 set in the same shared u32 at `0x0d3c` that capture #2 touched.** Pre u32 `0x402009fc` → post u32 `0x403009fc`. Capture #2 set bit 8 in this same u32 (`0x402008fc` → `0x402009fc`); now this BC clear set bit 20. **This u32 is a shared per-course bitfield — every course-clear-with-seed sets its own bit here.** Strong shared marker, present in both course-type captures. |
| `0x3390` | u32 | 0 | 1 | **Per-course flag u32 array #1** — slot index 12 past capture #2's slot (`0x3390 - 0x3360 = 0x30 = 12 u32s`). Same parallel-array structure. |
| `0x4438` | u32 | 0 | 1 | **Per-course flag u32 array #2** — slot index 12 past capture #2's slot (`0x4438 - 0x4408 = 0x30 = 12 u32s`). Confirms both arrays grow in lockstep with the same per-course index. |
| `0x53ec` | 8 bytes | `1b e2 d3 a4 5c e1 c5 03` | `00 00 00 00 00 00 00 00` | **★★ NEW DISCOVERY: "Currently-active Badge Challenge" slot cleared on completion.** Preceded at `0x53e8` by u32 `0x10` (= 16). The 8 bytes look like a name-hash pair (u32 `0xa4d3e21b` + u32 `0x03c5e15c`) — almost certainly the **identity hash of Parachute Cap 1 BC** that was set on entry and wiped on clear. Mirrors the equipped-badge name-hash pattern at `0x16b8`/`0x1690`. |

### What we did *not* see

- **No pair-region change** for any wonder-seed counter. 4th
  confirmation — the displayed total is computed, never stored.
- **`0x3480` (shop-seed flag) did NOT change.** ✓ Shop vs course
  paths stay separate.
- **`0x0010..0x001f` (shop-init block) did NOT change.** ✓ Confirms
  it's shop-only.
- **`0x0ea0` (badge ownership) did NOT change.** Clearing a Badge
  Challenge does not grant a badge.
- **`0x0f22` and `0x1249` (capture #2's other bitfield bits) did
  NOT change.** These regions were not touched by this BC clear.

### Cross-capture analysis: which bits mean what

Comparing capture #2 (normal world course + Top-of-Flag) vs
capture #3 (Badge Challenge, normal pole):

| Region | Capture #2 (normal + ToF) | Capture #3 (BC, no ToF) | Inference |
|---|---|---|---|
| `0x3360..` u32 array | slot 0 flipped | slot 12 flipped | **Shared per-course array.** Stride between samples = 12 slots. |
| `0x4408..` u32 array | slot 0 flipped | slot 12 flipped | **Parallel per-course array.** Same index as `0x3360..`. |
| `0x0d3c` shared u32 bitfield | bit 8 set | bit 20 set | **Shared global "course cleared" bitmap.** Bit position = some per-course index (8 vs 20 = 12 apart, matching the array stride). |
| `0x0cb0..` region | (unchanged) | bit 3 set @ `0x0cc0` | **Course-type-specific bitfield** (only fires for BC, not for normal world course). |
| `0x0f22` bitfield | bit 4 set | (unchanged) | **Course-type or Top-of-Flag-specific.** Could be either; needs disambiguation. |
| `0x1249` bitfield | bit 0 set | (unchanged) | Same — Top-of-Flag or normal-world-course-specific. |
| `0x53ec` 8-byte slot | (unchanged) | hash cleared | **Badge-Challenge-specific** — "currently-active BC" pointer wiped on completion. |

**Strongest single takeaway:** the *consistent* signature of "I cleared
a course and earned a Wonder Seed" appears to be:
- 1 u32 0→1 in the `0x3360..` array
- 1 u32 0→1 in the `0x4408..` array
- 1 bit set in the shared `0x0d3c` bitfield

The other bits are course-type-specific decorations (Badge Challenge
state machine, Top-of-Flag flags, etc.).

### Per-course array indexing — partial map

Stride 12 between the normal-world-course and the Badge Challenge
suggests these arrays hold all courses flat in some canonical order,
with 12 slots between capture #2's course and capture #3's BC. SMBW's
internal course list almost certainly groups by category (Worlds 1-6,
Special World, Badge Challenges, etc.); we now have 2 of ~70 anchored
points.

### Implication for M3 grant code (refined)

To grant "one wonder seed for clearing course N", AP grant code needs
to write **three primary bits** (the shared signature) per course N:

```
write 1 to per_course_array_1[course_index_N]   // base ~0x3360
write 1 to per_course_array_2[course_index_N]   // base ~0x4408
set bit N in shared_clear_bitfield              // around 0x0d3c, but
                                                // bit base needs mapping
```

Plus possibly the course-type-specific decoration bits, but those may
be safely ignored if the game recomputes them from the primary three
on next save/load.

### Badge Challenge state machine (new system mapped)

`0x53e8` u32 (= 16 / `0x10`) + `0x53ec` 8-byte hash forms a
**(state, identity) pair** for the currently-active Badge Challenge.
When you enter a BC, the game writes the BC's identity hash to
`0x53ec`. When you clear it, the hash is wiped (zeroed). The constant
`0x10` u32 in front may be a "BC mode" enum marker.

This is the same family of structure as the equipped-badge hash at
`0x16b8`. SMBW seems to like 4-byte identity hashes for badges and
8-byte (or 2×4) for BCs.

**Practical use**: when implementing "in-game BC progress" status for
AP, we could read this slot to detect "player is currently in BC X".
Not needed for grants but useful for the bridge's state mirroring.

### Open questions remaining

1. **Are bits at `0x0f22` and `0x1249` Top-of-Flag-specific or
   normal-world-course-specific?** Need a normal-world-course clear
   WITHOUT Top-of-Flag to discriminate.
2. **What's the course-index encoding** in the per-course u32
   arrays? Adjacent W1 courses should be 1 slot apart (4 bytes).
   Cross-world boundaries may have gaps. Need one more
   normal-world-course capture to start measuring stride.
3. **Does the Wonder Seed total UI refresh automatically** when we
   write these bits at runtime, or do we need to also trigger a
   recount?

### Next concrete capture proposal (refined)

**Clear ANOTHER never-cleared normal world course WITHOUT touching
Top of Flag** (touch the bottom of the flagpole). This single
capture answers two of the open questions:

1. **Discriminates Top-of-Flag bits vs normal-course bits.** Compare
   to capture #2 (which had Top-of-Flag): any of capture #2's bits at
   `0x0f22` or `0x1249` that DON'T appear in this new capture =
   Top-of-Flag-specific. Any that DO appear = normal-course-specific.
2. **Measures the per-course stride** for normal world courses.
   Compare this capture's slot offset in the `0x3360..` and
   `0x4408..` arrays to capture #2's. If the new course is the very
   next W1 course, stride should be 1 slot (4 bytes). If it's
   further, we'll still learn the actual encoding.

Capture protocol:
1. Save & quit. Copy save folder to Desktop as
   `before-normal-no-tof`.
2. Resume. Enter any uncleared normal world course (W1, W2, etc. —
   ideally one in the same world as capture #2 to test adjacent-slot
   theory).
3. Run to flagpole. **Touch the bottom half only — do NOT grab
   Top-of-Flag.**
4. Auto-save fires. Quit to home menu.
5. Copy save folder to Desktop as `after-normal-no-tof`.

After that capture, we'll have a 3-point dataset that cleanly
factorizes (course-type axis) × (Top-of-Flag axis) for the bit
clusters and gives us enough structure to draft the M3.3 grant code.

## 2026-05-23 — External corroboration: MemetendoYT/SMBW-SaveGame-Editor

Discovered a third-party SMBW save editor on GitHub
([MemetendoYT/SMBW-SaveGame-Editor](https://github.com/MemetendoYT/SMBW-SaveGame-Editor))
that **independently confirms most of our findings** and unlocks
several offsets we hadn't captured yet. It's a small C# Windows Forms
editor with hardcoded offsets and byte-pattern anchors, no docs but
the constants are self-explanatory.

### 1:1 corroborations of our empirical findings

| Our finding | MemetendoYT confirms |
|---|---|
| `flower_coin` key = `0xf4ee6827` at pair @ `0x0890`, value at `0x0894` | `PURPLE_COINS_PATTERN = { 0x27, 0x68, 0xEE, 0xF4 }` (LE = `0xf4ee6827`). Value read as **u16** (low+high byte), max 0xFFFF — confirms it's a small counter, not u32. |
| `regular_coin_count` key = `0x17f0bb21` at pair @ `0x08a8`, value at `0x08ac` | `COINS_PATTERN = { 0x21, 0xBB, 0xF0, 0x17 }` (LE = `0x17f0bb21`). Value read as **u8** (single byte). |
| Capture #3 BC "Parachute Cap I" CourseClear at `0x4438` | Editor hardcodes `WriteCouseClearBadge(... 0x4438, "CourseClear")` — **EXACT match** for the array base. Our flag flip lands at slot 0 = Parachute Cap I is the first BC in W1 (matches user's run). |
| Capture #3 BC "Parachute Cap I" GoalSeed at `0x3390` | Editor hardcodes `WriteCouseClearBadge(... 0x3390, "GoalSeed")` — **EXACT match**. Slot 0 again. |
| Capture #2 normal-course CourseClear at `0x4408` | Editor's `WriteCouseClearNormal(... 0x43F0, "CourseClear")` — `0x4408 - 0x43F0 = 0x18 = 4 × 6`, so capture #2 was **W1 normal-course slot 6**. |
| Capture #2 normal-course GoalSeed at `0x3360` | Editor's `WriteCouseClearNormal(... 0x3348, "GoalSeed")` — `0x3360 - 0x3348 = 0x18 = 4 × 6`, same slot 6 in the parallel array. **Confirms our "parallel arrays use same index" theory.** ✓ |
| Stride between adjacent courses = 4 bytes / 1 u32 | Editor's `WriteCouseClear*` methods all do `offset += 4` per course. ✓ |
| Wonder Seed total is computed, not stored | Editor has no offset for a total counter — only per-course flags. ✓ |

### Per-course array offset table (mostly from MemetendoYT, validated against our captures)

Each region is a flat u32-per-course array. The course-type sub-arrays
appear in distinct offset ranges:

| Region base | Per-course-type | Field | Notes |
|---|---|---|---|
| `0x0CD3` | Palace | `ClapperGate` | Palace boss-room gate state. Capture #3 hit nearby (`0x0CC0`) — different sub-region but same neighbourhood. |
| `0x1718` | Normal courses | `PurpleCoin` (10-coin bitmask?) | M2.2 ten-coin tracking probably lives here. |
| `0x1760` | Badge Challenges | `PurpleCoin` | (BCs only have 1 purple coin each.) |
| `0x1788` | Palace | `PurpleCoin` | |
| `0x17B0` | KO Arena | `PurpleCoin` | |
| `0x33E0` | KO Arena | `GoalSeed` | |
| `0x3348` | Normal courses | `GoalSeed` | ★ capture #2 slot 6 |
| `0x3390` | Badge Challenges | `GoalSeed` | ★ capture #3 slot 0 (EXACT) |
| `0x3408` | Break Time | `GoalSeed` | |
| `0x3AF8` | Normal courses | `WonderSeed` (mid-course Wonder Phase) | Distinct from `GoalSeed` — we haven't captured a Wonder Phase yet. |
| `0x43F0` | Normal courses | `CourseClear` | ★ capture #2 slot 6 |
| `0x4438` | Badge Challenges | `CourseClear` | ★ capture #3 slot 0 (EXACT) |
| `0x4460` | Palace | `CourseClear` | |
| `0x4488` | KO Arena | `CourseClear` | |
| `0x44B0` | Break Time | `CourseClear` | |
| `0x167C` | (single u8) | `Lives` | Player lives counter. |

The W1 region only covers W1 — the editor doesn't yet handle W2-W6.
But the structure pattern is clear: each world has its own block of
per-course-type arrays at fixed-ish offsets, with 4-byte stride.

### New keys we now know about (32-bit hash anchors, scanned via pattern)

These are pair-region keys (like flower_coin, regular_coin) found by
**byte-pattern search anywhere in the buffer**, not at fixed offsets.
Their values live at `key_offset + 4`.

| Key (LE u32) | MemetendoYT name | Likely meaning |
|---|---|---|
| `0x55815859` | `GRAND_SEED_WORLD1` | World 1 Royal/Grand Seed (Bowser Jr. palace clear) |
| `0x49abba86` | `GRAND_SEED_WORLD2` | World 2 Royal Seed |
| `0xb550d8d6` | `GRAND_SEED_WORLD3` | World 3 Royal Seed |
| `0x1dcf7f6e` | `GRAND_SEED_WORLD4` | World 4 Royal Seed |
| `0x0d5a3e00` | `GRAND_SEED_WORLD5` | World 5 Royal Seed |
| `0xd4660d2b` | `GRAND_SEED_WORLD6` | World 6 Royal Seed |
| `0x5d3ec9b4` | `COMPLETE_GAME` | Game-complete flag |
| `0x89f1cc52` | `INTRO_CUTSCENE_COMPLETED` | Intro cutscene watched flag |

**M3.3b Royal Seed mapping is now answered without needing a capture.**
We can grant a Royal Seed by setting `value=1` at the address found
by scanning for `GRAND_SEED_WORLD{N}`'s bytes — same pattern as
flower_coin.

User's current pair-region `--summary` (from save-diff-grants.md
header note: 16 set keys in the first table) likely includes some of
these — we can cross-reference offline.

### What MemetendoYT does NOT cover (gaps we still need to map empirically)

- **Badges**. The editor has no badge offsets at all. Our M3.2
  work at `0x0ea0` (ownership) and `0x16b8` (equipped) remains the
  only mapping for those.
- **Shop-purchased Wonder Seed flag** at `0x3480`. The editor doesn't
  expose this. Our capture #1 finding is unique.
- **Worlds 2–6 per-course arrays**. Editor is W1-only. Same
  structure presumably repeats; offsets will differ.
- **The shared `0x0d3c` u32 bitfield** we saw flip bits 8 and 20.
  Editor doesn't touch it. May be a derived popcount field that the
  game recomputes on next load — testable by writing `CourseClear`
  bits without touching `0x0d3c` and seeing if the game still
  recognizes the clear.
- **The Badge-Challenge "currently-active" 8-byte hash at `0x53e8/0x53ec`**.
  Editor doesn't expose this either. Our capture #3 finding is unique.
- **The shop-init header block at `0x0010..0x001f`**. Capture #1 only.

### Implications for M3 grant code (heavily refined)

We can now draft **byte-accurate grant code** for the majority of M3
items, without needing more captures for the well-mapped paths:

1. **Grant flower coins** — find `0xf4ee6827` pattern, write value at
   `+4` as u16.
2. **Grant regular coins / lives** — same anchor pattern for
   `0x17f0bb21`, write u8; lives at fixed `0x167C` u8.
3. **Grant Royal Seed N** — find `GRAND_SEED_WORLDN` pattern, set
   value at `+4` to 1.
4. **Grant course clear for W1 course slot N** —
   - Set u32 at `0x43F0 + 4*N` (normal), `0x4438 + 4*N` (BC),
     `0x4460 + 4*N` (Palace), `0x4488 + 4*N` (Arena), or
     `0x44B0 + 4*N` (Break Time) to 1.
   - Mirror at the `GoalSeed` array (`0x3348/0x3390/.../0x3408 + 4*N`)
     for the implicit wonder seed.
   - Possibly also mirror at `0x3AF8 + 4*N` for the Wonder-Phase seed.
   - Possibly also set the corresponding bit in the shared `0x0d3c`
     u32 bitfield — **testable: try writing without this and see if
     game accepts.**
5. **Grant badges** — still requires our `0x0ea0` empirical mapping
   from M3.2 work. MemetendoYT doesn't help here.

The "currently-active BC" `0x53e8/0x53ec` slot and the shop-init block
at `0x0010..0x001f` aren't needed for grants — they're state-machine
slots the game manages itself.

### Revised next-capture priority

The corroboration changes my recommendation. Previously: clear another
normal course without Top-of-Flag, to disambiguate bit clusters. Now:

**Capture a Royal Seed grant (palace clear) to verify the
`GRAND_SEED_WORLD{N}` pair-key pattern matches MemetendoYT's table.**

Why this is now the highest-value next capture:
1. **Validates the entire "MemetendoYT's byte-patterns map to our
   pair-region keys" theory in one shot.** If we capture a palace
   clear and find that the value associated with one of those six
   key patterns flipped 0 → 1, MemetendoYT's table is fully cross-
   verified. That gives us 8 new keys (6 Royal Seeds + game-complete
   + intro) for free.
2. **Maps M3.3b Royal Seeds directly** — was an outstanding capture.
3. **Tests the shared `0x0d3c` bitfield hypothesis** — if a Royal
   Seed event flips a bit there, then `0x0d3c` is a course-completion
   bitmap that mirrors per-course arrays. If it doesn't, `0x0d3c` is
   normal-course-and-BC-specific.
4. Royal Seeds were already on the M3.3b roadmap.

Capture protocol:
1. Save & quit. Copy save folder to Desktop as `before-royal-seed`.
2. Beat any palace boss you haven't yet (W2-W6 since W1 royal seed
   is already in our save per the M2.4 PlayReport corpus). Avoid
   collecting badges, flower coins beyond the boss room, or seeds.
3. The palace auto-saves after the boss. Quit to home menu.
4. Copy save folder to Desktop as `after-royal-seed`.

**Alternative** (if you prefer): clear another normal course without
Top-of-Flag (original plan #2) — still valuable for disambiguating
the byte-level bitfield bits, but lower marginal value now that the
per-course arrays are mapped.

## 2026-05-23 — Trust-but-verify log: taking MemetendoYT at face value

**Decision (2026-05-23):** rather than spend a capture cycle on Royal
Seeds to cross-verify MemetendoYT's pair-key table, treat the table as
correct and move on to grant-code work. The two pair keys MemetendoYT
documents that we independently confirmed (`COINS_PATTERN`,
`PURPLE_COINS_PATTERN`) and the four per-course array offsets we
independently confirmed (capture #2 slot 6 in normal arrays, capture
#3 slot 0 in BC arrays) give us 6-for-6 corroboration with zero
mismatches. That's enough confidence to proceed.

**Re-verification trigger conditions** — if any of these happen, come
back and run the Royal-Seed capture (and possibly more):
- A grant write to a MemetendoYT-derived offset has no in-game effect
  after save/reload.
- A grant write corrupts unrelated state (e.g. setting `GRAND_SEED_WORLD2`
  also clears badges, breaks the shop, etc.).
- The shared `0x0d3c` bitfield hypothesis fails in practice (we write
  the primary three flag bits per course and the game refuses to mark
  the course "cleared" on next load).
- Worlds 2-6 per-course array offsets, when we extrapolate them, land
  on already-known fields (collision = our extrapolation is wrong, or
  MemetendoYT's W1 base offsets were noisy).

If any of those fire, the cheapest re-verification is:
1. `python scripts/savediff.py --summary <current save>` to list all
   non-zero pair-region entries.
2. Check which of MemetendoYT's 8 pair-key patterns are present, with
   what values, and whether they make sense for the player's state
   (e.g. `INTRO_CUTSCENE_COMPLETED` should always be 1 once past
   intro; `GRAND_SEED_WORLD1` should be 1 if the player has 1+ royal
   seed in W1).
3. If pair-keys don't validate, do the Royal Seed capture
   (`before-royal-seed` / `after-royal-seed`) to ground-truth them.

## 2026-05-23 — Pair-key sanity check against existing save

Ran `savediff.py --summary` on the user's current `game_data.sav` to
verify MemetendoYT's pair-key patterns are present at the expected
positions.

Command:
```pwsh
python scripts\savediff.py --summary `
    "C:\Users\maxwe\Desktop\post_clear\0\game_data.sav"
```

**Result: all 8 MemetendoYT pair keys present with sane values.** Even
better — there are no false positives or missing keys.

| MemetendoYT key | Found at | Value | Interpretation |
|---|---|---|---|
| `0x5d3ec9b4` `COMPLETE_GAME` | pair 3 @ `0x0040` | 0 | ✓ game not complete |
| `0x89f1cc52` `INTRO_CUTSCENE_COMPLETED` | pair 32 @ `0x0128` | 1 | ✓ intro watched |
| `0x55815859` `GRAND_SEED_WORLD1` | pair 101 @ `0x0350` | **1** | ✓ user has 1 royal seed in W1 |
| `0x49abba86` `GRAND_SEED_WORLD2` | pair 7 @ `0x0060` | 0 | ✓ |
| `0xb550d8d6` `GRAND_SEED_WORLD3` | pair 107 @ `0x0380` | 0 | ✓ |
| `0x1dcf7f6e` `GRAND_SEED_WORLD4` | pair 57 @ `0x01f0` | 0 | ✓ |
| `0x0d5a3e00` `GRAND_SEED_WORLD5` | pair 104 @ `0x0368` | 0 | ✓ |
| `0xd4660d2b` `GRAND_SEED_WORLD6` | pair 18 @ `0x00b8` | 0 | ✓ |
| `0xf4ee6827` flower_coin (ours) | pair 269 @ `0x0890` | 8 | ✓ user's current purple coins |
| `0x17f0bb21` regular_coin (ours) | pair 272 @ `0x08a8` | 35 | ✓ matches capture #3 post-value |

**Verdict: MemetendoYT's table is fully cross-verified.** The Royal
Seed capture is no longer needed for validation. We can proceed to
grant-code work with high confidence.

### Bonus observations from the summary dump

The full pair-region structure is now clearer:

- **Pairs 0..127** (file `0x0028..0x0428`): the "first table" — flags
  and boolean-ish counters keyed by hash. Sparse, mostly 0/1 values.
  Royal Seeds, intro flag, game-complete flag, etc. live here.
- **Pair 128** is a special `(key=0, value=1)` sentinel at `0x0428` —
  it marks the start of a different section.
- **Pairs 128 onwards**: alternating sections, each opened by a
  `(key=0, value=section_id)` sentinel (section IDs 1..32 visible),
  followed by either:
  - **counter sections** (e.g., flower_coin and regular_coin around
    pairs 269 and 272), or
  - **blob-offset sections** where values are monotonically
    increasing u32 offsets into the post-`0xbf0` blob.
- Notably `0xf4ee6827` (flower_coin) and `0x17f0bb21` (regular_coin)
  live in section 3 (between sentinels at pair 279 and 284). They are
  NOT in the first table. The diff tool's earlier scoping of the
  whole pair region was correct.
- Pair 271 has `key=0x1779f32f, value=0xffffffff` — a sentinel "no
  value yet" marker for something the player hasn't done.

### Decision: proceed to grant-code work

With MemetendoYT validated end-to-end and our M3.2 badge mapping
intact, the save-diff sprint has done its job. The map we have is
complete enough to start writing actual grant code. Specifically we
now know byte-exact write targets for:

- Flower coins (u16, scan for `0xf4ee6827` key, value at `+4`)
- Regular coins (u8, scan for `0x17f0bb21` key, value at `+4`)
- All 6 Royal Seeds (set value to 1 next to `GRAND_SEED_WORLD{N}` key)
- Game-complete flag and intro flag (same pattern)
- Lives (u8 at `0x167C`)
- W1 per-course CourseClear/GoalSeed/WonderSeed/PurpleCoin/ClapperGate
  flags at known base offsets with 4-byte stride
- Badge ownership (u64 at `0x0ea0`) — from our M3.2 work
- Shop-purchased wonder seed flags (`0x3480`+) — from our M3.3 work

What we still need before code lands:

1. **Runtime save-buffer address anchor** — the bridge from
   save-file byte offsets to live in-memory addresses. Without it,
   we can read MemetendoYT's table all day and still can't write a
   single byte into the running game.
2. **One MVP grant proven end-to-end** — so we know our write target
   is right, our timing is right, and the game's UI/state refreshes.
3. **W2-W6 per-course array offsets** — eventually, but not blocking
   the MVP. W1 alone covers enough courses for first-grant validation.

### Next logical step (recommendation)

**Find the in-memory address of the save buffer in a running Ryujinx
session, using the `savedata_id` UUID as a unique scan anchor.**

Why this is THE next step:
- It's the precondition for every other grant. No write happens
  without it.
- The anchor is uniquely identifiable: the UUID
  `b813e675-eb254c8a-a3e0d052-df1afad0` (already known from M2.4
  PlayReport corpus) is at save-buffer file offset `0x50b8`. Subtract
  to get the base.
- We have two paths, in increasing complexity:

  **(a) Cheat Engine scan in Ryujinx (fastest, 5–15 min).** Boot the
  game, scan the process memory for the literal UUID bytes
  (`b8 13 e6 75 eb 25 4c 8a a3 e0 d0 52 df 1a fa d0`), note the
  address. `save_buffer_base = address - 0x50b8`. Cross-check by
  reading the magic `0x01020304` at base + 0. Cross-check by reading
  the flower_coin u16 at `base + 0x894` and confirming it matches
  the in-game purple coin counter.

  **(b) Subsdk one-shot scanner (durable, 30–60 min).** A C++
  function in `main.cpp` that scans the heap for the UUID on first
  frame, logs the resolved `save_buffer_base`, and stores it in a
  static variable. Mirrors the approach in
  [docs/save-diff-grants.md](save-diff-grants.md) Step 4. This is
  what we'll eventually ship anyway.

I'd start with (a) for a 15-min validation that the anchor works at
all, then promote it to (b) for production. Once `save_buffer_base`
is known, a first MVP grant (e.g. `flower_coin += 99` on a button
press) takes maybe a 20-line hook in `main.cpp` and proves the entire
pipeline.

**Followup after MVP grant lands**: extrapolate the W2-W6 per-course
array offsets by capturing one course clear in each world (5 quick
captures, mechanical) and confirming the stride/pattern from W1
holds.

## 2026-05-23 — Runtime anchor validated in Ryujinx (Cheat Engine)

Cheat Engine scan for the 16-byte savedata_id UUID (LE form
`75 E6 13 B8 8A 4C 25 EB 52 D0 E0 A3 D0 FA 1A DF`) on attached
`Ryujinx.exe` returned **2 matches**:

| Match | UUID address | Computed BASE | Bytes immediately after UUID | Identity |
|---|---|---|---|---|
| #1 | `0x1E9BCCEC480` | `0x1E9BCCE73C8` | `53 14 00 00 00 00 00 10` | **Live save buffer (profile 0)** ✓ |
| #2 | `0x1E9C0865260` | `0x1E9C08601A8` | `26 12 00 00 ...` (different session counter) | Profile 1 or working/cache copy |

Both matches sit in the same Ryujinx heap allocation block
(AllocationBase `0x1E9ABA80000`, Base `0x1E9BCCE7000`, Size
`0x5633000` = 86 MB — Ryujinx's guest-memory mapping).

### Critical architectural finding

Memory at `save_buffer_base + 0` does **NOT** match file offset 0
(no `04 03 02 01` magic). The bytes are `01 00 00 00 78 01 ...`
(file offset 8's `0xBF0` length field has a different value in
memory). This pattern doesn't appear anywhere in the on-disk save
file.

**Interpretation: the save is split into two regions in memory.**

| File offsets | Layout in memory | Access path |
|---|---|---|
| `0x000..0xBF0` (header + pair region) | Hash table at a separate address (the M3.3 container at guest `0x20D3DA07A8`) | Indirect — write via hash-table API or by walking the table |
| `0xBF0..end` (trailing structured-records region) | Contiguous flat buffer matching the file byte-for-byte, starting at `save_buffer_base` | Direct — `save_buffer_base + file_offset` |

This matches our existing M3.3 evidence: the container at
`0x20D3DA07A8` holds the hash-keyed pairs (flower_coin, regular_coin,
Royal Seeds). The on-disk pair region is just the serialized form of
that hash table, not a flat memory copy.

### Cross-checks against Match 1 — all passed

| Check | Address | Expected | Result |
|---|---|---|---|
| Badges u64 at `BASE + 0x0EA0` | `0x1E9BCCE8268` | `00 02 00 00 0C 40 00 00` (= bits 9/34/35/45/46 = 5 owned badges) | ✓ |
| BC Parachute Cap I clear flag at `BASE + 0x4438` | `0x1E9BCCEB800` | u32 = 1 (from capture #3) | ✓ |
| Normal-course slot 6 clear flag at `BASE + 0x4408` | `0x1E9BCCEB7D0` | u32 = 1 (from capture #2) | ✓ |

**Verdict: the trailing-region anchor works. All grants targeting
file offsets ≥ 0xBF0 can use the formula
`live_addr = save_buffer_base + file_offset`.**

### What's directly writable via the trailing-region anchor

- Badges (ownership at `0x0EA0`, equipped at `0x16B8`)
- Lives (`0x167C`)
- Per-course CourseClear arrays (`0x43F0` normal / `0x4438` BC /
  `0x4460` Palace / `0x4488` Arena / `0x44B0` Break Time)
- Per-course GoalSeed arrays (`0x3348` / `0x3390` / `0x33E0` / `0x3408`)
- Per-course WonderSeed array (`0x3AF8` normal)
- Per-course PurpleCoin arrays (`0x1718` / `0x1760` / `0x1788` /
  `0x17B0`)
- Per-course ClapperGate (`0x0CD3` Palace)
- Shop-purchased wonder seed flag (`0x3480`)
- BC currently-active hash (`0x53E8/0x53EC`)
- The shared course-clear bitfield at `0x0D3C`

### What still needs the hash-table path

- Flower coins (`0xf4ee6827`)
- Regular coins (`0x17f0bb21`)
- All 6 Royal Seeds (`GRAND_SEED_WORLD{N}`)
- COMPLETE_GAME flag
- INTRO_CUTSCENE_COMPLETED flag

These can be deferred for the first MVP grant; the trailing-region
items alone cover M3.2 (badges), most of M3.3 (per-course seed
flags), and the per-course clears needed for AP check-detection.
