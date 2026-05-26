// See ApDiscovery.hpp.
//
// Ported from smo_archipelago/switch-mod/src/ap/ApDiscovery.cpp, trimmed:
//   - Dropped the diagnostic DiscoveryReport (no on-Switch debug overlay yet).
//   - Replaced ApState::nowMs() with iteration counting (no wall-clock dep).
//   - Renamed hk::types u32/u8/s32 to <cstdint> equivalents.

#include "ApDiscovery.hpp"

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>

#include "../util/Json.hpp"
#include "../util/Log.hpp"

#ifndef BRIDGE_HOST_STRING
#define BRIDGE_HOST_STRING "192.168.1.1"
#endif

// Match the sockaddr layout nn::socket expects.  Names match what sail/
// libnx resolves against bsd:u.
struct in_addr   { std::uint32_t s_addr; };
struct sockaddr {
    std::uint8_t  sa_len;
    std::uint8_t  sa_family;
    std::uint16_t sa_port;
    in_addr       sa_addr;
    std::uint8_t  sa_zero[8];
};
struct pollfd   { std::int32_t fd; short events; short revents; };

namespace nn::socket {
    std::int32_t Socket(std::int32_t, std::int32_t, std::int32_t);
    std::int32_t SendTo(std::int32_t, const void*, unsigned long,
                        std::int32_t, const ::sockaddr*, std::uint32_t);
    std::int32_t RecvFrom(std::int32_t, void*, unsigned long,
                          std::int32_t, ::sockaddr*, std::uint32_t*);
    std::uint32_t Close(std::int32_t);
    std::int32_t Poll(::pollfd*, unsigned long, std::int32_t);
    std::uint16_t InetHtons(std::uint16_t);
    std::int32_t InetAton(const char*, ::in_addr*);
    std::int32_t GetLastErrno();
}

