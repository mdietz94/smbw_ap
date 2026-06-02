// On-Switch ImGui-backed debug console. Pops up when the SMBW Client (the
// PC-side Kivy bridge) is unreachable so the player can see (a) the
// connection state and (b) the tail of the in-memory log ring — without
// having to tail the Ryujinx log or plug into the PC.
//
// Ported from smo_archipelago/switch-mod/src/ui/ApDebugConsole.{cpp,hpp}.
// SMBW differences from the SMO original:
//   * No wall-clock ApState::nowMs() — SMBW dropped it (see ApDiscovery.cpp).
//     We derive ms from hk::svc::getSystemTick() / 19'200 (19.2 MHz ARM
//     generic timer, the rate already used in probe/Gates.cpp + DeathLink.cpp).
//   * No ap::DiscoveryReport — SMBW's ApDiscovery only exposes resolveBridge(),
//     so the discovery panel is dropped; we show connection + log only.
//   * Connection signal is read straight off ap::connState() each frame
//     (== ConnState::Ready) and the disconnect timestamp is self-latched in
//     the draw loop — no ApClient edit, fully self-contained.
//
// Visibility rule (auto-mode only; no input wiring):
//
//   visible = (ms_since_boot > 5000) AND (!connected) AND
//             (ms_since_disconnect > 5000)
//
// Hide-on-connect is instant. The 5 s boot grace + 5 s disconnect grace give
// a healthy fresh boot a clear 10 s window where the overlay never flickers.
//
// The rendering body is guarded by SMBWAP_HAS_DEBUG_RENDERER. That macro is
// only defined by CMake when BOTH lib/imgui and LibHakkun's ImGui/Nvn addons
// are present (see switch-mod/CMakeLists.txt). Until the overlay's NVN
// backend wiring is finished (see docs/handoff-imgui-overlay.md — the two
// open RE items: the ImGui heap source and the per-frame NVN command-buffer
// chokepoint), the macro stays undefined and init()/draw() compile to the
// cheap no-op paths so the live build is unchanged.

#pragma once

namespace smbwap::ui {

// One-time init. Stamps the boot tick used by the visibility gate, and (when
// SMBWAP_HAS_DEBUG_RENDERER is defined) creates the ImGui heap + initializes
// the NVN backend. MUST be called pre-orig from an early-init hook, before the
// engine fragments the heap (mirrors the load-bearing SMO pre-orig invariant).
// Safe to call when the backend isn't compiled in (just stamps + logs).
void initDebugConsole();

// Per-frame entry. Samples ap::connState() + the log ring and renders the
// overlay if the visibility conditions are met. Cheap when hidden — early
// returns before any ImGui calls. No-op unless SMBWAP_HAS_DEBUG_RENDERER.
//
// NOTE (open RE item): this must be driven from SMBW's real per-frame NVN
// present/draw chokepoint. PlayerTickLatch is a logic tick, not a render
// hook — see docs/handoff-imgui-overlay.md.
void drawDebugConsole();

}  // namespace smbwap::ui
