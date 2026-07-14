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
#include "probe/BadgeShop.hpp"
#include "probe/CharaGate.hpp"
#include "probe/ItemGetGate.hpp"
#include "ui/ApDebugConsole.hpp"
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
        // Phase 2g.8 diagnostic: confirm Send succeeded.  Without this,
        // a silent successful Send and a silent enqueue look identical
        // in the log.  Bounded so a long session doesn't flood (first 50
        // sends then 1-in-256).
        static std::atomic<std::uint32_t> s_send_count{0};
        const std::uint32_t c = s_send_count.fetch_add(
            1, std::memory_order_relaxed);
        if (c < 50 || (c & 0xFF) == 0) {
            SMBWAP_LOG_INFO(
                "[ring] outbound sent ok bytes=%d kind=%u (#%u)",
                static_cast<int>(n),
                static_cast<unsigned>(ev.kind), c);
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

namespace {

// Burst-suppress the "[ring] inbound full; dropping ..." WARN family.
// Drops happen when the game thread's drainInbound stalls long enough
// for the bridge's 2 s periodic absolute-state tick to saturate the
// inbound ring (steady state ~3 messages / 2 s once a session is
// active).  We've seen this hit ~1.5 drops/s during long scene-gated
// windows (cutscenes, world map sits) lasting up to 11 minutes
// continuously -- ~1000 identical WARN lines per such window.
//
// The first drop in a burst is the actionable signal (marks the
// start of an abnormally-long gated window); the rest are noise.  We
// log the first drop and a single "burst ended" summary when
// drainInbound catches back up (detected as the next successful push
// after a burst started).  WARN level is preserved -- the events
// remain interesting at a glance.
//
// Atomic flags so the network rx thread (this file) doesn't race with
// itself across consecutive messages; the game thread only ever
// pop()s from the ring, never push()es, so no cross-thread coord.
std::atomic<bool>     g_drop_burst_active{false};
std::atomic<uint64_t> g_drop_burst_count{0};

// Returns true iff the push succeeded.  Closes any active drop burst
// on success.  Call this in place of `inboundRing().push(msg)`.
bool tryPushInbound(const InboundMsg& msg) {
    if (!inboundRing().push(msg)) return false;
    if (g_drop_burst_active.exchange(false, std::memory_order_relaxed)) {
        const auto count = g_drop_burst_count.exchange(
            0, std::memory_order_relaxed);
        SMBWAP_LOG_WARN(
            "[ring] inbound burst ended: %llu drop(s) total since first WARN",
            static_cast<unsigned long long>(count));
    }
    return true;
}

// Returns true iff this is the first drop of a new burst (caller logs
// its kind-specific WARN); subsequent drops in the burst are counted
// silently and surfaced by the burst-ended summary above.
bool shouldLogDrop() {
    const bool was_active = g_drop_burst_active.exchange(
        true, std::memory_order_relaxed);
    g_drop_burst_count.fetch_add(1, std::memory_order_relaxed);
    return !was_active;
}

}  // namespace

void ApClient::handleLine(char* line, std::size_t len) {
    InboundMsg msg;
    if (!decodeInbound(line, len, msg)) {
        SMBWAP_LOG_WARN("[conn] decode failed: %.80s", line);
        return;
    }
    // Per-session dedupe for the absolute-state "[grant] received Set*"
    // log lines: the bridge re-pushes the same triplet every 2 s as
    // its idempotent backstop, producing thousands of identical INFO
    // lines.  Track last-logged payloads and skip the log when nothing
    // changed.  Reset in the HelloAck arm so a fresh bridge session
    // re-logs the initial sync (matches the bridge-side dedupe in
    // lan_server.py _writer_loop).
    static bool        s_have_badges  = false;
    static std::uint64_t s_last_badges  = 0;
    static bool        s_have_seeds   = false;
    static std::uint8_t  s_last_seeds   = 0;
    static bool        s_have_counts  = false;
    static std::uint32_t s_last_counts[8] = {0};
    // 2026-05-29 -- per-course Wonder Seed bitfield dedup.
    static bool        s_have_ws_bits = false;
    static std::uint64_t s_last_ws_lo  = 0;
    static std::uint64_t s_last_ws_hi  = 0;
    // 2026-06 -- open-world routable-world mask dedup.
    static bool          s_have_routable = false;
    static std::uint16_t s_last_routable = 0;
    // 2026-06-30 -- open-world force-cleared-courses mask dedup.
    static bool          s_have_force_cleared = false;
    static std::uint16_t s_last_force_cleared = 0;
    switch (msg.kind) {
        case InboundKind::HelloAck:
            SMBWAP_LOG_INFO(
                "[conn] HELLO acked: ok=%s bridge_ver=%s wire_ver=%d",
                msg.hello_ack.ok ? "true" : "false",
                msg.hello_ack.bridge_ver, msg.hello_ack.wire_ver);
            if (!msg.hello_ack.ok && msg.hello_ack.reason[0]) {
                SMBWAP_LOG_WARN("[conn] HELLO refused: %s", msg.hello_ack.reason);
            }
            // New session -> re-log initial absolute-state pushes.
            s_have_badges = false;
            s_have_seeds  = false;
            s_have_counts = false;
            s_have_ws_bits = false;
            s_have_routable = false;
            s_have_force_cleared = false;
            return;
        case InboundKind::SetBadgesAbsolute: {
            const std::uint64_t bits = msg.set_badges_absolute.bits;
            if (!s_have_badges || bits != s_last_badges) {
                SMBWAP_LOG_INFO(
                    "[grant] received SetBadgesAbsolute(bits=0x%016llx), enqueued",
                    static_cast<unsigned long long>(bits));
                s_last_badges = bits;
                s_have_badges = true;
            }
            // Forward to the game thread via the inbound ring;
            // ApFrameBridge::drainInbound applies it via
            // setBadgeBitfieldAbsolute.
            if (!tryPushInbound(msg) && shouldLogDrop()) {
                SMBWAP_LOG_WARN(
                    "[ring] inbound full; dropping SetBadgesAbsolute(bits=0x%016llx)",
                    static_cast<unsigned long long>(bits));
            }
            return;
        }
        case InboundKind::GrantHashKeyed:
            SMBWAP_LOG_INFO(
                "[grant] received GrantHashKeyed(hash=0x%08x, value=%u), enqueued",
                msg.grant_hash_keyed.hash, msg.grant_hash_keyed.value);
            // Forward to game thread; drainInbound applies via
            // probe::grantContainerACounter -> FUN_710049F648.
            if (!tryPushInbound(msg) && shouldLogDrop()) {
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
            if (!tryPushInbound(msg) && shouldLogDrop()) {
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
            if (!tryPushInbound(msg) && shouldLogDrop()) {
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
            if (!tryPushInbound(msg) && shouldLogDrop()) {
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
            if (!tryPushInbound(msg) && shouldLogDrop()) {
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
            if (!tryPushInbound(msg) && shouldLogDrop()) {
                SMBWAP_LOG_WARN(
                    "[ring] inbound full; dropping SetContainerCBit"
                    "(hash=0x%08x, bit=%u)",
                    msg.set_container_c_bit.hash,
                    msg.set_container_c_bit.bit_index);
            }
            return;
        case InboundKind::SetRoyalSeedsAbsolute: {
            const std::uint8_t mask = msg.set_royal_seeds_absolute.mask;
            if (!s_have_seeds || mask != s_last_seeds) {
                SMBWAP_LOG_INFO(
                    "[grant] received SetRoyalSeedsAbsolute(mask=0x%02x), enqueued",
                    static_cast<unsigned>(mask));
                s_last_seeds = mask;
                s_have_seeds = true;
            }
            // Forward to game thread; drainInbound loops the 6 Royal
            // Seed hashes and grants/clears each per bit via
            // probe::grantContainerBBool.
            if (!tryPushInbound(msg) && shouldLogDrop()) {
                SMBWAP_LOG_WARN(
                    "[ring] inbound full; dropping SetRoyalSeedsAbsolute"
                    "(mask=0x%02x)",
                    static_cast<unsigned>(mask));
            }
            return;
        }
        case InboundKind::SetWonderSeedsAbsolute: {
            // 2026-05-29 -- AP-authoritative per-course Wonder Seed
            // bitfield (container-C hash 0x60458608).  drainInbound
            // dedups + applies via probe::setWonderSeedBitfieldAbsolute.
            const std::uint64_t lo = msg.set_wonder_seeds_absolute.bits_lo;
            const std::uint64_t hi = msg.set_wonder_seeds_absolute.bits_hi;
            if (!s_have_ws_bits || lo != s_last_ws_lo || hi != s_last_ws_hi) {
                SMBWAP_LOG_INFO(
                    "[grant] received SetWonderSeedsAbsolute"
                    "(lo=0x%016llx, hi=0x%016llx), enqueued",
                    static_cast<unsigned long long>(lo),
                    static_cast<unsigned long long>(hi));
                s_last_ws_lo = lo;
                s_last_ws_hi = hi;
                s_have_ws_bits = true;
            }
            if (!tryPushInbound(msg) && shouldLogDrop()) {
                SMBWAP_LOG_WARN(
                    "[ring] inbound full; dropping SetWonderSeedsAbsolute"
                    "(lo=0x%016llx, hi=0x%016llx)",
                    static_cast<unsigned long long>(lo),
                    static_cast<unsigned long long>(hi));
            }
            return;
        }
        case InboundKind::SetWonderSeedCounts: {
            const auto& counts = msg.set_wonder_seed_counts.counts;
            bool changed = !s_have_counts;
            if (!changed) {
                for (int i = 0; i < 8; ++i) {
                    if (counts[i] != s_last_counts[i]) { changed = true; break; }
                }
            }
            if (changed) {
                SMBWAP_LOG_INFO(
                    "[grant] received SetWonderSeedCounts counts="
                    "[%u,%u,%u,%u,%u,%u,%u,%u], enqueued",
                    counts[0], counts[1], counts[2], counts[3],
                    counts[4], counts[5], counts[6], counts[7]);
                for (int i = 0; i < 8; ++i) s_last_counts[i] = counts[i];
                s_have_counts = true;
            }
            // Forward to game thread; drainInbound caches into the
            // static g_wonder_seed_counts[8] atomic array used by
            // NerveActivateOnce's per-current-world override tick.
            if (!tryPushInbound(msg) && shouldLogDrop()) {
                SMBWAP_LOG_WARN(
                    "[ring] inbound full; dropping SetWonderSeedCounts");
            }
            return;
        }
        case InboundKind::OverlayNotice:
            // Force the on-Switch debug overlay visible + show the notice.
            // Applied straight from this rx thread: setOverlayNotice only
            // writes the overlay's notice state (an atomic expiry + a small
            // char buffer the render thread reads), so -- unlike the grant
            // messages -- it does NOT go through the game-thread inbound ring.
            SMBWAP_LOG_DEBUG(
                "[overlay] received OverlayNotice(ttl_ms=%d): %.64s",
                msg.overlay_notice.ttl_ms, msg.overlay_notice.text);
            ui::setOverlayNotice(msg.overlay_notice.text,
                                 msg.overlay_notice.ttl_ms);
            return;
        case InboundKind::SetRoutableWorldsAbsolute: {
            // 2026-06 -- open-world routability.  drainInbound caches into
            // g_routable_world_mask; the FUN_7100935ce0 trampoline forces
            // matching worlds routable.
            const std::uint16_t mask = msg.set_routable_worlds_absolute.mask;
            if (!s_have_routable || mask != s_last_routable) {
                SMBWAP_LOG_INFO(
                    "[grant] received SetRoutableWorldsAbsolute(mask=0x%03x), "
                    "enqueued", static_cast<unsigned>(mask));
                s_last_routable = mask;
                s_have_routable = true;
            }
            if (!tryPushInbound(msg) && shouldLogDrop()) {
                SMBWAP_LOG_WARN(
                    "[ring] inbound full; dropping SetRoutableWorldsAbsolute"
                    "(mask=0x%03x)", static_cast<unsigned>(mask));
            }
            return;
        }
        case InboundKind::SetForceClearedCourses: {
            // 2026-06-30 -- open-world secret-exit unlock.  drainInbound
            // caches into g_force_cleared_courses_mask; the SceneTransition
            // hook writes IsInClearedCourse for a matching course.
            const std::uint16_t mask = msg.set_force_cleared_courses.mask;
            if (!s_have_force_cleared || mask != s_last_force_cleared) {
                SMBWAP_LOG_INFO(
                    "[grant] received SetForceClearedCourses(mask=0x%04x), "
                    "enqueued", static_cast<unsigned>(mask));
                s_last_force_cleared = mask;
                s_have_force_cleared = true;
            }
            if (!tryPushInbound(msg) && shouldLogDrop()) {
                SMBWAP_LOG_WARN(
                    "[ring] inbound full; dropping SetForceClearedCourses"
                    "(mask=0x%04x)", static_cast<unsigned>(mask));
            }
            return;
        }
        case InboundKind::ApplyWorldUnlock:
            // Open-world world/course unlock batch (2026-06).  drainInbound
            // routes by GameDataList category: count Int hashes via
            // probe::grantContainerACounter(hash, 1) and bool_count Bool
            // hashes via probe::grantContainerBBool(hash, 1).  Not
            // deduplicated: sent once at connect + HelloMsg replay, not on
            // the 2s tick.
            SMBWAP_LOG_INFO(
                "[unlock] received ApplyWorldUnlock(int=%u bool=%u), enqueued",
                static_cast<unsigned>(msg.apply_world_unlock.count),
                static_cast<unsigned>(msg.apply_world_unlock.bool_count));
            if (!tryPushInbound(msg) && shouldLogDrop()) {
                SMBWAP_LOG_WARN(
                    "[unlock] inbound full; dropping ApplyWorldUnlock"
                    "(int=%u bool=%u)",
                    static_cast<unsigned>(msg.apply_world_unlock.count),
                    static_cast<unsigned>(msg.apply_world_unlock.bool_count));
            }
            return;
        case InboundKind::SetItemGetDenyMask:
            // Power-up pickup negation (M3.1 / M5 groundwork).  Applied
            // directly on the network thread -- setDeniedItemGetMask is a
            // single atomic store consumed by the ItemGetMaskBuild
            // trampoline on the game thread, so no ring trip is needed
            // (same direct-apply pattern as OverlayNotice).
            SMBWAP_LOG_INFO(
                "[itemgate] received SetItemGetDenyMask(mask=0x%08x)",
                msg.set_itemget_deny_mask.mask);
            probe::setDeniedItemGetMask(msg.set_itemget_deny_mask.mask);
            return;
        case InboundKind::SetUnlockedCharas:
            // AP-authoritative character-selection gate (2026-07-08).
            // Applied directly on the network thread -- setUnlockedCharaMask
            // is a single atomic store consumed by the CharaSelectCommit
            // trampoline + the charaGateTick sweep on the game thread, so
            // no ring trip is needed (same direct-apply pattern as
            // SetItemGetDenyMask).
            SMBWAP_LOG_INFO(
                "[charagate] received SetUnlockedCharas(mask=0x%03x)",
                msg.set_unlocked_charas.mask);
            probe::setUnlockedCharaMask(msg.set_unlocked_charas.mask);
            return;
        case InboundKind::SetBadgeShopState:
            // AP-authoritative Poplin badge-shop ownership (2026-06-10).
            // Applied directly on the network thread -- setBadgeShopState is
            // two atomic stores consumed by the computeItemStates trampoline
            // on the game thread, so no ring trip is needed (same
            // direct-apply pattern as SetItemGetDenyMask).
            SMBWAP_LOG_INFO(
                "[badgeshop] received SetBadgeShopState(managed=0x%016llx "
                "sold=0x%016llx)",
                static_cast<unsigned long long>(msg.set_badge_shop_state.managed),
                static_cast<unsigned long long>(msg.set_badge_shop_state.sold));
            probe::setBadgeShopState(msg.set_badge_shop_state.managed,
                                     msg.set_badge_shop_state.sold);
            return;
        case InboundKind::SetBadgeShopText:
            // AP shop-text: custom per-badge description shown in the shop
            // detail panel.  Applied straight on the rx thread (atomic
            // store of the converted UTF-16 buffer), consumed by the
            // msbt-resolver trampoline on the game thread.
            SMBWAP_LOG_DEBUG(
                "[badgeshop] received SetBadgeShopText(id=%u): %.48s",
                msg.set_badge_shop_text.id, msg.set_badge_shop_text.text);
            probe::setBadgeShopText(msg.set_badge_shop_text.id,
                                    msg.set_badge_shop_text.text);
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
