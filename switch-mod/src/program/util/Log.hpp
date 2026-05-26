// SMBW Archipelago — minimal kernel-debug logger.
//
// Ported from mdietz94/smo_archipelago switch-mod/src/util/Log.{hpp,cpp}.
// Trimmed for M1: no AP-bridge forwarding (no bridge yet), no SD ring
// buffer (Ryujinx-only target — restore SMBWAP_DEBUG_SD_LOG when we cut
// to real hardware, using the proven SMO drainPendingToFile pattern).
//
// Primary sink: svcOutputDebugString. Visible in Ryujinx's normal log file
// at Trace level (%APPDATA%\Ryujinx\Logs\Ryujinx_*.log); on real Switch it
// goes to lm where binlog visibility is spotty.
//
// log() is safe to call from any thread (init, hooks, frame). No allocator
// dependency; no thread_local (see Log.cpp comment for why).

#pragma once

#include <cstdarg>

namespace smbwap::util {

enum class LogLevel { Debug, Info, Warn, Error };

void log(LogLevel lvl, const char* fmt, ...);

}  // namespace smbwap::util

#define SMBWAP_LOG_DEBUG(...) ::smbwap::util::log(::smbwap::util::LogLevel::Debug, __VA_ARGS__)
#define SMBWAP_LOG_INFO(...)  ::smbwap::util::log(::smbwap::util::LogLevel::Info,  __VA_ARGS__)
#define SMBWAP_LOG_WARN(...)  ::smbwap::util::log(::smbwap::util::LogLevel::Warn,  __VA_ARGS__)
#define SMBWAP_LOG_ERROR(...) ::smbwap::util::log(::smbwap::util::LogLevel::Error, __VA_ARGS__)
