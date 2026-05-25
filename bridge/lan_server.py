"""Asyncio TCP server that bridges the Switch subsdk to the AP client.

Listens on :17777 by default.  Accepts ONE active Switch connection at a
time -- M4 simplification.  Any second connection that arrives while a
client is held replaces the old one (smo's "kick old, accept new" pattern,
which makes Switch reboots / Ryujinx restarts transparent without
manual intervention).

Per connection, the server:

  1. Waits for a HELLO, replies with HELLO_ACK.
  2. Reads framed JSON lines, dispatches by ``"t"``:
       nerve        -> processor.process_event(state, NerveFireMsg)
                        -> forward each CheckEmitted to ``on_check_emitted``
       play_report  -> processor.process_event(state, PlayReportMsg)
                        -> forward each CheckEmitted
       ping         -> reply pong (M4 keepalive)
       hello        -> bounce ErrMsg (already shook hands)
  3. Spins a writer coroutine that drains ``self._send_queue`` and writes
     each :class:`GrantBadgeMsg` to the socket.  ``send_grant_badge`` is
     the only public producer.

The server does NOT own the AP client; it takes a callback for emitted
checks.  Wiring happens in :mod:`bridge.__main__`.

Modeled on smo_archipelago/apworld/smo_archipelago/client/switch_server.py
(~1400 LOC) but trimmed to the M4 surface: no snapshot replay, no scout
cache, no per-kingdom reconciliation, no GUI hooks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from . import wire
from .processor import process_event
from .protocol import CheckEmitted, NerveFireMsg, PlayReportMsg
from .state import BridgeState


log = logging.getLogger(__name__)


BRIDGE_VERSION = "bridge-m4-dev"
"""Reported back to the Switch in HELLO_ACK.  Bump for observable
behavior changes that a Switch-side log reader might care about."""


# Type alias for the per-check callback the LAN server invokes when the
# processor produces a CheckEmitted.  Async to leave room for AP send
# coroutines without forcing the caller to schedule extra tasks.
CheckEmittedHandler = Callable[[CheckEmitted], Awaitable[None]]


class LanServer:
    """One-Switch-at-a-time async TCP server.

    Lifecycle:

      lan = LanServer(state, on_check_emitted=ctx._handle_check_emitted)
      await lan.start(host="0.0.0.0", port=17777)
      ...
      lan.send_grant_badge(internal_id=4)   # enqueue, returns immediately
      ...
      await lan.stop()
    """

    def __init__(
        self,
        state: BridgeState,
        on_check_emitted: CheckEmittedHandler | None = None,
    ) -> None:
        self._state = state
        self._on_check_emitted = on_check_emitted

        self._server: asyncio.base_events.Server | None = None

        # The currently-active Switch session (writer + send queue +
        # writer task).  ``None`` means no client connected.  Replaced
        # wholesale on each new connection -- a HELLO from a new TCP
        # session displaces the previous holder.
        self._client_writer: asyncio.StreamWriter | None = None
        self._send_queue: asyncio.Queue[wire.GrantBadgeMsg] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._client_lock = asyncio.Lock()

    # ---- Lifecycle ----------------------------------------------------

    async def start(self, host: str = "0.0.0.0", port: int = 17777) -> None:
        """Bind the listening socket and start accepting connections."""
        self._server = await asyncio.start_server(self._handle_client, host, port)
        sockets = self._server.sockets or ()
        bound = ", ".join(str(s.getsockname()) for s in sockets) or "(no sockets)"
        log.info("listening on %s", bound)

    async def stop(self) -> None:
        """Stop accepting and tear down the active session."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self._drop_active_client()

    # ---- Public outbound API ------------------------------------------

    def send_grant_badge(self, internal_id: int) -> None:
        """Enqueue a GrantBadge to the active Switch client.

        Silently drops if no client is connected -- this matches the AP
        client's "fire and forget" pattern for received items.  M5 can
        add a per-client backlog if we need stronger guarantees, but for
        M4 the Switch reboot path replays badges from their save bit
        (the M3.2 primitive writes to the live gmd container which
        persists across save+reload)."""
        msg = wire.GrantBadgeMsg(internal_id=internal_id)
        if self._send_queue is None:
            log.warning(
                "send_grant_badge(%d): no Switch client connected; dropping",
                internal_id)
            return
        try:
            self._send_queue.put_nowait(msg)
            log.debug("send_grant_badge: enqueued internal_id=%d", internal_id)
        except asyncio.QueueFull:
            log.error(
                "send_grant_badge(%d): outbound queue full; dropping",
                internal_id)

    # ---- Per-connection handler ---------------------------------------

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        log.info("switch connected from %s", peer)

        # Displace any prior client.  M4 single-active-client policy.
        await self._install_client(writer)

        try:
            while True:
                try:
                    raw_line = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError:
                    log.info("switch %s closed (eof)", peer)
                    break
                except asyncio.LimitOverrunError:
                    # readuntil gave up; drain and complain.  asyncio's
                    # default StreamReader limit is 64 KiB; lines over
                    # MAX_LINE_BYTES will hit our wire.decode cap first.
                    log.error("switch %s line over limit; closing", peer)
                    await self._send(wire.ErrMsg(reason="line over limit"))
                    break

                await self._dispatch_line(raw_line, peer)
        except ConnectionResetError:
            log.info("switch %s reset", peer)
        except Exception:
            log.exception("switch %s handler crashed; closing connection", peer)
        finally:
            # Only tear down if we still own the writer; a newer client
            # may have displaced us, in which case _install_client
            # already cleaned up.
            if self._client_writer is writer:
                await self._drop_active_client()
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            log.info("switch %s disconnected", peer)

    async def _dispatch_line(self, raw_line: bytes, peer: Any) -> None:
        try:
            msg = wire.decode(raw_line)
        except wire.ProtocolError as e:
            log.warning("switch %s: malformed line: %s", peer, e)
            await self._send(wire.ErrMsg(reason=f"decode: {e}"))
            return

        # Dispatch by concrete type -- cleaner than a string switch since
        # decode() already validated the discriminator.
        if isinstance(msg, wire.HelloMsg):
            log.info(
                "switch %s hello: mod_ver=%s game_ver=%s pid=%d",
                peer, msg.mod_ver, msg.game_ver, msg.pid)
            await self._send(wire.HelloAckMsg(
                ok=True, bridge_ver=BRIDGE_VERSION))
            return

        if isinstance(msg, wire.NerveFireWireMsg):
            log.debug("nerve: kind=%s seq=%d", msg.kind.value, msg.seq)
            ev: NerveFireMsg = msg.to_event()
            await self._run_processor(ev)
            return

        if isinstance(msg, wire.PlayReportWireMsg):
            try:
                pr: PlayReportMsg = msg.to_event()
            except wire.ProtocolError as e:
                log.warning("switch %s: play_report decode failed: %s", peer, e)
                await self._send(wire.ErrMsg(reason=f"play_report: {e}"))
                return
            log.debug(
                "play_report: room=%s payload_bytes=%d",
                pr.room, len(pr.payload))
            await self._run_processor(pr)
            return

        if isinstance(msg, wire.PingMsg):
            await self._send(wire.PongMsg(ts_ms=msg.ts_ms))
            return

        if isinstance(msg, wire.PongMsg):
            # We don't currently send pings, but accept the reply for
            # forward compat without complaint.
            log.debug("pong: ts_ms=%d", msg.ts_ms)
            return

        if isinstance(msg, wire.ErrMsg):
            log.warning("switch %s reports err: %s", peer, msg.reason)
            return

        # HelloAck / GrantBadge from the Switch shouldn't happen; the
        # Switch is the client, not the server.
        log.warning(
            "switch %s sent unexpected message type %s; ignoring",
            peer, type(msg).__name__)

    async def _run_processor(self, event: Any) -> None:
        """Hand an event to the synchronous processor and forward each
        emitted CheckEmitted to the AP callback."""
        try:
            emitted = process_event(self._state, event)
        except Exception:
            log.exception("processor crashed on event %r", event)
            return
        if not self._on_check_emitted or not emitted:
            return
        for check in emitted:
            try:
                await self._on_check_emitted(check)
            except Exception:
                log.exception("on_check_emitted handler crashed for %r", check)

    # ---- Active-client management -------------------------------------

    async def _install_client(self, writer: asyncio.StreamWriter) -> None:
        async with self._client_lock:
            if self._client_writer is not None:
                log.info("displacing previous switch client")
                await self._drop_active_client_locked()

            self._client_writer = writer
            self._send_queue = asyncio.Queue()
            self._writer_task = asyncio.create_task(
                self._writer_loop(writer, self._send_queue),
                name="lan-writer",
            )

    async def _drop_active_client(self) -> None:
        async with self._client_lock:
            await self._drop_active_client_locked()

    async def _drop_active_client_locked(self) -> None:
        # Caller holds ``_client_lock``.  Cancels the writer task and
        # closes the writer.  Safe to call repeatedly.
        if self._writer_task is not None:
            self._writer_task.cancel()
            try:
                await self._writer_task
            except (asyncio.CancelledError, Exception):
                pass
            self._writer_task = None

        if self._client_writer is not None:
            try:
                self._client_writer.close()
            except Exception:
                pass
            self._client_writer = None

        self._send_queue = None

    async def _send(self, msg: wire.WireMsg) -> None:
        """Best-effort send to the active client; logs and drops on
        failure.  Connection-level errors are handled in the read loop's
        normal close path."""
        writer = self._client_writer
        if writer is None:
            log.debug("no active client; dropping outbound %s", type(msg).__name__)
            return
        try:
            writer.write(wire.encode(msg))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError) as e:
            log.warning("send failed (%s); peer likely disconnected", e)
        except Exception:
            log.exception("unexpected send error for %s", type(msg).__name__)

    async def _writer_loop(
        self,
        writer: asyncio.StreamWriter,
        queue: asyncio.Queue[wire.GrantBadgeMsg],
    ) -> None:
        """Drain the outbound queue into the socket.  Runs until cancelled
        or the writer fails."""
        try:
            while True:
                msg = await queue.get()
                try:
                    writer.write(wire.encode(msg))
                    await writer.drain()
                    log.info("-> grant_badge internal_id=%d", msg.internal_id)
                except (ConnectionResetError, BrokenPipeError) as e:
                    log.warning(
                        "writer_loop: send failed (%s); dropping grant_badge(%d)",
                        e, msg.internal_id)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("writer_loop crashed")
