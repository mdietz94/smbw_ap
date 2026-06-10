"""Sanity tests for badge_table + location_table.

Cross-checks every shipped entry against the manual apworld's
locations.json / items.json to catch typos before they hit the wire.
"""

from __future__ import annotations

import json
import os
import unittest

from ..badge_table import (
    _BADGES,
    all_badge_item_names,
    grant_internal_id_for_item,
    is_badge_item,
    mapped_bits,
    name_for_internal_id,
    next_unmapped_bit,
    unmapped_item_names,
)
from ..coin_table import _COIN_ITEMS, grant_for_item, is_coin_item
from ..location_table import (
    _SHOP_SEED_TABLE,
    _SHOP_SEED_WILDCARD_TABLE,
    _TABLE,
    _TEN_COIN_TABLE,
    lookup_name,
    pr_world_no_to_ap_world,
)
from ..protocol import CheckEmitted, CheckKind
from ..royal_seed_table import (
    ALL_MASK,
    ROYAL_SEED_HASHES,
    ROYAL_SEED_VALUE,
    WORLD_COUNT as ROYAL_SEED_WORLD_COUNT,
    _ROYAL_SEEDS,
    bit_for_item,
    hash_for_item,
    is_royal_seed_item,
    palace_stage_key_for_item,
)
from ..world_unlock_table import (
    WORLD_UNLOCK_BOOL_HASHES,
    WORLD_UNLOCK_HASHES,
    WORLD_UNLOCK_INT_HASHES,
)


# tests/ -> client/ -> smbw_archipelago/ ; data lives at smbw_archipelago/data/.
_DATA_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "data"))
_LOCATIONS_JSON = os.path.join(_DATA_ROOT, "locations.json")
_ITEMS_JSON = os.path.join(_DATA_ROOT, "items.json")


def _load_names(path: str) -> set[str]:
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)
    return {e["name"] for e in entries if "name" in e}


