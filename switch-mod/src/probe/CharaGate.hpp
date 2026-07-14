// AP-authoritative character-selection gate (2026-07-08).
//
// The 12 playable characters are AP items (7 base precollected + the 5
// "Character (Easy)" pool items).  When the player commits a character
// they haven't received from AP, we force the selection onto a random
// unlocked character instead.
//
// Enforcement is at the game's own EnumArray commit chokepoint,
// CharaSelectCommit (NSO +0x96e25c, single caller FUN_7101c41f20): it
// takes a record whose +0x30 holds the roster index (0-11, 0xc = none)
// and +0x10 the player slot, resolves the index to a character NAME via
// the 12-entry table @0x71034efad8, murmur3-hashes it, and writes the
// hash into the LocalPlayerCharaType (0x6c05cce3), PlayerCharaType
// (0x71e6f035) and per-course last-played (0x580b7eb4) EnumArrays via
// the gmd+0x2a8 deferred-ring writer FUN_7100387a84.  Rewriting the
// record's roster index BEFORE orig() routes the forced character
// through every one of those writes (and the select-UI usage bitmask)
// exactly as if the player had picked it.
//
// A game-thread sweep (charaGateTick, driven from PlayerTickLatch)
// additionally repairs selections that predate the bridge connection:
// it reads the live roster index per player slot via the
// name-matching reader FUN_710064f4f0(hash, slot, out) and rewrites
// locked entries of both transient arrays through the same
// FUN_7100387a84 writer.
//
// Mask semantics (matches wire.py SetUnlockedCharasMsg): bit i == roster
// index i is allowed, in the GAME's roster-enum order (Mario 0, Luigi 1,
// Peach 2, Daisy 3, KinopioYellow 4, KinopioBlue 5, Kinopico/Toadette 6,
// Totten/Nabbit 7, YoshiGreen 8, YoshiRed 9, YoshiYellow 10,
// YoshiBlue/Light-Blue 11).  mask == 0 -> gate inactive (vanilla).

#pragma once

#include <cstdint>

namespace probe {

// Replace the unlocked-character mask (bit i = roster index i allowed;
// 0 = gate off).  Called on the network rx thread (single atomic store)
// by ApClient::handleLine; every call re-arms the game-thread sweep, so
// the bridge's ~2 s resend cadence doubles as periodic enforcement.
void setUnlockedCharaMask(std::uint32_t mask);
std::uint32_t unlockedCharaMask();

// Game thread, from the CharaSelectCommit trampoline: if the record's
// roster index (+0x30) names a locked character, overwrite it with a
// randomly chosen unlocked one before the game commits it.
void filterCharaCommitRecord(void* rec);

// Game thread, once per frame from PlayerTickLatch: if a sweep is armed
// and the save is live, force locked entries of the LocalPlayerCharaType
// / PlayerCharaType arrays (all 4 player slots) to unlocked picks.
void charaGateTick();

}  // namespace probe
