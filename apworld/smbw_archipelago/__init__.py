"""SMBWonder Archipelago World.

Replaces the Manual_SMBWonder_Zim template world with a first-class
SMBWonder world.  The world definition (items, locations, regions, rules)
is largely the same as the prior Manual world — same data files, same
rules-DSL — but the `game` slug is now `SMBWonder` so that the SMBW
Client (Kivy, see `client/`) can advertise it to Archipelago server.

The module-level `add_client_to_launcher()` call registers the "SMBW
Client" Component with Archipelago Launcher so the user can spawn it
with a button click.
"""
# Stamped by .github/workflows/release.yml on tag push (e.g. "0.1.0-alpha1").
# Dev builds and PR CI keep the +dev suffix so user-reported logs are
# unambiguous about whether a release artifact or a dev checkout was running.
__version__ = "0.0.0+dev"

import logging
from pathlib import Path
from typing import Optional

from worlds.generic.Rules import forbid_items_for_player
from worlds.LauncherComponents import (
    Component, SuffixIdentifier, components, Type,
    launch as launch_component,
    launch_subprocess,
)

from .Data import item_table, location_table, category_table
from .Game import game_name, filler_item_name, starting_items
from .Meta import world_description, world_webworld, enable_region_diagram
from .Locations import location_id_to_name, location_name_to_id, location_name_to_location, location_name_groups, victory_names
from .Items import item_id_to_name, item_name_to_id, item_name_to_item, item_name_groups
from .DataValidation import runGenerationDataValidation, runPreFillDataValidation

from .Regions import create_regions
from .Items import SMBWonderItem
from .Rules import set_rules
from .Options import smbwonder_options_data
from .Helpers import is_item_enabled, get_option_value

from BaseClasses import ItemClassification, Item
from Options import PerGameCommonOptions
from worlds.AutoWorld import World

from ._setup.launcher_errors import visible_errors as _visible_errors

from .hooks.World import \
    before_create_regions, after_create_regions, \
    before_create_items_starting, before_create_items_filler, after_create_items, \
    before_create_item, after_create_item, \
    before_set_rules, after_set_rules, \
    before_generate_basic, after_generate_basic, \
    before_fill_slot_data, after_fill_slot_data, before_write_spoiler
from .hooks.Data import hook_interpret_slot_data


