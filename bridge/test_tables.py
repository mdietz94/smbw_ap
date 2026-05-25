"""Sanity tests for badge_table + location_table.

Cross-checks every shipped entry against the manual apworld's
locations.json / items.json to catch typos before they hit the wire.
"""

from __future__ import annotations

import json
import os
import unittest

if __package__ is None or __package__ == "":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from badge_table import _BADGES, grant_internal_id_for_item, is_badge_item
    from location_table import _TABLE, lookup_name
    from protocol import CheckEmitted, CheckKind
else:
    from .badge_table import _BADGES, grant_internal_id_for_item, is_badge_item
    from .location_table import _TABLE, lookup_name
    from .protocol import CheckEmitted, CheckKind


_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir))
_LOCATIONS_JSON = os.path.join(
    _REPO_ROOT, "manual_smbwonder_zim", "data", "locations.json")
_ITEMS_JSON = os.path.join(
    _REPO_ROOT, "manual_smbwonder_zim", "data", "items.json")


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

    def test_lookup_known_entry(self):
        check = CheckEmitted(
            kind=CheckKind.WONDER_SEED, stage_key=2937190396)
        self.assertEqual(
            lookup_name(check),
            "W1: Welcome to the Flower Kingdom! - Wonder Seed")

    def test_lookup_unknown_returns_none(self):
        check = CheckEmitted(kind=CheckKind.WONDER_SEED, stage_key=99999999)
        self.assertIsNone(lookup_name(check))


if __name__ == "__main__":
    unittest.main()
