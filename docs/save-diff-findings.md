# Save-diff findings — growing notes

Append-only log of what each capture told us. Capture protocol per item
is in [save-diff-grants.md](save-diff-grants.md).

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
