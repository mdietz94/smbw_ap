"""Tests for the M4 wire codec.

Round-trips every message type, exercises every documented
ProtocolError path, and verifies the wire<->event translation for the
NerveFire and PlayReport types.
"""

from __future__ import annotations

import json
import unittest

from ..protocol import BadgeAcquiredMsg, NerveFireMsg, NerveKind, PlayReportMsg
from ..wire import (
    MAX_LINE_BYTES,
    WIRE_VERSION,
    ApplyWorldUnlockMsg,
    BadgeAcquiredWireMsg,
    ErrMsg,
    GrantHashKeyedMsg,
    HelloAckMsg,
    HelloMsg,
    IncrementHashKeyedMsg,
    KillMsg,
    NerveFireWireMsg,
    PingMsg,
    PlayReportWireMsg,
    PongMsg,
    ProtocolError,
    SetBadgeShopStateMsg,
    SetBadgeShopTextMsg,
    SetBadgesAbsoluteMsg,
    SetItemGetDenyMaskMsg,
    SetRoutableWorldsAbsoluteMsg,
    SetRoyalSeedsAbsoluteMsg,
    decode,
    encode,
)


# ---------------------------------------------------------------------------
# Round-trip every message type.

class TestRoundTrip(unittest.TestCase):

    def _round_trip(self, msg):
        line = encode(msg)
        self.assertIsInstance(line, bytes)
        self.assertTrue(line.endswith(b"\n"))
        # No bare newlines mid-line.
        self.assertEqual(line.count(b"\n"), 1)
        decoded = decode(line)
        self.assertEqual(type(decoded), type(msg))
        self.assertEqual(decoded, msg)

    def test_hello(self):
        self._round_trip(HelloMsg(mod_ver="smbwap-m4", game_ver="smbw-1.0.0", pid=1))

    def test_hello_ack_ok(self):
        self._round_trip(HelloAckMsg(ok=True, bridge_ver="bridge-m4-dev"))

    def test_hello_ack_refused(self):
        self._round_trip(HelloAckMsg(
            ok=False, bridge_ver="bridge-m4-dev",
            reason="wire version mismatch (mine=1, yours=99)"))

    def test_nerve_wonder_seed(self):
        self._round_trip(NerveFireWireMsg(kind=NerveKind.WONDER_SEED_AWARDED, seq=42))

    def test_nerve_course_cleared(self):
        # The bridge drops this in the processor, but the wire format
        # must still admit it -- M3.8 will add DEATH_DETECTED on the
        # same channel and we don't want a schema bump in between.
        self._round_trip(NerveFireWireMsg(kind=NerveKind.COURSE_CLEARED, seq=17))

    def test_nerve_death(self):
        self._round_trip(NerveFireWireMsg(kind=NerveKind.DEATH_DETECTED, seq=3))

    def test_nerve_game_goal_reached(self):
        # M3.7 -- the one-shot Nerve translates to AP ClientStatus.CLIENT_GOAL
        # in the processor; the wire format must round-trip cleanly.
        self._round_trip(NerveFireWireMsg(kind=NerveKind.GAME_GOAL_REACHED, seq=1))

    def test_play_report_short(self):
        self._round_trip(PlayReportWireMsg(room="course_in", payload_hex="de0001"))

    def test_play_report_realistic(self):
        # A 100-byte synthetic payload to exercise the hex path.
        raw = bytes(range(100))
        self._round_trip(PlayReportWireMsg(room="course_result", payload_hex=raw.hex()))

    def test_play_report_empty(self):
        self._round_trip(PlayReportWireMsg(room="ping", payload_hex=""))

    def test_apply_world_unlock(self):
        # Int-category hashes go in "hashes", Bool-category in
        # "bool_hashes" -- the Switch routes them to different writers
        # (grantContainerACounter vs grantContainerBBool).
        self._round_trip(ApplyWorldUnlockMsg(
            hashes=(0x5AC1E406, 0x20FCED8B),
            bool_hashes=(0x3DA75AC4, 0x66500164, 0xBF40AF4F)))

    def test_apply_world_unlock_empty_bool(self):
        self._round_trip(ApplyWorldUnlockMsg(hashes=(0x5AC1E406,)))

    def test_apply_world_unlock_decode_without_bool_hashes(self):
        # Pre-split senders omit "bool_hashes"; decode must default to ().
        line = json.dumps(
            {"t": "apply_world_unlock", "hashes": [1, 2, 3]}).encode() + b"\n"
        decoded = decode(line)
        self.assertEqual(
            decoded, ApplyWorldUnlockMsg(hashes=(1, 2, 3), bool_hashes=()))

    def test_apply_world_unlock_bad_bool_hashes_rejected(self):
        line = json.dumps(
            {"t": "apply_world_unlock", "hashes": [],
             "bool_hashes": [1, "nope"]}).encode() + b"\n"
        with self.assertRaises(ProtocolError):
            decode(line)

    def test_set_badges_absolute_single_bit(self):
        self._round_trip(SetBadgesAbsoluteMsg(bits=1 << 4))  # Spring Feet

    def test_set_badges_absolute_zero(self):
        self._round_trip(SetBadgesAbsoluteMsg(bits=0))

    def test_set_badges_absolute_multiple_bits(self):
        # Spring Feet (4) + Coin Reward (9) + Auto Super Mushroom (46).
        self._round_trip(SetBadgesAbsoluteMsg(
            bits=(1 << 4) | (1 << 9) | (1 << 46)))

    def test_set_badges_absolute_full_u32(self):
        self._round_trip(SetBadgesAbsoluteMsg(bits=0xFFFFFFFF))

    def test_set_badges_absolute_full_u63(self):
        # Switch parses as int64; one below INT64_MAX is the safe upper.
        self._round_trip(SetBadgesAbsoluteMsg(bits=(1 << 63) - 1))

    def test_set_royal_seeds_absolute_zero(self):
        self._round_trip(SetRoyalSeedsAbsoluteMsg(mask=0))

    def test_set_royal_seeds_absolute_single_bit(self):
        # bit 0 = W1 Royal Seed.
        self._round_trip(SetRoyalSeedsAbsoluteMsg(mask=1 << 0))

    def test_set_royal_seeds_absolute_multiple_bits(self):
        # W1 + W3 + W6 -- the mid-multiworld pattern.
        self._round_trip(
            SetRoyalSeedsAbsoluteMsg(mask=(1 << 0) | (1 << 2) | (1 << 5)))

    def test_set_royal_seeds_absolute_full_mask(self):
        # All 6 seeds granted.
        self._round_trip(SetRoyalSeedsAbsoluteMsg(mask=0x3F))

    def test_set_routable_worlds_zero(self):
        # 0 == open-world inactive (Switch hook no-ops).
        self._round_trip(SetRoutableWorldsAbsoluteMsg(mask=0))

    def test_set_routable_worlds_active_set(self):
        # W1 + W3 + W5 routable.
        self._round_trip(SetRoutableWorldsAbsoluteMsg(mask=(1 << 0) | (1 << 2) | (1 << 4)))

    def test_set_routable_worlds_with_castle_bit(self):
        # Active worlds + Castle/Bowser unlocked.
        self._round_trip(SetRoutableWorldsAbsoluteMsg(
            mask=0x15 | (1 << SetRoutableWorldsAbsoluteMsg.CASTLE_BIT)))

    def test_set_itemget_deny_zero(self):
        # 0 == vanilla pickups (Switch hook no-ops).
        self._round_trip(SetItemGetDenyMaskMsg(mask=0))

    def test_set_itemget_deny_ap_powerups(self):
        # The 4 AP Power-Ups: fire + elephant + drill + bubble.
        msg = SetItemGetDenyMaskMsg(
            mask=SetItemGetDenyMaskMsg.AP_POWER_UPS_MASK)
        self.assertEqual(msg.mask, 0x41024)
        self._round_trip(msg)

    def test_set_itemget_deny_single_bit(self):
        self._round_trip(SetItemGetDenyMaskMsg(
            mask=1 << SetItemGetDenyMaskMsg.BIT_FIRE_FLOWER))

    def test_set_itemget_deny_full_u32(self):
        self._round_trip(SetItemGetDenyMaskMsg(mask=0xFFFFFFFF))

    def test_set_badge_shop_state(self):
        self._round_trip(SetBadgeShopStateMsg(
            managed=(1 << 9) | (1 << 58), sold=(1 << 9)))

    def test_set_badge_shop_state_empty(self):
        self._round_trip(SetBadgeShopStateMsg(managed=0, sold=0))

    def test_set_badge_shop_text(self):
        self._round_trip(SetBadgeShopTextMsg(id=9, text="PlayerB: Sword"))

    def test_set_badge_shop_text_empty_clears(self):
        self._round_trip(SetBadgeShopTextMsg(id=42, text=""))

    def test_set_badge_shop_text_unicode(self):
        self._round_trip(SetBadgeShopTextMsg(id=3, text="Zoë: Café★"))

    def test_set_badge_shop_text_overlong_rejected(self):
        long = "x" * SetBadgeShopTextMsg.TEXT_CAP
        line = encode(SetBadgeShopTextMsg(id=1, text=long))
        with self.assertRaises(ProtocolError):
            decode(line)

    def test_grant_hash_keyed_royal_seed_w1(self):
        self._round_trip(GrantHashKeyedMsg(hash=0x55815859, value=1))

    def test_grant_hash_keyed_flower_coin(self):
        self._round_trip(GrantHashKeyedMsg(hash=0xF4EE6827, value=99))

    def test_grant_hash_keyed_zero(self):
        self._round_trip(GrantHashKeyedMsg(hash=0, value=0))

    def test_grant_hash_keyed_max_u32(self):
        # Both fields at the high edge of the documented u32 range.
        self._round_trip(GrantHashKeyedMsg(hash=0xFFFFFFFF, value=0xFFFFFFFF))

    def test_increment_hash_keyed_ten_coin(self):
        # First user: "10 Coin" item adds 10 to flower_coin.
        self._round_trip(IncrementHashKeyedMsg(hash=0xF4EE6827, delta=10))

    def test_increment_hash_keyed_zero_delta(self):
        # A delta of 0 is legal on the wire (degenerate but harmless: RMW
        # writes the unchanged value back).
        self._round_trip(IncrementHashKeyedMsg(hash=0xF4EE6827, delta=0))

    def test_increment_hash_keyed_max_i32(self):
        self._round_trip(IncrementHashKeyedMsg(hash=0xFFFFFFFF, delta=(1 << 31) - 1))

    def test_increment_hash_keyed_negative_refund(self):
        # The TEN_COIN refund path sends delta=-10 to undo the in-game
        # purple-coin add when a 10-coin block is picked up locally.
        self._round_trip(IncrementHashKeyedMsg(hash=0xF4EE6827, delta=-10))

    def test_increment_hash_keyed_min_i32(self):
        self._round_trip(IncrementHashKeyedMsg(hash=0xF4EE6827, delta=-(1 << 31)))

    def test_badge_acquired_typical(self):
        self._round_trip(BadgeAcquiredWireMsg(internal_id=4, seq=1))

    def test_badge_acquired_zero(self):
        self._round_trip(BadgeAcquiredWireMsg(internal_id=0, seq=0))

    def test_badge_acquired_high_internal_id(self):
        self._round_trip(BadgeAcquiredWireMsg(internal_id=46, seq=99))

    def test_badge_acquired_max_u32(self):
        self._round_trip(BadgeAcquiredWireMsg(
            internal_id=0xFFFFFFFF, seq=0xFFFFFFFF))

    def test_kill_typical(self):
        self._round_trip(KillMsg(source="MarioSlot1", cause="mario_died"))

    def test_kill_empty_cause(self):
        # AP sometimes ships a Bounce with no cause string; we still
        # carry it across so the Switch log can record source only.
        self._round_trip(KillMsg(source="OtherPlayer", cause=""))

    def test_kill_truncates_oversize_source(self):
        # KillMsg.to_wire clips source to SOURCE_CAP before serializing;
        # decode reads the truncated form, so the round-trip equality
        # asserts the truncated source survives.
        long_src = "x" * (KillMsg.SOURCE_CAP + 10)
        encoded = encode(KillMsg(source=long_src, cause="c"))
        decoded = decode(encoded)
        self.assertEqual(decoded.source, "x" * KillMsg.SOURCE_CAP)
        self.assertEqual(decoded.cause, "c")

    def test_kill_truncates_oversize_cause(self):
        long_cause = "y" * (KillMsg.CAUSE_CAP + 50)
        encoded = encode(KillMsg(source="s", cause=long_cause))
        decoded = decode(encoded)
        self.assertEqual(decoded.cause, "y" * KillMsg.CAUSE_CAP)

    def test_err(self):
        self._round_trip(ErrMsg(reason="unknown message type 'foo'"))

    def test_ping(self):
        self._round_trip(PingMsg(ts_ms=1234567890))

    def test_pong(self):
        self._round_trip(PongMsg(ts_ms=1234567890))


