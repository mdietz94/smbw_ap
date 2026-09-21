// Host-side (no devkit) unit test for the subsdk JSON reader + the inbound
// wire decoder.  Compiles Json.cpp + ApProtocol.cpp with a host clang++ and
// exercises real wire lines, so a decoder regression is caught without a
// Ryujinx round-trip.
//
// Build + run (from switch-mod/, LLVM clang++ on PATH):
//
//   clang++ -std=c++20 -O1 -Wall -Wextra -Isrc -Isrc/ap \
//       tests/host/json_reader_test.cpp src/util/Json.cpp src/ap/ApProtocol.cpp \
//       -o build/json_reader_test && ./build/json_reader_test
//
// (on Windows the output is build/json_reader_test.exe)
//
// Coverage:
//   * Reader::skipValue -- the "unknown field" primitive.  Added 2026-09-21:
//     the old parseXxx() idiom chained isNull/nextString/nextInt/nextBool,
//     but nextString() set the sticky error flag on any non-'"' first byte,
//     so an unknown int/bool field from a newer client made an older subsdk
//     reject the whole line ("[conn] decode failed") -- the opposite of the
//     forward compatibility the branch existed for.
//   * decodeInbound on known messages carrying extra future fields of every
//     JSON type, which must still decode.
//   * Reader::nextUInt64 + int64 overflow rejection (PR #195, 2026-09-21):
//     a player's log showed every ~2 s sync tick failing with
//       [conn] decode failed: {"t":"set_wonder_seeds_absolute",
//                              "bits_lo":18446744069414584320,...
//     18446744069414584320 == 0xFFFFFFFF00000000 (W3 + W4 buckets full).
//     nextInt accumulated in int64, wrapped negative on bit 63, and the
//     parser rejected it with `v < 0`.  u64 wire fields now go through
//     nextUInt64; these tests exercise the exact lines.

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

using smbwap::util::json::Reader;

// ---- Reader::skipValue ---------------------------------------------------

// Skip the first value of `text`, then require that the reader is still
// healthy and positioned exactly before the trailing sentinel int 99.
// (A sticky error or a mis-positioned cursor makes the sentinel read fail.)
bool skipThenSentinel(const char* text) {
    Line l(text);
    Reader r(l.data(), l.size());
    if (!r.enterArray()) return false;
    if (!r.skipValue()) return false;
    std::int64_t v = 0;
    if (!r.nextInt(v)) return false;
    if (v != 99) return false;
    return r.exitArray();
}

bool skipFails(const char* text) {
    Line l(text);
    Reader r(l.data(), l.size());
    return !r.skipValue();
}

void testSkipValueEveryType() {
    // Scalars.
    CHECK(skipThenSentinel("[\"str\", 99]"));
    CHECK(skipThenSentinel("[\"esc \\\" \\\\ \\u0041 ] } , \", 99]"));  // brackets/comma inside a string
    CHECK(skipThenSentinel("[\"\", 99]"));
    CHECK(skipThenSentinel("[7, 99]"));
    CHECK(skipThenSentinel("[-7, 99]"));
    CHECK(skipThenSentinel("[1.5, 99]"));
    CHECK(skipThenSentinel("[1e3, 99]"));
    CHECK(skipThenSentinel("[-2.5E-3, 99]"));
    CHECK(skipThenSentinel("[18446744073709551615, 99]"));  // > INT64_MAX: skipped, never parsed
    CHECK(skipThenSentinel("[true, 99]"));
    CHECK(skipThenSentinel("[false, 99]"));
    CHECK(skipThenSentinel("[null, 99]"));

    // Containers, nested, with strings that contain bracket bytes.
    CHECK(skipThenSentinel("[{}, 99]"));
    CHECK(skipThenSentinel("[[], 99]"));
    CHECK(skipThenSentinel("[{\"a\":1,\"b\":[1,2,{\"c\":\"]}\"}],\"d\":null}, 99]"));
    CHECK(skipThenSentinel("[[[[[1]]]], 99]"));
    CHECK(skipThenSentinel("[ { \"k\" : [ true , false ] } , 99 ]"));  // whitespace everywhere

    // Malformed input is still a hard failure.
    CHECK(skipFails(""));
    CHECK(skipFails("   "));
    CHECK(skipFails("\"unterminated"));
    CHECK(skipFails("\"bad escape at end\\"));
    CHECK(skipFails("{\"a\":1"));       // unbalanced
    CHECK(skipFails("[1,2"));
    CHECK(skipFails("}"));               // stray close
    CHECK(skipFails("x"));               // unknown byte
    CHECK(skipFails("tru"));             // truncated literal
    // Nesting deeper than kMaxSkipDepth is rejected, not stack-walked.
    CHECK(skipFails("[[[[[[[[[[[[[[[[[[[[1]]]]]]]]]]]]]]]]]]]]"));  // 20 deep
}

void testSkipValueLeavesReaderHealthy() {
    // After skipping, the reader must not carry a sticky error and must
    // honour array comma handling on both sides of the skipped value.
    Line l("[1, {\"x\":[1,2]}, \"s\", 4]");
    Reader r(l.data(), l.size());
    std::int64_t v = 0;
    CHECK(r.enterArray());
    CHECK(r.nextInt(v) && v == 1);
    CHECK(r.skipValue());
    CHECK(r.skipValue());
    CHECK(r.nextInt(v) && v == 4);
    CHECK(!r.hasMoreInArray());
    CHECK(r.exitArray());
}

