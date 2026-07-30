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


def test_post_clear_regions_inherit_their_prerequisite_gate():
    """A ``Post-<X>`` region means "you cleared course X", so its ``requires``
    must repeat whatever X's own completion checks require.

    This invariant was NOT enforced and produced an unbeatable seed
    (player-reported 2026-07-20): ``W1 Post-Jet Run`` was ``[]``, so all eight
    *Bounce, Bounce, Bounce* checks -- including a W1 and a Petal Isles Wonder
    Seed -- were in logic while *Jet Run I*, the course you must clear to unlock
    them, needs ``|Jet Run Badge|``.  Fill buried required seeds there and the
    seed could not be completed.

    Note the ``Post-*`` regions are mixed buckets: they also HOST the
    prerequisite course itself (``W1 Post-Jet Run`` holds Jet Run I *and*
    Bounce³).  That is why the gate was easy to miss on inspection, and it is
    harmless -- the prerequisite's own checks already carry the same require.

    Beatability tests cannot catch this class: it makes checks *more* reachable
    than reality, which never raises FillError -- it just strands the player.
    """
    regions = _load_json("regions.json")
    # Post-clear region -> (unlocking course, requires it must inherit).
    # A course whose completion is open contributes "" (no gate needed).
    inherited = {
        "W1 Post-Jet Run": ("W1: Jet Run I", "|Jet Run Badge|"),
        "W2 Post-Jump": ("W2: Floating High Jump I",
                         "|Floating High Jump Badge| OR |@Yoshi:1|"),
        "W6 Post-Spring": ("W6: Jet Run II", "|Jet Run Badge|"),
        # Unlocked by courses whose completion checks are open -- no gate.
        "W4 Post-Invis": ("W4: Invisibility I", ""),
        "W5 Post-Wubba": ("W5: Wubba Ruins", ""),
        "W5 Post-Swaying": ("W5: Swaying Ruins", ""),
        "W1 Post-Bulrush Express": ("W1: Bulrush Express", ""),
        # Pure routing node (holds no locations) into W5 Start / W6 Start, both
        # of which carry their own Petal Isles seed toll.
        "PI Post-Airship": (None, ""),
    }
    locations = _load_json("locations.json")
    by_name = {loc["name"]: loc for loc in locations}

    for region, (course, expected) in inherited.items():
        assert region in regions, f"missing region {region!r}"
        actual = regions[region].get("requires", "")
        if actual == []:
            actual = ""
        assert actual == expected, (
            f"region {region!r} unlocks by clearing {course!r}, so its requires "
            f"must be {expected!r} but is {actual!r} -- a mismatch puts the "
            f"region's contents in logic before the player can clear the course"
        )
        # And the gate must actually match the prerequisite's Normal Exit.
        if expected:
            exit_loc = by_name.get(f"{course} - Normal Exit")
            assert exit_loc is not None, f"missing {course!r} Normal Exit"
            exit_req = exit_loc.get("requires", "")
            assert exit_req == expected, (
                f"{course!r} Normal Exit requires {exit_req!r} but "
                f"{region!r} gates on {expected!r} -- keep them in sync"
            )
    # Every Post-* region must be listed above, so a new one can't skip the rule.
    unlisted = sorted(
        name for name in regions
        if " Post-" in name and name not in inherited
    )
    assert not unlisted, (
        "new post-clear region(s) not covered by the inheritance rule: "
        + ", ".join(unlisted)
    )


