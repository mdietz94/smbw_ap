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
    # Pad first table to FIRST_TABLE_END_HINT so iter_first_table_pairs
    # walks the whole declared region.
    pad_needed = savediff.FIRST_TABLE_END_HINT - len(header) - len(body)
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


class FirstTablePairTests(unittest.TestCase):

    def test_yields_pairs_in_order(self):
        buf = make_save([(0xaaaa, 1), (0xbbbb, 2), (0xcccc, 3)])
        pairs = list(savediff.iter_first_table_pairs(buf))
        keys = [k for _, k, _ in pairs[:3]]
        self.assertEqual(keys, [0xaaaa, 0xbbbb, 0xcccc])


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
        # 0b0001 → 0b0101 — single new bit (bit 2) set on a previously-
        # nonzero value, so the 0 → N branch doesn't apply; xor popcount == 1.
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
        self.assertEqual(r.offset, savediff.FIRST_TABLE_END_HINT)
        self.assertEqual(r.before, b"hello")
        self.assertEqual(r.after, b"HELLO")

    def test_skips_first_table_region(self):
        # Pair change should NOT also surface as an outside-region change.
        a = make_save([(0xabcd, 0)])
        b = make_save([(0xabcd, 1)])
        self.assertEqual(savediff.diff_outside_pairs(a, b), [])


if __name__ == "__main__":
    unittest.main()
