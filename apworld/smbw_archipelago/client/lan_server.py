"""Asyncio TCP server that bridges the Switch subsdk to the AP client.

Listens on :17777 by default.  Accepts ONE active Switch connection at a
time -- M4 simplification.  Any second connection that arrives while a
client is held replaces the old one (smo's "kick old, accept new" pattern,
which makes Switch reboots / Ryujinx restarts transparent without
manual intervention).

Per connection, the server:

  1. Waits for a HELLO, replies with HELLO_ACK, then immediately pushes
     a SetBadgesAbsolute with the current AP-known mask AND one
     GrantHashKeyed per currently-known Royal Seed (replay-on-reconnect;
     this is M4.5 -- container-B bool grants survive Switch reconnect
     and save/reload because the bridge replays every seed every time
     the Switch handshakes).
  2. Reads framed JSON lines, dispatches by ``"t"``:
       nerve        -> processor.process_event(state, NerveFireMsg)
                        -> forward each CheckEmitted to ``on_check_emitted``
       play_report  -> processor.process_event(state, PlayReportMsg)
                        -> forward each CheckEmitted
       ping         -> reply pong (M4 keepalive)
       hello        -> bounce ErrMsg (already shook hands)
  3. Spins a writer coroutine that drains ``self._send_queue`` and writes
     each grant message (:class:`SetBadgesAbsoluteMsg` /
     :class:`GrantHashKeyedMsg`) to the socket.
     ``send_set_badges_absolute`` and ``send_grant_hash_keyed`` are the
     two public producers.
  4. Runs a periodic ~2 s tick task while a client is connected; pulls
     the current badge mask from ``badge_mask_provider`` and pushes a
     SetBadgesAbsolute.  This is what reverts in-game badge pickups
     (Poplin shop, badge house) to AP's view within seconds -- AP is
     the sole authority over the badge pool.

The server does NOT own the AP client; it takes a callback for emitted
checks and an optional provider for the current AP-known badge mask.
Wiring happens in :mod:`bridge.__main__`.

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
from .protocol import (
    BadgeAcquiredMsg,
    CheckEmitted,
    DeathReported,
    GoalCompleted,
    NerveFireMsg,
    PlayReportMsg,
)
from .state import BridgeState


log = logging.getLogger("SMBW")


BRIDGE_VERSION = "bridge-m4-dev"
"""Reported back to the Switch in HELLO_ACK.  Bump for observable
behavior changes that a Switch-side log reader might care about."""


BADGE_SYNC_INTERVAL_SEC = 2.0
"""How often the badge-sync tick fires while a Switch client is
connected.  Each tick re-sends a SetBadgesAbsolute with whatever the
``badge_mask_provider`` currently returns -- idempotent, so coalesces
with the per-ReceivedItems and per-HelloMsg sends without debounce
work.  Tradeoff: tighter intervals shrink the in-game-pickup visibility
window at the cost of LAN socket traffic.  At 2 s, an in-game badge
purchase is visible for up to ~2 s before being reverted; the LAN line
itself is ~60 bytes JSON, trivially cheap."""


# Type alias for the per-check callback the LAN server invokes when the
# processor produces a CheckEmitted.  Async to leave room for AP send
# coroutines without forcing the caller to schedule extra tasks.
CheckEmittedHandler = Callable[[CheckEmitted], Awaitable[None]]

# M3.8 DeathLink -- analog of CheckEmittedHandler for DeathReported.
# Wired to ``SMBWContext.handle_death_reported`` in :mod:`bridge.__main__`.
DeathReportedHandler = Callable[[DeathReported], Awaitable[None]]

# M3.7 game-completion -- analog for GoalCompleted.  Wired to
# ``SMBWContext.handle_goal_completed`` in the bridge entry point.
GoalCompletedHandler = Callable[[GoalCompleted], Awaitable[None]]

# Synchronous callable that returns the current AP-known badge bitmask.
# Set at construction time; the LAN server calls it on every Switch
# HelloMsg and on every periodic tick.  Returning 0 is fine and means
# "AP has no badges -- clobber the Switch to empty".
BadgeMaskProvider = Callable[[], int]


# Synchronous callable returning all currently-known container-B bool
# grants to replay on HelloMsg.  Each tuple is (hash, value).  Empty
# list is fine and means "no Royal Seeds received yet".  No periodic
# tick: Royal Seeds have no in-game acquisition path that bypasses AP
# (palace clears flow through AP first), so HelloMsg replay alone
# closes the save-survival gap.
RoyalSeedGrantsProvider = Callable[[], list[tuple[int, int]]]


# Synchronous callable returning the cumulative per-world Wonder Seed
# counts as a list of length ``wonder_seed_table.WORLD_COUNT`` (8).
# Used for the gate-override sync analog of ``BadgeMaskProvider``:
# every HelloMsg / 2 s tick / ReceivedItems event overwrites the
# Switch's "current-world Wonder Seed count" mirror hashes to the AP
# value for whichever world the player is viewing (Switch picks the
# bucket via container-A hash ``0x9f5ead3c``).  Returning all zeros
# is fine and means "AP knows of no Wonder Seeds yet -- clobber every
# world's count to 0".  Same idempotent-absolute-write pattern as
# badges: AP is the sole authority over the Wonder Seed gate, any
# in-game pickup (Wonder phase, flag-pole goal seed, 10-coin reward)
# is reverted within ~2 s.  See [wonder_seed_table.py](wonder_seed_table.py)
# for the bucket convention and [wire.py](wire.py)
# ``SetWonderSeedCountsMsg`` for the Switch-side dispatch.
WonderSeedCountsProvider = Callable[[], list[int]]


class _DrainIncrementsSentinel:
    """Internal sentinel posted by :meth:`LanServer.send_increment_hash_keyed`
    to wake the writer loop.  When the writer pops one of these, it
    drains :attr:`LanServer._pending_increments` (the per-hash coalesce
    buffer) and emits one :class:`wire.IncrementHashKeyedMsg` per
    non-zero accumulated delta.

    Why coalesce: the Switch-side primitive
    ``probe::incrementContainerACounter`` is RMW against the *persistent*
    container value -- the writer queues to a dirty buffer at
    ``gmd+0xf8`` that the reader does not observe before the next save
    flush.  If the bridge sends N rapid increments back-to-back, each
    RMW reads the same persistent ``cur`` and writes ``cur + delta``,
    with each new write clobbering the previous dirty-buffer entry --
    only the last delta survives.  Folding N pending deltas into a
    single outbound ``+sum(deltas)`` makes the Switch perform exactly
    one RMW, so the entire batch lands.

    Duplicate sentinels are harmless: the second sentinel from a given
    burst finds the dict already empty and is a no-op."""
    __slots__ = ()


_DRAIN_INCREMENTS: _DrainIncrementsSentinel = _DrainIncrementsSentinel()


# Union of bridge-to-Switch message types the writer loop can drain.
# Adding a new inbound variant (e.g. ``GrantPowerUpMsg`` in M5) means:
# define it in ``wire.py``, add a typed ``send_*`` method below, append
# to this alias, and add a log-line branch in ``_writer_loop``.
# ``_DrainIncrementsSentinel`` is an internal marker -- see its
# docstring; the writer translates it into one outbound
# IncrementHashKeyedMsg per pending hash.
GrantMsg = (
    wire.SetBadgesAbsoluteMsg
    | wire.GrantHashKeyedMsg
    | wire.IncrementHashKeyedMsg
    | wire.SetWonderSeedCountsMsg
    | wire.KillMsg
    | _DrainIncrementsSentinel
)


class LanServer:
    """One-Switch-at-a-time async TCP server.

    Lifecycle:

      lan = LanServer(state,
                      on_check_emitted=ctx.handle_check_emitted,
                      badge_mask_provider=ctx._recompute_badge_mask)
      await lan.start(host="0.0.0.0", port=17777)
      ...
      lan.send_set_badges_absolute(bits=0x10)   # enqueue, returns
      ...
      await lan.stop()
    """

    def __init__(
        self,
        state: BridgeState,
        on_check_emitted: CheckEmittedHandler | None = None,
        on_death_reported: DeathReportedHandler | None = None,
        on_goal_completed: GoalCompletedHandler | None = None,
        badge_mask_provider: BadgeMaskProvider | None = None,
        royal_seed_grants_provider: RoyalSeedGrantsProvider | None = None,
        wonder_seed_counts_provider: WonderSeedCountsProvider | None = None,
    ) -> None:
        self._state = state
        self._on_check_emitted = on_check_emitted
        self._on_death_reported = on_death_reported
        self._on_goal_completed = on_goal_completed
        self._badge_mask_provider = badge_mask_provider
        self._royal_seed_grants_provider = royal_seed_grants_provider
        self._wonder_seed_counts_provider = wonder_seed_counts_provider

        self._server: asyncio.base_events.Server | None = None

        # The currently-active Switch session (writer + send queue +
        # writer task + badge-sync tick task).  ``None`` means no client
        # connected.  Replaced wholesale on each new connection -- a
        # HELLO from a new TCP session displaces the previous holder.
        self._client_writer: asyncio.StreamWriter | None = None
        self._send_queue: asyncio.Queue[GrantMsg] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._badge_sync_task: asyncio.Task[None] | None = None
        self._client_lock = asyncio.Lock()

        # Per-hash coalesce buffer for ``send_increment_hash_keyed``;
        # see :class:`_DrainIncrementsSentinel` for the why.  Lives on
        # the LanServer (not the per-session record) because the public
        # ``send_*`` methods are called by SMBWContext without knowing
        # whether a session is active right now -- the dict is reset on
        # every session install/drop so deltas never cross sessions.
        self._pending_increments: dict[int, int] = {}

    # ---- Lifecycle ----------------------------------------------------

    async def start(self, host: str = "0.0.0.0", port: int = 17777) -> None:
        """Bind the listening socket and start accepting connections."""
        self._server = await asyncio.start_server(self._handle_client, host, port)
        sockets = self._server.sockets or ()
        bound = ", ".join(str(s.getsockname()) for s in sockets) or "(no sockets)"
        log.info("listening on %s", bound)

    async def stop(self) -> None:
        """Stop accepting and tear down the active session.

        Order is load-bearing: close the active client writer FIRST so
        its ``_handle_client`` reader unblocks and the per-connection
        task can return, THEN close the listening socket and await
        ``wait_closed``.  On Python 3.12+ ``Server.wait_closed()`` waits
        for every active client handler to finish, and our
        ``_handle_client`` is parked in ``reader.readuntil`` -- closing
        the listener alone does NOT kick connected peers, so without
        this teardown a clean window-close hangs forever whenever a
        Switch is connected.  Mirrors smo_archipelago's SwitchServer.stop
        (same Python 3.12 audit, same fix shape).
        """
        # 1) Close + cancel any active client first.  This drops the
        # writer, which causes the reader's readuntil to raise
        # IncompleteReadError and _handle_client's task to exit.
        await self._drop_active_client()
        # 2) Close the listener and bound-wait for it.  The timeout is
        # defensive in case _handle_client somehow doesn't observe the
        # writer close within a reasonable window (slow client teardown
        # over a half-dead LAN).
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            except asyncio.TimeoutError:
                log.warning("stop: server.wait_closed timed out; abandoning")
            except Exception:
                log.exception("stop: server.wait_closed raised; ignoring")
            self._server = None

    # ---- Public outbound API ------------------------------------------

    def send_set_badges_absolute(self, bits: int) -> None:
        """Enqueue a SetBadgesAbsolute to the active Switch client.

        ``bits`` is the absolute desired badge bitmask -- the Switch
        will overwrite its entire container-C owned-badge bitfield to
        match.  AP is the sole authority over the badge pool.

        Silently drops if no client is connected -- this matches the AP
        client's "fire and forget" pattern for received items.  The
        next HelloMsg will trigger a fresh send anyway (via
        :meth:`_push_badge_sync_now`), so any dropped tick is reliably
        recovered on reconnect.
        """
        msg = wire.SetBadgesAbsoluteMsg(bits=bits)
        if self._send_queue is None:
            log.warning(
                "send_set_badges_absolute(bits=0x%x): no Switch client "
                "connected; dropping", bits)
            return
        try:
            self._send_queue.put_nowait(msg)
            log.debug(
                "send_set_badges_absolute: enqueued bits=0x%x", bits)
        except asyncio.QueueFull:
            log.error(
                "send_set_badges_absolute(bits=0x%x): outbound queue "
                "full; dropping", bits)

    def send_kill(self, source: str, cause: str) -> None:
        """Enqueue a Kill (M3.8 DeathLink inbound) to the active Switch.

        Same drop-on-no-client semantics as
        :meth:`send_set_badges_absolute`.  The Switch dispatcher calls
        ``probe::synthKill()`` which writes 0 to the live HP int16 at
        ``live_base + 0x38``; the next tick of the player update
        function reads HP <= 0 and takes the death branch.

        ``source`` and ``cause`` are truncated to KillMsg's caps (48 /
        128) on the wire encoder side, so over-long inputs are silently
        clipped rather than rejected."""
        msg = wire.KillMsg(source=source, cause=cause)
        if self._send_queue is None:
            log.warning(
                "send_kill(source=%r, cause=%r): no Switch client "
                "connected; dropping",
                source, cause)
            return
        try:
            self._send_queue.put_nowait(msg)
            log.debug(
                "send_kill: enqueued source=%r cause=%r", source, cause)
        except asyncio.QueueFull:
            log.error(
                "send_kill(source=%r, cause=%r): outbound queue full; "
                "dropping",
                source, cause)

    def send_set_wonder_seed_counts(self, counts: list[int]) -> None:
        """Enqueue a SetWonderSeedCounts (per-world Wonder Seed gate
        override) to the active Switch client.  ``counts`` must be a
        list of length ``wonder_seed_table.WORLD_COUNT`` (8); see
        ``SetWonderSeedCountsMsg`` for the bucket convention.

        Same drop semantics as :meth:`send_set_badges_absolute`.

        The Switch caches the array on receipt; the in-game tick (~2 s
        cadence via NerveActivateOnce) reads container-A hash
        ``0x9f5ead3c`` to pick the current world's bucket and calls
        ``probe::pushWonderSeedOverride(counts[bucket])`` -- which
        writes that value to the 5 mirror hashes including
        ``0x390eb960`` (the one the gate predicate reads).  Idempotent
        absolute-overwrite: AP is the sole authority over Wonder Seed
        gating."""
        msg = wire.SetWonderSeedCountsMsg(counts=tuple(counts))
        if self._send_queue is None:
            log.warning(
                "send_set_wonder_seed_counts(counts=%s): no Switch "
                "client connected; dropping",
                counts)
            return
        try:
            self._send_queue.put_nowait(msg)
            log.debug(
                "send_set_wonder_seed_counts: enqueued counts=%s",
                counts)
        except asyncio.QueueFull:
            log.error(
                "send_set_wonder_seed_counts(counts=%s): outbound queue "
                "full; dropping",
                counts)

    def send_increment_hash_keyed(self, hash_: int, delta: int) -> None:
        """Enqueue an IncrementHashKeyed (container-A counter RMW) to the
        active Switch client.

        Same drop semantics as :meth:`send_set_badges_absolute`.  Used
        for AP filler items whose semantic is "add N to the running
        total" rather than "set to N" -- first user is the "10 Coin"
        item routing 10 to the ``flower_coin`` (purple coin) counter.

        **Coalesces per hash before sending.**  The Switch-side
        primitive RMWs against the persistent counter, but its write
        queues to a dirty buffer the reader doesn't observe before the
        next save flush -- so multiple ``+10``s in quick succession
        would each read the same persistent ``cur`` and clobber each
        other (only the last delta survives).  Instead we accumulate
        deltas in ``_pending_increments[hash]`` and post a single
        :data:`_DRAIN_INCREMENTS` sentinel; the writer loop drains the
        dict atomically and emits one outbound message per non-zero
        hash.  See :class:`_DrainIncrementsSentinel` for the full
        rationale.

        Save-survival caveat (same as ``send_grant_hash_keyed`` for
        counters): the Switch primitive writes to a deferred-write
        dirty buffer at ``gmd+0xf8`` flushed on next save.  A load-
        before-flush silently drops the increment.  Unlike Royal Seeds,
        we do NOT replay container-A counter increments on HelloMsg --
        replay would double-count.  For filler this is acceptable; for
        progression-critical counters we'd need per-AP-item-index
        dedup persisted across reconnects."""
        if self._send_queue is None:
            log.warning(
                "send_increment_hash_keyed(hash=0x%08x, delta=%d): no "
                "Switch client connected; dropping",
                hash_, delta)
            return
        # Coalesce: a synchronous Python dict update + put_nowait pair
        # is atomic from the event loop's POV (no awaits), so a burst
        # of synchronous send_increment_hash_keyed calls all land in
        # the dict before the writer loop ever sees the first sentinel.
        prev = self._pending_increments.get(hash_, 0)
        new_total = prev + delta
        self._pending_increments[hash_] = new_total
        try:
            self._send_queue.put_nowait(_DRAIN_INCREMENTS)
            log.debug(
                "send_increment_hash_keyed: accumulated hash=0x%08x "
                "delta=%d (pending total %d)",
                hash_, delta, new_total)
        except asyncio.QueueFull:
            # The dict update already landed and an earlier sentinel
            # is presumably still queued (or the queue is saturated by
            # other messages and will be drained imminently).  Either
            # way the next sentinel that the writer pops will pick up
            # our newly-added delta -- no message is lost.
            log.debug(
                "send_increment_hash_keyed(hash=0x%08x, delta=%d): "
                "outbound queue full but sentinel for this hash is "
                "still pending -- delta folded into the existing "
                "pending entry (new total %d)",
                hash_, delta, new_total)

    def send_grant_hash_keyed(self, hash_: int, value: int) -> None:
        """Enqueue a GrantHashKeyed (container-A counter or container-B
        bool write, routed Switch-side by ``isBoolHash`` in
        ``ApFrameBridge.cpp``) to the active Switch client.

        Same drop semantics as :meth:`send_set_badges_absolute`.

        Save-survival: the Switch-side primitive (both container-A
        ``FUN_710049F648`` and container-B ``FUN_710049EA24``) writes
        to a deferred-write dirty buffer at ``gmd+0xf8`` that flushes
        on next save.  If the player loads a fresh save before the
        flush lands, the grant is lost.  The HelloMsg dispatch above
        replays every currently-known Royal Seed via
        :meth:`_push_royal_seeds_now` (M4.5), so seeds survive Switch
        reconnect and save/reload.  Container-A counters
        (flower_coin, regular_coin) are NOT replayed -- not currently
        AP items; revisit if/when they are."""
        msg = wire.GrantHashKeyedMsg(hash=hash_, value=value)
        if self._send_queue is None:
            log.warning(
                "send_grant_hash_keyed(hash=0x%08x, value=%d): no Switch "
                "client connected; dropping",
                hash_, value)
            return
        try:
            self._send_queue.put_nowait(msg)
            log.debug(
                "send_grant_hash_keyed: enqueued hash=0x%08x value=%d",
                hash_, value)
        except asyncio.QueueFull:
            log.error(
                "send_grant_hash_keyed(hash=0x%08x, value=%d): outbound "
                "queue full; dropping",
                hash_, value)

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
            # Replay-on-HelloMsg: push the current AP-known badge mask
            # immediately so the Switch's container-C bitfield matches
            # AP's view from the moment the connection is up.  This is
            # what makes badges survive save/reload and Switch reboots.
            self._push_badge_sync_now()
            # M4.5: same idea for container-B bool grants (Royal Seeds).
            # The container-B writer is deferred-write so a load-before-
            # flush silently drops the grant; replaying on every HelloMsg
            # is the durable fix.
            self._push_royal_seeds_now()
            # Same idempotent absolute-overwrite pattern for the
            # container-A Wonder Seed counter (M3.3 follow-up): the
            # bridge holds the canonical AP-derived count and clobbers
            # the Switch's lifetime counter to match on every handshake.
            self._push_wonder_seeds_now()
            return

        if isinstance(msg, wire.NerveFireWireMsg):
            log.debug("nerve: kind=%s seq=%d", msg.kind.value, msg.seq)
            ev: NerveFireMsg = msg.to_event()
            await self._run_processor(ev)
            return

        if isinstance(msg, wire.BadgeAcquiredWireMsg):
            log.info(
                "badge_acquired: internal_id=%d seq=%d",
                msg.internal_id, msg.seq)
            badge_ev: BadgeAcquiredMsg = msg.to_event()
            await self._run_processor(badge_ev)
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
        emit to its matching async callback.  CheckEmitted routes to
        ``on_check_emitted`` (AP LocationChecks); DeathReported routes
        to ``on_death_reported`` (AP DeathLink Bounce)."""
        try:
            emitted = process_event(self._state, event)
        except Exception:
            log.exception("processor crashed on event %r", event)
            return
        for emit in emitted:
            if isinstance(emit, CheckEmitted):
                if self._on_check_emitted is None:
                    continue
                try:
                    await self._on_check_emitted(emit)
                except Exception:
                    log.exception(
                        "on_check_emitted handler crashed for %r", emit)
            elif isinstance(emit, DeathReported):
                if self._on_death_reported is None:
                    continue
                try:
                    await self._on_death_reported(emit)
                except Exception:
                    log.exception(
                        "on_death_reported handler crashed for %r", emit)
            elif isinstance(emit, GoalCompleted):
                if self._on_goal_completed is None:
                    continue
                try:
                    await self._on_goal_completed(emit)
                except Exception:
                    log.exception(
                        "on_goal_completed handler crashed for %r", emit)
            else:
                log.warning("processor emitted unknown type %r", type(emit).__name__)

    # ---- Active-client management -------------------------------------

    async def _install_client(self, writer: asyncio.StreamWriter) -> None:
        async with self._client_lock:
            if self._client_writer is not None:
                log.info("displacing previous switch client")
                await self._drop_active_client_locked()

            self._client_writer = writer
            self._send_queue = asyncio.Queue()
            # Fresh coalesce buffer per session.  Any deltas pending
            # from a previous session (if a sentinel raced with the
            # client drop) are stale -- the Switch has restarted and
            # would have no context for them.
            self._pending_increments = {}
            self._writer_task = asyncio.create_task(
                self._writer_loop(writer, self._send_queue),
                name="lan-writer",
            )
            self._badge_sync_task = asyncio.create_task(
                self._badge_sync_loop(),
                name="lan-badge-sync",
            )

    async def _drop_active_client(self) -> None:
        async with self._client_lock:
            await self._drop_active_client_locked()

    async def _drop_active_client_locked(self) -> None:
        # Caller holds ``_client_lock``.  Cancels the writer + badge-sync
        # tasks and closes the writer.  Safe to call repeatedly.
        if self._badge_sync_task is not None:
            self._badge_sync_task.cancel()
            try:
                await self._badge_sync_task
            except (asyncio.CancelledError, Exception):
                pass
            self._badge_sync_task = None

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
        # Pending coalesced increment deltas are tied to the session;
        # the Switch is gone, drop them so a future reconnect starts
        # from a clean slate (matches the per-session queue drop above).
        self._pending_increments = {}

    # ---- Badge sync ---------------------------------------------------

    def _push_badge_sync_now(self) -> None:
        """Pull the current AP-known badge mask from the provider and
        enqueue a SetBadgesAbsolute.  No-op if no provider was wired
        (e.g. unit tests that don't care about badges) or no client is
        connected.  Called from HelloMsg dispatch and from the periodic
        tick loop."""
        if self._badge_mask_provider is None:
            return
        try:
            bits = int(self._badge_mask_provider())
        except Exception:
            log.exception("badge_mask_provider raised; skipping sync")
            return
        self.send_set_badges_absolute(bits)

    def _push_royal_seeds_now(self) -> None:
        """Pull all currently-known Royal Seed grants from the provider
        and enqueue one GrantHashKeyed per (hash, value).  No-op if no
        provider was wired or no client is connected.  Called from the
        HelloMsg dispatch -- the durable fix for the container-B
        deferred-write save-survival gap (M4.5).  No periodic tick:
        Royal Seeds can only be earned through AP-routed palace clears,
        so there's nothing in-game to revert."""
        if self._royal_seed_grants_provider is None:
            return
        try:
            grants = self._royal_seed_grants_provider()
        except Exception:
            log.exception("royal_seed_grants_provider raised; skipping replay")
            return
        if not grants:
            return
        log.info("HelloMsg replay: %d Royal Seed grant(s)", len(grants))
        for h, v in grants:
            self.send_grant_hash_keyed(h, v)

    def _push_wonder_seeds_now(self) -> None:
        """Pull the AP-known per-world Wonder Seed counts from the
        provider and enqueue a ``SetWonderSeedCountsMsg``.  No-op if no
        provider was wired or no client is connected.  Same idempotent
        absolute-overwrite pattern as badges: AP holds the canonical
        per-world counts derived from ``items_received``; any in-game
        pickup (Wonder phase grab, flag-pole goal seed, 10-coin reward)
        gets clobbered back to AP's view within ~2 s by the sync loop.
        Called from HelloMsg dispatch, per-ReceivedItems push (via
        :meth:`send_set_wonder_seed_counts` directly in
        ``SMBWContext._handle_received_items``), and the periodic tick.
        See [wonder_seed_table.py](wonder_seed_table.py) for the bucket
        convention; Switch routes to the right bucket via container-A
        hash ``0x9f5ead3c`` (current world index)."""
        if self._wonder_seed_counts_provider is None:
            return
        try:
            counts = list(self._wonder_seed_counts_provider())
        except Exception:
            log.exception(
                "wonder_seed_counts_provider raised; skipping sync")
            return
        self.send_set_wonder_seed_counts(counts)

    async def _badge_sync_loop(self) -> None:
        """Periodic tick: every ``BADGE_SYNC_INTERVAL_SEC``, push the
        current AP-known badge mask AND the current AP-known Wonder
        Seed counter to the Switch.  This is what reverts in-game
        pickups that bypass AP (badge purchases at Poplin shop / badge
        house, Wonder phase seed grabs, flag-pole goal seeds, 10-coin
        rewards) to AP's view within seconds -- AP is the sole
        authority over both surfaces.

        Runs until cancelled by ``_drop_active_client_locked`` (i.e.
        only while a Switch client is connected; no point ticking when
        no one is listening).

        Naming note: this loop now syncs both badges and Wonder Seeds.
        The name is kept for diff continuity with the M3.2/M4 badge
        rollout; if a third idempotent-overwrite surface lands (e.g.
        per-course Wonder Seed flags after M3.3 follow-up), consider
        renaming to ``_idempotent_sync_loop``."""
        try:
            while True:
                await asyncio.sleep(BADGE_SYNC_INTERVAL_SEC)
                self._push_badge_sync_now()
                self._push_wonder_seeds_now()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("badge_sync_loop crashed")

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
        queue: asyncio.Queue[GrantMsg],
    ) -> None:
        """Drain the outbound queue into the socket.  Runs until cancelled
        or the writer fails."""
        try:
            while True:
                msg = await queue.get()
                if isinstance(msg, _DrainIncrementsSentinel):
                    # Atomic extract-and-replace.  Reassigning before
                    # iteration means a concurrent
                    # send_increment_hash_keyed call (which can run
                    # during our ``await writer.drain()`` below)
                    # accumulates into a fresh dict and posts its own
                    # sentinel -- the next iteration picks it up.
                    pending = self._pending_increments
                    self._pending_increments = {}
                    if not pending:
                        # A redundant sentinel from an earlier burst
                        # we already drained.  No-op.
                        continue
                    for h, delta in pending.items():
                        if delta == 0:
                            # Pure cancellation (e.g. +10 grant then
                            # -10 refund in the same coalesce window).
                            # Skip so we don't bother the Switch with
                            # a no-op RMW.
                            log.debug(
                                "writer_loop: skipping zero-delta drain "
                                "for hash=0x%08x", h)
                            continue
                        real = wire.IncrementHashKeyedMsg(
                            hash=h, delta=delta)
                        try:
                            writer.write(wire.encode(real))
                            await writer.drain()
                            log.info(
                                "-> increment_hash_keyed hash=0x%08x "
                                "delta=%d (coalesced)",
                                real.hash, real.delta)
                        except (ConnectionResetError, BrokenPipeError) as e:
                            log.warning(
                                "writer_loop: send failed (%s); dropping "
                                "coalesced IncrementHashKeyedMsg",
                                e)
                            return
                    continue
                try:
                    writer.write(wire.encode(msg))
                    await writer.drain()
                    if isinstance(msg, wire.SetBadgesAbsoluteMsg):
                        # Suppress the empty-mask periodic-tick spam; only
                        # log when there's an actual badge state to push.
                        if msg.bits:
                            log.info("-> set_badges_absolute bits=0x%x", msg.bits)
                    elif isinstance(msg, wire.GrantHashKeyedMsg):
                        log.info(
                            "-> grant_hash_keyed hash=0x%08x value=%d",
                            msg.hash, msg.value)
                    elif isinstance(msg, wire.IncrementHashKeyedMsg):
                        log.info(
                            "-> increment_hash_keyed hash=0x%08x delta=%d",
                            msg.hash, msg.delta)
                    elif isinstance(msg, wire.SetWonderSeedCountsMsg):
                        log.info(
                            "-> set_wonder_seed_counts counts=%s",
                            list(msg.counts))
                    elif isinstance(msg, wire.KillMsg):
                        log.info(
                            "-> kill source=%r cause=%r",
                            msg.source, msg.cause)
                    else:
                        log.info("-> %s", type(msg).__name__)
                except (ConnectionResetError, BrokenPipeError) as e:
                    log.warning(
                        "writer_loop: send failed (%s); dropping %s",
                        e, type(msg).__name__)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("writer_loop crashed")
