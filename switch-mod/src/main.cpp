// SMBW Archipelago — hakkun edition entry point.
//
// Phase 1 of the hakkun migration (docs/hakkun-migration-plan.md): the only
// goal is to prove that hakkun's subsdk loader boots SMBW on real hardware
// after the exlaunch-based subsdk crashed every Switch boot we tried (six
// identical `nninitStartup → SetHeapSize+0x78` reports on Atmosphere 1.11.1
// + HATS-2026-05-11 + SMBW v1.0.0; see memory/smbwap_exlaunch_real_hw_broken.md).
//
// Zero functional hooks are installed. hkMain logs START + END through
// hk::svc::OutputDebugString — Ryujinx writes that into its log file, so
// observing those two lines confirms the hakkun loader ran end-to-end.
// The exlaunch-era SMBWAP_HOOK_MASK and the 14 HOOK_DEFINE_TRAMPOLINE
// blocks come back in Phase 2 once we know the loader works.
//
// On-device SD-log drain (sd:/smbwap_boot.log): the Log.{hpp,cpp} port
// keeps drainPendingToFile() callable, but Phase 1 doesn't trigger it.
// Driving the drain requires either an allocator (hakkun's HeapSourceDynamic
// addon, which needs sail) or a hand-written thread spawn — both more
// scope than "prove the loader boots". The plan's failure-mode table
// covers this case explicitly ("Game boots but log doesn't appear →
// iterate on drain trigger"); we add that trigger in Phase 2 alongside
// the first real game hook (likely drawMain, mirroring smo).

#include "hk/svc/api.h"
#include "hk/types.h"

#include "util/Log.hpp"

extern "C" void hkMain() {
    SMBWAP_LOG_INFO("=== smbwap hkMain START ===");
    SMBWAP_LOG_INFO("Phase 1: no hooks installed; loader-boot diagnostic only");
#ifdef SMBWAP_DEBUG_SD_LOG
    SMBWAP_LOG_INFO("SMBWAP_DEBUG_SD_LOG=ON (drain function compiled in but not yet wired)");
#else
    SMBWAP_LOG_INFO("SMBWAP_DEBUG_SD_LOG=OFF; relying on Ryujinx kernel-log sink only");
#endif
    SMBWAP_LOG_INFO("=== smbwap hkMain END ===");
}
