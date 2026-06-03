from worlds.AutoWorld import World
from BaseClasses import MultiWorld, CollectionState

# When you populate a hook below, you'll likely want these too:
#   from ..Items import SMBWonderItem
#   from ..Locations import SMBWonderLocation
#   from ..Data import game_table, item_table, location_table, region_table
#   from ..Helpers import is_option_enabled, get_option_value
from ..Items import SMBWonderItem

########################################################################################
## Order of method calls when the world generates:
##    1. create_regions - Creates regions and locations
##    2. create_items - Creates the item pool
##    3. set_rules - Creates rules for accessing regions and locations
##    4. generate_basic - Runs any post item pool options, like place item/category
##    5. pre_fill - Creates the victory location
##
## The create_item method is used by plando and start_inventory settings to create an item from an item name.
## The fill_slot_data method will be used to send data to the Manual client for later use, like deathlink.
########################################################################################



# Called before regions and locations are created. Not clear why you'd want this, but it's here. Victory location is included, but Victory event is not placed yet.
def before_create_regions(world: World, multiworld: MultiWorld, player: int):
    pass

# Called after regions and locations are created, in case you want to see or modify that information. Victory location is included.
def after_create_regions(world: World, multiworld: MultiWorld, player: int):
    # Open-world mode: detach the worlds from the linear spine and strip
    # the inactive-world / hub locations.  Runs after the __init__ victory
    # wiring (so the unused-goal removal there has already happened).
    if getattr(world, "open_world", False):
        from ..open_world import restructure_regions, strip_inactive_locations
        restructure_regions(world, multiworld, player, world.active_worlds)
        strip_inactive_locations(world, multiworld, player, world.active_worlds)

    # Use this hook to remove locations from the world
    locationNamesToRemove = [] # List of location names

    # Add your code here to calculate which locations to remove

    for region in multiworld.regions:
        if region.player == player:
            for location in list(region.locations):
                if location.name in locationNamesToRemove:
                    region.locations.remove(location)
    if hasattr(multiworld, "clear_location_cache"):
        multiworld.clear_location_cache()

# The item pool before starting items are processed, in case you want to see the raw item pool at that stage
def before_create_items_starting(item_pool: list, world: World, multiworld: MultiWorld, player: int) -> list:
    return item_pool

# The item pool after starting items are processed but before filler is added, in case you want to see the raw item pool at that stage
def before_create_items_filler(item_pool: list, world: World, multiworld: MultiWorld, player: int) -> list:
    # Open-world mode: drop the seed items for inactive worlds plus the
    # Petal Isles / Special World seeds (their locations were removed).
    if getattr(world, "open_world", False):
        from ..open_world import inactive_item_pool
        item_pool = inactive_item_pool(item_pool, world.active_worlds)

    # Use this hook to remove items from the item pool
    itemNamesToRemove = [] # List of item names

    # Add your code here to calculate which items to remove.
    #
    # Because multiple copies of an item can exist, you need to add an item name
    # to the list multiple times if you want to remove multiple copies of it.

    for itemName in itemNamesToRemove:
        item = next(i for i in item_pool if i.name == itemName)
        item_pool.remove(item)

    return item_pool

    # Some other useful hook options:

    ## Place an item at a specific location
    # location = next(l for l in multiworld.get_unfilled_locations(player=player) if l.name == "Location Name")
    # item_to_place = next(i for i in item_pool if i.name == "Item Name")
    # location.place_locked_item(item_to_place)
    # item_pool.remove(item_to_place)

# The complete item pool prior to being set for generation is provided here, in case you want to make changes to it
def after_create_items(item_pool: list, world: World, multiworld: MultiWorld, player: int) -> list:
    return item_pool

# Stashes the original region dicts that open-world neutralizes, keyed on
# the world instance, so after_set_rules can restore the shared regionMap.
_OW_REGION_BACKUP_ATTR = "_ow_region_requires_backup"


