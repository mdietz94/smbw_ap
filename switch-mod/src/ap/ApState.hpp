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

// Fixed-size lock-free ring.  Named "Spsc" historically; as of 2026-07-23
// `pop()` is safe for MULTIPLE concurrent consumers (see the comment on
// pop()).  `push()` is still SINGLE-PRODUCER ONLY -- it reserves and
// publishes buf_[head_] with a blind store, so two concurrent producers
// can write the same slot and lose an event.
//
//   InboundRing  : 1 producer (ApClient rx thread), N consumers (the
//                  drainInbound hook sites).  Correct with the CAS pop.
//   OutboundRing : N producers (util::log -> enqueueLog runs on every
//                  thread that logs), 1 consumer (ApClient worker).  The
//                  producer side is NOT safe -- pre-existing, lossy but
//                  not fatal (indices stay in range; worst case is a
//                  dropped or torn diagnostic event).  Making push()
//                  multi-producer safe needs per-slot sequence numbers,
//                  not just a CAS on head_.
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

    // Multi-consumer safe (2026-07-23).  The original blind
    // `tail_.store(t + 1)` was only correct for ONE consumer, but
    // ap::drainInbound() is installed at four hook sites in main.cpp and
    // SMBW dispatches them from a job pool -- the 2026-07-23 freeze log
    // shows drains from four distinct guest threads (91/92/93/89).  With
    // concurrent consumers the blind store lets a thread holding a stale
    // `t` rewind tail_ behind another, so `t == head_` never comes true
    // and drainInbound's `while (pop(msg))` spins forever.  That spin is
    // silent -- every deduplicated kind breaks without logging -- so the
    // game froze with the log ending mid-drain and no guest exception.
    //
    // CAS on tail_ instead: losers re-read `t` from the failed exchange
    // and retry, so tail_ only ever advances.  The `out = buf_[t]` before
    // the CAS can be wasted work for a loser (it re-copies on retry), but
    // is never a torn read: the producer refuses to push when the ring is
    // full, so it never writes the slot a consumer is currently claiming.
    bool pop(T& out) {
        auto t = tail_.load(std::memory_order_relaxed);
        for (;;) {
            if (t == head_.load(std::memory_order_acquire)) return false;  // empty
            out = buf_[t];
            if (tail_.compare_exchange_weak(t, (t + 1) % N,
                                            std::memory_order_release,
                                            std::memory_order_relaxed)) {
                return true;
            }
            // CAS failed: another consumer won and wrote the current tail
            // back into `t`.  Loop and re-evaluate against head_.
        }
    }

    bool peek(T& out) const {
        const auto t = tail_.load(std::memory_order_relaxed);
        if (t == head_.load(std::memory_order_acquire)) return false;
        out = buf_[t];
        return true;
    }

    void popDiscard() {
        auto t = tail_.load(std::memory_order_relaxed);
        while (t != head_.load(std::memory_order_acquire)) {
            if (tail_.compare_exchange_weak(t, (t + 1) % N,
                                            std::memory_order_release,
                                            std::memory_order_relaxed)) {
                return;
            }
        }
    }

    // Approximate because head_/tail_ are sampled separately.  NOTE: the
    // `% N` here also *hides* an out-of-range index, so a sane-looking
    // depth is not proof the ring is consistent -- don't use this to
    // reason about ring health.
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
