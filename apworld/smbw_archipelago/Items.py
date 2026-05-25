from BaseClasses import Item
from .Data import item_table
from .Game import filler_item_name, starting_index
from .hooks.Items import before_item_table_processed

item_table = before_item_table_processed(item_table)

item_id_to_name: dict[int, str] = {}
item_name_to_item: dict[str, dict] = {}
item_name_groups: dict[str, str] = {}

count = starting_index

if filler_item_name:
    item_table.append({
        "name": filler_item_name
    })

for key, val in enumerate(item_table):
    item_table[key]["id"] = count
    item_table[key]["progression"] = val["progression"] if "progression" in val else False
    count += 1

for item in item_table:
    item_name = item["name"]
    item_id_to_name[item["id"]] = item_name
    item_name_to_item[item_name] = item

    for c in item.get("category", []):
        if c not in item_name_groups:
            item_name_groups[c] = []
        item_name_groups[c].append(item_name)

    for v in item.get("value", {}).keys():
        group_name = f"has_{v.lower().strip()}_value"
        if group_name not in item_name_groups:
            item_name_groups[group_name] = []
        item_name_groups[group_name].append(item_name)

item_id_to_name[None] = "__Victory__"
item_name_to_id = {name: id for id, name in item_id_to_name.items()}


class SMBWonderItem(Item):
    game = "SMBWonder"
