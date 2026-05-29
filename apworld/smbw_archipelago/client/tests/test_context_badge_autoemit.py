"""Tests for the AP-grant -> BADGE_ACQUIRED auto-emit path.

When the bridge receives a badge item from AP, the Switch's container-C
bitfield gets the bit set immediately -- which also tells the Poplin
shop the badge is "already owned" and locks the in-game purchase.
That makes the matching ``"<Badge> Obtained"`` location unreachable
unless the bridge resolves it directly at grant time.  These tests
pin that auto-emit behavior.

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
    "ensure conftest.py is loaded); skipping badge-autoemit tests.")
class TestContextBadgeAutoEmit(unittest.IsolatedAsyncioTestCase):

    # Spring Feet Badge: internal_id 4, location "Spring Feet Badge Obtained"
    SPRING_FEET_ITEM_ID = 100
    SPRING_FEET_LOC_ID = 500
    SPRING_FEET_BIT = 4

    # Coin Reward Badge: internal_id 9, location "Coin Reward Badge Obtained"
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
            "Spring Feet Badge Obtained": self.SPRING_FEET_LOC_ID,
            "Coin Reward Badge Obtained": self.COIN_REWARD_LOC_ID,
        }

    async def asyncTearDown(self) -> None:  # type: ignore[override]
        try:
            await self.ctx.shutdown()
        except Exception:
            pass

    # ---- Core auto-emit behavior --------------------------------------

    async def test_badge_receipt_sends_location_check(self):
        await self.ctx._handle_received_items(
            {"items": [{"item": self.COIN_REWARD_ITEM_ID}]})
        # SetBadgesAbsolute still flows -- the bitfield push wasn't disturbed.
        self.ctx.lan_server.send_set_badges_absolute.assert_called_once()
        # And a LocationChecks message for "Coin Reward Badge Obtained" fires.
        sent = [c.args[0] for c in self.ctx.send_msgs.await_args_list]
        loc_checks = [
            msg[0] for msg in sent
            if msg and msg[0].get("cmd") == "LocationChecks"
        ]
        self.assertTrue(
            any(self.COIN_REWARD_LOC_ID in m["locations"] for m in loc_checks),
            f"expected LocationChecks containing {self.COIN_REWARD_LOC_ID}, "
            f"got {sent!r}")

    async def test_badge_receipt_records_in_bridge_state(self):
        from ..protocol import CheckKind
        await self.ctx._handle_received_items(
            {"items": [{"item": self.SPRING_FEET_ITEM_ID}]})
        self.assertTrue(self.state.has_emitted(
            CheckKind.BADGE_ACQUIRED, self.SPRING_FEET_BIT))

    async def test_badge_receipt_dedups_across_replays(self):
        # Two ReceivedItems batches with the same badge -- the second
        # must NOT trigger a second LocationCheck or a second state record.
        await self.ctx._handle_received_items(
            {"items": [{"item": self.COIN_REWARD_ITEM_ID}]})
        first_send_count = self.ctx.send_msgs.await_count
        await self.ctx._handle_received_items(
            {"items": [{"item": self.COIN_REWARD_ITEM_ID}]})
        self.assertEqual(self.ctx.send_msgs.await_count, first_send_count,
                         "second receipt of the same badge re-sent")

    async def test_multiple_badges_in_one_batch_each_get_a_check(self):
        await self.ctx._handle_received_items({"items": [
            {"item": self.SPRING_FEET_ITEM_ID},
            {"item": self.COIN_REWARD_ITEM_ID},
        ]})
        sent = [c.args[0] for c in self.ctx.send_msgs.await_args_list]
        loc_ids: set[int] = set()
        for msg in sent:
            for m in msg:
                if m.get("cmd") == "LocationChecks":
                    loc_ids.update(m["locations"])
        self.assertEqual(
            loc_ids, {self.SPRING_FEET_LOC_ID, self.COIN_REWARD_LOC_ID})

    # ---- Negative paths -----------------------------------------------

    async def test_non_badge_item_does_not_emit_badge_check(self):
        # "10 Coin" takes the increment path; badge auto-emit must not fire.
        from ..protocol import CheckKind
        await self.ctx._handle_received_items(
            {"items": [{"item": self.TEN_COIN_ITEM_ID}]})
        self.assertEqual(
            self.state.count_emitted(CheckKind.BADGE_ACQUIRED), 0)

    async def test_probe_active_suppresses_location_check(self):
        """While a probe is running, the BADGE_ACQUIRED auto-emit still
        runs but ``handle_check_emitted``'s probe guard drops the
        outbound LocationCheck.  The dev workflow stays clean -- AP
        locations don't get spuriously ticked off mid-probe."""
        self.ctx.set_badge_probe_mask(1 << 7)
        self.ctx.send_msgs.reset_mock()  # ignore the probe-induced sync call

        await self.ctx._handle_received_items(
            {"items": [{"item": self.COIN_REWARD_ITEM_ID}]})

        # No LocationCheck message went out.
        sent_msgs = [
            m for c in self.ctx.send_msgs.await_args_list
            for m in c.args[0]
        ]
        self.assertFalse(
            any(m.get("cmd") == "LocationChecks" for m in sent_msgs),
            f"expected no LocationChecks during probe, got {sent_msgs!r}")


if __name__ == "__main__":
    unittest.main()
