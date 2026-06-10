"""Tests for badge_table -- in particular the equipped-badge identity
mapping that drives force-unequip of AP-disabled badges (2026-06-10).

The owned-badge bitfield is overwritten absolutely by the bridge's
SetBadgesAbsolute push, but the *equipped* field is separate: a level or
shop that grants+auto-equips a badge AP has disabled leaves the badge
equipped/available even after its owned bit is cleared.  The Switch's
``clearEquippedBadgesNotOwned`` resets any equip slot referencing a
not-owned badge to the Invalid sentinel.  These tests pin the
internal_id <-> equip-enum-value mapping (the Python mirror of
``kBadgeEnumValues`` in ContainerC.cpp) and the disabled-set semantics.

badge_table imports without Archipelago on sys.path (it reads items.json
via pkgutil), so these run unconditionally.
"""

from __future__ import annotations

import unittest

from .. import badge_table


class TestEquipEnumMapping(unittest.TestCase):

    def test_invalid_sentinel_value(self) -> None:
        # 0x7E3D1E46 -- also the field DefaultValue per GameDataList.
        self.assertEqual(badge_table.BADGE_INVALID_ENUM_VALUE, 0x7E3D1E46)

    def test_known_enum_values_match_re_map(self) -> None:
        # Cross-checked against the RE map save-file equipped identities.
        self.assertEqual(
            badge_table.equip_enum_value_for_internal_id(34), 0xE41B1ABA)  # Wall-Climb
        self.assertEqual(
            badge_table.equip_enum_value_for_internal_id(46), 0xB77086E2)  # Auto Super Mushroom
        self.assertEqual(
            badge_table.equip_enum_value_for_internal_id(4), 0xE9F7789A)   # Spring Feet

    def test_out_of_range_internal_id(self) -> None:
        self.assertIsNone(badge_table.equip_enum_value_for_internal_id(-1))
        self.assertIsNone(badge_table.equip_enum_value_for_internal_id(59))
        self.assertIsNone(badge_table.equip_enum_value_for_internal_id(1000))

    def test_roundtrip_all_badges(self) -> None:
        for internal_id in range(59):
            ev = badge_table.equip_enum_value_for_internal_id(internal_id)
            self.assertIsNotNone(ev)
            self.assertEqual(
                badge_table.internal_id_for_equip_enum_value(ev),
                internal_id,
                f"roundtrip failed for internal_id {internal_id}")

    def test_invalid_decodes_to_none(self) -> None:
        self.assertIsNone(
            badge_table.internal_id_for_equip_enum_value(
                badge_table.BADGE_INVALID_ENUM_VALUE))

    def test_unrecognized_enum_value_decodes_to_none(self) -> None:
        self.assertIsNone(
            badge_table.internal_id_for_equip_enum_value(0xDEADBEEF))

    def test_enum_values_are_unique(self) -> None:
        # No two badges share an equip enum value (else decode is ambiguous).
        vals = [badge_table.equip_enum_value_for_internal_id(i)
                for i in range(59)]
        self.assertEqual(len(vals), len(set(vals)))
        # ...and none collide with the Invalid sentinel.
        self.assertNotIn(badge_table.BADGE_INVALID_ENUM_VALUE, vals)


class TestDisabledBadgeSet(unittest.TestCase):

    def test_empty_owned_mask_disables_all(self) -> None:
        disabled = badge_table.disabled_badge_internal_ids(0)
        self.assertEqual(disabled, set(range(59)))

    def test_full_owned_mask_disables_none(self) -> None:
        full = (1 << 59) - 1
        self.assertEqual(badge_table.disabled_badge_internal_ids(full), set())

    def test_partial_owned_mask(self) -> None:
        # Own only Spring Feet (4) and Auto Super Mushroom (46).
        owned = (1 << 4) | (1 << 46)
        disabled = badge_table.disabled_badge_internal_ids(owned)
        self.assertNotIn(4, disabled)
        self.assertNotIn(46, disabled)
        self.assertIn(34, disabled)   # Wall-Climb not owned -> disabled
        self.assertIn(0, disabled)
        self.assertEqual(len(disabled), 57)

    def test_disabled_set_drives_unequip_decision(self) -> None:
        # The scenario from the bug report: a level grants+auto-equips a
        # badge (internal_id 34, Wall-Climb) that AP never placed.  With
        # an owned mask that omits 34, the badge IS in the disabled set,
        # so the Switch will reset its equip slot to Invalid.
        owned = (1 << 9)  # only Coin Reward owned
        self.assertIn(34, badge_table.disabled_badge_internal_ids(owned))


class TestPythonSubsdkTableParity(unittest.TestCase):
    """The Python _BADGE_ENUM_VALUES must mirror kBadgeEnumValues in
    switch-mod/src/probe/ContainerC.cpp.  Parse the C++ table out of the
    source and compare, so the two never silently drift."""

    def test_tables_match(self) -> None:
        import pathlib
        import re

        cpp = (pathlib.Path(__file__).resolve()
               .parents[4] / "switch-mod" / "src" / "probe" / "ContainerC.cpp")
        if not cpp.exists():
            self.skipTest(f"ContainerC.cpp not found at {cpp}")
        text = cpp.read_text(encoding="utf-8")
        m = re.search(
            r"kBadgeEnumValues\[\]\s*=\s*\{(.*?)\};", text, re.DOTALL)
        self.assertIsNotNone(m, "kBadgeEnumValues table not found in C++")
        pairs = re.findall(r"\{0x([0-9a-fA-F]+)u,\s*(\d+)\}", m.group(1))
        cpp_map = {int(v, 16): int(k) for v, k in pairs}
        py_map = {badge_table.equip_enum_value_for_internal_id(i): i
                  for i in range(59)}
        self.assertEqual(cpp_map, py_map)
        # Also confirm the invalid sentinel matches.
        m2 = re.search(
            r"kBadgeInvalidEnumValue\s*=\s*(\d+)u", text)
        self.assertIsNotNone(m2)
        self.assertEqual(
            int(m2.group(1)), badge_table.BADGE_INVALID_ENUM_VALUE)


if __name__ == "__main__":
    unittest.main()
