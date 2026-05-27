// See ApClient.hpp.
//
// Subset of smo_archipelago/switch-mod/src/ap/ApClient.cpp (1029 LOC),
// trimmed to the M4 surface:
//   - svcCreateThread/svcStartThread instead of hk::os::Thread (we're on
//     exlaunch + devkitA64, not Hakkun).
//   - No snapshot/replay, no Cappy bubbles, no save-load deferred announce.
//   - Two inbound types (set_badges_absolute, grant_hash_keyed) consumed
//     via ApFrameBridge ring.
//
// The recv/popLine/handleLine decoupling is the most important pattern
// to preserve verbatim: when the bridge sends N messages in one TCP
// push, we must drain ALL of them before going back to Poll() (which
// only checks the socket, not the buffer).  smo had a real bug here in
// 2026-05-16 where re-HELLO messages got held indefinitely.

#include "ApClient.hpp"

#include <cstdint>
#include <cstring>

// hakkun's hk::svc::SleepThread is a C++ namespaced wrapper around the SVC.
// Used to be wrapped in extern "C" when this code targeted libnx's flat
// svcSleepThread; now that we're on hakkun, the wrapper is removed.
#include "hk/svc/api.h"

// nn::os thread API.  We MUST use these instead of raw svcCreateThread
// for the worker: nn::os::* mutex operations (which nn::socket transitively
// uses for its bsd:u session lock) read a per-thread TLS slot that only
// exists on threads created via nn::os::CreateThread.  A raw SVC thread
// crashes in `nn::os::detail::InternalCriticalSectionImplByHorizon::Enter`
// on the very first socket call.  Witnessed on the M4 first-run smoke
// test (Ryujinx GuestThread 49, OutputDebugString trail: HELLO -> worker
// thread started -> entered connect loop -> [conn] state=connecting ->
// invalid memory access at 0x0).
namespace nn::os {
    struct ThreadType;  // 0x1C0 bytes; storage allocated as char buffer below
    void CreateThread(ThreadType* type, void (*entry)(void*), void* arg,
                      void* stack, size_t stack_size, int priority, int ideal_core);
    void StartThread(ThreadType* type);
}

#include "ApDiscovery.hpp"
#include "ApFrameBridge.hpp"
#include "ApProtocol.hpp"
#include "ApState.hpp"
#include "util/Json.hpp"
#include "util/Log.hpp"

// nn::nifm — networking bring-up.
namespace nn::nifm {
    std::uint32_t Initialize();
    void SubmitNetworkRequestAndWait();
    bool IsNetworkAvailable();
}

// Sockaddr layout matches Nintendo's bsd:u service (NOT POSIX).
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
    std::int32_t Socket(std::int32_t domain, std::int32_t type, std::int32_t proto);
    std::uint32_t Connect(std::int32_t fd, const ::sockaddr* addr, std::uint32_t len);
    std::int32_t Send(std::int32_t fd, const void* data, unsigned long len, std::int32_t flags);
    std::int32_t Recv(std::int32_t fd, void* out, unsigned long len, std::int32_t flags);
    std::int32_t SetSockOpt(std::int32_t fd, std::int32_t level, std::int32_t option,
                            const void* val, std::uint32_t len);
    std::uint32_t Close(std::int32_t fd);
    std::int32_t Poll(::pollfd* fds, unsigned long n, std::int32_t timeout_ms);
    std::uint16_t InetHtons(std::uint16_t v);
    std::int32_t InetAton(const char* s, ::in_addr* out);
    std::int32_t GetLastErrno();
}

