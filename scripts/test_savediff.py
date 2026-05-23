"""Unit tests for scripts/savediff.py."""

from __future__ import annotations

import struct
import unittest

from scripts import savediff


def make_save(pairs: list[tuple[int, int]], trailing: bytes = b"") -> bytes:
    """Build a minimal SMBW-formatted save buffer for tests."""
    header = struct.pack("<III", savediff.SMBW_SAVE_MAGIC, 1, 0xbf0)
    header += b"\x00" * (savediff.HEADER_BYTES - len(header))
    body = b"".join(struct.pack("<II", k, v) for k, v in pairs)
    # Pad pair region to PAIR_REGION_END_HINT so iter_pair_region walks
    # the whole declared region and `trailing` lives outside it.
    pad_needed = savediff.PAIR_REGION_END_HINT - len(header) - len(body)
    if pad_needed > 0:
        body += b"\x00" * pad_needed
    return header + body + trailing


class HeaderTests(unittest.TestCase):

    def test_parses_valid_header(self):
        buf = make_save([(0x11111111, 1)])
        magic, version, length = savediff.parse_header(buf)
        self.assertEqual(magic, savediff.SMBW_SAVE_MAGIC)
        self.assertEqual(version, 1)
        self.assertEqual(length, 0xbf0)

    def test_rejects_bad_magic(self):
        buf = bytearray(make_save([(1, 1)]))
        buf[0] = 0xFF
        with self.assertRaises(ValueError):
            savediff.parse_header(bytes(buf))

    def test_rejects_short_buffer(self):
        with self.assertRaises(ValueError):
            savediff.parse_header(b"\x04\x03\x02\x01")


class PairRegionTests(unittest.TestCase):

    def test_yields_pairs_in_order(self):
        buf = make_save([(0xaaaa, 1), (0xbbbb, 2), (0xcccc, 3)])
        pairs = list(savediff.iter_pair_region(buf))
        keys = [k for _, k, _ in pairs[:3]]
        self.assertEqual(keys, [0xaaaa, 0xbbbb, 0xcccc])

    def test_walks_full_region(self):
        """Pair region should span 0x28..0xbf0 = 377 pairs."""
        buf = make_save([])
        pairs = list(savediff.iter_pair_region(buf))
        self.assertEqual(len(pairs), savediff.PAIR_REGION_COUNT)
        self.assertEqual(savediff.PAIR_REGION_COUNT, 377)

    def test_back_compat_alias(self):
        """iter_first_table_pairs alias still works."""
        buf = make_save([(0xdead, 9)])
        pairs = list(savediff.iter_first_table_pairs(buf))
        self.assertEqual(pairs[0], (0, 0xdead, 9))


class DiffPairsTests(unittest.TestCase):

    def test_detects_no_change(self):
        a = make_save([(0x1, 5), (0x2, 0)])
        b = make_save([(0x1, 5), (0x2, 0)])
        self.assertEqual(savediff.diff_pairs(a, b), [])

    def test_detects_single_increment(self):
        a = make_save([(0xdeadbeef, 3)])
        b = make_save([(0xdeadbeef, 4)])
        changes = savediff.diff_pairs(a, b)
        self.assertEqual(len(changes), 1)
        c = changes[0]
        self.assertEqual(c.key, 0xdeadbeef)
        self.assertEqual(c.before_value, 3)
        self.assertEqual(c.after_value, 4)
        self.assertEqual(c.classify(), "increment by 1")

    def test_detects_first_acquire(self):
        a = make_save([(0xabcd, 0)])
        b = make_save([(0xabcd, 1)])
        c = savediff.diff_pairs(a, b)[0]
        self.assertEqual(c.classify(), "first-acquire / bit 0 set")

    def test_detects_bit_flip(self):
        a = make_save([(0xfeed, 0b0001)])
        b = make_save([(0xfeed, 0b0101)])
        c = savediff.diff_pairs(a, b)[0]
        self.assertEqual(c.classify(), "bit 2 flip (clear → set)")

    def test_detects_general_change(self):
        a = make_save([(0xface, 100)])
        b = make_save([(0xface, 150)])
        c = savediff.diff_pairs(a, b)[0]
        self.assertEqual(c.classify(), "change (+50)")

    def test_detects_multiple_changes(self):
        a = make_save([(0x1, 0), (0x2, 5), (0x3, 0)])
        b = make_save([(0x1, 1), (0x2, 6), (0x3, 0)])
        changes = savediff.diff_pairs(a, b)
        self.assertEqual(len(changes), 2)
        self.assertEqual({c.key for c in changes}, {0x1, 0x2})

    def test_detects_pair_change_past_old_first_table_boundary(self):
        """Pairs beyond 0x428 (the old first-table boundary) are now diffed.
        Regression test for the bug where flower_coin (at file offset
        0x890 in the real save) was missed because the old code stopped
        at 0x428."""
        # Place the changed pair at file offset 0x890 (the real
        # flower_coin location): pair_index = (0x890 - 0x28) // 8 = 269.
        target_index = (0x890 - savediff.PAIR_REGION_OFFSET) // 8
        pairs_a = [(i + 1, 0) for i in range(target_index)] + [(0xf4ee6827, 148)]
        pairs_b = [(i + 1, 0) for i in range(target_index)] + [(0xf4ee6827, 118)]
        a = make_save(pairs_a)
        b = make_save(pairs_b)
        changes = savediff.diff_pairs(a, b)
        self.assertEqual(len(changes), 1)
        c = changes[0]
        self.assertEqual(c.pair_index, target_index)
        self.assertEqual(c.offset, 0x890)
        self.assertEqual(c.key, 0xf4ee6827)
        self.assertEqual(c.before_value, 148)
        self.assertEqual(c.after_value, 118)


