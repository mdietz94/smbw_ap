"""Open-world mode for SMBWonder.

Vanilla SMBWonder is a single linear spine threaded through the Petal
Isles (PI) hub: ``W1 -> PI -> W2 -> ... -> W6 -> Bowser``, gated by
per-world Wonder-Seed counts and the previous world's Royal Seed (see
``data/regions.json``).  Open-world mode keeps each world's *internal*
Wonder-Seed progression but detaches the worlds from each other: a random
set of N worlds (``open_world_count``) all hang directly off the ``Manual``
menu region and are reachable from the start, and Bowser is reached
directly off ``Manual`` once all six AP Royal Seeds are held AND at least
``palaces_required`` of the active palaces are reachable (the client's
runtime death-gate enforces the matching "actually cleared
``palaces_required`` palaces in your own game" condition on every Bowser's
Castle course).

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
import re

from worlds.AutoWorld import World
from BaseClasses import MultiWorld

WORLD_NUMBERS = (1, 2, 3, 4, 5, 6)

# Intra-world region gates that, in standard mode, also require a forced badge
# level (a Badge House / Wiggler Race / POOF! course that must be cleared to
# physically advance the world -- see the smbw-logic skill's progression-wall
# rule).  Open-world opens every course node from the start, so these walls are
# NOT enforced there: the world-map Wonder-Seed bar is the only gate.  Leaving
# the badge half on keeps the section "out of logic" even with enough Wonder
# Seeds (player-reported).  In open-world we therefore drop the badge half and
# keep the Wonder-Seed toll.  The badge stays a meaningful progression item --
# the challenge level's own checks still require it (location layer).
_BADGE_WALL_REGIONS = ("W3 4 Seeds",)

_BADGE_TOKEN_RE = re.compile(r"\|[^|]*Badge\|")


def strip_badge_requirement(requires: str) -> str:
    """Return ``requires`` with any ``|<X> Badge|`` token removed, collapsing a
    leftover ``A AND B`` so the rest (e.g. the Wonder-Seed toll) stays valid."""
    parts = [p.strip() for p in requires.split(" AND ")]
    kept = [p for p in parts if not (_BADGE_TOKEN_RE.fullmatch(p))]
    return " AND ".join(kept)


def badge_wall_open_world_requires():
    """Open-world replacements for the badge-wall region gates: same dict keyed
    by region name, value = the region's ``requires`` with the badge token
    stripped.  Consumed by ``hooks.World.before_set_rules`` (swapped into
    ``regionMap`` before ``set_rules`` captures the rule closures, then
    restored)."""
    from .Regions import regionMap
    out = {}
    for name in _BADGE_WALL_REGIONS:
        entry = regionMap.get(name)
        if entry is None:
            continue
        out[name] = strip_badge_requirement(entry.get("requires", ""))
    return out

# Region holding the (forced) Bowser victory location.
BOWSER_REGION = "World Bowser"

# Goal location open-world forces; must exist in Locations.victory_names.
BOWSER_VICTORY_LOCATION = "BC: Bowser's Rage Stage - Royal Seed"

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
    worlds, hub regions, and the **non-course** locations of ``World Bowser``.

    Bowser's Castle is ALWAYS part of open-world: every ``BC:``-prefixed
    course location (the gauntlet courses + the final Bowser's Rage Stage
    goal) is kept as a live AP location, gated -- like the goal -- on the
    ``Manual -> World Bowser`` entrance (all six AP Royal Seeds).  The client
    death-gate additionally enforces the in-game palace-clear requirement on
    every castle course.

    ``World Bowser`` also bundles four non-course meta-locations -- the
    "All <X> Power Badge Obtained" achievements -- which require collecting a
    full badge family that open-world's reduced world set may never grant, so
    leaving them in logic lets AP place progression items the player can never
    reach.  Strip those (everything in ``World Bowser`` that isn't a ``BC:``
    course)."""
    active = set(active_worlds)
    for region in _player_regions(multiworld, player):
        if region.name == BOWSER_REGION:
            for location in list(region.locations):
                if not location.name.startswith("BC:"):
                    region.locations.remove(location)
            continue
        w = world_of_region(region.name)
        drop = (w is not None and w not in active) or is_hub_region(region.name)
        if not drop:
            continue
        for location in list(region.locations):
            region.locations.remove(location)


