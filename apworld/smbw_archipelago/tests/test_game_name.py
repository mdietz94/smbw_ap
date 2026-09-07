"""The game name must be identical everywhere it is declared.

Archipelago's apworld uploader compares the `game` key in the apworld
manifest (`archipelago.json`) against the `game:` key of the uploaded
YAML, which comes from `SMBWonderWorld.game` (i.e. `Game.game_name`).
When the two drifted apart the WebHost rejected every upload with:

    APWorld is for 'SMBWonder', but this YAML is for
    'Super Mario Bros Wonder'. Please upload the correct APWorld.

The SMBW Client's `GAME_NAME` is the same string a third time — it is
what the client sends in its AP `Connect` packet, so it has to match the
world's `game` or the server refuses the slot.

The three declarations are read with `ast` rather than imported, so the
test compares the literals as they are written on disk (and as they ship
in the release zip) without dragging in the AP client stack that
`client/context.py` imports at module scope.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

_WORLD_ROOT = Path(__file__).resolve().parent.parent


def _literal_assignment(path: Path, name: str) -> str:
    """Return the value of a module-level `name = <literal>` assignment."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"no module-level `{name}` assignment in {path}")


def test_manifest_game_matches_world_game():
    manifest = json.loads(
        (_WORLD_ROOT / "archipelago.json").read_text(encoding="utf-8"))
    game_name = _literal_assignment(_WORLD_ROOT / "Game.py", "game_name")

    assert manifest["game"] == game_name


def test_client_game_matches_world_game():
    game_name = _literal_assignment(_WORLD_ROOT / "Game.py", "game_name")
    client_game = _literal_assignment(
        _WORLD_ROOT / "client" / "context.py", "GAME_NAME")

    assert client_game == game_name