# Called before rules for accessing regions and locations are created. Not clear why you'd want this, but it's here.
def before_set_rules(world: World, multiworld: MultiWorld, player: int):
    # Open-world mode: drop the cross-world entry gates baked into each
    # active world's "Start" region and the impossible |@Royal Seed:6| on
    # World Bowser.  set_rules applies a region's `requires` to BOTH its
    # locations and its outgoing entrances, so leaving them would make
    # active worlds (and Bowser) permanently unreachable once the Petal
    # Isles / Special seeds are removed from the pool.
    #
    # We swap in neutralized *copies* before set_rules runs: its rule
    # closures capture `regionMap[region]` as a def-time default arg, so
    # they bind to the copy and keep reading the neutralized requires even
    # after after_set_rules restores the shared global.  This keeps the
    # change scoped to this player (no cross-slot bleed) while preserving
    # every per-location require (set_rules still ANDs those in).
    if not getattr(world, "open_world", False):
        return
    from ..Regions import regionMap
    from ..open_world import BOWSER_REGION

    names = [f"W{n} Start" for n in world.active_worlds] + [BOWSER_REGION]
    backup = {}
    for name in names:
        if name in regionMap:
            backup[name] = regionMap[name]
            regionMap[name] = {**regionMap[name], "requires": ""}
    setattr(world, _OW_REGION_BACKUP_ATTR, backup)

# Called after rules for accessing regions and locations are created, in case you want to see or modify that information.
def after_set_rules(world: World, multiworld: MultiWorld, player: int):
    # Open-world mode: restore the shared regionMap (the rule closures
    # already captured the neutralized copies in before_set_rules), then
    # gate Bowser on the active Royal-Seed count.  set_rules left the
    # Manual exits always-true (Manual has no requires); the Bowser gate
    # lives on the entrance because exit rules use the SOURCE region.
    if getattr(world, "open_world", False):
        from ..Regions import regionMap, getConnectionName
        from ..open_world import make_bowser_gate, BOWSER_REGION

        for name, original in getattr(world, _OW_REGION_BACKUP_ATTR, {}).items():
            regionMap[name] = original

        bowser = multiworld.get_entrance(getConnectionName("Manual", BOWSER_REGION), player)
        bowser.access_rule = make_bowser_gate(world.active_worlds, world.palaces_required, player)

    # Use this hook to modify the access rules for a given location

    def Example_Rule(state: CollectionState) -> bool:
        # Calculated rules take a CollectionState object and return a boolean
        # True if the player can access the location
        # CollectionState is defined in BaseClasses
        return True

    ## Common functions:
    # location = world.get_location(location_name, player)
    # location.access_rule = Example_Rule

    ## Combine rules:
    # old_rule = location.access_rule
    # location.access_rule = lambda state: old_rule(state) and Example_Rule(state)
    # OR
    # location.access_rule = lambda state: old_rule(state) or Example_Rule(state)

# The item name to create is provided before the item is created, in case you want to make changes to it
def before_create_item(item_name: str, world: World, multiworld: MultiWorld, player: int) -> str:
    return item_name

# The item that was created is provided after creation, in case you want to modify the item
def after_create_item(item: SMBWonderItem, world: World, multiworld: MultiWorld, player: int) -> SMBWonderItem:
    return item

# This method is run towards the end of pre-generation, before the place_item options have been handled and before AP generation occurs
def before_generate_basic(world: World, multiworld: MultiWorld, player: int) -> list:
    pass

# This method is run at the very end of pre-generation, once the place_item options have been handled and before AP generation occurs
def after_generate_basic(world: World, multiworld: MultiWorld, player: int):
    pass

# This is called before slot data is set and provides an empty dict ({}), in case you want to modify it before Manual does
def before_fill_slot_data(slot_data: dict, world: World, multiworld: MultiWorld, player: int) -> dict:
    return slot_data

# This is called after slot data is set and provides the slot data at the time, in case you want to check and modify it after Manual is done with it
def after_fill_slot_data(slot_data: dict, world: World, multiworld: MultiWorld, player: int) -> dict:
    # Open-world mode: the active-world set isn't an option (it's chosen at
    # random in generate_early), so inject it explicitly for the client.
    if getattr(world, "open_world", False):
        slot_data["open_world_active"] = list(world.active_worlds)
        slot_data["palaces_required"] = world.palaces_required
    return slot_data

# This is called right at the end, in case you want to write stuff to the spoiler log
def before_write_spoiler(world: World, multiworld: MultiWorld, spoiler_handle) -> None:
    pass
