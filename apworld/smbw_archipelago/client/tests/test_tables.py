"""Sanity tests for badge_table + location_table.

Cross-checks every shipped entry against the manual apworld's
locations.json / items.json to catch typos before they hit the wire.
"""

from __future__ import annotations

import json
import os
import unittest

from ..badge_table import _BADGES, grant_internal_id_for_item, is_badge_item
from ..location_table import _TABLE, _TEN_COIN_TABLE, lookup_name
from ..protocol import CheckEmitted, CheckKind
from ..royal_seed_table import (
    ROYAL_SEED_VALUE,
    _ROYAL_SEEDS,
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


if __name__ == "__main__":
    unittest.main()
