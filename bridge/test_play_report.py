"""Unit tests for the PlayReport decoder.

Run from project root: `python -m unittest bridge.test_play_report`
Or directly:           `python bridge/test_play_report.py`

The three big fixtures (world_activity / world_result / course_result)
are real bytes captured 2026-05-20 while playing through W1-1 Welcome to
the Flower Kingdom. They're cross-validated against the JSON Ryujinx's
ServicePrepo decoder printed for the same buffers.
"""

from __future__ import annotations

import unittest

# Allow `python bridge/test_play_report.py`.
if __package__ is None or __package__ == "":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from play_report import DecodeError, decode_play_report
else:
    from .play_report import DecodeError, decode_play_report


def _hex(*lines: str) -> bytes:
    """Assemble multi-line hex strings (with spaces) into one bytes object."""
    return bytes.fromhex("".join(lines).replace(" ", "").replace("\n", ""))


# ---------------------------------------------------------------------------
# Header + error-handling tests.

class TestHeader(unittest.TestCase):
    def test_empty_report(self):
        result = decode_play_report(b"\xde\x00\x00")
        self.assertEqual(result.entry_count, 0)
        self.assertEqual(result.decoded_count, 0)
        self.assertEqual(result.fields, {})
        self.assertIsNone(result.error)

    def test_bad_magic_raises(self):
        with self.assertRaises(DecodeError) as cm:
            decode_play_report(b"\x00\x00\x01")
        self.assertEqual(cm.exception.pos, 0)
        self.assertEqual(cm.exception.op, 0x00)

    def test_bad_magic_in_partial_still_raises(self):
        with self.assertRaises(DecodeError):
            decode_play_report(b"\x00\x00\x01", partial_ok=True)


# ---------------------------------------------------------------------------
# Primitive-value tests (synthesized 1-entry reports).

class TestPrimitiveValues(unittest.TestCase):
    def _one(self, body_hex: str):
        body = _hex(body_hex)
        result = decode_play_report(b"\xde\x00\x01" + body)
        self.assertEqual(result.decoded_count, 1)
        self.assertIsNone(result.error)
        return result

    def test_inline_uint_small(self):
        # key "x" (a1 78) + uint inline 5
        r = self._one("a1 78  05")
        self.assertEqual(r.fields, {"x": 5})

    def test_inline_uint_extends_past_cbor(self):
        # 0x1A = 26 — would be u32 marker in standard CBOR, but Nintendo
        # uses the whole 0x00..0x7F range for inline uints.
        r = self._one("a1 78  1a")
        self.assertEqual(r.fields, {"x": 26})

    def test_inline_uint_max(self):
        # 0x7F = 127 is the top of the inline range.
        r = self._one("a1 78  7f")
        self.assertEqual(r.fields, {"x": 127})

    def test_uint_u8(self):
        # 0xCC + u8. 0x87 = 135 (above inline range).
        r = self._one("a1 78  cc 87")
        self.assertEqual(r.fields, {"x": 135})

    def test_uint_u32(self):
        # 0xCE + u32_be.  Real example: system_report_tag = 2175206328.
        r = self._one("a1 78  ce 81 a7 03 b8")
        self.assertEqual(r.fields, {"x": 0x81A703B8})

    def test_signed_s32_via_d2(self):
        # 0xD2 + s32_be — signed 32-bit extension. Used by Struct::Add(long)
        # when the value fits in positive s32. Real example: W1-2 Piranha
        # Plants on Parade stage_key 232160011 = 0x0DD67B0B.
        r = self._one("a1 78  d2 0d d6 7b 0b")
        self.assertEqual(r.fields, {"x": 232160011})

    def test_signed_s32_negative_via_d2(self):
        # Negative s32: 0xFFFFFFFF in s32 BE = -1.
        r = self._one("a1 78  d2 ff ff ff ff")
        self.assertEqual(r.fields, {"x": -1})

    def test_signed_s64_via_d3(self):
        # 0xD3 + s64_be — signed 64-bit extension. Encoder bumps to s64
        # when the value won't fit in positive s32. Real example: W1-1
        # stage_key 2937190396 = 0xAF11F7FC (high bit set as s32 →
        # negative; high bytes 0 in s64 keeps it positive).
        r = self._one("a1 78  d3 00 00 00 00 af 11 f7 fc")
        self.assertEqual(r.fields, {"x": 2937190396})

    def test_signed_s8_via_d0(self):
        # GUESSED encoding (not yet observed live). 0xD0 + s8 = -5.
        r = self._one("a1 78  d0 fb")
        self.assertEqual(r.fields, {"x": -5})

    def test_signed_s16_via_d1(self):
        # GUESSED encoding (not yet observed live). 0xD1 + s16 BE = -300.
        r = self._one("a1 78  d1 fe d4")
        self.assertEqual(r.fields, {"x": -300})

    def test_any64bitid_via_d7(self):
        # total_play_time = 50 = 0x32, encoded as Any64BitId.
        # Encoding: 0xD7 + u8 TypeCode + u64 BE Value (9 bytes after opcode).
        r = self._one("a1 78  d7 00 00 00 00 00 00 00 00 32")
        self.assertEqual(r.fields, {"x": {"TypeCode": 0, "Value": 0x32}})

    def test_bool_false_c2(self):
        r = self._one("a1 78  c2")
        self.assertEqual(r.fields, {"x": False})

    def test_bool_true_c3(self):
        r = self._one("a1 78  c3")
        self.assertEqual(r.fields, {"x": True})

    def test_neg_one_ff(self):
        # 0xFF is the only negative we've seen — used for transition_type,
        # last_page, conn_result, etc.
        r = self._one("a1 78  ff")
        self.assertEqual(r.fields, {"x": -1})


# ---------------------------------------------------------------------------
# String, struct, array tests.

class TestStrings(unittest.TestCase):
    def test_short_string(self):
        # value "hello" — len 5, opcode 0xA5
        r = decode_play_report(_hex("de 00 01  a1 78  a5 68 65 6c 6c 6f"))
        self.assertEqual(r.fields, {"x": "hello"})

    def test_empty_string(self):
        r = decode_play_report(_hex("de 00 01  a1 78  a0"))
        self.assertEqual(r.fields, {"x": ""})

    def test_medium_string_d9(self):
        # 35-char UUID via Nintendo's 0xD9 marker.
        uuid = "b813e675-eb254c8a-a3e0d052-df1afad0"
        body = _hex("a1 78  d9 23") + uuid.encode()
        r = decode_play_report(b"\xde\x00\x01" + body)
        self.assertEqual(r.fields, {"x": uuid})


class TestContainers(unittest.TestCase):
    def test_empty_struct(self):
        r = decode_play_report(_hex("de 00 01  a1 78  80"))
        self.assertEqual(r.fields, {"x": {}})

    def test_struct_with_two_int_fields(self):
        # 0x82 = struct(2): "a" -> 1, "b" -> 2
        r = decode_play_report(
            _hex("de 00 01  a1 78  82  a1 61 01  a1 62 02"))
        self.assertEqual(r.fields, {"x": {"a": 1, "b": 2}})

    def test_empty_array(self):
        r = decode_play_report(_hex("de 00 01  a1 78  90"))
        self.assertEqual(r.fields, {"x": []})

    def test_array_of_ints(self):
        # 0x92 = array(2): [3, 7]
        r = decode_play_report(_hex("de 00 01  a1 78  92  03  07"))
        self.assertEqual(r.fields, {"x": [3, 7]})

    def test_array_of_bools(self):
        # get_medal_array shape: 6 bools, all false.
        r = decode_play_report(
            _hex("de 00 01  a1 78  96  c2 c2 c2 c2 c2 c2"))
        self.assertEqual(r.fields, {"x": [False] * 6})


# ---------------------------------------------------------------------------
# Live W1-1 captures — the canonical M2.4 fixtures.

# world_activity (239 bytes, 10 fields) — fires when entering the course.
WORLD_ACTIVITY = _hex(
    "de 00 0a ab 73 61 76 65 64 61 74 61 5f 69 64 d9 23 62 38 31 33 65"
    "36 37 35 2d 65 62 32 35 34 63 38 61 2d 61 33 65 30 64 30 35 32 2d"
    "64 66 31 61 66 61 64 30 a9 70 6c 61 79 5f 6d 6f 64 65 01 af 74 6f"
    "74 61 6c 5f 70 6c 61 79 5f 74 69 6d 65 d7 00 00 00 00 00 00 00 00"
    "32 aa 73 74 61 67 65 5f 69 6e 66 6f 83 a9 73 74 61 67 65 5f 6b 65"
    "79 d3 00 00 00 00 d4 a6 26 5d aa 77 6f 72 6c 64 5f 6b",
    "69 6e 64 00 a8 77 6f 72 6c 64 5f 6e 6f 01 ad 61 63 74 69 76 69 74"
    "79 5f 74 79 70 65 00 ae 61 63 74 69 76 69 74 79 5f 70 6c 61 63 65"
    "00 ae 61 63 74 69 76 69 74 79 5f 76 61 6c 75 65 02 ab 77 6f 6e 64"
    "65 72 5f 63 6f 69 6e cc 87 ab 77 6f 6e 64 65 72 5f 73 65 65 64 0e"
    "b1 73 79 73 74 65 6d 5f 72 65 70 6f 72 74 5f 74 61 67 ce 81 a7 03"
    "b8",
)


class TestWorldActivityPayload(unittest.TestCase):
    """The 'entering a course' report fires once on overworld→course."""

    def test_decodes_clean(self):
        self.assertEqual(len(WORLD_ACTIVITY), 239)
        r = decode_play_report(WORLD_ACTIVITY)
        self.assertEqual(r.entry_count, 10)
        self.assertEqual(r.decoded_count, 10)
        self.assertIsNone(r.error)

    def test_top_level_scalars_match_ryujinx(self):
        r = decode_play_report(WORLD_ACTIVITY)
        self.assertEqual(
            r.fields["savedata_id"],
            "b813e675-eb254c8a-a3e0d052-df1afad0")
        self.assertEqual(r.fields["play_mode"], 1)
        self.assertEqual(r.fields["activity_type"], 0)
        self.assertEqual(r.fields["activity_place"], 0)
        self.assertEqual(r.fields["activity_value"], 2)
        self.assertEqual(r.fields["wonder_coin"], 135)
        self.assertEqual(r.fields["wonder_seed"], 14)
        self.assertEqual(r.fields["system_report_tag"], 2175206328)

    def test_any64bitid_total_play_time(self):
        r = decode_play_report(WORLD_ACTIVITY)
        self.assertEqual(
            r.fields["total_play_time"],
            {"TypeCode": 0, "Value": 0x32})

    def test_nested_struct_stage_info(self):
        # In world_activity, stage_info has only 3 fields (no course_no).
        r = decode_play_report(WORLD_ACTIVITY)
        self.assertEqual(r.fields["stage_info"], {
            "stage_key": 3567658589,
            "world_kind": 0,
            "world_no": 1,
        })


