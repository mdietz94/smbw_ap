// Logging — kernel debug log + optional one-shot SD-card drain.
//
// Drag-and-drop port of smo_archipelago/switch-mod/src/util/Log.cpp (Phase 1
// of the hakkun migration; see docs/hakkun-migration-plan.md). Differences
// from the smo version:
//   * smoap namespace + log prefixes → smbwap.
//   * Bridge log-forwarder call (smoap::ap::enqueueRemoteLog) dropped —
//     Phase 1 has no ap/ subsystem in the build. Will return in Phase 2.
//   * Target SD-card path is sd:/smbwap_boot.log (not sd:/smo_ap.txt).
//
// log() flow:
//   1. hk::svc::OutputDebugString — kernel debug log. Ryujinx surfaces this
//      in its log file; on real Switch this routes to lm where binlog
//      visibility is spotty.
//   2. Always accumulates into a 16 KiB in-memory ring buffer (last ~200
//      log lines). Allocator-free; atomic_flag spinlock + memcpy.
//   3. If SMBWAP_DEBUG_SD_LOG is defined at compile time:
//      drainPendingToFile() will dump the ring to sd:/smbwap_boot.log on
//      first invocation. Caller (a worker thread spawned from hkMain) is
//      expected to sleep ~5 s before calling so nn::fs is ready.
//
// Symbol resolution: nn::fs::* entry points are looked up at runtime via
// hk::ro::lookupSymbol on first drain, NOT via sail's load-time .sym
// mechanism. Phase 1 has USE_SAIL=FALSE; runtime lookup soft-fails per-call
// if a symbol is missing on this firmware, which is the right failure mode
// for a diagnostic that should never abort the module.

#include "Log.hpp"

#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <cstring>

#include <hk/svc/api.h>

#ifdef SMBWAP_DEBUG_SD_LOG
#  include <hk/ro/RoUtil.h>
#  include <hk/types.h>
#endif

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

#ifdef SMBWAP_DEBUG_SD_LOG

namespace nnfs {
struct FileHandle { unsigned long long _internal; };
struct WriteOption { int flags; };
constexpr int kWriteOptionFlush = 1 << 0;
constexpr int kOpenModeWrite = 2;  // Read=1, Write=2, Append=4

using MountSdCardForDebugFn = bool (*)(char const*);
using CreateFileFn          = unsigned int (*)(char const*, long long);
using OpenFileFn            = unsigned int (*)(FileHandle*, char const*, int);
using CloseFileFn           = void (*)(FileHandle);
using WriteFileFn           = unsigned int (*)(FileHandle, long long,
                                               void const*, unsigned long long,
                                               WriteOption const&);
using DeleteFileFn          = unsigned int (*)(char const*);

MountSdCardForDebugFn s_MountSdCardForDebug = nullptr;
CreateFileFn          s_CreateFile          = nullptr;
OpenFileFn            s_OpenFile            = nullptr;
CloseFileFn           s_CloseFile           = nullptr;
WriteFileFn           s_WriteFile           = nullptr;
DeleteFileFn          s_DeleteFile          = nullptr;
bool                  s_symbols_resolved    = false;
bool                  s_symbols_ok          = false;

bool resolveSymbols() {
    if (s_symbols_resolved) return s_symbols_ok;
    s_symbols_resolved = true;

    auto resolve = [](const char* mangled) -> ::ptr {
        const ::ptr addr = hk::ro::lookupSymbol(mangled);
        if (addr == 0) {
            SMBWAP_LOG_WARN("[sd-log] lookupSymbol miss: %s", mangled);
        }
        return addr;
    };
    auto resolveAlt = [](const char* primary, const char* alt) -> ::ptr {
        ::ptr addr = hk::ro::lookupSymbol(primary);
        if (addr != 0) return addr;
        addr = hk::ro::lookupSymbol(alt);
        if (addr == 0) {
            SMBWAP_LOG_WARN("[sd-log] lookupSymbol miss: %s (also tried %s)",
                            primary, alt);
        }
        return addr;
    };

    // s64/u64 in Nintendo headers typedef to `long`/`unsigned long` on LP64
    // aarch64 (encoded `l`/`m`), NOT to `long long`/`unsigned long long`
    // (`x`/`y`). Try the `l`/`m` variant first since that matches what the
    // game actually ships.
    const ::ptr a_mount  = resolve("_ZN2nn2fs19MountSdCardForDebugEPKc");
    const ::ptr a_create = resolveAlt("_ZN2nn2fs10CreateFileEPKcl",
                                      "_ZN2nn2fs10CreateFileEPKcx");
    const ::ptr a_open   = resolve("_ZN2nn2fs8OpenFileEPNS0_10FileHandleEPKci");
    const ::ptr a_close  = resolve("_ZN2nn2fs9CloseFileENS0_10FileHandleE");
    const ::ptr a_write  = resolveAlt(
        "_ZN2nn2fs9WriteFileENS0_10FileHandleElPKvmRKNS0_11WriteOptionE",
        "_ZN2nn2fs9WriteFileENS0_10FileHandleExPKvyRKNS0_11WriteOptionE");
    const ::ptr a_delete = resolve("_ZN2nn2fs10DeleteFileEPKc");

    if (!(a_mount && a_create && a_open && a_close && a_write && a_delete)) {
        SMBWAP_LOG_WARN("[sd-log] nn::fs symbol set incomplete on this build; "
                        "boot-time SD capture disabled");
        return false;
    }

    s_MountSdCardForDebug = reinterpret_cast<MountSdCardForDebugFn>(a_mount);
    s_CreateFile          = reinterpret_cast<CreateFileFn>(a_create);
    s_OpenFile            = reinterpret_cast<OpenFileFn>(a_open);
    s_CloseFile           = reinterpret_cast<CloseFileFn>(a_close);
    s_WriteFile           = reinterpret_cast<WriteFileFn>(a_write);
    s_DeleteFile          = reinterpret_cast<DeleteFileFn>(a_delete);
    s_symbols_ok = true;
    return true;
}

}  // namespace nnfs