class TestBadgeTable(unittest.TestCase):

    def test_every_badge_name_exists_in_items_json(self):
        item_names = _load_names(_ITEMS_JSON)
        missing = [name for name, _, _ in _BADGES if name not in item_names]
        self.assertEqual(missing, [], f"badges not found in items.json: {missing}")

    def test_internal_ids_in_range(self):
        for name, bit, _ in _BADGES:
            self.assertTrue(
                0 <= bit < 64,
                f"{name}: internal_id {bit} out of [0, 64)")

    def test_no_duplicate_ids(self):
        seen: dict[int, str] = {}
        for name, bit, _ in _BADGES:
            self.assertNotIn(
                bit, seen,
                f"duplicate internal_id={bit}: {seen.get(bit)!r} and {name!r}")
            seen[bit] = name

    def test_spring_feet_is_bit_4(self):
        self.assertEqual(grant_internal_id_for_item("Spring Feet Badge"), 4)

    def test_unknown_item_returns_none(self):
        self.assertIsNone(grant_internal_id_for_item("Imaginary Mushroom"))

    def test_is_badge_item(self):
        self.assertTrue(is_badge_item("Spring Feet Badge"))
        self.assertFalse(is_badge_item("Filler"))

    def test_name_for_internal_id_spring_feet(self):
        self.assertEqual(name_for_internal_id(4), "Spring Feet Badge")

    def test_name_for_internal_id_unknown_returns_none(self):
        self.assertIsNone(name_for_internal_id(999))

    def test_reverse_lookup_round_trips(self):
        for name, bit, _ in _BADGES:
            self.assertEqual(name_for_internal_id(bit), name)
            self.assertEqual(grant_internal_id_for_item(name), bit)

    # ---- M2.3 probe-and-discover helpers --------------------------

    def test_mapped_bits_matches_badges(self):
        self.assertEqual(mapped_bits(), {bit for _, bit, _ in _BADGES})

    def test_all_badge_item_names_count_is_24(self):
        # The apworld currently ships exactly 24 badge AP items.  If
        # this ever changes the M2.3 probe loop's "you've mapped N of M"
        # status line needs no change -- the assertion just protects
        # against a silent rename that would break the discovery flow.
        self.assertEqual(len(all_badge_item_names()), 24)

    def test_unmapped_item_names_excludes_mapped(self):
        mapped = {n for n, _, _ in _BADGES}
        for n in unmapped_item_names():
            self.assertNotIn(n, mapped)

    def test_all_24_badges_mapped(self):
        """M2.3 completion lock: every badge in items.json has an
        internal_id mapping in _BADGES.  This must NEVER regress -- if
        you're adding a new badge to items.json, also add its bit to
        _BADGES (probe via /badge_probe to find it).  If you're
        removing a badge, drop the matching _BADGES row in the same
        commit."""
        self.assertEqual(
            unmapped_item_names(), [],
            "Some badges in items.json have no internal_id mapping. "
            "Run the client + /badge_probe_next to fill them in, "
            "then add the (name, bit, 'probe') tuple to _BADGES.")

    def test_unmapped_plus_mapped_equals_all(self):
        all_names = set(all_badge_item_names())
        mapped = {n for n, _, _ in _BADGES if n in all_names}
        self.assertEqual(set(unmapped_item_names()) | mapped, all_names)

    def test_next_unmapped_bit_from_start(self):
        # Returns the lowest unmapped bit position; this is whatever's
        # not in mapped_bits().  Don't pin to a specific value -- the
        # table grows as the probe loop fills in mappings; just assert
        # the result is consistent with mapped_bits().
        nxt = next_unmapped_bit(after=-1)
        self.assertIsNotNone(nxt)
        self.assertNotIn(nxt, mapped_bits())
        # And it should be <= the smallest gap, i.e. lower than every
        # unmapped value > nxt.
        self.assertEqual(nxt, min(set(range(64)) - mapped_bits()))

    def test_next_unmapped_bit_skips_mapped(self):
        # Walking from -1 should eventually skip the 3 mapped bits.
        seen: list[int] = []
        cur = -1
        for _ in range(64):
            cur_opt = next_unmapped_bit(after=cur)
            if cur_opt is None:
                break
            seen.append(cur_opt)
            cur = cur_opt
        for bit in mapped_bits():
            self.assertNotIn(bit, seen)

    def test_next_unmapped_bit_exhausts(self):
        # After ceiling there are no more bits.
        self.assertIsNone(next_unmapped_bit(after=63))
        self.assertIsNone(next_unmapped_bit(after=100))

    def test_next_unmapped_bit_honors_extra_skip(self):
        # `/badge_probe_invalid` adds bits to a runtime skip set; the
        # iterator must skip both _BADGES bits AND the invalid set.
        # Build a skip set that covers every bit between two specific
        # bits, then assert the returned bit isn't in either skip set.
        skip = {1, 2, 3, 5, 6, 7}
        nxt = next_unmapped_bit(after=0, extra_skip=skip)
        self.assertIsNotNone(nxt)
        self.assertNotIn(nxt, skip)
        self.assertNotIn(nxt, mapped_bits())
        # Walk the whole space with a big skip; should still terminate
        # at some valid bit (or return None if every unmapped bit is
        # also skipped).
        big_skip = set(range(40)) - mapped_bits()  # nuke bits 0..39
        nxt2 = next_unmapped_bit(after=-1, extra_skip=big_skip)
        if nxt2 is not None:
            self.assertGreaterEqual(nxt2, 40)
            self.assertNotIn(nxt2, mapped_bits())

    def test_next_unmapped_bit_extra_skip_none_equivalent_to_empty(self):
        self.assertEqual(
            next_unmapped_bit(after=-1, extra_skip=None),
            next_unmapped_bit(after=-1, extra_skip=set()))


