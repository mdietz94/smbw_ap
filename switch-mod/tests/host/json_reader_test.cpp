// Host-side (no devkit) unit test for the subsdk JSON reader + the inbound
// wire decoder's u64 fields.  Added 2026-09-21 after a player's Switch log
// showed every ~2 s sync tick failing with
//
//   [smbwap wrn] [conn] decode failed: {"t":"set_wonder_seeds_absolute",
//                                      "bits_lo":18446744069414584320,...
//
// 18446744069414584320 == 0xFFFFFFFF00000000 (W3 + W4 buckets full).  The
// old Reader::nextInt accumulated in int64, wrapped negative on bit 63, and
// parseSetWonderSeedsAbsolute rejected it with `v < 0`.  This test compiles
// Json.cpp + ApProtocol.cpp with a host clang++ and exercises the exact line.
//
// Build + run (from the repo root, LLVM clang++ on PATH):
//
//   clang++ -std=c++20 -O1 -Wall -Wextra -Isrc -Isrc/ap \
//       tests/host/json_reader_test.cpp src/util/Json.cpp src/ap/ApProtocol.cpp \
//       -o build/json_reader_test && ./build/json_reader_test
//
// (run from switch-mod/; on Windows the output is build/json_reader_test.exe)

#include "ap/ApProtocol.hpp"
#include "util/Json.hpp"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

namespace {

int g_failures = 0;
int g_checks = 0;

#define CHECK(cond)                                                              \
    do {                                                                         \
        ++g_checks;                                                              \
        if (!(cond)) {                                                           \
            ++g_failures;                                                        \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);         \
        }                                                                        \
    } while (0)

// The reader decodes escapes in place, so every input goes through a
// writable copy.
struct Line {
    std::string buf;
    explicit Line(const char* s) : buf(s) {}
    char* data() { return buf.data(); }
    std::size_t size() const { return buf.size(); }
};

// ---- Reader::nextUInt64 -------------------------------------------------

bool readU64(const char* text, std::uint64_t& out) {
    Line l(text);
    smbwap::util::json::Reader r(l.data(), l.size());
    return r.nextUInt64(out);
}

bool readI64(const char* text, std::int64_t& out) {
    Line l(text);
    smbwap::util::json::Reader r(l.data(), l.size());
    return r.nextInt(out);
}

void testNextUInt64() {
    std::uint64_t v = 0;

    CHECK(readU64("0", v) && v == 0u);
    CHECK(readU64("42", v) && v == 42u);
    CHECK(readU64("9223372036854775807", v) && v == 0x7FFFFFFFFFFFFFFFull);  // INT64_MAX
    CHECK(readU64("9223372036854775808", v) && v == 0x8000000000000000ull);  // 1 << 63
    CHECK(readU64("18446744069414584320", v) && v == 0xFFFFFFFF00000000ull); // the log value
    CHECK(readU64("18446744073709551615", v) && v == 0xFFFFFFFFFFFFFFFFull); // UINT64_MAX

    // Overflow: UINT64_MAX + 1, and a wildly long digit string.
    CHECK(!readU64("18446744073709551616", v));
    CHECK(!readU64("99999999999999999999999", v));
    // Sign, fraction, exponent, non-numeric.
    CHECK(!readU64("-1", v));
    CHECK(!readU64("1.5", v));
    CHECK(!readU64("1e3", v));
    CHECK(!readU64("\"7\"", v));
    CHECK(!readU64("true", v));
}

void testNextIntOverflowRejected() {
    std::int64_t v = 0;

    CHECK(readI64("0", v) && v == 0);
    CHECK(readI64("-7", v) && v == -7);
    CHECK(readI64("9223372036854775807", v) && v == 0x7FFFFFFFFFFFFFFFll);
    CHECK(readI64("-9223372036854775808", v) && v == (-0x7FFFFFFFFFFFFFFFll - 1));

    // Previously UB (wrapped negative); now a clean reject.
    CHECK(!readI64("9223372036854775808", v));
    CHECK(!readI64("18446744069414584320", v));
    CHECK(!readI64("-9223372036854775809", v));
    CHECK(!readI64("18446744073709551616", v));
}

// ---- decodeInbound on the real wire lines -------------------------------

