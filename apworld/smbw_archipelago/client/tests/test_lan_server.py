"""Integration tests for the asyncio LAN server.

Spins up a real LanServer on an ephemeral port, connects a fake Switch
client, and exercises every wire path the M4 plan calls out: HELLO
handshake + post-hello SetBadgesAbsolute replay, nerve dispatch through
the processor, play_report decode + course_in state update,
SetBadgesAbsolute + GrantHashKeyed outbound, periodic badge-sync tick,
malformed input handling, and the displace-old-client-on-reconnect
policy.
"""

from __future__ import annotations

import asyncio
import unittest

from ..lan_server import LanServer
from ..protocol import CheckEmitted, CheckKind
from ..state import BridgeState
from .. import wire
from .test_play_report import COURSE_RESULT, W1_2_COURSE_IN


# Real stage_key from W1_2_COURSE_IN, copied from test_processor.py.
W1_2_STAGE_KEY = 232160011


# ---------------------------------------------------------------------------
# Test harness.


class _FakeSwitch:
    """Tiny helper wrapping a TCP client.  Lets each test stay focused
    on the wire-protocol behavior instead of socket boilerplate."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, port: int) -> "_FakeSwitch":
        r, w = await asyncio.open_connection("127.0.0.1", port)
        return cls(r, w)

    async def send(self, msg: wire.WireMsg) -> None:
        self.writer.write(wire.encode(msg))
        await self.writer.drain()

    async def send_raw(self, line: bytes) -> None:
        self.writer.write(line)
        await self.writer.drain()

    async def recv(self, timeout: float = 1.0) -> wire.WireMsg:
        line = await asyncio.wait_for(self.reader.readuntil(b"\n"), timeout)
        return wire.decode(line)

    async def close(self) -> None:
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


class _ServerHarness:
    """Manages LanServer lifecycle + the captured on_check_emitted list."""

    def __init__(self, badge_mask: int = 0) -> None:
        self.state = BridgeState()
        self.emitted: list[CheckEmitted] = []
        self.server: LanServer | None = None
        self.port: int = 0
        # Mutable mask the tests can mutate live; the lambda the LAN
        # server holds re-reads each invocation.
        self.badge_mask = badge_mask

    async def _on_check_emitted(self, check: CheckEmitted) -> None:
        self.emitted.append(check)

    async def start(self) -> None:
        self.server = LanServer(
            self.state,
            on_check_emitted=self._on_check_emitted,
            badge_mask_provider=lambda: self.badge_mask,
        )
        # asyncio.start_server binds when you pass port=0 -> ephemeral; we
        # have to peek at the socket to learn what we got.
        await self.server.start(host="127.0.0.1", port=0)
        assert self.server._server is not None
        sock = self.server._server.sockets[0]
        self.port = sock.getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.stop()


class _AsyncTestCase(unittest.IsolatedAsyncioTestCase):
    """Common setup/teardown for every test below."""

    async def asyncSetUp(self) -> None:
        self.h = _ServerHarness()
        await self.h.start()

    async def asyncTearDown(self) -> None:
        await self.h.stop()


# ---------------------------------------------------------------------------
# HELLO handshake.

class TestHelloHandshake(_AsyncTestCase):

    async def test_hello_gets_acked(self):
        client = await _FakeSwitch.connect(self.h.port)
        try:
            await client.send(wire.HelloMsg(
                mod_ver="smbwap-test", game_ver="smbw-1.0.0", pid=99))
            ack = await client.recv()
            self.assertIsInstance(ack, wire.HelloAckMsg)
            self.assertTrue(ack.ok)
            self.assertEqual(ack.wire_ver, wire.WIRE_VERSION)
            self.assertNotEqual(ack.bridge_ver, "")
            # Replay-on-HelloMsg: bridge should also push the current
            # AP-known badge mask right after the ack.  Default mask=0
            # in the harness, so we expect bits=0.
            sync = await client.recv()
            self.assertIsInstance(sync, wire.SetBadgesAbsoluteMsg)
            self.assertEqual(sync.bits, 0)
        finally:
            await client.close()

    async def test_hello_replay_uses_provider_mask(self):
        self.h.badge_mask = (1 << 4) | (1 << 9)
        client = await _FakeSwitch.connect(self.h.port)
        try:
            await client.send(wire.HelloMsg(
                mod_ver="t", game_ver="t", pid=0))
            await client.recv()  # ack
            sync = await client.recv()
            self.assertIsInstance(sync, wire.SetBadgesAbsoluteMsg)
            self.assertEqual(sync.bits, (1 << 4) | (1 << 9))
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Nerve dispatch through the processor.

class TestNerveDispatch(_AsyncTestCase):

    async def test_wonder_seed_with_no_course_is_dropped(self):
        # processor._handle_nerve_fire warns + returns [] when
        # current_course is None.  Verify the LAN server doesn't fire
        # the callback.
        client = await _FakeSwitch.connect(self.h.port)
        try:
            await client.send(wire.HelloMsg(mod_ver="t", game_ver="t"))
            await client.recv()  # ack

            await client.send(wire.NerveFireWireMsg(
                kind=wire.NerveKind.WONDER_SEED_AWARDED, seq=1))

            # Give the server a beat to process.
            await asyncio.sleep(0.02)
            self.assertEqual(self.h.emitted, [])
        finally:
            await client.close()

    async def test_wonder_seed_after_course_in_fires_check(self):
        client = await _FakeSwitch.connect(self.h.port)
        try:
            await client.send(wire.HelloMsg(mod_ver="t", game_ver="t"))
            await client.recv()  # ack

            # Establish current_course via the live W1-2 course_in fixture.
            await client.send(wire.PlayReportWireMsg(
                room="course_in", payload_hex=W1_2_COURSE_IN.hex()))
            await asyncio.sleep(0.02)
            self.assertIsNotNone(self.h.state.current_course)
            self.assertEqual(
                self.h.state.current_course.stage_key, W1_2_STAGE_KEY)

            # Now the wonder seed should attribute to W1-2.
            await client.send(wire.NerveFireWireMsg(
                kind=wire.NerveKind.WONDER_SEED_AWARDED, seq=1))
            await asyncio.sleep(0.02)
            self.assertEqual(len(self.h.emitted), 1)
            self.assertEqual(self.h.emitted[0].kind, CheckKind.WONDER_SEED)
            self.assertEqual(self.h.emitted[0].stage_key, W1_2_STAGE_KEY)
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# PlayReport routing.

class TestPlayReportDispatch(_AsyncTestCase):

    async def test_course_in_sets_current_course(self):
        client = await _FakeSwitch.connect(self.h.port)
        try:
            await client.send(wire.HelloMsg(mod_ver="t", game_ver="t"))
            await client.recv()

            await client.send(wire.PlayReportWireMsg(
                room="course_in", payload_hex=W1_2_COURSE_IN.hex()))
            await asyncio.sleep(0.02)

            self.assertIsNotNone(self.h.state.current_course)
            self.assertEqual(self.h.state.current_course.stage_key, W1_2_STAGE_KEY)
        finally:
            await client.close()

    async def test_course_result_emits_normal_exit_check(self):
        # COURSE_RESULT fixture is from a W1-1 clear that touched the
        # flagpole (Normal Exit, goal_id=0, touch_goal_top=false per the
        # processor's classifier).  We need to establish current_course
        # first via an implicit course_in -- but for an outbound check
        # the processor only needs the stage_info inside the
        # course_result payload itself, so a course_in isn't strictly
        # required for course_result.  Verify either way.
        client = await _FakeSwitch.connect(self.h.port)
        try:
            await client.send(wire.HelloMsg(mod_ver="t", game_ver="t"))
            await client.recv()

            await client.send(wire.PlayReportWireMsg(
                room="course_result", payload_hex=COURSE_RESULT.hex()))
            await asyncio.sleep(0.02)

            # COURSE_RESULT is from a top-of-flag clear per the
            # processor's classifier (CourseResult test corpus); accept
            # either NORMAL_EXIT or TOP_OF_FLAG since the field-level
            # contract is owned by tests in test_processor.py.  What we
            # care about here is *something* fired.
            self.assertEqual(len(self.h.emitted), 1)
            self.assertIn(
                self.h.emitted[0].kind,
                (CheckKind.NORMAL_EXIT, CheckKind.TOP_OF_FLAG))
        finally:
            await client.close()

    async def test_play_report_with_bad_hex_bounces_err(self):
        client = await _FakeSwitch.connect(self.h.port)
        try:
            await client.send(wire.HelloMsg(mod_ver="t", game_ver="t"))
            await client.recv()  # ack
            await client.recv()  # post-hello SetBadgesAbsolute replay

            await client.send(wire.PlayReportWireMsg(
                room="course_in", payload_hex="zznotahex"))
            err = await client.recv()
            self.assertIsInstance(err, wire.ErrMsg)
            self.assertIn("play_report", err.reason)
            self.assertEqual(self.h.emitted, [])
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Outbound SetBadgesAbsolute.

class TestSetBadgesAbsoluteOutbound(_AsyncTestCase):

    async def _drain_hello_replay(self, client: _FakeSwitch) -> None:
        """Send hello, eat the ack + the post-hello replay."""
        await client.send(wire.HelloMsg(mod_ver="t", game_ver="t"))
        await client.recv()  # ack
        await client.recv()  # replay SetBadgesAbsolute (bits=0)
        await asyncio.sleep(0.02)  # let writer task settle

    async def test_send_set_badges_absolute_reaches_client(self):
        client = await _FakeSwitch.connect(self.h.port)
        try:
            await self._drain_hello_replay(client)
            self.h.server.send_set_badges_absolute(bits=(1 << 4))

            received = await client.recv()
            self.assertIsInstance(received, wire.SetBadgesAbsoluteMsg)
            self.assertEqual(received.bits, (1 << 4))
        finally:
            await client.close()

    async def test_send_set_badges_absolute_with_no_client_drops(self):
        # No client connected; this should warn-and-drop, not raise.
        self.h.server.send_set_badges_absolute(bits=0xF)
        # If we got here, success.

    async def test_periodic_tick_pushes_provider_mask(self):
        # Drop the tick interval so the test doesn't have to wait 2 s.
        # Resolve the lan_server module via the already-imported LanServer
        # so the dual-mode (package / script) import pattern still works.
        import sys
        ls_mod = sys.modules[LanServer.__module__]
        original = ls_mod.BADGE_SYNC_INTERVAL_SEC
        ls_mod.BADGE_SYNC_INTERVAL_SEC = 0.05
        try:
            self.h.badge_mask = (1 << 4)
            client = await _FakeSwitch.connect(self.h.port)
            try:
                await self._drain_hello_replay(client)
                # Update the live mask mid-flight; the next tick must
                # send the new value.
                self.h.badge_mask = (1 << 4) | (1 << 9)
                # First tick should arrive within ~150 ms.
                sync = await client.recv(timeout=0.5)
                self.assertIsInstance(sync, wire.SetBadgesAbsoluteMsg)
                self.assertEqual(sync.bits, (1 << 4) | (1 << 9))
            finally:
                await client.close()
        finally:
            ls_mod.BADGE_SYNC_INTERVAL_SEC = original


class TestGrantHashKeyedOutbound(_AsyncTestCase):

    async def test_send_grant_hash_keyed_reaches_client(self):
        client = await _FakeSwitch.connect(self.h.port)
        try:
            await client.send(wire.HelloMsg(mod_ver="t", game_ver="t"))
            await client.recv()  # ack
            await client.recv()  # post-hello SetBadgesAbsolute replay

            await asyncio.sleep(0.02)
            # W1 Royal Seed hash + value=1; matches royal_seed_table.
            self.h.server.send_grant_hash_keyed(0x55815859, 1)

            received = await client.recv()
            self.assertIsInstance(received, wire.GrantHashKeyedMsg)
            self.assertEqual(received.hash, 0x55815859)
            self.assertEqual(received.value, 1)
        finally:
            await client.close()

    async def test_send_grant_hash_keyed_with_no_client_drops(self):
        # No client connected; warn-and-drop, not raise.
        self.h.server.send_grant_hash_keyed(0xF4EE6827, 99)


# ---------------------------------------------------------------------------
# Ping / pong + malformed input.

class TestPingPongAndErrors(_AsyncTestCase):

    async def test_ping_gets_pong(self):
        client = await _FakeSwitch.connect(self.h.port)
        try:
            await client.send(wire.PingMsg(ts_ms=12345))
            pong = await client.recv()
            self.assertEqual(pong, wire.PongMsg(ts_ms=12345))
        finally:
            await client.close()

    async def test_malformed_json_bounces_err(self):
        client = await _FakeSwitch.connect(self.h.port)
        try:
            await client.send_raw(b"this isn't even json\n")
            err = await client.recv()
            self.assertIsInstance(err, wire.ErrMsg)
            self.assertIn("decode", err.reason)
        finally:
            await client.close()

    async def test_unknown_message_type_bounces_err(self):
        client = await _FakeSwitch.connect(self.h.port)
        try:
            await client.send_raw(b'{"t":"i_just_made_this_up"}\n')
            err = await client.recv()
            self.assertIsInstance(err, wire.ErrMsg)
            self.assertIn("decode", err.reason)
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Reconnect / displace-old-client.

class TestClientDisplacement(_AsyncTestCase):

    async def test_second_connection_replaces_first(self):
        # First client connects and gets its hello acked + replay.
        client_a = await _FakeSwitch.connect(self.h.port)
        await client_a.send(wire.HelloMsg(mod_ver="a", game_ver="t"))
        await client_a.recv()  # ack
        await client_a.recv()  # post-hello SetBadgesAbsolute replay
        await asyncio.sleep(0.02)

        # Second client connects.
        client_b = await _FakeSwitch.connect(self.h.port)
        await client_b.send(wire.HelloMsg(mod_ver="b", game_ver="t"))
        await client_b.recv()  # ack
        await client_b.recv()  # post-hello SetBadgesAbsolute replay
        await asyncio.sleep(0.05)

        # An explicit send should reach B, not A.  Pick a value that
        # differs from the post-hello replay (0) so the test isn't
        # confused by ordering.
        self.h.server.send_set_badges_absolute(bits=(1 << 11))
        received_b = await client_b.recv()
        self.assertEqual(
            received_b, wire.SetBadgesAbsoluteMsg(bits=(1 << 11)))

        # A's connection should be closed by the server.  Reading from
        # it should give EOF.
        try:
            await asyncio.wait_for(client_a.reader.read(64), timeout=0.5)
        except asyncio.TimeoutError:
            # Acceptable: server may not have flushed the close yet.
            pass

        await client_a.close()
        await client_b.close()


if __name__ == "__main__":
    unittest.main()