# world_result (1059 bytes, 26 fields) — fires on overworld→course transition,
# carries next_stage_info with the destination course's stage_key.
WORLD_RESULT = _hex(
    "de 00 1a ab 73 61 76 65 64 61 74 61 5f 69 64 d9 23 62 38 31 33 65"
    "36 37 35 2d 65 62 32 35 34 63 38 61 2d 61 33 65 30 64 30 35 32 2d"
    "64 66 31 61 66 61 64 30 a9 70 6c 61 79 5f 6d 6f 64 65 01 af 74 6f"
    "74 61 6c 5f 70 6c 61 79 5f 74 69 6d 65 d7 00 00 00 00 00 00 00 00"
    "32 aa 73 74 61 67 65 5f 69 6e 66 6f 83 a9 73 74 61 67 65 5f 6b 65"
    "79 d3 00 00 00 00 d4 a6 26 5d aa 77 6f 72 6c 64 5f 6b",
    "69 6e 64 00 a8 77 6f 72 6c 64 5f 6e 6f 01 af 6e 65 78 74 5f 73 74"
    "61 67 65 5f 69 6e 66 6f 85 a9 73 74 61 67 65 5f 6b 65 79 d3 00 00"
    "00 00 af 11 f7 fc aa 73 74 61 67 65 5f 74 79 70 65 01 aa 77 6f 72"
    "6c 64 5f 6b 69 6e 64 00 a8 77 6f 72 6c 64 5f 6e 6f 01 a9 63 6f 75"
    "72 73 65 5f 69 64 02 af 74 72 61 6e 73 69 74 69 6f 6e 5f 69 6e 66"
    "6f 84 af 74 72 61 6e 73 69 74 69 6f 6e 5f 74 79 70 65",
    "ff ab 77 6f 72 6c 64 6d 61 70 5f 69 64 00 a9 63 6f 75 72 73 65 5f"
    "69 64 00 a6 6e 70 63 5f 69 64 00 b3 74 6f 74 61 6c 5f 70 6c 61 79"
    "5f 74 69 6d 65 5f 73 65 63 1a b2 6c 61 73 74 5f 63 74 72 6c 5f 62"
    "79 5f 73 74 63 69 6b c3 b0 6c 6f 63 61 6c 5f 70 6c 61 79 65 72 5f"
    "6e 75 6d 01 b2 76 69 73 69 74 6f 72 5f 70 6c 61 79 65 72 5f 6e 75"
    "6d 00 b5 77 6f 72 6c 64 5f 72 6f 6f 6d 5f 6d 65 6d 62",
    "65 72 5f 6e 75 6d 00 b6 66 72 69 65 6e 64 5f 72 6f 6f 6d 5f 6d 65"
    "6d 62 65 72 5f 6e 75 6d 00 b3 66 72 69 65 6e 64 5f 72 6f 6f 6d 5f"
    "68 61 73 68 5f 69 64 00 b4 63 6f 75 72 73 65 5f 6c 69 73 74 5f 77"
    "61 72 70 5f 6e 75 6d 00 b5 67 65 74 5f 79 65 6c 6c 6f 77 5f 63 6f"
    "69 6e 5f 63 6f 75 6e 74 00 b5 67 65 74 5f 77 6f 6e 64 65 72 5f 63"
    "6f 69 6e 5f 63 6f 75 6e 74 00 ae 61 64 64 5f 72 65 73",
    "74 5f 63 6f 75 6e 74 00 b1 77 6f 72 6c 64 5f 6d 6f 74 68 65 72 5f"
    "73 65 65 64 c2 b8 6f 70 65 6e 5f 63 6f 75 72 73 65 5f 73 65 6c 65"
    "63 74 5f 61 72 72 61 79 92 00 00 b7 6f 70 65 6e 5f 62 61 64 67 65"
    "5f 73 65 6c 65 63 74 5f 61 72 72 61 79 92 00 00 a5 65 6d 6f 74 65"
    "84 a6 70 69 63 74 5f 30 00 a6 70 69 63 74 5f 31 00 a6 70 69 63 74"
    "5f 32 00 a6 70 69 63 74 5f 33 00 aa 63 74 72 6c 5f 67",
    "75 69 64 65 85 aa 6f 70 65 6e 5f 63 6f 75 6e 74 00 a9 6c 61 73 74"
    "5f 70 61 67 65 ff ac 70 61 67 65 5f 66 72 61 6d 65 5f 30 00 ac 70"
    "61 67 65 5f 66 72 61 6d 65 5f 31 00 ac 70 61 67 65 5f 66 72 61 6d"
    "65 5f 32 00 ac 6f 6e 6c 69 6e 65 5f 67 75 69 64 65 8e aa 6f 70 65"
    "6e 5f 63 6f 75 6e 74 00 ae 63 75 72 5f 66 69 72 73 74 5f 70 61 67"
    "65 ff ac 70 61 67 65 5f 66 72 61 6d 65 5f 30 00 ac 70",
    "61 67 65 5f 66 72 61 6d 65 5f 31 00 ac 70 61 67 65 5f 66 72 61 6d"
    "65 5f 32 00 ac 70 61 67 65 5f 66 72 61 6d 65 5f 33 00 ac 70 61 67"
    "65 5f 66 72 61 6d 65 5f 34 00 ac 70 61 67 65 5f 66 72 61 6d 65 5f"
    "35 00 ac 70 61 67 65 5f 66 72 61 6d 65 5f 36 00 ac 70 61 67 65 5f"
    "66 72 61 6d 65 5f 37 00 ac 70 61 67 65 5f 66 72 61 6d 65 5f 38 00"
    "ac 70 61 67 65 5f 66 72 61 6d 65 5f 39 00 ad 70 61 67",
    "65 5f 66 72 61 6d 65 5f 31 30 00 ad 70 61 67 65 5f 66 72 61 6d 65"
    "5f 31 31 00 a8 6e 65 74 5f 63 6f 6e 6e 86 ac 63 68 61 6e 67 65 5f"
    "62 79 5f 6d 6d c2 ac 63 68 61 6e 67 65 5f 62 79 5f 63 74 c2 ac 63"
    "6f 6e 6e 5f 73 65 74 74 69 6e 67 c2 ac 6d 61 74 63 68 5f 6d 61 6b"
    "69 6e 67 c2 aa 63 6f 6e 6e 65 63 74 69 6e 67 c2 ab 63 6f 6e 6e 5f"
    "72 65 73 75 6c 74 ff af 67 65 74 5f 6d 65 64 61 6c 5f",
    "61 72 72 61 79 96 c2 c2 c2 c2 c2 c2 b1 73 79 73 74 65 6d 5f 72 65"
    "70 6f 72 74 5f 74 61 67 ce 81 a7 03 b8",
)


class TestWorldResultPayload(unittest.TestCase):
    """Overworld→course transition. Carries `next_stage_info` with the
    destination course's stage_key — usable in the AP bridge to know which
    course is about to load."""

    def test_decodes_clean(self):
        self.assertEqual(len(WORLD_RESULT), 1059)
        r = decode_play_report(WORLD_RESULT)
        self.assertEqual(r.entry_count, 26)
        self.assertEqual(r.decoded_count, 26)
        self.assertIsNone(r.error)

    def test_next_stage_info_carries_destination_stage_key(self):
        r = decode_play_report(WORLD_RESULT)
        self.assertEqual(r.fields["next_stage_info"], {
            "stage_key": 2937190396,
            "stage_type": 1,
            "world_kind": 0,
            "world_no": 1,
            "course_id": 2,
        })

    def test_transition_info_has_neg_one(self):
        r = decode_play_report(WORLD_RESULT)
        self.assertEqual(r.fields["transition_info"], {
            "transition_type": -1,
            "worldmap_id": 0,
            "course_id": 0,
            "npc_id": 0,
        })

    def test_arrays_of_zeros(self):
        r = decode_play_report(WORLD_RESULT)
        self.assertEqual(r.fields["open_course_select_array"], [0, 0])
        self.assertEqual(r.fields["open_badge_select_array"], [0, 0])

    def test_get_medal_array_all_false(self):
        r = decode_play_report(WORLD_RESULT)
        self.assertEqual(r.fields["get_medal_array"], [False] * 6)

    def test_nested_struct_with_14_fields(self):
        # online_guide has 14 sub-fields — uses opener 0x8E (= 0x80+14).
        r = decode_play_report(WORLD_RESULT)
        og = r.fields["online_guide"]
        self.assertEqual(len(og), 14)
        self.assertEqual(og["open_count"], 0)
        self.assertEqual(og["cur_first_page"], -1)
        self.assertEqual(og["page_frame_11"], 0)

    def test_total_play_time_sec_inline_26(self):
        # 0x1A would be a u32 marker in CBOR but Nintendo uses it as
        # inline uint 26.
        r = decode_play_report(WORLD_RESULT)
        self.assertEqual(r.fields["total_play_time_sec"], 26)