class TestLocationTable(unittest.TestCase):

    def test_every_location_name_exists_in_locations_json(self):
        loc_names = _load_names(_LOCATIONS_JSON)
        missing = [n for n in _TABLE.values() if n not in loc_names]
        self.assertEqual(missing, [], f"locations not found in locations.json: {missing}")

    def test_every_ten_coin_name_exists_in_locations_json(self):
        loc_names = _load_names(_LOCATIONS_JSON)
        missing = [n for n in _TEN_COIN_TABLE.values() if n not in loc_names]
        self.assertEqual(
            missing, [],
            f"ten_coin locations not found in locations.json: {missing}")

    def test_lookup_known_entry(self):
        check = CheckEmitted(
            kind=CheckKind.WONDER_SEED, stage_key=2937190396)
        self.assertEqual(
            lookup_name(check),
            "W1: Welcome to the Flower Kingdom! - Wonder Seed")

    def test_lookup_unknown_returns_none(self):
        check = CheckEmitted(kind=CheckKind.WONDER_SEED, stage_key=99999999)
        self.assertIsNone(lookup_name(check))

    def test_lookup_ten_coin_returns_indexed_name(self):
        # Index 0 → #1, index 2 → #3 (blind mapping per m2.2-runbook).
        for idx, expected in enumerate([
            "W1: Welcome to the Flower Kingdom! - 10 Coin #1",
            "W1: Welcome to the Flower Kingdom! - 10 Coin #2",
            "W1: Welcome to the Flower Kingdom! - 10 Coin #3",
        ]):
            check = CheckEmitted(
                kind=CheckKind.TEN_COIN,
                stage_key=2937190396,
                metadata={"coin_index": idx})
            self.assertEqual(lookup_name(check), expected)

    def test_lookup_ten_coin_unknown_stage_returns_none(self):
        check = CheckEmitted(
            kind=CheckKind.TEN_COIN,
            stage_key=99999999,
            metadata={"coin_index": 0})
        self.assertIsNone(lookup_name(check))

    def test_every_shop_seed_name_exists_in_locations_json(self):
        loc_names = _load_names(_LOCATIONS_JSON)
        missing = [n for n in _SHOP_SEED_TABLE.values() if n not in loc_names]
        self.assertEqual(
            missing, [],
            f"shop_seed locations not found in locations.json: {missing}")

    def test_every_shop_seed_wildcard_name_exists_in_locations_json(self):
        loc_names = _load_names(_LOCATIONS_JSON)
        missing = [
            n for n in _SHOP_SEED_WILDCARD_TABLE.values()
            if n not in loc_names]
        self.assertEqual(
            missing, [],
            f"shop_seed wildcard locations not found in locations.json: {missing}")

    def test_lookup_shop_seed_pi_west_via_wildcard(self):
        # PI East is exact at (2, 4, 0); any OTHER npc_id in PI must be
        # West by elimination (houses don't fire general_shop_result).
        # Try a few hypothetical npc_ids that aren't 4.
        for fake_npc in [1, 2, 3, 5, 99]:
            check = CheckEmitted(
                kind=CheckKind.SHOP_SEED,
                stage_key=(2 << 16) | fake_npc,
                metadata={"world_no": 2, "npc_id": fake_npc, "item_value": 0})
            self.assertEqual(
                lookup_name(check),
                "PI: Poplin Shop (West) - Wonder Seed",
                f"wildcard didn't match for npc_id={fake_npc}")

    def test_lookup_shop_seed_pi_east_exact_wins_over_wildcard(self):
        # Exact (2, 4, 0) → East, NOT the PI West wildcard.  Wildcard
        # is only consulted on exact-table miss.
        check = CheckEmitted(
            kind=CheckKind.SHOP_SEED,
            stage_key=(2 << 16) | 4,
            metadata={"world_no": 2, "npc_id": 4, "item_value": 0})
        self.assertEqual(
            lookup_name(check),
            "PI: Poplin Shop (East) - Wonder Seed")

    def test_lookup_shop_seed_w5_via_wildcard(self):
        # W5 has exactly one shop in the apworld (its 2 Houses don't
        # fire general_shop_result), so any general_shop_result in
        # PR world_no=6 is the W5 Poplin Shop regardless of npc_id.
        check = CheckEmitted(
            kind=CheckKind.SHOP_SEED,
            stage_key=(6 << 16) | 42,
            metadata={"world_no": 6, "npc_id": 42, "item_value": 0})
        self.assertEqual(
            lookup_name(check),
            "W5: Poplin Shop - Wonder Seed")

    def test_lookup_shop_seed_w6_via_wildcard(self):
        check = CheckEmitted(
            kind=CheckKind.SHOP_SEED,
            stage_key=(7 << 16) | 1,
            metadata={"world_no": 7, "npc_id": 1, "item_value": 0})
        self.assertEqual(
            lookup_name(check),
            "W6: Poplin Shop - Wonder Seed")

    def test_lookup_shop_seed_w1_poplin(self):
        # W1 Poplin Shop: PR world_no=1, npc_id=2, slot=0.
        check = CheckEmitted(
            kind=CheckKind.SHOP_SEED,
            stage_key=(1 << 16) | 2,
            metadata={"world_no": 1, "npc_id": 2, "item_value": 0})
        self.assertEqual(
            lookup_name(check),
            "W1: Poplin Shop - Wonder Seed")

    def test_lookup_shop_seed_w2_poplin_bottom(self):
        # W2 Poplin Shop (Bottom): the apworld's "W2" label is PR
        # world_no=3 because Petal Isles takes the world_no=2 slot
        # in the PlayReport numbering.  npc_id=4 captured 2026-05-25.
        check = CheckEmitted(
            kind=CheckKind.SHOP_SEED,
            stage_key=(3 << 16) | 4,
            metadata={"world_no": 3, "npc_id": 4, "item_value": 0})
        self.assertEqual(
            lookup_name(check),
            "W2: Poplin Shop (Bottom) - Wonder Seed")

    def test_lookup_shop_seed_w4_secret_triple(self):
        # W4 Poplin Shop (Secret): same (world_no=5, npc_id=5) for all
        # three priced seeds; item_value 0/1/2 maps to the 30/100/200
        # AP locations in shop-shelf order.
        for slot, expected in enumerate([
            "W4: Poplin Shop (Secret) - Wonder Seed (30 Coins)",
            "W4: Poplin Shop (Secret) - Wonder Seed (100 Coins)",
            "W4: Poplin Shop (Secret) - Wonder Seed (200 Coins)",
        ]):
            check = CheckEmitted(
                kind=CheckKind.SHOP_SEED,
                stage_key=(5 << 16) | 5,
                metadata={"world_no": 5, "npc_id": 5, "item_value": slot})
            self.assertEqual(lookup_name(check), expected)

    def test_lookup_shop_seed_w2_poplin_top(self):
        # W2 Poplin Shop (Top): same world_no=3 as the Bottom shop,
        # distinct npc_id=3 (note: lower npc_id than the Bottom shop's
        # 4 — the npc_id ordering is arbitrary, not Top/Bottom-aligned).
        check = CheckEmitted(
            kind=CheckKind.SHOP_SEED,
            stage_key=(3 << 16) | 3,
            metadata={"world_no": 3, "npc_id": 3, "item_value": 0})
        self.assertEqual(
            lookup_name(check),
            "W2: Poplin Shop (Top) - Wonder Seed")

    def test_lookup_shop_seed_unmapped_returns_none(self):
        # A world_no with no exact OR wildcard entry returns None.
        # (Worlds 1, 3, 4 are fully exact-mapped; 2, 6, 7 have
        # wildcards.  World 9 is fictional → miss both lookups.)
        check = CheckEmitted(
            kind=CheckKind.SHOP_SEED,
            stage_key=(9 << 16) | 99,
            metadata={"world_no": 9, "npc_id": 99, "item_value": 0})
        self.assertIsNone(lookup_name(check))

    def test_lookup_ten_coin_palace_stage_resolves(self):
        # The four palaces with hidden coin rooms (W1, W2, W4, W6) ship
        # "10 Coin" AP locations in locations.json and the table must
        # cover them.  Regression guard for the bug where palace clears
        # logged "no AP location for ten_coin stage_key=..." despite
        # the apworld listing the locations.
        for stage_key, expected in [
            (0x89927C97, "W1: Pipe-Rock Plateau Palace - 10 Coin #1"),
            (0xB2B07454, "W2: Fluff-Puff Peaks Palace - 10 Coin #1"),
            (0x1969941E, "W4: Sunbaked Desert Palace - 10 Coin #1"),
            (0x7E523816, "W6: Deep Magma Bog Palace - 10 Coin #1"),
        ]:
            check = CheckEmitted(
                kind=CheckKind.TEN_COIN,
                stage_key=stage_key,
                metadata={"coin_index": 0})
            self.assertEqual(lookup_name(check), expected)

    def test_shop_badges_resolve_course_badges_do_not(self):
        """Only shop-purchased badges are AP checks: each resolves to its
        "<Name> Obtained" location (with the All Bubble Flower Badge ->
        All Bubble Power Badge Obtained override).  Course / Badge
        Challenge badges are granted but not checked, so they resolve to
        None.  The items-json-cross-check above asserts each resolved
        name actually exists in locations.json."""
        from ..location_table import (
            _BADGE_LOCATION_NAME_OVERRIDES, _SHOP_BADGE_ITEM_NAMES)
        for name, bit, _ in _BADGES:
            check = CheckEmitted(
                kind=CheckKind.BADGE_ACQUIRED, stage_key=bit)
            if name in _SHOP_BADGE_ITEM_NAMES:
                expected = _BADGE_LOCATION_NAME_OVERRIDES.get(
                    name, f"{name} Obtained")
                self.assertEqual(lookup_name(check), expected)
            else:
                self.assertIsNone(
                    lookup_name(check),
                    f"course badge {name!r} should not be an AP check")

    def test_badge_location_unmapped_id_returns_none(self):
        check = CheckEmitted(kind=CheckKind.BADGE_ACQUIRED, stage_key=99)
        self.assertIsNone(lookup_name(check))

    # ---- PlayReport world_no -> AP world mapping --------------------

    def test_pr_world_no_to_ap_world_numbered_worlds(self):
        # The six numbered overworlds (PI takes the world_no=2 slot, so
        # W2..W6 are shifted up by one).
        self.assertEqual(pr_world_no_to_ap_world(1), 1)  # W1
        self.assertEqual(pr_world_no_to_ap_world(3), 2)  # W2
        self.assertEqual(pr_world_no_to_ap_world(4), 3)  # W3
        self.assertEqual(pr_world_no_to_ap_world(5), 4)  # W4
        self.assertEqual(pr_world_no_to_ap_world(6), 5)  # W5
        self.assertEqual(pr_world_no_to_ap_world(7), 6)  # W6

    def test_pr_world_no_to_ap_world_non_numbered_slots(self):
        self.assertIsNone(pr_world_no_to_ap_world(2))  # Petal Isles (hub)
        self.assertIsNone(pr_world_no_to_ap_world(8))  # Castle / Bowser
        self.assertIsNone(pr_world_no_to_ap_world(9))  # Special world
        self.assertIsNone(pr_world_no_to_ap_world(0))  # unknown / default