namespace smbwap::ap {

namespace {

constexpr std::int32_t kAfInet     = 2;
constexpr std::int32_t kSockStream = 1;
constexpr std::int32_t kIpprotoTcp = 6;
constexpr std::int32_t kPollIn     = 0x0001;
constexpr std::int32_t kPollErr    = 0x0008;
constexpr std::int32_t kPollHup    = 0x0010;
constexpr std::int32_t kSolSocket  = 0xffff;
constexpr std::int32_t kSoKeepAlive = 0x0008;

// Worker thread stack: 64 KiB, page-aligned static storage.
constexpr std::size_t kWorkerStackSize = 0x10000;
alignas(0x1000) std::uint8_t g_worker_stack[kWorkerStackSize];

// nn::os::ThreadType is 0x1C0 bytes (per nn/os/os_ThreadTypes.h).  Held
// as raw aligned storage so we don't need to include + instantiate the
// SDK header here -- nn::os::CreateThread populates it via the linked
// SDK code.
alignas(8) std::uint8_t g_worker_thread_storage[0x1C0];

// Backoff schedule.  After kBackoffSeq.size() failures, stay at the cap.
constexpr std::uint32_t kBackoffSeqMs[] = {1000, 2000, 5000, 10000, 30000};

// Per-recv poll timeout.  Short so outbound drains stay reactive.
constexpr std::uint32_t kRecvPollMs = 200;

// Translate a NerveKind sequence number to a SMBWAP_LOG-friendly literal.
const char* connStateName(ConnState s) {
    switch (s) {
        case ConnState::Disconnected: return "disconnected";
        case ConnState::Connecting:   return "connecting";
        case ConnState::Hello:        return "hello";
        case ConnState::Ready:        return "ready";
    }
    return "unknown";
}

void setConn(ConnState s) {
    connState().store(s, std::memory_order_release);
    SMBWAP_LOG_INFO("[conn] state=%s", connStateName(s));
}

void sleepMs(std::int64_t ms) {
    // svcSleepThread is in nanoseconds.
    hk::svc::SleepThread(ms * 1'000'000ll);
}

bool sockAddrFromIpv4(const char* host, std::uint16_t port, ::sockaddr& out) {
    ::in_addr ia{};
    if (nn::socket::InetAton(host, &ia) == 0) return false;
    out = ::sockaddr{};
    out.sa_len    = sizeof(out);
    out.sa_family = static_cast<std::uint8_t>(kAfInet);
    out.sa_port   = nn::socket::InetHtons(port);
    out.sa_addr   = ia;
    return true;
}

int sockPollReadable(std::int32_t fd, std::uint32_t timeout_ms) {
    ::pollfd pfd{ .fd = fd, .events = kPollIn, .revents = 0 };
    const std::int32_t n = nn::socket::Poll(
        &pfd, 1, static_cast<std::int32_t>(timeout_ms));
    if (n < 0) return -1;
    if (n == 0) return 0;
    if (pfd.revents & (kPollErr | kPollHup)) return -1;
    return (pfd.revents & kPollIn) ? 1 : 0;
}

}  // namespace

// Worker entry trampoline.  ABI: void(void*) per svcCreateThread.
void apClientWorkerEntry(void* arg) {
    static_cast<ApClient*>(arg)->threadMain();
    // Should never return; sleep forever to keep the kernel happy.
    while (true) hk::svc::SleepThread(INT64_MAX);
}

ApClient& ApClient::instance() {
    static ApClient s;
    return s;
}

void ApClient::start() {
    bool expected = false;
    if (!started_.compare_exchange_strong(expected, true)) {
        SMBWAP_LOG_INFO("[conn] start: already running, ignoring");
        return;
    }

    SMBWAP_LOG_INFO("[net] nn::nifm::Initialize");
    const std::uint32_t nifm_rc = nn::nifm::Initialize();
    if (nifm_rc != 0) {
        SMBWAP_LOG_ERROR("[net] nifm Initialize failed rc=0x%x", nifm_rc);
        started_.store(false);
        return;
    }
    SMBWAP_LOG_INFO("[net] SubmitNetworkRequestAndWait");
    nn::nifm::SubmitNetworkRequestAndWait();
    const bool up = nn::nifm::IsNetworkAvailable();
    SMBWAP_LOG_INFO("[net] network available: %s", up ? "YES" : "NO");

    // nn::os::CreateThread takes the stack BASE (not top -- SDK manages
    // the descending pointer internally).  Priority 28 matches smo's
    // worker thread priority.  ideal_core=-2 means "any core".
    auto* tt = reinterpret_cast<nn::os::ThreadType*>(g_worker_thread_storage);
    nn::os::CreateThread(
        tt, &apClientWorkerEntry, this,
        g_worker_stack, kWorkerStackSize,
        /*priority=*/28, /*ideal_core=*/-2);
    nn::os::StartThread(tt);
    SMBWAP_LOG_INFO("[net] worker thread started");
}

void ApClient::threadMain() {
    SMBWAP_LOG_INFO("[worker] entered connect loop");

    std::size_t backoff_idx = 0;
    while (true) {
        setConn(ConnState::Connecting);

        BridgeTarget target;
        if (!resolveBridge(target)) {
            const std::uint32_t wait = kBackoffSeqMs[backoff_idx];
            SMBWAP_LOG_INFO(
                "[conn] discovery failed; retrying in %u ms", wait);
            sleepMs(wait);
            if (backoff_idx + 1 < (sizeof(kBackoffSeqMs) / sizeof(kBackoffSeqMs[0]))) {
                ++backoff_idx;
            }
            continue;
        }

        SMBWAP_LOG_INFO("[conn] discovered bridge -> %s:%u",
                        target.host.c_str(), target.port);

        if (!connectOnce(target)) {
            disconnect();
            const std::uint32_t wait = kBackoffSeqMs[backoff_idx];
            SMBWAP_LOG_WARN("[conn] connect failed; retrying in %u ms", wait);
            sleepMs(wait);
            if (backoff_idx + 1 < (sizeof(kBackoffSeqMs) / sizeof(kBackoffSeqMs[0]))) {
                ++backoff_idx;
            }
            continue;
        }

        // Connection up.  Reset backoff to the first slot.
        backoff_idx = 0;
        setConn(ConnState::Hello);
        sendHello();
        setConn(ConnState::Ready);

        // Service loop: poll the socket, drain outbound, drain inbound.
        while (true) {
            pumpOutbound();

            const int p = sockPollReadable(socket_fd_, kRecvPollMs);
            if (p < 0) {
                SMBWAP_LOG_WARN("[conn] poll error; reconnecting");
                break;
            }
            if (p > 0) {
                if (!recvIntoBuf()) {
                    SMBWAP_LOG_INFO("[conn] peer closed");
                    break;
                }
                // Drain every complete line in the buffer.
                char line[kInboundLineCap];
                std::size_t line_len = 0;
                while (popLine(line, line_len)) {
                    handleLine(line, line_len);
                }
            }
        }

        disconnect();
        // Disconnected mid-session: drop into backoff but start at the
        // shortest interval (don't punish a long-stable session for one
        // transient drop).
        backoff_idx = 0;
        const std::uint32_t wait = kBackoffSeqMs[backoff_idx];
        SMBWAP_LOG_INFO("[conn] peer closed; reconnecting in %u ms", wait);
        sleepMs(wait);
    }
}

bool ApClient::connectOnce(const BridgeTarget& target) {
    socket_fd_ = nn::socket::Socket(kAfInet, kSockStream, kIpprotoTcp);
    if (socket_fd_ < 0) {
        SMBWAP_LOG_WARN("[conn] Socket() failed errno=%d",
                        nn::socket::GetLastErrno());
        socket_fd_ = -1;
        return false;
    }

    ::sockaddr addr{};
    if (!sockAddrFromIpv4(target.host.c_str(), target.port, addr)) {
        SMBWAP_LOG_WARN("[conn] bad host literal %s", target.host.c_str());
        return false;
    }

    const std::uint32_t rc = nn::socket::Connect(socket_fd_, &addr, sizeof(addr));
    if (rc != 0) {
        SMBWAP_LOG_WARN("[conn] Connect to %s:%u failed errno=%d",
                        target.host.c_str(), target.port,
                        nn::socket::GetLastErrno());
        return false;
    }

    const std::int32_t one = 1;
    (void)nn::socket::SetSockOpt(
        socket_fd_, kSolSocket, kSoKeepAlive, &one, sizeof(one));

    SMBWAP_LOG_INFO("[conn] TCP connect OK -> %s:%u",
                    target.host.c_str(), target.port);
    return true;
}

void ApClient::disconnect() {
    if (socket_fd_ >= 0) {
        (void)nn::socket::Close(socket_fd_);
        socket_fd_ = -1;
    }
    read_buf_len_ = 0;
    setConn(ConnState::Disconnected);
}

void ApClient::sendHello() {
    WireHello h{};
    copyFixed(h.mod_ver, "smbwap-m4");
    copyFixed(h.game_ver, "smbw-1.0.0");
    h.pid = 1;

    util::json::LineBuffer line;
    encodeHello(line, h);
    const std::int32_t n = nn::socket::Send(
        socket_fd_, line.data(), line.size(), 0);
    if (n < 0) {
        SMBWAP_LOG_WARN("[conn] HELLO send failed errno=%d",
                        nn::socket::GetLastErrno());
    } else {
        SMBWAP_LOG_INFO("[conn] HELLO sent (%d bytes)", n);
    }
}

void ApClient::pumpOutbound() {
    OutboundEvent ev;
    while (outboundRing().pop(ev)) {
        util::json::LineBuffer line;
        if (!encodeOutbound(line, ev)) {
            SMBWAP_LOG_WARN("[ring] encode skipped (unknown outbound kind)");
            continue;
        }
        if (line.truncated()) {
            SMBWAP_LOG_WARN("[ring] outbound line truncated, dropping");
            continue;
        }
        const std::int32_t n = nn::socket::Send(
            socket_fd_, line.data(), line.size(), 0);
        if (n < 0) {
            // Push back is not feasible with a pop-based ring; the message
            // is lost.  Caller observes via bridge-side gaps; M5 adds
            // snapshot/replay.
            SMBWAP_LOG_WARN(
                "[ring] outbound send failed errno=%d (kind=%u)",
                nn::socket::GetLastErrno(),
                static_cast<unsigned>(ev.kind));
            break;
        }
    }
}

bool ApClient::recvIntoBuf() {
    if (read_buf_len_ >= sizeof(read_buf_)) {
        SMBWAP_LOG_WARN(
            "[conn] recv buffer full without newline; dropping connection");
        return false;
    }
    const std::int32_t n = nn::socket::Recv(
        socket_fd_,
        read_buf_ + read_buf_len_,
        sizeof(read_buf_) - read_buf_len_,
        0);
    if (n < 0) {
        SMBWAP_LOG_WARN("[conn] Recv failed errno=%d",
                        nn::socket::GetLastErrno());
        return false;
    }
    if (n == 0) {
        // Orderly peer close.
        return false;
    }
    read_buf_len_ += static_cast<std::size_t>(n);
    return true;
}

bool ApClient::popLine(char* out, std::size_t& out_len) {
    // Find first '\n' in [0, read_buf_len_).
    std::size_t i = 0;
    for (; i < read_buf_len_; ++i) {
        if (read_buf_[i] == '\n') break;
    }
    if (i >= read_buf_len_) return false;

    const std::size_t line_len = i;
    if (line_len > kInboundLineCap - 1) {
        // Line too long; drop the whole line and continue.
        SMBWAP_LOG_WARN("[conn] inbound line over cap (%zu); dropping", line_len);
        std::memmove(read_buf_, read_buf_ + i + 1, read_buf_len_ - i - 1);
        read_buf_len_ -= (i + 1);
        return false;
    }

    std::memcpy(out, read_buf_, line_len);
    out[line_len] = '\0';
    out_len = line_len;

    // Compact the buffer.
    const std::size_t remain = read_buf_len_ - (i + 1);
    if (remain > 0) {
        std::memmove(read_buf_, read_buf_ + i + 1, remain);
    }
    read_buf_len_ = remain;
    return true;
}

void ApClient::handleLine(char* line, std::size_t len) {
    InboundMsg msg;
    if (!decodeInbound(line, len, msg)) {
        SMBWAP_LOG_WARN("[conn] decode failed: %.80s", line);
        return;
    }
    switch (msg.kind) {
        case InboundKind::HelloAck:
            SMBWAP_LOG_INFO(
                "[conn] HELLO acked: ok=%s bridge_ver=%s wire_ver=%d",
                msg.hello_ack.ok ? "true" : "false",
                msg.hello_ack.bridge_ver, msg.hello_ack.wire_ver);
            if (!msg.hello_ack.ok && msg.hello_ack.reason[0]) {
                SMBWAP_LOG_WARN("[conn] HELLO refused: %s", msg.hello_ack.reason);
            }
            return;
        case InboundKind::SetBadgesAbsolute:
            SMBWAP_LOG_INFO(
                "[grant] received SetBadgesAbsolute(bits=0x%016llx), enqueued",
                static_cast<unsigned long long>(msg.set_badges_absolute.bits));
            // Forward to the game thread via the inbound ring;
            // ApFrameBridge::drainInbound applies it via
            // setBadgeBitfieldAbsolute.
            if (!inboundRing().push(msg)) {
                SMBWAP_LOG_WARN(
                    "[ring] inbound full; dropping SetBadgesAbsolute(bits=0x%016llx)",
                    static_cast<unsigned long long>(msg.set_badges_absolute.bits));
            }
            return;
        case InboundKind::GrantHashKeyed:
            SMBWAP_LOG_INFO(
                "[grant] received GrantHashKeyed(hash=0x%08x, value=%u), enqueued",
                msg.grant_hash_keyed.hash, msg.grant_hash_keyed.value);
            // Forward to game thread; drainInbound applies via
            // probe::grantContainerACounter -> FUN_710049F648.
            if (!inboundRing().push(msg)) {
                SMBWAP_LOG_WARN(
                    "[ring] inbound full; dropping GrantHashKeyed(hash=0x%08x)",
                    msg.grant_hash_keyed.hash);
            }
            return;
        case InboundKind::IncrementHashKeyed:
            SMBWAP_LOG_INFO(
                "[grant] received IncrementHashKeyed(hash=0x%08x, delta=%d), enqueued",
                msg.increment_hash_keyed.hash,
                static_cast<int>(msg.increment_hash_keyed.delta));
            // Forward to game thread; drainInbound applies via
            // probe::incrementContainerACounter (saturating RMW on
            // FUN_710012AE94 + FUN_710049F648).
            if (!inboundRing().push(msg)) {
                SMBWAP_LOG_WARN(
                    "[ring] inbound full; dropping IncrementHashKeyed(hash=0x%08x)",
                    msg.increment_hash_keyed.hash);
            }
            return;
        case InboundKind::Kill:
            SMBWAP_LOG_INFO(
                "[deathlink] received Kill(source=%s, cause=%s), enqueued",
                msg.kill.source, msg.kill.cause);
            // Forward to game thread; drainInbound applies via
            // probe::synthKill (HP=0 int16 write at live_base + 0x38).
            if (!inboundRing().push(msg)) {
                SMBWAP_LOG_WARN(
                    "[ring] inbound full; dropping Kill(source=%s)",
                    msg.kill.source);
            }
            return;
        case InboundKind::SetPerCourseBitfield:
            SMBWAP_LOG_INFO(
                "[grant] received SetPerCourseBitfield(hash=0x%08x, "
                "course=%u, bitmask=0x%08x), enqueued",
                msg.set_per_course_bitfield.hash,
                msg.set_per_course_bitfield.course_index,
                msg.set_per_course_bitfield.bitmask);
            // Forward to game thread; drainInbound applies via
            // probe::setPerCourseBitfieldAbsolute -> FUN_7101F2B354.
            if (!inboundRing().push(msg)) {
                SMBWAP_LOG_WARN(
                    "[ring] inbound full; dropping SetPerCourseBitfield"
                    "(hash=0x%08x, course=%u)",
                    msg.set_per_course_bitfield.hash,
                    msg.set_per_course_bitfield.course_index);
            }
            return;
        case InboundKind::DumpSaveField:
            SMBWAP_LOG_INFO(
                "[probe] received DumpSaveField(base=NSO+0x%x, "
                "offset=0x%x), enqueued",
                msg.dump_save_field.base_nso_offset,
                msg.dump_save_field.field_offset);
            if (!inboundRing().push(msg)) {
                SMBWAP_LOG_WARN(
                    "[ring] inbound full; dropping DumpSaveField"
                    "(base=NSO+0x%x, offset=0x%x)",
                    msg.dump_save_field.base_nso_offset,
                    msg.dump_save_field.field_offset);
            }
            return;
        case InboundKind::SetContainerCBit:
            SMBWAP_LOG_INFO(
                "[grant] received SetContainerCBit(hash=0x%08x, bit=%u, "
                "value=%u), enqueued",
                msg.set_container_c_bit.hash,
                msg.set_container_c_bit.bit_index,
                static_cast<unsigned>(msg.set_container_c_bit.value));
            // Forward to game thread; drainInbound applies via
            // probe::setContainerCBit -> direct container-C memory write.
            if (!inboundRing().push(msg)) {
                SMBWAP_LOG_WARN(
                    "[ring] inbound full; dropping SetContainerCBit"
                    "(hash=0x%08x, bit=%u)",
                    msg.set_container_c_bit.hash,
                    msg.set_container_c_bit.bit_index);
            }
            return;
        case InboundKind::SetRoyalSeedsAbsolute:
            SMBWAP_LOG_INFO(
                "[grant] received SetRoyalSeedsAbsolute(mask=0x%02x), enqueued",
                static_cast<unsigned>(msg.set_royal_seeds_absolute.mask));
            // Forward to game thread; drainInbound loops the 6 Royal
            // Seed hashes and grants/clears each per bit via
            // probe::grantContainerBBool.
            if (!inboundRing().push(msg)) {
                SMBWAP_LOG_WARN(
                    "[ring] inbound full; dropping SetRoyalSeedsAbsolute"
                    "(mask=0x%02x)",
                    static_cast<unsigned>(msg.set_royal_seeds_absolute.mask));
            }
            return;
        case InboundKind::SetWonderSeedCounts:
            SMBWAP_LOG_INFO(
                "[grant] received SetWonderSeedCounts counts="
                "[%u,%u,%u,%u,%u,%u,%u,%u], enqueued",
                msg.set_wonder_seed_counts.counts[0],
                msg.set_wonder_seed_counts.counts[1],
                msg.set_wonder_seed_counts.counts[2],
                msg.set_wonder_seed_counts.counts[3],
                msg.set_wonder_seed_counts.counts[4],
                msg.set_wonder_seed_counts.counts[5],
                msg.set_wonder_seed_counts.counts[6],
                msg.set_wonder_seed_counts.counts[7]);
            // Forward to game thread; drainInbound caches into the
            // static g_wonder_seed_counts[8] atomic array used by
            // NerveActivateOnce's per-current-world override tick.
            if (!inboundRing().push(msg)) {
                SMBWAP_LOG_WARN(
                    "[ring] inbound full; dropping SetWonderSeedCounts");
            }
            return;
        case InboundKind::Err:
            SMBWAP_LOG_WARN("[conn] bridge reports err: %s", msg.err.reason);
            return;
        case InboundKind::Pong:
            SMBWAP_LOG_DEBUG("[conn] pong ts_ms=%lld",
                             static_cast<long long>(msg.pong.ts_ms));
            return;
        case InboundKind::None:
            return;
    }
}

}  // namespace smbwap::ap
