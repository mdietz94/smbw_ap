#include "probe/ItemGetGate.hpp"

#include <atomic>

#include "util/Log.hpp"

namespace probe {

namespace {

std::atomic<std::uint32_t> s_denied_mask{0};

// Edge-triggered observability: log the first few times a non-zero deny
// mask actually strips bits from a rebuilt mask (monotonic counter, not
// the fetch_sub budget idiom — that one underflows and spams).
std::atomic<std::uint32_t> s_apply_log_count{0};

}  // namespace

void setDeniedItemGetMask(std::uint32_t mask) {
    const std::uint32_t prev = s_denied_mask.exchange(mask, std::memory_order_relaxed);
    if (prev != mask) {
        SMBWAP_LOG_INFO("[itemgate] deny mask 0x%08x -> 0x%08x", prev, mask);
        s_apply_log_count.store(0, std::memory_order_relaxed);
    }
}

std::uint32_t deniedItemGetMask() {
    return s_denied_mask.load(std::memory_order_relaxed);
}

void applyItemGetDenyMask(void* item_get_component) {
    const std::uint32_t deny = s_denied_mask.load(std::memory_order_relaxed);
    if (deny == 0 || item_get_component == nullptr) return;
    auto* mask = reinterpret_cast<std::uint32_t*>(
        reinterpret_cast<std::uintptr_t>(item_get_component) + 0xB0);
    const std::uint32_t before = *mask;
    const std::uint32_t after = before & ~deny;
    if (after != before) {
        *mask = after;
        if (s_apply_log_count.fetch_add(1, std::memory_order_relaxed) < 4) {
            SMBWAP_LOG_INFO(
                "[itemgate] stripped can-get mask 0x%08x -> 0x%08x (deny 0x%08x)",
                before, after, deny);
        }
    }
}

}  // namespace probe
