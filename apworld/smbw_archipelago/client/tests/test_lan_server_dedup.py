"""Unit tests for the periodic-tick dedup logic in :class:`LanServer`.

The 2 s ``_idempotent_sync_loop`` fires a ``SetBadgesAbsolute`` +
``SetRoyalSeedsAbsolute`` + ``SetWonderSeedCounts`` triplet every tick,
even when nothing has changed.  Switch-side ``drainInbound`` is gated
by ``isSaveLoaded`` + ``isInSceneTransitionWindow``, so identical
triplets pile up in the SPSC ring during gated windows and flush as
bursts when the gate opens (the parent triage saw 18 triplets drained
in 3 ms after a long gated window in
Ryujinx_1.3.3_2026-05-26_19-16-12.log lines 11197-11405).

Dedup contract verified here:

* ``dedup=True`` skips the put when the new payload byte-equals the
  last successfully enqueued value for that message type -- the path
  the periodic tick uses.
* ``dedup=False`` always enqueues -- the path HelloMsg replays and
  per-ReceivedItems pushes use.  Bypassing dedup also re-establishes
  the cache so a subsequent ``dedup=True`` call deduplicates against
  the freshly-enqueued value.
* A real state change after a dedup-skipped tick still enqueues on the
  next ``dedup=True`` call (the cache only matches the *last enqueued*
  value, not the *last seen* value).
* Reinstalling a client clears the cache -- a fresh Switch hasn't seen
  any of the previous session's payloads.

The Switch side is not under test -- its handlers are idempotent on
absolute overwrites and unchanged by this PR.
"""

from __future__ import annotations

import asyncio
import unittest

from .. import wire
from ..lan_server import LanServer
from ..state import BridgeState


def _drain_queue(server: LanServer) -> list[wire.WireMsg]:
    """Pull every queued message synchronously without running the
    writer task.  The tests drive ``send_*`` directly and inspect what
    landed in the queue, so we don't need real sockets or a real Switch
    client.  Mirrors the pattern smo_archipelago's switch_server tests
    use for queue-shape assertions."""
    out: list[wire.WireMsg] = []
    queue = server._send_queue
    assert queue is not None, "no active session"
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


