#include "ApFrameBridge.hpp"

#include <atomic>
#include <cstdio>
#include <cstring>

#include "ApState.hpp"
#include "probe/Gmd.hpp"
#include "util/Log.hpp"

namespace smbwap::ap {

void enqueueLog(const char* level, const char* msg) {
    // No SMBWAP_LOG_* here -- would recurse through util::log().
    // Drop silently when not connected so we don't backlog the ring.
    if (connState().load(std::memory_order_relaxed) == ConnState::Disconnected) return;
    OutboundEvent ev;
    ev.kind = OutboundKind::Log;
    ev.log = WireLog{};
    copyFixed(ev.log.level, level);
    copyFixed(ev.log.msg, msg);
    outboundRing().push(ev);  // silent drop on full -- no log here
}

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

// Format a dedup-suffix string for the coalesced/drained apply-block
// log lines.  Returns the leading verb ("coalesced" if anything was
// collapsed within this drain, otherwise "drained") and writes the
// suffix into `out` ("" or " (collapsed N earlier duplicate(s) within
// this drain)").  Keeps each apply block to a single SMBWAP_LOG_INFO
// call instead of an if/else pair.
static const char* coalesceVerb(char* out, std::size_t out_cap, int skipped) {
    if (skipped > 0) {
        std::snprintf(out, out_cap,
                      " (collapsed %d earlier duplicate%s within this drain)",
                      skipped, skipped == 1 ? "" : "s");
        return "coalesced";
    }
    if (out_cap > 0) out[0] = '\0';
    return "drained";
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

    // 2026-05-26: scene-transition gate.  All game writers we call
    // (FUN_710049F648 container-A, FUN_710049EA24/FUN_71001F263FC
    // container-B, setBadgeBitfieldAbsolute container-C, FUN_7101F2B354
    // container-D, synthKill HP write) race with game-natural writes
    // during scene transitions.  Live-reproduced abort sites:
    //   * FUN_710049F750+0x0  -- container-A secondary insert (fixed
    //                            for the WS interceptor; this gate
    //                            covers the rest)
    //   * FUN_71001F263FC+0xF8 -- container-B bool secondary path
    //                             (triggered SetRoyalSeedsAbsolute
    //                             writes during a gate-unlock
    //                             transition, 2026-05-26)
    // All bridge messages are either idempotent absolute-overwrite or
    // AP-replayed on next 2 s tick / HelloMsg / ReceivedItems, so
    // deferring during a ~3 s window costs at most that much
    // staleness with no progress loss.  The bridge keeps producing;
    // messages sit in the SPSC ring (256 cap, ample for 3 s of 1.5
    // msg/s).
    if (probe::isInSceneTransitionWindow()) {
        static std::atomic<std::uint32_t> s_trans_log_budget{20};
        if (s_trans_log_budget.fetch_sub(1, std::memory_order_relaxed) > 0) {
            const auto pending = inboundRing().pendingApprox();
            SMBWAP_LOG_DEBUG(
                "[grant] drainInbound: scene transition active; "
                "deferring %zu msg(s)", pending);
        }
        return;
    }

    // Observability (2026-05-26 follow-up to the level-entry abort): log
    // EVERY entry that actually drains a message, so a post-mortem can
    // see how many messages flushed in this batch and whether the gate
    // happened to NOT engage at the relevant moment.  By construction
    // we're past the !isInSceneTransitionWindow() early-return, so any
    // log line here means the gate was not engaged at entry.
    //
    // Extended 2026-05-27 with dirty-queue depths.  Each container has a
    // bounded lock-free ring at substruct+0x30 (cap at +0x28, head+gen
    // at +0x38).  Overflow tail-calls FUN_7100472CF0 → AbortImpl, hard.
    // The container-A "insert new entry" ring (substruct gmd+0x128) is
    // the one that overflowed in the 2026-05-26 level-entry abort and
    // is most at risk during 5-distinct-hash bursts; the container-B
    // ring overflowed in the 2026-05-27 9 h SetRoyalSeedsAbsolute audit
    // (5ca82aa).  Read the three rings here so any crash in the next
    // few frames has the most recent depth snapshot in the log.  See
    // [probe/Gmd.hpp](../probe/Gmd.hpp) for layout details.
    const auto pending_at_entry = inboundRing().pendingApprox();
    if (pending_at_entry > 0) {
        const auto qa  = probe::readQueueA_primary();
        const auto qas = probe::readQueueA_secondary();
        const auto qb  = probe::readQueueB();
        SMBWAP_LOG_INFO(
            "[grant] drainInbound RUN: pending=%zu  "
            "qA=%u/%u(gen=0x%03x) qA2=%u/%u(gen=0x%03x) qB=%u/%u(gen=0x%03x)  "
            "(gate was OPEN at entry; if a crash follows in the next few "
            "frames, this is the bracket that bypassed the scene gate)",
            pending_at_entry,
            qa.depth(),  qa.cap,  qa.gen(),
            qas.depth(), qas.cap, qas.gen(),
            qb.depth(),  qb.cap,  qb.gen());
    }

    // Coalesce absolute-overwrite kinds within this drain call.
    //
    // The bridge sends a SetBadgesAbsolute + SetRoyalSeedsAbsolute +
    // SetWonderSeedCounts triplet every ~2 s as an idempotent backstop
    // that reverts in-game pickups bypassing AP.  Under the gates above
    // (isSaveLoaded + isInSceneTransitionWindow), identical triplets
    // pile up in the inbound ring during gated windows and flush as
    // bursts when the gate opens -- one ~3 s gated window observed
    // 18 triplets drained in 3 ms (Ryujinx_1.3.3_2026-05-26_19-16-12.log
    // lines 11197-11405).
    //
    // For absolute-overwrite payloads, last-write-wins is the entire
    // semantic, so applying each of N piled messages produces the same
    // final state as applying only the latest.  We defer them until
    // after the drain loop and apply once per kind with the latest
    // payload.  Non-deduplicable kinds (GrantHashKeyed, Kill, etc.)
    // still apply inline preserving their original order, because
    // they aren't last-write-wins (RMW counter increments, side-effects
    // like HP=0) -- losing any of them is observable.
    //
    // This is purely Switch-side and only collapses messages that pile
    // up between drain calls.  When the gate is open and drainInbound
    // runs every tick, each drain pops at most one of each absolute-
    // overwrite kind, and we apply that one -- so a real state change
    // never gets skipped, even when its payload matches a previous one
    // (live state may have drifted from in-game writes).  This is the
    // key correctness property that prevented us from doing the same
    // dedup bridge-side: the bridge can't see Switch-local drift, but
    // the Switch always can because it IS the live state.
    InboundMsg last_badges{};
    InboundMsg last_seeds{};
    InboundMsg last_wsc{};
    bool has_badges = false;
    bool has_seeds = false;
    bool has_wsc = false;
    int dedup_badges_skipped = 0;
    int dedup_seeds_skipped = 0;
    int dedup_wsc_skipped = 0;

    InboundMsg msg;
    int drained = 0;
    while (inboundRing().pop(msg)) {
        ++drained;
        switch (msg.kind) {
            case InboundKind::SetBadgesAbsolute: {
                if (has_badges) ++dedup_badges_skipped;
                last_badges = msg;
                has_badges = true;
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
            case InboundKind::SetRoyalSeedsAbsolute: {
                if (has_seeds) ++dedup_seeds_skipped;
                last_seeds = msg;
                has_seeds = true;
                break;
            }
            case InboundKind::SetWonderSeedCounts: {
                if (has_wsc) ++dedup_wsc_skipped;
                last_wsc = msg;
                has_wsc = true;
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
    // Apply the latest payload of each deduplicable absolute-overwrite
    // kind.  See the comment above the deferral declarations for the
    // last-write-wins rationale.  Each apply block uses coalesceVerb()
    // to pick between "coalesced" / "drained" in the log line so the
    // log-call itself stays single-format.

    char dedup_suffix[80];

    if (has_badges) {
        const auto bits = last_badges.set_badges_absolute.bits;
        const bool ok = probe::setBadgeBitfieldAbsolute(bits);
        const char* verb = coalesceVerb(
            dedup_suffix, sizeof(dedup_suffix), dedup_badges_skipped);
        SMBWAP_LOG_INFO(
            "[grant] %s SetBadgesAbsolute(bits=0x%016llx)%s -> "
            "setBadgeBitfieldAbsolute returned %s",
            verb, static_cast<unsigned long long>(bits), dedup_suffix,
            ok ? "true" : "false");
    }

    if (has_seeds) {
        // AP-authoritative Royal Seed sync (2026-05-26).  Loop the 6
        // container-B bool hashes in mask-bit order and grant/clear each
        // per the mask.  The container-B bool writer FUN_710049EA24
        // accepts value=0 just fine, so a 0 bit cleanly reverts an
        // in-game palace clear that ran ahead of AP releasing the
        // matching seed item.
        const auto mask = last_seeds.set_royal_seeds_absolute.mask;
        std::uint32_t granted = 0;
        for (std::size_t i = 0; i < kRoyalSeedCount; ++i) {
            const std::uint32_t bit_v = (mask >> i) & 1u;
            if (probe::grantContainerBBool(kRoyalSeedHashes[i], bit_v)) {
                if (bit_v) ++granted;
            }
        }
        const char* verb = coalesceVerb(
            dedup_suffix, sizeof(dedup_suffix), dedup_seeds_skipped);
        SMBWAP_LOG_INFO(
            "[grant] %s SetRoyalSeedsAbsolute(mask=0x%02x)%s -> "
            "%u/%u seed(s) granted (others cleared)",
            verb, static_cast<unsigned>(mask), dedup_suffix,
            granted, static_cast<unsigned>(kRoyalSeedCount));
    }

    if (has_wsc) {
        // AP-authoritative per-world Wonder Seed gate override.  Cache
        // the 8 counts; ContainerAReader::Callback in main.cpp
        // substitutes the AP-authoritative value on every read of the
        // 3 safe-substituted mirror hashes (gate + 2 UI mirrors).  No
        // proactive container write -- the previous
        // pushWonderSeedOverrideCurrentWorld() call here raced with the
        // game's secondary-container mutations during scene transitions
        // PR #40's gate didn't catch (notably level-entry transitions),
        // and aborted inside FUN_710049F750.  Stack-trace forensics:
        // Ryujinx_1.3.3_2026-05-26_19-29-05.log (user report, crash on
        // entering a level).
        //
        // The reader-side substitution provides the SAME visible
        // behavior as the old proactive push:
        //   * Gate predicate (FUN_71001787b40) reads 0x390eb960
        //     -> substituted on every read.
        //   * In-course HUD reads 0x21f89ab1 or 0x8c20ccb7
        //     -> substituted on every read.
        // Without the race surface of writing to container A from our
        // thread during a transition.
        for (std::uint32_t i = 0; i < kWorldCount; ++i) {
            g_wonder_seed_counts[i].store(
                last_wsc.set_wonder_seed_counts.counts[i],
                std::memory_order_relaxed);
        }
        const char* verb = coalesceVerb(
            dedup_suffix, sizeof(dedup_suffix), dedup_wsc_skipped);
        SMBWAP_LOG_INFO(
            "[grant] %s SetWonderSeedCounts%s counts="
            "[%u,%u,%u,%u,%u,%u,%u,%u]",
            verb, dedup_suffix,
            last_wsc.set_wonder_seed_counts.counts[0],
            last_wsc.set_wonder_seed_counts.counts[1],
            last_wsc.set_wonder_seed_counts.counts[2],
            last_wsc.set_wonder_seed_counts.counts[3],
            last_wsc.set_wonder_seed_counts.counts[4],
            last_wsc.set_wonder_seed_counts.counts[5],
            last_wsc.set_wonder_seed_counts.counts[6],
            last_wsc.set_wonder_seed_counts.counts[7]);
    }

    if (drained > 0) {
        SMBWAP_LOG_DEBUG("[grant] drainInbound drained %d msg(s)", drained);
    }
}

}  // namespace smbwap::ap
