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
from ..location_table import _TABLE, _TEN_COIN_TABLE, lookup_name
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

    def test_lookup_ten_coin_palace_stage_returns_none(self):
        # Palaces have no 10-coin locations; the apworld doesn't list any
        # and we don't ship table entries for them.
        check = CheckEmitted(
            kind=CheckKind.TEN_COIN,
            stage_key=2308078743,  # Pipe-Rock Plateau Palace
            metadata={"coin_index": 0})
        self.assertIsNone(lookup_name(check))

    def test_badge_locations_present_for_every_mapped_badge(self):
        """Every entry in badge_table._BADGES should resolve to SOME
        AP location.  The default pattern is "<Name> Obtained" but the
        apworld has one asymmetric case (All Bubble Flower Badge ->
        All Bubble Power Badge Obtained); location_table's override
        map handles that.  This test just checks every badge resolves;
        the items-json-cross-check above asserts the resolved name
        actually exists."""
        from ..location_table import _BADGE_LOCATION_NAME_OVERRIDES
        for name, bit, _ in _BADGES:
            check = CheckEmitted(
                kind=CheckKind.BADGE_ACQUIRED, stage_key=bit)
            expected = _BADGE_LOCATION_NAME_OVERRIDES.get(
                name, f"{name} Obtained")
            self.assertEqual(lookup_name(check), expected)

    def test_badge_location_unmapped_id_returns_none(self):
        check = CheckEmitted(kind=CheckKind.BADGE_ACQUIRED, stage_key=99)
        self.assertIsNone(lookup_name(check))


class TestRoyalSeedTable(unittest.TestCase):

    def test_every_royal_seed_name_exists_in_items_json(self):
        item_names = _load_names(_ITEMS_JSON)
        missing = [n for n, _, _ in _ROYAL_SEEDS if n not in item_names]
        self.assertEqual(
            missing, [],
            f"royal seeds not found in items.json: {missing}")

    def test_all_six_worlds_present(self):
        names = {n for n, _, _ in _ROYAL_SEEDS}
        self.assertEqual(
            names,
            {f"W{i} Royal Seed" for i in range(1, 7)})

    def test_no_duplicate_hashes(self):
        seen: dict[int, str] = {}
        for name, h, _ in _ROYAL_SEEDS:
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
        seed_names = {n for n, _, _ in _ROYAL_SEEDS}
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
        seed_names = {n for n, _, _ in _ROYAL_SEEDS}
        coin_names = {n for n, _, _ in _COIN_ITEMS}
        self.assertEqual(coin_names & badge_names, set())
        self.assertEqual(coin_names & seed_names, set())


if __name__ == "__main__":
    unittest.main()
