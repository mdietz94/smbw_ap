// SMBW Archipelago — minimal kernel-debug logger.
//
// Routes to svcOutputDebugString (single kernel syscall, no allocator,
// no filesystem state, safe from any thread). Format is a printf-style
// line with a "[smbwap LVL] " prefix and trailing newline.
//
// Why no thread_local: subsdk has no thread-local memory allocator
// registered. Any `thread_local` variable in this TU causes nnSdk's
// SetMemoryAllocatorForThreadLocal to Abort at module load, ~0x24 bytes
// into exl_main, before any of our code runs. Crash signature: Result
// 0xCA8, User Break, stack ends at SetMemoryAllocatorForThreadLocal.
// If a future edit needs re-entry guarding, use a `static std::atomic`
// with a thread-id check, NOT thread_local.

#include "util/Log.hpp"

#include <cstdarg>
#include <cstdio>
#include <cstring>

// svc.h declares svcOutputDebugString without an `extern "C"` wrapper, so
// including it would produce a C++-mangled call against the asm-side
// unmangled SVC_BEGIN definition. Forward-declare here with the correct
// linkage instead. Signature: Result = u32, size in bytes.
extern "C" unsigned int svcOutputDebugString(const char* str, unsigned long size);

// Compile-time threshold for the SVC sink. 0=Debug 1=Info 2=Warn 3=Error.
// Default INFO keeps per-frame DEBUG diagnostics off normal Ryujinx logs;
// rebuild with -DSMBWAP_LOG_SINK_MIN_LEVEL=0 to surface DEBUG when
// investigating an issue.
#ifndef SMBWAP_LOG_SINK_MIN_LEVEL
#  define SMBWAP_LOG_SINK_MIN_LEVEL 1
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

}  // namespace

void log(LogLevel lvl, const char* fmt, ...) {
    if (static_cast<int>(lvl) < SMBWAP_LOG_SINK_MIN_LEVEL) return;

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

    svcOutputDebugString(buf, total);
}

}  // namespace smbwap::util
