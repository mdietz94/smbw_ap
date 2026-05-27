// Lightweight logging for the SMBW Archipelago subsdk.
//
// Each SMBWAP_LOG_* writes to hk::svc::OutputDebugString (Ryujinx-visible;
// on real Switch routed into lm where binlog visibility is spotty).
//
// Optional: configure with -DSMBWAP_DEBUG_SD_LOG=ON to additionally drain
// the in-memory ring buffer once to sd:/smbwap_boot.log ~5 seconds into
// boot. Phase 1's only on-device diagnostic, since hkMain doesn't install
// any game hooks that could otherwise tick the drain.
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

// Compile-time-gated diagnostic: when SMBWAP_DEBUG_SD_LOG is defined, drains
// the ring buffer to sd:/smbwap_boot.log exactly once per session. Caller is
// expected to invoke this from a context where nn::fs is ready (typically a
// short-lived worker thread spawned from hkMain after a sleep).
// When the flag is undefined, this is a no-op.
void drainPendingToFile();

// Copy the in-memory log ring into `out` (up to `cap` bytes). Writes the
// actual number of bytes copied to `*out_len` if non-null. Safe to call
// from any thread. Returns the head pointer for convenience.
char* snapshotRecentLogs(char* out, std::size_t cap, std::size_t* out_len);

}  // namespace smbwap::util
