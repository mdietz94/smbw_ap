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
