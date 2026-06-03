// NVN present-hook bootstrap for the on-Switch ImGui debug overlay.
//
// Hakkun port of the wondar/exlaunch-lineage src/program/imgui_nvn.cpp. The
// approach is game-agnostic (works on any NVN Switch title), which is why it
// needs no SMBW-specific render function:
//
//   1. Trampoline `nvnBootstrapLoader` (SDK symbol) via installAtSym.
//   2. From it, intercept `nvnDeviceGetProcAddress`; when the game resolves
//      nvnDeviceInitialize / nvnQueueInitialize / nvnCommandBufferInitialize /
//      nvnQueuePresentTexture / nvnCommandBufferSetViewport, capture the
//      Device/Queue/CommandBuffer and return our own shims.
//   3. The presentTexture shim runs each frame with the live command buffer:
//      it drives the ImGui frame (NewFrame -> smbwap::ui::drawDebugConsole()
//      -> Render -> renderDrawData) and then chains the original present.
//
// Whole TU is gated by SMBWAP_HAS_DEBUG_RENDERER; installNvnImGuiHooks() is a
// no-op when the renderer isn't compiled in, so this is safe to call
// unconditionally from hkMain. See docs/handoff-imgui-overlay.md.

#pragma once

namespace smbwap::ui {

// Install the NVN bootstrap trampoline that brings up the ImGui overlay.
// Call once from hkMain. No-op unless SMBWAP_HAS_DEBUG_RENDERER.
void installNvnImGuiHooks();

}  // namespace smbwap::ui