# course_result (1577 bytes, 57 fields) — the M2.4 holy grail. Fires once
# ~8 ms after the M1 COURSE_CLEARED nerve. Contains stage_info.stage_key
# (uniquely identifies WHICH course was just cleared) and a wealth of
# clear-state metadata.
#
# IMPORTANT: this capture is a "Wonder-Seed-collected" clear — the player
# touched the Wonder Flower and finished the Wonder Phase before reaching
# the goal flag. That's why total_get_finish_seed_count == 1 (and why the
# corresponding M1 WONDER_SEED_AWARDED nerve also fired during this run,
# though that fires elsewhere in the log).  A future "no-Wonder-Seed
# clear" capture would assert total_get_finish_seed_count == 0 and likely
# diff in big_flower_coin_* counts; useful contrast fixture to add.
COURSE_RESULT = _hex(
    "de 00 39 ab 73 61 76 65 64 61 74 61 5f 69 64 d9 23 62 38 31 33 65"
    "36 37 35 2d 65 62 32 35 34 63 38 61 2d 61 33 65 30 64 30 35 32 2d"
    "64 66 31 61 66 61 64 30 a9 70 6c 61 79 5f 6d 6f 64 65 01 af 74 6f"
    "74 61 6c 5f 70 6c 61 79 5f 74 69 6d 65 d7 00 00 00 00 00 00 00 00"
    "34 aa 73 74 61 67 65 5f 69 6e 66 6f 84 a9 73 74 61 67 65 5f 6b 65"
    "79 d3 00 00 00 00 af 11 f7 fc aa 77 6f 72 6c 64 5f 6b",
    "69 6e 64 00 a8 77 6f 72 6c 64 5f 6e 6f 01 a9 63 6f 75 72 73 65 5f"
    "6e 6f 02 ad 63 6f 75 72 73 65 5f 69 6e 5f 75 74 63 d7 00 00 00 00"
    "00 6a 0e 5d 1f b3 74 6f 74 61 6c 5f 70 6c 61 79 5f 74 69 6d 65 5f"
    "73 65 63 6c b5 63 75 72 72 65 6e 74 5f 70 6c 61 79 5f 74 69 6d 65"
    "5f 73 65 63 6c ad 63 6f 75 72 73 65 5f 72 65 73 75 6c 74 01 b0 68"
    "61 6e 61 5f 72 61 63 65 5f 72 65 73 75 6c 74 00 a7 67",
    "6f 61 6c 5f 69 64 00 b6 72 65 6d 6f 74 65 5f 65 6e 63 6f 75 6e 74"
    "65 72 5f 63 6f 75 6e 74 00 b5 67 68 6f 73 74 5f 65 6e 63 6f 75 6e"
    "74 65 72 5f 63 6f 75 6e 74 00 af 67 65 74 5f 79 65 6c 6c 6f 77 5f"
    "63 6f 69 6e 13 ae 67 65 74 5f 6c 75 63 6b 79 5f 63 6f 69 6e 00 b5"
    "79 65 6c 6c 6f 77 5f 63 6f 69 6e 5f 63 6f 75 72 73 65 5f 69 6e 2a"
    "b6 79 65 6c 6c 6f 77 5f 63 6f 69 6e 5f 63 6f 75 72 73",
    "65 5f 6f 75 74 3d b5 66 6c 6f 77 65 72 5f 63 6f 69 6e 5f 63 6f 75"
    "72 73 65 5f 69 6e cc 87 b6 66 6c 6f 77 65 72 5f 63 6f 69 6e 5f 63"
    "6f 75 72 73 65 5f 6f 75 74 cc 87 b9 62 69 67 5f 66 6c 6f 77 65 72"
    "5f 63 6f 69 6e 5f 63 6f 75 72 73 65 5f 69 6e 93 c3 c3 c3 ba 62 69"
    "67 5f 66 6c 6f 77 65 72 5f 63 6f 69 6e 5f 63 6f 75 72 73 65 5f 6f"
    "75 74 93 c3 c3 c3 b1 61 72 65 6e 61 5f 73 63 6f 72 65",
    "5f 65 6e 74 65 72 ce ff ff ff ff b2 61 72 65 6e 61 5f 73 63 6f 72"
    "65 5f 72 65 73 75 6c 74 ce ff ff ff ff b4 74 6f 75 63 68 5f 67 6f"
    "61 6c 5f 74 6f 70 5f 65 6e 74 65 72 c3 b5 74 6f 75 63 68 5f 67 6f"
    "61 6c 5f 74 6f 70 5f 72 65 73 75 6c 74 c3 b0 6e 65 77 5f 66 6c 6f"
    "77 65 72 5f 63 6f 75 6e 74 00 b0 67 65 74 5f 66 6c 6f 77 65 72 5f"
    "63 6f 75 6e 74 02 b3 77 6f 72 6c 64 5f 77 6f 6e 64 65",
    "72 5f 66 6c 6f 77 65 72 0e b1 77 6f 72 6c 64 5f 6d 6f 74 68 65 72"
    "5f 73 65 65 64 c2 b1 6c 61 73 74 5f 70 75 74 5f 70 61 6e 65 6c 5f"
    "69 64 ff a9 73 74 61 72 74 5f 6d 6d 70 00 aa 72 65 73 75 6c 74 5f"
    "6d 6d 70 00 b2 66 72 69 65 6e 64 5f 72 61 63 65 5f 6d 65 6d 62 65"
    "72 00 b2 66 72 69 65 6e 64 5f 72 61 63 65 5f 72 65 73 75 6c 74 00"
    "b1 72 6f 6f 6d 5f 6d 65 6d 62 65 72 5f 65 6e 74 65 72",
    "00 af 72 6f 6f 6d 5f 6d 65 6d 62 65 72 5f 6d 61 78 00 b2 6c 61 73"
    "74 5f 63 74 72 6c 5f 62 79 5f 73 74 63 69 6b c3 a6 72 65 73 63 75"
    "65 8a b4 72 65 73 63 75 65 5f 72 65 6d 6f 74 65 5f 64 69 72 65 63"
    "74 00 b1 72 65 73 63 75 65 5f 72 65 6d 6f 74 65 5f 6b 6b 73 00 b5"
    "72 65 73 63 75 65 64 5f 72 65 6d 6f 74 65 5f 64 69 72 65 63 74 00"
    "b2 72 65 73 63 75 65 64 5f 72 65 6d 6f 74 65 5f 6b 6b",
    "73 00 b5 72 65 73 63 75 65 64 5f 72 65 6d 6f 74 65 5f 75 6b 5f 6b"
    "6b 73 00 b4 72 65 73 63 75 65 64 5f 67 68 6f 73 74 5f 64 69 72 65"
    "63 74 00 b4 72 65 73 63 75 65 64 5f 6c 6f 63 61 6c 5f 64 69 72 65"
    "63 74 00 b1 72 65 73 63 75 65 64 5f 6c 6f 63 61 6c 5f 6b 6b 73 00"
    "b0 72 65 73 63 75 65 64 5f 73 65 6c 66 5f 6b 6b 73 00 ad 73 65 74"
    "5f 6c 6f 63 61 6c 5f 6b 6b 73 00 a8 69 74 65 6d 5f 62",
    "6c 6e 85 a8 73 65 74 5f 6c 62 6c 6e 00 ad 67 65 74 5f 73 65 6c 66"
    "5f 6c 62 6c 6e 00 ae 67 65 74 5f 6f 74 68 65 72 5f 6c 62 6c 6e 00"
    "a8 67 65 74 5f 72 62 6c 6e 00 af 67 65 74 5f 6c 62 6c 6e 5f 62 79"
    "5f 72 6d 74 00 a5 65 6d 6f 74 65 84 a6 70 69 63 74 5f 30 00 a6 70"
    "69 63 74 5f 31 00 a6 70 69 63 74 5f 32 00 a6 70 69 63 74 5f 33 00"
    "aa 63 74 72 6c 5f 67 75 69 64 65 85 aa 6f 70 65 6e 5f",
    "63 6f 75 6e 74 00 a9 6c 61 73 74 5f 70 61 67 65 ff ac 70 61 67 65"
    "5f 66 72 61 6d 65 5f 30 00 ac 70 61 67 65 5f 66 72 61 6d 65 5f 31"
    "00 ac 70 61 67 65 5f 66 72 61 6d 65 5f 32 00 af 63 68 61 6c 6c 65"
    "6e 67 65 5f 63 6f 75 6e 74 01 b2 74 6f 74 61 6c 5f 77 6f 6e 64 65"
    "72 5f 63 6f 75 6e 74 01 b0 6d 61 78 5f 77 6f 6e 64 65 72 5f 63 6f"
    "75 6e 74 01 bb 74 6f 74 61 6c 5f 67 65 74 5f 66 69 6e",
    "69 73 68 5f 73 65 65 64 5f 63 6f 75 6e 74 01 a8 6e 65 74 5f 6d 6f"
    "64 65 c2 ae 62 61 64 67 65 5f 69 64 5f 61 72 72 61 79 91 22 b5 70"
    "6c 61 79 65 72 5f 72 65 73 74 5f 63 6f 75 72 73 65 5f 69 6e 04 b6"
    "70 6c 61 79 65 72 5f 72 65 73 74 5f 63 6f 75 72 73 65 5f 6f 75 74"
    "05 af 74 6f 74 61 6c 5f 31 75 70 5f 63 6f 75 6e 74 01 b0 6c 6f 63"
    "61 6c 5f 70 6c 61 79 65 72 5f 6e 75 6d 01 b0 63 74 72",
    "6c 5f 73 74 79 6c 65 5f 61 72 72 61 79 94 00 05 05 05 b0 63 68 61"
    "72 61 5f 74 79 70 65 5f 61 72 72 61 79 91 03 b7 73 65 6c 66 5f 73"
    "68 61 62 6f 6e 5f 63 6f 75 6e 74 5f 61 72 72 61 79 94 00 00 00 00"
    "b7 6d 69 73 73 5f 73 68 61 62 6f 6e 5f 63 6f 75 6e 74 5f 61 72 72"
    "61 79 94 00 00 00 00 b0 64 65 61 64 5f 63 6f 75 6e 74 5f 61 72 72"
    "61 79 94 00 00 00 00 b7 64 69 72 65 63 74 5f 64 65 61",
    "64 5f 63 6f 75 6e 74 5f 61 72 72 61 79 94 00 00 00 00 b1 73 79 73"
    "74 65 6d 5f 72 65 70 6f 72 74 5f 74 61 67 ce 81 a7 03 b8",
)


class TestCourseResultPayload(unittest.TestCase):
    """The M2.4 holy-grail event: per-course-clear report. Carries
    stage_info.stage_key (course identity) plus the full clear-state
    inventory. Fires ~8 ms after the M1 COURSE_CLEARED nerve."""

    def test_decodes_clean(self):
        self.assertEqual(len(COURSE_RESULT), 1577)
        r = decode_play_report(COURSE_RESULT)
        self.assertEqual(r.entry_count, 57)
        self.assertEqual(r.decoded_count, 57)
        self.assertIsNone(r.error)

    def test_stage_info_identifies_w1_1(self):
        # The whole point of M2.4. W1-1 Welcome to the Flower Kingdom:
        # stage_key 2937190396, world_no 1, course_no 2.
        r = decode_play_report(COURSE_RESULT)
        self.assertEqual(r.fields["stage_info"], {
            "stage_key": 2937190396,
            "world_kind": 0,
            "world_no": 1,
            "course_no": 2,
        })

    def test_course_result_field_indicates_clear(self):
        # course_result: 1 = cleared (vs 0 = quit?)
        r = decode_play_report(COURSE_RESULT)
        self.assertEqual(r.fields["course_result"], 1)

    def test_touch_goal_top_flags(self):
        # We touched the top of the flagpole in this run.
        r = decode_play_report(COURSE_RESULT)
        self.assertEqual(r.fields["touch_goal_top_enter"], True)
        self.assertEqual(r.fields["touch_goal_top_result"], True)

    def test_badge_id_array(self):
        # The badge we had equipped: id 34 (single-element array).
        r = decode_play_report(COURSE_RESULT)
        self.assertEqual(r.fields["badge_id_array"], [34])

    def test_big_flower_coin_arrays_all_true(self):
        # All 3 wonder-flower coins collected during this clear.
        r = decode_play_report(COURSE_RESULT)
        self.assertEqual(
            r.fields["big_flower_coin_course_in"], [True, True, True])
        self.assertEqual(
            r.fields["big_flower_coin_course_out"], [True, True, True])

    def test_arena_score_u32_max(self):
        # 0xFFFFFFFF as a u32 (encoded `ce ff ff ff ff`), not as -1.
        # This tells us about encoder behavior: unsigned types stay
        # unsigned even at 0xFFFFFFFF, while signed -1 uses the 0xFF
        # short-form.
        r = decode_play_report(COURSE_RESULT)
        self.assertEqual(r.fields["arena_score_enter"], 4294967295)
        self.assertEqual(r.fields["arena_score_result"], 4294967295)

    def test_ctrl_style_array_mixed_values(self):
        # [0, 5, 5, 5] — first slot inline 0, rest inline 5.
        r = decode_play_report(COURSE_RESULT)
        self.assertEqual(r.fields["ctrl_style_array"], [0, 5, 5, 5])

    def test_nested_rescue_struct(self):
        # 10-field nested struct (opener 0x8A = 0x80+10), all zeros.
        r = decode_play_report(COURSE_RESULT)
        rescue = r.fields["rescue"]
        self.assertEqual(len(rescue), 10)
        self.assertTrue(all(v == 0 for v in rescue.values()))

    def test_total_get_finish_seed_count(self):
        # The course's Wonder Seed was collected during this playthrough
        # (Wonder Flower touched, Wonder Phase completed, seed grabbed).
        # M1's WONDER_SEED_AWARDED nerve fires mid-course for the same event;
        # this field is the end-of-course confirmation.  A clear without
        # touching the Wonder Flower would have this == 0.
        r = decode_play_report(COURSE_RESULT)
        self.assertEqual(r.fields["total_get_finish_seed_count"], 1)


