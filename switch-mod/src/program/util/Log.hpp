// SMBW Archipelago — minimal kernel-debug logger.
//
// Ported from mdietz94/smo_archipelago switch-mod/src/util/Log.{hpp,cpp}.
// M5 (real-hardware): in-memory ring buffer + optional one-shot SD drain.
//
// Primary sink: svcOutputDebugString. Visible in Ryujinx's normal log file
// at Trace level (%APPDATA%\Ryujinx\Logs\Ryujinx_*.log); on real Switch it
// goes to lm where binlog visibility is spotty.
//
// Secondary sink: always-on 32 KiB in-memory ring buffer. Holds the most
// recent log lines. drainPendingToSd() snapshots the ring and writes it
// to sd:/smbwap_boot.log. Called once from CreateFileDeviceMgr::Callback
// after sead's file device mgr is up and SD is mountable — gives us a
// post-boot record of how far exl_main got even when the game crashes
// later in init. If the game crashes BEFORE CreateFileDeviceMgr, the
// Atmosphere crash report at sd:/atmosphere/crash_reports/ is the only
// signal (subsdk9 will appear in the module list either way).
//
// log() is safe to call from any thread (init, hooks, frame). No allocator
// dependency; no thread_local (see Log.cpp comment for why).

#pragma once

#include <cstdarg>
#include <cstddef>

namespace smbwap::util {

enum class LogLevel { Debug, Info, Warn, Error };

void log(LogLevel lvl, const char* fmt, ...);

// One-shot SD drain. Safe to call repeatedly; idempotent after the first
// successful write (subsequent calls are atomic-load no-ops). Soft-fails
// if nn::fs::* symbols can't be resolved or the write fails — never
// aborts. Returns true if drain succeeded.
bool drainPendingToSd();

// Snapshot the recent-logs ring buffer into a caller-provided buffer.
// Returns the number of bytes copied (capped at `cap`).
std::size_t snapshotRecentLogs(char* out, std::size_t cap);

}  // namespace smbwap::util

#define SMBWAP_LOG_DEBUG(...) ::smbwap::util::log(::smbwap::util::LogLevel::Debug, __VA_ARGS__)
#define SMBWAP_LOG_INFO(...)  ::smbwap::util::log(::smbwap::util::LogLevel::Info,  __VA_ARGS__)
#define SMBWAP_LOG_WARN(...)  ::smbwap::util::log(::smbwap::util::LogLevel::Warn,  __VA_ARGS__)
#define SMBWAP_LOG_ERROR(...) ::smbwap::util::log(::smbwap::util::LogLevel::Error, __VA_ARGS__)
