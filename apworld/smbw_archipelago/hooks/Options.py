"""Hooks for adding/modifying AP options before SMBWonder's options
dataclass is built.  `after_options_defined` currently no-ops; populate
`options` in `before_options_defined` to register new YAML-configurable
options.
"""
from Options import Toggle, Range


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


class CharacterBlockSanity(Toggle):
    """Character-block sanity (default OFF).  Adds ~154 AP checks, one per
    (course, character) hidden ``ObjectBlockClarityCharacter`` block -- the
    player-specific blocks that only the matching character can materialize
    and bump (e.g. the Search-Party blocks).  Each block is its own check;
    no character items are added to the pool (all 12 characters are
    vanilla-available), so this adds locations without new logic gates.
    Requires the Switch mod's GetDamageReactionPlayerNo hook + a client that
    understands the ``char_block_hit`` wire event."""
    display_name = "Character Block Sanity"


class PalacesRequired(Range):
    """How many Royal Seeds (palaces) must be cleared to unlock Bowser in
    open-world mode.  ``0`` means "all active worlds" (i.e. equal to
    ``open_world_count``).  Values above the active-world count are
    clamped down.  Ignored unless ``open_world`` is on."""
    display_name = "Palaces Required"
    range_start = 0
    range_end = 6
    default = 0


def before_options_defined(options: dict) -> dict:
    options["open_world"] = OpenWorld
    options["open_world_count"] = OpenWorldCount
    options["palaces_required"] = PalacesRequired
    # Default-OFF Toggle; pre-seeding here means the category-driven loop in
    # Options.py (which would otherwise auto-create a DefaultOnToggle for any
    # category yaml_option) leaves it alone -- it only fills options NOT
    # already present.  So "Character Block" locations stay disabled unless
    # the player opts in.
    options["character_block_sanity"] = CharacterBlockSanity
    return options


def after_options_defined(options: dict) -> dict:
    return options
