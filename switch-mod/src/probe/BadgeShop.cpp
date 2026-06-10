#include "probe/BadgeShop.hpp"

#include <atomic>
#include <cstdint>

#include "ap/ApFrameBridge.hpp"
#include "util/Log.hpp"

namespace probe {

namespace {

// AP-authoritative shop ownership.  Both bit-indexed by badge internal_id.
std::atomic<std::uint64_t> s_managed_mask{0};
std::atomic<std::uint64_t> s_sold_mask{0};

// Edge-triggered observability: log the first few times the override
// actually changes a rebuilt item state (monotonic counter, not the
// fetch_sub budget idiom -- that one underflows and spams; see
// [[smbwap-log-budget-underflow]]).
std::atomic<std::uint32_t> s_apply_log_count{0};
std::atomic<std::uint32_t> s_purchase_log_count{0};

// --- UIBadgeShopScreen layout (NSO v1.0.0, proven 2026-06-10) -----------
// computeItemStates (+0x1c3f6a4) reads:
constexpr std::ptrdiff_t kOffCoinsHeld   = 0x6e0;  // u32 currency this shop uses
constexpr std::ptrdiff_t kOffItemCount   = 0x7d0;  // u32 item-model count
constexpr std::ptrdiff_t kOffItemArray   = 0x7d8;  // ptr -> array of item* (stride 8)
// item model struct:
constexpr std::ptrdiff_t kOffItemType    = 0x00;   // u32: 0 badge / 1 ws / 2 1up / 3 kakashi
constexpr std::ptrdiff_t kOffItemBadgeId = 0x04;   // s32 badge internal_id (== owned-bit index)
constexpr std::ptrdiff_t kOffItemPrice   = 0x18;   // s32 price
constexpr std::ptrdiff_t kOffItemState   = 0x20;   // u32 display state (we override)

constexpr std::uint32_t kItemTypeBadge   = 0;
// Display-state enum (written to item+0x20 by computeItemStates):
constexpr std::uint32_t kStateBuyable    = 0;
constexpr std::uint32_t kStateUnaffordable = 1;
constexpr std::uint32_t kStateSoldOut    = 2;

// Defensive cap: real badge-shop lineups hold a handful of rows; a count
// this large means we latched a garbage / mid-construction screen pointer.
constexpr std::uint32_t kMaxItems = 256;

}  // namespace

void setBadgeShopState(std::uint64_t managed_mask, std::uint64_t sold_mask) {
    const std::uint64_t prev_m =
        s_managed_mask.exchange(managed_mask, std::memory_order_relaxed);
    const std::uint64_t prev_s =
        s_sold_mask.exchange(sold_mask, std::memory_order_relaxed);
    if (prev_m != managed_mask || prev_s != sold_mask) {
        SMBWAP_LOG_INFO(
            "[badgeshop] state managed 0x%016llx->0x%016llx sold "
            "0x%016llx->0x%016llx",
            static_cast<unsigned long long>(prev_m),
            static_cast<unsigned long long>(managed_mask),
            static_cast<unsigned long long>(prev_s),
            static_cast<unsigned long long>(sold_mask));
        s_apply_log_count.store(0, std::memory_order_relaxed);
    }
}

void applyBadgeShopItemStates(void* screen) {
    const std::uint64_t managed = s_managed_mask.load(std::memory_order_relaxed);
    if (managed == 0 || screen == nullptr) return;  // inert / vanilla
    const std::uint64_t sold = s_sold_mask.load(std::memory_order_relaxed);

    auto* base = reinterpret_cast<unsigned char*>(screen);
    const std::uint32_t count =
        *reinterpret_cast<std::uint32_t*>(base + kOffItemCount);
    const std::uintptr_t arr =
        *reinterpret_cast<std::uintptr_t*>(base + kOffItemArray);
    if (arr == 0 || count == 0 || count > kMaxItems) return;
    const std::uint32_t coins =
        *reinterpret_cast<std::uint32_t*>(base + kOffCoinsHeld);

    for (std::uint32_t i = 0; i < count; ++i) {
        const std::uintptr_t item =
            reinterpret_cast<std::uintptr_t*>(arr)[i];
        if (item == 0) continue;
        auto* ib = reinterpret_cast<unsigned char*>(item);
        if (*reinterpret_cast<std::uint32_t*>(ib + kOffItemType) != kItemTypeBadge)
            continue;
        const std::int32_t id =
            *reinterpret_cast<std::int32_t*>(ib + kOffItemBadgeId);
        if (id < 0 || id >= 64) continue;
        if (((managed >> id) & 1u) == 0) continue;  // AP doesn't own this row

        auto* state = reinterpret_cast<std::uint32_t*>(ib + kOffItemState);
        std::uint32_t want;
        if ((sold >> id) & 1u) {
            want = kStateSoldOut;
        } else {
            const std::int32_t price =
                *reinterpret_cast<std::int32_t*>(ib + kOffItemPrice);
            // Buyable iff the player can afford it -- ignore the
            // owned/purchased bits the vanilla code keyed on.
            want = (price >= 0 && coins >= static_cast<std::uint32_t>(price))
                       ? kStateBuyable
                       : kStateUnaffordable;
        }
        if (*state != want) {
            *state = want;
            if (s_apply_log_count.fetch_add(1, std::memory_order_relaxed) < 8) {
                SMBWAP_LOG_INFO(
                    "[badgeshop] override row id=%d -> state=%u "
                    "(coins=%u sold=%d)",
                    id, want, coins,
                    static_cast<int>((sold >> id) & 1u));
            }
        }
    }
}

void onBadgeShopPurchase(int badge_internal_id) {
    if (badge_internal_id < 0 || badge_internal_id >= 64) return;
    if (s_purchase_log_count.fetch_add(1, std::memory_order_relaxed) < 16) {
        SMBWAP_LOG_INFO("[badgeshop] purchase committed: internal_id=%d",
                        badge_internal_id);
    }
    smbwap::ap::enqueueBadgeAcquired(
        static_cast<std::uint32_t>(badge_internal_id));
}

}  // namespace probe
