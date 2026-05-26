// In-process state shared by the LAN worker thread and the game thread.
//
// - Outbound SPSC ring  (game thread -> worker thread): Nerve fires and
//   PlayReport captures get pushed here from M1/M2.4 hook callbacks; the
//   worker thread drains and JSON-encodes onto the TCP socket.
//
// - Inbound SPSC ring   (worker thread -> game thread): SetBadgesAbsolute
//   and GrantHashKeyed commands from the bridge land here;
//   ApFrameBridge::drainInbound() pulls them on the game thread and calls
//   probe::setBadgeBitfieldAbsolute / probe::grantContainerACounter.
//
// SpscRing template ported from smo_archipelago/switch-mod/src/ap/ApState.hpp
// (lines 72-131).  Lock-free, allocation-free, fixed-size.
//
// NO std::mutex, NO thread_local (per CLAUDE.md gotcha #1 / #4).

#pragma once

#include <array>
#include <atomic>
#include <cstdint>

#include "ApProtocol.hpp"

namespace smbwap::ap {

enum class ConnState : std::uint8_t {
    Disconnected = 0,
    Connecting = 1,
    Hello = 2,
    Ready = 3,
};

template <typename T, std::size_t N>
class SpscRing {
public:
    bool push(const T& v) {
        const auto h = head_.load(std::memory_order_relaxed);
        const auto next = (h + 1) % N;
        if (next == tail_.load(std::memory_order_acquire)) return false;  // full
        buf_[h] = v;
        head_.store(next, std::memory_order_release);
        return true;
    }

    bool pop(T& out) {
        const auto t = tail_.load(std::memory_order_relaxed);
        if (t == head_.load(std::memory_order_acquire)) return false;  // empty
        out = buf_[t];
        tail_.store((t + 1) % N, std::memory_order_release);
        return true;
    }

    bool peek(T& out) const {
        const auto t = tail_.load(std::memory_order_relaxed);
        if (t == head_.load(std::memory_order_acquire)) return false;
        out = buf_[t];
        return true;
    }

    void popDiscard() {
        const auto t = tail_.load(std::memory_order_relaxed);
        tail_.store((t + 1) % N, std::memory_order_release);
    }

    std::size_t pendingApprox() const {
        const auto h = head_.load(std::memory_order_acquire);
        const auto t = tail_.load(std::memory_order_relaxed);
        return (h + N - t) % N;
    }

private:
    std::array<T, N> buf_{};
    std::atomic<std::size_t> head_{0};
    std::atomic<std::size_t> tail_{0};
};

// Ring sizes:
//   outbound: 256 entries.  Each is an OutboundEvent (largest variant is
//             WirePlayReport ~1.5 KB), total ~400 KB.  256-deep is enough
//             that the bridge being down for the full 30 s backoff cap
//             holds an entire normal play session's events.
//   inbound : 256 entries.  Bumped from 64 in M4.5: on cold-boot the
//             bridge replay floods grants onto the wire BEFORE the
//             player has selected a save (ApClient connects pre-save-
//             select).  drainInbound is gated on s_save_loaded until
//             the save-load hook fires, so the ring must hold the full
//             replay backlog -- badges + Royal Seeds + future M5 items.
//             SpscRing uses modulo wrap so 256 isn't strictly required
//             to be a power of two; pick a round generous number.
inline constexpr std::size_t kOutboundCap = 256;
inline constexpr std::size_t kInboundCap = 256;

using OutboundRing = SpscRing<OutboundEvent, kOutboundCap>;
using InboundRing = SpscRing<InboundMsg, kInboundCap>;

// Singleton accessor pair.  ApFrameBridge and ApClient share the rings;
// both call here.  Memory is statically allocated -- no construction
// timing concerns.
OutboundRing& outboundRing();
InboundRing& inboundRing();

// Coarse connection state for diagnostic logging.  Worker thread mutates;
// any thread can read.
std::atomic<ConnState>& connState();

}  // namespace smbwap::ap