# ---------------------------------------------------------------------------
# Wire <-> event translation for the types the processor consumes.

class TestEventBridge(unittest.TestCase):

    def test_nerve_to_event_preserves_kind_and_seq(self):
        wire_msg = NerveFireWireMsg(kind=NerveKind.WONDER_SEED_AWARDED, seq=11)
        ev = wire_msg.to_event()
        self.assertIsInstance(ev, NerveFireMsg)
        self.assertEqual(ev.kind, NerveKind.WONDER_SEED_AWARDED)
        self.assertEqual(ev.seq, 11)

    def test_nerve_from_event_round_trip(self):
        ev = NerveFireMsg(kind=NerveKind.COURSE_CLEARED, seq=7)
        wire_msg = NerveFireWireMsg.from_event(ev)
        self.assertEqual(wire_msg.to_event(), ev)

    def test_badge_acquired_to_event_preserves_fields(self):
        wire_msg = BadgeAcquiredWireMsg(internal_id=46, seq=7)
        ev = wire_msg.to_event()
        self.assertIsInstance(ev, BadgeAcquiredMsg)
        self.assertEqual(ev.internal_id, 46)
        self.assertEqual(ev.seq, 7)

    def test_badge_acquired_from_event_round_trip(self):
        ev = BadgeAcquiredMsg(internal_id=9, seq=3)
        wire_msg = BadgeAcquiredWireMsg.from_event(ev)
        self.assertEqual(wire_msg.to_event(), ev)

    def test_play_report_to_event_decodes_hex(self):
        wire_msg = PlayReportWireMsg(room="course_in", payload_hex="deadbeef")
        ev = wire_msg.to_event()
        self.assertIsInstance(ev, PlayReportMsg)
        self.assertEqual(ev.room, "course_in")
        self.assertEqual(ev.payload, b"\xde\xad\xbe\xef")

    def test_play_report_from_event_round_trip(self):
        ev = PlayReportMsg(room="course_result", payload=b"\x01\x02\x03")
        wire_msg = PlayReportWireMsg.from_event(ev)
        self.assertEqual(wire_msg.room, "course_result")
        self.assertEqual(wire_msg.payload_hex, "010203")
        self.assertEqual(wire_msg.to_event(), ev)

    def test_play_report_bad_hex_raises(self):
        wire_msg = PlayReportWireMsg(room="x", payload_hex="not hex!")
        with self.assertRaises(ProtocolError):
            wire_msg.to_event()


