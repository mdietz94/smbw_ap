// Power-up pickup negation via the engine's own ItemGet "can-get" mask.
//
// RE summary (2026-06-10 static session, see the smbw-re-map ledger):
// the player's ItemGet component caches a per-item-type permission bitmask
// at component+0xB0 (u32), rebuilt from the active
// game::actor::component::ItemGetSetting by the builder at NSO +0x3c4050.
// The pickup path tests `mask >> runtime_item_type & 1` (canonical reader
// at NSO +0x182c8bc, plus inlined copies in the sensor code) and simply
// refuses the touch when the bit is clear — identical to how the vanilla
// "DrillDig" setting makes power-ups untouchable while drilling: the item
// stays in the level, no pickup animation, no form change, no damage.
//
// main.cpp trampolines the builder and, after the vanilla mask is built,
// clears every bit in the deny mask below.  Default deny mask is 0 — the
// hook is inert until the AP bridge (or a smoke test) sets it.
//
// Runtime item-type bit positions (bit = RomFS ItemGetActorType enum + 1;
// derived from the builder's per-field OR constants and cross-checked
// against the WonderSeed multi-bit constant 0x2840):

#pragma once

#include <cstdint>

namespace probe {

inline constexpr std::uint32_t kItemGetBit_Kinoko       = 1u << 1;   // Super Mushroom
inline constexpr std::uint32_t kItemGetBit_FireFlower   = 1u << 2;
inline constexpr std::uint32_t kItemGetBit_Star         = 1u << 3;
inline constexpr std::uint32_t kItemGetBit_OneUpKinoko  = 1u << 4;
inline constexpr std::uint32_t kItemGetBit_ElephantSuit = 1u << 5;
inline constexpr std::uint32_t kItemGetBit_Key          = 1u << 10;
inline constexpr std::uint32_t kItemGetBit_DrillSuit    = 1u << 12;
inline constexpr std::uint32_t kItemGetBit_AwaFlower    = 1u << 18;  // Bubble Flower

// The 4 AP Power-Up items (M3.1 set: Elephant / Fire / Bubble / Drill).
inline constexpr std::uint32_t kItemGetBits_ApPowerUps =
    kItemGetBit_FireFlower | kItemGetBit_ElephantSuit |
    kItemGetBit_DrillSuit | kItemGetBit_AwaFlower;

// Replace the deny mask (bits = item types the player must NOT be able to
// pick up).  Thread-safe; takes effect on the next mask rebuild, which the
// per-frame player tick performs (~60 Hz).
void setDeniedItemGetMask(std::uint32_t mask);
std::uint32_t deniedItemGetMask();

// Called by the +0x3c4050 trampoline after orig(): clears denied bits in
// the freshly built mask at component+0xB0.
void applyItemGetDenyMask(void* item_get_component);

}  // namespace probe
