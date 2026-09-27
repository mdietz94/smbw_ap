"""Tests for ``SMBWContext._recompute_wonder_seed_bits`` -- the packing
behind the ``set_wonder_seeds_absolute`` push (2026-09-21).

The client packs 16 bits per world bucket (W1 = bits 0..15 ... W4 =
bits 48..63 of ``bits_lo``; W5.. in ``bits_hi``).  A player holding
16+ W4 Wonder Seeds therefore sets bit 63 of ``bits_lo``, which pushes
the u64 above INT64_MAX.  Subsdk builds before 2026-09-21 parsed the
halves as int64 and dropped the whole message on every sync tick
(``[conn] decode failed: {"t":"set_wonder_seeds_absolute",
"bits_lo":18446744069414584320,...`` in a player's Switch log).  These
tests pin the exact wire values so a future repack can't silently
change what the Switch has to accept.

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
    "ensure conftest.py is loaded); skipping Wonder-Seed bitfield tests.")
class TestContextWonderSeedBits(unittest.IsolatedAsyncioTestCase):

    # Synthetic AP item ids, one per world bucket (bucket index == order).
    SEED_ITEM_IDS = {
        "W1 Wonder Seed": 300,
        "W2 Wonder Seed": 301,
        "W3 Wonder Seed": 302,
        "W4 Wonder Seed": 303,
        "W5 Wonder Seed": 304,
        "W6 Wonder Seed": 305,
        "Petal Isles Wonder Seed": 306,
        "Special World Wonder Seed": 307,
    }
    OTHER_ITEM_ID = 400  # not a Wonder Seed; must contribute nothing

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

        id_to_name = {v: k for k, v in self.SEED_ITEM_IDS.items()}
        id_to_name[self.OTHER_ITEM_ID] = "10 Coin"
        self.ctx.item_names = MagicMock()
        self.ctx.item_names.lookup_in_game = (
            lambda i: id_to_name.get(i, f"?{i}"))

    async def asyncTearDown(self) -> None:  # type: ignore[override]
        try:
            await self.ctx.shutdown()
        except Exception:
            pass

    def _give(self, counts: dict[str, int]) -> None:
        items = []
        for name, n in counts.items():
            items.extend({"item": self.SEED_ITEM_IDS[name]} for _ in range(n))
        # items_received is walked via getattr(it, "item") with a dict
        # fallback; feed dicts directly like the other context tests.
        self.ctx.items_received = items

    def test_no_seeds_is_zero_zero(self):
        self._give({})
        self.assertEqual(self.ctx._recompute_wonder_seed_bits(), (0, 0))

    def test_single_w1_seed_is_bit0(self):
        self._give({"W1 Wonder Seed": 1})
        self.assertEqual(self.ctx._recompute_wonder_seed_bits(), (1, 0))

    def test_full_w4_bucket_sets_bit63(self):
        # 16 W4 seeds -> bits 48..63 of bits_lo.  Bit 63 set means the
        # value exceeds INT64_MAX; the Switch must read it as u64.
        self._give({"W4 Wonder Seed": 16})
        lo, hi = self.ctx._recompute_wonder_seed_bits()
        self.assertEqual(lo, 0xFFFF000000000000)
        self.assertEqual(hi, 0)
        self.assertTrue(lo & (1 << 63))
        self.assertGreater(lo, (1 << 63) - 1)

    def test_w3_and_w4_full_matches_player_log_value(self):
        # The exact value from the 2026-09-21 Eden log:
        # bits_lo = 18446744069414584320 = 0xFFFFFFFF00000000.
        self._give({"W3 Wonder Seed": 16, "W4 Wonder Seed": 16})
        lo, hi = self.ctx._recompute_wonder_seed_bits()
        self.assertEqual(lo, 18446744069414584320)
        self.assertEqual(lo, 0xFFFFFFFF00000000)
        self.assertEqual(hi, 0)

    def test_w4_bucket_caps_at_16(self):
        # Overflowing the 16-bit bucket must NOT bleed into bits_hi or
        # wrap: 40 W4 seeds still yields the same full-bucket mask.
        self._give({"W4 Wonder Seed": 40})
        self.assertEqual(
            self.ctx._recompute_wonder_seed_bits(), (0xFFFF000000000000, 0))

    def test_all_eight_buckets_full(self):
        self._give({name: 16 for name in self.SEED_ITEM_IDS})
        self.assertEqual(
            self.ctx._recompute_wonder_seed_bits(),
            ((1 << 64) - 1, (1 << 64) - 1))

    def test_w5_lands_in_bits_hi(self):
        self._give({"W5 Wonder Seed": 3})
        self.assertEqual(self.ctx._recompute_wonder_seed_bits(), (0, 0b111))

    def test_non_seed_items_ignored(self):
        self.ctx.items_received = [
            {"item": self.OTHER_ITEM_ID},
            {"item": self.SEED_ITEM_IDS["W2 Wonder Seed"]},
            {"item": 9999},
        ]
        self.assertEqual(
            self.ctx._recompute_wonder_seed_bits(), (1 << 16, 0))

    def test_full_w4_bucket_encodes_on_the_wire(self):
        # End-to-end through the codec: the value the Switch must parse.
        from ..wire import SetWonderSeedsAbsoluteMsg, decode, encode
        self._give({"W3 Wonder Seed": 16, "W4 Wonder Seed": 16})
        lo, hi = self.ctx._recompute_wonder_seed_bits()
        line = encode(SetWonderSeedsAbsoluteMsg(bits_lo=lo, bits_hi=hi))
        self.assertIn(b'"bits_lo":18446744069414584320', line)
        self.assertEqual(decode(line).bits_lo, 0xFFFFFFFF00000000)


if __name__ == "__main__":
    unittest.main()