void testSkipValueInObjectFieldPosition() {
    // The real call site: cursor just after "key": inside an object.
    Line l("{\"a\":{\"deep\":[null,true]},\"b\":-12.5,\"c\":\"z\",\"d\":7}");
    Reader r(l.data(), l.size());
    std::string_view key;
    std::int64_t d = 0;
    int fields = 0;
    CHECK(r.enterObject());
    while (r.nextField(key)) {
        ++fields;
        if (key == "d") {
            CHECK(r.nextInt(d));
        } else {
            CHECK(r.skipValue());
        }
    }
    CHECK(fields == 4);
    CHECK(d == 7);
    CHECK(r.exitObject());
}

// ---- decodeInbound: known messages with future fields --------------------

void testDecodeInboundToleratesFutureIntAndBool() {
    using namespace smbwap::ap;
    // The exact scenario from the bug report: a newer client adds an int and
    // a bool field to a message an older subsdk already understands.
    {
        Line l("{\"t\":\"set_badges_absolute\",\"bits\":16,"
               "\"future_int\":7,\"future_bool\":true}\n");
        InboundMsg m;
        CHECK(decodeInbound(l.data(), l.size(), m));
        CHECK(m.kind == InboundKind::SetBadgesAbsolute);
        CHECK(m.set_badges_absolute.bits == 16u);
    }
    // Unknown fields BEFORE the known one -- order must not matter.
    {
        Line l("{\"t\":\"set_badges_absolute\","
               "\"future_int\":7,\"future_bool\":true,\"bits\":16}");
        InboundMsg m;
        CHECK(decodeInbound(l.data(), l.size(), m));
        CHECK(m.set_badges_absolute.bits == 16u);
    }
}

void testDecodeInboundToleratesEveryFutureType() {
    using namespace smbwap::ap;
    // kill: required string fields + one optional bool, plus one unknown
    // field of every JSON type interleaved.
    Line l("{\"t\":\"kill\","
           "\"f_null\":null,"
           "\"source\":\"Alice\","
           "\"f_neg\":-3,"
           "\"f_float\":2.75,"
           "\"immediate\":true,"
           "\"f_false\":false,"
           "\"f_arr\":[1,\"two\",{\"three\":3}],"
           "\"cause\":\"fell\","
           "\"f_obj\":{\"nested\":{\"deeper\":[null]}},"
           "\"f_str\":\"] } \\\" \"}");
    InboundMsg m;
    CHECK(decodeInbound(l.data(), l.size(), m));
    CHECK(m.kind == InboundKind::Kill);
    CHECK(std::strcmp(m.kill.source, "Alice") == 0);
    CHECK(std::strcmp(m.kill.cause, "fell") == 0);
    CHECK(m.kill.immediate);
}

void testDecodeInboundOtherParsersTolerateFutureInt() {
    using namespace smbwap::ap;
    {
        Line l("{\"t\":\"set_seed_shop_state\",\"managed\":261,\"sold\":0,"
               "\"future_int\":7}");
        InboundMsg m;
        CHECK(decodeInbound(l.data(), l.size(), m));
        CHECK(m.kind == InboundKind::SetSeedShopState);
        CHECK(m.set_seed_shop_state.managed == 261u);
        CHECK(m.set_seed_shop_state.sold == 0u);
    }
    {
        Line l("{\"t\":\"hello_ack\",\"future_bool\":false,\"future_int\":1}");
        InboundMsg m;
        // hello_ack has no required fields on this build; it must decode.
        CHECK(decodeInbound(l.data(), l.size(), m));
        CHECK(m.kind == InboundKind::HelloAck);
    }
    {
        Line l("{\"t\":\"pong\",\"future_arr\":[1,2,3]}");
        InboundMsg m;
        CHECK(decodeInbound(l.data(), l.size(), m));
        CHECK(m.kind == InboundKind::Pong);
    }
}

void testDecodeInboundStillRejectsBadLines() {
    using namespace smbwap::ap;
    // Skipping unknown fields must not loosen validation of known ones.
    {
        Line l("{\"t\":\"set_badges_absolute\",\"bits\":\"sixteen\",\"future_int\":7}");
        InboundMsg m;
        CHECK(!decodeInbound(l.data(), l.size(), m));
    }
    {
        Line l("{\"t\":\"set_badges_absolute\",\"future_int\":7}");  // bits missing
        InboundMsg m;
        CHECK(!decodeInbound(l.data(), l.size(), m));
    }
    {
        Line l("{\"t\":\"set_badges_absolute\",\"bits\":16,\"future\":[1,2}");  // malformed
        InboundMsg m;
        CHECK(!decodeInbound(l.data(), l.size(), m));
    }
    // Unknown "t" from a newer client: still dropped (false), never a crash,
    // even when its fields are ints/bools/containers.
    {
        Line l("{\"t\":\"from_the_future\",\"n\":7,\"b\":true,\"o\":{\"a\":[1]}}");
        InboundMsg m;
        CHECK(!decodeInbound(l.data(), l.size(), m));
    }
}

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
    testSkipValueEveryType();
    testSkipValueLeavesReaderHealthy();
    testSkipValueInObjectFieldPosition();
    testDecodeInboundToleratesFutureIntAndBool();
    testDecodeInboundToleratesEveryFutureType();
    testDecodeInboundOtherParsersTolerateFutureInt();
    testDecodeInboundStillRejectsBadLines();
    testNextUInt64();
    testNextIntOverflowRejected();
    testSetWonderSeedsAbsoluteBit63();
    testSetBadgesAbsoluteFullU64();
    testSetBadgeShopStateFullU64();
    testSetSeedShopStateStillDecodes();

    std::printf("%d checks, %d failures\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