void testSetWonderSeedsAbsoluteBit63() {
    using namespace smbwap::ap;

    // Verbatim shape of the 2026-09-21 player log line (bits_hi is what a
    // W3+W4-full, W5..-empty player sends: 0).
    {
        Line l("{\"t\":\"set_wonder_seeds_absolute\","
               "\"bits_lo\":18446744069414584320,\"bits_hi\":0}\n");
        InboundMsg m;
        CHECK(decodeInbound(l.data(), l.size(), m));
        CHECK(m.kind == InboundKind::SetWonderSeedsAbsolute);
        CHECK(m.set_wonder_seeds_absolute.bits_lo == 0xFFFFFFFF00000000ull);
        CHECK(m.set_wonder_seeds_absolute.bits_hi == 0u);
    }
    // Both halves saturated (all eight buckets full).
    {
        Line l("{\"t\":\"set_wonder_seeds_absolute\","
               "\"bits_lo\":18446744073709551615,"
               "\"bits_hi\":18446744073709551615}");
        InboundMsg m;
        CHECK(decodeInbound(l.data(), l.size(), m));
        CHECK(m.set_wonder_seeds_absolute.bits_lo == 0xFFFFFFFFFFFFFFFFull);
        CHECK(m.set_wonder_seeds_absolute.bits_hi == 0xFFFFFFFFFFFFFFFFull);
    }
    // Zero / small values still fine.
    {
        Line l("{\"t\":\"set_wonder_seeds_absolute\",\"bits_lo\":1,\"bits_hi\":0}");
        InboundMsg m;
        CHECK(decodeInbound(l.data(), l.size(), m));
        CHECK(m.set_wonder_seeds_absolute.bits_lo == 1u);
    }
    // Out of range / negative / missing field -> rejected.
    {
        Line l("{\"t\":\"set_wonder_seeds_absolute\","
               "\"bits_lo\":18446744073709551616,\"bits_hi\":0}");
        InboundMsg m;
        CHECK(!decodeInbound(l.data(), l.size(), m));
    }
    {
        Line l("{\"t\":\"set_wonder_seeds_absolute\",\"bits_lo\":-1,\"bits_hi\":0}");
        InboundMsg m;
        CHECK(!decodeInbound(l.data(), l.size(), m));
    }
    {
        Line l("{\"t\":\"set_wonder_seeds_absolute\",\"bits_lo\":5}");
        InboundMsg m;
        CHECK(!decodeInbound(l.data(), l.size(), m));
    }
}

void testSetBadgesAbsoluteFullU64() {
    using namespace smbwap::ap;
    {
        Line l("{\"t\":\"set_badges_absolute\",\"bits\":18446744073709551615}");
        InboundMsg m;
        CHECK(decodeInbound(l.data(), l.size(), m));
        CHECK(m.kind == InboundKind::SetBadgesAbsolute);
        CHECK(m.set_badges_absolute.bits == 0xFFFFFFFFFFFFFFFFull);
    }
    {
        Line l("{\"t\":\"set_badges_absolute\",\"bits\":16}");
        InboundMsg m;
        CHECK(decodeInbound(l.data(), l.size(), m));
        CHECK(m.set_badges_absolute.bits == 16u);
    }
    {
        Line l("{\"t\":\"set_badges_absolute\",\"bits\":-1}");
        InboundMsg m;
        CHECK(!decodeInbound(l.data(), l.size(), m));
    }
}

void testSetBadgeShopStateFullU64() {
    using namespace smbwap::ap;
    Line l("{\"t\":\"set_badge_shop_state\","
           "\"managed\":9223372036854775808,\"sold\":18446744073709551615}");
    InboundMsg m;
    CHECK(decodeInbound(l.data(), l.size(), m));
    CHECK(m.kind == InboundKind::SetBadgeShopState);
    CHECK(m.set_badge_shop_state.managed == 0x8000000000000000ull);
    CHECK(m.set_badge_shop_state.sold == 0xFFFFFFFFFFFFFFFFull);
}

void testSetSeedShopStateStillDecodes() {
    // The other "decode failed" line from the same player log.  It parses
    // fine on a current build; the player's failure is an unknown-"t"
    // report from a subsdk older than PR #189 (2026-09-12).
    using namespace smbwap::ap;
    Line l("{\"t\":\"set_seed_shop_state\",\"managed\":261,\"sold\":0}");
    InboundMsg m;
    CHECK(decodeInbound(l.data(), l.size(), m));
    CHECK(m.kind == InboundKind::SetSeedShopState);
    CHECK(m.set_seed_shop_state.managed == 261u);
    CHECK(m.set_seed_shop_state.sold == 0u);
}

}  // namespace

int main() {
    testNextUInt64();
    testNextIntOverflowRejected();
    testSetWonderSeedsAbsoluteBit63();
    testSetBadgesAbsoluteFullU64();
    testSetBadgeShopStateFullU64();
    testSetSeedShopStateStillDecodes();

    std::printf("%d checks, %d failures\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