def test_petal_isles_depth_requires_world_completion():
    """Pin the world-progress gates on the deeper Petal Isles regions.

    Petal Isles is the hub; unlike a numbered world it does NOT open by
    collecting its own Wonder Seeds -- its islands unlock as you clear the prior
    world's final palace level.  Without these gates the whole PI-seed-gated spur
    (PI 5 Seeds + PI 8 Seeds, ~58 checks) is reachable with zero World 2
    progress, because the spur branches off the pre-W2 hub node and was gated
    only on |Petal Isles Wonder Seed:N|.

    The gate is the *condition to play that world's final level*, NOT the world's
    Royal Seed: Royal Seeds are AP pool items placed anywhere, so |W2 Royal Seed|
    only means "AP granted it", not "you cleared World 2".  In the seed-toll model
    the condition to reach a world's palace region is its full Wonder-Seed count
    (plus any wall on the way there).

    Real-game anchors (Super Mario Wiki):
      * Wiggler Race Swimming! (PI 5 Seeds) unlocks after clearing Fluff-Puff
        Peaks Palace -- the W2 palace lives in `W2 14 Seeds` (|W2 Wonder Seed:14|).
      * Jewel-Block Cave (PI 8 Seeds) unlocks after the Shining Falls Royal Seed
        Mansion -- the W3 palace lives in `W3 10 Seeds` (|W3 Wonder Seed:10|), and
        reaching it must clear the W3 wall POOF! Crouching High Jump I
        (|Crouching High Jump Badge|).

    A region's ``requires`` gates the checks INSIDE it (each location's rule is
    its own region's requires), so these tokens keep every PI-depth check out of
    logic until you could actually play the gating world's finale.
    """
    regions = _load_json("regions.json")
    # region -> tokens that must all appear in its requires
    gates = {
        "PI 5 Seeds": ["W2 Wonder Seed:14"],
        "PI 8 Seeds": ["W3 Wonder Seed:10", "Crouching High Jump Badge"],
    }
    for region, tokens in gates.items():
        assert region in regions, f"missing region {region!r}"
        requires = regions[region].get("requires", "")
        for token in tokens:
            assert f"|{token}|" in requires, (
                f"region {region!r} must gate on |{token}| (Petal Isles opens by "
                f"clearing the prior world's final level, not by AP Royal Seeds) "
                f"but requires == {requires!r} -- removing this puts Petal Isles in "
                f"logic before that world's finale is playable"
            )
    # The Royal Seed pool item must NOT be the gate (it can be placed anywhere).
    for region in gates:
        requires = regions[region].get("requires", "")
        assert "Royal Seed" not in requires, (
            f"{region} must NOT gate on a Royal Seed -- it is an AP pool item, not "
            f"proof the world was cleared. requires == {requires!r}"
        )


def test_w6_palace_gates_at_15_not_25_seeds():
    """The Deep Magma Bog Palace unlocks at 15 W6 Wonder Seeds, not 25.

    Player-reported: "over 15 W6 Wonder Seeds and the palace isn't in logic."
    Per Game8's Deep Magma Bog course list, the Palace requires **15** Wonder
    Seeds; the **25**-seed threshold gates the six badge-challenge "II" courses
    (Jet Run II, Floating High Jump II, Boosting Spin Jump II, Grappling Vine II,
    Invisibility II, Spring Feet II).  The old model lumped the Palace into the
    25-seed region (and left three of the badge courses ungated in W6 Start),
    so the Palace sat out of logic until 25 seeds -- a fidelity bug that also
    put the badge-course checks in logic far too early.

    Source: https://game8.co/games/Super-Mario-Bros-Wonder/archives/430860
    """
    regions = _load_json("regions.json")
    locations = _load_json("locations.json")

    assert regions.get("W6 15 Seeds", {}).get("requires") == "|W6 Wonder Seed:15|"
    assert regions.get("W6 25 Seeds", {}).get("requires") == "|W6 Wonder Seed:25|"
    # W6 Start must route to the 15-seed palace region, which chains to 25.
    assert "W6 15 Seeds" in regions["W6 Start"]["connects_to"]
    assert "W6 25 Seeds" in regions["W6 15 Seeds"]["connects_to"]

    region_of = {l["name"]: l["region"] for l in locations}
    # Every Palace check lives in the 15-seed region.
    palace = [n for n in region_of if n.startswith("W6: Deep Magma Bog Palace -")]
    assert palace, "no Deep Magma Bog Palace locations found"
    for name in palace:
        assert region_of[name] == "W6 15 Seeds", (
            f"{name} must gate at 15 seeds (region W6 15 Seeds), not "
            f"{region_of[name]!r}")

    # All six badge-challenge "II" courses gate at 25 seeds.
    for course in ("Jet Run II", "Floating High Jump II", "Boosting Spin Jump II",
                   "Grappling Vine II", "Invisibility II", "Spring Feet II"):
        checks = [n for n in region_of if n.startswith(f"W6: {course} -")]
        assert checks, f"no locations for W6 {course}"
        for name in checks:
            assert region_of[name] == "W6 25 Seeds", (
                f"{name} must gate at 25 seeds (region W6 25 Seeds), not "
                f"{region_of[name]!r}")


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
#
# Dolphin Kick and Jet Run were added here after a player report ("Dolphin Kick
# I/II + Jet Run I require their badges for normal exit + top of flag").  Jet
# Run II is included for consistency + softlock-safety (same badge challenge;
# gating a check is always the beatability-safe direction -- the player only
# reached I).
_STRUCTURAL_BADGE_LEVELS = {
    "Wall-Climb Jump I", "Wall-Climb Jump II",
    "Grappling Vine I", "Grappling Vine II",
    "Boosting Spin Jump I", "Boosting Spin Jump II",
    "Floating High Jump I", "Floating High Jump II",
    "Crouching High Jump I", "Crouching High Jump II",
    "Dolphin Kick I", "Dolphin Kick II",
    "Jet Run I", "Jet Run II",
    # Player-reported 2026-07-20: "completely doable with Yoshi, but impossible
    # without him" -- so the completion checks need |Spring Feet Badge| OR a
    # Yoshi.  Spring Feet I stays open (its exits are player-confirmed doable).
    "Spring Feet II",
}

