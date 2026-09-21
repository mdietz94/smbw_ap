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

}  // namespace

int main() {
    testSkipValueEveryType();
    testSkipValueLeavesReaderHealthy();
    testSkipValueInObjectFieldPosition();
    testDecodeInboundToleratesFutureIntAndBool();
    testDecodeInboundToleratesEveryFutureType();
    testDecodeInboundOtherParsersTolerateFutureInt();
    testDecodeInboundStillRejectsBadLines();

    std::printf("%d checks, %d failures\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
