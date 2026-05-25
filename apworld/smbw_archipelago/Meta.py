from BaseClasses import Tutorial
from worlds.AutoWorld import World, WebWorld
from .Data import meta_table


class SMBWonderWeb(WebWorld):
    tutorials = [Tutorial(
        "Multiworld Setup Guide",
        "A guide to setting up Super Mario Bros. Wonder integration for Archipelago multiworld games.",
        "English",
        "setup_en.md",
        "setup/en",
        ["Zim"]
    )]


def set_world_description(base_doc: str) -> str:
    if meta_table.get("docs", {}).get("apworld_description", None) is None:
        return base_doc

    if isinstance(meta_table["docs"]["apworld_description"], str):
        base_doc = meta_table["docs"]["apworld_description"]
    else:
        fullstring = ""
        for line in meta_table["docs"]["apworld_description"]:
            fullstring += "\n" + line
        base_doc = fullstring
    return base_doc

def set_world_webworld(web: WebWorld) -> WebWorld:
    if meta_table.get("docs", {}).get("web", {}):
        Web_Config = meta_table["docs"]["web"]

        web.theme = Web_Config.get("theme", web.theme)
        web.game_info_languages = Web_Config.get("game_info_languages", web.game_info_languages)
        web.options_presets = Web_Config.get("options_presets", web.options_presets)
        web.options_page = Web_Config.get("options_page", web.options_page)
        if hasattr(web, 'bug_report_page'):
            web.bug_report_page = Web_Config.get("bug_report_page", web.bug_report_page)
        else:
            web.bug_report_page = Web_Config.get("bug_report_page", None)

        if Web_Config.get("tutorials", []):
            tutorials = []
            for tutorial in Web_Config.get("tutorials", []):
                tutorials.append(Tutorial(
                    tutorial.get("name", "Multiworld Setup Guide"),
                    tutorial.get("description", "A guide to setting up Super Mario Bros. Wonder integration for Archipelago multiworld games."),
                    tutorial.get("language", "English"),
                    tutorial.get("file_name", "setup_en.md"),
                    tutorial.get("link", "setup/en"),
                    tutorial.get("authors", [meta_table.get("creator", meta_table.get("player", "Unknown"))])
                ))
            web.tutorials = tutorials
    return web


world_description: str = set_world_description("""
    Super Mario Bros. Wonder integration for Archipelago.  Course-clear,
    Top-of-Flag, Wonder Seed, and Palace events are detected automatically
    via a Switch subsdk that streams events over LAN; the bridge here
    relays them as Archipelago location checks and pushes received items
    (badges, Royal Seeds) back to the live save.
    """)
world_webworld: SMBWonderWeb = set_world_webworld(SMBWonderWeb())

enable_region_diagram = bool(meta_table.get("enable_region_diagram", False))