# course_in for W1-2 Piranha Plants on Parade (351 bytes, 15 fields).
# Captured on entering the course.  Importantly exercises the 0xD2 (signed
# s32) opcode for stage_info.stage_key — different from the W1-1 fixtures
# which all use 0xD3 (s64).  Confirms encoder picks smallest signed width.
W1_2_COURSE_IN = _hex(
    "de 00 0f ab 73 61 76 65 64 61 74 61 5f 69 64 d9 23 62 38 31 33 65"
    "36 37 35 2d 65 62 32 35 34 63 38 61 2d 61 33 65 30 64 30 35 32 2d"
    "64 66 31 61 66 61 64 30 a9 70 6c 61 79 5f 6d 6f 64 65 01 af 74 6f"
    "74 61 6c 5f 70 6c 61 79 5f 74 69 6d 65 d7 00 00 00 00 00 00 00 00"
    "35 aa 73 74 61 67 65 5f 69 6e 66 6f 84 a9 73 74 61 67 65 5f 6b 65"
    "79 d2 0d d6 7b 0b aa 77 6f 72 6c 64 5f 6b 69 6e 64 00",
    "a8 77 6f 72 6c 64 5f 6e 6f 01 a9 63 6f 75 72 73 65 5f 6e 6f 03 ad"
    "63 6f 75 72 73 65 5f 69 6e 5f 75 74 63 d7 00 00 00 00 00 6a 0e 61"
    "69 b1 6c 6f 63 61 6c 5f 70 6c 61 79 65 72 5f 72 65 73 74 05 aa 6c"
    "75 63 6b 79 5f 63 6f 69 6e cc 87 b3 77 6f 72 6c 64 5f 77 6f 6e 64"
    "65 72 5f 66 6c 6f 77 65 72 0e a8 6e 65 74 5f 6d 6f 64 65 c2 ae 72"
    "65 63 6f 6d 5f 62 61 64 67 65 5f 69 64 ff b2 72 65 63",
    "6f 6d 5f 62 61 64 67 65 5f 72 65 73 75 6c 74 00 b2 70 72 65 5f 72"
    "65 63 6f 6d 5f 62 61 64 67 65 5f 69 64 91 ff ae 65 71 75 69 70 5f"
    "62 61 64 67 65 5f 69 64 91 22 b0 6c 6f 63 61 6c 5f 70 6c 61 79 65"
    "72 5f 6e 75 6d 00 b1 73 79 73 74 65 6d 5f 72 65 70 6f 72 74 5f 74"
    "61 67 ce 81 a7 03 b8",
)


class TestW1_2CourseInPayload(unittest.TestCase):
    """course_in for W1-2 Piranha Plants on Parade.  Different stage_key
    encoding than W1-1 — uses 0xD2 (s32) instead of 0xD3 (s64), because
    232160011 fits in positive s32 while W1-1's 2937190396 does not."""

    def test_decodes_clean(self):
        self.assertEqual(len(W1_2_COURSE_IN), 351)
        r = decode_play_report(W1_2_COURSE_IN)
        self.assertEqual(r.entry_count, 15)
        self.assertEqual(r.decoded_count, 15)
        self.assertIsNone(r.error)

    def test_stage_info_identifies_w1_2(self):
        r = decode_play_report(W1_2_COURSE_IN)
        self.assertEqual(r.fields["stage_info"], {
            "stage_key": 232160011,
            "world_kind": 0,
            "world_no": 1,
            "course_no": 3,
        })

    def test_state_fields_match_ryujinx_reference(self):
        r = decode_play_report(W1_2_COURSE_IN)
        self.assertEqual(r.fields["play_mode"], 1)
        self.assertEqual(r.fields["local_player_rest"], 5)
        self.assertEqual(r.fields["lucky_coin"], 135)
        self.assertEqual(r.fields["world_wonder_flower"], 14)
        self.assertEqual(r.fields["net_mode"], False)
        self.assertEqual(r.fields["recom_badge_id"], -1)
        self.assertEqual(r.fields["pre_recom_badge_id"], [-1])
        self.assertEqual(r.fields["equip_badge_id"], [34])
        self.assertEqual(r.fields["system_report_tag"], 2175206328)

    def test_any64bitid_course_in_utc(self):
        r = decode_play_report(W1_2_COURSE_IN)
        self.assertEqual(r.fields["course_in_utc"],
                         {"TypeCode": 0, "Value": 0x6A0E6169})


# course_result for W1-2 Piranha Plants on Parade — SECRET EXIT clear
# (1575 bytes, 57 fields).  Side-by-side with the W1-1 COURSE_RESULT
# fixture this nails down the M2.5 exit-type distinguisher: same shape,
# same field set, only `goal_id` differs (0 vs 1).
W1_2_COURSE_RESULT_SECRET = _hex(
    "de 00 39 ab 73 61 76 65 64 61 74 61 5f 69 64 d9 23 62 38 31 33 65"
    "36 37 35 2d 65 62 32 35 34 63 38 61 2d 61 33 65 30 64 30 35 32 2d"
    "64 66 31 61 66 61 64 30 a9 70 6c 61 79 5f 6d 6f 64 65 01 af 74 6f"
    "74 61 6c 5f 70 6c 61 79 5f 74 69 6d 65 d7 00 00 00 00 00 00 00 00"
    "39 aa 73 74 61 67 65 5f 69 6e 66 6f 84 a9 73 74 61 67 65 5f 6b 65"
    "79 d2 0d d6 7b 0b aa 77 6f 72 6c 64 5f 6b 69 6e 64 00",
    "a8 77 6f 72 6c 64 5f 6e 6f 01 a9 63 6f 75 72 73 65 5f 6e 6f 03 ad"
    "63 6f 75 72 73 65 5f 69 6e 5f 75 74 63 d7 00 00 00 00 00 6a 0e 61"
    "69 b3 74 6f 74 61 6c 5f 70 6c 61 79 5f 74 69 6d 65 5f 73 65 63 cc"
    "d8 b5 63 75 72 72 65 6e 74 5f 70 6c 61 79 5f 74 69 6d 65 5f 73 65"
    "63 cc d8 ad 63 6f 75 72 73 65 5f 72 65 73 75 6c 74 01 b0 68 61 6e"
    "61 5f 72 61 63 65 5f 72 65 73 75 6c 74 00 a7 67 6f 61",
    "6c 5f 69 64 01 b6 72 65 6d 6f 74 65 5f 65 6e 63 6f 75 6e 74 65 72"
    "5f 63 6f 75 6e 74 00 b5 67 68 6f 73 74 5f 65 6e 63 6f 75 6e 74 65"
    "72 5f 63 6f 75 6e 74 00 af 67 65 74 5f 79 65 6c 6c 6f 77 5f 63 6f"
    "69 6e 22 ae 67 65 74 5f 6c 75 63 6b 79 5f 63 6f 69 6e 0b b5 79 65"
    "6c 6c 6f 77 5f 63 6f 69 6e 5f 63 6f 75 72 73 65 5f 69 6e 3d b6 79"
    "65 6c 6c 6f 77 5f 63 6f 69 6e 5f 63 6f 75 72 73 65 5f",
    "6f 75 74 5f b5 66 6c 6f 77 65 72 5f 63 6f 69 6e 5f 63 6f 75 72 73"
    "65 5f 69 6e cc 87 b6 66 6c 6f 77 65 72 5f 63 6f 69 6e 5f 63 6f 75"
    "72 73 65 5f 6f 75 74 cc 92 b9 62 69 67 5f 66 6c 6f 77 65 72 5f 63"
    "6f 69 6e 5f 63 6f 75 72 73 65 5f 69 6e 93 c2 c3 c3 ba 62 69 67 5f"
    "66 6c 6f 77 65 72 5f 63 6f 69 6e 5f 63 6f 75 72 73 65 5f 6f 75 74"
    "93 c2 c3 c3 b1 61 72 65 6e 61 5f 73 63 6f 72 65 5f 65",
    "6e 74 65 72 ce ff ff ff ff b2 61 72 65 6e 61 5f 73 63 6f 72 65 5f"
    "72 65 73 75 6c 74 ce ff ff ff ff b4 74 6f 75 63 68 5f 67 6f 61 6c"
    "5f 74 6f 70 5f 65 6e 74 65 72 c3 b5 74 6f 75 63 68 5f 67 6f 61 6c"
    "5f 74 6f 70 5f 72 65 73 75 6c 74 c3 b0 6e 65 77 5f 66 6c 6f 77 65"
    "72 5f 63 6f 75 6e 74 01 b0 67 65 74 5f 66 6c 6f 77 65 72 5f 63 6f"
    "75 6e 74 03 b3 77 6f 72 6c 64 5f 77 6f 6e 64 65 72 5f",
    "66 6c 6f 77 65 72 0f b1 77 6f 72 6c 64 5f 6d 6f 74 68 65 72 5f 73"
    "65 65 64 c2 b1 6c 61 73 74 5f 70 75 74 5f 70 61 6e 65 6c 5f 69 64"
    "ff a9 73 74 61 72 74 5f 6d 6d 70 00 aa 72 65 73 75 6c 74 5f 6d 6d"
    "70 00 b2 66 72 69 65 6e 64 5f 72 61 63 65 5f 6d 65 6d 62 65 72 00"
    "b2 66 72 69 65 6e 64 5f 72 61 63 65 5f 72 65 73 75 6c 74 00 b1 72"
    "6f 6f 6d 5f 6d 65 6d 62 65 72 5f 65 6e 74 65 72 00 af",
    "72 6f 6f 6d 5f 6d 65 6d 62 65 72 5f 6d 61 78 00 b2 6c 61 73 74 5f"
    "63 74 72 6c 5f 62 79 5f 73 74 63 69 6b c3 a6 72 65 73 63 75 65 8a"
    "b4 72 65 73 63 75 65 5f 72 65 6d 6f 74 65 5f 64 69 72 65 63 74 00"
    "b1 72 65 73 63 75 65 5f 72 65 6d 6f 74 65 5f 6b 6b 73 00 b5 72 65"
    "73 63 75 65 64 5f 72 65 6d 6f 74 65 5f 64 69 72 65 63 74 00 b2 72"
    "65 73 63 75 65 64 5f 72 65 6d 6f 74 65 5f 6b 6b 73 00",
    "b5 72 65 73 63 75 65 64 5f 72 65 6d 6f 74 65 5f 75 6b 5f 6b 6b 73"
    "00 b4 72 65 73 63 75 65 64 5f 67 68 6f 73 74 5f 64 69 72 65 63 74"
    "00 b4 72 65 73 63 75 65 64 5f 6c 6f 63 61 6c 5f 64 69 72 65 63 74"
    "00 b1 72 65 73 63 75 65 64 5f 6c 6f 63 61 6c 5f 6b 6b 73 00 b0 72"
    "65 73 63 75 65 64 5f 73 65 6c 66 5f 6b 6b 73 00 ad 73 65 74 5f 6c"
    "6f 63 61 6c 5f 6b 6b 73 00 a8 69 74 65 6d 5f 62 6c 6e",
    "85 a8 73 65 74 5f 6c 62 6c 6e 00 ad 67 65 74 5f 73 65 6c 66 5f 6c"
    "62 6c 6e 00 ae 67 65 74 5f 6f 74 68 65 72 5f 6c 62 6c 6e 00 a8 67"
    "65 74 5f 72 62 6c 6e 00 af 67 65 74 5f 6c 62 6c 6e 5f 62 79 5f 72"
    "6d 74 00 a5 65 6d 6f 74 65 84 a6 70 69 63 74 5f 30 00 a6 70 69 63"
    "74 5f 31 00 a6 70 69 63 74 5f 32 00 a6 70 69 63 74 5f 33 00 aa 63"
    "74 72 6c 5f 67 75 69 64 65 85 aa 6f 70 65 6e 5f 63 6f",
    "75 6e 74 00 a9 6c 61 73 74 5f 70 61 67 65 ff ac 70 61 67 65 5f 66"
    "72 61 6d 65 5f 30 00 ac 70 61 67 65 5f 66 72 61 6d 65 5f 31 00 ac"
    "70 61 67 65 5f 66 72 61 6d 65 5f 32 00 af 63 68 61 6c 6c 65 6e 67"
    "65 5f 63 6f 75 6e 74 01 b2 74 6f 74 61 6c 5f 77 6f 6e 64 65 72 5f"
    "63 6f 75 6e 74 01 b0 6d 61 78 5f 77 6f 6e 64 65 72 5f 63 6f 75 6e"
    "74 01 bb 74 6f 74 61 6c 5f 67 65 74 5f 66 69 6e 69 73",
    "68 5f 73 65 65 64 5f 63 6f 75 6e 74 01 a8 6e 65 74 5f 6d 6f 64 65"
    "c2 ae 62 61 64 67 65 5f 69 64 5f 61 72 72 61 79 91 22 b5 70 6c 61"
    "79 65 72 5f 72 65 73 74 5f 63 6f 75 72 73 65 5f 69 6e 05 b6 70 6c"
    "61 79 65 72 5f 72 65 73 74 5f 63 6f 75 72 73 65 5f 6f 75 74 05 af"
    "74 6f 74 61 6c 5f 31 75 70 5f 63 6f 75 6e 74 00 b0 6c 6f 63 61 6c"
    "5f 70 6c 61 79 65 72 5f 6e 75 6d 01 b0 63 74 72 6c 5f",
    "73 74 79 6c 65 5f 61 72 72 61 79 94 00 05 05 05 b0 63 68 61 72 61"
    "5f 74 79 70 65 5f 61 72 72 61 79 91 01 b7 73 65 6c 66 5f 73 68 61"
    "62 6f 6e 5f 63 6f 75 6e 74 5f 61 72 72 61 79 94 00 00 00 00 b7 6d"
    "69 73 73 5f 73 68 61 62 6f 6e 5f 63 6f 75 6e 74 5f 61 72 72 61 79"
    "94 00 00 00 00 b0 64 65 61 64 5f 63 6f 75 6e 74 5f 61 72 72 61 79"
    "94 00 00 00 00 b7 64 69 72 65 63 74 5f 64 65 61 64 5f",
    "63 6f 75 6e 74 5f 61 72 72 61 79 94 00 00 00 00 b1 73 79 73 74 65"
    "6d 5f 72 65 70 6f 72 74 5f 74 61 67 ce 81 a7 03 b8",
)