class TestRoyalSeedTable(unittest.TestCase):

    def test_every_royal_seed_name_exists_in_items_json(self):
        item_names = _load_names(_ITEMS_JSON)
        missing = [s[0] for s in _ROYAL_SEEDS if s[0] not in item_names]
        self.assertEqual(
            missing, [],
            f"royal seeds not found in items.json: {missing}")

    def test_all_six_worlds_present(self):
        names = {s[0] for s in _ROYAL_SEEDS}
        self.assertEqual(
            names,
            {f"W{i} Royal Seed" for i in range(1, 7)})

    def test_no_duplicate_hashes(self):
        seen: dict[int, str] = {}
        for entry in _ROYAL_SEEDS:
            name, h = entry[0], entry[1]
            self.assertNotIn(
                h, seen,
                f"duplicate hash=0x{h:08x}: "
                f"{seen.get(h)!r} and {name!r}")
            seen[h] = name

    def test_w1_royal_seed_hash(self):
        # MemetendoYT-verified; canonical reference for the smoke test.
        self.assertEqual(hash_for_item("W1 Royal Seed"), 0x55815859)

    def test_w6_royal_seed_hash(self):
        self.assertEqual(hash_for_item("W6 Royal Seed"), 0xD4660D2B)

    def test_is_royal_seed_item(self):
        self.assertTrue(is_royal_seed_item("W3 Royal Seed"))
        self.assertFalse(is_royal_seed_item("Spring Feet Badge"))
        self.assertFalse(is_royal_seed_item("Filler"))

    def test_unknown_item_returns_none(self):
        self.assertIsNone(hash_for_item("Made-up Seed"))

    def test_no_overlap_with_badge_table(self):
        # Routing in ap_client checks is_badge_item first then
        # is_royal_seed_item; the elif chain assumes the sets are
        # disjoint.  If a badge name ever collides with a royal seed
        # name, the routing would silently misclassify.
        badge_names = {n for n, _, _ in _BADGES}
        seed_names = {s[0] for s in _ROYAL_SEEDS}
        overlap = badge_names & seed_names
        self.assertEqual(overlap, set())

    def test_royal_seed_value_is_one(self):
        # u8 bool slot -- truncated by FUN_710049F648; 1 == True.
        self.assertEqual(ROYAL_SEED_VALUE, 1)

    def test_bit_for_item_per_world(self):
        # Bit positions are part of the wire contract with the Switch
        # dispatch: bit N here MUST match kRoyalSeedHashes[N] in
        # switch-mod/src/program/ap/ApFrameBridge.hpp.
        for i in range(1, 7):
            self.assertEqual(bit_for_item(f"W{i} Royal Seed"), i - 1)

    def test_bit_for_unknown_item_is_none(self):
        self.assertIsNone(bit_for_item("Made-up Seed"))
        self.assertIsNone(bit_for_item("Spring Feet Badge"))

    def test_world_count_is_six(self):
        self.assertEqual(ROYAL_SEED_WORLD_COUNT, 6)

    def test_all_mask_covers_six_bits(self):
        self.assertEqual(ALL_MASK, 0x3F)

    def test_royal_seed_hashes_ordered_by_bit(self):
        # ROYAL_SEED_HASHES[N] is the hash for the N-th-bit world.
        self.assertEqual(len(ROYAL_SEED_HASHES), 6)
        self.assertEqual(ROYAL_SEED_HASHES[0], 0x55815859)  # W1
        self.assertEqual(ROYAL_SEED_HASHES[5], 0xD4660D2B)  # W6

    def test_palace_stage_key_per_world(self):
        # Spot-check: W3 is the "Royal Seed Mansion" odd-one-out, W5
        # is "Operation Poplin Rescue", everyone else is a *_PALACE.
        # Values mirror location_table's _STAGE_*_PALACE constants.
        self.assertEqual(
            palace_stage_key_for_item("W1 Royal Seed"), 0x89927C97)
        self.assertEqual(
            palace_stage_key_for_item("W3 Royal Seed"), 0xA5E2BB3A)
        self.assertEqual(
            palace_stage_key_for_item("W5 Royal Seed"), 0x87E6D263)
        self.assertEqual(
            palace_stage_key_for_item("W6 Royal Seed"), 0x7E523816)

    def test_palace_stage_key_unknown_item_is_none(self):
        self.assertIsNone(palace_stage_key_for_item("Made-up Seed"))
        self.assertIsNone(palace_stage_key_for_item("Spring Feet Badge"))

    def test_palace_stage_keys_resolve_in_location_table(self):
        # Every Royal Seed item's palace stage_key MUST resolve to a
        # "<World> ... - Royal Seed" entry under CheckKind.PALACE_CLEAR
        # in the location table; otherwise the AP-grant auto-emit in
        # context._handle_received_items would silently drop the check.
        for item_name in (f"W{i} Royal Seed" for i in range(1, 7)):
            sk = palace_stage_key_for_item(item_name)
            self.assertIsNotNone(sk, f"no palace_stage_key for {item_name}")
            check = CheckEmitted(
                kind=CheckKind.PALACE_CLEAR, stage_key=sk)
            name = lookup_name(check)
            self.assertIsNotNone(
                name,
                f"{item_name} stage_key 0x{sk:08x} not in location table")
            self.assertTrue(
                name.endswith(" - Royal Seed"),
                f"{item_name} resolved to non-seed location {name!r}")