class SMBWonderWorld(World):
    __doc__ = world_description
    game: str = game_name
    web = world_webworld

    options_dataclass = smbwonder_options_data
    data_version = 2
    required_client_version = (0, 3, 4)

    item_table = item_table
    location_table = location_table
    category_table = category_table

    item_id_to_name = item_id_to_name
    item_name_to_id = item_name_to_id
    item_name_to_item = item_name_to_item
    item_name_groups = item_name_groups

    item_counts = {}
    start_inventory = {}

    location_id_to_name = location_id_to_name
    location_name_to_id = location_name_to_id
    location_name_to_location = location_name_to_location
    location_name_groups = location_name_groups
    victory_names = victory_names

    def interpret_slot_data(self, slot_data: dict[str, any]):
        regen = False
        for key, value in slot_data.items():
            if key in self.options_dataclass.type_hints:
                getattr(self.options, key).value = value
                regen = True

        regen = hook_interpret_slot_data(self, self.player, slot_data) or regen
        return regen

    @classmethod
    def stage_assert_generate(cls, multiworld) -> None:
        runGenerationDataValidation()

    def generate_early(self) -> None:
        """Resolve open-world settings before regions/items/rules are
        built.  Always sets ``self.open_world`` / ``self.active_worlds`` /
        ``self.palaces_required`` so the World.py hooks can read them
        unconditionally.  When open-world is on, picks the random active
        worlds (seeded via ``self.random``), resolves the palace
        threshold, writes it back to the option (so ``fill_slot_data``
        exports the resolved value), and forces the goal to Bowser."""
        from . import open_world as ow

        self.open_world = bool(get_option_value(self.multiworld, self.player, "open_world"))
        self.active_worlds = list(ow.WORLD_NUMBERS)
        self.palaces_required = len(self.active_worlds)
        if not self.open_world:
            return

        self.active_worlds = ow.choose_active_worlds(self)
        self.palaces_required = ow.resolve_palaces_required(self, self.active_worlds)
        self.options.palaces_required.value = self.palaces_required

        if ow.BOWSER_VICTORY_LOCATION in victory_names:
            self.options.goal.value = victory_names.index(ow.BOWSER_VICTORY_LOCATION)

    def create_regions(self):
        before_create_regions(self, self.multiworld, self.player)

        create_regions(self, self.multiworld, self.player)

        location_game_complete = self.multiworld.get_location(victory_names[get_option_value(self.multiworld, self.player, 'goal')], self.player)
        location_game_complete.address = None

        for unused_goal in [self.multiworld.get_location(name, self.player) for name in victory_names if name != location_game_complete.name]:
            unused_goal.parent_region.locations.remove(unused_goal)

        location_game_complete.place_locked_item(
            SMBWonderItem("__Victory__", ItemClassification.progression, None, player=self.player))

        after_create_regions(self, self.multiworld, self.player)

    def create_items(self):
        pool = []
        traps = []
        configured_item_names = self.item_id_to_name.copy()

        for name in configured_item_names.values():
            if name == "__Victory__": continue
            if name == filler_item_name: continue

            item = self.item_name_to_item[name]
            item_count = int(item.get("count", 1))

            if item.get("trap"):
                traps.append(name)

            if "category" in item:
                if not is_item_enabled(self.multiworld, self.player, item):
                    item_count = 0

            if item_count == 0: continue

            for i in range(item_count):
                new_item = self.create_item(name)
                pool.append(new_item)

            if item.get("early"):
                self.multiworld.early_items[self.player][name] = item_count
            if item.get("local"):
                if name not in self.multiworld.local_items[self.player].value:
                    self.options.local_items.value.add(name)

        pool = before_create_items_starting(pool, self, self.multiworld, self.player)

        items_started = []

        if starting_items:
            for starting_item_block in starting_items:
                if "if_previous_item" in starting_item_block:
                    matching_items = [item for item in items_started if item.name in starting_item_block["if_previous_item"]]

                    if len(matching_items) == 0:
                        continue

                items = pool

                if "items" in starting_item_block:
                    items = [item for item in pool if item.name in starting_item_block["items"]]

                if "item_categories" in starting_item_block:
                    items_in_categories = [item["name"] for item in self.item_name_to_item.values() if "category" in item and len(set(starting_item_block["item_categories"]).intersection(item["category"])) > 0]
                    items = [item for item in pool if item.name in items_in_categories]

                self.random.shuffle(items)

                if "random" in starting_item_block:
                    items = items[0:starting_item_block["random"]]

                for starting_item in items:
                    items_started.append(starting_item)
                    self.multiworld.push_precollected(starting_item)
                    pool.remove(starting_item)

        self.start_inventory = {i.name: items_started.count(i) for i in items_started}

        pool = before_create_items_filler(pool, self, self.multiworld, self.player)
        pool = self.adjust_filler_items(pool, traps)
        pool = after_create_items(pool, self, self.multiworld, self.player)

        self.multiworld.itempool += pool

    def create_item(self, name: str) -> Item:
        name = before_create_item(name, self, self.multiworld, self.player)

        item = self.item_name_to_item[name]
        classification = ItemClassification.filler

        if "trap" in item and item["trap"]:
            classification = ItemClassification.trap

        if "useful" in item and item["useful"]:
            classification = ItemClassification.useful

        if "progression" in item and item["progression"]:
            classification = ItemClassification.progression

        if "progression_skip_balancing" in item and item["progression_skip_balancing"]:
            classification = ItemClassification.progression_skip_balancing

        item_object = SMBWonderItem(name, classification,
                        self.item_name_to_id[name], player=self.player)

        item_object = after_create_item(item_object, self, self.multiworld, self.player)

        return item_object

    def set_rules(self):
        before_set_rules(self, self.multiworld, self.player)

        set_rules(self, self.multiworld, self.player)

        after_set_rules(self, self.multiworld, self.player)

    def generate_basic(self):
        before_generate_basic(self, self.multiworld, self.player)

        smbw_locations_with_forbid = {location['name']: location for location in location_name_to_location.values() if "dont_place_item" in location or "dont_place_item_category" in location}
        locations_with_forbid = [l for l in self.multiworld.get_unfilled_locations(player=self.player) if l.name in smbw_locations_with_forbid.keys()]
        for location in locations_with_forbid:
            smbw_location = smbw_locations_with_forbid[location.name]
            forbidden_item_names = []

            if "dont_place_item" in smbw_location:
                if len(smbw_location["dont_place_item"]) == 0:
                    continue

                forbidden_item_names.extend([i["name"] for i in item_name_to_item.values() if i["name"] in smbw_location["dont_place_item"]])

            if "dont_place_item_category" in smbw_location:
                if len(smbw_location["dont_place_item_category"]) == 0:
                    continue

                forbidden_item_names.extend([i["name"] for i in item_name_to_item.values() if "category" in i and set(i["category"]).intersection(smbw_location["dont_place_item_category"])])

            if len(forbidden_item_names) > 0:
                forbid_items_for_player(location, forbidden_item_names, self.player)
                forbidden_item_names.clear()

        smbw_locations_with_placements = {location['name']: location for location in location_name_to_location.values() if "place_item" in location or "place_item_category" in location}
        locations_with_placements = [l for l in self.multiworld.get_unfilled_locations(player=self.player) if l.name in smbw_locations_with_placements.keys()]
        for location in locations_with_placements:
            smbw_location = smbw_locations_with_placements[location.name]
            eligible_items = []

            if "place_item" in smbw_location:
                if len(smbw_location["place_item"]) == 0:
                    continue

                eligible_items = [item for item in self.multiworld.itempool if item.name in smbw_location["place_item"] and item.player == self.player]

                if len(eligible_items) == 0:
                    raise Exception("Could not find a suitable item to place at %s. No items that match %s." % (smbw_location["name"], ", ".join(smbw_location["place_item"])))

            if "place_item_category" in smbw_location:
                if len(smbw_location["place_item_category"]) == 0:
                    continue

                eligible_item_names = [i["name"] for i in item_name_to_item.values() if "category" in i and set(i["category"]).intersection(smbw_location["place_item_category"])]
                eligible_items = [item for item in self.multiworld.itempool if item.name in eligible_item_names and item.player == self.player]

                if len(eligible_items) == 0:
                    raise Exception("Could not find a suitable item to place at %s. No items that match categories %s." % (smbw_location["name"], ", ".join(smbw_location["place_item_category"])))

            if "dont_place_item" in smbw_location:
                if len(smbw_location["dont_place_item"]) == 0:
                    continue

                eligible_items = [item for item in eligible_items if item.name not in smbw_location["dont_place_item"]]

                if len(eligible_items) == 0:
                    raise Exception("Could not find a suitable item to place at %s. No items that match placed_items(_category) because of forbidden %s." % (smbw_location["name"], ", ".join(smbw_location["dont_place_item"])))

            if "dont_place_item_category" in smbw_location:
                if len(smbw_location["dont_place_item_category"]) == 0:
                    continue

                forbidden_item_names = [i["name"] for i in item_name_to_item.values() if "category" in i and set(i["category"]).intersection(smbw_location["dont_place_item_category"])]

                eligible_items = [item for item in eligible_items if item.name not in forbidden_item_names]

                if len(eligible_items) == 0:
                    raise Exception("Could not find a suitable item to place at %s. No items that match placed_items(_category) because of forbidden categories %s." % (smbw_location["name"], ", ".join(smbw_location["dont_place_item_category"])))
                forbidden_item_names.clear()

            if len(eligible_items) == 0:
                raise Exception("Custom item placement at location %s failed." % (smbw_location["name"]))

            item_to_place = self.random.choice(eligible_items)
            location.place_locked_item(item_to_place)

            self.multiworld.itempool.remove(item_to_place)


        after_generate_basic(self, self.multiworld, self.player)

        if enable_region_diagram:
            from Utils import visualize_regions
            visualize_regions(self.multiworld.get_region("Menu", self.player), f"{self.game}_{self.player}.puml")

    def pre_fill(self):
        runPreFillDataValidation(self, self.multiworld)

    def fill_slot_data(self):
        slot_data = before_fill_slot_data({}, self, self.multiworld, self.player)

        common_options = set(PerGameCommonOptions.type_hints.keys())
        for option_key, _ in self.options_dataclass.type_hints.items():
            if option_key in common_options:
                continue
            slot_data[option_key] = get_option_value(self.multiworld, self.player, option_key)

        # Resolve the integer `goal` option to its location name so the
        # client doesn't have to mirror victory_names ordering.  The
        # location's address has already been wiped (see create_regions),
        # so the bridge uses this name to recognize when an outgoing
        # CheckEmitted is the player's chosen goal and convert it into a
        # StatusUpdate(CLIENT_GOAL).
        goal_idx = get_option_value(self.multiworld, self.player, 'goal')
        if isinstance(goal_idx, int) and 0 <= goal_idx < len(victory_names):
            slot_data["goal_location_name"] = victory_names[goal_idx]

        slot_data = after_fill_slot_data(slot_data, self, self.multiworld, self.player)

        return slot_data

    def write_spoiler(self, spoiler_handle):
        before_write_spoiler(self, self.multiworld, spoiler_handle)

    ###
    # Non-standard AP world methods
    ###

    def adjust_filler_items(self, item_pool, traps):
        extras = len(self.multiworld.get_unfilled_locations(player=self.player)) - len(item_pool)

        if extras > 0:
            trap_percent = get_option_value(self.multiworld, self.player, "filler_traps")
            if not traps:
                trap_percent = 0

            trap_count = extras * trap_percent // 100
            filler_count = extras - trap_count

            for _ in range(0, trap_count):
                extra_item = self.create_item(self.random.choice(traps))
                item_pool.append(extra_item)

            for _ in range(0, filler_count):
                extra_item = self.create_item(filler_item_name)
                item_pool.append(extra_item)
        elif extras < 0:
            logging.warning(f"{self.game} has more items than locations. {abs(extras)} non-progression items will be removed at random.")
            fillers = [item for item in item_pool if item.classification == ItemClassification.filler]
            traps = [item for item in item_pool if item.classification == ItemClassification.trap]
            useful = [item for item in item_pool if item.classification == ItemClassification.useful]
            self.random.shuffle(fillers)
            self.random.shuffle(traps)
            self.random.shuffle(useful)
            for _ in range(0, abs(extras)):
                popped = None
                if fillers:
                    popped = fillers.pop()
                elif traps:
                    popped = traps.pop()
                elif useful:
                    popped = useful.pop()
                else:
                    logging.warning("Could not remove enough non-progression items from the pool.")
                    break
                item_pool.remove(popped)

        return item_pool

    def get_item_counts(self, player: Optional[int] = None, reset: bool = False) -> dict[str, int]:
        """returns the player real item count"""
        if player is None:
            player = self.player
        if not self.item_counts.get(player, {}) or reset:
            real_pool = self.multiworld.get_items()
            self.item_counts[player] = {i.name: real_pool.count(i) for i in real_pool if i.player == player}
        return self.item_counts.get(player)


