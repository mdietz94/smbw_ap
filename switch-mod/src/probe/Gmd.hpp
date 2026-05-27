// Shared helpers for the probe:: namespace (Phase 2g).
//
// The probe:: M3 grant primitives operate on the live SMBW save state
// reached via the `gmd::GameDataMgr` singleton (NSO +0x363F0F0).  Several
// of them also share the M3.8 scene-transition gate window and the M4.5
// save-loaded latch.  Centralized here so all probe sources include one
// header.
//
// Direct-function-pointer calls into NSO (e.g. FUN_710049F648 the
// container-A writer) use `mainBase() + offset` cast to the appropriate
// function-pointer type.  Where a Phase 2d trampoline already sits on the
// offset (e.g. GmdContainerAWriter @ +0x49F648) the call routes through
// the trampoline, producing free observability of every grant.  This
// matches the legacy exlaunch pattern documented at
// switch-mod/src/program/main.cpp:1511-1515.
//
// There is NO sail-resolved symbol path for these anonymous game
// functions -- they have no SDK mangling and only exist as NSO offsets
// that the M3 static-analysis sprint pinned down.  See
// docs/static-analysis-findings.md for the full list.

#pragma once

#include <cstdint>

#include "hk/ro/RoModule.h"
#include "hk/ro/RoUtil.h"

namespace probe {

// NSO offset of the gmd::GameDataMgr::sInstance qword anchor (decompiled
// 2026-05-24, see CLAUDE.md "GameDataMgr (gmd::) save-data API").
//
// Dereferencing this gives the live GameDataMgr*.  Returns 0 (null) until
// the save deserializer runs (first save-select); see [[smbwap-hakkun-
// migration]] gates discussion.  All grant primitives must null-check.
inline constexpr std::uint32_t kGmdSInstanceNsoOffset = 0x0363F0F0;

inline std::uintptr_t mainBase() {
    const auto* mod = hk::ro::getMainModule();
    return mod ? mod->range().start() : 0;
}

inline void* gmdSingleton() {
    const std::uintptr_t base = mainBase();
    if (base == 0) return nullptr;
    return *reinterpret_cast<void**>(base + kGmdSInstanceNsoOffset);
}

}  // namespace probe
