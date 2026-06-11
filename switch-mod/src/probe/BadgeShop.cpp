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

// ---------------------------------------------------------------------------
// AP shop-text substitution.

namespace {

constexpr std::uint32_t kMaxBadges  = 64;
constexpr std::size_t   kTextCap16  = 96;   // char16_t units per badge
constexpr std::uint32_t kArmedBit   = 0x80000000u;

// Per-badge AP description text (UTF-16LE).  s_text_present gates reads;
// written by the LAN rx thread, read on the game thread.  Cosmetic, so a
// torn read during a rare concurrent overwrite is acceptable (the bridge
// re-pushes on its tick).
char16_t                  s_text[kMaxBadges][kTextCap16] = {};
std::atomic<std::uint16_t> s_text_len[kMaxBadges] = {};
std::atomic<bool>          s_text_present[kMaxBadges] = {};

// Shop arm: kArmedBit | badge_id.  Set by resolvePaneContent (only the badge
// shop calls it), honored by the global msbt resolver for the selected
// badge's NAME lookup.  We do NOT consume it on serve (the name pane can
// resolve twice per selection -- main + shadow textbox).  Instead it expires
// after kArmMaxFrames so a stale arm can't relabel a badge in the equip menu
// (a different screen that never re-arms) once the shop closes.
std::atomic<std::uint32_t> s_text_arm{0};
std::atomic<std::uint32_t> s_text_frame{0};       // per-frame clock
std::atomic<std::uint32_t> s_text_arm_frame{0};   // frame the arm was set
constexpr std::uint32_t    kArmMaxFrames = 16;    // ~0.27 s @ 60 Hz
std::atomic<std::uint32_t> s_text_log_count{0};
std::atomic<std::uint32_t> s_text_arm_log_count{0};
std::atomic<std::uint32_t> s_text_serve_log_count{0};

// Minimal UTF-8 -> UTF-16LE converter.  Returns the number of char16_t
// units written (<= cap), always NUL-terminating if room.  Malformed
// bytes are skipped.  BMP + astral (surrogate pairs) handled.
std::size_t utf8ToUtf16(const char* in, char16_t* out, std::size_t cap) {
    if (in == nullptr || cap == 0) return 0;
    std::size_t o = 0;
    const auto* p = reinterpret_cast<const unsigned char*>(in);
    while (*p != 0 && o < cap) {
        std::uint32_t cp;
        unsigned char c = *p;
        if (c < 0x80) { cp = c; p += 1; }
        else if ((c & 0xE0) == 0xC0 && (p[1] & 0xC0) == 0x80) {
            cp = ((c & 0x1Fu) << 6) | (p[1] & 0x3Fu); p += 2;
        } else if ((c & 0xF0) == 0xE0 && (p[1] & 0xC0) == 0x80
                   && (p[2] & 0xC0) == 0x80) {
            cp = ((c & 0x0Fu) << 12) | ((p[1] & 0x3Fu) << 6) | (p[2] & 0x3Fu);
            p += 3;
        } else if ((c & 0xF8) == 0xF0 && (p[1] & 0xC0) == 0x80
                   && (p[2] & 0xC0) == 0x80 && (p[3] & 0xC0) == 0x80) {
            cp = ((c & 0x07u) << 18) | ((p[1] & 0x3Fu) << 12)
               | ((p[2] & 0x3Fu) << 6) | (p[3] & 0x3Fu);
            p += 4;
        } else { p += 1; continue; }  // malformed: skip one byte

        if (cp <= 0xFFFF) {
            out[o++] = static_cast<char16_t>(cp);
        } else {
            if (o + 2 > cap) break;
            cp -= 0x10000;
            out[o++] = static_cast<char16_t>(0xD800 + (cp >> 10));
            out[o++] = static_cast<char16_t>(0xDC00 + (cp & 0x3FF));
        }
    }
    if (o < cap) out[o] = 0;  // NUL-terminate when there's room
    return o;
}

}  // namespace

