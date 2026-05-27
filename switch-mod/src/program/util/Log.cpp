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

#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <cstring>

#include "nn/fs.hpp"
#include <nn/ro.h>

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

// ---- always-on ring buffer ----------------------------------------------
// Holds the last ~32 KiB of log output in memory. Captured under a
// spinlock; drainPendingToSd() snapshots under the same lock. 32 KiB
// fits ~250 lines at the 128-byte average we see in boot-time logs and
// is small enough to fit in BSS without alarming the static allocator.
constexpr std::size_t kRingCap = 32 * 1024;
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
        // Drop oldest by shifting. Keeps the newest lines visible even
        // when the buffer fills.
        std::uint32_t drop = used + static_cast<std::uint32_t>(len) - kRingCap;
        if (drop > used) drop = used;
        std::memmove(g_ring, g_ring + drop, used - drop);
        used -= drop;
    }
    std::memcpy(g_ring + used, buf, len);
    g_ring_used.store(used + static_cast<std::uint32_t>(len),
                      std::memory_order_relaxed);
}

// ---- SD drain -----------------------------------------------------------
// Each call overwrites sd:/smbwap_boot.log with the current ring snapshot.
// Soft-fails on any error — never aborts the subsdk.  Re-entrant so we
// can drain at multiple points (e.g. end of exl_main + CreateFileDeviceMgr)
// and the file always holds the most-recent state.  Once a symbol-resolve
// or mount failure occurs, sticky-disable so we don't pay the cost every
// subsequent call.
//
// Why runtime symbol lookup for nn::fs::*: a load-time .sym binding via
// our linker script would refuse to load the subsdk if any single symbol
// failed to resolve on this firmware. Runtime nn::ro::LookupSymbol returns
// a status we can branch on, so a missing symbol just disables SD drain
// instead of crashing the module. The mangling matches the smo_archipelago
// proven pattern (s64 → 'l', u64 → 'm' on aarch64 LP64).
std::atomic<bool> g_sd_drain_disabled{false};

// In-process cache of resolved nn::fs::* function pointers.  First successful
// call populates these and flips g_sd_resolved.  Subsequent calls reuse.
std::atomic<bool> g_sd_resolved{false};
std::uint64_t g_a_mount  = 0;
std::uint64_t g_a_create = 0;
std::uint64_t g_a_open   = 0;
std::uint64_t g_a_close  = 0;
std::uint64_t g_a_write  = 0;
std::uint64_t g_a_delete = 0;

const char* kLogFilePath = "sd:/smbwap_boot.log";

// Local result-style return wrappers.  nn::fs::* in this codebase return
// the u32 typedef from src/nn/nn_common.hpp (a plain success-when-zero
// integer).  Use raw u32 here so the function pointer cast stays valid.
using FsResult = std::uint32_t;
using MountSdCardForDebugFn = FsResult (*)(char const*);
using CreateFileFn          = FsResult (*)(char const*, long long);
using OpenFileFn            = FsResult (*)(nn::fs::FileHandle*, char const*, int);
using CloseFileFn           = void     (*)(nn::fs::FileHandle);
using WriteFileFn           = FsResult (*)(nn::fs::FileHandle, long long,
                                            void const*, unsigned long long,
                                            nn::fs::WriteOption const&);
using DeleteFileFn          = FsResult (*)(char const*);

bool resolveFsSymbol(const char* mangled, std::uint64_t* out) {
    return nn::ro::LookupSymbol(out, mangled).IsSuccess();
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
        svcOutputDebugString(buf, total);
    }

    // Always capture into the ring buffer so the SD drain has a record
    // even when SMBWAP_LOG_SINK_MIN_LEVEL filters out the SVC sink.
    ringAppend(buf, total);
}

std::size_t snapshotRecentLogs(char* out, std::size_t cap) {
    if (!out || cap == 0) return 0;
    SpinGuard g;
    const std::uint32_t used = g_ring_used.load(std::memory_order_relaxed);
    const std::size_t take = (used < cap) ? used : cap;
    std::memcpy(out, g_ring, take);
    return take;
}

bool drainPendingToSd() {
    // Sticky disable on previous fatal failures (symbol-resolve / mount).
    if (g_sd_drain_disabled.load(std::memory_order_acquire)) return false;

    // Resolve nn::fs::* once, cache for subsequent calls.
    if (!g_sd_resolved.load(std::memory_order_acquire)) {
        if (!resolveFsSymbol(
                "_ZN2nn2fs19MountSdCardForDebugEPKc", &g_a_mount) ||
            !resolveFsSymbol(
                "_ZN2nn2fs10CreateFileEPKcl", &g_a_create) ||
            !resolveFsSymbol(
                "_ZN2nn2fs8OpenFileEPNS0_10FileHandleEPKci", &g_a_open) ||
            !resolveFsSymbol(
                "_ZN2nn2fs9CloseFileENS0_10FileHandleE", &g_a_close) ||
            !resolveFsSymbol(
                "_ZN2nn2fs9WriteFileENS0_10FileHandleElPKvmRKNS0_11WriteOptionE",
                &g_a_write) ||
            !resolveFsSymbol(
                "_ZN2nn2fs10DeleteFileEPKc", &g_a_delete)) {
            SMBWAP_LOG_WARN("[sd-log] nn::fs symbol set incomplete; SD drain disabled");
            g_sd_drain_disabled.store(true, std::memory_order_release);
            return false;
        }
        g_sd_resolved.store(true, std::memory_order_release);
    }

    auto fn_mount  = reinterpret_cast<MountSdCardForDebugFn>(g_a_mount);
    auto fn_create = reinterpret_cast<CreateFileFn>(g_a_create);
    auto fn_open   = reinterpret_cast<OpenFileFn>(g_a_open);
    auto fn_close  = reinterpret_cast<CloseFileFn>(g_a_close);
    auto fn_write  = reinterpret_cast<WriteFileFn>(g_a_write);
    auto fn_delete = reinterpret_cast<DeleteFileFn>(g_a_delete);

    // Snapshot the ring under the spinlock. ~32 KiB stack frame here is
    // fine: subsdk main thread has a 1 MiB stack (npdm).
    char snapshot[kRingCap];
    std::size_t snap_len = snapshotRecentLogs(snapshot, sizeof(snapshot));
    if (snap_len == 0) {
        // Nothing to write yet — not a failure, just no-op.
        return false;
    }

    // Mount SD; ignore the Result — if mount fails the subsequent file ops
    // will fail too, and we soft-fail on any of them. MountSdCardForDebug
    // is idempotent (it's safe to re-mount).
    (void)fn_mount("sd");

    // Per the smo_archipelago lunakit pattern: CreateFile sized to the
    // exact data, NOT 0. CreateFile(0)+WriteFile(N) extends the file and
    // aborts in nn::fs past trivial sizes (Result 0xCA8 in FlushFile).
    // Pre-sizing eliminates the extension path entirely.
    (void)fn_delete(kLogFilePath);
    if (fn_create(kLogFilePath, static_cast<long long>(snap_len)) != 0) {
        return false;
    }

    nn::fs::FileHandle fh{};
    if (fn_open(&fh, kLogFilePath, nn::fs::OpenMode_Write) != 0) {
        return false;
    }

    nn::fs::WriteOption opt = nn::fs::WriteOption::CreateOption(
        nn::fs::WriteOptionFlag_Flush);
    const bool wrote = fn_write(fh, 0, snapshot, snap_len, opt) == 0;
    fn_close(fh);
    return wrote;
}

}  // namespace smbwap::util
