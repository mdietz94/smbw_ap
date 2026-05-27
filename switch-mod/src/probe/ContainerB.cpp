// M3.3b container-B bool writer (Phase 2g port).
//
// FUN_710049EA24 at NSO +0x0049EA24 is the high-level container-B bool
// setter for the gmd+8 substruct (hash-keyed bools).  Recovered by static-
// analysis sprint 2 (docs/static-analysis-findings.md lines 3170-3171 +
// 3215).  Body:
//     if (FUN_710049ea78(gmd + 0x68) & 1) return;   // init/lock gate
//     FUN_7101f263fc(gmd + 8, value & 1, hash);     // delegate
// Signature parallel to the container-A writer:
//     void FUN_710049EA24(GameDataMgr*, uint32_t value, uint32_t hash);
//
// Used for the 8 documented bool hashes (6 Royal Seeds + COMPLETE_GAME +
// INTRO_CUTSCENE_COMPLETED -- see kBoolHashes in ApFrameBridge.hpp).
// The existing Phase 2d GmdBoolWriter trampoline at NSO +0x01F263FC
// (the inner delegate) logs every call for free observability;
// substruct should equal gmd + 8 on the trampoline log line.
//
// Same deferred-write semantics as container A: the bool is queued to
// the dirty buffer and flushed at next save.  M4.5 bridge replay-on-
// HelloMsg keeps grants alive across load-before-flush.
//
// We call FUN_710049EA24 directly by NSO offset cast -- no separate
// trampoline.  The inner delegate +0x01F263FC IS hooked by Phase 2d so
// the log line surfaces there.

#include <atomic>
#include <cstdint>

#include "probe/Gmd.hpp"
#include "util/Log.hpp"

namespace probe {

namespace {

using GmdSetBoolFn = void (*)(void* gmd, std::uint32_t value, std::uint32_t hash);
constexpr std::uint32_t kContainerBWriterOffset = 0x0049EA24;

}  // namespace

bool grantContainerBBool(std::uint32_t hash, std::uint32_t value) {
    void* gmd = gmdSingleton();
    if (gmd == nullptr) return false;
    const auto fn = reinterpret_cast<GmdSetBoolFn>(
        mainBase() + kContainerBWriterOffset);
    fn(gmd, value, hash);

    static std::atomic<std::uint32_t> log_budget{16};
    if (log_budget.fetch_sub(1) > 0) {
        SMBWAP_LOG_INFO(
            "GrantHashKeyedBool: hash=0x%08x value=%u gmd=%p",
            hash, value, gmd);
    }
    return true;
}

}  // namespace probe
