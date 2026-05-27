# SMBW Archipelago — first-time setup

This page walks a new user from "downloaded `smbwonder.apworld`" to
"playing a multiworld seed on a modded Switch under Ryujinx."

## Prerequisites

- **Windows 10/11** (the SMBW Client setup wizard is Windows-only).
- **Ryujinx 1.3.3** with **Super Mario Bros. Wonder v1.0.0** dumped from
  your own cartridge. *Do not* apply the v1.0.1 update — every offset in
  our Switch mod is pinned to v1.0.0.
- A working Archipelago install (see https://archipelago.gg).

The wizard handles the rest (devkitPro, CMake, Ninja, Python 3.11, git
submodules, Archipelago pip deps) on first launch.

## Install

1. Download `smbwonder.apworld` and `smbwonder.apworld.sha256` from the
   release page.
2. (Optional but recommended) verify the download:
   ```pwsh
   sha256sum -c smbwonder.apworld.sha256
   ```
3. Drop `smbwonder.apworld` into Archipelago's `custom_worlds/`
   directory (where you have Archipelago installed).
4. Launch Archipelago — a new **SMBW Client** button appears in the
   Launcher.

## Generate a multiworld

Use Archipelago's normal seed-generation flow. The world's game name is
**Super Mario Bros Wonder**. Per-player options are documented in the
generated YAML template.

Generation produces a `.smbwap` file per slot — that's the entry point
for each player.

## Launch SMBW Client

Double-click your `.smbwap` file. The Archipelago Launcher resolves the
extension and opens **SMBW Client** with your slot name pre-filled.

If this is your first run, type `/setup` in the SMBW Client chat box.
The setup wizard opens in a new window and walks through:

1. **Probe** — detects which prerequisites are already installed.
2. **Install** — auto-installs everything missing (devkitPro, CMake,
   Ninja, Python 3.11, git submodules, Archipelago pip deps). devkitPro's
   own installer UI will pop up; click through it.
3. **Junction** — creates the dev-mode junction at
   `vendor/Archipelago/custom_worlds/smbw_archipelago` so subsequent
   AP installs see the apworld directly.
4. **Build** — compiles `subsdk9` from the bundled `switch-mod/` source
   tree. Takes ~5-10 min on a cold build.
5. **Deploy** — copies `subsdk9` + `subsdk9.npdm` (renamed to
   `main.npdm`) into Ryujinx's mods directory at
   `%APPDATA%\Ryujinx\mods\contents\010015100b514000\smbwap\exefs\`.

Re-run `/setup` any time to bump toolchains, re-deploy after a code
update, or switch deploy targets.

## Play

1. Launch Ryujinx and start SMBW.
2. In SMBW Client, click **Connect** (the server address and password
   come from your multiworld host).
3. Pick up a Wonder Seed in-game — it should appear as a check in the AP
   tracker.

## Troubleshooting

- **`%APPDATA%\SMBWArchipelago\launch-crash.log`** — captures any
  uncaught exception from the SMBW Client launcher.
- **`%APPDATA%\SMBWArchipelago\setup_state.json`** — remembers your last
  deploy target.
- **Ryujinx logs** — `%APPDATA%\Ryujinx\Logs\` contains the live game
  log. Lines prefixed `[smbwap` are from our Switch mod.

For anything else, open an issue at
https://github.com/mdietz94/smwonder_archipelago/issues.
