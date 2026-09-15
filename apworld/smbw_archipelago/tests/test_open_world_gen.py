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
from ..open_world import (
    BOWSER_VICTORY_LOCATION,
    CASTLE_UNLOCK_ITEM,
    OPENING_COURSE_PREFIX,
    world_of_region,
    world_unlock_item,
)
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
        # world_unlock_items OFF: every active world opens at once.
        multiworld, world = _gen(
            {"open_world": 1, "open_world_count": 4, "world_unlock_items": 0})
        state = CollectionState(multiworld)
        reachable = {r.name for r in multiworld.get_regions(1) if state.can_reach(r, "Region", 1)}
        for n in world.active_worlds:
            self.assertIn(f"W{n} Start", reachable, f"W{n} Start should be reachable from the start")
        # Bowser's Castle opens with the worlds; only its final course (the
        # goal) is gated behind the Royal Seeds.
        self.assertIn("World Bowser", reachable)
        self.assertTrue(multiworld.get_location(
            "BC: Missile Meg Mayhem - Normal Exit", 1).can_reach(state))
        self.assertFalse(multiworld.get_location(
            BOWSER_VICTORY_LOCATION, 1).can_reach(state))

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

    def test_castle_gated_on_unlock_only_final_course_on_royal_seeds(self):
        # Bowser's Castle is unlocked like a world: its gauntlet courses need
        # only "Bowser's Castle Unlock"; the final Bowser's Rage Stage (goal)
        # additionally needs all six Royal Seeds + the palaces.
        multiworld, world = _gen({"open_world": 1, "open_world_count": 3})
        gauntlet = multiworld.get_location("BC: Missile Meg Mayhem - Normal Exit", 1)
        goal = multiworld.get_location(BOWSER_VICTORY_LOCATION, 1)
        state = CollectionState(multiworld)
        self.assertFalse(state.can_reach_region("World Bowser", 1))

        state.collect(world.create_item(CASTLE_UNLOCK_ITEM), prevent_sweep=True)
        self.assertTrue(state.can_reach_region("World Bowser", 1))
        self.assertTrue(gauntlet.can_reach(state))
        self.assertFalse(goal.can_reach(state))

        # Everything but one Royal Seed still leaves the final course shut.
        held_back = None
        for item in multiworld.itempool:
            if item.player != 1:
                continue
            if held_back is None and item.name == "W1 Royal Seed":
                held_back = item
                continue
            state.collect(item, prevent_sweep=True)
        self.assertIsNotNone(held_back)
        self.assertFalse(goal.can_reach(state))
        state.collect(held_back, prevent_sweep=True)
        self.assertTrue(goal.can_reach(state))

    def test_solvable_with_unlock_items_off(self):
        multiworld, _ = _gen(
            {"open_world": 1, "open_world_count": 3, "world_unlock_items": 0},
            seed=4003, fill=True)
        self.assertTrue(multiworld.can_beat_game())

    def test_victory_placed_on_bowser(self):
        multiworld, world = _gen({"open_world": 1, "open_world_count": 2})
        victory = multiworld.get_location(BOWSER_VICTORY_LOCATION, 1)
        self.assertEqual(victory.item.name, "__Victory__")
        self.assertEqual(victory.parent_region.name, "World Bowser")


