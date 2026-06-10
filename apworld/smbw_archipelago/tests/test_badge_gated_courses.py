"""Generation tests for the ``badge_gated_courses`` option.

Verifies that gating an arbitrary course's entry on a badge:
  * adds the badge requirement to *every* check in that course,
  * leaves ungated courses untouched,
  * still fills + is beatable (the badge is a progression item AP places
    before it expects you in the gated course),
  * supports multiple badges (AND) per course, and
  * aborts generation on a misspelled course or badge name.
"""
from __future__ import annotations

import logging
import unittest

from BaseClasses import CollectionState
from test.general import gen_steps, setup_multiworld
from Fill import distribute_items_restrictive

from .. import SMBWonderWorld
from ..DataValidation import ValidationError


def _gen(options, seed=2025, fill=False):
    multiworld = setup_multiworld(SMBWonderWorld, gen_steps, seed=seed, options=options)
    if fill:
        logging.disable(logging.WARNING)
        try:
            distribute_items_restrictive(multiworld)
        finally:
            logging.disable(logging.NOTSET)
    return multiworld, multiworld.worlds[1]


def _course_locations(multiworld, player, course):
    out = []
    for region in multiworld.regions:
        if region.player != player:
            continue
        for loc in region.locations:
            if loc.name.rsplit(" - ", 1)[0] == course:
                out.append(loc)
    return out


class TestBadgeGatedCourses(unittest.TestCase):
    # A course with no vanilla badge gate, picked so the effect is observable.
    COURSE = "W1: Piranha Plants on Parade"
    BADGE = "Spring Feet Badge"

    def test_gate_blocks_without_badge_and_opens_with_it(self):
        mw, _ = _gen({"badge_gated_courses": {self.COURSE: self.BADGE}})
        locs = _course_locations(mw, 1, self.COURSE)
        self.assertTrue(locs, "expected the gated course to have locations")

        # A maxed state that lacks ONLY the gating badge must not reach the
        # course; granting the badge must flip every check reachable-wise.
        without = CollectionState(mw)
        for item in mw.get_items():
            if item.player == 1 and item.name != self.BADGE:
                without.collect(item, prevent_sweep=True)
        for loc in locs:
            self.assertFalse(
                loc.can_reach(without),
                f"{loc.name} should be blocked without {self.BADGE}")

        with_badge = without.copy()
        badge_item = next(i for i in mw.get_items()
                          if i.player == 1 and i.name == self.BADGE)
        with_badge.collect(badge_item, prevent_sweep=True)
        for loc in locs:
            self.assertTrue(
                loc.can_reach(with_badge),
                f"{loc.name} should be reachable once {self.BADGE} is held")

    def test_ungated_course_untouched(self):
        mw, _ = _gen({"badge_gated_courses": {self.COURSE: self.BADGE}})
        other = "W1: Welcome to the Flower Kingdom!"
        locs = _course_locations(mw, 1, other)
        full = CollectionState(mw)
        for item in mw.get_items():
            if item.player == 1 and item.name != self.BADGE:
                full.collect(item, prevent_sweep=True)
        # Welcome to the Flower Kingdom is the W1 opener; with everything but
        # the Spring Feet badge it must still be reachable.
        for loc in locs:
            self.assertTrue(loc.can_reach(full),
                            f"{loc.name} must not be affected by gating {self.COURSE}")

    def test_multiple_badges_all_required(self):
        course = "W2: Outmaway Valley"
        badges = ["Dolphin Kick Badge", "Floating High Jump Badge"]
        mw, _ = _gen({"badge_gated_courses": {course: badges}})
        locs = _course_locations(mw, 1, course)
        self.assertTrue(locs)

        # Holding only ONE of the two badges is not enough.
        partial = CollectionState(mw)
        for item in mw.get_items():
            if item.player == 1 and item.name != badges[1]:
                partial.collect(item, prevent_sweep=True)
        self.assertFalse(locs[0].can_reach(partial),
                         "one of two required badges must not suffice")

    def test_fill_and_beatable(self):
        mw, _ = _gen(
            {"badge_gated_courses": {self.COURSE: self.BADGE,
                                     "W2: Outmaway Valley": "Grappling Vine Badge"}},
            fill=True)
        self.assertTrue(mw.can_beat_game(),
                        "seed with badge-gated courses must be beatable")

    def test_empty_option_is_noop(self):
        mw, _ = _gen({"badge_gated_courses": {}}, fill=True)
        self.assertTrue(mw.can_beat_game())

    def test_unknown_course_aborts(self):
        with self.assertRaises((ValidationError, Exception)):
            _gen({"badge_gated_courses": {"Not A Real Course": self.BADGE}})

    def test_unknown_badge_aborts(self):
        with self.assertRaises((ValidationError, Exception)):
            _gen({"badge_gated_courses": {self.COURSE: "Nonexistent Badge"}})


if __name__ == "__main__":
    unittest.main()
