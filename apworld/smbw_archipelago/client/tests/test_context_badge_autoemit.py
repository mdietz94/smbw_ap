"""Tests for badge-receipt handling after the AP-authoritative shop
ownership change (2026-06-10).

Previously, receiving a shop badge from AP auto-emitted its "<Badge>
Obtained" LocationCheck, because setting the owned bit also told the
Poplin shop the badge was already owned and locked the in-game purchase.

That coupling is gone: the Switch badge-shop override (SetBadgeShopState)
keeps an AP-granted-but-unbought shop badge purchasable, so the player
completes the check by actually buying it.  These tests pin the new
contract -- receiving a badge item NEVER sends a LocationCheck -- while the
badge bitfield push is undisturbed.  The shop-state mask logic itself lives
in test_context_badge_shop.py.

Same Archipelago-availability guard pattern as the other test_context_*
files.
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
    "ensure conftest.py is loaded); skipping badge-receipt tests.")
class TestContextBadgeReceipt(unittest.IsolatedAsyncioTestCase):

    # Spring Feet Badge: internal_id 4.  A *course* badge -- granted by
    # AP but NOT an AP check.
    SPRING_FEET_ITEM_ID = 100
    SPRING_FEET_BIT = 4

    # Coin Reward Badge: internal_id 9, shop badge with location
    # "Coin Reward Badge Obtained".
    COIN_REWARD_ITEM_ID = 101
    COIN_REWARD_LOC_ID = 501
    COIN_REWARD_BIT = 9

    # Non-badge: a known-good "10 Coin" routed through the increment path.
    TEN_COIN_ITEM_ID = 200

    async def asyncSetUp(self) -> None:  # type: ignore[override]
        from ..context import SMBWContext
        from ..state import BridgeState
        self._SMBWContext = SMBWContext

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
            self.SPRING_FEET_ITEM_ID: "Spring Feet Badge",
            self.COIN_REWARD_ITEM_ID: "Coin Reward Badge",
            self.TEN_COIN_ITEM_ID: "10 Coin",
        }.get(i, f"?{i}")

        self.ctx._location_name_to_id = {
            "Coin Reward Badge Obtained": self.COIN_REWARD_LOC_ID,
        }

    async def asyncTearDown(self) -> None:  # type: ignore[override]
        try:
            await self.ctx.shutdown()
        except Exception:
            pass

    def _location_checks(self) -> list[int]:
        loc_ids: list[int] = []
        for c in self.ctx.send_msgs.await_args_list:
            for m in c.args[0]:
                if m.get("cmd") == "LocationChecks":
                    loc_ids.extend(m["locations"])
        return loc_ids

    async def test_shop_badge_receipt_does_not_send_location_check(self):
        # Receiving the shop badge from AP must NOT auto-complete its
        # "Obtained" location -- the player buys it in-game instead.
        await self.ctx._handle_received_items(
            {"items": [{"item": self.COIN_REWARD_ITEM_ID}]})
        self.assertNotIn(self.COIN_REWARD_LOC_ID, self._location_checks())

    async def test_badge_receipt_still_pushes_bitfield(self):
        # The owned bitfield push is undisturbed (Mario can still equip the
        # AP-granted badge).
        await self.ctx._handle_received_items(
            {"items": [{"item": self.COIN_REWARD_ITEM_ID}]})
        self.ctx.lan_server.send_set_badges_absolute.assert_called_once()

    async def test_badge_receipt_emits_no_badge_check_state(self):
        from ..protocol import CheckKind
        await self.ctx._handle_received_items({"items": [
            {"item": self.SPRING_FEET_ITEM_ID},
            {"item": self.COIN_REWARD_ITEM_ID},
        ]})
        self.assertEqual(
            self.state.count_emitted(CheckKind.BADGE_ACQUIRED), 0)
        self.assertEqual(self._location_checks(), [])

    async def test_non_badge_item_unaffected(self):
        from ..protocol import CheckKind
        await self.ctx._handle_received_items(
            {"items": [{"item": self.TEN_COIN_ITEM_ID}]})
        self.assertEqual(
            self.state.count_emitted(CheckKind.BADGE_ACQUIRED), 0)


if __name__ == "__main__":
    unittest.main()
