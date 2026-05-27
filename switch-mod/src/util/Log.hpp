// Lightweight logging for the SMBW Archipelago subsdk.
//
// Each SMBWAP_LOG_* writes to hk::svc::OutputDebugString (Ryujinx-visible;
// on real Switch routed into lm where binlog visibility is spotty).
//
// log() is safe to call from any thread; allocator-free, atomic_flag
// spinlock + memcpy when appending to the ring.

#pragma once

#include <cstdarg>
#include <cstddef>

namespace smbwap::util {

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
