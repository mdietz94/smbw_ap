"""Setup wizard for the SMBW Archipelago client.

The user types `/setup` in SMBW Client → SMBW Client's `/setup` handler
spawns `_setup.wizard.run_setup_wizard` in a new window via
`launch_subprocess`. The same flow handles first-time setup and re-runs
(toolchain update, apworld update, switching deploy targets).

The wizard's job is to turn a fresh machine into one that can:
  - build the Switch subsdk for Super Mario Bros. Wonder using the
    LibHakkun framework + LLVM 19 cross-compiler
  - deploy the result to Ryujinx's mods directory (SD card / real Switch
    deploy is M6 territory; deferred)

The packages here are organized so each step is independently testable:

  - `smbwap_file.py`  — `.smbwap` JSON metadata read/write
                        (no I/O dependencies beyond stdlib)
  - `prereqs.py`      — detect LLVM 19, CMake, Ninja, git, Python 3.11,
                        Archipelago submodule + deps, switch-mod hakkun
                        + libs, Windows Dev Mode, Ryujinx
  - `installers.py`   — auto-installers (winget for cmake/ninja/python/git,
                        LLVM 19 tarball download + extract, git submodule
                        update, pip Archipelago deps)
  - `junction.py`     — create the
                        vendor/Archipelago/custom_worlds/smbw_archipelago
                        directory junction (also exposed at
                        scripts/install_smbw_apworld.py)
  - `build.py`        — CMake configure + ninja build of switch-mod
  - `deploy.py`       — copy `subsdk9` + `main.npdm` to the Ryujinx mods
                        folder; size assertion on every copy
  - `wizard.py`       — Kivy multi-page UI (imports Kivy lazily so the rest
                        of the package stays importable on AP-gen hosts)
  - `wizard_cli.py`   — Headless orchestrator + JSON-event CLI. Same
                        probe → install → junction → build → deploy
                        sequencing as the Kivy wizard, but stateless and
                        callable from pytest / CI without Kivy.

Output destinations on the user's machine (all under %APPDATA% /
%LOCALAPPDATA%, off-repo so nothing accidentally enters version control):

  %APPDATA%/SMBWArchipelago/launch-crash.log    ← visible_errors decorator
  %APPDATA%/SMBWArchipelago/launch-warning.log  ← show_launch_warning
  %APPDATA%/SMBWArchipelago/setup_state.json    ← remembers last deploy
  %LOCALAPPDATA%/SMBWArchipelago/ap_deps.ok     ← Archipelago pip marker
  %LOCALAPPDATA%/SMBWArchipelago/llvm/          ← portable LLVM 19 install
"""

from __future__ import annotations

import os
from pathlib import Path


def appdata_root() -> Path:
    """Per-user output root: `%APPDATA%/SMBWArchipelago/` on Windows,
    `~/.local/share/SMBWArchipelago/` elsewhere.

    Honors `SMBWAP_APPDATA_ROOT` as an override — used by tests to
    redirect into a tempdir so the user's real %APPDATA% is never
    touched. Set ONLY in test/CI/harness contexts; the wizard itself
    must never set this.

    Created on first access.
    """
    override = os.environ.get("SMBWAP_APPDATA_ROOT")
    if override:
        root = Path(override)
    else:
        base = os.environ.get("APPDATA")
        if base:
            root = Path(base) / "SMBWArchipelago"
        else:
            root = Path.home() / ".local" / "share" / "SMBWArchipelago"
    root.mkdir(parents=True, exist_ok=True)
    return root


def local_appdata_root() -> Path:
    """Per-machine cache root: `%LOCALAPPDATA%/SMBWArchipelago/` on
    Windows, `~/.cache/SMBWArchipelago/` elsewhere. Used for downloaded
    installer payloads and prereq marker files (anything that shouldn't
    roam between machines and can be regenerated on demand).

    Honors `SMBWAP_LOCALAPPDATA_ROOT` for test isolation.
    """
    override = os.environ.get("SMBWAP_LOCALAPPDATA_ROOT")
    if override:
        root = Path(override)
    else:
        base = os.environ.get("LOCALAPPDATA")
        if base:
            root = Path(base) / "SMBWArchipelago"
        else:
            root = Path.home() / ".cache" / "SMBWArchipelago"
    root.mkdir(parents=True, exist_ok=True)
    return root


def setup_state_path() -> Path:
    return appdata_root() / "setup_state.json"
