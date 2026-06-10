"""Generation tests for open-world mode.

Exercises the random world selection, region restructuring, content
exclusion, the Royal-Seed-gated Bowser goal, slot_data export, and
solvability — plus a regression that open_world=0 leaves the vanilla
linear spine untouched and a two-player check for regionMap bleed.
"""
from __future__ import annotations

import logging
import unittest

from BaseClasses import CollectionState
from test.general import gen_steps, setup_multiworld
from Fill import distribute_items_restrictive

from .. import SMBWonderWorld
from ..open_world import BOWSER_VICTORY_LOCATION, world_of_region
from ..Regions import regionMap


def _gen(options, seed=1234, fill=False):
    multiworld = setup_multiworld(SMBWonderWorld, gen_steps, seed=seed, options=options)
    if fill:
        # adjust_filler_items warns loudly about trimming the surplus pool;
        # that's expected here, so quiet it for the test run.
        logging.disable(logging.WARNING)
        try:
            distribute_items_restrictive(multiworld)
        finally:
            logging.disable(logging.NOTSET)
    return multiworld, multiworld.worlds[1]


class TestOpenWorldGeneration(unittest.TestCase):
    def test_random_active_world_count(self):
        _, world = _gen({"open_world": 1, "open_world_count": 3})
        self.assertTrue(world.open_world)
        self.assertEqual(len(world.active_worlds), 3)
        self.assertEqual(len(set(world.active_worlds)), 3, "active worlds must be distinct")
        self.assertTrue(all(1 <= n <= 6 for n in world.active_worlds))

    def test_goal_forced_to_bowser(self):
        _, world = _gen({"open_world": 1, "open_world_count": 2})
        self.assertEqual(world.victory_names[world.options.goal.value], BOWSER_VICTORY_LOCATION)

    def test_active_starts_reachable_empty_handed(self):
        multiworld, world = _gen({"open_world": 1, "open_world_count": 4})
        state = CollectionState(multiworld)
        reachable = {r.name for r in multiworld.get_regions(1) if state.can_reach(r, "Region", 1)}
        for n in world.active_worlds:
            self.assertIn(f"W{n} Start", reachable, f"W{n} Start should be reachable from the start")
        # Bowser must NOT be free — it's gated behind the Royal Seeds.
        self.assertNotIn("World Bowser", reachable)

    def test_inactive_and_hub_content_removed(self):
        multiworld, world = _gen({"open_world": 1, "open_world_count": 3})
        active = set(world.active_worlds)

        for region in multiworld.get_regions(1):
            n = world_of_region(region.name)
            is_hub = region.name.startswith("PI ") or region.name in (
                "Pre-W4 Special", "Special End", "Post-Badge")
            if (n is not None and n not in active) or is_hub:
                self.assertEqual(list(region.locations), [],
                                 f"{region.name} should have no locations in open-world")

        item_names = [i.name for i in multiworld.itempool if i.player == 1]
        for name in item_names:
            self.assertNotIn(name, ("Petal Isles Wonder Seed", "Special World Wonder Seed"))
            n = world_of_region(name) if (name.endswith(" Royal Seed") or name.endswith(" Wonder Seed")) else None
            if n is not None:
                self.assertIn(n, active, f"leaked inactive seed item {name!r}")

    def test_inactive_wonder_seeds_precollected(self):
        multiworld, world = _gen({"open_world": 1, "open_world_count": 3})
        active = set(world.active_worlds)
        precollected = [i.name for i in multiworld.precollected_items[1]]

        for n in range(1, 7):
            name = f"W{n} Wonder Seed"
            want = int(world.item_name_to_item[name].get("count", 0))
            got = precollected.count(name)
            if n in active:
                self.assertEqual(got, 0, f"active world W{n} should not be granted its Wonder Seeds")
            else:
                self.assertEqual(got, want, f"inactive world W{n} should be granted all {want} Wonder Seeds")

        # Royal Seeds are never precollected (active or inactive).
        for n in range(1, 7):
            self.assertEqual(precollected.count(f"W{n} Royal Seed"), 0)

    def test_palaces_required_sentinel_equals_count(self):
        _, world = _gen({"open_world": 1, "open_world_count": 4, "palaces_required": 0})
        self.assertEqual(world.palaces_required, 4)

    def test_palaces_required_explicit_and_clamped(self):
        _, world = _gen({"open_world": 1, "open_world_count": 4, "palaces_required": 2})
        self.assertEqual(world.palaces_required, 2)
        # Above the active count clamps down.
        _, world2 = _gen({"open_world": 1, "open_world_count": 2, "palaces_required": 5}, seed=99)
        self.assertEqual(world2.palaces_required, 2)

    def test_slot_data_exports_active_worlds(self):
        _, world = _gen({"open_world": 1, "open_world_count": 3})
        slot_data = world.fill_slot_data()
        self.assertEqual(slot_data["open_world_active"], list(world.active_worlds))
        self.assertEqual(slot_data["palaces_required"], world.palaces_required)
        self.assertEqual(slot_data["goal_location_name"], BOWSER_VICTORY_LOCATION)

    def test_solvable_across_counts(self):
        for count in (1, 3, 6):
            with self.subTest(count=count):
                multiworld, _ = _gen({"open_world": 1, "open_world_count": count}, seed=2000 + count, fill=True)
                self.assertTrue(multiworld.can_beat_game(), f"open-world N={count} should be beatable")

    def test_victory_placed_on_bowser(self):
        multiworld, world = _gen({"open_world": 1, "open_world_count": 2})
        victory = multiworld.get_location(BOWSER_VICTORY_LOCATION, 1)
        self.assertEqual(victory.item.name, "__Victory__")
        self.assertEqual(victory.parent_region.name, "World Bowser")


