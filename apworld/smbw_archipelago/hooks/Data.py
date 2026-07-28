"""Hooks for transforming raw JSON data files after load, and for
restoring extra state from slot_data (e.g. Universal Tracker).  All
hooks currently no-op."""


def after_load_game_file(game_table: dict) -> dict:
    return game_table


def after_load_item_file(item_table: list) -> list:
    return item_table


def after_load_location_file(location_table: list) -> list:
    return location_table


def after_load_region_file(region_table: dict) -> dict:
    return region_table


def after_load_category_file(category_table: dict) -> dict:
    return category_table


def after_load_meta_file(meta_table: dict) -> dict:
    return meta_table


# Universal Tracker compatibility — return True from this hook if you
# mutated the world such that AP needs to regenerate state.
def hook_interpret_slot_data(world, player: int, slot_data: dict) -> bool:
    regen = False
    if "open_world_active" in slot_data:
        # Pin the active worlds from slot_data so generate_early won't
        # re-roll them via world.random.  In a Universal Tracker
        # single-player regeneration the RNG state at generate_early time
        # differs from the original multi-player game, so re-rolling would
        # silently select the wrong worlds and cause UT to see only the
        # first world as available (the others aren't wired into Manual).
        world._ow_pinned_active_worlds = [int(n) for n in slot_data["open_world_active"]]
        regen = True
    if "starting_characters" in slot_data:
        # Pin the precollected starter character(s) for the same reason: the
        # starter is a world.random roll in create_items (starting_items
        # "random": 1), and world.random diverges in a UT single-player
        # regeneration.  Without this, UT re-rolls a DIFFERENT starter, and
        # since every Character Block is `{OptOne(|Char|)}` (clamped to |X:0|
        # for the precollected character -> always-true), the wrong
        # character's blocks show as in-logic.
        world._pinned_starting_characters = list(slot_data["starting_characters"])
        regen = True
    return regen