// One-shot drain. Calling twice is a no-op (idempotent on the boot-log).
std::atomic<bool> g_drain_done{false};

// SD root, NOT under atmosphere/contents/<TID>/ — Atmosphere holds a dir
// lock there during boot which makes WriteFile abort (Result 0xCA8). Root
// SD path avoids the conflict.
const char* kLogFilePath = "sd:/smbwap_boot.log";

#endif  // SMBWAP_DEBUG_SD_LOG

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

void drainPendingToFile() {
#ifdef SMBWAP_DEBUG_SD_LOG
    // One-shot: drain exactly once per session.
    if (g_drain_done.load(std::memory_order_acquire)) return;
    g_drain_done.store(true, std::memory_order_release);

    if (!nnfs::resolveSymbols()) return;

    char snapshot[kRingCap];
    std::size_t snap_len = 0;
    {
        SpinGuard g;
        snap_len = g_ring_used.load(std::memory_order_relaxed);
        if (snap_len == 0) return;
        std::memcpy(snapshot, g_ring, snap_len);
    }

    // CreateFile sized to the exact data we're about to write. CreateFile(0)
    // + WriteFile(N) extends the file and aborts in nn::fs past trivial sizes
    // (Result 0xCA8 in FlushFile). Pre-sizing eliminates the extension path.
    (void)nnfs::s_MountSdCardForDebug("sd");
    (void)nnfs::s_DeleteFile(kLogFilePath);
    if (nnfs::s_CreateFile(kLogFilePath,
                           static_cast<long long>(snap_len)) != 0) {
        return;
    }

    nnfs::FileHandle fh{};
    if (nnfs::s_OpenFile(&fh, kLogFilePath, nnfs::kOpenModeWrite) != 0) return;

    nnfs::WriteOption opt{ nnfs::kWriteOptionFlush };
    if (nnfs::s_WriteFile(fh, 0, snapshot, snap_len, opt) != 0) return;
    nnfs::s_CloseFile(fh);
#endif  // SMBWAP_DEBUG_SD_LOG
}

}  // namespace smbwap::util
