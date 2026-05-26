"""Drift guard: ensure `wizard.py` delegates orchestration to
`wizard_cli` instead of calling the leaf phase modules directly.

This mirrors the smo project's same-named test. Rationale: the Kivy
wizard exists to render the orchestrator's output. When per-page
workers reach past `wizard_cli` and call `_setup.prereqs.check_all()`
or `_setup.build.run_build_phase()` directly, the GUI and the headless
CLI drift apart — they end up implementing slightly different
sequencing, and changes to one don't take effect in the other. Catch
that drift at test time.

The check is AST-level (cheap, no runtime imports), so it doesn't need
Kivy to run.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_WIZARD_PY = (
    Path(__file__).resolve().parents[1] / "wizard.py"
)

# Modules wizard.py is ALLOWED to import directly. Everything else
# under _setup. is suspect.
_ALLOWED_DIRECT_IMPORTS = {
    # The orchestrator itself.
    "wizard_cli",
    # Tiny stateless helpers the GUI uses directly (no orchestration in them).
    "deploy",            # detect_ryujinx_path for input field defaults
    "launcher_errors",   # visible_errors / show_launch_error wrappers
    # The package root (__init__) exposes appdata_root / setup_state_path
    # — those are file-system helpers, not orchestration.
    "",
}


def _setup_imports_in_wizard_py() -> set[str]:
    """Return the set of `_setup.*` submodules imported by wizard.py."""
    tree = ast.parse(_WIZARD_PY.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        # `from . import X` or `from .X import Y` (relative).
        if isinstance(node, ast.ImportFrom):
            if node.level == 1 and node.module:
                # `from .submodule import ...`
                imported.add(node.module.split(".")[0])
            elif node.level == 1 and not node.module:
                # `from . import submodule[, ...]`
                for alias in node.names:
                    imported.add(alias.name)
    return imported


def test_wizard_does_not_reach_past_wizard_cli() -> None:
    """Per-page workers should call wizard_cli.run_* functions, not the
    raw `_setup.{prereqs,installers,junction,build}` modules."""
    forbidden = {"prereqs", "installers", "junction", "build"}
    imported = _setup_imports_in_wizard_py()
    leaked = imported & forbidden
    assert not leaked, (
        f"wizard.py imports leaf orchestration modules directly: "
        f"{sorted(leaked)}. Use wizard_cli.run_* instead so the GUI "
        f"and headless CLI stay in lockstep."
    )


def test_wizard_actually_imports_wizard_cli() -> None:
    """Sanity check the drift guard itself — if wizard.py stopped using
    wizard_cli entirely, the forbidden-import check above would still
    pass (vacuously). This catches that."""
    imported = _setup_imports_in_wizard_py()
    assert "wizard_cli" in imported, (
        "wizard.py must import from wizard_cli — that's the seam the "
        "drift guard exists to protect."
    )
