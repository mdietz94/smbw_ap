#include "ApProtocol.hpp"

#include <cstring>

namespace smbwap::ap {

const char* toWire(NerveKind k) {
    switch (k) {
        case NerveKind::WonderSeedAwarded: return "wonder_seed_awarded";
        case NerveKind::CourseCleared:     return "course_cleared";
        case NerveKind::DeathDetected:     return "death_detected";
        case NerveKind::GameGoalReached:   return "game_goal_reached";
    }
    return "unknown";
}

namespace {

// Hex-encode `n` bytes from `src` into the encoder as the value of the
// current key.  Lowercase nibbles.  Matches Python's `bytes.hex()`.
void appendHexValue(util::json::Encoder& enc, const std::uint8_t* src, std::size_t n) {
    char tmp[2 * 64];  // chunk through the LineBuffer in 64-byte bursts
    static const char hex[] = "0123456789abcdef";
    // Encoder::value(string_view) writes the wrapping quotes; build the full
    // hex string in our own LineBuffer-fed chunks via repeated value() would
    // re-quote each one.  Instead encode directly: emit one big string by
    // chunking through Encoder's value(string_view) once.  We need an
    // intermediate fixed buffer.
    char buf[2 * kPayloadCap + 4];
    std::size_t take = (n <= kPayloadCap) ? n : kPayloadCap;
    for (std::size_t i = 0; i < take; ++i) {
        buf[2 * i + 0] = hex[(src[i] >> 4) & 0xF];
        buf[2 * i + 1] = hex[src[i] & 0xF];
    }
    enc.value(std::string_view(buf, 2 * take));
    (void)tmp;
}

}  // namespace

void encodeHello(util::json::LineBuffer& out, const WireHello& msg) {
    util::json::Encoder e(out);
    e.beginObject();
        e.key("t").value("hello");
        e.key("wire_ver").value(1);
        e.key("mod_ver").value(msg.mod_ver);
        e.key("game_ver").value(msg.game_ver);
        e.key("pid").value(static_cast<int>(msg.pid));
    e.endObject();
    out.append('\n');
}

void encodeNerveFire(util::json::LineBuffer& out, const WireNerveFire& msg) {
    util::json::Encoder e(out);
    e.beginObject();
        e.key("t").value("nerve");
        e.key("kind").value(toWire(msg.kind));
        e.key("seq").value(static_cast<int>(msg.seq));
    e.endObject();
    out.append('\n');
}

void encodeBadgeAcquired(util::json::LineBuffer& out, const WireBadgeAcquired& msg) {
    util::json::Encoder e(out);
    e.beginObject();
        e.key("t").value("badge_acquired");
        e.key("internal_id").value(static_cast<int>(msg.internal_id));
        e.key("seq").value(static_cast<int>(msg.seq));
    e.endObject();
    out.append('\n');
}

void encodePlayReport(util::json::LineBuffer& out, const WirePlayReport& msg) {
    util::json::Encoder e(out);
    e.beginObject();
        e.key("t").value("play_report");
        e.key("room").value(msg.room);
        e.key("payload");
        appendHexValue(e, msg.payload, msg.payload_len);
    e.endObject();
    out.append('\n');
}

void encodePing(util::json::LineBuffer& out, const WirePing& msg) {
    util::json::Encoder e(out);
    e.beginObject();
        e.key("t").value("ping");
        e.key("ts_ms").value(msg.ts_ms);
    e.endObject();
    out.append('\n');
}

void encodePong(util::json::LineBuffer& out, std::int64_t ts_ms) {
    util::json::Encoder e(out);
    e.beginObject();
        e.key("t").value("pong");
        e.key("ts_ms").value(ts_ms);
    e.endObject();
    out.append('\n');
}

void encodeLog(util::json::LineBuffer& out, const WireLog& msg) {
    util::json::Encoder e(out);
    e.beginObject();
        e.key("t").value("log");
        e.key("level").value(msg.level);
        e.key("msg").value(msg.msg);
    e.endObject();
    out.append('\n');
}

bool encodeOutbound(util::json::LineBuffer& out, const OutboundEvent& ev) {
    switch (ev.kind) {
        case OutboundKind::Hello:         encodeHello(out, ev.hello); return true;
        case OutboundKind::NerveFire:     encodeNerveFire(out, ev.nerve); return true;
        case OutboundKind::PlayReport:    encodePlayReport(out, ev.play_report); return true;
        case OutboundKind::Ping:          encodePing(out, ev.ping); return true;
        case OutboundKind::BadgeAcquired: encodeBadgeAcquired(out, ev.badge_acquired); return true;
        case OutboundKind::Log:           encodeLog(out, ev.log); return true;
        case OutboundKind::None:
        default:
            return false;
    }
}

// ---- Inbound decoder ---------------------------------------------------

namespace {

bool sv_eq(std::string_view sv, const char* lit) {
    return sv.size() == std::strlen(lit) && std::memcmp(sv.data(), lit, sv.size()) == 0;
}

bool parseSetBadgesAbsolute(util::json::Reader& r, WireSetBadgesAbsolute& out) {
    // Cursor is positioned just after the "t":"set_badges_absolute" pair
    // within the same object.  `bits` is the absolute u64 badge bitfield.
    // The bridge validates [0, 2**64); we accept any non-negative int64
    // (a u64 mask above 2**63 would silently truncate on JSON parse, but
    // realistic badge registries never approach 2**24).
    std::string_view key;
    bool saw_bits = false;
    while (r.nextField(key)) {
        if (sv_eq(key, "bits")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            if (v < 0) return false;
            out.bits = static_cast<std::uint64_t>(v);
            saw_bits = true;
        } else {
            // Unknown field -- consume a value to keep the parser in sync.
            std::string_view dummy;
            std::int64_t i = 0;
            bool b = false;
            if (r.isNull()) continue;
            if (r.nextString(dummy)) continue;
            if (r.nextInt(i)) continue;
            if (r.nextBool(b)) continue;
            return false;
        }
    }
    return saw_bits;
}

bool parseKill(util::json::Reader& r, WireKill& out) {
    // Cursor positioned just after "t":"kill".  Both fields are strings
    // with caps mirrored from bridge/wire.py KillMsg.  Either field
    // missing -> reject the line; the bridge always emits both.
    std::string_view key;
    bool saw_source = false;
    bool saw_cause = false;
    while (r.nextField(key)) {
        if (sv_eq(key, "source")) {
            std::string_view sv;
            if (!r.nextString(sv)) return false;
            copyFixedN(out.source, sv.data(), sv.size());
            saw_source = true;
        } else if (sv_eq(key, "cause")) {
            std::string_view sv;
            if (!r.nextString(sv)) return false;
            copyFixedN(out.cause, sv.data(), sv.size());
            saw_cause = true;
        } else {
            std::string_view dummy;
            std::int64_t i = 0;
            bool b = false;
            if (r.isNull()) continue;
            if (r.nextString(dummy)) continue;
            if (r.nextInt(i)) continue;
            if (r.nextBool(b)) continue;
            return false;
        }
    }
    return saw_source && saw_cause;
}

bool parseGrantHashKeyed(util::json::Reader& r, WireGrantHashKeyed& out) {
    // Cursor is positioned just after "t":"grant_hash_keyed".  Both fields
    // are u32; the bridge validates [0, 2**32) before sending.  We accept
    // the full u32 range and reject anything else.
    std::string_view key;
    bool saw_hash = false;
    bool saw_value = false;
    while (r.nextField(key)) {
        if (sv_eq(key, "hash")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            if (v < 0 || v > 0xFFFFFFFFLL) return false;
            out.hash = static_cast<std::uint32_t>(v);
            saw_hash = true;
        } else if (sv_eq(key, "value")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            if (v < 0 || v > 0xFFFFFFFFLL) return false;
            out.value = static_cast<std::uint32_t>(v);
            saw_value = true;
        } else {
            std::string_view dummy;
            std::int64_t i = 0;
            bool b = false;
            if (r.isNull()) continue;
            if (r.nextString(dummy)) continue;
            if (r.nextInt(i)) continue;
            if (r.nextBool(b)) continue;
            return false;
        }
    }
    return saw_hash && saw_value;
}

bool parseDumpSaveField(util::json::Reader& r, WireDumpSaveField& out) {
    // Cursor positioned just after "t":"dump_save_field".  Two u32 fields:
    // base_nso_offset selects the singleton; field_offset is within it.
    std::string_view key;
    bool saw_base = false;
    bool saw_offset = false;
    while (r.nextField(key)) {
        if (sv_eq(key, "base_nso_offset")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            if (v < 0 || v > 0xFFFFFFFFLL) return false;
            out.base_nso_offset = static_cast<std::uint32_t>(v);
            saw_base = true;
        } else if (sv_eq(key, "field_offset")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            if (v < 0 || v > 0xFFFFFFFFLL) return false;
            out.field_offset = static_cast<std::uint32_t>(v);
            saw_offset = true;
        } else {
            std::string_view dummy;
            std::int64_t i = 0;
            bool b = false;
            if (r.isNull()) continue;
            if (r.nextString(dummy)) continue;
            if (r.nextInt(i)) continue;
            if (r.nextBool(b)) continue;
            return false;
        }
    }
    return saw_base && saw_offset;
}

bool parseSetContainerCBit(util::json::Reader& r,
                           WireSetContainerCBit& out) {
    // Cursor positioned just after "t":"set_container_c_bit".  Both u32
    // fields are validated [0, 2**32); value is a u8 (0 = clear, nonzero
    // = set).  Bridge will only send 0 or 1.
    std::string_view key;
    bool saw_hash = false;
    bool saw_bit = false;
    bool saw_value = false;
    while (r.nextField(key)) {
        if (sv_eq(key, "hash")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            if (v < 0 || v > 0xFFFFFFFFLL) return false;
            out.hash = static_cast<std::uint32_t>(v);
            saw_hash = true;
        } else if (sv_eq(key, "bit_index")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            if (v < 0 || v > 0xFFFFFFFFLL) return false;
            out.bit_index = static_cast<std::uint32_t>(v);
            saw_bit = true;
        } else if (sv_eq(key, "value")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            if (v < 0 || v > 0xFF) return false;
            out.value = static_cast<std::uint8_t>(v);
            saw_value = true;
        } else {
            std::string_view dummy;
            std::int64_t i = 0;
            bool b = false;
            if (r.isNull()) continue;
            if (r.nextString(dummy)) continue;
            if (r.nextInt(i)) continue;
            if (r.nextBool(b)) continue;
            return false;
        }
    }
    return saw_hash && saw_bit && saw_value;
}

bool parseSetPerCourseBitfield(util::json::Reader& r,
                               WireSetPerCourseBitfield& out) {
    // Cursor positioned just after "t":"set_per_course_bitfield".  All
    // three fields are u32; the bridge validates [0, 2**32) before sending.
    std::string_view key;
    bool saw_hash = false;
    bool saw_course = false;
    bool saw_bitmask = false;
    while (r.nextField(key)) {
        if (sv_eq(key, "hash")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            if (v < 0 || v > 0xFFFFFFFFLL) return false;
            out.hash = static_cast<std::uint32_t>(v);
            saw_hash = true;
        } else if (sv_eq(key, "course_index")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            if (v < 0 || v > 0xFFFFFFFFLL) return false;
            out.course_index = static_cast<std::uint32_t>(v);
            saw_course = true;
        } else if (sv_eq(key, "bitmask")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            if (v < 0 || v > 0xFFFFFFFFLL) return false;
            out.bitmask = static_cast<std::uint32_t>(v);
            saw_bitmask = true;
        } else {
            std::string_view dummy;
            std::int64_t i = 0;
            bool b = false;
            if (r.isNull()) continue;
            if (r.nextString(dummy)) continue;
            if (r.nextInt(i)) continue;
            if (r.nextBool(b)) continue;
            return false;
        }
    }
    return saw_hash && saw_course && saw_bitmask;
}

bool parseIncrementHashKeyed(util::json::Reader& r, WireIncrementHashKeyed& out) {
    // Cursor positioned just after "t":"increment_hash_keyed".  `hash`
    // is u32; `delta` is signed i32 so the bridge can request refunds
    // (e.g. -10 to undo the in-game purple-coin add when a TEN_COIN
    // location fires).  The Switch saturates at 0 on the apply side.
    constexpr std::int64_t kI32Min = -(static_cast<std::int64_t>(1) << 31);
    constexpr std::int64_t kI32Max = (static_cast<std::int64_t>(1) << 31) - 1;
    std::string_view key;
    bool saw_hash = false;
    bool saw_delta = false;
    while (r.nextField(key)) {
        if (sv_eq(key, "hash")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            if (v < 0 || v > 0xFFFFFFFFLL) return false;
            out.hash = static_cast<std::uint32_t>(v);
            saw_hash = true;
        } else if (sv_eq(key, "delta")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            if (v < kI32Min || v > kI32Max) return false;
            out.delta = static_cast<std::int32_t>(v);
            saw_delta = true;
        } else {
            std::string_view dummy;
            std::int64_t i = 0;
            bool b = false;
            if (r.isNull()) continue;
            if (r.nextString(dummy)) continue;
            if (r.nextInt(i)) continue;
            if (r.nextBool(b)) continue;
            return false;
        }
    }
    return saw_hash && saw_delta;
}

bool parseSetRoyalSeedsAbsolute(util::json::Reader& r,
                                WireSetRoyalSeedsAbsolute& out) {
    // Cursor positioned just after "t":"set_royal_seeds_absolute".  One
    // required field: "mask" -> u8 in [0, 64) (only the 6 Royal Seed
    // bits valid).  Bridge already validates the range; we mirror the
    // check here so a buggy bridge can't slip a wider value in.
    std::string_view key;
    bool saw_mask = false;
    while (r.nextField(key)) {
        if (sv_eq(key, "mask")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            if (v < 0 || v > 0x3F) return false;
            out.mask = static_cast<std::uint8_t>(v);
            saw_mask = true;
        } else {
            std::string_view dummy;
            std::int64_t i = 0;
            bool b = false;
            if (r.isNull()) continue;
            if (r.nextString(dummy)) continue;
            if (r.nextInt(i)) continue;
            if (r.nextBool(b)) continue;
            return false;
        }
    }
    return saw_mask;
}

bool parseSetRoutableWorldsAbsolute(util::json::Reader& r,
                                    WireSetRoutableWorldsAbsolute& out) {
    // Cursor positioned just after "t":"set_routable_worlds".  One
    // required field: "mask" -> u16 in [0, 2**16).  9 bits are
    // meaningful (W1..W6, Petal, Special, Castle); the bridge validates
    // the range, mirrored here against a buggy bridge.
    std::string_view key;
    bool saw_mask = false;
    while (r.nextField(key)) {
        if (sv_eq(key, "mask")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            if (v < 0 || v > 0xFFFF) return false;
            out.mask = static_cast<std::uint16_t>(v);
            saw_mask = true;
        } else {
            std::string_view dummy;
            std::int64_t i = 0;
            bool b = false;
            if (r.isNull()) continue;
            if (r.nextString(dummy)) continue;
            if (r.nextInt(i)) continue;
            if (r.nextBool(b)) continue;
            return false;
        }
    }
    return saw_mask;
}

bool parseSetWonderSeedCounts(util::json::Reader& r,
                              WireSetWonderSeedCounts& out) {
    // Cursor positioned just after "t":"set_wonder_seed_counts".  One
    // required field: "counts" -> array of exactly kWorldCount (8) u32s.
    // Bridge validates length + per-element range before sending; we
    // enforce the same here to keep the decoder resilient to a buggy
    // bridge.
    std::string_view key;
    bool saw_counts = false;
    while (r.nextField(key)) {
        if (sv_eq(key, "counts")) {
            if (!r.enterArray()) return false;
            std::size_t i = 0;
            while (r.hasMoreInArray()) {
                if (i >= kWorldCount) return false;
                std::int64_t v = 0;
                if (!r.nextInt(v)) return false;
                if (v < 0 || v > 0xFFFFFFFFLL) return false;
                out.counts[i++] = static_cast<std::uint32_t>(v);
            }
            if (!r.exitArray()) return false;
            if (i != kWorldCount) return false;
            saw_counts = true;
        } else {
            std::string_view dummy;
            std::int64_t i = 0;
            bool b = false;
            if (r.isNull()) continue;
            if (r.nextString(dummy)) continue;
            if (r.nextInt(i)) continue;
            if (r.nextBool(b)) continue;
            return false;
        }
    }
    return saw_counts;
}

bool parseSetWonderSeedsAbsolute(util::json::Reader& r,
                                 WireSetWonderSeedsAbsolute& out) {
    // Cursor positioned just after "t":"set_wonder_seeds_absolute".  Two
    // required u64 fields: "bits_lo" and "bits_hi".  Each accepted as
    // int64 in [0, 2**63); top bit reserved (typical AP scenarios put
    // 0..16 bits per world bucket so the masks fit comfortably).
    std::string_view key;
    bool saw_lo = false;
    bool saw_hi = false;
    while (r.nextField(key)) {
        if (sv_eq(key, "bits_lo")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            if (v < 0) return false;
            out.bits_lo = static_cast<std::uint64_t>(v);
            saw_lo = true;
        } else if (sv_eq(key, "bits_hi")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            if (v < 0) return false;
            out.bits_hi = static_cast<std::uint64_t>(v);
            saw_hi = true;
        } else {
            std::string_view dummy;
            std::int64_t i = 0;
            bool b = false;
            if (r.isNull()) continue;
            if (r.nextString(dummy)) continue;
            if (r.nextInt(i)) continue;
            if (r.nextBool(b)) continue;
            return false;
        }
    }
    return saw_lo && saw_hi;
}

bool parseHelloAck(util::json::Reader& r, WireHelloAck& out) {
    std::string_view key;
    while (r.nextField(key)) {
        if (sv_eq(key, "ok")) {
            if (!r.nextBool(out.ok)) return false;
        } else if (sv_eq(key, "wire_ver")) {
            std::int64_t v = 0;
            if (!r.nextInt(v)) return false;
            out.wire_ver = static_cast<int>(v);
        } else if (sv_eq(key, "bridge_ver")) {
            std::string_view sv;
            if (!r.nextString(sv)) return false;
            copyFixedN(out.bridge_ver, sv.data(), sv.size());
        } else if (sv_eq(key, "reason")) {
            std::string_view sv;
            if (!r.nextString(sv)) return false;
            copyFixedN(out.reason, sv.data(), sv.size());
        } else {
            std::string_view dummy;
            std::int64_t i = 0;
            bool b = false;
            if (r.isNull()) continue;
            if (r.nextString(dummy)) continue;
            if (r.nextInt(i)) continue;
            if (r.nextBool(b)) continue;
            return false;
        }
    }
    return true;
}

bool parseErr(util::json::Reader& r, WireErr& out) {
    std::string_view key;
    while (r.nextField(key)) {
        if (sv_eq(key, "reason")) {
            std::string_view sv;
            if (!r.nextString(sv)) return false;
            copyFixedN(out.reason, sv.data(), sv.size());
        } else {
            std::string_view dummy;
            std::int64_t i = 0;
            bool b = false;
            if (r.isNull()) continue;
            if (r.nextString(dummy)) continue;
            if (r.nextInt(i)) continue;
            if (r.nextBool(b)) continue;
            return false;
        }
    }
    return true;
}

bool parsePong(util::json::Reader& r, WirePong& out) {
    std::string_view key;
    while (r.nextField(key)) {
        if (sv_eq(key, "ts_ms")) {
            if (!r.nextInt(out.ts_ms)) return false;
        } else {
            std::string_view dummy;
            std::int64_t i = 0;
            bool b = false;
            if (r.isNull()) continue;
            if (r.nextString(dummy)) continue;
            if (r.nextInt(i)) continue;
            if (r.nextBool(b)) continue;
            return false;
        }
    }
    return true;
}

}  // namespace

bool decodeInbound(char* data, std::size_t len, InboundMsg& out) {
    if (len == 0) return false;
    // Trim a single trailing newline if present.
    while (len > 0 && (data[len - 1] == '\n' || data[len - 1] == '\r')) --len;
    if (len == 0) return false;

    util::json::Reader r(data, len);
    if (!r.enterObject()) return false;

    // First field MUST be "t" (encoder always emits it first; the Python side
    // does the same with separators=(",", ":") and dict ordering).  If it's
    // not, scan until we find it -- spec-compliant JSON allows any order.
    std::string_view key;
    char t_val[32] = {};
    bool got_t = false;

    // Collect "t" while skipping other fields' values for the first pass; if
    // we hit a value-bearing field before "t", we have to bail because the
    // dispatchers below assume cursor sits right after the "t" pair.
    if (!r.nextField(key)) return false;
    if (!sv_eq(key, "t")) {
        // Out-of-order discriminator -- not supported in M4.  Bail.
        return false;
    }
    std::string_view tv;
    if (!r.nextString(tv)) return false;
    copyFixedN(t_val, tv.data(), tv.size());
    got_t = true;
    (void)got_t;

    if (std::strcmp(t_val, "set_badges_absolute") == 0) {
        out.kind = InboundKind::SetBadgesAbsolute;
        out.set_badges_absolute = WireSetBadgesAbsolute{};
        if (!parseSetBadgesAbsolute(r, out.set_badges_absolute)) return false;
    } else if (std::strcmp(t_val, "grant_hash_keyed") == 0) {
        out.kind = InboundKind::GrantHashKeyed;
        out.grant_hash_keyed = WireGrantHashKeyed{};
        if (!parseGrantHashKeyed(r, out.grant_hash_keyed)) return false;
    } else if (std::strcmp(t_val, "increment_hash_keyed") == 0) {
        out.kind = InboundKind::IncrementHashKeyed;
        out.increment_hash_keyed = WireIncrementHashKeyed{};
        if (!parseIncrementHashKeyed(r, out.increment_hash_keyed)) return false;
    } else if (std::strcmp(t_val, "set_per_course_bitfield") == 0) {
        out.kind = InboundKind::SetPerCourseBitfield;
        out.set_per_course_bitfield = WireSetPerCourseBitfield{};
        if (!parseSetPerCourseBitfield(r, out.set_per_course_bitfield)) return false;
    } else if (std::strcmp(t_val, "set_container_c_bit") == 0) {
        out.kind = InboundKind::SetContainerCBit;
        out.set_container_c_bit = WireSetContainerCBit{};
        if (!parseSetContainerCBit(r, out.set_container_c_bit)) return false;
    } else if (std::strcmp(t_val, "dump_save_field") == 0) {
        out.kind = InboundKind::DumpSaveField;
        out.dump_save_field = WireDumpSaveField{};
        if (!parseDumpSaveField(r, out.dump_save_field)) return false;
    } else if (std::strcmp(t_val, "set_wonder_seed_counts") == 0) {
        out.kind = InboundKind::SetWonderSeedCounts;
        out.set_wonder_seed_counts = WireSetWonderSeedCounts{};
        if (!parseSetWonderSeedCounts(r, out.set_wonder_seed_counts)) return false;
    } else if (std::strcmp(t_val, "set_royal_seeds_absolute") == 0) {
        out.kind = InboundKind::SetRoyalSeedsAbsolute;
        out.set_royal_seeds_absolute = WireSetRoyalSeedsAbsolute{};
        if (!parseSetRoyalSeedsAbsolute(r, out.set_royal_seeds_absolute)) return false;
    } else if (std::strcmp(t_val, "set_wonder_seeds_absolute") == 0) {
        out.kind = InboundKind::SetWonderSeedsAbsolute;
        out.set_wonder_seeds_absolute = WireSetWonderSeedsAbsolute{};
        if (!parseSetWonderSeedsAbsolute(r, out.set_wonder_seeds_absolute)) return false;
    } else if (std::strcmp(t_val, "set_routable_worlds") == 0) {
        out.kind = InboundKind::SetRoutableWorldsAbsolute;
        out.set_routable_worlds_absolute = WireSetRoutableWorldsAbsolute{};
        if (!parseSetRoutableWorldsAbsolute(r, out.set_routable_worlds_absolute)) return false;
    } else if (std::strcmp(t_val, "kill") == 0) {
        out.kind = InboundKind::Kill;
        out.kill = WireKill{};
        if (!parseKill(r, out.kill)) return false;
    } else if (std::strcmp(t_val, "hello_ack") == 0) {
        out.kind = InboundKind::HelloAck;
        out.hello_ack = WireHelloAck{};
        if (!parseHelloAck(r, out.hello_ack)) return false;
    } else if (std::strcmp(t_val, "err") == 0) {
        out.kind = InboundKind::Err;
        out.err = WireErr{};
        if (!parseErr(r, out.err)) return false;
    } else if (std::strcmp(t_val, "pong") == 0) {
        out.kind = InboundKind::Pong;
        out.pong = WirePong{};
        if (!parsePong(r, out.pong)) return false;
    } else {
        // Unknown type -- consume any remaining fields to keep the reader
        // happy, then return false so the caller drops the line.
        std::string_view dummy;
        std::int64_t i = 0;
        bool b = false;
        while (r.nextField(key)) {
            if (r.isNull()) continue;
            if (r.nextString(dummy)) continue;
            if (r.nextInt(i)) continue;
            if (r.nextBool(b)) continue;
            break;
        }
        return false;
    }

    if (!r.exitObject()) return false;
    return true;
}

}  // namespace smbwap::ap
