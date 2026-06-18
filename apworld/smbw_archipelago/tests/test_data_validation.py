"""Generation-time data-validation regression tests.

These guard the apworld's JSON data tables against the class of error that
aborts ``Generate.py`` before a seed can roll: an item that gates a location
(or region) via ``requires`` but is not flagged ``progression``.  Archipelago's
fill needs every access-gating item in the progression pool, so the Manual
``runGenerationDataValidation()`` pass raises if one is merely ``useful`` /
``filler``.

Badges in SMBW can block course entry (e.g. *Auto Super Mushroom Badge* gates
*W1: Mountaineering!*), so any badge named in a ``requires`` string must be
progression.  See the badge-progression PR (follow-up to #109).

The tests load ``DataValidation.py`` standalone (by file path) rather than
importing the ``apworld.smbw_archipelago`` package: importing the package
registers the SMBWonder world, which collides with an already-installed copy in
``vendor/Archipelago/custom_worlds`` on a developer machine.  The standalone
module only needs ``worlds``/``BaseClasses`` on ``sys.path``, which the repo
root ``conftest.py`` arranges from the vendored Archipelago checkout.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re

import pytest

_WORLD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_WORLD_DIR, "data")


def _load_data_validation():
    """Load DataValidation.py as a standalone module (no package import)."""
    path = os.path.join(_WORLD_DIR, "DataValidation.py")
    spec = importlib.util.spec_from_file_location("smbw_dv_standalone", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_json(name):
    with open(os.path.join(_DATA_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def _iter_entries(table):
    """Yield entry dicts from a list-shaped or name-keyed-dict-shaped table."""
    if isinstance(table, dict):
        for value in table.values():
            if isinstance(value, dict):
                yield value
    else:
        yield from table


def _required_item_names(table):
    """Collect every item name named in a ``|Item|`` token across requires."""
    names = set()
    for entry in _iter_entries(table):
        requires = entry.get("requires", "")
        if not isinstance(requires, str):
            requires = json.dumps(requires)
        for token in re.findall(r"\|([^|]+)\|", requires):
            # strip an optional ":count" suffix, skip category (@...) tokens
            name = token.split(":")[0].strip()
            if name and not name.startswith("@"):
                names.add(name)
    return names


@pytest.fixture(scope="module")
def items():
    return _load_json("items.json")


def _is_progression(item):
    return bool(item.get("progression") or item.get("progression_skip_balancing"))


def test_gating_badges_are_progression(items):
    """Every Badge that gates a location/region must be marked progression."""
    by_name = {item["name"]: item for item in items}
    gating = _required_item_names(_load_json("locations.json"))
    gating |= _required_item_names(_load_json("regions.json"))

    offenders = [
        name
        for name in sorted(gating)
        if name in by_name
        and "Badge" in by_name[name].get("category", [])
        and not _is_progression(by_name[name])
    ]
    assert not offenders, (
        "Badges gate course entry but are not marked progression "
        "(generation will abort): " + ", ".join(offenders)
    )


def test_known_blocking_badges_are_progression(items):
    """Pin the specific badges fixed in this PR so a regression is obvious."""
    by_name = {item["name"]: item for item in items}
    for name in (
        "Auto Super Mushroom Badge",
        "Rhythm Jump Badge",
        "Sensor Badge",
        "Sound Off? Badge",
    ):
        assert name in by_name, f"missing item {name!r}"
        assert _is_progression(by_name[name]), f"{name} must be progression"


def test_progression_wall_badges_gate_regions():
    """Pin the badge-granting levels that are genuine *progression walls*.

    A region-layer badge gate is correct ONLY when a level you MUST clear to
    advance the world also REQUIRES that badge to clear it.  The seeds-only
    region graph can't see such a wall, so without the explicit gate fill can
    bury the badge in a later world and softlock the seed.

    Only Crouching High Jump qualifies: POOF! Crouching High Jump I is a
    *structural* badge challenge (you can't reach its goal without the badge)
    AND it is forced -- the wiki calls it "the only Badge Challenge required to
    complete the game" (it unlocks the W3 Royal Seed Mansion path).

    Parachute Cap (Badge House) and Auto Super Mushroom (Wiggler Race
    Mountaineering!) were ALSO gated here, but the level that grants each does
    NOT require the badge to clear (the Badge House just hands it over; the
    Wiggler race needs no badge) and no other forced level needs them -- so they
    were never real walls.  Both removed (player-confirmed: not needed to
    progress W1).  See the smbw-logic skill reconciliation record.
    """
    regions = _load_json("regions.json")
    # region -> badge that must gate the transition (genuine wall)
    walls = {
        "W3 4 Seeds": "Crouching High Jump Badge",   # POOF! Crouching High Jump I
    }
    for region, badge in walls.items():
        assert region in regions, f"missing region {region!r}"
        requires = regions[region].get("requires", "")
        assert f"|{badge}|" in requires, (
            f"region {region!r} must gate on |{badge}| (progression wall) "
            f"but requires == {requires!r} -- removing this re-introduces a softlock"
        )
    # These are NOT walls -- pin their removal so they aren't re-added.
    not_walls = {
        "W1 3 Seeds": "Parachute Cap Badge",       # Badge House just grants it
        "W1 10 Seeds": "Auto Super Mushroom Badge",  # Wiggler race needs no badge
    }
    for region, badge in not_walls.items():
        requires = regions.get(region, {}).get("requires", "")
        assert badge not in requires, (
            f"{region} must NOT require |{badge}| -- the granting level does not "
            f"block W1 progression (player-confirmed). requires == {requires!r}"
        )


def test_petal_isles_depth_requires_world_completion():
    """Pin the world-completion gates on the deeper Petal Isles regions.

    Petal Isles is the hub; unlike a numbered world it does NOT open by
    collecting its own Wonder Seeds -- its islands unlock as you clear world
    palaces (Royal Seeds).  Without these gates the whole PI-seed-gated spur
    (PI 5 Seeds + PI 8 Seeds, ~58 checks) is reachable with zero World 2
    progress, because the spur branches off the pre-W2 hub node and was gated
    only on |Petal Isles Wonder Seed:N|.

    Real-game anchors (Super Mario Wiki):
      * Wiggler Race Swimming! (PI 5 Seeds) unlocks after clearing Fluff-Puff
        Peaks Palace  -> requires World 2 complete (|W2 Royal Seed|).
      * Jewel-Block Cave (PI 8 Seeds) unlocks after visiting the Shining Falls
        Royal Seed Mansion -> requires World 3 complete (|W3 Royal Seed|).

    A region's ``requires`` gates the checks INSIDE it (each location's rule is
    its own region's requires), so putting the Royal-Seed token here keeps every
    PI-depth check out of logic until the gating world is done.
    """
    regions = _load_json("regions.json")
    gates = {
        "PI 5 Seeds": "W2 Royal Seed",
        "PI 8 Seeds": "W3 Royal Seed",
    }
    for region, seed in gates.items():
        assert region in regions, f"missing region {region!r}"
        requires = regions[region].get("requires", "")
        assert f"|{seed}|" in requires, (
            f"region {region!r} must gate on |{seed}| (Petal Isles opens by world "
            f"completion, not by PI Wonder Seeds) but requires == {requires!r} -- "
            f"removing this puts all of Petal Isles in logic before that world is done"
        )


# The 20 badge-challenge "I/II" courses and the badge each one requires.
#
# The badge is NOT auto-available inside the course in the AP mod (AP is the
# sole badge authority).  But a badge challenge can still be *completed* without
# its badge -- the badge is "practice" content, not always a structural
# requirement.  So the model is PER-CHECK (maintainer scope, PR #137 + the
# follow-up player reports):
#   * 10-Coin checks ALWAYS require the badge -- the badge-themed coins are the
#     badge-practice collectibles (player-confirmed: Dolphin Kick I "all the 10
#     coins require it").  Gating them is also the safe default.
#   * Normal Exit / Top of Flag require the badge ONLY where the badge is
#     STRUCTURALLY required to reach the goal (you cannot clear the level
#     without it).  Confirmed: Wall-Climb Jump I/II (can't climb the walls ->
#     can't finish).  All other courses' completion checks stay open.
_BADGE_CHALLENGE_LEVELS = {
    "Wall-Climb Jump I": "Wall-Climb Jump Badge",
    "Wall-Climb Jump II": "Wall-Climb Jump Badge",
    "Dolphin Kick I": "Dolphin Kick Badge",
    "Dolphin Kick II": "Dolphin Kick Badge",
    "Boosting Spin Jump I": "Boosting Spin Jump Badge",
    "Boosting Spin Jump II": "Boosting Spin Jump Badge",
    "Crouching High Jump I": "Crouching High Jump Badge",
    "Crouching High Jump II": "Crouching High Jump Badge",
    "Floating High Jump I": "Floating High Jump Badge",
    "Floating High Jump II": "Floating High Jump Badge",
    "Spring Feet I": "Spring Feet Badge",
    "Spring Feet II": "Spring Feet Badge",
    "Grappling Vine I": "Grappling Vine Badge",
    "Grappling Vine II": "Grappling Vine Badge",
    "Jet Run I": "Jet Run Badge",
    "Jet Run II": "Jet Run Badge",
    "Invisibility I": "Invisibility Badge",
    "Invisibility II": "Invisibility Badge",
    "Parachute Cap I": "Parachute Cap Badge",
    "Parachute Cap II": "Parachute Cap Badge",
}


# Courses where the badge is structurally required to COMPLETE the level (the
# completion checks, not just the coins, must require the badge) -- maintainer-
# confirmed: you can't reach the goal without the ability (climb the walls,
# swing the vine, spin/float/crouch-jump to the exit).
_STRUCTURAL_BADGE_LEVELS = {
    "Wall-Climb Jump I", "Wall-Climb Jump II",
    "Grappling Vine I", "Grappling Vine II",
    "Boosting Spin Jump I", "Boosting Spin Jump II",
    "Floating High Jump I", "Floating High Jump II",
    "Crouching High Jump I", "Crouching High Jump II",
}

_CHECK_RE = re.compile(r"^[^:]+: (.*?) - (.+)$")


def _challenge_checks():
    """Yield (location, level, kind, badge) for every badge-challenge check."""
    for loc in _load_json("locations.json"):
        m = _CHECK_RE.match(loc["name"])
        if not m:
            continue
        badge = _BADGE_CHALLENGE_LEVELS.get(m.group(1))
        if badge is None:
            continue
        requires = loc.get("requires", "")
        if not isinstance(requires, str):
            requires = json.dumps(requires)
        yield loc, m.group(1), m.group(2), badge, requires


def test_badge_challenge_coins_require_their_badge():
    """Every badge-challenge 10-Coin must require its badge (softlock pin).

    Leaving these open let fill bury a needed Wonder Seed behind a badge the
    player never had -- the Dolphin Kick I softlock ("all the 10 coins require
    it").  Gating the coins is also the safe default for the whole class.
    """
    offenders = [
        f"{loc['name']} (needs |{badge}|): {requires!r}"
        for loc, _lvl, kind, badge, requires in _challenge_checks()
        if kind.startswith("10 Coin") and f"|{badge}|" not in requires
    ]
    assert not offenders, (
        "Badge-challenge 10-Coins missing their badge (softlock risk):\n  "
        + "\n  ".join(offenders)
    )
    seen = {lvl for _l, lvl, _k, _b, _r in _challenge_checks()}
    missing = set(_BADGE_CHALLENGE_LEVELS) - seen
    assert not missing, f"expected badge-challenge levels not found: {sorted(missing)}"


def test_structural_badge_levels_gate_completion():
    """Wall-Climb Jump I/II can't be cleared without the badge, so their Normal
    Exit / Top of Flag must require it (player-reported softlock: "no way to
    clear it without the badge")."""
    offenders = [
        f"{loc['name']} (needs |{badge}|): {requires!r}"
        for loc, lvl, kind, badge, requires in _challenge_checks()
        if lvl in _STRUCTURAL_BADGE_LEVELS
        and kind in ("Normal Exit", "Top of Flag")
        and f"|{badge}|" not in requires
    ]
    assert not offenders, (
        "Structural badge-challenge completion checks missing their badge:\n  "
        + "\n  ".join(offenders)
    )


def test_nonstructural_badge_completion_stays_open():
    """A badge challenge that is completable without the badge keeps its Normal
    Exit / Top of Flag open (maintainer scope, PR #137).  Pin Dolphin Kick I --
    the player-confirmed example -- so a future blanket re-gate is caught."""
    for loc, lvl, kind, badge, requires in _challenge_checks():
        if lvl == "Dolphin Kick I" and kind in ("Normal Exit", "Top of Flag"):
            assert f"|{badge}|" not in requires, (
                f"{loc['name']} should NOT require |{badge}| "
                f"(completable without it): {requires!r}"
            )


# KO-arena ("Rumble") battle levels: out of logic without at least one Power-Up.
_KO_ARENAS = (
    "Pipe-Rock Rumble", "Fluff-Puff Kerfuff", "Sunbaked Skirmish",
    "Fungi Funk", "Magma Flare-Up", "Petal Meddle",
)


def test_ko_arenas_require_a_powerup():
    """Every KO-arena check (Wonder Seed + 3x 10-Coin) requires |@Power-Up:1| --
    the fight is not in logic without a power-up (player-reported)."""
    offenders = []
    seen = set()
    for loc in _load_json("locations.json"):
        arena = next((a for a in _KO_ARENAS if a in loc["name"]), None)
        if arena is None:
            continue
        seen.add(arena)
        requires = loc.get("requires", "")
        if not isinstance(requires, str):
            requires = json.dumps(requires)
        if "|@Power-Up:1|" not in requires:
            offenders.append(f"{loc['name']}: {requires!r}")
    assert not offenders, (
        "KO-arena checks missing the power-up gate:\n  " + "\n  ".join(offenders)
    )
    assert set(_KO_ARENAS) - seen == set(), "not all KO arenas found"


def test_generation_data_validation_passes():
    """The full Manual generation validation pass must not raise.

    This is the exact check ``Generate.py`` runs; a non-progression gating item
    surfaces here as the ValidationError that aborts generation.
    """
    dv = _load_data_validation()
    D = dv.DataValidation
    D.game_table = _load_json("game.json")
    D.item_table = _load_json("items.json")
    D.location_table = _load_json("locations.json")
    D.region_table = _load_json("regions.json")

    # Raises Exception with the aggregated ValidationErrors on failure.
    dv.runGenerationDataValidation()