class TestCoinTable(unittest.TestCase):

    def test_every_coin_item_name_exists_in_items_json(self):
        item_names = _load_names(_ITEMS_JSON)
        missing = [n for n, _, _ in _COIN_ITEMS if n not in item_names]
        self.assertEqual(
            missing, [], f"coin items not found in items.json: {missing}")

    def test_ten_coin_maps_to_flower_coin_hash(self):
        # MemetendoYT-verified flower_coin hash; canonical reference.
        self.assertEqual(grant_for_item("10 Coin"), (0xF4EE6827, 10))

    def test_is_coin_item(self):
        self.assertTrue(is_coin_item("10 Coin"))
        self.assertFalse(is_coin_item("Spring Feet Badge"))
        self.assertFalse(is_coin_item("W1 Royal Seed"))

    def test_unknown_item_returns_none(self):
        self.assertIsNone(grant_for_item("Imaginary Coin"))

    def test_no_overlap_with_badge_or_royal_seed_tables(self):
        # Routing in _handle_received_items is an if/elif chain that
        # assumes the three categories are disjoint.  If a badge or
        # royal seed name ever collides with a coin name, routing would
        # silently misclassify the coin item.
        badge_names = {n for n, _, _ in _BADGES}
        seed_names = {s[0] for s in _ROYAL_SEEDS}
        coin_names = {n for n, _, _ in _COIN_ITEMS}
        self.assertEqual(coin_names & badge_names, set())
        self.assertEqual(coin_names & seed_names, set())


