// Logging — kernel debug log + in-memory ring buffer.
//
// log() flow:
//   1. hk::svc::OutputDebugString — kernel debug log. Ryujinx surfaces this
//      in its log file; on real Switch this routes to lm where binlog
//      visibility is spotty.
//   2. Always accumulates into a 16 KiB in-memory ring buffer (last ~200
//      log lines). Allocator-free; atomic_flag spinlock + memcpy.
//      snapshotRecentLogs() exposes the ring to callers (e.g., LAN replay
//      of recent logs to the bridge for forensic capture).
//
// The earlier "drain ring to sd:/smbwap_boot.log on boot" diagnostic was
// removed 2026-05-27 once Phase 2 hook coverage made svcOutputDebugString
// the primary signal under Ryujinx; the SD path was a Phase-1-only crutch.

#include "Log.hpp"

#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <cstring>

#include <hk/svc/api.h>

// Compile-time threshold for the kernel debug-log sink. 0=Debug, 1=Info, ...
// Default INFO keeps per-frame DEBUG diagnostics out of normal Ryujinx logs.
// Rebuild with -DSMBWAP_LOG_SINK_MIN_LEVEL=0 to surface DEBUG when needed.
#ifndef SMBWAP_LOG_SINK_MIN_LEVEL
#  define SMBWAP_LOG_SINK_MIN_LEVEL 0  // Phase 1: surface everything until
                                       // we know hkMain runs end-to-end.
#endif

namespace smbwap::util {

namespace {

const char* prefix(LogLevel lvl) {
    switch (lvl) {
        case LogLevel::Debug: return "[smbwap dbg] ";
        case LogLevel::Info:  return "[smbwap inf] ";
        case LogLevel::Warn:  return "[smbwap wrn] ";
        case LogLevel::Error: return "[smbwap err] ";
    }
    return "[smbwap ?] ";
}

// ---- always-on ring buffer ----------------------------------------------
constexpr std::size_t kRingCap = 16 * 1024;
char g_ring[kRingCap];
std::atomic<std::uint32_t> g_ring_used{0};
std::atomic_flag g_ring_lock = ATOMIC_FLAG_INIT;

class SpinGuard {
public:
    SpinGuard() {
        while (g_ring_lock.test_and_set(std::memory_order_acquire)) {
            // Spin briefly; ring writes complete in microseconds.
        }
    }
    ~SpinGuard() { g_ring_lock.clear(std::memory_order_release); }
};

void ringAppend(const char* buf, std::size_t len) {
    if (len == 0 || len > kRingCap) return;
    SpinGuard g;
    std::uint32_t used = g_ring_used.load(std::memory_order_relaxed);
    if (used + len > kRingCap) {
        std::uint32_t drop = used + static_cast<std::uint32_t>(len) - kRingCap;
        if (drop > used) drop = used;
        std::memmove(g_ring, g_ring + drop, used - drop);
        used -= drop;
    }
    std::memcpy(g_ring + used, buf, len);
    g_ring_used.store(used + static_cast<std::uint32_t>(len),
                      std::memory_order_relaxed);
}

}  // namespace

void log(LogLevel lvl, const char* fmt, ...) {
    char buf[512];
    const char* pfx = prefix(lvl);
    const std::size_t pfx_len = std::strlen(pfx);
    if (pfx_len >= sizeof(buf) - 2) return;
    std::memcpy(buf, pfx, pfx_len);

    va_list ap;
    va_start(ap, fmt);
    const int n = std::vsnprintf(buf + pfx_len, sizeof(buf) - pfx_len - 1, fmt, ap);
    va_end(ap);
    if (n < 0) return;

    std::size_t total = pfx_len + static_cast<std::size_t>(n);
    if (total >= sizeof(buf) - 1) total = sizeof(buf) - 2;

    buf[total++] = '\n';

    if (static_cast<int>(lvl) >= SMBWAP_LOG_SINK_MIN_LEVEL) {
        hk::svc::OutputDebugString(buf, total);
    }

    ringAppend(buf, total);
}

char* snapshotRecentLogs(char* out, std::size_t cap, std::size_t* out_len) {
    if (!out || cap == 0) {
        if (out_len) *out_len = 0;
        return out;
    }
    SpinGuard g;
    const std::uint32_t used = g_ring_used.load(std::memory_order_relaxed);
    const std::size_t take = (used < cap) ? used : cap;
    std::memcpy(out, g_ring, take);
    if (out_len) *out_len = take;
    return out;
}

}  // namespace smbwap::util
