"""Simulate a Universal Tracker regeneration the way UT actually does it.

UT (``worlds/tracker/TrackerCore.py::regen_slots``) does NOT hand the
connected slot's state to the world it ends up tracking with::

    temp = world.interpret_slot_data(slot_data)
    if temp:
        self.re_gen_passthrough = {self.game: temp}
        self.run_generator(slot_data, tempdir)

``run_generator`` builds a brand-new ``MultiWorld`` with brand-new ``World``
instances and sets ``multiworld.re_gen_passthrough`` on it before generation.
The instance ``interpret_slot_data`` ran on is discarded, so anything stashed
on ``self`` there is gone — the return value is the only channel that
survives, and the new world has to read it back off the passthrough.

Tests that only call ``interpret_slot_data`` and then re-run a gen step on the
SAME instance pass even when that channel is broken; use ``regen_like_ut``
instead.
"""
from __future__ import annotations

from typing import Any

from BaseClasses import CollectionState, MultiWorld
from test.general import gen_steps, setup_multiworld
from worlds.AutoWorld import call_all

from .. import SMBWonderWorld


def regen_like_ut(slot_data: dict[str, Any], options: dict[str, Any],
                  seed: int) -> tuple[MultiWorld, SMBWonderWorld]:
    """Run the full UT flow against ``slot_data`` and return the multiworld
    UT would actually track with.

    ``seed`` seeds both throwaway and final generations; pick one whose own
    random rolls differ from the ones recorded in ``slot_data`` so the test
    can tell a restored value from a coincidence.
    """
    # The multiworld UT generated from the player's yaml, and throws away.
    throwaway = setup_multiworld(SMBWonderWorld, gen_steps, seed=seed, options=options)
    passthrough = throwaway.worlds[1].interpret_slot_data(slot_data)
    assert passthrough, "interpret_slot_data must be truthy to trigger UT's regen"

    # run_generator: a fresh multiworld carrying the passthrough, generated
    # from scratch.  steps=() so we can install the passthrough first.
    multiworld = setup_multiworld(SMBWonderWorld, (), seed=seed, options=options)
    multiworld.re_gen_passthrough = {SMBWonderWorld.game: passthrough}
    multiworld.state = CollectionState(multiworld)
    for step in gen_steps:
        call_all(multiworld, step)
    return multiworld, multiworld.worlds[1]