class TestUniversalTrackerCompat(unittest.TestCase):
    """Verify that interpret_slot_data → generate_early produces the same
    active-world set recorded in slot_data (Universal Tracker regression)."""

    def test_pinned_worlds_survive_generate_early(self):
        # 1. Generate with seed 1234 to capture slot_data.
        _, world_orig = _gen({"open_world": 1, "open_world_count": 3}, seed=1234)
        slot_data = world_orig.fill_slot_data()
        pinned = slot_data["open_world_active"]
        self.assertEqual(len(pinned), 3)

        # 2. Generate a second world with a different seed (different random
        #    state → different random world selection without the fix).
        _, world_ut = _gen({"open_world": 1, "open_world_count": 3}, seed=9999)

        # 3. Simulate Universal Tracker: interpret_slot_data then generate_early.
        world_ut.interpret_slot_data(slot_data)
        self.assertTrue(hasattr(world_ut, "_ow_pinned_active_worlds"),
                        "interpret_slot_data must set _ow_pinned_active_worlds")
        world_ut.generate_early()

        # 4. active_worlds must match slot_data, not the seed-9999 random roll.
        self.assertEqual(sorted(world_ut.active_worlds), sorted(pinned))


class TestOpenWorldRegression(unittest.TestCase):
    def test_open_world_off_keeps_linear_spine(self):
        multiworld, world = _gen({"open_world": 0})
        self.assertFalse(world.open_world)
        state = CollectionState(multiworld)
        reachable = {r.name for r in multiworld.get_regions(1) if state.can_reach(r, "Region", 1)}
        self.assertIn("W1 Start", reachable)
        # Later worlds are gated behind the spine, not free.
        self.assertNotIn("W4 Start", reachable)
        self.assertNotIn("open_world_active", world.fill_slot_data())

    def test_open_world_off_still_solvable(self):
        multiworld, _ = _gen({"open_world": 0}, seed=321, fill=True)
        self.assertTrue(multiworld.can_beat_game())

    def test_two_players_no_regionmap_bleed(self):
        multiworld = setup_multiworld(
            [SMBWonderWorld, SMBWonderWorld], gen_steps, seed=42,
            options=[{"open_world": 1, "open_world_count": 2},
                     {"open_world": 1, "open_world_count": 3}])
        logging.disable(logging.WARNING)
        try:
            distribute_items_restrictive(multiworld)
        finally:
            logging.disable(logging.NOTSET)
        self.assertTrue(multiworld.can_beat_game())
        # The shared regionMap must be restored after generation.
        self.assertEqual(regionMap["World Bowser"].get("requires"), "|@Royal Seed:6|")
        self.assertEqual(regionMap["W4 Start"].get("requires"), "|Petal Isles Wonder Seed:10|")
