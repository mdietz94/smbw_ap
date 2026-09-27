# SMBW Archipelago — first-time setup

This page walks a new user from "downloaded `smbwonder.apworld`" to
"playing a multiworld seed on a modded Switch under Ryujinx."

## Prerequisites

- **Windows 10/11** (the SMBW Client setup wizard is Windows-only).
- **Ryujinx 1.3.3** with **Super Mario Bros. Wonder v1.0.0** dumped from
  your own cartridge. *Do not* apply the v1.0.1 update — every offset in
  our Switch mod is pinned to v1.0.0.
- A working Archipelago install (see https://archipelago.gg).

The wizard handles the rest (LLVM 19.1.x, CMake, Ninja, Python 3.11,
git submodules, Archipelago pip deps) on first launch.

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

## First-time setup

Click the **SMBW Client** button in the Archipelago Launcher, then type
`/setup` in the chat box. The setup wizard opens in a new window and
walks through:

1. **Probe** — detects which prerequisites are already installed.
2. **Install** — auto-installs everything missing (CMake, Ninja, Python
   3.11, git submodules, Archipelago pip deps via winget; LLVM 19.1.7
   via a portable download to `%LOCALAPPDATA%\SMBWArchipelago\llvm\` —
   ~806 MB download, ~3.3 GB on disk; coexists with any other LLVM
   you have installed).
3. **Junction** — creates the dev-mode junction at
   `vendor/Archipelago/custom_worlds/smbw_archipelago` so subsequent
   AP installs see the apworld directly.
4. **Build** — compiles `subsdk9` from the bundled `switch-mod/` source
   tree. Takes ~5-10 min on a cold build.
5. **Deploy** — copies `subsdk9` + `subsdk9.npdm` (renamed to
   `main.npdm`) into the emulator's mod folder. Pick the target in the
   wizard's dropdown:
   - **Ryujinx** → `%APPDATA%\Ryujinx\mods\contents\010015100b514000\smbwap\exefs\`
   - **Eden (yuzu-family)** → `%APPDATA%\eden\load\010015100b514000\smbwap\exefs\`
     (the folder Eden's *Open Mod Data Location* opens; Linux:
     `~/.local/share/eden/load/...`). Launch Eden once first so its user
     folder exists, or point the path field at a portable install's
     `user\` folder. Other yuzu forks (yuzu, suyu, citron, sudachi) use
     the same layout and are auto-detected under the same target.
   - **SD card** / **Custom folder** → Atmosphere layout for a real Switch.

Re-run `/setup` any time to bump toolchains, re-deploy after a code
update, or switch deploy targets.

## Generate a multiworld

Use Archipelago's normal seed-generation flow. The world's game name is
**Super Mario Bros Wonder**. Per-player options are documented in the
generated YAML template.

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
- **Which mod build am I running?** — the mod prints
  `[smbwap inf] build: <id>` right after `hkMain START`, and the same id
  shows up in the SMBW Client log as `hello: mod_ver=<id>` and in the
  Switch status line of the SMBW tab. The id is
  `<source>@<build date>`: `0.2.0+g3f9a1c2@20260921` for a wizard build of
  release 0.2.0, `g3f9a1c2-dirty@20260921` for a dev checkout. If the
  client logs a *"does not match this apworld"* or *"before build-id
  stamping"* warning on connect, the emulator is loading a stale
  `subsdk9`: re-run `/setup` and check the mod folder it actually reads.

For anything else, open an issue at
https://github.com/mdietz94/smwonder_archipelago/issues.
