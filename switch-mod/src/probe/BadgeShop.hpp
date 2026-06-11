// AP-authoritative Poplin badge-shop ownership (2026-06-10 RE session).
//
// PROBLEM (the two-way coupling we're breaking): SMBW computes each badge
// shop row's display state from the badge OWNED / EVER-PURCHASED GameData
// bits (BoolArray hashes 0x105df820 / 0xe48a1168).  Those bits serve dual
// duty -- Mario equips from "owned" AND the shop refuses to re-sell any
// badge it sees owned-or-purchased.  That coupling broke AP both ways:
//   1. A badge whose AP "<Badge> Obtained" location is already checked
//      still showed purchasable (the game only sets the bit when *it*
//      sells the badge, not when AP records the check).
//   2. A badge AP GRANTED as an item (owned bit set so Mario can equip it)
//      showed SOLD OUT, so the player could never complete the shop check
//      -- which forced the old auto-grant-the-check workaround.
//
// FIX: make AP the sole authority over the *shop display state* of the
// badges it manages, independent of the in-game bits.  The bridge pushes
// two id-indexed masks (bit == badge internal_id, the same index as the
// owned bitfield):
//   * managed_mask -- badges whose shop state AP owns (the shop-check
//     badges).  Bits NOT in this mask are left at the vanilla state.
//   * sold_mask    -- of the managed badges, the ones whose AP location is
//     already obtained (checked or check-in-flight) -> show SOLD OUT.
// A managed badge NOT in sold_mask shows PURCHASABLE (affordability only,
// ignoring the owned/purchased bits), so an AP-granted-but-unbought badge
// can still be bought to complete its check -- no more auto-grant.
//
// The override is applied AFTER the vanilla state computer
// (UIBadgeShopScreen_computeItemStates, NSO +0x1c3f6a4) runs, by walking
// the freshly-built item model array on the screen object.  See main.cpp
// for the trampoline + the proven struct offsets.
//
// Purchase detection: the shop purchase-commit state machine
// (FUN_7101c4072c, NSO +0x1c4072c) grants the badge exactly once per
// confirmed buy.  main.cpp's trampoline detects that edge and calls
// onBadgeShopPurchase(id), which emits a BadgeAcquired event so the bridge
// fires the "<Badge> Obtained" LocationCheck even when the owned bit was
// already set by an AP grant (the case the bitfield-diff path misses).

#pragma once

#include <cstdint>

namespace probe {

// Replace the AP-authoritative shop ownership masks.  Thread-safe (atomic
// store); consumed by applyBadgeShopItemStates on the game thread.  Both
// masks are bit-indexed by badge internal_id.  managed_mask == 0 makes the
// whole feature inert (vanilla shop behavior).
void setBadgeShopState(std::uint64_t managed_mask, std::uint64_t sold_mask);

// Called by the computeItemStates trampoline AFTER orig() has populated
// the per-item display states.  For each badge row whose internal_id is in
// the managed mask, overwrites item+0x20: SOLD OUT (2) when the badge is in
// the sold mask, else PURCHASABLE/UNAFFORDABLE (0/1) by price-vs-coins --
// never letting the owned/purchased bit force SOLD OUT.  No-op when the
// managed mask is 0.
void applyBadgeShopItemStates(void* screen);

// Called by the purchase-commit trampoline once per confirmed badge buy.
// Emits a BadgeAcquired(internal_id) so the bridge resolves + sends the AP
// shop-check, independent of whether the owned bit was already set.
void onBadgeShopPurchase(int badge_internal_id);

// ---------------------------------------------------------------------------
// AP shop-text (2026-06-10): show, in the badge-shop detail panel, custom
// text reflecting the AP check a badge purchase would send (e.g. the
// scouted "<player>: <item>").  We substitute the badge DESCRIPTION (msbt
// file "GameMsg/BadgeInfo", label "BadgeId%02d") in the global msbt
// resolver FUN_7100250dec, but scope it to the shop by arming a one-shot
// from UIBadgeShopScreen_resolvePaneContent (the only caller that resolves
// badge panes) so the badge equip menu's descriptions stay vanilla.

// Set/replace the per-badge AP description text (UTF-8 in; converted to
// UTF-16LE for the renderer).  Empty/null text clears the override for
// that id.  Thread-safe (called on the LAN rx thread).
void setBadgeShopText(std::uint32_t badge_internal_id, const char* utf8_text);

// Called once per game frame (from the player-tick hook) to advance the
// shop-text arm's freshness clock.  The arm (set by resolvePaneContent) is
// honored only for a few frames so it can't bleed into the badge equip menu
// after the shop closes -- see tryServeBadgeShopText.
void badgeShopTextTick();

// Called on entry to the resolvePaneContent trampoline: arm the shop-text
// context for the currently-selected badge (id from screen+0x6ac), or pass
// -1 to disarm (non-badge selection).  Consumed by the next badge name/desc
// msbt lookup -- this is what scopes the global resolver override to the
// shop.
void armBadgeShopText(int badge_internal_id);

// Called by the msbt-resolver trampoline with the lookup's file string
// (x2) and the resolved label string (*(char**)x3).  If the shop is armed
// and this is the selected badge's DESCRIPTION lookup and we have AP text
// for it, fills the OUT handle {char16_t* text; u32 len; u64 id} at
// `out_handle` and returns true (the trampoline then returns 0 without
// calling the original).  Consumes the arm on any badge name/desc lookup.
// Returns false (run vanilla) for everything else -- the common path for
// all non-shop text in the game.
bool tryServeBadgeShopText(const char* file, const char* label,
                           void* out_handle);

}  // namespace probe
