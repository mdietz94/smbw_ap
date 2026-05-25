"""Root pytest conftest.

Adds the vendored Archipelago checkout (`vendor/Archipelago/`) to
`sys.path` before any test imports.  The apworld at
`apworld/smbw_archipelago/` imports Archipelago modules (`Utils`,
`BaseClasses`, `worlds.LauncherComponents`, ...) at package load time
via `__init__.py`; without this shim, pytest collection fails before
the first test runs.
"""
from __future__ import annotations

import os
import sys


_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_AP_ROOT = os.path.join(_REPO_ROOT, "vendor", "Archipelago")


if os.path.isfile(os.path.join(_AP_ROOT, "CommonClient.py")):
    if _AP_ROOT not in sys.path:
        sys.path.insert(0, _AP_ROOT)
# Also ensure the repo root is on sys.path so `apworld.smbw_archipelago.*`
# resolves regardless of where pytest is invoked from.
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