class TestW1_2SecretExitCourseResult(unittest.TestCase):
    """Per-course-clear report from W1-2 Piranha Plants on Parade,
    SECRET EXIT path.  Compare against COURSE_RESULT (W1-1 normal Top
    of Flag clear) — they're structurally identical except for the
    field values, most importantly `goal_id` (1 vs 0).  Together they
    pin down the M2.5 exit-type distinguisher empirically."""

    def test_decodes_clean(self):
        self.assertEqual(len(W1_2_COURSE_RESULT_SECRET), 1575)
        r = decode_play_report(W1_2_COURSE_RESULT_SECRET)
        self.assertEqual(r.entry_count, 57)
        self.assertEqual(r.decoded_count, 57)
        self.assertIsNone(r.error)

    def test_goal_id_is_one_for_secret_exit(self):
        """The headline M2.5 finding."""
        r = decode_play_report(W1_2_COURSE_RESULT_SECRET)
        self.assertEqual(r.fields["goal_id"], 1)
        # touch_goal_top_* is True too — incidental, the secret-exit
        # goal pole was top-touched. That makes it independent of
        # goal_id; only goal_id discriminates exit *type*.
        self.assertEqual(r.fields["touch_goal_top_result"], True)

    def test_stage_info_w1_2(self):
        # Same stage_key as TestW1_2CourseInPayload — confirms the same
        # course is identified at entry and clear.
        r = decode_play_report(W1_2_COURSE_RESULT_SECRET)
        self.assertEqual(r.fields["stage_info"], {
            "stage_key": 232160011,
            "world_kind": 0,
            "world_no": 1,
            "course_no": 3,
        })

    def test_total_play_time_sec_above_inline_range(self):
        # 216 doesn't fit in inline (>127) — encoder uses 0xCC + u8.
        # First live exercise of the cc-with-value-above-127 path.
        r = decode_play_report(W1_2_COURSE_RESULT_SECRET)
        self.assertEqual(r.fields["total_play_time_sec"], 216)
        self.assertEqual(r.fields["current_play_time_sec"], 216)

    def test_wonder_seed_collected(self):
        # Same clear style as W1-1 — Wonder Phase completed.
        r = decode_play_report(W1_2_COURSE_RESULT_SECRET)
        self.assertEqual(r.fields["total_get_finish_seed_count"], 1)

    def test_world_wonder_flower_incremented(self):
        # Player had 14 entering W1-2, 15 after this clear (got 1 more
        # world wonder flower).  Tracks the world-wide Wonder Seed count.
        r = decode_play_report(W1_2_COURSE_RESULT_SECRET)
        self.assertEqual(r.fields["world_wonder_flower"], 15)

    def test_big_flower_coin_partial_collection(self):
        # Player got coins #2 and #3 but not #1.  Contrast with the
        # W1-1 capture which had [True, True, True].
        r = decode_play_report(W1_2_COURSE_RESULT_SECRET)
        self.assertEqual(
            r.fields["big_flower_coin_course_in"], [False, True, True])
        self.assertEqual(
            r.fields["big_flower_coin_course_out"], [False, True, True])


# koopajr_result for Pipe-Rock Plateau Palace LOSS — died to Bowser Jr in
# phase 2.  499 bytes, 16 fields.  Key structural novelty: koopajr_step_info
# is the first observed *array of structs* — already supported by the
# decoder via mutual recursion in _decode_value, but worth a fixture.
#
# Identification note: stage_key=2308078743 / course_no=30 is
# Pipe-Rock Plateau Palace.  This is the same key labelled "Bulrush
# Coming Through" in the first crash run's notes — that earlier label
# was wrong; the room name `koopajr_result` (palace boss) and the
# koopajr_* field set settle it.
KOOPAJR_RESULT_LOSS = _hex(
    "de 00 10 ab 73 61 76 65 64 61 74 61 5f 69 64 d9 23 62 38 31 33 65"
    "36 37 35 2d 65 62 32 35 34 63 38 61 2d 61 33 65 30 64 30 35 32 2d"
    "64 66 31 61 66 61 64 30 a9 70 6c 61 79 5f 6d 6f 64 65 01 af 74 6f"
    "74 61 6c 5f 70 6c 61 79 5f 74 69 6d 65 d7 00 00 00 00 00 00 00 00"
    "3f aa 73 74 61 67 65 5f 69 6e 66 6f 84 a9 73 74 61 67 65 5f 6b 65"
    "79 d3 00 00 00 00 89 92 7c 97 aa 77 6f 72 6c 64 5f 6b",
    "69 6e 64 00 a8 77 6f 72 6c 64 5f 6e 6f 01 a9 63 6f 75 72 73 65 5f"
    "6e 6f 1e ad 63 6f 75 72 73 65 5f 69 6e 5f 75 74 63 d7 00 00 00 00"
    "00 6a 0e 64 ae ad 62 61 74 74 6c 65 5f 72 65 73 75 6c 74 c2 b3 6b"
    "6f 6f 70 61 6a 72 5f 66 69 6e 61 6c 5f 73 74 61 67 65 02 b1 6b 6f"
    "6f 70 61 6a 72 5f 73 74 65 70 5f 69 6e 66 6f 93 83 a4 73 74 65 70"
    "00 b4 70 6c 61 79 65 72 5f 64 61 6d 61 67 65 64 5f 63",
    "6f 75 6e 74 01 a4 74 69 6d 65 14 83 a4 73 74 65 70 01 b4 70 6c 61"
    "79 65 72 5f 64 61 6d 61 67 65 64 5f 63 6f 75 6e 74 00 a4 74 69 6d"
    "65 1c 83 a4 73 74 65 70 02 b4 70 6c 61 79 65 72 5f 64 61 6d 61 67"
    "65 64 5f 63 6f 75 6e 74 01 a4 74 69 6d 65 12 b2 6b 6f 6f 70 61 6a"
    "72 5f 74 6f 74 61 6c 5f 74 69 6d 65 43 b7 6b 6f 6f 70 61 6a 72 5f"
    "63 68 61 6c 6c 65 6e 67 65 5f 63 6f 75 6e 74 01 b3 6b",
    "6f 6f 70 61 6a 72 5f 70 6c 61 79 65 72 5f 72 65 73 74 05 b0 6c 6f"
    "63 61 6c 5f 70 6c 61 79 65 72 5f 6e 75 6d 01 b9 6b 6f 6f 70 61 6a"
    "72 5f 73 74 61 72 74 5f 70 6c 61 79 65 72 5f 6d 6f 64 65 91 01 ae"
    "62 61 64 67 65 5f 69 64 5f 61 72 72 61 79 91 22 a8 6e 65 74 5f 6d"
    "6f 64 65 c2 b1 73 79 73 74 65 6d 5f 72 65 70 6f 72 74 5f 74 61 67"
    "ce 81 a7 03 b8",
)


