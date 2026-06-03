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
// are present (see switch-mod/CMakeLists.txt). Until the NVN renderer backend
// is finished (see docs/handoff-imgui-overlay.md), the macro stays undefined
// and init()/draw() compile to the cheap no-op paths so the live build is
// unchanged.
//
// Frame ownership (Option B / wondar lineage): the per-frame ImGui lifecycle
// (NewFrame / Render / renderDrawData) is owned by the NVN present hook in
// ImGuiNvnBootstrap.cpp. drawDebugConsole() is called by that shim between
// NewFrame and Render and emits widgets only.

#pragma once

namespace smbwap::ui {

// One-time init. Stamps the boot tick the visibility gate keys off of. The
// ImGui context + NVN backend are created lazily by the present hook
// (ImGuiNvnBootstrap.cpp), not here. Safe to call when the backend isn't
// compiled in (just stamps + logs). Wired pre-orig from an early-init hook.
void initDebugConsole();

// Per-frame overlay entry, called by the NVN present shim between
// ImGui::NewFrame() and ImGui::Render(). Samples ap::connState() + the log
// ring and emits the overlay window if the visibility conditions are met —
// widgets only, no frame-lifecycle calls. Cheap when hidden. No-op unless
// SMBWAP_HAS_DEBUG_RENDERER.
void drawDebugConsole();

}  // namespace smbwap::ui
