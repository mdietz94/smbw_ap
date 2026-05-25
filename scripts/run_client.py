"""Dev entry point: launch the SMBW Client without the Archipelago Launcher.

Sets up `sys.path` so the vendored Archipelago checkout is importable
BEFORE the apworld's `__init__.py` (which pulls Archipelago at module
load) runs.  Then forwards CLI args to `apworld.smbw_archipelago.client.main.launch`.

Usage:
    python scripts/run_client.py --connect=localhost:38281 --name=Mario
    python scripts/run_client.py --nogui --connect=localhost:38281 --name=Mario

The Archipelago Launcher's "SMBW Client" button doesn't need this shim --
Launcher already loads CommonClient etc. before the apworld is imported.
"""
from __future__ import annotations

import os
import sys


_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir))
_AP_ROOT = os.path.join(_REPO_ROOT, "vendor", "Archipelago")


def _setup_sys_path() -> None:
    if not os.path.isfile(os.path.join(_AP_ROOT, "CommonClient.py")):
        sys.stderr.write(
            f"error: Archipelago not found at {_AP_ROOT}.  Did you run "
            f"`git submodule update --init --recursive`?\n"
        )
        raise SystemExit(2)
    if _AP_ROOT not in sys.path:
        sys.path.insert(0, _AP_ROOT)
    # NOTE: we deliberately DO NOT add _REPO_ROOT to sys.path.  Loading
    # `apworld.smbw_archipelago` from the source tree at the same time
    # Archipelago's worlds.AutoWorldRegister loads it from the
    # `vendor/Archipelago/custom_worlds/smbw_archipelago.apworld` zip
    # double-registers SMBWonderWorld and crashes.  Instead: ensure the
    # zip is installed (built by `scripts/build_apworld.py`), then import
    # via `worlds.smbw_archipelago.*` after AP's worlds loader has run.


def main() -> None:
    _setup_sys_path()
    # Importing `worlds` triggers Archipelago's AutoWorldRegister scan,
    # which loads our .apworld from custom_worlds/.  After this, the
    # apworld is accessible under `worlds.smbw_archipelago.*`.
    import worlds  # noqa: F401
    try:
        from worlds.smbw_archipelago.client.main import launch  # type: ignore
    except ModuleNotFoundError as e:
        sys.stderr.write(
            f"error: smbw_archipelago apworld not loaded by Archipelago.  "
            f"Run `python scripts/build_apworld.py` then copy "
            f"`build/smbw_archipelago.apworld` to "
            f"`vendor/Archipelago/custom_worlds/`.\n"
            f"Original error: {e}\n"
        )
        raise SystemExit(2)
    launch(*sys.argv[1:])


if __name__ == "__main__":
    main()