# course_result emitted ALONGSIDE koopajr_result for a palace WIN.  1579 bytes,
# 57 fields.  Pipe-Rock Plateau Palace, this time beaten on the 2nd try (the
# loss + retry from KOOPAJR_RESULT_LOSS).  Key new properties for the M2.5
# bridge logic:
#   - world_mother_seed = True (False for normal level clears)
#   - goal_id = 0 + touch_goal_top_result = False (would misclassify as
#     "Normal Exit" without the koopajr_result override)
#   - first live 0xCD + u16 encoding: total_play_time_sec = 266
PALACE_COURSE_RESULT = _hex(
    "de 00 39 ab 73 61 76 65 64 61 74 61 5f 69 64 d9 23 62 38 31 33 65"
    "36 37 35 2d 65 62 32 35 34 63 38 61 2d 61 33 65 30 64 30 35 32 2d"
    "64 66 31 61 66 61 64 30 a9 70 6c 61 79 5f 6d 6f 64 65 01 af 74 6f"
    "74 61 6c 5f 70 6c 61 79 5f 74 69 6d 65 d7 00 00 00 00 00 00 00 00"
    "41 aa 73 74 61 67 65 5f 69 6e 66 6f 84 a9 73 74 61 67 65 5f 6b 65"
    "79 d3 00 00 00 00 89 92 7c 97 aa 77 6f 72 6c 64 5f 6b",
    "69 6e 64 00 a8 77 6f 72 6c 64 5f 6e 6f 01 a9 63 6f 75 72 73 65 5f"
    "6e 6f 1e ad 63 6f 75 72 73 65 5f 69 6e 5f 75 74 63 d7 00 00 00 00"
    "00 6a 0e 64 ae b3 74 6f 74 61 6c 5f 70 6c 61 79 5f 74 69 6d 65 5f"
    "73 65 63 cd 01 0a b5 63 75 72 72 65 6e 74 5f 70 6c 61 79 5f 74 69"
    "6d 65 5f 73 65 63 5e ad 63 6f 75 72 73 65 5f 72 65 73 75 6c 74 01"
    "b0 68 61 6e 61 5f 72 61 63 65 5f 72 65 73 75 6c 74 00",
    "a7 67 6f 61 6c 5f 69 64 00 b6 72 65 6d 6f 74 65 5f 65 6e 63 6f 75"
    "6e 74 65 72 5f 63 6f 75 6e 74 00 b5 67 68 6f 73 74 5f 65 6e 63 6f"
    "75 6e 74 65 72 5f 63 6f 75 6e 74 00 af 67 65 74 5f 79 65 6c 6c 6f"
    "77 5f 63 6f 69 6e 1f ae 67 65 74 5f 6c 75 63 6b 79 5f 63 6f 69 6e"
    "02 b5 79 65 6c 6c 6f 77 5f 63 6f 69 6e 5f 63 6f 75 72 73 65 5f 69"
    "6e 5f b6 79 65 6c 6c 6f 77 5f 63 6f 69 6e 5f 63 6f 75",
    "72 73 65 5f 6f 75 74 1a b5 66 6c 6f 77 65 72 5f 63 6f 69 6e 5f 63"
    "6f 75 72 73 65 5f 69 6e cc 92 b6 66 6c 6f 77 65 72 5f 63 6f 69 6e"
    "5f 63 6f 75 72 73 65 5f 6f 75 74 cc 94 b9 62 69 67 5f 66 6c 6f 77"
    "65 72 5f 63 6f 69 6e 5f 63 6f 75 72 73 65 5f 69 6e 93 c2 c2 c2 ba"
    "62 69 67 5f 66 6c 6f 77 65 72 5f 63 6f 69 6e 5f 63 6f 75 72 73 65"
    "5f 6f 75 74 93 c2 c2 c2 b1 61 72 65 6e 61 5f 73 63 6f",
    "72 65 5f 65 6e 74 65 72 ce ff ff ff ff b2 61 72 65 6e 61 5f 73 63"
    "6f 72 65 5f 72 65 73 75 6c 74 ce ff ff ff ff b4 74 6f 75 63 68 5f"
    "67 6f 61 6c 5f 74 6f 70 5f 65 6e 74 65 72 c2 b5 74 6f 75 63 68 5f"
    "67 6f 61 6c 5f 74 6f 70 5f 72 65 73 75 6c 74 c2 b0 6e 65 77 5f 66"
    "6c 6f 77 65 72 5f 63 6f 75 6e 74 01 b0 67 65 74 5f 66 6c 6f 77 65"
    "72 5f 63 6f 75 6e 74 01 b3 77 6f 72 6c 64 5f 77 6f 6e",
    "64 65 72 5f 66 6c 6f 77 65 72 10 b1 77 6f 72 6c 64 5f 6d 6f 74 68"
    "65 72 5f 73 65 65 64 c3 b1 6c 61 73 74 5f 70 75 74 5f 70 61 6e 65"
    "6c 5f 69 64 ff a9 73 74 61 72 74 5f 6d 6d 70 00 aa 72 65 73 75 6c"
    "74 5f 6d 6d 70 00 b2 66 72 69 65 6e 64 5f 72 61 63 65 5f 6d 65 6d"
    "62 65 72 00 b2 66 72 69 65 6e 64 5f 72 61 63 65 5f 72 65 73 75 6c"
    "74 00 b1 72 6f 6f 6d 5f 6d 65 6d 62 65 72 5f 65 6e 74",
    "65 72 00 af 72 6f 6f 6d 5f 6d 65 6d 62 65 72 5f 6d 61 78 00 b2 6c"
    "61 73 74 5f 63 74 72 6c 5f 62 79 5f 73 74 63 69 6b c3 a6 72 65 73"
    "63 75 65 8a b4 72 65 73 63 75 65 5f 72 65 6d 6f 74 65 5f 64 69 72"
    "65 63 74 00 b1 72 65 73 63 75 65 5f 72 65 6d 6f 74 65 5f 6b 6b 73"
    "00 b5 72 65 73 63 75 65 64 5f 72 65 6d 6f 74 65 5f 64 69 72 65 63"
    "74 00 b2 72 65 73 63 75 65 64 5f 72 65 6d 6f 74 65 5f",
    "6b 6b 73 00 b5 72 65 73 63 75 65 64 5f 72 65 6d 6f 74 65 5f 75 6b"
    "5f 6b 6b 73 00 b4 72 65 73 63 75 65 64 5f 67 68 6f 73 74 5f 64 69"
    "72 65 63 74 00 b4 72 65 73 63 75 65 64 5f 6c 6f 63 61 6c 5f 64 69"
    "72 65 63 74 00 b1 72 65 73 63 75 65 64 5f 6c 6f 63 61 6c 5f 6b 6b"
    "73 00 b0 72 65 73 63 75 65 64 5f 73 65 6c 66 5f 6b 6b 73 00 ad 73"
    "65 74 5f 6c 6f 63 61 6c 5f 6b 6b 73 00 a8 69 74 65 6d",
    "5f 62 6c 6e 85 a8 73 65 74 5f 6c 62 6c 6e 00 ad 67 65 74 5f 73 65"
    "6c 66 5f 6c 62 6c 6e 00 ae 67 65 74 5f 6f 74 68 65 72 5f 6c 62 6c"
    "6e 00 a8 67 65 74 5f 72 62 6c 6e 00 af 67 65 74 5f 6c 62 6c 6e 5f"
    "62 79 5f 72 6d 74 00 a5 65 6d 6f 74 65 84 a6 70 69 63 74 5f 30 00"
    "a6 70 69 63 74 5f 31 00 a6 70 69 63 74 5f 32 00 a6 70 69 63 74 5f"
    "33 00 aa 63 74 72 6c 5f 67 75 69 64 65 85 aa 6f 70 65",
    "6e 5f 63 6f 75 6e 74 00 a9 6c 61 73 74 5f 70 61 67 65 ff ac 70 61"
    "67 65 5f 66 72 61 6d 65 5f 30 00 ac 70 61 67 65 5f 66 72 61 6d 65"
    "5f 31 00 ac 70 61 67 65 5f 66 72 61 6d 65 5f 32 00 af 63 68 61 6c"
    "6c 65 6e 67 65 5f 63 6f 75 6e 74 02 b2 74 6f 74 61 6c 5f 77 6f 6e"
    "64 65 72 5f 63 6f 75 6e 74 05 b0 6d 61 78 5f 77 6f 6e 64 65 72 5f"
    "63 6f 75 6e 74 03 bb 74 6f 74 61 6c 5f 67 65 74 5f 66",
    "69 6e 69 73 68 5f 73 65 65 64 5f 63 6f 75 6e 74 01 a8 6e 65 74 5f"
    "6d 6f 64 65 c2 ae 62 61 64 67 65 5f 69 64 5f 61 72 72 61 79 91 22"
    "b5 70 6c 61 79 65 72 5f 72 65 73 74 5f 63 6f 75 72 73 65 5f 69 6e"
    "05 b6 70 6c 61 79 65 72 5f 72 65 73 74 5f 63 6f 75 72 73 65 5f 6f"
    "75 74 05 af 74 6f 74 61 6c 5f 31 75 70 5f 63 6f 75 6e 74 01 b0 6c"
    "6f 63 61 6c 5f 70 6c 61 79 65 72 5f 6e 75 6d 01 b0 63",
    "74 72 6c 5f 73 74 79 6c 65 5f 61 72 72 61 79 94 00 05 05 05 b0 63"
    "68 61 72 61 5f 74 79 70 65 5f 61 72 72 61 79 91 06 b7 73 65 6c 66"
    "5f 73 68 61 62 6f 6e 5f 63 6f 75 6e 74 5f 61 72 72 61 79 94 00 00"
    "00 00 b7 6d 69 73 73 5f 73 68 61 62 6f 6e 5f 63 6f 75 6e 74 5f 61"
    "72 72 61 79 94 00 00 00 00 b0 64 65 61 64 5f 63 6f 75 6e 74 5f 61"
    "72 72 61 79 94 01 00 00 00 b7 64 69 72 65 63 74 5f 64",
    "65 61 64 5f 63 6f 75 6e 74 5f 61 72 72 61 79 94 01 00 00 00 b1 73"
    "79 73 74 65 6d 5f 72 65 70 6f 72 74 5f 74 61 67 ce 81 a7 03 b8",
)


# koopajr_result for Pipe-Rock Plateau Palace WIN (battle_result=True).
# Same shape as KOOPAJR_RESULT_LOSS, just different field values:
#   - battle_result True (0xC3) vs False (0xC2)
#   - all 3 step phases finished with 0 damage (the winning attempt)
#   - koopajr_total_time 84 (longer than the 67-sec loss attempt because
#     they actually had to defeat each phase rather than dying out)
#   - koopajr_challenge_count = 2 (2nd attempt — they lost once first)
KOOPAJR_RESULT_WIN = _hex(
    "de 00 10 ab 73 61 76 65 64 61 74 61 5f 69 64 d9 23 62 38 31 33 65"
    "36 37 35 2d 65 62 32 35 34 63 38 61 2d 61 33 65 30 64 30 35 32 2d"
    "64 66 31 61 66 61 64 30 a9 70 6c 61 79 5f 6d 6f 64 65 01 af 74 6f"
    "74 61 6c 5f 70 6c 61 79 5f 74 69 6d 65 d7 00 00 00 00 00 00 00 00"
    "41 aa 73 74 61 67 65 5f 69 6e 66 6f 84 a9 73 74 61 67 65 5f 6b 65"
    "79 d3 00 00 00 00 89 92 7c 97 aa 77 6f 72 6c 64 5f 6b",
    "69 6e 64 00 a8 77 6f 72 6c 64 5f 6e 6f 01 a9 63 6f 75 72 73 65 5f"
    "6e 6f 1e ad 63 6f 75 72 73 65 5f 69 6e 5f 75 74 63 d7 00 00 00 00"
    "00 6a 0e 64 ae ad 62 61 74 74 6c 65 5f 72 65 73 75 6c 74 c3 b3 6b"
    "6f 6f 70 61 6a 72 5f 66 69 6e 61 6c 5f 73 74 61 67 65 02 b1 6b 6f"
    "6f 70 61 6a 72 5f 73 74 65 70 5f 69 6e 66 6f 93 83 a4 73 74 65 70"
    "00 b4 70 6c 61 79 65 72 5f 64 61 6d 61 67 65 64 5f 63",
    "6f 75 6e 74 00 a4 74 69 6d 65 0f 83 a4 73 74 65 70 01 b4 70 6c 61"
    "79 65 72 5f 64 61 6d 61 67 65 64 5f 63 6f 75 6e 74 00 a4 74 69 6d"
    "65 1c 83 a4 73 74 65 70 02 b4 70 6c 61 79 65 72 5f 64 61 6d 61 67"
    "65 64 5f 63 6f 75 6e 74 00 a4 74 69 6d 65 29 b2 6b 6f 6f 70 61 6a"
    "72 5f 74 6f 74 61 6c 5f 74 69 6d 65 54 b7 6b 6f 6f 70 61 6a 72 5f"
    "63 68 61 6c 6c 65 6e 67 65 5f 63 6f 75 6e 74 02 b3 6b",
    "6f 6f 70 61 6a 72 5f 70 6c 61 79 65 72 5f 72 65 73 74 05 b0 6c 6f"
    "63 61 6c 5f 70 6c 61 79 65 72 5f 6e 75 6d 01 b9 6b 6f 6f 70 61 6a"
    "72 5f 73 74 61 72 74 5f 70 6c 61 79 65 72 5f 6d 6f 64 65 91 01 ae"
    "62 61 64 67 65 5f 69 64 5f 61 72 72 61 79 91 22 a8 6e 65 74 5f 6d"
    "6f 64 65 c2 b1 73 79 73 74 65 6d 5f 72 65 70 6f 72 74 5f 74 61 67"
    "ce 81 a7 03 b8",
)