class _FakeWriter:
    """Stand-in for :class:`asyncio.StreamWriter` that records nothing.
    The dedup logic only consults ``self._send_queue`` and the cache,
    so we never need real I/O for these tests."""

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass

    def write(self, _data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass


class TestPeriodicTickDedup(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self.state = BridgeState()
        self.server = LanServer(self.state)
        # Stand up a fake "session" so the send_* methods don't drop on
        # ``_send_queue is None``.  We bypass _install_client because
        # we don't want the writer / sync tick tasks running -- the
        # tests want exact control over what's in the queue.
        self.server._client_writer = _FakeWriter()  # type: ignore[assignment]
        self.server._send_queue = asyncio.Queue()
        self.server._last_sent_badges_bits = None
        self.server._last_sent_royal_seeds_mask = None
        self.server._last_sent_wonder_seed_counts = None

    async def asyncTearDown(self) -> None:
        self.server._send_queue = None
        self.server._client_writer = None

    # ---- SetBadgesAbsolute --------------------------------------------

    async def test_badges_dedup_skips_identical_successive_payloads(self) -> None:
        self.server.send_set_badges_absolute(0x10, dedup=True)
        self.server.send_set_badges_absolute(0x10, dedup=True)
        self.server.send_set_badges_absolute(0x10, dedup=True)
        queued = _drain_queue(self.server)
        self.assertEqual(len(queued), 1)
        self.assertIsInstance(queued[0], wire.SetBadgesAbsoluteMsg)
        self.assertEqual(queued[0].bits, 0x10)

    async def test_badges_dedup_passes_real_changes_through(self) -> None:
        # An interleaved sequence of changes + repeats should produce
        # one queued message per distinct payload.
        for bits in (0x0, 0x0, 0x10, 0x10, 0x30, 0x30, 0x10, 0x10):
            self.server.send_set_badges_absolute(bits, dedup=True)
        queued = _drain_queue(self.server)
        self.assertEqual(
            [m.bits for m in queued],  # type: ignore[attr-defined]
            [0x0, 0x10, 0x30, 0x10],
        )

    async def test_badges_no_dedup_always_enqueues(self) -> None:
        # HelloMsg + ReceivedItems path: dedup=False must always fire,
        # even when the payload matches the cache.
        self.server.send_set_badges_absolute(0x10, dedup=True)
        self.server.send_set_badges_absolute(0x10, dedup=False)
        self.server.send_set_badges_absolute(0x10, dedup=False)
        queued = _drain_queue(self.server)
        self.assertEqual(len(queued), 3)
        self.assertTrue(all(
            isinstance(m, wire.SetBadgesAbsoluteMsg) and m.bits == 0x10
            for m in queued
        ))

    async def test_badges_no_dedup_updates_cache_for_next_dedup_call(self) -> None:
        # After a dedup=False enqueue updates the cache, a dedup=True
        # call with the same payload should skip.
        self.server.send_set_badges_absolute(0x42, dedup=False)
        self.server.send_set_badges_absolute(0x42, dedup=True)
        queued = _drain_queue(self.server)
        self.assertEqual(len(queued), 1)

    # ---- SetRoyalSeedsAbsolute ----------------------------------------

    async def test_royal_seeds_dedup_skips_identical(self) -> None:
        self.server.send_set_royal_seeds_absolute(0x05, dedup=True)
        self.server.send_set_royal_seeds_absolute(0x05, dedup=True)
        queued = _drain_queue(self.server)
        self.assertEqual(len(queued), 1)
        self.assertIsInstance(queued[0], wire.SetRoyalSeedsAbsoluteMsg)
        self.assertEqual(queued[0].mask, 0x05)

    async def test_royal_seeds_dedup_passes_changes(self) -> None:
        for mask in (0x0, 0x0, 0x01, 0x05, 0x05, 0x07):
            self.server.send_set_royal_seeds_absolute(mask, dedup=True)
        queued = _drain_queue(self.server)
        self.assertEqual(
            [m.mask for m in queued],  # type: ignore[attr-defined]
            [0x0, 0x01, 0x05, 0x07],
        )

    async def test_royal_seeds_no_dedup_always_enqueues(self) -> None:
        self.server.send_set_royal_seeds_absolute(0x3F, dedup=True)
        self.server.send_set_royal_seeds_absolute(0x3F, dedup=False)
        queued = _drain_queue(self.server)
        self.assertEqual(len(queued), 2)

    # ---- SetWonderSeedCounts ------------------------------------------

    async def test_wonder_seed_counts_dedup_skips_identical(self) -> None:
        counts = [3, 0, 5, 0, 0, 0, 0, 0]
        self.server.send_set_wonder_seed_counts(list(counts), dedup=True)
        self.server.send_set_wonder_seed_counts(list(counts), dedup=True)
        self.server.send_set_wonder_seed_counts(list(counts), dedup=True)
        queued = _drain_queue(self.server)
        self.assertEqual(len(queued), 1)
        self.assertIsInstance(queued[0], wire.SetWonderSeedCountsMsg)
        self.assertEqual(list(queued[0].counts), counts)  # type: ignore[attr-defined]

    async def test_wonder_seed_counts_dedup_detects_single_element_change(
        self,
    ) -> None:
        # Element-wise compare: flipping a single bucket from 0 to 1
        # must NOT be deduplicated against the all-zero baseline.
        zeros = [0] * 8
        bumped = [0, 0, 1, 0, 0, 0, 0, 0]
        self.server.send_set_wonder_seed_counts(list(zeros), dedup=True)
        self.server.send_set_wonder_seed_counts(list(zeros), dedup=True)
        self.server.send_set_wonder_seed_counts(list(bumped), dedup=True)
        self.server.send_set_wonder_seed_counts(list(bumped), dedup=True)
        queued = _drain_queue(self.server)
        self.assertEqual(len(queued), 2)
        self.assertEqual(list(queued[0].counts), zeros)  # type: ignore[attr-defined]
        self.assertEqual(list(queued[1].counts), bumped)  # type: ignore[attr-defined]

    async def test_wonder_seed_counts_no_dedup_always_enqueues(self) -> None:
        counts = [1, 2, 3, 4, 5, 6, 7, 8]
        self.server.send_set_wonder_seed_counts(list(counts), dedup=True)
        self.server.send_set_wonder_seed_counts(list(counts), dedup=False)
        queued = _drain_queue(self.server)
        self.assertEqual(len(queued), 2)

    # ---- Cache isolation between message types ------------------------

    async def test_dedup_caches_are_per_message_type(self) -> None:
        # Sending badges=0 should not influence the royal-seeds or
        # wonder-counts cache, even though they all happen to be
        # "zero-ish".  This guards against accidentally collapsing the
        # caches into a single field.
        self.server.send_set_badges_absolute(0, dedup=True)
        self.server.send_set_royal_seeds_absolute(0, dedup=True)
        self.server.send_set_wonder_seed_counts([0] * 8, dedup=True)
        queued = _drain_queue(self.server)
        self.assertEqual(len(queued), 3)
        self.assertIsInstance(queued[0], wire.SetBadgesAbsoluteMsg)
        self.assertIsInstance(queued[1], wire.SetRoyalSeedsAbsoluteMsg)
        self.assertIsInstance(queued[2], wire.SetWonderSeedCountsMsg)

    # ---- Triplet-burst scenario (the parent investigation) ------------

    async def test_periodic_tick_triplet_burst_collapses_to_zero(self) -> None:
        """Simulate the parent investigation directly: the
        ``_idempotent_sync_loop`` body calls
        ``_push_badge_sync_now(dedup=True)``,
        ``_push_royal_seeds_now(dedup=True)``,
        ``_push_wonder_seeds_now(dedup=True)`` once per tick.  Three
        ticks in a row with unchanged providers should produce three
        messages total (the first tick's baseline), not nine."""
        self.server._badge_mask_provider = lambda: 0x10
        self.server._royal_seed_mask_provider = lambda: 0x05
        self.server._wonder_seed_counts_provider = lambda: [1, 0, 0, 0, 0, 0, 0, 0]

        for _ in range(3):
            self.server._push_badge_sync_now(dedup=True)
            self.server._push_royal_seeds_now(dedup=True)
            self.server._push_wonder_seeds_now(dedup=True)

        queued = _drain_queue(self.server)
        self.assertEqual(len(queued), 3)
        self.assertIsInstance(queued[0], wire.SetBadgesAbsoluteMsg)
        self.assertIsInstance(queued[1], wire.SetRoyalSeedsAbsoluteMsg)
        self.assertIsInstance(queued[2], wire.SetWonderSeedCountsMsg)

    async def test_hello_path_pushes_after_dedup_skip(self) -> None:
        """A HelloMsg-style replay (dedup=False) must always reach the
        Switch, even after the periodic tick has cached the same
        payload."""
        self.server._badge_mask_provider = lambda: 0x10
        self.server._royal_seed_mask_provider = lambda: 0x05
        self.server._wonder_seed_counts_provider = lambda: [0] * 8

        # Periodic tick fills the cache.
        self.server._push_badge_sync_now(dedup=True)
        self.server._push_royal_seeds_now(dedup=True)
        self.server._push_wonder_seeds_now(dedup=True)
        # Drain so we see only the HelloMsg-path messages below.
        _drain_queue(self.server)

        # HelloMsg-style replay: identical payloads, but always pushed.
        self.server._push_badge_sync_now()
        self.server._push_royal_seeds_now()
        self.server._push_wonder_seeds_now()
        queued = _drain_queue(self.server)
        self.assertEqual(len(queued), 3)


class TestDedupCacheLifecycle(unittest.IsolatedAsyncioTestCase):
    """A fresh client session must start with an empty dedup cache --
    otherwise the next periodic tick would skip a push the Switch has
    never seen."""

    async def test_install_client_clears_cache(self) -> None:
        state = BridgeState()
        server = LanServer(state)
        try:
            await server.start(host="127.0.0.1", port=0)
            # Pretend a prior session left state in the cache.
            server._last_sent_badges_bits = 0xDEAD
            server._last_sent_royal_seeds_mask = 0x3F
            server._last_sent_wonder_seed_counts = (9,) * 8

            writer = _FakeWriter()
            await server._install_client(writer)  # type: ignore[arg-type]

            self.assertIsNone(server._last_sent_badges_bits)
            self.assertIsNone(server._last_sent_royal_seeds_mask)
            self.assertIsNone(server._last_sent_wonder_seed_counts)
        finally:
            await server.stop()

    async def test_drop_client_clears_cache(self) -> None:
        state = BridgeState()
        server = LanServer(state)
        try:
            await server.start(host="127.0.0.1", port=0)
            writer = _FakeWriter()
            await server._install_client(writer)  # type: ignore[arg-type]
            # Pretend a few sends landed.
            server._last_sent_badges_bits = 0x10
            server._last_sent_royal_seeds_mask = 0x05
            server._last_sent_wonder_seed_counts = (1,) * 8

            await server._drop_active_client()

            self.assertIsNone(server._last_sent_badges_bits)
            self.assertIsNone(server._last_sent_royal_seeds_mask)
            self.assertIsNone(server._last_sent_wonder_seed_counts)
        finally:
            await server.stop()


if __name__ == "__main__":
    unittest.main()
