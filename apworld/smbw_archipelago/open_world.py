"""Open-world mode for SMBWonder.

Vanilla SMBWonder is a single linear spine threaded through the Petal
Isles (PI) hub: ``W1 -> PI -> W2 -> ... -> W6 -> Bowser``, gated by
per-world Wonder-Seed counts and the previous world's Royal Seed (see
``data/regions.json``).  Open-world mode keeps each world's *internal*
Wonder-Seed progression but detaches the worlds from each other: a random
set of N worlds (``open_world_count``) all hang directly off the ``Manual``
menu region and are reachable from the start, and Bowser is reached
directly off ``Manual`` once enough palaces (Royal Seeds) are cleared.

The restructuring is done by mutating this player's in-memory region graph
(``Regions.create_regions`` has already built the full vanilla graph by the
time we run) rather than editing the shared module-global ``regionMap`` /
``regions.json`` — that table is imported once and reused across every
slot, so editing it would bleed between players.

Region/exit/location registers are cache-backed (``BaseClasses.Region``),
so ``region.exits.remove(...)`` / ``region.locations.remove(...)`` keep the
``entrance_cache`` / ``location_cache`` consistent.  Whole-region removal is
*not* cleanly supported, so unselected worlds and hub regions are isolated
(exits severed, locations stripped) and left as empty islands rather than
deleted — harmless, since AP fill only considers locations and reachable
regions.
"""
from worlds.AutoWorld import World
from BaseClasses import MultiWorld

WORLD_NUMBERS = (1, 2, 3, 4, 5, 6)

# Region holding the (forced) Bowser victory location.
BOWSER_REGION = "World Bowser"

# Goal location open-world forces; must exist in Locations.victory_names.
BOWSER_VICTORY_LOCATION = "PI: Bowser's Rage Stage - Royal Seed"

# Hub / Special-World spine regions that are not prefixed "PI ".
_EXTRA_HUB_REGIONS = frozenset({
    "Pre-W4 Special",
    "Special End",
    "Post-Badge",
})

# Wonder-Seed item names for content that open-world always strips (the
# Petal Isles + Special World seeds live only in removed hub regions).
_ALWAYS_REMOVE_ITEMS = frozenset({
    "Petal Isles Wonder Seed",
    "Special World Wonder Seed",
})


def world_of_region(name: str):
    """World number (1-6) a region belongs to, or None for menu / hub /
    Bowser regions.  World regions are named ``"W<n> ..."``."""
    if len(name) >= 3 and name[0] == "W" and name[1].isdigit() and name[2] == " ":
        return int(name[1])
    return None


def is_hub_region(name: str) -> bool:
    """True for Petal Isles / Special-World spine regions removed in
    open-world mode.  ``World Bowser`` is *not* a hub region (it holds the
    goal and is kept)."""
    return name.startswith("PI ") or name in _EXTRA_HUB_REGIONS


def _world_of_seed_item(name: str):
    """World number for a ``"W<n> Royal Seed"`` / ``"W<n> Wonder Seed"``
    item, else None."""
    if name.endswith(" Royal Seed") or name.endswith(" Wonder Seed"):
        return world_of_region(name)
    return None


def royal_seed_item(n: int) -> str:
    return f"W{n} Royal Seed"


# Petal Isles has 34 Wonder Seeds (data/items.json).  In open-world the hub's
# inter-world routes are gated by a cumulative Petal-Isles Wonder-Seed count.
PETAL_WONDER_SEED_ITEM = "Petal Isles Wonder Seed"


def precollect_petal_wonder_seeds(world, multiworld, player) -> int:
    """Open-world: grant the player every Petal Isles Wonder Seed at start.

    PI's hub map gates the routes to each world behind a cumulative Wonder-Seed
    count (the world-map "seed bar" gates that block PI -> world travel).  The
    client buckets received PI Wonder Seeds into the Petal-Isles count it
    pushes to the Switch, which the in-game gate predicate reads -- so granting
    all of them opens every PI route from the start, letting the player walk to
    each active world's entrance on foot.  (The byte-level route-gate hook does
    NOT suffice here: the game recomputes these gates from the seed count, so
    the count is the authoritative lever.)  PI seeds are otherwise stripped
    from the pool -- their locations were removed with the hub regions -- so we
    create them fresh and push them to the player's starting inventory.
    Returns the number granted."""
    item_def = world.item_name_to_item.get(PETAL_WONDER_SEED_ITEM, {})
    count = int(item_def.get("count", 0))
    for _ in range(count):
        multiworld.push_precollected(world.create_item(PETAL_WONDER_SEED_ITEM))
    return count


def precollect_inactive_wonder_seeds(world, multiworld, player, active_worlds) -> int:
    """Open-world: grant the player every Wonder Seed for the worlds they are
    NOT playing.

    Inactive worlds' locations were stripped, so their Wonder Seeds have no home
    in the pool (``inactive_item_pool`` removes them).  Rather than just dropping
    them silently, push a precollected copy of each into the player's starting
    inventory: the client buckets received Wonder Seeds per world and pushes the
    count to the Switch, so the skipped worlds show up with their Wonder-Seed
    counters already full -- making it obvious the player doesn't need to play
    them.  (Royal Seeds for inactive worlds stay removed: the Bowser gate only
    counts active-world Royal Seeds.)  Returns the number granted."""
    active = set(active_worlds)
    granted = 0
    for n in WORLD_NUMBERS:
        if n in active:
            continue
        name = f"W{n} Wonder Seed"
        item_def = world.item_name_to_item.get(name, {})
        count = int(item_def.get("count", 0))
        for _ in range(count):
            multiworld.push_precollected(world.create_item(name))
            granted += 1
    return granted


