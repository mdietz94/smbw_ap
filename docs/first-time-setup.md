# SMBW Archipelago — first-time setup

This page walks a new user from "downloaded `smbwonder.apworld`" to
"playing a multiworld seed." Two ways to play are supported:

- **A real Switch** running Atmosphere custom firmware (tested on
  Atmosphere 1.11.1 + HATS-2026-05-11), or
- **Ryujinx** on the same PC as the SMBW Client.

The mod, the client, and the setup wizard are the same for both. The only
differences are where the wizard copies the mod (the deploy target) and how
the game reaches the client (over your LAN for a Switch, loopback for
Ryujinx).

## Prerequisites

- **Windows 10/11** (the SMBW Client setup wizard is Windows-only).
- **Super Mario Bros. Wonder v1.0.0**. *Do not* apply the v1.0.1 update:
  every offset in our Switch mod is pinned to v1.0.0.
- A working Archipelago install (see https://archipelago.gg).
- One of:
  - **Real Switch:** a Switch running **Atmosphere** CFW, with SMBW v1.0.0
    installed or on cartridge, an SD card you can mount on the PC (reader
    or USB mass storage, e.g. Hekate UMS), and the Switch on the **same
    LAN** as the PC that runs the SMBW Client.
  - **Emulator:** **Ryujinx 1.3.3** with SMBW v1.0.0 dumped from your own
    cartridge.

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
   `main.npdm`) to the **Deploy target** you picked:

   | Target | Use it for | Writes to |
   |---|---|---|
   | **SD card** | Real Switch, SD card mounted on this PC | `<drive>:\atmosphere\contents\010015100b514000\exefs\` |
   | **Custom folder** | Real Switch, when you copy files to the SD card yourself (FTP, another PC, …) | `<folder>\atmosphere\contents\010015100b514000\exefs\` |
   | **Ryujinx** | Emulator | `%APPDATA%\Ryujinx\mods\contents\010015100b514000\smbwap\exefs\` |
   | **None** | Build only | — |

   The wizard prefills the SD card path with the first mounted drive that
   has an `atmosphere\` folder at its root. For **Custom folder**, copy
   the generated `atmosphere\` folder onto the root of the SD card,
   merging it with the one already there.

### Real Switch: set the Bridge IP

The Switch finds the SMBW Client over your LAN. The wizard's **Bridge IP**
field is prefilled with this PC's LAN address, and that address is
compiled into the mod as the network to search. Leave it as is if the
Switch will play on the same network as this PC. If you will play on a
different network, enter any IPv4 address on that network's /24 (or run
`/setup_ip <addr>` in the client to open the wizard with it filled in),
then rebuild and redeploy. Ryujinx on the same PC always connects over
loopback, so it ignores this setting.

Re-run `/setup` any time to bump toolchains, re-deploy after a code
update, or switch deploy targets.

## Generate a multiworld

Use Archipelago's normal seed-generation flow. The world's game name is
**Super Mario Bros Wonder**. Per-player options are documented in the
generated YAML template.

## Play

1. Start the SMBW Client and click **Connect** (the server address and
   password come from your multiworld host).
2. Start SMBW:
   - **Real Switch:** put the SD card back in, boot into Atmosphere
     (CFW, not stock firmware), and launch SMBW.
   - **Ryujinx:** launch Ryujinx and start SMBW.
3. The game connects to the SMBW Client on its own. On a real Switch,
   allow the Archipelago/Python process through Windows Firewall on
   private networks if prompted: the Switch needs to reach the PC on
   UDP 17776 (discovery) and TCP 17777 (bridge).
4. Pick up a Wonder Seed in-game. It should appear as a check in the AP
   tracker.

## Troubleshooting

- **`%APPDATA%\SMBWArchipelago\launch-crash.log`** — captures any
  uncaught exception from the SMBW Client launcher.
- **`%APPDATA%\SMBWArchipelago\setup_state.json`** — remembers your last
  deploy target.
- **Ryujinx logs** — `%APPDATA%\Ryujinx\Logs\` contains the live game
  log. Lines prefixed `[smbwap` are from our Switch mod.
- **Real Switch never connects** — check that the Switch and PC are on
  the same network, that Windows Firewall isn't blocking UDP 17776 /
  TCP 17777, and that the Bridge IP the mod was built with is on the
  network the Switch is using (see "Real Switch: set the Bridge IP").
- **Real Switch doesn't load the mod** — confirm the files sit at
  `atmosphere\contents\010015100b514000\exefs\subsdk9` and
  `…\exefs\main.npdm` on the SD card, that you booted into Atmosphere,
  and that the game is v1.0.0 (no update installed).

For anything else, open an issue at
https://github.com/mdietz94/smwonder_archipelago/issues.
