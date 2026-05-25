"""Tests for the UDP discovery responder.

Spins up a real responder on an ephemeral port, sends synthetic probes,
and checks the reply shape.  Uses a fake ``get_lan_ip`` so tests don't
depend on the host's real network config.
"""

from __future__ import annotations

import asyncio
import json
import socket
import unittest

if __package__ is None or __package__ == "":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from discovery import DiscoveryResponder
else:
    from .discovery import DiscoveryResponder


def _ephemeral_udp_port() -> int:
    """Return a free UDP port by binding 0 + reading it back, then
    closing.  Race-y in theory; fine in practice for tests."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _probe_and_recv(
    target_port: int,
    probe: bytes,
    timeout: float = 1.0,
) -> bytes:
    """Send a UDP probe to 127.0.0.1:target_port from a fresh socket and
    return the reply bytes (or raise asyncio.TimeoutError)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    try:
        s.bind(("127.0.0.1", 0))
        loop = asyncio.get_running_loop()
        await loop.sock_sendto(s, probe, ("127.0.0.1", target_port))
        data = await asyncio.wait_for(loop.sock_recv(s, 4096), timeout)
        return data
    finally:
        s.close()


class TestDiscoveryReplies(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        port = _ephemeral_udp_port()
        self.tcp_port = 17777  # what the responder advertises
        self.responder = DiscoveryResponder(
            tcp_port=self.tcp_port,
            bind_host="127.0.0.1",
            port=port,
            get_lan_ip=lambda: "10.0.0.42",
        )
        ok = await self.responder.start()
        self.assertTrue(ok)
        self.disc_port = port

    async def asyncTearDown(self) -> None:
        self.responder.stop()

    async def test_valid_probe_gets_reply(self):
        probe = b'{"t":"discover","mod_ver":"smbwap-test"}\n'
        reply = await _probe_and_recv(self.disc_port, probe)
        msg = json.loads(reply.decode("utf-8").rstrip("\n"))
        self.assertEqual(msg["t"], "bridge")
        self.assertEqual(msg["host"], "10.0.0.42")
        self.assertEqual(msg["port"], self.tcp_port)

    async def test_wrong_t_no_reply(self):
        probe = b'{"t":"hello"}\n'
        with self.assertRaises(asyncio.TimeoutError):
            await _probe_and_recv(self.disc_port, probe, timeout=0.3)

    async def test_malformed_json_no_reply(self):
        probe = b'not json at all\n'
        with self.assertRaises(asyncio.TimeoutError):
            await _probe_and_recv(self.disc_port, probe, timeout=0.3)

    async def test_oversized_probe_no_reply(self):
        probe = b'{"t":"discover","mod_ver":"' + b"x" * 1000 + b'"}\n'
        with self.assertRaises(asyncio.TimeoutError):
            await _probe_and_recv(self.disc_port, probe, timeout=0.3)


if __name__ == "__main__":
    unittest.main()