def choose_active_worlds(world: World) -> list:
    """Randomly pick ``open_world_count`` distinct worlds (seeded via
    ``world.random``).  Returns a sorted list of world numbers."""
    from .Helpers import clamp, get_option_value
    n = clamp(int(get_option_value(world.multiworld, world.player, "open_world_count")), 1, 6)
    return sorted(world.random.sample(WORLD_NUMBERS, n))


def resolve_palaces_required(world: World, active_worlds: list) -> int:
    """Resolve the ``palaces_required`` option: ``0`` -> all active
    worlds; otherwise clamp to ``[1, len(active_worlds)]``."""
    from .Helpers import get_option_value
    pr = int(get_option_value(world.multiworld, world.player, "palaces_required"))
    if pr <= 0:
        return len(active_worlds)
    return max(1, min(pr, len(active_worlds)))


def _player_regions(multiworld: MultiWorld, player: int):
    return [r for r in multiworld.regions if r.player == player]


def _disconnect_exit(region, exit_):
    """Remove ``exit_`` from ``region.exits`` (drops it from the entrance
    cache) and from its target's incoming-entrance list."""
    target = exit_.connected_region
    region.exits.remove(exit_)
    if target is not None and exit_ in target.entrances:
        target.entrances.remove(exit_)


def restructure_regions(world: World, multiworld: MultiWorld, player: int, active_worlds) -> None:
    """Detach the worlds from the linear spine.

    1. Sever every cross-compartment exit (keep only intra-world edges),
       isolating hub regions and every world from each other.
    2. Rebuild ``Manual``'s exits: one always-open edge to each active
       world's ``Start``, plus a single edge to ``World Bowser`` (the
       Royal-Seed gate is attached later in ``after_set_rules``).
    """
    from .Regions import getConnectionName

    active = set(active_worlds)

    # (1) Sever cross-compartment edges.  Menu (-> Manual) and Manual
    # (rebuilt below) are handled separately.
    for region in _player_regions(multiworld, player):
        if region.name in ("Menu", "Manual"):
            continue
        src_world = world_of_region(region.name)
        for exit_ in list(region.exits):
            tgt = exit_.connected_region
            tgt_world = world_of_region(tgt.name) if tgt is not None else None
            # Keep only edges that stay within one world.
            if src_world is not None and src_world == tgt_world:
                continue
            _disconnect_exit(region, exit_)

    # (2) Rebuild Manual's exits.
    manual = multiworld.get_region("Manual", player)
    for exit_ in list(manual.exits):
        _disconnect_exit(manual, exit_)

    for n in sorted(active):
        target = multiworld.get_region(f"W{n} Start", player)
        manual.create_exit(getConnectionName("Manual", f"W{n} Start")).connect(target)

    bowser = multiworld.get_region(BOWSER_REGION, player)
    manual.create_exit(getConnectionName("Manual", BOWSER_REGION)).connect(bowser)


def strip_inactive_locations(world: World, multiworld: MultiWorld, player: int, active_worlds) -> None:
    """Remove every location AP fill must not place into: those in inactive
    worlds, hub regions, and the **non-goal** locations of ``World Bowser``.

    ``World Bowser`` is kept as a region (it holds the forced goal), but it
    also bundles the Petal-Isles / Special-World ("Wonder's World") post-game
    courses (Missile Meg Mayhem, High-Voltage Gauntlet, ...) and the four
    "All <X> Power Badge Obtained" meta-locations.  None of those are reachable
    in open-world -- the player only plays the selected worlds -- so leaving
    them in logic lets AP place progression items the player can never reach
    (observed: a required badge landing on PI: High-Voltage Gauntlet).  Strip
    everything in ``World Bowser`` except the goal location itself."""
    active = set(active_worlds)
    for region in _player_regions(multiworld, player):
        if region.name == BOWSER_REGION:
            for location in list(region.locations):
                if location.name != BOWSER_VICTORY_LOCATION:
                    region.locations.remove(location)
            continue
        w = world_of_region(region.name)
        drop = (w is not None and w not in active) or is_hub_region(region.name)
        if not drop:
            continue
        for location in list(region.locations):
            region.locations.remove(location)


def inactive_item_pool(item_pool: list, active_worlds) -> list:
    """Return ``item_pool`` minus the seed items for inactive worlds and
    minus all Petal Isles / Special World seeds (their locations are
    removed)."""
    active = set(active_worlds)

    def keep(item) -> bool:
        if item.name in _ALWAYS_REMOVE_ITEMS:
            return False
        w = _world_of_seed_item(item.name)
        if w is not None and w not in active:
            return False
        return True

    return [item for item in item_pool if keep(item)]


def make_bowser_gate(active_worlds, palaces_required: int, player: int):
    """Access rule for the ``Manual -> World Bowser`` edge: held distinct
    active-world Royal Seeds must reach ``palaces_required``."""
    seeds = [royal_seed_item(n) for n in active_worlds]

    def rule(state) -> bool:
        return sum(1 for s in seeds if state.has(s, player)) >= palaces_required

    return rule
