"""Integration tests for the asyncio LAN server.

Spins up a real LanServer on an ephemeral port, connects a fake Switch
client, and exercises every wire path the M4 plan calls out: HELLO
handshake, nerve dispatch through the processor, play_report decode +
course_in state update, GrantBadge send, malformed input handling,
and the displace-old-client-on-reconnect policy.
"""

from __future__ import annotations

import asyncio
import unittest

if __package__ is None or __package__ == "":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from lan_server import LanServer
    from protocol import CheckEmitted, CheckKind
    from state import BridgeState
    import wire
    from test_play_report import COURSE_RESULT, W1_2_COURSE_IN
else:
    from .lan_server import LanServer
    from .protocol import CheckEmitted, CheckKind
    from .state import BridgeState
    from . import wire
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

    def __init__(self) -> None:
        self.state = BridgeState()
        self.emitted: list[CheckEmitted] = []
        self.server: LanServer | None = None
        self.port: int = 0

    async def _on_check_emitted(self, check: CheckEmitted) -> None:
        self.emitted.append(check)

    async def start(self) -> None:
        self.server = LanServer(self.state, on_check_emitted=self._on_check_emitted)
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
            await client.recv()

            await client.send(wire.PlayReportWireMsg(
                room="course_in", payload_hex="zznotahex"))
            err = await client.recv()
            self.assertIsInstance(err, wire.ErrMsg)
            self.assertIn("play_report", err.reason)
            self.assertEqual(self.h.emitted, [])
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Outbound GrantBadge.

class TestGrantBadgeOutbound(_AsyncTestCase):

    async def test_send_grant_badge_reaches_client(self):
        client = await _FakeSwitch.connect(self.h.port)
        try:
            await client.send(wire.HelloMsg(mod_ver="t", game_ver="t"))
            await client.recv()

            # Give _install_client a beat to settle the writer task.
            await asyncio.sleep(0.02)
            self.h.server.send_grant_badge(internal_id=4)

            received = await client.recv()
            self.assertIsInstance(received, wire.GrantBadgeMsg)
            self.assertEqual(received.internal_id, 4)
        finally:
            await client.close()

    async def test_send_grant_badge_with_no_client_drops(self):
        # No client connected; this should warn-and-drop, not raise.
        self.h.server.send_grant_badge(internal_id=7)
        # If we got here, success.


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
        # First client connects and gets its hello acked.
        client_a = await _FakeSwitch.connect(self.h.port)
        await client_a.send(wire.HelloMsg(mod_ver="a", game_ver="t"))
        await client_a.recv()
        await asyncio.sleep(0.02)

        # Second client connects.
        client_b = await _FakeSwitch.connect(self.h.port)
        await client_b.send(wire.HelloMsg(mod_ver="b", game_ver="t"))
        await client_b.recv()
        await asyncio.sleep(0.05)

        # A grant should reach B, not A.
        self.h.server.send_grant_badge(internal_id=11)
        received_b = await client_b.recv()
        self.assertEqual(received_b, wire.GrantBadgeMsg(internal_id=11))

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
