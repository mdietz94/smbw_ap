# Handoff — on-Switch ImGui debug overlay (port from smo_archipelago)

An ImGui overlay that pops up on the TV when the PC-side **SMBW Client** bridge
has been unreachable for >5 s, showing the connection state + the tail of the
in-memory log ring, and hides instantly when the bridge reconnects. Same idea
as SMO's `ApDebugConsole`.

## Chosen approach: Option B — game-agnostic NVN present hook

A 2026-06 web search settled the architecture. ImGui-in-Wonder is already proven
(`fruityloops1/wondar` ships an ImGui tool, "Peepa"; this repo's `switch-mod`
was forked from wondar), and the draw path is **title-independent**: hook the
SDK symbol `nvnBootstrapLoader`, intercept `nvnDeviceGetProcAddress`, capture the
NVN Device/Queue/CommandBuffer, and swap `nvnQueuePresentTexture` for a shim that
draws ImGui each frame then chains the original present. **No Wonder-specific
draw function or `Application`-struct walk is needed** — the present shim already
holds the live command buffer. (SMO only used Odyssey's `Application->drawContext`
for convenience; it is not the general pattern.)

## What landed in this branch (build-safe, behavior-neutral)

All of this compiles + ships today and changes nothing in the live build: the
renderer body is gated behind `SMBWAP_HAS_DEBUG_RENDERER`, which CMake defines
only when `lib/imgui` is checked out (it isn't by default).

- **`src/ui/ApDebugConsole.{cpp,hpp}`** — the overlay content + visibility gate.
  - Visibility: `(since_boot > 5s) AND (!connected) AND (since_disconnect > 5s)`.
  - Clock from `hk::svc::getSystemTick() / 19'200` (SMBW has no `ApState::nowMs()`).
  - Connection read straight off `ap::connState() == Ready`, disconnect tick
    self-latched in the draw loop (no `ApClient` edit).
  - **Widget-only**: `drawDebugConsole()` emits the ImGui window but does NOT
    call NewFrame/Render — the present shim owns the frame lifecycle.
- **`src/ui/ImGuiNvnBootstrap.{cpp,hpp}`** — the NVN present-hook glue, ported
  from the retired exlaunch `src/program/imgui_nvn.cpp` to hakkun:
  `HOOK_DEFINE_TRAMPOLINE`/`InstallAtSymbol` → `HkTrampoline` +
  `installAtSym<"nvnBootstrapLoader">()`. Captures Device/Queue/CommandBuffer via
  the GetProcAddress intercepts; the `presentTextureShim` runs `procDraw()`
  (`ImguiNvnBackend::newFrame` → `ImGui::NewFrame` → `drawDebugConsole()` →
  `ImGui::Render` → `ImguiNvnBackend::renderDrawData`) then chains the original.
  Drops the HID input-disable hooks (display-only) and the drawQueue (calls the
  overlay directly); ImGui allocator uses malloc/free instead of exlaunch's
  `nn::init::GetAllocator()`.
- **`src/ui/EmbeddedFontKarla.hpp`** — Karla TTF (OFL 1.1), loaded in `initImGui`.
- **`CMakeLists.txt`** — adds both sources; defines `SMBWAP_HAS_DEBUG_RENDERER`
  when `lib/imgui/imgui.h` exists.
- **`main.cpp`** — `initDebugConsole()` (stamps the boot tick) in the
  `GameFrameworkInitialize` pre-orig hook; `installNvnImGuiHooks()` in `hkMain`.
  Both are no-ops without the gate.

## What's left (needs the toolchain + a Switch/Ryujinx — can't be done in CI)

The bootstrap glue + overlay are complete. The one substantial remaining piece is
**bringing the NVN renderer backend into the hakkun build** — the bootstrap calls
`ImguiNvnBackend::{InitBackend,newFrame,renderDrawData}`, which today live only in
the retired exlaunch tree at `src/imgui_backend/` (`imgui_impl_nvn.cpp`,
`ImguiShaderCompiler`, `MemoryBuffer`, `MemoryPoolMaker`). Porting it requires:

1. **NVN SDK headers.** `imgui_impl_nvn` + the bootstrap include `nvn_Cpp.h` /
   `nvn_CppFuncPtrImpl.h` / `nvn_CppMethods.h`. These are **not in the repo**.
   Source them from LibHakkun's `addons/Nvn` (preferred — it vendors NVN
   bindings) or a vendored NVN header set, and add the include path to the gated
   CMake block (alongside `src/imgui_backend` and `src/glslc`).
2. **Shaders without the SD card.** `ImguiShaderCompiler` runtime-compiles GLSL
   via `glslc` and reads the shader files off the SD card (`FsHelper`). Either
   keep that (ship the shader files) or, cleaner, precompile + embed the control
   binary the way LibHakkun's ImGui addon does.
3. **De-exlaunch the backend.** Replace `EXL_ASSERT`/`EXL_ABORT`, `lib.hpp`,
   `nn::init::GetAllocator()`, and the `helpers/` (fsHelper, InputHelper) deps
   with hakkun/std equivalents (malloc/free is already wired in the bootstrap).
4. **Build config** (`config/config.cmake`): `set(USE_SAIL TRUE)` and add the NVN
   addon to `HAKKUN_ADDONS` (e.g. `Nvn`); init the submodule
   (`git submodule update --init switch-mod/lib/imgui`). Confirm `nvnBootstrapLoader`
   resolves — it's an `@sdk` symbol; under HK_DISABLE_SAIL `installAtSym` resolves
   it via `hk::ro::lookupSymbol`, so verify nnSdk exports it (or add a `.sym`).

### Alternative renderer: LibHakkun's `ImGuiBackendNvn`
Instead of porting the exlaunch backend you could point `procDraw()` at
LibHakkun's `hk::gfx::ImGuiBackendNvn` (vendored NVN bindings + embedded shaders,
no SD dependency). Caveat confirmed from its header: it has **no auto-draw**
(`draw(drawData, cmdBuf)` takes a caller-supplied command buffer — feed it our
captured `s_cmdBuf`), and it installs its **own** `nvnBootstrapLoader` hook via
`installHooks()`, which would collide with ours — so you'd either use its hooks
*or* ours, not both, and reconcile how it receives the device. Trade-off: less
code to port, but an unresolved device-handoff/double-hook question vs. the
exlaunch backend, whose `InitBackend({device,queue,cmdBuf})` matches our captured
trio exactly.

## Prior art / reference implementations
- **`fruityloops1/wondar`** — base project `switch-mod` was forked from; ships
  ImGui-in-Wonder ("Peepa"). Origin of `src/program/imgui_nvn.cpp` +
  `src/imgui_backend/`. (exlaunch.)
- **`Retinalogic/imgui-nvn`** — standalone game-agnostic ImGui-for-NVN.
- **`CraftyBoss/SMO-Exlaunch-Base`** / **`GLOSHSEP/s3_imgui_base`** — same
  NVN-present-hook lineage on other titles.
- **`fruityloops1/LibHakkun`** `addons/{Nvn,ImGui,DebugRenderer}` — maintained
  alternative backend (`ImGuiBackendNvn`).

## Smoke test once wired
Boot SMBW in Ryujinx with the **SMBW Client closed**: after ~10 s the overlay
should appear (`Bridge: DISCONNECTED` + the recent `[smbwap ...]` log tail).
Launch the client; on bridge `Ready` the overlay should vanish within a frame.
