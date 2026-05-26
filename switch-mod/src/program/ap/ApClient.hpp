// TCP client to the PC bridge.
//
// Owns a single TCP connection on a dedicated worker thread.  The game
// thread (Nerve / PrepoSaveReport callbacks) only touches the SPSC rings
// in ApState; the worker does all blocking I/O.
//
// Lifecycle:
//   1. Initialize nn::socket pre-orig in GameFrameworkInitialize::Callback
//      (handled in main.cpp -- the worker assumes it's already up).
//   2. ApClient::instance().start() AFTER the network is ready -- the
//      worker thread then runs the reconnect loop until process exit.
//
// Reconnect backoff: 1s -> 2s -> 5s -> 10s -> 30s cap.

#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>

#include "../lib/nx/types.h"

#include "ApDiscovery.hpp"

namespace smbwap::ap {

inline constexpr std::size_t kInboundLineCap = 8 * 1024;

class ApClient {
public:
    static ApClient& instance();

    // Spawn the worker thread.  Idempotent: subsequent calls are no-ops.
    // Call from the game thread (GameFrameworkInitialize::Callback) after
    // nn::socket is up.
    void start();

private:
    ApClient() = default;

    void threadMain();
    bool connectOnce(const BridgeTarget& target);
    void disconnect();
    void sendHello();
    void pumpOutbound();
    bool recvIntoBuf();
    bool popLine(char* out, std::size_t& out_len);
    void handleLine(char* line, std::size_t len);

    std::atomic<bool> started_{false};
    std::int32_t socket_fd_ = -1;
    char read_buf_[kInboundLineCap]{};
    std::size_t read_buf_len_ = 0;

    // The worker entry function trampolines into threadMain.
    friend void apClientWorkerEntry(void* arg);
};

}  // namespace smbwap::ap
