"""Asyncio TCP server that bridges the Switch subsdk to the AP client.

Listens on :17777 by default.  Accepts ONE active Switch connection at a
time -- M4 simplification.  Any second connection that arrives while a
client is held replaces the old one (smo's "kick old, accept new" pattern,
which makes Switch reboots / Ryujinx restarts transparent without
manual intervention).

Per connection, the server:

  1. Waits for a HELLO, replies with HELLO_ACK, then immediately pushes
     a SetBadgesAbsolute with the current AP-known mask (replay-on-
     reconnect; container-C badge bits survive Switch reconnect and
     save/reload because the bridge re-clobbers them to AP's view every
     handshake).  Royal Seeds are NOT pushed -- the vanilla game owns
     that state; ``send_set_royal_seeds_absolute`` exists only as a
     manual ``/send_royal_seeds`` override.
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
     the current badge mask, Royal Seed mask, and per-world Wonder
     Seed counts from their providers and pushes the matching
     idempotent absolute-overwrite messages.  This is what reverts
     in-game pickups that bypass AP (badge purchases at Poplin shop /
     badge house, palace clears before AP releases the seed, Wonder
     phase seed grabs) to AP's view within seconds -- AP is the sole
     authority over each surface.

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
    GateEntered,
    GoalCompleted,
    NerveFireMsg,
    PlayReportMsg,
)
from .state import BridgeState


log = logging.getLogger("SMBW")

_SWITCH_LEVEL_MAP: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}


BRIDGE_VERSION = "bridge-m4-dev"
"""Reported back to the Switch in HELLO_ACK.  Bump for observable
behavior changes that a Switch-side log reader might care about."""


BADGE_SYNC_INTERVAL_SEC = 2.0
"""How often the idempotent-sync tick fires while a Switch client is
connected.  Each tick re-sends a SetBadgesAbsolute and a
SetWonderSeedCounts (plus the per-course Wonder Seed bitfield) with
whatever the providers currently return -- all idempotent, so they
coalesce with the per-ReceivedItems and per-HelloMsg sends without
debounce work.  Royal Seeds are NOT synced (vanilla-owned).
Tradeoff: tighter intervals shrink the in-game-pickup visibility
window at the cost of LAN socket traffic.  At 2 s, an in-game pickup
(badge purchase, Wonder phase seed grab, etc.) is visible for up to
~2 s before being reverted; each LAN line is ~60 bytes JSON, trivially
cheap.

