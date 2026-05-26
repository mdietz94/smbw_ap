"""Root pytest conftest.

Adds the vendored Archipelago checkout (`vendor/Archipelago/`) to
`sys.path` before any test imports.  The apworld at
`apworld/smbw_archipelago/` imports Archipelago modules (`Utils`,
`BaseClasses`, `worlds.LauncherComponents`, ...) at package load time
via `__init__.py`; without this shim, pytest collection fails before
the first test runs.

Also tells pytest to skip the `vendor/` tree during test discovery.
The setup wizard's junction at
`vendor/Archipelago/custom_worlds/smbw_archipelago` points back at the
source apworld, so without this filter pytest collects every test file
twice and the second collection triggers AP's "Game SMBWonder already
registered" guard.
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


collect_ignore_glob = [
    # Skip the junction that the setup wizard creates back into the
    # source apworld — would double-collect every test under
    # apworld/smbw_archipelago/.
    "vendor/Archipelago/custom_worlds/smbw_archipelago/**",
    # Skip everything under vendor/Archipelago itself — those are
    # upstream Archipelago tests, not ours.
    "vendor/**",
]
