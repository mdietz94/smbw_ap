from Options import DefaultOnToggle, Choice, Range, PerGameCommonOptions, DeathLink
from dataclasses import make_dataclass
from .hooks.Options import before_options_defined, after_options_defined
from .Data import category_table, game_table
from .Locations import victory_names
from .Items import item_table


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
            smbwonder_options[option_name] = type(option_name, (DefaultOnToggle,), {"default": True})
            smbwonder_options[option_name].__doc__ = "Should items/locations linked to this option be enabled?"

smbwonder_options = after_options_defined(smbwonder_options)
smbwonder_options_data = make_dataclass('SMBWonderOptionsClass', smbwonder_options.items(), bases=(PerGameCommonOptions,))
