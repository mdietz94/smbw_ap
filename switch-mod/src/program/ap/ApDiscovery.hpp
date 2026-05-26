// Runtime bridge discovery via UDP.
//
// Ported from smo_archipelago/switch-mod/src/ap/ApDiscovery.{hpp,cpp}.
// Probe sequence:
//
//   1. UDP loopback 127.0.0.1:<port> -- Ryujinx-on-same-host (250ms).
//   2. UDP unicast sweep over the BRIDGE_HOST_STRING /24 subnet -- the
//      seed is baked at CMake configure time.  Every .1..254 host on
//      (seed & 0xFFFFFF00) gets one probe in a burst; first valid
//      `{"t":"bridge","host":...,"port":...}` reply wins.
//
// No fallback IP (BRIDGE_HOST_STRING is only a sweep seed, not a target).
// Caller wraps in its own exponential-backoff loop.

#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace smbwap::ap {

#ifdef DISCOVERY_PORT_VALUE
inline constexpr std::uint16_t kDefaultDiscoveryPort = DISCOVERY_PORT_VALUE;
#else
inline constexpr std::uint16_t kDefaultDiscoveryPort = 17776;
#endif

struct BridgeTarget {
    std::string host;
    std::uint16_t port = 0;
};

// Returns true and fills `out` if discovery succeeded, false otherwise.
bool resolveBridge(BridgeTarget& out,
                   std::uint16_t discovery_port = kDefaultDiscoveryPort);

}  // namespace smbwap::ap