def inactive_item_pool(item_pool: list, active_worlds) -> list:
    """Return ``item_pool`` minus the inactive worlds' **Wonder** Seeds and
    all Petal Isles / Special World Wonder Seeds (their locations are
    removed; the inactive ones are precollected instead).

    **Royal** Seeds are KEPT for every world regardless of the active set:
    facing Bowser requires holding all six AP Royal Seeds (see
    ``make_bowser_gate``), so fill must be free to place every one in the
    active content."""
    active = set(active_worlds)

    def keep(item) -> bool:
        if item.name in _ALWAYS_REMOVE_ITEMS:
            return False
        if item.name.endswith(" Wonder Seed"):
            w = world_of_region(item.name)
            if w is not None and w not in active:
                return False
        return True

    return [item for item in item_pool if keep(item)]


# Per-world palace-clear AP location (the "Royal Seed" location earned by
# beating that world's palace).  Used to encode the open-world final-Bowser
# "clear palaces_required palaces" requirement into AP logic.
PALACE_LOCATION = {
    1: "W1: Pipe-Rock Plateau Palace - Royal Seed",
    2: "W2: Fluff-Puff Peaks Palace - Royal Seed",
    3: "W3: Royal Seed Mansion - Royal Seed",
    4: "W4: Sunbaked Desert Palace - Royal Seed",
    5: "W5: Operation Poplin Rescue - Royal Seed",
    6: "W6: Deep Magma Bog Palace - Royal Seed",
}


def make_bowser_gate(player: int, active_worlds, palaces_required: int):
    """Access rule for the ``Manual -> World Bowser`` edge (and thus every
    Bowser's Castle course location): the player must hold **all six** AP
    Royal Seeds AND be able to clear at least ``palaces_required`` of the
    active worlds' palaces.

    Why the palace half is modelled in logic (it mirrors the client's runtime
    death-gate, which bounces the player out of any castle course unless they
    hold all six AP Royal Seeds and have cleared ``palaces_required`` palaces
    in their own game): the Castle's course locations are in the pool, so
    without it fill could place a progression item the player needs to *reach*
    a required palace behind the all-six-seed Castle gate -- a deadlock (you
    can't enter the Castle to grab the item without first clearing the palace
    that item gates).  Gating the Castle on the active palaces being
    *reachable* (= clearable) forces fill to keep every palace prerequisite
    outside the Castle.

    All six Royal Seeds are kept in the pool regardless of the active-world
    set (see ``inactive_item_pool``).  ``can_reach_location`` is an indirect
    condition, so ``after_set_rules`` must register each active palace region
    against this entrance (see ``register_bowser_indirect_conditions``)."""
    seeds = [royal_seed_item(n) for n in WORLD_NUMBERS]
    palace_locs = [PALACE_LOCATION[n] for n in active_worlds if n in PALACE_LOCATION]

    def rule(state) -> bool:
        if not all(state.has(s, player) for s in seeds):
            return False
        reachable = sum(
            1 for loc in palace_locs if state.can_reach_location(loc, player))
        return reachable >= palaces_required

    return rule


def register_bowser_indirect_conditions(multiworld, player, active_worlds, bowser_entrance) -> None:
    """Register each active palace location's region as an indirect condition
    for the Bowser entrance, so AP re-evaluates ``make_bowser_gate`` when a
    palace becomes reachable (the gate calls ``can_reach_location`` on them).
    Required because the world uses explicit indirect conditions -- without it
    the entrance would be evaluated once, before the palaces are reachable,
    and never retried."""
    for n in active_worlds:
        loc_name = PALACE_LOCATION.get(n)
        if loc_name is None:
            continue
        region = multiworld.get_location(loc_name, player).parent_region
        multiworld.register_indirect_condition(region, bowser_entrance)
