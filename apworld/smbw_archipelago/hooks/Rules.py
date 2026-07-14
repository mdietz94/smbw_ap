"""Custom rule functions invokable from `requires` strings in
locations.json / regions.json via the `{func_name(args)}` DSL.

Nothing references these today — the `{OptOne(...)}` clauses left with the
button / Wonder Flower / Wonder Effect items (abandoned M3.5/M3.6 shuffles).
`OptOne` stays available for optional-item gating if the DSL is used again.
"""
from worlds.AutoWorld import World
from ..Helpers import clamp
from BaseClasses import MultiWorld, CollectionState


# Wraps an item reference (or category) into a |item:count| token, clamping
# the count to how many of that item actually exist in the pool.  This lets
# locations.json say "{OptOne(|Wonder Flower|)}" and gracefully degrade to
# "|Wonder Flower:0|" if the item was disabled by yaml options.
def OptOne(world: World, multiworld: MultiWorld, state: CollectionState, player: int, item: str, items_counts=None):
    """Check if the passed item (with or without ||) is enabled, then this returns |item:count|
    where count is clamped to the maximum number of said item in the itempool.\n
    Eg. requires: "{OptOne(|DisabledItem|)} and |other items|" become "|DisabledItem:0| and |other items|" if the item is disabled.
    """
    if item == "":
        return ""
    if not items_counts:
        items_counts = world.get_item_counts()

    require_type = 'item'

    if '@' in item[:2]:
        require_type = 'category'

    item = item.lstrip('|@$').rstrip('|')

    item_parts = item.split(":")
    item_name = item
    item_count = '1'

    if len(item_parts) > 1:
        item_name = item_parts[0]
        item_count = item_parts[1]

    if require_type == 'category':
        if item_count.isnumeric():
            category_items = [item for item in world.item_name_to_item.values() if "category" in item and item_name in item["category"]]
            category_items_counts = sum([items_counts.get(category_item["name"], 0) for category_item in category_items])
            item_count = clamp(int(item_count), 0, category_items_counts)
        return f"|@{item_name}:{item_count}|"
    elif require_type == 'item':
        if item_count.isnumeric():
            item_current_count = items_counts.get(item_name, 0)
            item_count = clamp(int(item_count), 0, item_current_count)
        return f"|{item_name}:{item_count}|"