class TestPalaceCourseResultPayload(unittest.TestCase):
    """The course_result that fires *alongside* the koopajr_result for a
    palace WIN.  The bridge needs to know this duplication exists so it
    doesn't double-fire AP checks (palace WIN must be classified by the
    koopajr_result, not by this course_result's goal_id/touch_goal_top)."""

    def test_decodes_clean(self):
        self.assertEqual(len(PALACE_COURSE_RESULT), 1579)
        r = decode_play_report(PALACE_COURSE_RESULT)
        self.assertEqual(r.entry_count, 57)
        self.assertEqual(r.decoded_count, 57)
        self.assertIsNone(r.error)

    def test_world_mother_seed_true_for_palace(self):
        # THE distinguishing flag if no koopajr_result correlation is
        # available.  False in all normal level clears we've captured.
        r = decode_play_report(PALACE_COURSE_RESULT)
        self.assertEqual(r.fields["world_mother_seed"], True)

    def test_palace_course_result_has_misleading_goal_id_and_touch_top(self):
        # IMPORTANT: by goal_id + touch_goal_top alone, the bridge would
        # misclassify this as "Normal Exit" — but it's a palace clear.
        # The koopajr_result event (or world_mother_seed flag) must
        # override.
        r = decode_play_report(PALACE_COURSE_RESULT)
        self.assertEqual(r.fields["goal_id"], 0)
        self.assertEqual(r.fields["touch_goal_top_enter"], False)
        self.assertEqual(r.fields["touch_goal_top_result"], False)

    def test_total_play_time_sec_uses_cd_u16(self):
        # 266 = 0x010A — first live exercise of the 0xCD + u16 opcode.
        # Confirms the GUESSED encoding was right.
        r = decode_play_report(PALACE_COURSE_RESULT)
        self.assertEqual(r.fields["total_play_time_sec"], 266)
        self.assertEqual(r.fields["current_play_time_sec"], 94)

    def test_stage_info_palace(self):
        r = decode_play_report(PALACE_COURSE_RESULT)
        self.assertEqual(r.fields["stage_info"], {
            "stage_key": 2308078743,
            "world_kind": 0,
            "world_no": 1,
            "course_no": 30,
        })

    def test_no_big_flower_coins_in_palace(self):
        # Palaces have no Big Flower Coins — different game-design space
        # from regular courses.
        r = decode_play_report(PALACE_COURSE_RESULT)
        self.assertEqual(
            r.fields["big_flower_coin_course_in"], [False, False, False])
        self.assertEqual(
            r.fields["big_flower_coin_course_out"], [False, False, False])

    def test_chara_type_array(self):
        # Different character chosen this run: chara_type 6 (vs 3 in W1-1).
        r = decode_play_report(PALACE_COURSE_RESULT)
        self.assertEqual(r.fields["chara_type_array"], [6])

    def test_dead_count_array_shows_death(self):
        # Player 1 died once during this run (the player lost a life to
        # one of the boss phases before winning); other slots are 0.
        r = decode_play_report(PALACE_COURSE_RESULT)
        self.assertEqual(r.fields["dead_count_array"], [1, 0, 0, 0])
        self.assertEqual(r.fields["direct_dead_count_array"], [1, 0, 0, 0])


class TestKoopajrResultWinPayload(unittest.TestCase):
    """Palace boss-fight WIN.  battle_result == True is THE Royal Seed
    trigger for the AP bridge."""

    def test_decodes_clean(self):
        self.assertEqual(len(KOOPAJR_RESULT_WIN), 499)
        r = decode_play_report(KOOPAJR_RESULT_WIN)
        self.assertEqual(r.entry_count, 16)
        self.assertEqual(r.decoded_count, 16)
        self.assertIsNone(r.error)

    def test_battle_result_true_for_win(self):
        r = decode_play_report(KOOPAJR_RESULT_WIN)
        self.assertEqual(r.fields["battle_result"], True)

    def test_zero_damage_run(self):
        # No-hit clear: all three phases ended with player_damaged_count=0.
        r = decode_play_report(KOOPAJR_RESULT_WIN)
        steps = r.fields["koopajr_step_info"]
        self.assertEqual([s["player_damaged_count"] for s in steps], [0, 0, 0])
        self.assertEqual([s["time"] for s in steps], [15, 28, 41])

    def test_challenge_count_two(self):
        # 2nd attempt at the palace (the prior LOSS was attempt #1).
        r = decode_play_report(KOOPAJR_RESULT_WIN)
        self.assertEqual(r.fields["koopajr_challenge_count"], 2)

    def test_same_stage_info_as_loss_capture(self):
        # Confirms stage_key is stable across attempts at the same palace.
        win = decode_play_report(KOOPAJR_RESULT_WIN)
        loss = decode_play_report(KOOPAJR_RESULT_LOSS)
        self.assertEqual(win.fields["stage_info"], loss.fields["stage_info"])


class TestKoopajrResultLossPayload(unittest.TestCase):
    """Palace boss-fight LOSS — died to Bowser Jr in the Pipe-Rock Plateau
    Palace.  battle_result == False indicates a fail attempt; an AP Royal
    Seed check would only fire on battle_result == True.  Capturing a WIN
    is still TBD but won't change the field shape."""

    def test_decodes_clean(self):
        self.assertEqual(len(KOOPAJR_RESULT_LOSS), 499)
        r = decode_play_report(KOOPAJR_RESULT_LOSS)
        self.assertEqual(r.entry_count, 16)
        self.assertEqual(r.decoded_count, 16)
        self.assertIsNone(r.error)

    def test_battle_result_false_for_loss(self):
        # The headline palace-clear field — drives the AP Royal Seed check.
        r = decode_play_report(KOOPAJR_RESULT_LOSS)
        self.assertEqual(r.fields["battle_result"], False)

    def test_stage_info_identifies_palace(self):
        r = decode_play_report(KOOPAJR_RESULT_LOSS)
        self.assertEqual(r.fields["stage_info"], {
            "stage_key": 2308078743,
            "world_kind": 0,
            "world_no": 1,
            "course_no": 30,
        })

    def test_koopajr_step_info_is_array_of_structs(self):
        # First fixture exercising array-of-structs in real data.
        # Each step = {step: phase index, player_damaged_count: int, time: sec}.
        # In this run the player died after phases 0+1 cleared and phase 2
        # partially through (koopajr_final_stage == 2, battle_result False).
        r = decode_play_report(KOOPAJR_RESULT_LOSS)
        self.assertEqual(r.fields["koopajr_step_info"], [
            {"step": 0, "player_damaged_count": 1, "time": 20},
            {"step": 1, "player_damaged_count": 0, "time": 28},
            {"step": 2, "player_damaged_count": 1, "time": 18},
        ])

    def test_koopajr_metadata_fields(self):
        r = decode_play_report(KOOPAJR_RESULT_LOSS)
        self.assertEqual(r.fields["koopajr_final_stage"], 2)
        self.assertEqual(r.fields["koopajr_total_time"], 67)
        self.assertEqual(r.fields["koopajr_challenge_count"], 1)
        self.assertEqual(r.fields["koopajr_player_rest"], 5)
        self.assertEqual(r.fields["koopajr_start_player_mode"], [1])
        self.assertEqual(r.fields["badge_id_array"], [34])


class TestM25ExitTypeMapping(unittest.TestCase):
    """The M2.5 distinguisher table, empirically derived 2026-05-20.

    With both a Normal Exit clear (W1-1) and a Secret Exit clear (W1-2)
    in the corpus, this asserts the AP-bridge classification logic:

        goal_id == 0 + touch_goal_top_result == True  -> Top of Flag
        goal_id == 0 + touch_goal_top_result == False -> Normal Exit
        goal_id == 1                                  -> Secret Exit
        goal_id == 2 (guessed)                        -> Fake Exit
        room == "koopajr_result"                      -> Palace Clear

    Captures still wanted: a non-Top-of-Flag normal exit (just don't
    touch the top), a Fake Exit (5 levels have one), and a palace
    clear.  None block M2.5 spec completion since the logic above
    works for the 96+89+9 = 194 of 199 AP checks we can already classify.
    """

    def test_w1_1_classifies_as_top_of_flag(self):
        r = decode_play_report(COURSE_RESULT)
        self.assertEqual(r.fields["goal_id"], 0)
        self.assertEqual(r.fields["touch_goal_top_result"], True)
        # → "Top of Flag" by the table above.

    def test_w1_2_classifies_as_secret_exit(self):
        r = decode_play_report(W1_2_COURSE_RESULT_SECRET)
        self.assertEqual(r.fields["goal_id"], 1)
        # touch_goal_top is True here too but irrelevant: goal_id
        # takes precedence over the Top-of-Flag distinguisher.

    def test_palace_loss_should_not_fire_royal_seed_check(self):
        # The room name is the first discriminator (course_result vs
        # koopajr_result), then for palaces battle_result gates whether
        # the AP Royal Seed check actually fires.  This loss capture has
        # battle_result == False, so the bridge should ignore it.
        r = decode_play_report(KOOPAJR_RESULT_LOSS)
        # Field-level confirmation that this is a palace-style report:
        # no goal_id, no touch_goal_top_*, but has battle_result and
        # koopajr_* metadata.
        self.assertNotIn("goal_id", r.fields)
        self.assertNotIn("touch_goal_top_result", r.fields)
        self.assertIn("battle_result", r.fields)
        self.assertEqual(r.fields["battle_result"], False)
        # → AP bridge should NOT fire a Royal Seed check for this event.

    def test_palace_win_fires_BOTH_course_result_AND_koopajr_result(self):
        """The most important M2.5 finding: a palace WIN emits *both*
        a course_result AND a koopajr_result, milliseconds apart.

        Naive M2.5 classification on the course_result alone would
        misclassify the palace win as 'Normal Exit' (goal_id=0,
        touch_goal_top_result=False).  The bridge has two ways to handle
        this correctly:

          a) Priority rule: if koopajr_result fires within ~50ms, use it.
          b) Defensive flag: course_result.world_mother_seed == True
             means it's a palace clear, regardless of goal_id.

        This test pins down both signals so the bridge logic stays right
        even if the timing window assumption breaks."""
        palace_cr = decode_play_report(PALACE_COURSE_RESULT)
        palace_kj = decode_play_report(KOOPAJR_RESULT_WIN)

        # Misleading course_result fields — would route to "Normal Exit"
        # without the override.
        self.assertEqual(palace_cr.fields["goal_id"], 0)
        self.assertEqual(palace_cr.fields["touch_goal_top_result"], False)
        # Defensive flag (option b):
        self.assertEqual(palace_cr.fields["world_mother_seed"], True)
        # Priority signal (option a) — koopajr_result with battle_result True
        # fires alongside the course_result for the same clear:
        self.assertEqual(palace_kj.fields["battle_result"], True)
        # Both events reference the same palace:
        self.assertEqual(
            palace_cr.fields["stage_info"]["stage_key"],
            palace_kj.fields["stage_info"]["stage_key"])

    def test_normal_clear_has_world_mother_seed_false(self):
        """Cross-check the defensive flag: normal level clears never set
        world_mother_seed.  Confirmed across W1-1 (Top of Flag) and W1-2
        (Secret Exit) — gives us a Boolean discriminator independent of
        the koopajr_result correlation."""
        self.assertEqual(
            decode_play_report(COURSE_RESULT).fields["world_mother_seed"],
            False)
        self.assertEqual(
            decode_play_report(W1_2_COURSE_RESULT_SECRET).fields["world_mother_seed"],
            False)