# ---------------------------------------------------------------------------
# Encoded JSON shape.

class TestEncodedShape(unittest.TestCase):

    def test_hello_includes_wire_ver(self):
        line = encode(HelloMsg(mod_ver="x", game_ver="y", pid=0))
        obj = json.loads(line.decode("utf-8").rstrip("\n"))
        self.assertEqual(obj["t"], "hello")
        self.assertEqual(obj["wire_ver"], WIRE_VERSION)

    def test_hello_ack_omits_empty_reason(self):
        line = encode(HelloAckMsg(ok=True, bridge_ver="b"))
        obj = json.loads(line.decode("utf-8").rstrip("\n"))
        self.assertNotIn("reason", obj)

    def test_hello_ack_includes_reason_when_set(self):
        line = encode(HelloAckMsg(ok=False, bridge_ver="b", reason="nope"))
        obj = json.loads(line.decode("utf-8").rstrip("\n"))
        self.assertEqual(obj["reason"], "nope")

    def test_compact_json_no_whitespace(self):
        # Switch decoder reads byte-by-byte; extra spaces are wasted bandwidth.
        line = encode(SetBadgesAbsoluteMsg(bits=1 << 4))
        self.assertNotIn(b" ", line[:-1])

    def test_set_badges_absolute_serializes_minimally(self):
        line = encode(SetBadgesAbsoluteMsg(bits=1 << 4))
        self.assertEqual(
            line, b'{"t":"set_badges_absolute","bits":16}\n')

    def test_set_royal_seeds_absolute_serializes_minimally(self):
        line = encode(SetRoyalSeedsAbsoluteMsg(mask=(1 << 0) | (1 << 2)))
        # bit0 | bit2 == 5.
        self.assertEqual(
            line, b'{"t":"set_royal_seeds_absolute","mask":5}\n')

    def test_grant_hash_keyed_serializes_minimally(self):
        line = encode(GrantHashKeyedMsg(hash=0x55815859, value=1))
        # Python's json.dumps emits ints as decimal -- the Switch
        # decoder (parseGrantHashKeyed) reads them via nextInt which
        # handles decimal natively.  0x55815859 == 1434540121.
        self.assertEqual(
            line,
            b'{"t":"grant_hash_keyed","hash":1434540121,"value":1}\n')

    def test_increment_hash_keyed_serializes_minimally(self):
        line = encode(IncrementHashKeyedMsg(hash=0xF4EE6827, delta=10))
        # 0xF4EE6827 == 4109264935.  Delta is signed decimal.
        self.assertEqual(
            line,
            b'{"t":"increment_hash_keyed","hash":4109264935,"delta":10}\n')

    def test_increment_hash_keyed_negative_delta_serializes_signed(self):
        line = encode(IncrementHashKeyedMsg(hash=0xF4EE6827, delta=-10))
        self.assertEqual(
            line,
            b'{"t":"increment_hash_keyed","hash":4109264935,"delta":-10}\n')

    def test_badge_acquired_serializes_minimally(self):
        line = encode(BadgeAcquiredWireMsg(internal_id=4, seq=1))
        self.assertEqual(
            line,
            b'{"t":"badge_acquired","internal_id":4,"seq":1}\n')

    def test_nerve_kind_serializes_as_string_value(self):
        line = encode(NerveFireWireMsg(kind=NerveKind.WONDER_SEED_AWARDED, seq=0))
        obj = json.loads(line.decode("utf-8").rstrip("\n"))
        self.assertEqual(obj["kind"], "wonder_seed_awarded")


