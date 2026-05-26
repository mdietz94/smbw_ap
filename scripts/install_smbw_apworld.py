r"""Dev-mode install: junction the apworld into the Archipelago Launcher.

Creates a Windows directory junction at
  vendor/Archipelago/custom_worlds/smbw_archipelago
pointing at
  apworld/smbw_archipelago
so Archipelago Launcher autodiscovers our apworld (including the
"SMBW Client" Component) on every Launcher start.

Idempotent.  Re-run after a fresh clone or after blowing away the
vendor/Archipelago submodule.

This script supersedes the older `install_smbw_apworld.ps1` — the
PowerShell version called `cmd /c mklink` which resolved to msys2's
shell-shim `cmd` (not Windows' `cmd.exe`) when `$DEVKITPRO\msys2\usr\bin`
was on the user's PATH, and failed with "Cannot run a document in the
middle of a pipeline".  This Python port routes through
`subprocess.run(['cmd', '/c', ...])` which goes through the Windows
loader's app-paths resolver and always lands on the right `cmd.exe`.

Usage:

    python scripts/install_smbw_apworld.py

The implementation lives in `apworld/smbw_archipelago/_setup/junction.py`
so the same code path is reachable from the `/setup` wizard's Junction
phase — no duplication, no drift.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    # Resolve repo root from this script's location: scripts/<this>.py
    repo = Path(__file__).resolve().parent.parent
    # Add the apworld to sys.path so we can import the leaf module
    # without triggering the apworld __init__ (which would require
    # vendor/Archipelago to already be a working Python package).
    sys.path.insert(0, str(repo / "apworld" / "smbw_archipelago"))
    import _setup.junction as J
    import _setup.prereqs as P
    # Override the prereqs.repo_root() lookup so it points at our actual
    # repo (the default walks up from `apworld/smbw_archipelago/_setup/`
    # which assumes the package was imported via apworld.smbw_archipelago).
    P.repo_root = lambda: repo

    result = J.install_junction()
    print(result.message)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