class TestWorldUnlockItems(unittest.TestCase):
    """``world_unlock_items`` (default ON): each active world is gated
    behind its own "W<n> Unlock" item and exactly one starts unlocked."""

    def _sphere_one_worlds(self, multiworld, world):
        state = CollectionState(multiworld)
        return {n for n in world.active_worlds
                if state.can_reach_region(f"W{n} Start", 1)}

    def test_on_by_default(self):
        _, world = _gen({"open_world": 1, "open_world_count": 4})
        self.assertTrue(world.world_unlock_items)
        self.assertIn(world.start_world, world.active_worlds)

    def test_only_the_start_world_is_open(self):
        for seed in (1234, 99, 7):
            with self.subTest(seed=seed):
                multiworld, world = _gen(
                    {"open_world": 1, "open_world_count": 4}, seed=seed)
                self.assertEqual(self._sphere_one_worlds(multiworld, world),
                                 {world.start_world})

    def test_start_unlock_precollected_rest_in_pool(self):
        multiworld, world = _gen({"open_world": 1, "open_world_count": 4})
        start_item = world_unlock_item(world.start_world)
        precollected = [i.name for i in multiworld.precollected_items[1]]
        self.assertIn(start_item, precollected)

        pool = [i.name for i in multiworld.itempool if i.player == 1]
        self.assertNotIn(start_item, pool)
        self.assertEqual(
            sorted(n for n in pool if n.endswith(" Unlock")),
            sorted([world_unlock_item(n) for n in world.active_worlds
                    if n != world.start_world] + [CASTLE_UNLOCK_ITEM]))

    def test_castle_unlock_in_pool_never_precollected(self):
        for count in (1, 6):
            with self.subTest(count=count):
                multiworld, _ = _gen({"open_world": 1, "open_world_count": count})
                pool = [i.name for i in multiworld.itempool if i.player == 1]
                precollected = [i.name for i in multiworld.precollected_items[1]]
                self.assertEqual(pool.count(CASTLE_UNLOCK_ITEM), 1)
                self.assertNotIn(CASTLE_UNLOCK_ITEM, precollected)

    def test_inactive_worlds_get_no_unlock_item(self):
        multiworld, world = _gen({"open_world": 1, "open_world_count": 3})
        active = set(world.active_worlds)
        names = ([i.name for i in multiworld.itempool if i.player == 1]
                 + [i.name for i in multiworld.precollected_items[1]])
        for name in names:
            if name.endswith(" Unlock") and name != CASTLE_UNLOCK_ITEM:
                self.assertIn(world_of_region(name), active,
                              f"leaked inactive world unlock {name!r}")

    def test_unlock_items_are_progression(self):
        multiworld, world = _gen({"open_world": 1, "open_world_count": 4})
        for item in multiworld.itempool:
            if item.player == 1 and item.name.endswith(" Unlock"):
                self.assertTrue(item.advancement, f"{item.name} must be progression")

    def test_disabled_creates_no_unlock_items(self):
        multiworld, world = _gen(
            {"open_world": 1, "open_world_count": 4, "world_unlock_items": 0})
        self.assertFalse(world.world_unlock_items)
        self.assertIsNone(world.start_world)
        names = ([i.name for i in multiworld.itempool if i.player == 1]
                 + [i.name for i in multiworld.precollected_items[1]])
        self.assertEqual([n for n in names if n.endswith(" Unlock")], [])

    def test_standard_mode_creates_no_unlock_items(self):
        multiworld, world = _gen({"open_world": 0})
        self.assertFalse(world.world_unlock_items)
        names = ([i.name for i in multiworld.itempool if i.player == 1]
                 + [i.name for i in multiworld.precollected_items[1]])
        self.assertEqual([n for n in names if n.endswith(" Unlock")], [])

    def test_solvable_across_counts(self):
        for count in (1, 3, 6):
            with self.subTest(count=count):
                multiworld, _ = _gen(
                    {"open_world": 1, "open_world_count": count},
                    seed=6000 + count, fill=True)
                self.assertTrue(multiworld.can_beat_game(),
                                f"world-unlock N={count} should be beatable")

    def test_slot_data_exports_unlock_state(self):
        _, world = _gen({"open_world": 1, "open_world_count": 3})
        slot_data = world.fill_slot_data()
        self.assertTrue(slot_data["open_world_unlock_items"])
        self.assertTrue(slot_data["open_world_castle_unlock"])
        self.assertEqual(slot_data["open_world_start_world"], world.start_world)

        _, off = _gen(
            {"open_world": 1, "open_world_count": 3, "world_unlock_items": 0})
        off_data = off.fill_slot_data()
        self.assertFalse(off_data["open_world_unlock_items"])
        self.assertNotIn("open_world_start_world", off_data)

    def test_pinned_start_world_survives_ut_regeneration(self):
        opts = {"open_world": 1, "open_world_count": 4}
        _, world_orig = _gen(opts, seed=1234)
        slot_data = world_orig.fill_slot_data()

        _, world_ut = regen_like_ut(slot_data, opts, 9999)
        self.assertEqual(world_ut.start_world, world_orig.start_world)
        self.assertEqual(
            world_ut.fill_slot_data()["open_world_start_world"],
            slot_data["open_world_start_world"])


