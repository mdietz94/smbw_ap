"""Game metadata for the SMBWonder Archipelago world.

Renamed from the Manual_SMBWonder_Zim template: the literal `SMBWonder`
game name replaces the Manual-template-derived `Manual_<game>_<creator>`
slug.  starting_index is still derived from the game name characters so
that item/location IDs remain in a stable, non-colliding pool.
"""
from .Data import game_table

game_name = "Super Mario Bros Wonder"
filler_item_name = game_table["filler_item_name"] if "filler_item_name" in game_table else "Filler"
starting_items = game_table["starting_items"] if "starting_items" in game_table else None

# Hardcoded to "SMBWonder" so the starting_index (and therefore every item /
# location ID) is invariant under display-name changes.  data/game.json still
# carries "game": "SMBWonder" for backwards compat with the Manual template,
# but the user-visible name is set above.
_id_seed = "SMBWonder"
starting_index = (ord(_id_seed[:1]) * 100000000) + \
    (ord(_id_seed[1:2]) * 70000000) + \
    (ord(_id_seed[-1:]) * 10000000)

if len(_id_seed) > 3:
    for index in range(2, len(_id_seed) - 1):
        starting_index += (ord(_id_seed[index:index+1]) * 100000)

# Folded in the legacy creator string so id offsets match Manual_SMBWonder_Zim.
for index in range(0, len(game_table.get("creator", ""))):
    starting_index += (ord(game_table["creator"][index:index+1]) * 1000)
