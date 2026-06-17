"""Tests for net_util helpers — IPv4 validation + the LAN subnet seed
that the setup wizard defaults BRIDGE_HOST_STRING to."""

from __future__ import annotations

import unittest

from .. import net_util


class IsPlausibleIPv4Tests(unittest.TestCase):
    def test_accepts_dotted_quads(self) -> None:
        for s in ("10.0.0.5", "192.168.1.1", "0.0.0.0", "255.255.255.255"):
            self.assertTrue(net_util.is_plausible_ipv4(s), s)

    def test_rejects_garbage(self) -> None:
        for s in ("", "auto", "10.0.0", "10.0.0.5.6", "300.1.1.1",
                  "10.0.0.x", "10.0.0.-1", "::1"):
            self.assertFalse(net_util.is_plausible_ipv4(s), s)


class LanSubnetSeedTests(unittest.TestCase):
    def test_returns_lan_ip_when_routable(self) -> None:
        orig = net_util.detect_lan_ip
        net_util.detect_lan_ip = lambda: "192.168.7.42"  # type: ignore[assignment]
        try:
            self.assertEqual(net_util.lan_subnet_seed(), "192.168.7.42")
        finally:
            net_util.detect_lan_ip = orig  # type: ignore[assignment]

    def test_returns_empty_when_only_loopback(self) -> None:
        orig = net_util.detect_lan_ip
        net_util.detect_lan_ip = lambda: "127.0.0.1"  # type: ignore[assignment]
        try:
            # Falls back to "" so callers use the compiled-in default rather
            # than seeding a useless 127.0.0.0/24 sweep.
            self.assertEqual(net_util.lan_subnet_seed(), "")
        finally:
            net_util.detect_lan_ip = orig  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