# Badge-challenge levels whose 10-Coins are player-confirmed obtainable WITHOUT
# the badge (or a Yoshi), so they are NOT gated on the badge.  The default for
# the class is still "coins require the badge" (safe against burying a Wonder
# Seed behind a badge the player lacks); this set is the vetted exceptions.
_OPEN_COIN_LEVELS = {
    "Parachute Cap I",  # player-confirmed: all 10 coins reachable with nothing
    # Player-reported 2026-07-20: both courses had their Normal Exit / Top of
    # Flag open but their coins badge-gated, which is backwards -- the coins are
    # reachable without the badge (Invisibility II: "even easier without it").
    "Spring Feet I",
    "Invisibility II",
    # Maintainer ruling 2026-07-20: "Invisibility should not require the
    # Invisibility badge, that badge is never required."  Invisibility I's coins
    # were the last site -- |Invisibility Badge| now appears in NO requires.
    # Pinned by test_invisibility_badge_is_never_required.
    "Invisibility I",
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
        for loc, lvl, kind, badge, requires in _challenge_checks()
        if kind.startswith("10 Coin")
        and lvl not in _OPEN_COIN_LEVELS
        and f"|{badge}|" not in requires
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


def test_invisibility_badge_is_never_required():
    """The Invisibility Badge gates NOTHING (maintainer ruling 2026-07-20:
    "Invisibility should not require the Invisibility badge, that badge is never
    required").  Invisibility I/II are completable and fully 10-Coin-able
    without it, so the token must not appear in any location or region rule."""
    offenders = []
    for loc in _load_json("locations.json"):
        requires = loc.get("requires", "")
        if isinstance(requires, str) and "|Invisibility Badge|" in requires:
            offenders.append(f"location {loc['name']}: {requires!r}")
    for name, region in _load_json("regions.json").items():
        requires = region.get("requires", "")
        if isinstance(requires, str) and "|Invisibility Badge|" in requires:
            offenders.append(f"region {name}: {requires!r}")
    assert not offenders, (
        "|Invisibility Badge| must not gate anything:\n  " + "\n  ".join(offenders)
    )


def test_nonstructural_badge_completion_stays_open():
    """A badge challenge that is completable without the badge keeps its Normal
    Exit / Top of Flag open (maintainer scope, PR #137).  Pin Invisibility I --
    a still-open example -- so a future blanket re-gate is caught.  (Dolphin Kick
    and Jet Run were the original pin but moved to _STRUCTURAL_BADGE_LEVELS after
    a player report that they DO require the badge to complete.)"""
    for loc, lvl, kind, badge, requires in _challenge_checks():
        if lvl == "Invisibility I" and kind in ("Normal Exit", "Top of Flag"):
            assert f"|{badge}|" not in requires, (
                f"{loc['name']} should NOT require |{badge}| "
                f"(completable without it): {requires!r}"
            )


def test_open_coin_levels_stay_open():
    """The vetted _OPEN_COIN_LEVELS keep their 10-Coins un-gated (player-
    confirmed obtainable without the badge).  Pin the removal so a future
    blanket re-gate of the whole coin class doesn't silently re-add it."""
    for loc, lvl, kind, badge, requires in _challenge_checks():
        if lvl in _OPEN_COIN_LEVELS and kind.startswith("10 Coin"):
            assert f"|{badge}|" not in requires, (
                f"{loc['name']} should NOT require |{badge}| "
                f"(player-confirmed obtainable without it): {requires!r}"
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


# ---------------------------------------------------------------------------
# Playtest regressions (Discord reports, 2026-07-28).

def test_badge_marathon_requires_every_wonder_seed():
    """``Post-Badge`` (Special: Badge Marathon / WONDER?) is the game's 100%
    reward: in vanilla it needs every Wonder Seed, every 10-Flower Coin and
    every gold flag.  The region gate used to ask for *partial* per-world seed
    counts (14/14/10/15/11/25/15/16), so the check showed in logic for a player
    who was still missing seeds -- and a progression item placed there would be
    stranded (player-reported: "Badge Marathon is in logic when it shouldn't
    be ... this could lead to an impossible seed").

    The gate now demands the FULL pool count of every Wonder Seed item, derived
    from items.json so it tracks any future pool change.  (10 Coins / Gold
    Flags remain unmodelled -- they are filler items, and making 376 of them
    progression overflows the pool past the location count in open-world mode.
    See the logic-reconciliation reference.)
    """
    counts = {
        item["name"]: int(item["count"])
        for item in _load_json("items.json")
        if item["name"].endswith("Wonder Seed")
    }
    assert len(counts) == 8, sorted(counts)

    requires = _load_json("regions.json")["Post-Badge"]["requires"]
    missing = [
        f"|{name}:{total}|"
        for name, total in sorted(counts.items())
        if f"|{name}:{total}|" not in requires
    ]
    assert not missing, (
        "Post-Badge must require every Wonder Seed in the pool; missing:\n  "
        + "\n  ".join(missing)
        + f"\ngate is: {requires!r}"
    )
    assert "|@Royal Seed:6|" in requires, requires


def test_item_park_blocks_share_the_course_powerup_wall():
    """W6: Item Park is walled behind Elephant + Bubble + Drill, and both of
    its character blocks sit past that wall.  The Toadette block inherited the
    wall in the 2026-07-20 pass but the Daisy block was left bare
    ``{OptOne(|Daisy|)}`` -- player-reported as in logic "since I got access to
    the course, when it shouldn't have been"."""
    wall = [
        "(|Elephant Fruit| OR |All Elephant Power Badge|)",
        "(|Bubble Flower| OR |All Bubble Flower Badge|)",
        "(|Drill Mushroom| OR |All Drill Power Badge|)",
    ]
    targets = {
        "W6: Item Park - Wonder Seed",
        "W6: Item Park - Daisy Block",
        "W6: Item Park - Toadette Block",
    }
    seen = set()
    offenders = []
    for loc in _load_json("locations.json"):
        if loc["name"] not in targets:
            continue
        seen.add(loc["name"])
        requires = loc.get("requires", "")
        for clause in wall:
            if clause not in requires:
                offenders.append(f"{loc['name']} missing {clause}: {requires!r}")
    assert seen == targets, f"Item Park checks not found: {sorted(targets - seen)}"
    assert not offenders, "\n  ".join(offenders)


def test_boosting_spin_jump_ii_allows_yoshi():
    """Video-confirmed 2026-07-28: *Boosting Spin Jump II* is clearable with a
    Yoshi and no badge, matching *Boosting Spin Jump I*, which already carried
    the alternative.  The badge stays in the rule (the course is still
    structural, see ``_STRUCTURAL_BADGE_LEVELS``) -- Yoshi is an OR."""
    offenders = [
        f"{loc['name']}: {loc.get('requires', '')!r}"
        for loc in _load_json("locations.json")
        if "Boosting Spin Jump II" in loc["name"]
        and "|@Yoshi:1|" not in loc.get("requires", "")
    ]
    assert not offenders, (
        "Boosting Spin Jump II checks missing the Yoshi alternative:\n  "
        + "\n  ".join(offenders)
    )
