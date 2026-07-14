"""Generation tests for the character start + Character-Block gating.

Covers two coupled changes:

  * ``starting_items`` precollects exactly ONE, randomly-chosen base
    character (``data/game.json`` ``"random": 1``) instead of all seven;
    the other eleven characters go into the item pool to be found.

  * When ``character_block_sanity`` is on, each of the 154 "Character
    Block" checks requires its character in logic (``{OptOne(|Char|)}``
    baked into ``locations.json`` by ``scripts/romfs/build_charblock_table.py``),
    and every character item is marked progression (``data/items.json``)
    so the fill / beatability sweep collects them and can gate the blocks.
    A precollected starter's blocks stay reachable because OptOne clamps
    its requirement to the pool count (0 -> always-true).
"""
from __future__ import annotations

import logging
import unittest

from test.general import gen_steps, setup_multiworld
from Fill import distribute_items_restrictive

from .. import SMBWonderWorld

BASE_CHARACTERS = frozenset({
    "Mario", "Luigi", "Peach", "Daisy", "Yellow Toad", "Blue Toad", "Toadette",
})
EASY_CHARACTERS = frozenset({
    "Green Yoshi", "Red Yoshi", "Yellow Yoshi", "Light-Blue Yoshi", "Nabbit",
})
ALL_CHARACTERS = BASE_CHARACTERS | EASY_CHARACTERS


def _gen(options, seed=1234, fill=False):
    multiworld = setup_multiworld(SMBWonderWorld, gen_steps, seed=seed, options=options)
    if fill:
        logging.disable(logging.WARNING)  # quiet the surplus-pool trim warning
        try:
            distribute_items_restrictive(multiworld)
        finally:
            logging.disable(logging.NOTSET)
    return multiworld, multiworld.worlds[1]


class TestCharacterStart(unittest.TestCase):
    def _precollected_chars(self, multiworld):
        return [i.name for i in multiworld.precollected_items[1]
                if i.name in ALL_CHARACTERS]

    def test_exactly_one_base_character_precollected(self):
        # Try several seeds so we exercise different random picks.
        for seed in (1, 7, 42, 99, 2024):
            multiworld, _ = _gen({"character_block_sanity": 0}, seed=seed)
            pre = self._precollected_chars(multiworld)
            self.assertEqual(len(pre), 1, f"seed {seed}: expected one starting character")
            self.assertIn(pre[0], BASE_CHARACTERS,
                          f"seed {seed}: starter {pre[0]!r} must be a base character")

    def test_other_eleven_characters_in_pool(self):
        multiworld, _ = _gen({"character_block_sanity": 0}, seed=7)
        pre = set(self._precollected_chars(multiworld))
        pool_chars = {i.name for i in multiworld.itempool
                      if i.player == 1 and i.name in ALL_CHARACTERS}
        self.assertEqual(len(pool_chars), 11)
        self.assertEqual(pool_chars | pre, ALL_CHARACTERS,
                         "every character must be either precollected or in the pool")

    def test_characters_are_progression(self):
        # All character items carry progression so they can gate the
        # Character Block checks and are never trimmed as surplus filler.
        _, world = _gen({"character_block_sanity": 1}, seed=7)
        for item in world.multiworld.itempool:
            if item.player == 1 and item.name in ALL_CHARACTERS:
                self.assertTrue(item.advancement,
                                f"{item.name} should be progression")


class TestCharacterBlockGating(unittest.TestCase):
    def _char_block_locations(self, multiworld):
        return [l for r in multiworld.get_regions(1) for l in r.locations
                if l.name.endswith(" Block")]

    def test_blocks_present_only_when_sanity_on(self):
        on, _ = _gen({"character_block_sanity": 1}, seed=7)
        off, _ = _gen({"character_block_sanity": 0}, seed=7)
        self.assertEqual(len(self._char_block_locations(on)), 154)
        self.assertEqual(len(self._char_block_locations(off)), 0)

    def test_each_block_gates_on_its_character(self):
        # The access rule for "<Course> - <Char> Block" must depend on the
        # matching character item: with only the (single) starter held, a
        # block for a different, still-pooled character is unreachable.
        multiworld, world = _gen({"character_block_sanity": 1}, seed=7, fill=True)
        starter = next(i.name for i in multiworld.precollected_items[1]
                       if i.name in ALL_CHARACTERS)
        # Pick a block whose character is NOT the starter and assert it is
        # not reachable from an empty-handed (starter-only) state.
        from BaseClasses import CollectionState
        base_state = CollectionState(multiworld)
        blocks = self._char_block_locations(multiworld)
        # "Green Yoshi" blocks are named "... - Yoshi Block".
        def block_char(name: str) -> str:
            suffix = name.rsplit(" - ", 1)[-1][:-len(" Block")]
            return "Green Yoshi" if suffix == "Yoshi" else suffix
        foreign = [b for b in blocks if block_char(b.name) != starter]
        self.assertTrue(foreign, "expected blocks for non-starter characters")
        # At least one foreign-character block must be gated (unreachable
        # without that character) -- proving the requires actually bind.
        self.assertTrue(
            any(not b.can_reach(base_state) for b in foreign),
            "no Character Block was gated on its character")

    def test_beatable_both_modes(self):
        for sanity in (0, 1):
            for seed in (1, 42, 99):
                multiworld, _ = _gen({"character_block_sanity": sanity},
                                     seed=seed, fill=True)
                self.assertTrue(multiworld.can_beat_game(),
                                f"unbeatable: sanity={sanity} seed={seed}")


if __name__ == "__main__":
    unittest.main()
