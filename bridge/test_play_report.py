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
