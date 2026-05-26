#include "ApState.hpp"

namespace smbwap::ap {

namespace {
OutboundRing g_outbound;
InboundRing g_inbound;
std::atomic<ConnState> g_conn{ConnState::Disconnected};
}

OutboundRing& outboundRing() { return g_outbound; }
InboundRing& inboundRing() { return g_inbound; }
std::atomic<ConnState>& connState() { return g_conn; }

}  // namespace smbwap::ap
