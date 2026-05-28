"""Tests for the "10 Coin" inbound grant + TEN_COIN refund wiring in
:mod:`...client.context`.

Exercises:
  1. Receiving a "10 Coin" AP item routes to
     ``lan_server.send_increment_hash_keyed(flower_coin_hash, +10)``.
  2. ``handle_check_emitted(TEN_COIN)`` sends -10 to undo the in-game
     auto-add -- AP authoritative over what each block grants.
  3. The refund is GATED on ``ten_coin_sanity``: with sanity OFF, the
     block isn't an AP location and no inbound item is coming back, so
     subtracting would just drain the player's purple coins.
  4. ``_handle_ap_package("Connected", ...)`` reads
     ``slot_data["ten_coin_sanity"]`` and toggles the gate (defaults
     ON to match the yaml ``DefaultOnToggle`` when the key is missing).

Same Archipelago-availability guard pattern as
``test_context_deathlink.py``.
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
    "ensure conftest.py is loaded); skipping context 10-coin tests.")
class TestContextTenCoin(unittest.IsolatedAsyncioTestCase):

    FLOWER_COIN_HASH = 0xF4EE6827

    async def asyncSetUp(self) -> None:  # type: ignore[override]
        from ..context import SMBWContext
        from ..protocol import CheckEmitted, CheckKind
        from ..state import BridgeState
        self._SMBWContext = SMBWContext
        self._CheckEmitted = CheckEmitted
        self._CheckKind = CheckKind

        # CommonContext.__init__ reads `network_data_package["games"][game]`
        # to seed its checksums dict; in a pytest run our world may not
        # have been registered before AP built the data package (depends on
        # pytest-xdist worker load order), so stub the entry to avoid
        # KeyError. Mirrors the pattern in test_context_deathlink.py. The
        # key MUST be the full game name (`SMBWContext.game`), not the
        # short slug.
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

        self.ctx.send_msgs = AsyncMock()             # type: ignore[assignment]
        self.ctx.update_death_link = AsyncMock()     # type: ignore[assignment]
        self.ctx.lan_server = MagicMock()

        # AP id 1 -> "10 Coin".  item_names.lookup_in_game is what
        # _handle_received_items uses to resolve names.
        self.ctx.item_names = MagicMock()
        self.ctx.item_names.lookup_in_game = lambda i: {
            1: "10 Coin", 2: "Spring Feet Badge"}.get(i, f"?{i}")

        # Location-name table for the TEN_COIN lookup path.  The first
        # course's coin #1 is the convenient round-trip target (its
        # stage_key + coin_index 0 is in location_table._TEN_COIN_TABLE).
        self.ctx._location_name_to_id = {
            "W1: Welcome to the Flower Kingdom! - 10 Coin #1": 9001,
        }

    async def asyncTearDown(self) -> None:  # type: ignore[override]
        try:
            await self.ctx.shutdown()
        except Exception:
            pass

    # ---- inbound: "10 Coin" item received -------------------------------

    async def test_ten_coin_item_received_sends_positive_increment(self):
        await self.ctx._handle_received_items({"items": [{"item": 1}]})
        self.ctx.lan_server.send_increment_hash_keyed.assert_called_once_with(
            self.FLOWER_COIN_HASH, 10)

    async def test_ten_coin_inbound_grant_not_gated_by_sanity(self):
        # In a multiworld a peer with sanity ON can ship a 10 Coin to
        # a slot with sanity OFF; the recipient should still get +10.
        self.ctx.ten_coin_sanity_enabled = False
        await self.ctx._handle_received_items({"items": [{"item": 1}]})
        self.ctx.lan_server.send_increment_hash_keyed.assert_called_once_with(
            self.FLOWER_COIN_HASH, 10)

    async def test_non_coin_item_does_not_send_increment(self):
        # Spring Feet Badge takes the badge branch (covered by
        # SetBadgesAbsolute), not the increment path.
        await self.ctx._handle_received_items({"items": [{"item": 2}]})
        self.ctx.lan_server.send_increment_hash_keyed.assert_not_called()

    # ---- outbound: TEN_COIN check fires refund -------------------------

    async def test_ten_coin_check_with_sanity_on_refunds_negative(self):
        self.ctx.ten_coin_sanity_enabled = True
        check = self._CheckEmitted(
            kind=self._CheckKind.TEN_COIN,
            stage_key=2937190396,                      # W1-1 stage_key
            metadata={"coin_index": 0})
        await self.ctx.handle_check_emitted(check)
        self.ctx.lan_server.send_increment_hash_keyed.assert_called_once_with(
            self.FLOWER_COIN_HASH, -10)
        # LocationCheck still flows -- the refund is a parallel side-channel.
        self.ctx.send_msgs.assert_awaited_once()

    async def test_ten_coin_check_with_sanity_off_no_refund(self):
        """The critical gate: with sanity OFF, picking up a 10-coin
        block must NOT trigger the refund.  The block isn't an AP
        location, no item is coming back, and subtracting would just
        drain the player's purple coins for free."""
        self.ctx.ten_coin_sanity_enabled = False
        check = self._CheckEmitted(
            kind=self._CheckKind.TEN_COIN,
            stage_key=2937190396,
            metadata={"coin_index": 0})
        await self.ctx.handle_check_emitted(check)
        self.ctx.lan_server.send_increment_hash_keyed.assert_not_called()

    async def test_ten_coin_check_refund_deduped_per_session(self):
        # Second handle on the same loc_id should NOT re-refund -- the
        # _sent_loc_ids dedup gate sits above the refund branch.
        self.ctx.ten_coin_sanity_enabled = True
        check = self._CheckEmitted(
            kind=self._CheckKind.TEN_COIN,
            stage_key=2937190396,
            metadata={"coin_index": 0})
        await self.ctx.handle_check_emitted(check)
        await self.ctx.handle_check_emitted(check)
        self.assertEqual(
            self.ctx.lan_server.send_increment_hash_keyed.call_count, 1)

    async def test_non_ten_coin_check_does_not_refund(self):
        """A WONDER_SEED check (or any other kind) must NOT send a
        refund -- only TEN_COIN does."""
        self.ctx.ten_coin_sanity_enabled = True
        # Pre-populate a known wonder-seed location id so the AP send
        # path resolves without warning.
        self.ctx._location_name_to_id[
            "W1: Welcome to the Flower Kingdom! - Wonder Seed"] = 7777
        check = self._CheckEmitted(
            kind=self._CheckKind.WONDER_SEED,
            stage_key=2937190396)
        await self.ctx.handle_check_emitted(check)
        self.ctx.lan_server.send_increment_hash_keyed.assert_not_called()

    # ---- Connected -> slot_data ten_coin_sanity toggle -----------------

    async def test_connected_slot_data_disables_sanity(self):
        # Default is ON; slot_data False must flip it off.
        await self.ctx._handle_ap_package(
            "Connected", {"slot_data": {"ten_coin_sanity": False}})
        self.assertFalse(self.ctx.ten_coin_sanity_enabled)

    async def test_connected_slot_data_enables_sanity(self):
        self.ctx.ten_coin_sanity_enabled = False
        await self.ctx._handle_ap_package(
            "Connected", {"slot_data": {"ten_coin_sanity": True}})
        self.assertTrue(self.ctx.ten_coin_sanity_enabled)

    async def test_connected_missing_slot_data_defaults_on(self):
        # No key at all -> match the yaml ``DefaultOnToggle`` default.
        self.ctx.ten_coin_sanity_enabled = False
        await self.ctx._handle_ap_package("Connected", {})
        self.assertTrue(self.ctx.ten_coin_sanity_enabled)


if __name__ == "__main__":
    unittest.main()