# world_result for the W1 -> W2 inter-world transition.  1059 bytes,
# 26 fields.  Same shape as the intra-world WORLD_RESULT fixture but
# with several first-time field values that pin down semantics:
#   - next_stage_info.world_no = 2 (first non-1 world_no anywhere)
#   - next_stage_info.stage_type = 2 (vs 1 for normal courses — likely
#     "world overworld" vs "course")
#   - next_stage_info.course_id = 0 (no specific course; entering W2)
#   - transition_info.transition_type = 0 (vs -1 for intra-world)
#   - transition_info.worldmap_id = 1 (vs 0 for intra-world)
#   - world_mother_seed = True at top level (Royal Seed earned earlier)
#   - last_ctrl_by_stcik = False (first observed False — controller state)
WORLD_RESULT_W1_TO_W2 = _hex(
    "de 00 1a ab 73 61 76 65 64 61 74 61 5f 69 64 d9 23 62 38 31 33 65"
    "36 37 35 2d 65 62 32 35 34 63 38 61 2d 61 33 65 30 64 30 35 32 2d"
    "64 66 31 61 66 61 64 30 a9 70 6c 61 79 5f 6d 6f 64 65 01 af 74 6f"
    "74 61 6c 5f 70 6c 61 79 5f 74 69 6d 65 d7 00 00 00 00 00 00 00 00"
    "42 aa 73 74 61 67 65 5f 69 6e 66 6f 83 a9 73 74 61 67 65 5f 6b 65"
    "79 d3 00 00 00 00 d4 a6 26 5d aa 77 6f 72 6c 64 5f 6b",
    "69 6e 64 00 a8 77 6f 72 6c 64 5f 6e 6f 01 af 6e 65 78 74 5f 73 74"
    "61 67 65 5f 69 6e 66 6f 85 a9 73 74 61 67 65 5f 6b 65 79 d3 00 00"
    "00 00 b9 4e 40 df aa 73 74 61 67 65 5f 74 79 70 65 02 aa 77 6f 72"
    "6c 64 5f 6b 69 6e 64 00 a8 77 6f 72 6c 64 5f 6e 6f 02 a9 63 6f 75"
    "72 73 65 5f 69 64 00 af 74 72 61 6e 73 69 74 69 6f 6e 5f 69 6e 66"
    "6f 84 af 74 72 61 6e 73 69 74 69 6f 6e 5f 74 79 70 65",
    "00 ab 77 6f 72 6c 64 6d 61 70 5f 69 64 01 a9 63 6f 75 72 73 65 5f"
    "69 64 00 a6 6e 70 63 5f 69 64 00 b3 74 6f 74 61 6c 5f 70 6c 61 79"
    "5f 74 69 6d 65 5f 73 65 63 0e b2 6c 61 73 74 5f 63 74 72 6c 5f 62"
    "79 5f 73 74 63 69 6b c2 b0 6c 6f 63 61 6c 5f 70 6c 61 79 65 72 5f"
    "6e 75 6d 01 b2 76 69 73 69 74 6f 72 5f 70 6c 61 79 65 72 5f 6e 75"
    "6d 00 b5 77 6f 72 6c 64 5f 72 6f 6f 6d 5f 6d 65 6d 62",
    "65 72 5f 6e 75 6d 00 b6 66 72 69 65 6e 64 5f 72 6f 6f 6d 5f 6d 65"
    "6d 62 65 72 5f 6e 75 6d 00 b3 66 72 69 65 6e 64 5f 72 6f 6f 6d 5f"
    "68 61 73 68 5f 69 64 00 b4 63 6f 75 72 73 65 5f 6c 69 73 74 5f 77"
    "61 72 70 5f 6e 75 6d 00 b5 67 65 74 5f 79 65 6c 6c 6f 77 5f 63 6f"
    "69 6e 5f 63 6f 75 6e 74 00 b5 67 65 74 5f 77 6f 6e 64 65 72 5f 63"
    "6f 69 6e 5f 63 6f 75 6e 74 00 ae 61 64 64 5f 72 65 73",
    "74 5f 63 6f 75 6e 74 00 b1 77 6f 72 6c 64 5f 6d 6f 74 68 65 72 5f"
    "73 65 65 64 c3 b8 6f 70 65 6e 5f 63 6f 75 72 73 65 5f 73 65 6c 65"
    "63 74 5f 61 72 72 61 79 92 00 00 b7 6f 70 65 6e 5f 62 61 64 67 65"
    "5f 73 65 6c 65 63 74 5f 61 72 72 61 79 92 00 00 a5 65 6d 6f 74 65"
    "84 a6 70 69 63 74 5f 30 00 a6 70 69 63 74 5f 31 00 a6 70 69 63 74"
    "5f 32 00 a6 70 69 63 74 5f 33 00 aa 63 74 72 6c 5f 67",
    "75 69 64 65 85 aa 6f 70 65 6e 5f 63 6f 75 6e 74 00 a9 6c 61 73 74"
    "5f 70 61 67 65 ff ac 70 61 67 65 5f 66 72 61 6d 65 5f 30 00 ac 70"
    "61 67 65 5f 66 72 61 6d 65 5f 31 00 ac 70 61 67 65 5f 66 72 61 6d"
    "65 5f 32 00 ac 6f 6e 6c 69 6e 65 5f 67 75 69 64 65 8e aa 6f 70 65"
    "6e 5f 63 6f 75 6e 74 00 ae 63 75 72 5f 66 69 72 73 74 5f 70 61 67"
    "65 ff ac 70 61 67 65 5f 66 72 61 6d 65 5f 30 00 ac 70",
    "61 67 65 5f 66 72 61 6d 65 5f 31 00 ac 70 61 67 65 5f 66 72 61 6d"
    "65 5f 32 00 ac 70 61 67 65 5f 66 72 61 6d 65 5f 33 00 ac 70 61 67"
    "65 5f 66 72 61 6d 65 5f 34 00 ac 70 61 67 65 5f 66 72 61 6d 65 5f"
    "35 00 ac 70 61 67 65 5f 66 72 61 6d 65 5f 36 00 ac 70 61 67 65 5f"
    "66 72 61 6d 65 5f 37 00 ac 70 61 67 65 5f 66 72 61 6d 65 5f 38 00"
    "ac 70 61 67 65 5f 66 72 61 6d 65 5f 39 00 ad 70 61 67",
    "65 5f 66 72 61 6d 65 5f 31 30 00 ad 70 61 67 65 5f 66 72 61 6d 65"
    "5f 31 31 00 a8 6e 65 74 5f 63 6f 6e 6e 86 ac 63 68 61 6e 67 65 5f"
    "62 79 5f 6d 6d c2 ac 63 68 61 6e 67 65 5f 62 79 5f 63 74 c2 ac 63"
    "6f 6e 6e 5f 73 65 74 74 69 6e 67 c2 ac 6d 61 74 63 68 5f 6d 61 6b"
    "69 6e 67 c2 aa 63 6f 6e 6e 65 63 74 69 6e 67 c2 ab 63 6f 6e 6e 5f"
    "72 65 73 75 6c 74 ff af 67 65 74 5f 6d 65 64 61 6c 5f",
    "61 72 72 61 79 96 c2 c2 c2 c2 c2 c2 b1 73 79 73 74 65 6d 5f 72 65"
    "70 6f 72 74 5f 74 61 67 ce 81 a7 03 b8",
)


class TestW1ToW2WorldTransition(unittest.TestCase):
    """world_result for the moment the player leaves W1 and enters W2.
    Confirms first-time observed values for several fields that we'd
    previously only seen the default (e.g. world_no=1 everywhere)."""

    def test_decodes_clean(self):
        self.assertEqual(len(WORLD_RESULT_W1_TO_W2), 1059)
        r = decode_play_report(WORLD_RESULT_W1_TO_W2)
        self.assertEqual(r.entry_count, 26)
        self.assertEqual(r.decoded_count, 26)
        self.assertIsNone(r.error)

    def test_destination_is_world_2(self):
        # First payload where next_stage_info.world_no != 1.  Confirms
        # the field IS 1-indexed by player-facing world numbering — we
        # just hadn't crossed worlds before.
        r = decode_play_report(WORLD_RESULT_W1_TO_W2)
        self.assertEqual(r.fields["next_stage_info"], {
            "stage_key": 3108913375,
            "stage_type": 2,
            "world_kind": 0,
            "world_no": 2,
            "course_id": 0,
        })

    def test_inter_world_transition_info(self):
        # Inter-world transition has different fingerprint than
        # overworld→course transition (which has transition_type=-1,
        # worldmap_id=0).
        r = decode_play_report(WORLD_RESULT_W1_TO_W2)
        self.assertEqual(r.fields["transition_info"], {
            "transition_type": 0,
            "worldmap_id": 1,
            "course_id": 0,
            "npc_id": 0,
        })

    def test_world_mother_seed_persists_after_royal_seed(self):
        # Top-level world_mother_seed is True because the player just
        # won the Pipe-Rock Palace and earned a Royal Seed.  Persists
        # across this world transition, not just on the clear itself.
        r = decode_play_report(WORLD_RESULT_W1_TO_W2)
        self.assertEqual(r.fields["world_mother_seed"], True)

    def test_last_ctrl_by_stcik_false(self):
        # First observed False.  Just controller-state telemetry; the
        # bridge can ignore but worth confirming the bool decode works
        # both ways at top level too.
        r = decode_play_report(WORLD_RESULT_W1_TO_W2)
        self.assertEqual(r.fields["last_ctrl_by_stcik"], False)


# ---------------------------------------------------------------------------
# Error-reporting tests.

class TestErrorReporting(unittest.TestCase):
    def test_unknown_value_opcode_reports_position(self):
        # Bytes: header + key "k" + opcode 0xE0 (unmapped — likely a future
        # neg-int range, but not implemented yet).
        bad = _hex("de 00 01  a1 6b  e0")
        with self.assertRaises(DecodeError) as cm:
            decode_play_report(bad)
        self.assertEqual(cm.exception.pos, 5)
        self.assertEqual(cm.exception.op, 0xE0)

    def test_partial_mode_returns_error_string(self):
        bad = _hex("de 00 02  a1 6b 05  a1 6c e0")
        r = decode_play_report(bad, partial_ok=True)
        self.assertEqual(r.entry_count, 2)
        self.assertEqual(r.decoded_count, 1)
        self.assertEqual(r.fields, {"k": 5})
        self.assertIsNotNone(r.error)
        self.assertIn("0xe0", r.error.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