void setBadgeShopText(std::uint32_t badge_internal_id, const char* utf8_text) {
    if (badge_internal_id >= kMaxBadges) return;
    if (utf8_text == nullptr || utf8_text[0] == '\0') {
        s_text_present[badge_internal_id].store(false, std::memory_order_release);
        return;
    }
    const std::size_t n =
        utf8ToUtf16(utf8_text, s_text[badge_internal_id], kTextCap16);
    s_text_len[badge_internal_id].store(static_cast<std::uint16_t>(n),
                                        std::memory_order_relaxed);
    s_text_present[badge_internal_id].store(true, std::memory_order_release);
    if (s_text_log_count.fetch_add(1, std::memory_order_relaxed) < 16) {
        SMBWAP_LOG_INFO("[badgeshop] text id=%u set (%zu u16)",
                        badge_internal_id, n);
    }
}

void badgeShopTextTick() {
    s_text_frame.fetch_add(1, std::memory_order_relaxed);
}

void armBadgeShopText(int badge_internal_id) {
    if (badge_internal_id >= 0 && badge_internal_id < static_cast<int>(kMaxBadges)) {
        s_text_arm_frame.store(s_text_frame.load(std::memory_order_relaxed),
                               std::memory_order_relaxed);
        s_text_arm.store(kArmedBit | static_cast<std::uint32_t>(badge_internal_id),
                         std::memory_order_release);
        if (s_text_arm_log_count.fetch_add(1, std::memory_order_relaxed) < 24) {
            SMBWAP_LOG_INFO("[badgeshop] arm id=%d", badge_internal_id);
        }
    } else {
        s_text_arm.store(0, std::memory_order_release);
    }
}

bool tryServeBadgeShopText(const char* file, const char* label,
                           void* out_handle) {
    const std::uint32_t armed = s_text_arm.load(std::memory_order_acquire);
    if ((armed & kArmedBit) == 0 || out_handle == nullptr) return false;

    // Expire a stale arm: only honored for a few frames after
    // resolvePaneContent set it, so it can't relabel the badge equip menu
    // (a different screen) once the shop closes.
    const std::uint32_t age =
        s_text_frame.load(std::memory_order_relaxed)
        - s_text_arm_frame.load(std::memory_order_relaxed);
    if (age > kArmMaxFrames) {
        s_text_arm.store(0, std::memory_order_release);
        return false;
    }

    // Identify by ARM + file only.  Live logs (2026-06-11) showed the
    // per-badge label (*(char**)x3) is NOT a usable id here ("ExActReturnJump"
    // / "Badge_PowerUp"), but the selected badge's DESCRIPTION is the single
    // "GameMsg/BadgeInfo" lookup per shop render, and resolvePaneContent
    // re-armed the selected badge id (this+0x6ac -- the same field the
    // working purchase-commit hook reads) right before it.  So we serve that
    // one lookup, keyed by the armed id, and ignore the label.  ("Name_Badge"
    // is looked up several times per render -> ambiguous, so we override the
    // description, not the name; this also keeps the badge's real name.)  We
    // do NOT consume the arm -- a pane can resolve twice (main + shadow); the
    // frame-expiry above retires it after the render.
    const int id = static_cast<int>(armed & 0x7Fu);
    if (s_text_serve_log_count.fetch_add(1, std::memory_order_relaxed) < 40) {
        SMBWAP_LOG_INFO("[badgeshop] serve-call file=%.24s label=%.20s "
                        "armed_id=%d",
                        file ? file : "(null)", label ? label : "(null)", id);
    }

    const bool is_info = (file != nullptr)
        && std::strcmp(file, "GameMsg/BadgeInfo") == 0;
    if (!is_info) return false;
    if (id < 0 || id >= static_cast<int>(kMaxBadges)
        || !s_text_present[id].load(std::memory_order_acquire)) {
        return false;
    }

    // Fill the OUT handle: {char16_t* text @ +0x00; u32 len @ +0x08;
    // u64 id @ +0x10} (len is element count).
    auto* base = reinterpret_cast<unsigned char*>(out_handle);
    *reinterpret_cast<std::uint64_t*>(base + 0x00) =
        reinterpret_cast<std::uintptr_t>(&s_text[id][0]);
    *reinterpret_cast<std::uint32_t*>(base + 0x08) =
        s_text_len[id].load(std::memory_order_relaxed);
    *reinterpret_cast<std::uint64_t*>(base + 0x10) =
        static_cast<std::uint64_t>(id);
    if (s_text_serve_log_count.load(std::memory_order_relaxed) < 40) {
        SMBWAP_LOG_INFO("[badgeshop] SERVED desc id=%d (%u u16)", id,
                        s_text_len[id].load(std::memory_order_relaxed));
    }
    return true;
}

}  // namespace probe
