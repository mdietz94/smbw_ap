from BaseClasses import Location
from .Data import location_table
from .Game import starting_index
from .hooks.Locations import before_location_table_processed

location_table = before_location_table_processed(location_table)

count = starting_index + 500
victory_names: list[str] = []

for key, _ in enumerate(location_table):
    if "victory" in location_table[key] and location_table[key]["victory"]:
        victory_names.append(location_table[key]["name"])

    location_table[key]["id"] = count

    if not "region" in location_table[key]:
        location_table[key]["region"] = "Manual"

    count += 1

if not victory_names:
    location_table.append({
        "id": count + 1,
        "name": "__SMBWonder Game Complete__",
        "region": "Manual",
        "requires": []
    })
    victory_names.append("__SMBWonder Game Complete__")

location_id_to_name: dict[int, str] = {}
location_name_to_location: dict[str, dict] = {}
location_name_groups: dict[str, list[str]] = {}

for item in location_table:
    location_id_to_name[item["id"]] = item["name"]
    location_name_to_location[item["name"]] = item

    for c in item.get("category", []):
        if c not in location_name_groups:
            location_name_groups[c] = []
        location_name_groups[c].append(item["name"])


location_name_to_id = {name: id for id, name in location_id_to_name.items()}


class SMBWonderLocation(Location):
    game = "Super Mario Bros Wonder"
