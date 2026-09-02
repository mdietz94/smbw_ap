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
from .ut_sim import regen_like_ut

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

    @staticmethod
    def _block_char(name: str) -> str:
        """'W5: Wubba Ruins - Peach Block' -> 'Peach'.  Green Yoshi's location
        is named '... - Yoshi Block'."""
        suffix = name.rsplit(" - ", 1)[-1][:-len(" Block")]
        return "Green Yoshi" if suffix == "Yoshi" else suffix

    @staticmethod
    def _state_with_everything_but_characters(multiworld):
        """A CollectionState holding every pooled item EXCEPT the character
        items, plus the precollected starter.

        Region gates (Wonder Seed tolls, badge walls) must be satisfied or
        almost no Character Block is reachable for any reason, and a test that
        just checks "not reachable" passes vacuously.
        """
        from BaseClasses import CollectionState
        state = CollectionState(multiworld)
        for item in multiworld.itempool:
            if item.player == 1 and item.name not in ALL_CHARACTERS:
                state.collect(item, prevent_sweep=True)
        for item in multiworld.precollected_items[1]:
            state.collect(item, prevent_sweep=True)
        return state

    def _ungated_block_chars(self, multiworld):
        """Characters whose blocks are reachable without holding them."""
        state = self._state_with_everything_but_characters(multiworld)
        return sorted({self._block_char(b.name)
                       for b in self._char_block_locations(multiworld)
                       if b.can_reach(state)})

    def test_each_block_gates_on_its_character(self):
        # The access rule for "<Course> - <Char> Block" must depend on the
        # matching character item: with only the (single) starter held, a
        # block for a different, still-pooled character is unreachable.
        multiworld, world = _gen({"character_block_sanity": 1}, seed=7, fill=True)
        starter = next(i.name for i in multiworld.precollected_items[1]
                       if i.name in ALL_CHARACTERS)
        self.assertTrue(self._char_block_locations(multiworld))
        # EVERY non-starter character's blocks must be gated on that character.
        # This used to assert any() against an empty-handed state, which passed
        # vacuously -- the regions weren't reachable, so nothing was.
        self.assertEqual(
            self._ungated_block_chars(multiworld), [starter],
            f"starter={starter}: only the starter's blocks may be ungated "
            f"(OptOne clamps the precollected character to |X:0|)")

    def test_item_counts_not_shared_across_generations(self):
        """`item_counts` / `start_inventory` must be per-World-instance.

        They are caches keyed by player NUMBER.  Held at class scope they leak
        into the next generation in the same process (WebHost worker reuse,
        Universal Tracker regen, batch generation), so generation 2's player 1
        reads generation 1's counts.

        Player-visible symptom: OptOne clamps each Character Block to the
        cached pool count, so the PREVIOUS seed's precollected starter reads 0
        copies -> |Char:0| -> always true, and all of that character's blocks
        sit in logic for a player who never had them.  Reported as "all Peach
        blocks were in logic when they shouldn't have been".

        Three generations with different starters, in one process.
        """
        seen_starters = set()
        for seed in (99, 7, 1234):
            multiworld, world = _gen({"character_block_sanity": 1}, seed=seed,
                                     fill=True)
            starter = next(i.name for i in multiworld.precollected_items[1]
                           if i.name in ALL_CHARACTERS)
            seen_starters.add(starter)
            # Only the CURRENT generation's starter may be ungated.
            self.assertEqual(
                self._ungated_block_chars(multiworld), [starter],
                f"seed {seed}: starter={starter} -- a different character's "
                f"blocks are ungated, so item_counts leaked from a prior "
                f"generation")
        self.assertGreater(
            len(seen_starters), 1,
            "test is vacuous: every seed rolled the same starter")

    def test_starter_pinned_for_universal_tracker(self):
        """UT regen must reproduce the ORIGINAL game's starter, not re-roll.

        The starter is a ``world.random`` roll in ``create_items``.  In a
        Universal Tracker single-player regeneration ``world.random`` diverges
        from the multi-player game, so a fresh roll picks a DIFFERENT base
        character -- and because every Character Block is ``{OptOne(|Char|)}``
        (clamped to ``|X:0|`` -> always-true for the precollected character),
        UT would show the wrong character's blocks as in-logic.

        ``fill_slot_data`` exports ``starting_characters``; UT's regen carries
        it over on ``multiworld.re_gen_passthrough`` and ``generate_early``
        pins it so ``create_items`` reproduces it exactly.  Driven through
        ``regen_like_ut`` because UT throws away the instance
        ``interpret_slot_data`` ran on.
        """
        # 1. Original game (seed 99) -> capture its starter from slot_data.
        mw_orig, world_orig = _gen({"character_block_sanity": 1}, seed=99)
        slot_data = world_orig.fill_slot_data()
        orig_starter = next(i.name for i in mw_orig.precollected_items[1]
                            if i.name in ALL_CHARACTERS)
        self.assertEqual(slot_data["starting_characters"], [orig_starter])

        # 2. A different seed rolls a different starter without the pin.
        #    Find one so the test isn't vacuous.
        for ut_seed in (7, 1234, 42, 1, 2024, 555):
            mw_probe, _ = _gen({"character_block_sanity": 1}, seed=ut_seed)
            if next(i.name for i in mw_probe.precollected_items[1]
                    if i.name in ALL_CHARACTERS) != orig_starter:
                break
        else:
            self.skipTest("no seed produced a different starter")

        # 3. Run UT's real flow on that other seed: interpret_slot_data on a
        #    throwaway world, then a full regeneration carrying only what
        #    interpret_slot_data returned.
        mw_ut, _ = regen_like_ut(slot_data, {"character_block_sanity": 1}, ut_seed)
        ut_starter = [i.name for i in mw_ut.precollected_items[1]
                      if i.name in ALL_CHARACTERS]
        self.assertEqual(ut_starter, [orig_starter],
                         "UT regen must reproduce the original game's starter")

        # 4. ...and the gating must follow the restored starter, not the roll.
        self.assertEqual(self._ungated_block_chars(mw_ut), [orig_starter])

    def test_interpret_slot_data_returns_slot_data_not_bool(self):
        """The return value is UT's only channel into the regenerated world.

        UT stashes it as ``multiworld.re_gen_passthrough[game]``; a bare
        ``True`` there carries nothing, so the regenerated world re-rolls its
        starter and open-world set.  Returning the slot_data is equally
        truthy, so it still triggers the regen.
        """
        _, world = _gen({"character_block_sanity": 1}, seed=99)
        slot_data = world.fill_slot_data()
        returned = world.interpret_slot_data(slot_data)
        self.assertIsInstance(returned, dict)
        self.assertEqual(returned.get("starting_characters"),
                         slot_data["starting_characters"])

    def test_beatable_both_modes(self):
        for sanity in (0, 1):
            for seed in (1, 42, 99):
                multiworld, _ = _gen({"character_block_sanity": sanity},
                                     seed=seed, fill=True)
                self.assertTrue(multiworld.can_beat_game(),
                                f"unbeatable: sanity={sanity} seed={seed}")


if __name__ == "__main__":
    unittest.main()
