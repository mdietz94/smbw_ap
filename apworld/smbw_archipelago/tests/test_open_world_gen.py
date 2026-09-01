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
from .ut_sim import regen_like_ut


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
            # Inactive worlds' WONDER seeds are stripped (precollected instead);
            # their ROYAL seeds are KEPT -- all six are required to face Bowser.
            if name.endswith(" Wonder Seed"):
                n = world_of_region(name)
                if n is not None:
                    self.assertIn(n, active, f"leaked inactive Wonder Seed {name!r}")

        # All six Royal Seeds stay in the pool regardless of the active set.
        royal_in_pool = {n for n in item_names if n.endswith(" Royal Seed")}
        self.assertEqual(
            royal_in_pool, {f"W{n} Royal Seed" for n in range(1, 7)},
            "all six Royal Seeds must remain in the pool in open-world")

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

    def test_bowser_castle_courses_active_metas_stripped(self):
        # Bowser's Castle is always part of open-world: every BC: course
        # location stays in the pool (regardless of which worlds are active),
        # while the four non-course "All <X> Power Badge Obtained" metas are
        # stripped.
        multiworld, _ = _gen({"open_world": 1, "open_world_count": 2})
        wb = multiworld.get_region("World Bowser", 1)
        names = {loc.name for loc in wb.locations}
        self.assertTrue(names, "World Bowser should keep its BC: courses")
        self.assertTrue(all(n.startswith("BC:") for n in names),
                        f"non-course location leaked into World Bowser: {names}")
        for course in ("Missile Meg Mayhem", "High-Voltage Gauntlet",
                       "Evade the Seeker Bullet Bills!",
                       "KnuckleFest Bowser's Blazing Beats",
                       "Bowser's Rage Stage"):
            self.assertTrue(any(course in n for n in names),
                            f"BC course {course!r} missing from the pool")
        self.assertFalse(any("Power Badge Obtained" in n for n in names),
                         "the All-Power-Badge metas should be stripped")

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

    def test_no_castle_item_is_a_bowser_prerequisite(self):
        # Anti-softlock: the player must be able to open Bowser's Castle
        # (hold all six Royal Seeds + reach palaces_required palaces) using
        # ONLY items found outside the Castle.  Otherwise a Castle item would
        # be a prerequisite for entering the Castle -- a deadlock the runtime
        # death-gate would hit (can't enter to grab it without first clearing
        # a palace that item gates).  The palace-reachability term in
        # make_bowser_gate is what forces fill to keep prerequisites out.
        for count in (1, 3, 6):
            with self.subTest(count=count):
                multiworld, _ = _gen(
                    {"open_world": 1, "open_world_count": count},
                    seed=4000 + count, fill=True)
                state = CollectionState(multiworld)
                for loc in multiworld.get_filled_locations(1):
                    if loc.parent_region is not None \
                            and loc.parent_region.name == "World Bowser":
                        continue
                    if loc.item is not None:
                        state.collect(loc.item, prevent_sweep=True)
                self.assertTrue(
                    state.can_reach_region("World Bowser", 1),
                    f"a Bowser's Castle item is a prerequisite for entering "
                    f"the Castle (count={count})")

    def test_victory_placed_on_bowser(self):
        multiworld, world = _gen({"open_world": 1, "open_world_count": 2})
        victory = multiworld.get_location(BOWSER_VICTORY_LOCATION, 1)
        self.assertEqual(victory.item.name, "__Victory__")
        self.assertEqual(victory.parent_region.name, "World Bowser")


class TestUniversalTrackerCompat(unittest.TestCase):
    """A Universal Tracker regeneration must land on the same active-world
    set recorded in slot_data, not re-roll its own."""

    def test_pinned_worlds_survive_ut_regeneration(self):
        opts = {"open_world": 1, "open_world_count": 3}

        # 1. Generate with seed 1234 to capture slot_data.
        _, world_orig = _gen(opts, seed=1234)
        slot_data = world_orig.fill_slot_data()
        pinned = slot_data["open_world_active"]
        self.assertEqual(len(pinned), 3)

        # 2. A different seed picks a different set on its own.
        _, world_probe = _gen(opts, seed=9999)
        if sorted(world_probe.active_worlds) == sorted(pinned):
            self.skipTest("seed 9999 happens to roll the same worlds")

        # 3. UT's real flow: interpret_slot_data on a throwaway world, then a
        #    full regeneration that only carries the returned passthrough.
        _, world_ut = regen_like_ut(slot_data, opts, 9999)

        # 4. active_worlds must match slot_data, not the seed-9999 roll.
        self.assertEqual(sorted(world_ut.active_worlds), sorted(pinned))
        self.assertEqual(world_ut.fill_slot_data()["open_world_active"], pinned)


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