Named ``BADGE_SYNC_INTERVAL_SEC`` for diff continuity with the M3.2/M4
rollout; now drives badges and Wonder Seed counts together."""


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

# Level-entry gate -- analog for GateEntered.  Wired to
# ``SMBWContext.handle_gate_entered``, which arms the delayed-kill loop
# when the player sequence-broke into a course AP logic gates.
GateEnteredHandler = Callable[[GateEntered], Awaitable[None]]

# Synchronous callable that returns the current AP-known badge bitmask.
# Set at construction time; the LAN server calls it on every Switch
# HelloMsg and on every periodic tick.  Returning 0 is fine and means
# "AP has no badges -- clobber the Switch to empty".
BadgeMaskProvider = Callable[[], int]


# NOTE: Royal Seeds are no longer pushed to the Switch -- the vanilla
# game owns Royal Seed state.  The former ``RoyalSeedMaskProvider`` and
# its HelloMsg / tick / ReceivedItems sync were removed; AP enforces the
# final-Bowser gate via the level-entry death-gate (see
# ``SMBWContext.handle_gate_entered``) instead of overwriting seed bools.


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


# Synchronous callable returning the absolute 128-bit per-course Wonder
# Seed bitfield AP has granted, as a ``(bits_lo, bits_hi)`` tuple of
# two u64s.  Switch overwrites container-C hash ``0x60458608`` to match
# on every HelloMsg / ReceivedItems / 2 s tick -- same idempotent
# absolute-overwrite pattern as :data:`BadgeMaskProvider`.  Returning
# ``(0, 0)`` is fine and means "AP has no Wonder Seeds yet -- clobber
# the Switch's per-course bitfield to empty".  See
# :meth:`SMBWContext._recompute_wonder_seed_bits` for the bit
# derivation.  (2026-05-29)
WonderSeedBitsProvider = Callable[[], tuple[int, int]]


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
    | wire.SetRoyalSeedsAbsoluteMsg
    | wire.SetRoutableWorldsAbsoluteMsg
    | wire.GrantHashKeyedMsg
    | wire.IncrementHashKeyedMsg
    | wire.SetWonderSeedCountsMsg
    | wire.SetWonderSeedsAbsoluteMsg
    | wire.KillMsg
    | _DrainIncrementsSentinel
)


# Open-world mode (2026-06) providers, analogs of :data:`BadgeMaskProvider`.
# ``RoutableWorldsProvider`` returns the AP-authoritative routable-world
# mask (see :class:`wire.SetRoutableWorldsAbsoluteMsg`); 0 means open-world
# is inactive and the Switch hook no-ops.  ``OpenWorldRoyalSeedProvider``
# returns the Royal-Seed mask to force once Bowser is unlocked, or ``None``
# to skip (the non-open-world default, where the vanilla game owns Royal
# Seeds).  Both are replayed on HelloMsg + the periodic tick so the
# open-world state survives Switch reboots / save reloads.
RoutableWorldsProvider = Callable[[], int]
OpenWorldRoyalSeedProvider = Callable[[], "int | None"]


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
        on_gate_entered: GateEnteredHandler | None = None,
        badge_mask_provider: BadgeMaskProvider | None = None,
        wonder_seed_counts_provider: WonderSeedCountsProvider | None = None,
        wonder_seed_bits_provider: WonderSeedBitsProvider | None = None,
        routable_worlds_provider: RoutableWorldsProvider | None = None,
        open_world_royal_seed_provider: OpenWorldRoyalSeedProvider | None = None,
    ) -> None:
        self._state = state
        self._on_check_emitted = on_check_emitted
        self._on_death_reported = on_death_reported
        self._on_goal_completed = on_goal_completed
        self._on_gate_entered = on_gate_entered
        self._badge_mask_provider = badge_mask_provider
        self._wonder_seed_counts_provider = wonder_seed_counts_provider
        self._wonder_seed_bits_provider = wonder_seed_bits_provider
        self._routable_worlds_provider = routable_worlds_provider
        self._open_world_royal_seed_provider = open_world_royal_seed_provider

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

        # Last-logged absolute-state payloads for the periodic-tick
        # send_set_* methods.  The 2 s tick re-pushes the same value
        # most of the time; logging every push produced ~3000 redundant
        # INFO lines per minute.  We log only on first enqueue per
        # session and on payload change -- so a transition (badge
        # granted, seed unlocked, wonder seed count advanced) shows up
        # while idle ticks stay silent.  Reset on _install_client and
        # _drop_active_client_locked so reconnect re-logs the first
        # absolute push.
        self._last_logged_badges_bits: int | None = None
        self._last_logged_wonder_seed_counts: tuple[int, ...] | None = None
        self._last_logged_wonder_seed_bits: tuple[int, int] | None = None

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
            if self._last_logged_badges_bits != bits:
                log.debug(
                    "send_set_badges_absolute: enqueued bits=0x%x", bits)
                self._last_logged_badges_bits = bits
        except asyncio.QueueFull:
            log.error(
                "send_set_badges_absolute(bits=0x%x): outbound queue "
                "full; dropping", bits)

    def send_set_royal_seeds_absolute(self, mask: int) -> None:
        """Enqueue a SetRoyalSeedsAbsolute to the active Switch client.

        ``mask`` is the absolute 6-bit Royal Seed set to write (bit N =
        world N+1).  The Switch loops the 6 container-B bool hashes and
        writes ``(mask >> bit) & 1`` to each, so the call BOTH grants
        the set bits and clears the unset ones.

        NOT called automatically any more -- Royal Seeds are vanilla-
        owned.  Retained as a manual override surfaced via the
        ``/send_royal_seeds`` client command, for forcing the seeds if
        the death-gate ever misbehaves.  Drop semantics match
        :meth:`send_set_badges_absolute`."""
        msg = wire.SetRoyalSeedsAbsoluteMsg(mask=mask)
        if self._send_queue is None:
            log.warning(
                "send_set_royal_seeds_absolute(mask=0x%x): no Switch "
                "client connected; dropping", mask)
            return
        try:
            self._send_queue.put_nowait(msg)
            log.info(
                "send_set_royal_seeds_absolute: enqueued mask=0x%x", mask)
        except asyncio.QueueFull:
            log.error(
                "send_set_royal_seeds_absolute(mask=0x%x): outbound queue "
                "full; dropping", mask)

    def send_set_routable_worlds(self, mask: int) -> None:
        """Enqueue a SetRoutableWorldsAbsolute (open-world routability) to
        the active Switch client.  ``mask`` bit N = AP-bucket-N world is
        routable (bit 0 = W1 ... bit 5 = W6, bit 8 = Castle/Bowser).  A
        mask of 0 means open-world is inactive and the Switch hook no-ops.

        Same drop-on-no-client semantics as
        :meth:`send_set_badges_absolute`; the next HelloMsg re-pushes via
        :meth:`_push_routable_worlds_now` so a dropped tick is recovered
        on reconnect."""
        msg = wire.SetRoutableWorldsAbsoluteMsg(mask=mask)
        if self._send_queue is None:
            log.warning(
                "send_set_routable_worlds(mask=0x%x): no Switch client "
                "connected; dropping", mask)
            return
        try:
            self._send_queue.put_nowait(msg)
            log.debug("send_set_routable_worlds: enqueued mask=0x%x", mask)
        except asyncio.QueueFull:
            log.error(
                "send_set_routable_worlds(mask=0x%x): outbound queue full; "
                "dropping", mask)

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
            counts_t = tuple(counts)
            if self._last_logged_wonder_seed_counts != counts_t:
                log.debug(
                    "send_set_wonder_seed_counts: enqueued counts=%s",
                    counts)
                self._last_logged_wonder_seed_counts = counts_t
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

    def send_set_wonder_seeds_absolute(self, bits_lo: int, bits_hi: int) -> None:
        """Enqueue a SetWonderSeedsAbsolute (per-course Wonder Seed
        bitfield AP-authoritative sync, 2026-05-29) to the active Switch
        client.  ``bits_lo`` and ``bits_hi`` together form an absolute
        128-bit bitfield -- bit N = Wonder Seed for course with internal
        index N.  Switch overwrites the entire container-C bitfield at
        hash ``0x60458608`` to match.

        Same drop semantics as :meth:`send_set_badges_absolute`.  The
        next HelloMsg triggers a fresh send via
        :meth:`_push_wonder_seed_bits_now`, so any dropped tick is
        reliably recovered on reconnect.
        """
        msg = wire.SetWonderSeedsAbsoluteMsg(bits_lo=bits_lo, bits_hi=bits_hi)
        if self._send_queue is None:
            log.warning(
                "send_set_wonder_seeds_absolute(lo=0x%x, hi=0x%x): no "
                "Switch client connected; dropping",
                bits_lo, bits_hi)
            return
        try:
            self._send_queue.put_nowait(msg)
            tup = (bits_lo, bits_hi)
            if self._last_logged_wonder_seed_bits != tup:
                log.debug(
                    "send_set_wonder_seeds_absolute: enqueued "
                    "bits_lo=0x%x bits_hi=0x%x",
                    bits_lo, bits_hi)
                self._last_logged_wonder_seed_bits = tup
        except asyncio.QueueFull:
            log.error(
                "send_set_wonder_seeds_absolute(lo=0x%x, hi=0x%x): "
                "outbound queue full; dropping",
                bits_lo, bits_hi)

    def send_grant_hash_keyed(self, hash_: int, value: int) -> None:
        """Enqueue a GrantHashKeyed (container-A counter or container-B
        bool write, routed Switch-side by ``isBoolHash`` in
        ``ApFrameBridge.cpp``) to the active Switch client.

        Same drop semantics as :meth:`send_set_badges_absolute`.

        Save-survival: the Switch-side primitive (both container-A
        ``FUN_710049F648`` and container-B ``FUN_710049EA24``) writes
        to a deferred-write dirty buffer at ``gmd+0xf8`` that flushes
        on next save.  This method is exposed for the ``/grant_hash``
        debug command and ad-hoc tests."""
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
            # Royal Seeds are intentionally NOT replayed here -- the
            # vanilla game owns Royal Seed state.  (Was a
            # SetRoyalSeedsAbsolute clobber; removed 2026-06-03.)
            # Same idempotent absolute-overwrite pattern for the
            # container-A Wonder Seed counter (M3.3 follow-up): the
            # bridge holds the canonical AP-derived count and clobbers
            # the Switch's lifetime counter to match on every handshake.
            self._push_wonder_seeds_now()
            # 2026-05-29 -- AP-authoritative per-course Wonder Seed
            # bitfield (container-C hash 0x60458608).  Same handshake-
            # replay so a Switch reconnect (or save-load that wipes
            # live state) re-applies AP's canonical bitfield within
            # one round-trip.
            self._push_wonder_seed_bits_now()
            # Open-world (2026-06): re-assert which worlds are routable
            # from the start (+ the Castle bit once Bowser is unlocked)
            # and, once unlocked, re-force all Royal Seeds.  Replay-on-
            # HelloMsg so the open-world state survives Switch reboots /
            # save reloads.  No-ops when open-world is inactive.
            self._push_routable_worlds_now()
            self._push_open_world_royal_seeds_now()
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

        if isinstance(msg, wire.LogMsg):
            log.log(
                _SWITCH_LEVEL_MAP.get(msg.level, logging.INFO),
                "[switch] %s",
                msg.msg,
            )
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
            elif isinstance(emit, GateEntered):
                if self._on_gate_entered is None:
                    continue
                try:
                    await self._on_gate_entered(emit)
                except Exception:
                    log.exception(
                        "on_gate_entered handler crashed for %r", emit)
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
            # Forget last-logged absolute-state payloads so the new
            # session's first SetBadges/SetWonderSeeds push logs
            # (matters when a Switch reconnects and we need to see the
            # initial sync land).
            self._last_logged_badges_bits = None
            self._last_logged_wonder_seed_counts = None
            self._last_logged_wonder_seed_bits = None
            self._writer_task = asyncio.create_task(
                self._writer_loop(writer, self._send_queue),
                name="lan-writer",
            )
            self._badge_sync_task = asyncio.create_task(
                self._idempotent_sync_loop(),
                name="lan-idempotent-sync",
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
        # Match the per-session reset done in _install_client so the
        # next session's first absolute-state push logs.
        self._last_logged_badges_bits = None
        self._last_logged_wonder_seed_counts = None

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

    def _push_wonder_seed_bits_now(self) -> None:
        """Pull the AP-known 128-bit per-course Wonder Seed bitfield from
        the provider and enqueue a ``SetWonderSeedsAbsoluteMsg``.  No-op
        if no provider was wired (e.g. unit tests that don't care about
        seeds) or no client is connected.  Same idempotent absolute-
        overwrite pattern as badges: AP holds the canonical 128-bit
        bitfield derived from ``items_received``; any in-game Wonder Seed
        grab gets clobbered back to AP's view within ~2 s.  Called from
        HelloMsg dispatch, per-ReceivedItems push (via
        :meth:`send_set_wonder_seeds_absolute` directly in
        ``SMBWContext._handle_received_items``), and the periodic tick.
        (2026-05-29)"""
        if self._wonder_seed_bits_provider is None:
            return
        try:
            bits_lo, bits_hi = self._wonder_seed_bits_provider()
        except Exception:
            log.exception(
                "wonder_seed_bits_provider raised; skipping sync")
            return
        self.send_set_wonder_seeds_absolute(int(bits_lo), int(bits_hi))

    def _push_routable_worlds_now(self) -> None:
        """Pull the AP-known routable-world mask from the provider and
        enqueue a ``SetRoutableWorldsAbsoluteMsg``.  No-op if no provider
        was wired (non-open-world clients) or no client is connected.
        Called from HelloMsg dispatch, the per-ReceivedItems push (via
        :meth:`send_set_routable_worlds` directly in
        ``SMBWContext._handle_received_items``), and the periodic tick."""
        if self._routable_worlds_provider is None:
            return
        try:
            mask = int(self._routable_worlds_provider())
        except Exception:
            log.exception("routable_worlds_provider raised; skipping sync")
            return
        self.send_set_routable_worlds(mask)

    def _push_open_world_royal_seeds_now(self) -> None:
        """Open-world only: once Bowser is unlocked, re-force the Royal
        Seeds (the provider returns the mask to set, or ``None`` to skip).
        No-op when no provider was wired or the provider returns ``None``
        (the non-open-world default, where the vanilla game owns Royal
        Seeds).  Replayed on HelloMsg + tick so the unlocked Castle route
        survives Switch reboots / save reloads."""
        if self._open_world_royal_seed_provider is None:
            return
        try:
            mask = self._open_world_royal_seed_provider()
        except Exception:
            log.exception(
                "open_world_royal_seed_provider raised; skipping sync")
            return
        if mask is None:
            return
        self.send_set_royal_seeds_absolute(int(mask))

    async def _idempotent_sync_loop(self) -> None:
        """Periodic tick: every ``BADGE_SYNC_INTERVAL_SEC``, push the
        current AP-known badge mask, Royal Seed mask, AND per-world
        Wonder Seed counts to the Switch.  This is what reverts in-game
        pickups that bypass AP (badge purchases at Poplin shop / badge
        house, palace clears before AP releases the seed, Wonder phase
        seed grabs, flag-pole goal seeds, 10-coin rewards) to AP's view
        within seconds -- AP is the sole authority over each surface.

        Runs until cancelled by ``_drop_active_client_locked`` (i.e.
        only while a Switch client is connected; no point ticking when
        no one is listening)."""
        try:
            while True:
                await asyncio.sleep(BADGE_SYNC_INTERVAL_SEC)
                self._push_badge_sync_now()
                # Royal Seeds intentionally not synced -- vanilla-owned
                # (except open-world, where _push_open_world_royal_seeds_now
                # re-forces them once Bowser is unlocked).
                self._push_wonder_seeds_now()
                self._push_wonder_seed_bits_now()
                self._push_routable_worlds_now()
                self._push_open_world_royal_seeds_now()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("idempotent_sync_loop crashed")

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
        # Per-connection last-logged absolute-state values, so the
        # `-> set_*` lines emit only on payload change (or first send).
        # Naturally scoped: a new connection spawns a new _writer_loop
        # task with these reset to None, so the initial sync logs.
        last_badges_bits: int | None = None
        last_royal_seeds_mask: int | None = None
        last_wonder_seed_counts: tuple[int, ...] | None = None
        last_wonder_seed_bits: tuple[int, int] | None = None
        last_routable_worlds_mask: int | None = None
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
                        # Log only on payload change.  Subsumes the
                        # previous "skip empty mask" rule (the initial
                        # all-zero mask is logged once, then suppressed
                        # until something flips) and also silences the
                        # steady-state replays of any non-zero mask.
                        if msg.bits != last_badges_bits:
                            log.info("-> set_badges_absolute bits=0x%x", msg.bits)
                            last_badges_bits = msg.bits
                    elif isinstance(msg, wire.SetRoyalSeedsAbsoluteMsg):
                        if msg.mask != last_royal_seeds_mask:
                            log.info(
                                "-> set_royal_seeds_absolute mask=0x%x",
                                msg.mask)
                            last_royal_seeds_mask = msg.mask
                    elif isinstance(msg, wire.SetRoutableWorldsAbsoluteMsg):
                        if msg.mask != last_routable_worlds_mask:
                            log.info(
                                "-> set_routable_worlds mask=0x%x", msg.mask)
                            last_routable_worlds_mask = msg.mask
                    elif isinstance(msg, wire.SetWonderSeedsAbsoluteMsg):
                        tup = (msg.bits_lo, msg.bits_hi)
                        if tup != last_wonder_seed_bits:
                            log.info(
                                "-> set_wonder_seeds_absolute "
                                "bits_lo=0x%x bits_hi=0x%x",
                                msg.bits_lo, msg.bits_hi)
                            last_wonder_seed_bits = tup
                    elif isinstance(msg, wire.GrantHashKeyedMsg):
                        log.info(
                            "-> grant_hash_keyed hash=0x%08x value=%d",
                            msg.hash, msg.value)
                    elif isinstance(msg, wire.IncrementHashKeyedMsg):
                        log.info(
                            "-> increment_hash_keyed hash=0x%08x delta=%d",
                            msg.hash, msg.delta)
                    elif isinstance(msg, wire.SetWonderSeedCountsMsg):
                        counts_t = tuple(msg.counts)
                        if counts_t != last_wonder_seed_counts:
                            log.info(
                                "-> set_wonder_seed_counts counts=%s",
                                list(msg.counts))
                            last_wonder_seed_counts = counts_t
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