class TestWorldUnlockTable(unittest.TestCase):
    """Pins the 2026-06-09 GameDataList category split.

    The two switch-side writers are not interchangeable:
    grantContainerACounter silently no-ops on Bool-category hashes and
    grantContainerBBool null-derefs on Int-category ones, so a hash
    landing in the wrong list is either a dead grant or a crash.  The
    split was audited against RomFS GameDataList.Product.100; these
    tests pin the audited shape so an edit can't silently merge them.
    """

    # The six WorldMapCloudPackunVanishInfo.IsVanish* bools that
    # probe::applyOpenWorldEntry also force-grants switch-side.
    IS_VANISH = {
        0xC687FB5F, 0xCFF5F3D2, 0x048BC39C,
        0x1677F038, 0x95539EC5, 0x7F6E8A47,
    }

    def test_category_split_shape(self):
        self.assertEqual(len(WORLD_UNLOCK_BOOL_HASHES), 84)
        self.assertEqual(
            WORLD_UNLOCK_INT_HASHES, (0x5AC1E406, 0x20FCED8B))

    def test_lists_disjoint_and_combined(self):
        bool_set = set(WORLD_UNLOCK_BOOL_HASHES)
        int_set = set(WORLD_UNLOCK_INT_HASHES)
        self.assertEqual(len(bool_set), len(WORLD_UNLOCK_BOOL_HASHES))
        self.assertEqual(bool_set & int_set, set())
        self.assertEqual(
            WORLD_UNLOCK_HASHES,
            WORLD_UNLOCK_INT_HASHES + WORLD_UNLOCK_BOOL_HASHES)

    def test_all_hashes_u32(self):
        for h in WORLD_UNLOCK_HASHES:
            self.assertTrue(0 <= h < (1 << 32), f"0x{h:x} not u32")

    def test_is_vanish_bools_present_in_bool_list(self):
        self.assertLessEqual(self.IS_VANISH, set(WORLD_UNLOCK_BOOL_HASHES))

    def test_no_royal_seed_or_complete_game_hashes(self):
        # AP owns the Royal Seeds; the game owns COMPLETE_GAME.
        forbidden = set(ROYAL_SEED_HASHES) | {0x5D3EC9B4}
        self.assertEqual(set(WORLD_UNLOCK_HASHES) & forbidden, set())


if __name__ == "__main__":
    unittest.main()