class DiffOutsidePairsTests(unittest.TestCase):

    def test_no_changes_outside_pairs(self):
        a = make_save([(1, 1)], trailing=b"\x00" * 16)
        b = make_save([(1, 1)], trailing=b"\x00" * 16)
        self.assertEqual(savediff.diff_outside_pairs(a, b), [])

    def test_change_in_trailing_blob(self):
        a = make_save([(1, 1)], trailing=b"hello world")
        b = make_save([(1, 1)], trailing=b"HELLO world")
        regions = savediff.diff_outside_pairs(a, b)
        self.assertEqual(len(regions), 1)
        r = regions[0]
        self.assertEqual(r.offset, savediff.PAIR_REGION_END_HINT)
        self.assertEqual(r.before, b"hello")
        self.assertEqual(r.after, b"HELLO")

    def test_skips_pair_region(self):
        """Pair change should NOT also surface as an outside-region change."""
        a = make_save([(0xabcd, 0)])
        b = make_save([(0xabcd, 1)])
        self.assertEqual(savediff.diff_outside_pairs(a, b), [])

    def test_skips_full_pair_region_past_old_boundary(self):
        """Pairs above 0x428 must not leak into trailing-region diffs."""
        # Change pair index 200 (well past the old 0x428 boundary).
        pairs_a = [(i + 1, 0) for i in range(200)] + [(0x12345678, 7)]
        pairs_b = [(i + 1, 0) for i in range(200)] + [(0x12345678, 8)]
        a = make_save(pairs_a)
        b = make_save(pairs_b)
        self.assertEqual(savediff.diff_outside_pairs(a, b), [])

    def test_mask_suppresses_known_noise(self):
        """A change inside a masked range is dropped."""
        mask = [(savediff.PAIR_REGION_END_HINT + 4, 2)]
        a = make_save([(1, 1)], trailing=b"\x00" * 16)
        b_buf = bytearray(a)
        # Touch a byte inside the masked range.
        b_buf[savediff.PAIR_REGION_END_HINT + 5] = 0xFF
        regions = savediff.diff_outside_pairs(a, bytes(b_buf), mask=mask)
        self.assertEqual(regions, [])

    def test_mask_does_not_suppress_outside_range(self):
        """A change outside the masked range still shows up."""
        mask = [(savediff.PAIR_REGION_END_HINT + 4, 2)]
        a = make_save([(1, 1)], trailing=b"\x00" * 16)
        b_buf = bytearray(a)
        b_buf[savediff.PAIR_REGION_END_HINT + 10] = 0xAB
        regions = savediff.diff_outside_pairs(a, bytes(b_buf), mask=mask)
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].offset, savediff.PAIR_REGION_END_HINT + 10)


class FormatRegionChangeTests(unittest.TestCase):

    def test_format_without_context_matches_old_behavior(self):
        r = savediff.RegionChange(offset=0x100, before=b"ab", after=b"AB")
        out = savediff.format_region_change(r)
        self.assertIn("0x0100", out)
        self.assertIn("61 62", out)
        self.assertIn("41 42", out)
        # No context-only fields should appear.
        self.assertNotIn("ctx @", out)

    def test_format_with_context_includes_ascii_and_marker(self):
        before = b"".join(b"abcd" for _ in range(64))  # 256 bytes
        after = bytearray(before)
        after[100] = ord("Z")
        r = savediff.RegionChange(offset=100, before=before[100:101], after=bytes(after[100:101]))
        out = savediff.format_region_change(r, before=before, after=bytes(after), context=8)
        self.assertIn("ctx @ 0x", out)
        self.assertIn("marker:", out)
        # Marker should contain at least one ^^ pair (one changed byte).
        self.assertIn("^^", out)
        # ASCII view should contain printable chars around the change.
        self.assertIn("abcd", out)


if __name__ == "__main__":
    unittest.main()
