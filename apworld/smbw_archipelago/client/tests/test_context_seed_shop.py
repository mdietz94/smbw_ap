"""Tests for AP-authoritative Poplin shop Wonder-Seed rows (2026-09-12).

The Switch seed-row override (``wire.SetSeedShopStateMsg``) is driven by
``SMBWContext._recompute_seed_shop_state`` over
:mod:`seed_shop_table`: ``managed`` = the slots whose locations the server
has for this slot, ``sold`` = the managed slots whose location(s) are
obtained (server-confirmed via ``checked_locations`` or sent this session
via ``_sent_loc_ids``).  These tests pin the slot table against
``location_table``, the all-locations rule for the two ambiguous slots, and
the push points (Connected, RoomUpdate, on-check-emitted).

Same Archipelago-availability guard as the other test_context_* files.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from .. import seed_shop_table


class TestSeedShopTable(unittest.TestCase):
    """Pure-table checks -- no Archipelago import needed."""

    def test_slot_count_matches_switch_constant(self):
        # kSeedShopSlotCount in switch-mod/src/probe/BadgeShop.cpp.
        self.assertEqual(seed_shop_table.SLOT_COUNT, 10)

    def test_covers_every_shop_seed_location_exactly_once(self):
        names = seed_shop_table.all_location_names()
        self.assertEqual(len(names), 12)
        self.assertEqual(len(set(names)), 12, "duplicate location in table")

    def test_location_names_exist_in_locations_json(self):
        import json
        import pathlib
        data = json.loads(
            (pathlib.Path(__file__).parents[2] / "data" / "locations.json")
            .read_text(encoding="utf-8"))
        known = {loc["name"] for loc in data}
        for name in seed_shop_table.all_location_names():
            self.assertIn(name, known)

    def test_matches_location_tables_shop_seed_entries(self):
        """The slot table and the PlayReport-keyed _SHOP_SEED_TABLE must
        describe the same 12 shop seeds -- both derive from the RomFS
        WorldMapInfo NpcTable, so a drift in either is a bug."""
        from .. import location_table
        exact = set(location_table._SHOP_SEED_TABLE.values())
        wildcard = set(location_table._SHOP_SEED_WILDCARD_TABLE.values())
        ours = set(seed_shop_table.all_location_names())
        self.assertEqual(exact | wildcard, ours)

    def test_switch_key_is_unique_per_slot(self):
        keys = [(w, rows, row) for w, rows, row, _ in
                seed_shop_table.SEED_SHOP_SLOTS]
        self.assertEqual(len(keys), len(set(keys)),
                         "two slots share a (world, seed_rows, row) key -- "
                         "the Switch could not tell them apart")

    def test_multi_row_shops_are_contiguous(self):
        """A 3-seed lineup must occupy base+0/1/2 so the Switch's
        `base_slot + row_index` arithmetic lands on the right slot."""
        by_shop: dict[tuple[int, int], list[int]] = {}
        for slot, (w, rows, row, _) in enumerate(
                seed_shop_table.SEED_SHOP_SLOTS):
            by_shop.setdefault((w, rows), []).append(slot - row)
        for shop, bases in by_shop.items():
            self.assertEqual(len(set(bases)), 1,
                             f"{shop} rows disagree on their base slot")

    def test_masks_empty_without_reverse_maps(self):
        managed, sold = seed_shop_table.recompute_masks({}, set())
        self.assertEqual((managed, sold), (0, 0))

    def test_ambiguous_slot_needs_all_locations(self):
        names = seed_shop_table.all_location_names()
        name_to_id = {n: 1000 + i for i, n in enumerate(names)}
        pi_slot, pi_names = next(
            (slot, locs)
            for slot, (_w, _r, _row, locs)
            in enumerate(seed_shop_table.SEED_SHOP_SLOTS) if len(locs) == 2)
        # One of the pair checked -> still purchasable (never block a check).
        one = {name_to_id[pi_names[0]]}
        managed, sold = seed_shop_table.recompute_masks(name_to_id, one)
        self.assertTrue((managed >> pi_slot) & 1)
        self.assertFalse((sold >> pi_slot) & 1)
        # Both checked -> sold out.
        both = {name_to_id[n] for n in pi_names}
        _managed, sold = seed_shop_table.recompute_masks(name_to_id, both)
        self.assertTrue((sold >> pi_slot) & 1)

    def test_single_location_slot_sells_out_on_its_own_check(self):
        names = seed_shop_table.all_location_names()
        name_to_id = {n: 1000 + i for i, n in enumerate(names)}
        slot, locs = next(
            (slot, locs)
            for slot, (_w, _r, _row, locs)
            in enumerate(seed_shop_table.SEED_SHOP_SLOTS) if len(locs) == 1)
        _managed, sold = seed_shop_table.recompute_masks(
            name_to_id, {name_to_id[locs[0]]})
        self.assertTrue((sold >> slot) & 1)

    def test_slot_unmanaged_when_server_lacks_the_location(self):
        names = seed_shop_table.all_location_names()
        name_to_id = {n: 1000 + i for i, n in enumerate(names)}
        # Server has everything except slot 0's location -> slot 0 stays
        # on vanilla behavior rather than being forced purchasable.
        slot0_locs = seed_shop_table.SEED_SHOP_SLOTS[0][3]
        present = {i for n, i in name_to_id.items() if n not in slot0_locs}
        managed, _sold = seed_shop_table.recompute_masks(
            name_to_id, set(), present)
        self.assertFalse(managed & 1)
        self.assertNotEqual(managed, 0)


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
    "ensure conftest.py is loaded); skipping seed-shop tests.")
class TestContextSeedShop(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:  # type: ignore[override]
        from ..context import SMBWContext
        from ..state import BridgeState

        import CommonClient  # type: ignore
        CommonClient.network_data_package["games"].setdefault(
            "Super Mario Bros Wonder",
            {"checksum": "", "item_name_to_id": {}, "location_name_to_id": {}})

        self.names = seed_shop_table.all_location_names()
        self.state = BridgeState()
        self.ctx = SMBWContext(
            server_address=None, password=None, state=self.state)
        self.ctx.auth = "MarioSlot"
        self.ctx.slot = 1
        self.ctx.send_msgs = AsyncMock()
        self.ctx.lan_server = MagicMock()
        self.ctx._location_name_to_id = {
            n: 2000 + i for i, n in enumerate(self.names)}
        self.ctx.server_locations = set(
            self.ctx._location_name_to_id.values())

    async def asyncTearDown(self) -> None:  # type: ignore[override]
        try:
            await self.ctx.shutdown()
        except Exception:
            pass

    async def test_all_slots_managed_nothing_sold(self):
        managed, sold = self.ctx._recompute_seed_shop_state()
        self.assertEqual(managed, (1 << seed_shop_table.SLOT_COUNT) - 1)
        self.assertEqual(sold, 0)

    async def test_inert_before_reverse_maps(self):
        self.ctx._location_name_to_id = {}
        self.assertEqual(self.ctx._recompute_seed_shop_state(), (0, 0))

    async def test_sold_from_checked_locations(self):
        loc = "W1: Poplin Shop - Wonder Seed"
        self.ctx.checked_locations.add(self.ctx._location_name_to_id[loc])
        _managed, sold = self.ctx._recompute_seed_shop_state()
        self.assertEqual(sold, 1)  # slot 0 == W1

    async def test_sold_from_sent_loc_ids(self):
        # Optimistic: a locally-sent check flips the row to SOLD OUT so it
        # doesn't read purchasable during the AP round-trip.
        loc = "W3: Poplin Shop - Wonder Seed"
        self.ctx._sent_loc_ids.add(self.ctx._location_name_to_id[loc])
        _managed, sold = self.ctx._recompute_seed_shop_state()
        self.assertTrue((sold >> 3) & 1)  # slot 3 == W3

    async def test_w4_secret_rows_are_independent(self):
        self.ctx.checked_locations.add(self.ctx._location_name_to_id[
            "W4: Poplin Shop (Secret) - Wonder Seed (100 Coins)"])
        _managed, sold = self.ctx._recompute_seed_shop_state()
        self.assertEqual(sold, 1 << 6)  # slot 6 == the 100-coin shelf only

    async def test_push_sends_wire_message(self):
        self.ctx._push_seed_shop_state()
        self.ctx.lan_server.send_set_seed_shop_state.assert_called_once()
        managed, sold = (
            self.ctx.lan_server.send_set_seed_shop_state.call_args.args)
        self.assertEqual(managed, (1 << seed_shop_table.SLOT_COUNT) - 1)
        self.assertEqual(sold, 0)

    async def test_push_noop_without_lan_server(self):
        self.ctx.lan_server = None
        self.ctx._push_seed_shop_state()  # must not raise

    async def test_room_update_repushes(self):
        await self.ctx._handle_ap_package(
            "RoomUpdate", {"checked_locations": []})
        self.ctx.lan_server.send_set_seed_shop_state.assert_called_once()


if __name__ == "__main__":
    unittest.main()