namespace smbwap::ap {

namespace {

constexpr std::int32_t kAfInet     = 2;
constexpr std::int32_t kSockDgram  = 2;
constexpr std::int32_t kIpprotoUdp = 17;
constexpr std::int32_t kPollIn     = 0x0001;

constexpr std::uint32_t kLoopbackProbeMs = 250;
// Sweep collection: do ~20 poll iterations of 50 ms each = 1 s total.
constexpr std::int32_t  kSweepPollMs     = 50;
constexpr int           kSweepPollIters  = 20;

constexpr std::size_t kReplyBufBytes = 512;
constexpr std::uint32_t kSubnetMask  = 0xFFFFFF00u;
constexpr int kMaxSweepHosts = 254;

// Build the JSON probe payload once per resolveBridge call.
std::size_t buildProbe(char* dst, std::size_t cap) {
    util::json::LineBuffer line;
    util::json::Encoder e(line);
    e.beginObject()
        .key("t").value("discover")
        .key("mod_ver").value("smbwap-m4")
     .endObject();
    line.append('\n');
    const std::size_t take = (line.size() < cap) ? line.size() : cap;
    std::memcpy(dst, line.data(), take);
    return take;
}

std::int32_t openUdpSocket() {
    return nn::socket::Socket(kAfInet, kSockDgram, kIpprotoUdp);
}

void closeSocket(std::int32_t fd) {
    (void)nn::socket::Close(fd);
}

bool waitReadable(std::int32_t fd, std::uint32_t timeout_ms) {
    ::pollfd pfd{ .fd = fd, .events = kPollIn, .revents = 0 };
    const std::int32_t n = nn::socket::Poll(
        &pfd, 1, static_cast<std::int32_t>(timeout_ms));
    if (n <= 0) return false;
    return (pfd.revents & kPollIn) != 0;
}

bool parseReply(const char* data, std::size_t len, BridgeTarget& out) {
    char scratch[kReplyBufBytes];
    if (len > sizeof(scratch)) len = sizeof(scratch);
    std::memcpy(scratch, data, len);

    util::json::Reader r(scratch, len);
    if (!r.enterObject()) return false;

    bool saw_t_bridge = false;
    char host[64] = {0};
    int port = 0;

    std::string_view key;
    while (r.nextField(key)) {
        if (key == "t") {
            std::string_view tv;
            if (!r.nextString(tv)) return false;
            if (tv == "bridge") saw_t_bridge = true;
        } else if (key == "host") {
            std::string_view host_val;
            if (!r.nextString(host_val)) return false;
            const std::size_t take = host_val.size() < sizeof(host) - 1
                ? host_val.size() : sizeof(host) - 1;
            std::memcpy(host, host_val.data(), take);
            host[take] = '\0';
        } else if (key == "port") {
            std::int64_t p = 0;
            if (!r.nextInt(p)) return false;
            port = static_cast<int>(p);
        } else {
            std::string_view _sv;
            std::int64_t _i;
            bool _b;
            (void)(r.isNull() || r.nextString(_sv) ||
                   r.nextInt(_i) || r.nextBool(_b));
        }
    }
    if (!saw_t_bridge || host[0] == '\0' || port <= 0 || port > 0xFFFF) {
        return false;
    }
    // Construct a fresh string + move-assign instead of operator=(const char*).
    // The latter inlines _M_replace which references _M_replace_cold -- a
    // symbol that's not in our subsdk's stripped libstdc++ link archive.
    // _M_construct(ptr, len) doesn't have the same dependency.
    out.host = std::string(host, std::strlen(host));
    out.port = static_cast<std::uint16_t>(port);
    return true;
}

void makeSockAddrFromU32(std::uint32_t host_nbo, std::uint16_t port,
                         ::sockaddr& out) {
    out = ::sockaddr{};
    out.sa_len    = sizeof(out);
    out.sa_family = static_cast<std::uint8_t>(kAfInet);
    out.sa_port   = nn::socket::InetHtons(port);
    out.sa_addr.s_addr = host_nbo;
}

bool oneProbeLiteral(std::int32_t fd,
                     const char* probe_data, std::size_t probe_len,
                     const char* host, std::uint16_t port,
                     std::uint32_t timeout_ms, BridgeTarget& out) {
    ::in_addr ia{};
    if (nn::socket::InetAton(host, &ia) == 0) {
        SMBWAP_LOG_WARN("[disc] InetAton failed for %s", host);
        return false;
    }
    ::sockaddr addr{};
    addr.sa_len    = sizeof(addr);
    addr.sa_family = static_cast<std::uint8_t>(kAfInet);
    addr.sa_port   = nn::socket::InetHtons(port);
    addr.sa_addr   = ia;

    const std::int32_t sent = nn::socket::SendTo(
        fd, probe_data, probe_len, 0, &addr, sizeof(addr));
    if (sent < 0) {
        SMBWAP_LOG_WARN("[disc] sendTo %s:%u failed errno=%d",
                        host, port, nn::socket::GetLastErrno());
        return false;
    }
    if (!waitReadable(fd, timeout_ms)) return false;
    char buf[kReplyBufBytes];
    ::sockaddr from{};
    std::uint32_t from_len = sizeof(from);
    const std::int32_t got = nn::socket::RecvFrom(
        fd, buf, sizeof(buf), 0, &from, &from_len);
    if (got <= 0) return false;
    return parseReply(buf, static_cast<std::size_t>(got), out);
}

inline std::uint32_t byteswap32(std::uint32_t v) {
    return ((v & 0x000000FFu) << 24) | ((v & 0x0000FF00u) << 8) |
           ((v & 0x00FF0000u) >> 8)  | ((v & 0xFF000000u) >> 24);
}

bool sweepSubnet(std::int32_t fd,
                 const char* probe_data, std::size_t probe_len,
                 std::uint32_t seed_ip_nbo, std::uint16_t port,
                 BridgeTarget& out) {
    const std::uint32_t seed_ho  = byteswap32(seed_ip_nbo);
    const std::uint32_t net_ho   = seed_ho & kSubnetMask;
    const std::uint32_t bcast_ho = net_ho | ~kSubnetMask;

    int sent_count = 0;
    for (std::uint32_t ip_ho = net_ho + 1;
         ip_ho < bcast_ho && sent_count < kMaxSweepHosts; ++ip_ho) {
        ::sockaddr addr{};
        makeSockAddrFromU32(byteswap32(ip_ho), port, addr);
        const std::int32_t n = nn::socket::SendTo(
            fd, probe_data, probe_len, 0, &addr, sizeof(addr));
        if (n >= 0) ++sent_count;
    }
    SMBWAP_LOG_INFO(
        "[disc] sweep sent %d probes (subnet %u.%u.%u.0/24)",
        sent_count,
        (net_ho >> 24) & 0xFF, (net_ho >> 16) & 0xFF, (net_ho >> 8) & 0xFF);

    // Collect replies via repeated poll-iterations.  No wall-clock
    // dependency -- we just poll up to `kSweepPollIters` times with a
    // short per-iteration timeout, accepting the first valid reply.
    for (int i = 0; i < kSweepPollIters; ++i) {
        if (!waitReadable(fd, static_cast<std::uint32_t>(kSweepPollMs))) {
            continue;
        }
        char buf[kReplyBufBytes];
        ::sockaddr from{};
        std::uint32_t from_len = sizeof(from);
        const std::int32_t got = nn::socket::RecvFrom(
            fd, buf, sizeof(buf), 0, &from, &from_len);
        if (got <= 0) continue;
        BridgeTarget t;
        if (parseReply(buf, static_cast<std::size_t>(got), t)) {
            out = t;
            return true;
        }
    }
    return false;
}

}  // namespace

bool resolveBridge(BridgeTarget& out, std::uint16_t discovery_port) {
    char probe[kReplyBufBytes];
    const std::size_t probe_len = buildProbe(probe, sizeof(probe));
    if (probe_len == 0) return false;

    // Step 1: loopback (Ryujinx-on-same-host).
    {
        const std::int32_t fd = openUdpSocket();
        if (fd >= 0) {
            BridgeTarget t;
            const bool ok = oneProbeLiteral(
                fd, probe, probe_len,
                "127.0.0.1", discovery_port,
                kLoopbackProbeMs, t);
            closeSocket(fd);
            if (ok) {
                SMBWAP_LOG_INFO(
                    "[disc] resolved via loopback -> %s:%u",
                    t.host.c_str(), t.port);
                out = t;
                return true;
            }
        } else {
            SMBWAP_LOG_WARN("[disc] UDP socket() failed (loopback step)");
        }
    }

    // Step 2: /24 sweep seeded from BRIDGE_HOST_STRING.
    ::in_addr seed_ia{};
    if (nn::socket::InetAton(BRIDGE_HOST_STRING, &seed_ia) == 0) {
        SMBWAP_LOG_WARN(
            "[disc] BRIDGE_HOST_STRING ('%s') is not valid IPv4; sweep disabled",
            BRIDGE_HOST_STRING);
    } else {
        const std::int32_t fd = openUdpSocket();
        if (fd >= 0) {
            BridgeTarget t;
            const bool ok = sweepSubnet(
                fd, probe, probe_len, seed_ia.s_addr, discovery_port, t);
            closeSocket(fd);
            if (ok) {
                SMBWAP_LOG_INFO(
                    "[disc] resolved via subnet sweep -> %s:%u",
                    t.host.c_str(), t.port);
                out = t;
                return true;
            }
        } else {
            SMBWAP_LOG_WARN("[disc] UDP socket() failed (sweep step)");
        }
    }

    SMBWAP_LOG_INFO(
        "[disc] no UDP reply (loopback + sweep); caller retries with backoff");
    return false;
}

}  // namespace smbwap::ap