###
# Launcher Component registration
###

@_visible_errors("SMBW Client launcher")
def launch_smbw_client(*launch_args: str) -> None:
    """Archipelago Launcher click handler — spawns the SMBW Client.

    Triggered by double-clicking a `.smbwap` file (the Component's
    `SuffixIdentifier('.smbwap')` registers the extension globally) or by
    clicking the "SMBW Client" button directly. Always launches SMBW
    Client; when a `.smbwap` is provided its slot_name / server_address /
    password are expanded into CLI overrides so the Connect bar lands
    pre-filled.

    The setup wizard (toolchain install, junction, build + deploy) is
    NOT auto-triggered here. Users invoke it via the `/setup` slash
    command inside SMBW Client — that path covers both first-time setup
    and re-runs (toolchain update, apworld update, switching deploy
    targets).
    """
    smbwap_path = next((a for a in launch_args if a.endswith(".smbwap")), None)
    final_args: list[str] = list(launch_args)
    if smbwap_path:
        try:
            from ._setup.smbwap_file import parse_smbwap, smbwap_to_launch_args
            s = parse_smbwap(Path(smbwap_path))
            # Drop the .smbwap arg itself (SMBW Client's argparser
            # doesn't know about it) and prepend the expanded credentials.
            final_args = [a for a in final_args if not a.endswith(".smbwap")]
            final_args = smbwap_to_launch_args(s) + final_args
        except Exception as e:
            # Don't block the launch — surface the failure so the user
            # (and we, if they report) can see why their .smbwap didn't
            # pre-fill, then let SMBW Client open with the Connect bar
            # blank. logging.warning alone would vanish into a void
            # since the Launcher has no console attached on
            # file-association invocations.
            from ._setup.launcher_errors import show_launch_warning
            show_launch_warning(
                f".smbwap could not be parsed ({Path(smbwap_path).name}) — "
                "SMBW Client will open without pre-fill; enter your slot "
                "name manually in the Connect bar.",
                e,
            )
            final_args = [a for a in final_args if not a.endswith(".smbwap")]

    from .client.main import launch
    launch_component(launch, name="SMBW Client", args=tuple(final_args))


@_visible_errors("Setup wizard")
def _run_setup_wizard() -> None:
    """Module-level subprocess entry: open the setup wizard.

    Invoked by the `/setup` slash command in SMBW Client (which goes
    through `launch_subprocess` so SMBW Client stays open while the
    wizard runs in its own window). The wizard handles first-time
    setup and re-runs alike — toolchain bumps, apworld updates,
    switching deploy targets."""
    from ._setup.wizard import run_setup_wizard
    run_setup_wizard()


def add_client_to_launcher() -> None:
    """Register the "SMBW Client" Component with the Archipelago Launcher.

    Idempotent: re-importing this module (e.g. AP's apworld autodiscover
    can call us more than once across reloads) won't create duplicates."""
    for c in components:
        if c.display_name == "SMBW Client":
            return
    components.append(Component(
        "SMBW Client",
        func=launch_smbw_client,
        component_type=Type.CLIENT,
        file_identifier=SuffixIdentifier('.smbwap'),
        game_name=game_name,
    ))


add_client_to_launcher()
