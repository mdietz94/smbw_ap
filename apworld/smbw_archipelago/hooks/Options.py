"""Hooks for adding/modifying AP options before SMBWonder's options
dataclass is built.  `after_options_defined` currently no-ops; populate
`options` in `before_options_defined` to register new YAML-configurable
options.
"""
from Options import Toggle, Range, OptionDict


class OpenWorld(Toggle):
    """Open-world mode.  Instead of the linear World 1 -> Bowser spine,
    start with a random set of N worlds (see ``open_world_count``) all
    unlocked at once; clear ``palaces_required`` of their palaces to
    unlock Bowser.  Non-selected worlds (and the Petal Isles / Special
    World content) are removed, and the goal is forced to defeating
    Bowser."""
    display_name = "Open World"


class OpenWorldCount(Range):
    """How many of the six worlds are active in open-world mode (1-6).
    Which worlds you get is chosen at random per seed.  Ignored unless
    ``open_world`` is on."""
    display_name = "Open World Count"
    range_start = 1
    range_end = 6
    default = 6


class PalacesRequired(Range):
    """How many Royal Seeds (palaces) must be cleared to unlock Bowser in
    open-world mode.  ``0`` means "all active worlds" (i.e. equal to
    ``open_world_count``).  Values above the active-world count are
    clamped down.  Ignored unless ``open_world`` is on."""
    display_name = "Palaces Required"
    range_start = 0
    range_end = 6
    default = 0


class BadgeGatedCourses(OptionDict):
    """Gate the *entry* of arbitrary courses on possessing specific badge(s).

    A mapping of course name -> badge requirement.  When a course is gated,
    none of its checks (Normal Exit, Wonder Seed, Top of Flag, 10-Coins, ...)
    become reachable in logic until you have received the listed badge(s) from
    Archipelago, so AP fill will never expect you to enter that course before
    the badge is collected.

    The course name is the prefix shown before " - " in a location name, e.g.
    ``"W2: Hot-Hot Hot!"`` (NOT ``"W2: Hot-Hot Hot! - Normal Exit"``).  The
    value is either a single badge item name or a list of them; a list means
    ALL of those badges are required (AND).  Badge names are the AP item names,
    e.g. ``"Spring Feet Badge"``.

    Example yaml::

        badge_gated_courses:
          "W2: Hot-Hot Hot!": "Spring Feet Badge"
          "W4: Maw-Maw Mouthful": ["Dolphin Kick Badge", "Floating High Jump Badge"]

    Unknown course names or unknown badge names abort generation with a clear
    error so a typo is caught immediately.

    NOTE: this is a *logic-side* (randomizer) gate.  The live game has no
    runtime per-course entry lock the mod can write — the only in-game lever is
    the AP-authoritative Wonder-Seed count, which gates whole worlds/areas, not
    single courses.  This option enforces the constraint where it actually
    matters for a randomizer: in the fill + beatability logic.
    """
    display_name = "Badge-Gated Courses"
    default = {}


def before_options_defined(options: dict) -> dict:
    options["open_world"] = OpenWorld
    options["open_world_count"] = OpenWorldCount
    options["palaces_required"] = PalacesRequired
    options["badge_gated_courses"] = BadgeGatedCourses
    return options


def after_options_defined(options: dict) -> dict:
    return options