class TestOpeningCourseAlwaysInLogic(unittest.TestCase):
    """W1-1 is the forced opening course (every save starts in it, and the
    client never world-gates it), so with world-unlock items its checks stay
    in sphere 1 even while World 1 is locked."""

    @staticmethod
    def _gen_w1_locked(extra=None):
        """First seed where World 1 is active but NOT the start world."""
        for seed in range(1, 50):
            multiworld, world = _gen(
                {"open_world": 1, "open_world_count": 6, **(extra or {})},
                seed=seed)
            if world.start_world != 1:
                return multiworld, world
        raise AssertionError("no seed with World 1 locked at start")

    @staticmethod
    def _opening_locations(multiworld):
        return [loc for loc in multiworld.get_locations(1)
                if loc.name.startswith(OPENING_COURSE_PREFIX)]

    def test_opening_course_in_sphere_one_while_w1_locked(self):
        multiworld, _ = self._gen_w1_locked()
        state = CollectionState(multiworld)
        opening = self._opening_locations(multiworld)
        self.assertTrue(opening)
        for loc in opening:
            self.assertTrue(loc.can_reach(state), f"{loc.name} should be sphere 1")
            self.assertEqual(loc.parent_region.name, "Manual")

    def test_rest_of_w1_start_still_gated(self):
        multiworld, _ = self._gen_w1_locked()
        state = CollectionState(multiworld)
        w1_start = multiworld.get_region("W1 Start", 1)
        others = list(w1_start.locations)
        self.assertTrue(others, "W1 Start should keep its other courses")
        for loc in others:
            self.assertFalse(loc.name.startswith(OPENING_COURSE_PREFIX))
            self.assertFalse(loc.can_reach(state),
                             f"{loc.name} must stay behind W1 Unlock")
        state.collect(multiworld.create_item("W1 Unlock", 1), prevent_sweep=True)
        self.assertTrue(state.can_reach_region("W1 Start", 1))

    def test_moved_location_keeps_its_own_requirement(self):
        # With character-block sanity the 1-1 Yoshi Block requires Green
        # Yoshi (a "Character (Easy)" pool item, never the precollected
        # starter).  Moving it in front of the gate must not drop that.
        multiworld, _ = self._gen_w1_locked({"character_block_sanity": 1})
        yoshi = multiworld.get_location(
            OPENING_COURSE_PREFIX + "Yoshi Block", 1)
        self.assertEqual(yoshi.parent_region.name, "Manual")
        state = CollectionState(multiworld)
        self.assertFalse(yoshi.can_reach(state))
        state.collect(multiworld.create_item("Green Yoshi", 1), prevent_sweep=True)
        self.assertTrue(yoshi.can_reach(state))

    def test_w1_inactive_strips_the_opening_course(self):
        # World 1 not in the seed -> its checks (1-1 included) are removed.
        for seed in range(1, 50):
            multiworld, world = _gen(
                {"open_world": 1, "open_world_count": 3}, seed=seed)
            if 1 not in world.active_worlds:
                break
        else:
            self.fail("no seed with World 1 inactive")
        self.assertEqual(self._opening_locations(multiworld), [])

    def test_unlock_items_off_leaves_opening_course_in_w1_start(self):
        multiworld, _ = _gen(
            {"open_world": 1, "open_world_count": 6, "world_unlock_items": 0})
        opening = self._opening_locations(multiworld)
        self.assertTrue(opening)
        for loc in opening:
            self.assertEqual(loc.parent_region.name, "W1 Start")

    def test_solvable_with_w1_locked(self):
        multiworld, _ = self._gen_w1_locked()
        logging.disable(logging.WARNING)
        try:
            distribute_items_restrictive(multiworld)
        finally:
            logging.disable(logging.NOTSET)
        self.assertTrue(multiworld.can_beat_game())


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


class TestOpenWorldBadgeWall(unittest.TestCase):
    """Open-world keeps the ``W3 4 Seeds`` Crouching High Jump wall.

    The mod only force-draws world-map roads on Petal Isles + the Castle, so
    inside W3 the road past POOF! Crouching High Jump I is vanilla: the Midway
    Trial and everything behind it need CHJ I cleared, which needs the badge.
    Stripping the badge from the gate in open-world let fill bury it past the
    wall (player-reported softlock: no badge, every earlier check done).
    """

    WALLED = "W3: The Midway Trial - Normal Exit"

    def test_wall_holds_without_badge(self):
        multiworld, world = _gen({"open_world": 1, "open_world_count": 6})
        self.assertIn(3, world.active_worlds)
        state = CollectionState(multiworld)
        state.collect(world.create_item(world_unlock_item(3)), True)
        for _ in range(10):
            state.collect(world.create_item("W3 Wonder Seed"), True)
        walled = multiworld.get_location(self.WALLED, 1)
        self.assertFalse(walled.can_reach(state),
                         "past the CHJ I wall must require the badge in open-world")
        state.collect(world.create_item("Crouching High Jump Badge"), True)
        self.assertTrue(walled.can_reach(state))

    def test_w3_only_seeds_solvable(self):
        # W3 alone is the tightest case: the badge and the first four W3 seeds
        # must all land in W3 Start.
        solved = 0
        for seed in range(1, 400):
            _, probe = _gen({"open_world": 1, "open_world_count": 1}, seed=seed)
            if probe.active_worlds != [3]:
                continue
            multiworld, _ = _gen({"open_world": 1, "open_world_count": 1}, seed=seed, fill=True)
            self.assertTrue(multiworld.can_beat_game(), f"W3-only seed {seed} unbeatable")
            solved += 1
            if solved == 3:
                break
        self.assertEqual(solved, 3, "expected three W3-only seeds in range")
