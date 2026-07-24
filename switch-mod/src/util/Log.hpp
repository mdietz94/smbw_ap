// Lightweight logging for the SMBW Archipelago subsdk.
//
// Each SMBWAP_LOG_* writes to hk::svc::OutputDebugString (Ryujinx-visible;
// on real Switch routed into lm where binlog visibility is spotty).
//
// log() is safe to call from any thread; allocator-free, atomic_flag
// spinlock + memcpy when appending to the ring.

#pragma once

#include <atomic>
#include <cstdarg>
#include <cstddef>
#include <cstdint>

namespace smbwap::util {

// Rate-limit helper for the `static std::atomic<...> log_budget{N};` idiom.
// Returns true for the first N calls, false forever after.
//
// Use this instead of `budget.fetch_sub(1) > 0`.  On an UNSIGNED counter
// that expression underflows past 0 to UINT32_MAX and then logs forever --
// which is why SetBadgesAbsolute printed on all 120+ drains of the
// 2026-07-23 session despite a budget of 16.  The same footgun is called
// out in ApFrameBridge.cpp's drainInbound gates.  Saturating CAS here so
// the counter can never wrap, signed or not.
inline bool takeBudget(std::atomic<std::int32_t>& budget) {
    auto v = budget.load(std::memory_order_relaxed);
    while (v > 0) {
        if (budget.compare_exchange_weak(v, v - 1,
                                         std::memory_order_relaxed,
                                         std::memory_order_relaxed)) {
            return true;
        }
    }
    return false;
}

enum class LogLevel { Debug, Info, Warn, Error };

void log(LogLevel lvl, const char* fmt, ...);

}  // namespace smbwap::util

#define SMBWAP_LOG_DEBUG(...) ::smbwap::util::log(::smbwap::util::LogLevel::Debug, __VA_ARGS__)
#define SMBWAP_LOG_INFO(...)  ::smbwap::util::log(::smbwap::util::LogLevel::Info,  __VA_ARGS__)
#define SMBWAP_LOG_WARN(...)  ::smbwap::util::log(::smbwap::util::LogLevel::Warn,  __VA_ARGS__)
#define SMBWAP_LOG_ERROR(...) ::smbwap::util::log(::smbwap::util::LogLevel::Error, __VA_ARGS__)

namespace smbwap::util {

// Copy the in-memory log ring into `out` (up to `cap` bytes). Writes the
// actual number of bytes copied to `*out_len` if non-null. Safe to call
// from any thread. Returns the head pointer for convenience.
char* snapshotRecentLogs(char* out, std::size_t cap, std::size_t* out_len);

}  // namespace smbwap::util
