from Options import DefaultOnToggle, Toggle, Choice, Range, PerGameCommonOptions, DeathLink, Visibility
from dataclasses import make_dataclass
from .hooks.Options import before_options_defined, after_options_defined
from .Data import category_table, game_table
from .Locations import victory_names
from .Items import item_table


# Options the Switch mod can't yet enforce in-game; force off and hide
# from the user-visible yaml until the matching milestone lands.  See
# docs/milestones.md M7 "Deferred from M3":
#   button_shuffle      -> M3.6 (button-input suppression)
#   wonder_flower_rando -> M3.5 (Wonder Flower spawn/touch suppression)
#   wonder_effect_rando -> M3.5 (Wonder Effect start suppression)
# Items in the matching categories stay out of the pool because
# Helpers.is_category_enabled checks the option value (which is locked
# to 0 by _LockedOffToggle).
_DEFERRED_OPTIONS = frozenset({
    "button_shuffle",
    "wonder_flower_rando",
    "wonder_effect_rando",
})


class _LockedOffToggle(Toggle):
    """Toggle that is invisible to the yaml templates and always reads 0.

    Any user-supplied value (including ``true``) is coerced to 0 so a
    stale yaml or copy-pasted preset can't accidentally re-enable a
    feature the Switch mod can't yet enforce.
    """
    default = 0
    visibility = Visibility.none

    def __init__(self, value: int = 0) -> None:
        super().__init__(0)

    @classmethod
    def from_text(cls, text: str) -> "_LockedOffToggle":
        return cls(0)

    @classmethod
    def from_any(cls, data) -> "_LockedOffToggle":
        return cls(0)


class FillerTrapPercent(Range):
    """How many fillers will be replaced with traps. 0 means no additional traps, 100 means all fillers are traps."""
    range_end = 100

smbwonder_options = before_options_defined({})

if len(victory_names) > 1:
    goal = {'option_' + v: i for i, v in enumerate(victory_names)}
    smbwonder_options['goal'] = type('goal', (Choice,), goal)
    smbwonder_options['goal'].__doc__ = "Choose your victory condition."

if any(item.get('trap') for item in item_table):
    smbwonder_options["filler_traps"] = FillerTrapPercent

if game_table.get("death_link"):
    smbwonder_options["death_link"] = DeathLink

for category in category_table:
    for option_name in category_table[category].get("yaml_option", []):
        if option_name[0] == "!":
            option_name = option_name[1:]
        if option_name not in smbwonder_options:
            if option_name in _DEFERRED_OPTIONS:
                smbwonder_options[option_name] = type(
                    option_name, (_LockedOffToggle,), {})
                smbwonder_options[option_name].__doc__ = (
                    "Deferred to M7; the Switch mod can't enforce this "
                    "yet, so the option is hidden and locked to off.")
            else:
                smbwonder_options[option_name] = type(option_name, (DefaultOnToggle,), {"default": True})
                smbwonder_options[option_name].__doc__ = "Should items/locations linked to this option be enabled?"

smbwonder_options = after_options_defined(smbwonder_options)
smbwonder_options_data = make_dataclass('SMBWonderOptionsClass', smbwonder_options.items(), bases=(PerGameCommonOptions,))
