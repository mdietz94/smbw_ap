"""Character-block sanity: wire round-trip, processor filtering, dedup."""

from __future__ import annotations

import unittest

from .. import char_block_table, wire
from ..processor import process_event
from ..protocol import CharBlockHitMsg, CheckKind
from ..state import BridgeState, CurrentCourse
from ..location_table import lookup_name


# W1-1 "Welcome to the Flower Kingdom!" stage_key; the charblock table has
# a Mario (chara 0) block here.
_W1_1 = 0xAF11F7FC


class TestCharBlockWire(unittest.TestCase):
    def test_roundtrip(self):
        m = wire.CharBlockHitWireMsg(
            player_slot=2, chara=7, pos=(1.5, 2.0, 0.0), seq=9)
        dec = wire.decode(wire.encode(m))
        self.assertEqual(dec, m)

    def test_defaults(self):
        m = wire.CharBlockHitWireMsg(player_slot=0)
        dec = wire.decode(wire.encode(m))
        self.assertEqual(dec.chara, -1)
        self.assertEqual(dec.pos, (0.0, 0.0, 0.0))
        self.assertEqual(dec.seq, 0)

    def test_to_event(self):
        ev = wire.CharBlockHitWireMsg(player_slot=1, chara=3, seq=4).to_event()
        self.assertIsInstance(ev, CharBlockHitMsg)
        self.assertEqual((ev.player_slot, ev.chara, ev.seq), (1, 3, 4))

    def test_bad_pos_rejected(self):
        with self.assertRaises(wire.ProtocolError):
            wire.CharBlockHitWireMsg.from_wire(
                {"t": "char_block_hit", "player_slot": 0, "pos": [1, 2]})


class TestCharBlockTable(unittest.TestCase):
    def test_row_count(self):
        # 166 instances collapse to 154 (course, chara) rows.
        self.assertEqual(char_block_table.row_count(), 154)

    def test_known_pair(self):
        self.assertEqual(
            char_block_table.lookup(_W1_1, 0),
            "W1: Welcome to the Flower Kingdom! - Mario Block")

    def test_unknown_pair(self):
        # Blue Toad (chara 5) has no block in W1-1.
        self.assertIsNone(char_block_table.lookup(_W1_1, 5))


class TestCharBlockProcessor(unittest.TestCase):
    def _state(self, stage_key=_W1_1, chara=0, unlocked=None):
        st = BridgeState()
        st.mark_course_entered(CurrentCourse(stage_key=stage_key, chara=chara))
        # Unless a test says otherwise, all characters are unlocked (the
        # context pushes this set from items_received; the seven base
        # characters are precollected so this is the common live state).
        st.set_unlocked_charas(
            set(range(12)) if unlocked is None else set(unlocked))
        return st

    def test_matching_character_emits(self):
        st = self._state(chara=0)
        emits = process_event(st, CharBlockHitMsg(player_slot=0, chara=0))
        self.assertEqual(len(emits), 1)
        self.assertEqual(emits[0].kind, CheckKind.CHARACTER_BLOCK)
        self.assertEqual(emits[0].stage_key, _W1_1)
        self.assertEqual(emits[0].metadata["chara"], 0)
        self.assertEqual(
            lookup_name(emits[0]),
            "W1: Welcome to the Flower Kingdom! - Mario Block")

    def test_non_charblock_character_dropped(self):
        # Blue Toad bumps a regular ? block in W1-1: no block table row.
        st = self._state(chara=5)
        emits = process_event(st, CharBlockHitMsg(player_slot=0, chara=5))
        self.assertEqual(emits, [])

    def test_falls_back_to_course_chara(self):
        # Switch didn't resolve the character (chara=-1); the course's
        # captured chara (Mario) disambiguates.
        st = self._state(chara=0)
        emits = process_event(st, CharBlockHitMsg(player_slot=0, chara=-1))
        self.assertEqual(len(emits), 1)
        self.assertEqual(emits[0].metadata["chara"], 0)

    def test_no_course_dropped(self):
        st = BridgeState()  # not in a course
        emits = process_event(st, CharBlockHitMsg(player_slot=0, chara=0))
        self.assertEqual(emits, [])

    def test_dedup(self):
        st = self._state(chara=0)
        first = process_event(st, CharBlockHitMsg(player_slot=0, chara=0))
        second = process_event(st, CharBlockHitMsg(player_slot=0, chara=0))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_two_characters_same_course_dedup_independently(self):
        # W3 Sugarstar Trial has Peach (2) and Yoshi (7) blocks.
        sk = 0x04BF20CF  # _STAGE_THE_SUGARSTAR_TRIAL
        st = BridgeState()
        st.mark_course_entered(CurrentCourse(stage_key=sk, chara=2))
        st.set_unlocked_charas(set(range(12)))
        e1 = process_event(st, CharBlockHitMsg(player_slot=0, chara=2))
        e2 = process_event(st, CharBlockHitMsg(player_slot=0, chara=7))
        self.assertEqual(len(e1), 1)
        self.assertEqual(len(e2), 1)
        self.assertNotEqual(e1[0].metadata["chara"], e2[0].metadata["chara"])


class TestCharBlockUnlockGate(unittest.TestCase):
    """A block hit only credits when the hitting character's AP item has
    been received (state.unlocked_charas, pushed by the context)."""

    def _state(self, unlocked, chara=0):
        st = BridgeState()
        st.mark_course_entered(CurrentCourse(stage_key=_W1_1, chara=chara))
        st.set_unlocked_charas(set(unlocked))
        return st

    def test_locked_character_dropped(self):
        st = self._state(unlocked=set())  # nothing received yet
        emits = process_event(st, CharBlockHitMsg(player_slot=0, chara=0))
        self.assertEqual(emits, [])

    def test_unlocked_character_emits(self):
        st = self._state(unlocked={0})
        emits = process_event(st, CharBlockHitMsg(player_slot=0, chara=0))
        self.assertEqual(len(emits), 1)
        self.assertEqual(emits[0].metadata["chara"], 0)

    def test_rebump_after_unlock_credits(self):
        # The gate sits BEFORE the emit dedup: a hit while locked must not
        # burn the (course, chara) dedup key.
        st = self._state(unlocked=set())
        first = process_event(st, CharBlockHitMsg(player_slot=0, chara=0))
        self.assertEqual(first, [])
        st.set_unlocked_charas({0})  # character item arrives
        second = process_event(st, CharBlockHitMsg(player_slot=0, chara=0))
        self.assertEqual(len(second), 1)

    def test_other_character_unlock_does_not_leak(self):
        # Only Mario is unlocked; a Mario-block hit resolved via the
        # course-chara fallback still requires MARIO to be unlocked --
        # unlocking someone else doesn't help.
        st = self._state(unlocked={5})
        emits = process_event(st, CharBlockHitMsg(player_slot=0, chara=0))
        self.assertEqual(emits, [])

    def test_chara_item_names_roster(self):
        # The gate's chara->item mapping: spot-check the divergent entry
        # (roster says "Yoshi"; the AP item is "Green Yoshi") and the set
        # builder.
        self.assertEqual(char_block_table.chara_item_name(7), "Green Yoshi")
        self.assertEqual(char_block_table.chara_item_name(0), "Mario")
        self.assertIsNone(char_block_table.chara_item_name(12))
        self.assertEqual(
            char_block_table.charas_for_item_names(
                {"Mario", "Green Yoshi", "Nabbit", "Spring Feet Badge"}),
            {0, 7, 11})


if __name__ == "__main__":
    unittest.main()
