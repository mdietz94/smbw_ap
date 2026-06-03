"""Tests for the ``/send_royal_seeds`` client command.

Royal Seeds are vanilla-owned; the bridge no longer auto-syncs them.
``/send_royal_seeds`` is the manual override that writes an absolute
6-bit mask to the Switch via ``lan_server.send_set_royal_seeds_absolute``.

Archipelago is loaded onto sys.path by the repo-root conftest.py; gated
with @skipUnless like the other framework-dependent tests because
``commands`` imports ``CommonClient``.
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
    "Archipelago not importable; skipping /send_royal_seeds command tests.")
class TestSendRoyalSeedsCommand(unittest.TestCase):

    def _make(self):
        from ..commands import SMBWCommandProcessor
        ctx = MagicMock()
        cp = SMBWCommandProcessor(ctx)
        cp.output = MagicMock()
        return cp, ctx

    def test_default_forces_all_six(self):
        cp, ctx = self._make()
        self.assertTrue(cp._cmd_send_royal_seeds())
        ctx.lan_server.send_set_royal_seeds_absolute.assert_called_once_with(
            0x3F)

    def test_explicit_hex_mask(self):
        cp, ctx = self._make()
        cp._cmd_send_royal_seeds("0x05")
        ctx.lan_server.send_set_royal_seeds_absolute.assert_called_once_with(
            0x05)

    def test_explicit_decimal_mask(self):
        cp, ctx = self._make()
        cp._cmd_send_royal_seeds("3")
        ctx.lan_server.send_set_royal_seeds_absolute.assert_called_once_with(3)

    def test_clear_all(self):
        cp, ctx = self._make()
        cp._cmd_send_royal_seeds("0")
        ctx.lan_server.send_set_royal_seeds_absolute.assert_called_once_with(0)

    def test_out_of_range_rejected(self):
        cp, ctx = self._make()
        cp._cmd_send_royal_seeds("0x40")  # bit 6 -> no such seed
        ctx.lan_server.send_set_royal_seeds_absolute.assert_not_called()

    def test_unparseable_rejected(self):
        cp, ctx = self._make()
        cp._cmd_send_royal_seeds("nope")
        ctx.lan_server.send_set_royal_seeds_absolute.assert_not_called()

    def test_no_lan_server_does_not_crash(self):
        cp, ctx = self._make()
        ctx.lan_server = None
        # Must return True (command handled) and not raise.
        self.assertTrue(cp._cmd_send_royal_seeds())


if __name__ == "__main__":
    unittest.main(verbosity=2)
