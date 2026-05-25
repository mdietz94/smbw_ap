"""Tests for the M2.3 badge-probe override in :mod:`...client.context`.

Exercises the iterative-discovery flow:
  1. ``set_badge_probe_mask`` overrides what ``_recompute_badge_mask``
     returns -- AP items_received is bypassed while the override is set.
  2. Clearing the probe restores the AP-derived mask.
  3. Setting the probe immediately pushes a SetBadgesAbsolute (no wait
     for the 2 s tick) so the player sees the new bit right away.

Archipelago is loaded onto sys.path by the repo-root conftest.py.  We
gate with @skipUnless so the suite stays green in environments without
the vendor/Archipelago submodule.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock


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
    "ensure conftest.py is loaded); skipping context badge-probe tests.")
class TestContextBadgeProbe(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:  # type: ignore[override]
        # Mirror test_context_deathlink's bootstrap -- CommonContext.__init__
        # spawns a keep_alive Task that needs a live event loop, hence
        # asyncSetUp.  Imports are local so the @skipUnless short-circuits.
        from ..context import SMBWContext
        from ..state import BridgeState
        self._SMBWContext = SMBWContext

        import CommonClient  # type: ignore
        CommonClient.network_data_package["games"].setdefault(
            "SMBWonder",
            {"checksum": "", "item_name_to_id": {}, "location_name_to_id": {}})

        self.state = BridgeState()
        self.ctx = SMBWContext(
            server_address=None, password=None, state=self.state)
        # Stand-in LAN server so set_badge_probe_mask's "push now" hook
        # is observable without spinning up a real asyncio TCP server.
        self.ctx.lan_server = MagicMock()

    async def asyncTearDown(self) -> None:  # type: ignore[override]
        try:
            await self.ctx.shutdown()
        except Exception:
            pass

    def test_default_no_probe_returns_zero_with_empty_items(self):
        # No items_received, no probe -> mask is 0.
        self.assertEqual(self.ctx._recompute_badge_mask(), 0)
        self.assertIsNone(self.ctx._badge_probe_mask)

    def test_set_probe_overrides_recompute(self):
        self.ctx.set_badge_probe_mask(1 << 7)
        self.assertEqual(self.ctx._recompute_badge_mask(), 1 << 7)
        # And pushes sync immediately so the badge is visible without
        # waiting for the 2 s tick.
        self.ctx.lan_server._push_badge_sync_now.assert_called_once()

    def test_set_probe_zero_is_distinct_from_none(self):
        """Probe=0 should still override AP-derived mask (showing an empty
        bitfield); probe=None should restore AP-derived behavior.  This
        matters when the user wants to confirm the override is active by
        seeing the in-game badge inventory go empty."""
        self.ctx.set_badge_probe_mask(0)
        self.assertEqual(self.ctx._recompute_badge_mask(), 0)
        self.assertEqual(self.ctx._badge_probe_mask, 0)

    def test_clear_probe_restores_ap_derived(self):
        self.ctx.set_badge_probe_mask(1 << 7)
        self.assertEqual(self.ctx._recompute_badge_mask(), 1 << 7)
        self.ctx.set_badge_probe_mask(None)
        self.assertIsNone(self.ctx._badge_probe_mask)
        self.assertEqual(self.ctx._recompute_badge_mask(), 0)
        # Each set_badge_probe_mask call pushes sync -> 2 calls total.
        self.assertEqual(self.ctx.lan_server._push_badge_sync_now.call_count, 2)

    def test_probe_survives_multiple_sets(self):
        for bit in (3, 7, 42):
            self.ctx.set_badge_probe_mask(1 << bit)
            self.assertEqual(self.ctx._recompute_badge_mask(), 1 << bit)

    def test_set_probe_without_lan_server_is_safe(self):
        # User can set the probe before the Switch handshake completes;
        # the push-now call should silently no-op rather than crash.
        self.ctx.lan_server = None
        self.ctx.set_badge_probe_mask(1 << 5)
        self.assertEqual(self.ctx._badge_probe_mask, 1 << 5)
        self.assertEqual(self.ctx._recompute_badge_mask(), 1 << 5)

    # ---- handle_check_emitted guard while probe is active ----------

    async def test_badge_check_suppressed_while_probe_active(self):
        """The Switch's diff-on-overwrite path fires BadgeAcquired for
        the previous probe's bit each time we change probes; those
        events must NOT reach AP, or we'd tick off real locations
        spuriously during exploration."""
        from ..protocol import CheckEmitted, CheckKind
        from unittest.mock import AsyncMock
        self.ctx.send_msgs = AsyncMock()
        self.ctx.set_badge_probe_mask(1 << 4)
        await self.ctx.handle_check_emitted(CheckEmitted(
            kind=CheckKind.BADGE_ACQUIRED, stage_key=4))
        self.ctx.send_msgs.assert_not_called()

    async def test_non_badge_check_still_fires_under_probe(self):
        """Only BADGE_ACQUIRED is suppressed; other check kinds (e.g.
        WONDER_SEED, TOP_OF_FLAG) flow through normally so a probe
        session doesn't accidentally swallow in-progress play events.
        We just need a known-good location to lookup_name on; W1-1
        Wonder Seed is in the table."""
        from ..protocol import CheckEmitted, CheckKind
        from unittest.mock import AsyncMock
        self.ctx.send_msgs = AsyncMock()
        self.ctx._location_name_to_id = {
            "W1: Welcome to the Flower Kingdom! - Wonder Seed": 12345,
        }
        self.ctx.set_badge_probe_mask(1 << 4)
        await self.ctx.handle_check_emitted(CheckEmitted(
            kind=CheckKind.WONDER_SEED, stage_key=2937190396))
        self.ctx.send_msgs.assert_awaited_once()

    async def test_badge_check_fires_when_probe_cleared(self):
        from ..protocol import CheckEmitted, CheckKind
        from unittest.mock import AsyncMock
        self.ctx.send_msgs = AsyncMock()
        # Probe active -> drop.
        self.ctx.set_badge_probe_mask(1 << 4)
        await self.ctx.handle_check_emitted(CheckEmitted(
            kind=CheckKind.BADGE_ACQUIRED, stage_key=4))
        self.ctx.send_msgs.assert_not_called()
        # Clear probe -> next BADGE_ACQUIRED check goes through.
        self.ctx._location_name_to_id = {"Spring Feet Badge Obtained": 999}
        self.ctx.set_badge_probe_mask(None)
        await self.ctx.handle_check_emitted(CheckEmitted(
            kind=CheckKind.BADGE_ACQUIRED, stage_key=4))
        self.ctx.send_msgs.assert_awaited_once()

    # ---- runtime invalid-bit set ------------------------------------

    def test_invalid_set_starts_empty(self):
        self.assertEqual(self.ctx._badge_probe_invalid, set())

    def test_invalid_set_skipped_by_next_unmapped_bit(self):
        from .. import badge_table
        self.ctx._badge_probe_invalid = {1, 2, 3, 5, 6, 7}
        # First unmapped+non-invalid bit higher than -1.
        nxt = badge_table.next_unmapped_bit(
            after=-1, extra_skip=self.ctx._badge_probe_invalid)
        self.assertNotIn(nxt, self.ctx._badge_probe_invalid)
        self.assertNotIn(nxt, badge_table.mapped_bits())


class TestParseBitSpec(unittest.TestCase):
    """Pure-function tests for the bit-spec parser commands use to
    interpret /badge_probe_range and /badge_probe_invalid arguments."""

    def setUp(self):
        from ..commands import _parse_bit_spec
        self.parse = _parse_bit_spec

    def test_single_bit(self):
        self.assertEqual(self.parse("7"), {7})

    def test_hex_bit(self):
        self.assertEqual(self.parse("0x10"), {16})

    def test_range_inclusive(self):
        self.assertEqual(self.parse("1-5"), {1, 2, 3, 4, 5})

    def test_list(self):
        self.assertEqual(self.parse("1,3,5,7"), {1, 3, 5, 7})

    def test_mixed(self):
        self.assertEqual(self.parse("1-3,7,10-12"), {1, 2, 3, 7, 10, 11, 12})

    def test_whitespace_tolerated(self):
        self.assertEqual(self.parse(" 1 - 3 , 7 "), {1, 2, 3, 7})

    def test_empty_returns_empty(self):
        self.assertEqual(self.parse(""), set())

    def test_non_numeric_returns_none(self):
        self.assertIsNone(self.parse("abc"))

    def test_out_of_range_returns_none(self):
        self.assertIsNone(self.parse("64"))
        self.assertIsNone(self.parse("-1"))
        self.assertIsNone(self.parse("1-100"))

    def test_inverted_range_returns_none(self):
        self.assertIsNone(self.parse("5-1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