# ---------------------------------------------------------------------------
# Decoder accepts str OR bytes, with or without trailing newline.

class TestDecodeInputForms(unittest.TestCase):

    def test_decode_bytes_with_newline(self):
        line = b'{"t":"ping","ts_ms":1}\n'
        msg = decode(line)
        self.assertEqual(msg, PingMsg(ts_ms=1))

    def test_decode_bytes_without_newline(self):
        line = b'{"t":"ping","ts_ms":1}'
        msg = decode(line)
        self.assertEqual(msg, PingMsg(ts_ms=1))

    def test_decode_str(self):
        msg = decode('{"t":"ping","ts_ms":1}')
        self.assertEqual(msg, PingMsg(ts_ms=1))


# ---------------------------------------------------------------------------
# ProtocolError paths.

class TestDecodeErrors(unittest.TestCase):

    def test_invalid_json(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b"{this isn't JSON\n")
        self.assertIn("not valid JSON", str(cm.exception))

    def test_top_level_array(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'["t","hello"]\n')
        self.assertIn("top-level JSON must be an object", str(cm.exception))

    def test_top_level_string(self):
        with self.assertRaises(ProtocolError):
            decode(b'"hello"\n')

    def test_missing_t(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"foo":"bar"}\n')
        self.assertIn("'t'", str(cm.exception))

    def test_non_string_t(self):
        with self.assertRaises(ProtocolError):
            decode(b'{"t":42}\n')

    def test_unknown_message_type(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"warp_to_world_8"}\n')
        self.assertIn("unknown message type", str(cm.exception))

    def test_invalid_utf8(self):
        with self.assertRaises(ProtocolError) as cm:
            # 0x80 is a continuation byte with no leading byte.
            decode(b'\x80')
        self.assertIn("UTF-8", str(cm.exception))

    def test_oversized_line_decode(self):
        big = b'{"t":"err","reason":"' + b"x" * (MAX_LINE_BYTES * 2) + b'"}\n'
        with self.assertRaises(ProtocolError) as cm:
            decode(big)
        self.assertIn("MAX_LINE_BYTES", str(cm.exception))

    def test_hello_missing_mod_ver(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"hello","game_ver":"x"}\n')
        self.assertIn("mod_ver", str(cm.exception))

    def test_hello_ack_missing_bridge_ver(self):
        with self.assertRaises(ProtocolError):
            decode(b'{"t":"hello_ack","ok":true}\n')

    def test_nerve_unknown_kind(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"nerve","kind":"ate_a_mushroom","seq":1}\n')
        self.assertIn("unknown nerve kind", str(cm.exception))

    def test_nerve_missing_kind(self):
        with self.assertRaises(ProtocolError):
            decode(b'{"t":"nerve","seq":1}\n')

    def test_play_report_missing_room(self):
        with self.assertRaises(ProtocolError):
            decode(b'{"t":"play_report","payload":"00"}\n')

    def test_play_report_missing_payload(self):
        with self.assertRaises(ProtocolError):
            decode(b'{"t":"play_report","room":"course_in"}\n')

    def test_set_badges_absolute_missing_bits(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"set_badges_absolute"}\n')
        self.assertIn("bits", str(cm.exception))

    def test_set_badges_absolute_non_int_bits(self):
        with self.assertRaises(ProtocolError):
            decode(b'{"t":"set_badges_absolute","bits":"0x10"}\n')

    def test_set_badges_absolute_bool_bits(self):
        # Python's int subsumes bool; codec rejects bool explicitly.
        with self.assertRaises(ProtocolError):
            decode(b'{"t":"set_badges_absolute","bits":true}\n')

    def test_set_badges_absolute_negative_bits(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"set_badges_absolute","bits":-1}\n')
        self.assertIn("out of range", str(cm.exception))

    def test_set_badges_absolute_too_large_bits(self):
        # 2**64 is one past the documented u64 range.
        big = (1 << 64)
        with self.assertRaises(ProtocolError) as cm:
            decode(f'{{"t":"set_badges_absolute","bits":{big}}}\n'.encode())
        self.assertIn("out of range", str(cm.exception))

    def test_set_royal_seeds_absolute_missing_mask(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"set_royal_seeds_absolute"}\n')
        self.assertIn("mask", str(cm.exception))

    def test_set_royal_seeds_absolute_non_int_mask(self):
        with self.assertRaises(ProtocolError):
            decode(b'{"t":"set_royal_seeds_absolute","mask":"0x3"}\n')

    def test_set_royal_seeds_absolute_bool_mask(self):
        with self.assertRaises(ProtocolError):
            decode(b'{"t":"set_royal_seeds_absolute","mask":true}\n')

    def test_set_royal_seeds_absolute_negative_mask(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"set_royal_seeds_absolute","mask":-1}\n')
        self.assertIn("out of range", str(cm.exception))

    def test_set_royal_seeds_absolute_mask_above_6_bits(self):
        # 64 is one past 2**6; rejected (only 6 Royal Seed bits valid).
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"set_royal_seeds_absolute","mask":64}\n')
        self.assertIn("out of range", str(cm.exception))

    def test_set_routable_worlds_missing_mask(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"set_routable_worlds"}\n')
        self.assertIn("mask", str(cm.exception))

    def test_set_routable_worlds_bool_mask(self):
        with self.assertRaises(ProtocolError):
            decode(b'{"t":"set_routable_worlds","mask":true}\n')

    def test_set_routable_worlds_negative_mask(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"set_routable_worlds","mask":-1}\n')
        self.assertIn("out of range", str(cm.exception))

    def test_set_routable_worlds_mask_above_u16(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(f'{{"t":"set_routable_worlds","mask":{1 << 16}}}\n'.encode())
        self.assertIn("out of range", str(cm.exception))

    def test_grant_hash_keyed_missing_hash(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"grant_hash_keyed","value":1}\n')
        self.assertIn("hash", str(cm.exception))

    def test_grant_hash_keyed_missing_value(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"grant_hash_keyed","hash":1}\n')
        self.assertIn("value", str(cm.exception))

    def test_grant_hash_keyed_non_int_hash(self):
        with self.assertRaises(ProtocolError):
            decode(b'{"t":"grant_hash_keyed","hash":"0x1","value":1}\n')

    def test_grant_hash_keyed_bool_value(self):
        # int subsumes bool in Python; codec rejects bool explicitly.
        with self.assertRaises(ProtocolError):
            decode(b'{"t":"grant_hash_keyed","hash":1,"value":true}\n')

    def test_grant_hash_keyed_negative_hash(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"grant_hash_keyed","hash":-1,"value":1}\n')
        self.assertIn("out of range", str(cm.exception))

    def test_grant_hash_keyed_too_large_value(self):
        # 2**32 is one past the documented u32 range.
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"grant_hash_keyed","hash":1,"value":4294967296}\n')
        self.assertIn("out of range", str(cm.exception))

    def test_increment_hash_keyed_missing_hash(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"increment_hash_keyed","delta":10}\n')
        self.assertIn("hash", str(cm.exception))

    def test_increment_hash_keyed_missing_delta(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"increment_hash_keyed","hash":1}\n')
        self.assertIn("delta", str(cm.exception))

    def test_increment_hash_keyed_non_int_delta(self):
        with self.assertRaises(ProtocolError):
            decode(b'{"t":"increment_hash_keyed","hash":1,"delta":"10"}\n')

    def test_increment_hash_keyed_bool_delta(self):
        # int subsumes bool in Python; codec rejects bool explicitly so a
        # JSON `true` doesn't get silently treated as delta=1.
        with self.assertRaises(ProtocolError):
            decode(b'{"t":"increment_hash_keyed","hash":1,"delta":true}\n')

    def test_increment_hash_keyed_negative_hash(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"increment_hash_keyed","hash":-1,"delta":10}\n')
        self.assertIn("out of range", str(cm.exception))

    def test_increment_hash_keyed_too_large_delta(self):
        # 2**31 is one past the documented i32 range.
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"increment_hash_keyed","hash":1,"delta":2147483648}\n')
        self.assertIn("out of i32 range", str(cm.exception))

    def test_increment_hash_keyed_too_negative_delta(self):
        # -2**31 - 1 is one past the i32 negative limit.
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"increment_hash_keyed","hash":1,"delta":-2147483649}\n')
        self.assertIn("out of i32 range", str(cm.exception))

    def test_badge_acquired_missing_internal_id(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"badge_acquired","seq":1}\n')
        self.assertIn("internal_id", str(cm.exception))

    def test_badge_acquired_non_int_internal_id(self):
        with self.assertRaises(ProtocolError):
            decode(b'{"t":"badge_acquired","internal_id":"4","seq":1}\n')

    def test_badge_acquired_bool_internal_id(self):
        # int subsumes bool in Python; codec rejects bool explicitly.
        with self.assertRaises(ProtocolError):
            decode(b'{"t":"badge_acquired","internal_id":true,"seq":1}\n')

    def test_badge_acquired_negative_internal_id(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"badge_acquired","internal_id":-1,"seq":1}\n')
        self.assertIn("out of range", str(cm.exception))

    def test_badge_acquired_too_large_internal_id(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"badge_acquired","internal_id":4294967296,"seq":1}\n')
        self.assertIn("out of range", str(cm.exception))

    def test_badge_acquired_seq_defaults_to_zero(self):
        msg = decode(b'{"t":"badge_acquired","internal_id":4}\n')
        self.assertEqual(msg, BadgeAcquiredWireMsg(internal_id=4, seq=0))

    def test_err_missing_reason(self):
        with self.assertRaises(ProtocolError):
            decode(b'{"t":"err"}\n')

    def test_kill_missing_source(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"kill","cause":"x"}\n')
        self.assertIn("source", str(cm.exception))

    def test_kill_missing_cause(self):
        with self.assertRaises(ProtocolError) as cm:
            decode(b'{"t":"kill","source":"x"}\n')
        self.assertIn("cause", str(cm.exception))

    def test_kill_non_string_source(self):
        with self.assertRaises(ProtocolError):
            decode(b'{"t":"kill","source":42,"cause":"x"}\n')


# ---------------------------------------------------------------------------
# Encode-side guard against oversized lines.

class TestEncodeErrors(unittest.TestCase):

    def test_oversized_play_report_raises(self):
        # 5000 bytes hex-encoded = 10000 chars, well over MAX_LINE_BYTES.
        huge = "ab" * 5000
        msg = PlayReportWireMsg(room="course_result", payload_hex=huge)
        with self.assertRaises(ValueError) as cm:
            encode(msg)
        self.assertIn("MAX_LINE_BYTES", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
