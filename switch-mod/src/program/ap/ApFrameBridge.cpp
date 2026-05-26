#include "ApFrameBridge.hpp"

#include <atomic>
#include <cstring>

#include "ApState.hpp"
#include "../util/Log.hpp"

namespace smbwap::ap {

// AP-authoritative Wonder Seed per-world counts.  Updated by drainInbound
// on every SetWonderSeedCountsMsg; read by main.cpp's NerveActivateOnce
// tick to decide what value to push via probe::pushWonderSeedOverride.
// Atomic so the tick and drainInbound (both nominally on the game thread
// but defensively decoupled) can't tear a u32.
static std::atomic<std::uint32_t> g_wonder_seed_counts[kWorldCount] = {};

std::uint32_t getWonderSeedCount(std::uint32_t bucket) {
    if (bucket >= kWorldCount) return 0;
    return g_wonder_seed_counts[bucket].load(std::memory_order_relaxed);
}

bool enqueueNerveFire(NerveKind kind, std::uint32_t seq) {
    OutboundEvent ev;
    ev.kind = OutboundKind::NerveFire;
    ev.nerve = WireNerveFire{kind, seq};
    bool ok = outboundRing().push(ev);
    if (!ok) {
        SMBWAP_LOG_WARN(
            "[ring] outbound dropped (full): kind=%s seq=%u",
            toWire(kind), seq);
    }
    return ok;
}

bool enqueueBadgeAcquired(std::uint32_t internal_id) {
    static std::atomic<std::uint32_t> s_badge_seq{0};
    const std::uint32_t seq = s_badge_seq.fetch_add(1, std::memory_order_relaxed);
    OutboundEvent ev;
    ev.kind = OutboundKind::BadgeAcquired;
    ev.badge_acquired = WireBadgeAcquired{internal_id, seq};
    bool ok = outboundRing().push(ev);
    if (!ok) {
        SMBWAP_LOG_WARN(
            "[ring] outbound dropped (full): badge_acquired internal_id=%u seq=%u",
            internal_id, seq);
    } else {
        SMBWAP_LOG_INFO(
            "badge_acquired internal_id=%u seq=%u (enqueued)",
            internal_id, seq);
    }
    return ok;
}

bool enqueuePlayReport(const char* room,
                       const void* payload, std::size_t payload_len) {
    OutboundEvent ev;
    ev.kind = OutboundKind::PlayReport;
    // Manually construct WirePlayReport in-place to avoid the union
    // copy assignment edge cases on the 1.5 KB payload field.
    ev.play_report = WirePlayReport{};
    copyFixed(ev.play_report.room, room);

    std::size_t take = payload_len;
    if (take > kPayloadCap) {
        SMBWAP_LOG_WARN(
            "[ring] play_report truncated: room=%s size=%zu cap=%zu",
            room ? room : "(null)", payload_len, kPayloadCap);
        take = kPayloadCap;
    }
    if (payload && take > 0) {
        std::memcpy(ev.play_report.payload, payload, take);
    }
    ev.play_report.payload_len = static_cast<std::uint16_t>(take);

    bool ok = outboundRing().push(ev);
    if (!ok) {
        SMBWAP_LOG_WARN(
            "[ring] outbound dropped (full): play_report room=%s",
            room ? room : "(null)");
    }
    return ok;
}

void drainInbound() {
    // M4.5: gate on save-load.  Pre-save-select, the gmd singleton is
    // either null or points to title-screen-scoped data that gets
    // replaced when the player picks a save; applying grants then is
    // either a no-op or a write to the wrong container.  We sit on the
    // SPSC ring (now 256 entries, kInboundCap in ApState.hpp) until the
    // GmdContainerAWriter / GmdBoolWriter trampolines in main.cpp set
    // probe::isSaveLoaded -- they fire from the game's save deserializer
    // the first time the player selects a save.
    if (!probe::isSaveLoaded()) {
        // Throttled log so the operator can see the gate is intentional
        // without flooding -- NerveActivateOnce fires many times per
        // second.
        static std::atomic<std::uint32_t> s_skip_log_budget{20};
        if (s_skip_log_budget.fetch_sub(1, std::memory_order_relaxed) > 0) {
            const auto pending = inboundRing().pendingApprox();
            SMBWAP_LOG_DEBUG(
                "[grant] drainInbound: save not loaded yet; buffering "
                "%zu msg(s)", pending);
        }
        return;
    }

    InboundMsg msg;
    int drained = 0;
    while (inboundRing().pop(msg)) {
        ++drained;
        switch (msg.kind) {
            case InboundKind::SetBadgesAbsolute: {
                const auto bits = msg.set_badges_absolute.bits;
                const bool ok = probe::setBadgeBitfieldAbsolute(bits);
                SMBWAP_LOG_INFO(
                    "[grant] drained SetBadgesAbsolute(bits=0x%016llx) -> "
                    "setBadgeBitfieldAbsolute returned %s",
                    static_cast<unsigned long long>(bits),
                    ok ? "true" : "false");
                break;
            }
            case InboundKind::GrantHashKeyed: {
                const auto h = msg.grant_hash_keyed.hash;
                const auto v = msg.grant_hash_keyed.value;
                // M3.3b: bool-typed hashes route through container-B
                // (FUN_710049EA24); counters stay on container-A
                // (FUN_710049F648).  Container-A is typed per-slot and
                // silently no-ops on bool fields (live-falsified
                // 2026-05-25), so the split is required.
                const bool is_bool = isBoolHash(h);
                const bool ok = is_bool
                    ? probe::grantContainerBBool(h, v)
                    : probe::grantContainerACounter(h, v);
                SMBWAP_LOG_INFO(
                    "[grant] drained GrantHashKeyed(hash=0x%08x, value=%u) -> "
                    "%s returned %s",
                    h, v,
                    is_bool ? "grantContainerBBool" : "grantContainerACounter",
                    ok ? "true" : "false");
                break;
            }
            case InboundKind::IncrementHashKeyed: {
                const auto h = msg.increment_hash_keyed.hash;
                const auto d = msg.increment_hash_keyed.delta;
                // Container-A only.  Bool hashes (Royal Seeds, etc.)
                // are never increments -- semantic mismatch, and the
                // container-A reader doesn't observe gmd+8 substruct
                // bools anyway.  If the bridge ever sends one, the
                // RMW lands a zero+delta write that the container-A
                // writer no-ops on bool slots.  Belt-and-braces: log
                // it so we'd notice.
                if (isBoolHash(h)) {
                    SMBWAP_LOG_WARN(
                        "[grant] IncrementHashKeyed on bool hash 0x%08x; "
                        "container-A RMW will no-op", h);
                }
                const bool ok = probe::incrementContainerACounter(h, d);
                SMBWAP_LOG_INFO(
                    "[grant] drained IncrementHashKeyed(hash=0x%08x, delta=%d) -> "
                    "incrementContainerACounter returned %s",
                    h, static_cast<int>(d), ok ? "true" : "false");
                break;
            }
            case InboundKind::DumpSaveField: {
                const auto base = msg.dump_save_field.base_nso_offset;
                const auto off = msg.dump_save_field.field_offset;
                const bool ok = probe::dumpSaveField(base, off);
                SMBWAP_LOG_INFO(
                    "[probe] drained DumpSaveField(base=NSO+0x%x, "
                    "offset=0x%x) -> dumpSaveField returned %s",
                    base, off, ok ? "true" : "false");
                break;
            }
            case InboundKind::SetContainerCBit: {
                const auto h = msg.set_container_c_bit.hash;
                const auto b = msg.set_container_c_bit.bit_index;
                const bool v = msg.set_container_c_bit.value != 0;
                const bool ok = probe::setContainerCBit(h, b, v);
                SMBWAP_LOG_INFO(
                    "[grant] drained SetContainerCBit(hash=0x%08x, "
                    "bit=%u, value=%d) -> setContainerCBit returned %s",
                    h, b, v ? 1 : 0, ok ? "true" : "false");
                break;
            }
            case InboundKind::SetPerCourseBitfield: {
                const auto h = msg.set_per_course_bitfield.hash;
                const auto c = msg.set_per_course_bitfield.course_index;
                const auto b = msg.set_per_course_bitfield.bitmask;
                // Container-D writer.  Absolute set: overwrites the entire
                // u32 bitmask for (hash, course_index) regardless of prior
                // state.  Bridge holds canonical AP-authoritative masks and
                // pushes the new total any time it changes.
                const bool ok = probe::setPerCourseBitfieldAbsolute(h, c, b);
                SMBWAP_LOG_INFO(
                    "[grant] drained SetPerCourseBitfield(hash=0x%08x, "
                    "course=%u, bitmask=0x%08x) -> "
                    "setPerCourseBitfieldAbsolute returned %s",
                    h, c, b, ok ? "true" : "false");
                break;
            }
            case InboundKind::SetWonderSeedCounts: {
                // AP-authoritative per-world Wonder Seed gate override.
                // Cache the 8 counts; the NerveActivateOnce tick in
                // main.cpp reads container-A hash 0x9f5ead3c (current
                // world index), picks the matching bucket, and calls
                // probe::pushWonderSeedOverride(counts[bucket]).
                for (std::uint32_t i = 0; i < kWorldCount; ++i) {
                    g_wonder_seed_counts[i].store(
                        msg.set_wonder_seed_counts.counts[i],
                        std::memory_order_relaxed);
                }
                SMBWAP_LOG_INFO(
                    "[grant] drained SetWonderSeedCounts counts="
                    "[%u,%u,%u,%u,%u,%u,%u,%u]",
                    msg.set_wonder_seed_counts.counts[0],
                    msg.set_wonder_seed_counts.counts[1],
                    msg.set_wonder_seed_counts.counts[2],
                    msg.set_wonder_seed_counts.counts[3],
                    msg.set_wonder_seed_counts.counts[4],
                    msg.set_wonder_seed_counts.counts[5],
                    msg.set_wonder_seed_counts.counts[6],
                    msg.set_wonder_seed_counts.counts[7]);
                break;
            }
            case InboundKind::Kill: {
                // M3.8 DeathLink inbound apply.  synthKill writes 0 to
                // the latched live_base + 0x1C (HP byte) and sets the
                // loop-guard atomic so the outbound DEATH_DETECTED that
                // fires as a consequence gets suppressed by the nerve
                // callback in main.cpp.  Returns false if live_base
                // hasn't been latched yet -- expected on first run
                // before the player has grabbed a purple coin (or
                // before the LiveBaseLatch hook is enabled).
                const bool ok = probe::synthKill();
                SMBWAP_LOG_INFO(
                    "[deathlink in] source=%s cause=%s -> synthKill "
                    "returned %s",
                    msg.kill.source, msg.kill.cause,
                    ok ? "true" : "false");
                break;
            }
            case InboundKind::HelloAck:
            case InboundKind::Err:
            case InboundKind::Pong:
            case InboundKind::None:
                // Worker should have consumed these before they reach the
                // game-thread inbound ring -- ApClient routes them
                // internally and only forwards SetBadgesAbsolute /
                // GrantHashKeyed / Kill.  Log + drop.
                SMBWAP_LOG_WARN(
                    "[grant] unexpected inbound kind %u on game thread",
                    static_cast<unsigned>(msg.kind));
                break;
        }
    }
    if (drained > 0) {
        SMBWAP_LOG_DEBUG("[grant] drainInbound drained %d msg(s)", drained);
    }
}

}  // namespace smbwap::ap
