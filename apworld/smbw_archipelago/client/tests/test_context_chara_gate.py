"""Tests for the AP-authoritative character-selection gate mask.

The 12 characters are AP items (7 base precollected + 5 "Character
(Easy)" pool items); the Switch forces a committed selection of a
locked character onto a random unlocked one (CharaSelectCommit hook +
per-frame sweep).  The gate is ALWAYS on -- no slot_data flag: the mask
derives purely from received character items, so it stays 0 (inert)
until any arrive.  These tests pin the client-side mask derivation: the
roster bit order (the game's enum order -- Nabbit at roster index 7,
Yoshis at 8-11), the no-items-inert fallback, and the Connected /
ReceivedItems pushes.

Same Archipelago-availability guard pattern as the other
test_context_* files.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock


def _try_import_archipelago() -> bool:
    try:
        import CommonClient  # noqa: F401
        return True
    except Exception:
        return False


_ARCHIPELAGO_AVAILABLE = _try_import_archipelago()


@unittest.skipUnless(
    _ARCHIPELAGO_AVAILABLE,
    "Archipelago not importable (run `git submodule update --init` and "
    "ensure conftest.py is loaded); skipping chara-gate tests.")
class TestContextCharaGate(unittest.IsolatedAsyncioTestCase):

    # Item ids are arbitrary; the lookup mock maps them to item names.
    MARIO_ID = 400
    LUIGI_ID = 401
    NABBIT_ID = 407
    GREEN_YOSHI_ID = 408
    LIGHT_BLUE_YOSHI_ID = 411
    OTHER_ID = 420

    async def asyncSetUp(self) -> None:  # type: ignore[override]
        from ..context import SMBWContext
        from ..state import BridgeState

        import CommonClient  # type: ignore
        CommonClient.network_data_package["games"].setdefault(
            "Super Mario Bros Wonder",
            {"checksum": "", "item_name_to_id": {}, "location_name_to_id": {}})

        self.state = BridgeState()
        self.ctx = SMBWContext(
            server_address=None, password=None, state=self.state)
        self.ctx.auth = "MarioSlot"
        self.ctx.slot = 1
        self.ctx.player_names = {1: "MarioSlot"}

        self.ctx.send_msgs = AsyncMock()
        self.ctx.update_death_link = AsyncMock()
        self.ctx.lan_server = MagicMock()

        self.ctx.item_names = MagicMock()
        self.ctx.item_names.lookup_in_game = lambda i: {
            self.MARIO_ID: "Mario",
            self.LUIGI_ID: "Luigi",
            self.NABBIT_ID: "Nabbit",
            self.GREEN_YOSHI_ID: "Green Yoshi",
            self.LIGHT_BLUE_YOSHI_ID: "Light-Blue Yoshi",
            self.OTHER_ID: "Standee",
        }.get(i, f"?{i}")

    def _recv(self, *item_ids: int) -> None:
        for iid in item_ids:
            it = MagicMock()
            it.item = iid
            self.ctx.items_received.append(it)

    # ---- mask derivation -------------------------------------------------

    def test_no_items_is_zero(self) -> None:
        # No character items received yet -> mask 0 keeps the Switch gate
        # inert (never strand the player with no pickable character; also
        # the natural state of a seed without character items).
        self.assertEqual(self.ctx._recompute_unlocked_chara_mask(), 0)

    def test_roster_bit_positions(self) -> None:
        # Roster order is the GAME's enum order: Mario bit 0, Nabbit bit
        # 7 (before the Yoshis), Green Yoshi bit 8, Light-Blue Yoshi bit
        # 11.
        self._recv(self.MARIO_ID)
        self.assertEqual(self.ctx._recompute_unlocked_chara_mask(), 1 << 0)
        self._recv(self.NABBIT_ID)
        self.assertEqual(
            self.ctx._recompute_unlocked_chara_mask(), (1 << 0) | (1 << 7))
        self._recv(self.GREEN_YOSHI_ID, self.LIGHT_BLUE_YOSHI_ID)
        self.assertEqual(
            self.ctx._recompute_unlocked_chara_mask(),
            (1 << 0) | (1 << 7) | (1 << 8) | (1 << 11))

    def test_non_character_items_ignored(self) -> None:
        self._recv(self.OTHER_ID)
        self.assertEqual(self.ctx._recompute_unlocked_chara_mask(), 0)
        self._recv(self.LUIGI_ID)
        self.assertEqual(self.ctx._recompute_unlocked_chara_mask(), 1 << 1)

    # ---- push triggers -----------------------------------------------------

    async def test_received_items_pushes_mask(self) -> None:
        self._recv(self.MARIO_ID, self.NABBIT_ID)
        await self.ctx._handle_received_items({"items": []})
        self.ctx.lan_server.send_set_unlocked_charas.assert_called_with(
            (1 << 0) | (1 << 7))

    async def test_connected_pushes_mask(self) -> None:
        # Connected pushes the current mask unconditionally (no slot_data
        # flag) so a locked pre-selected character is repaired even when
        # the connect-time ReceivedItems batch is empty.
        self._recv(self.MARIO_ID)
        await self.ctx._handle_ap_package("Connected", {"slot_data": {}})
        self.ctx.lan_server.send_set_unlocked_charas.assert_called_with(1 << 0)

    async def test_connected_no_items_pushes_zero(self) -> None:
        await self.ctx._handle_ap_package("Connected", {"slot_data": {}})
        self.ctx.lan_server.send_set_unlocked_charas.assert_called_with(0)
