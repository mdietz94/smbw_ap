# Handoff — on-Switch ImGui debug overlay (port from smo_archipelago)

Ports SMO's `ApDebugConsole` to SMBW: a Dear ImGui overlay that pops up on the
TV when the PC-side **SMBW Client** bridge has been unreachable for >5 s, showing
the connection state + the tail of the in-memory log ring, and hides instantly
when the bridge reconnects. Same idea as SMO's overlay (LibHakkun `Nvn`/`ImGui`/
`DebugRenderer` addons + Dear ImGui).

## What landed in this branch (portable, build-safe)

These compile and ship today; they change **nothing** in the live build because
the rendering body is gated behind `SMBWAP_HAS_DEBUG_RENDERER`, which CMake only
defines when the addon submodules are present (they aren't yet).

- `switch-mod/src/ui/ApDebugConsole.{cpp,hpp}` — the overlay. Ported 1:1 from
  SMO with these SMBW adaptations:
  - **Clock:** SMBW dropped `ApState::nowMs()`, so ms is derived from
    `hk::svc::getSystemTick() / 19'200` (19.2 MHz ARM generic timer — the same
    rate `probe/Gates.cpp` and `probe/DeathLink.cpp` already use).
  - **Connection signal:** read straight off `ap::connState()` (`== Ready`)
    each frame; the disconnect timestamp is self-latched in the draw loop. No
    `ApClient` edit, fully self-contained (SMO used an explicit
    `notifyConnectChange()` callback — not needed here).
  - **Discovery panel dropped:** SMBW's `ApDiscovery` exposes only
    `resolveBridge()`, no `DiscoveryReport` snapshot, so the overlay shows
    connection + log only.
- `switch-mod/src/ui/EmbeddedFontKarla.hpp` — Karla-Regular TTF (OFL 1.1) as a
  byte array, copied verbatim from SMO (namespace `smbwap::ui`). Atlas swap
  replaces the blurry 13px ProggyClean bitmap with crisp 22px Karla.
- `switch-mod/CMakeLists.txt` — adds `ApDebugConsole.cpp` to the source list and
  a gated include block: if `lib/imgui/imgui.h` **and** `sys/addons/ImGui/
  CMakeLists.txt` exist, it adds the imgui + addon include dirs and defines
  `SMBWAP_HAS_DEBUG_RENDERER=1`; otherwise it prints a "gated off" status.
- `switch-mod/src/main.cpp` — `smbwap::ui::initDebugConsole()` is now the first
  statement of the `GameFrameworkInitialize` **pre-orig** lambda (stamps the
  boot tick + builds the backend pre-orig when the addon is on — mirrors SMO's
  load-bearing pre-orig ordering). A `NOTE(imgui-overlay)` at `PlayerTickLatch`
  documents why the per-frame draw is *not* wired there.

With the addon off, `initDebugConsole()` just stamps a tick + logs one line, and
`drawDebugConsole()` is a no-op. The live build is byte-for-byte unaffected in
behavior.

## What's left (needs the toolchain + a Switch/Ryujinx — can't be done in CI)

### 0. Build-config flips (`switch-mod/config/config.cmake`)
```cmake
set(USE_SAIL TRUE)                                # NVN symbols need sail
set(HAKKUN_ADDONS HeapSourceDynamic Nvn ImGui DebugRenderer)
```
and check out the submodules:
```sh
git submodule update --init --recursive switch-mod/sys switch-mod/lib/imgui
```
(`switch-mod/sys` = LibHakkun, `switch-mod/lib/imgui` = Dear ImGui, `docking`
branch per `.gitmodules`.) Confirm LibHakkun's pinned commit actually ships
`sys/addons/{Nvn,ImGui,DebugRenderer}/` — SMO pins LibHakkun to a `main` commit
that has them; SMBW's pin may need bumping to match. Add an `nvn.sym`
(`nvnBootstrapLoader`, tagged `@sdk`) under `switch-mod/syms/` like SMO's, and
install the bootstrap hook in `hkMain`:
`hk::gfx::ImGuiBackendNvn::instance()->installHooks(false);`

### RE item #1 — ImGui heap source (`ensureSetup()` in ApDebugConsole.cpp)
SMBW is **not** the Odyssey `al::` engine, so `al::getStationedHeap()` does not
exist. `ensureSetup()` has a `parent = nullptr` placeholder with a TODO. Wire it
to SMBW's sead root/stationed heap — most likely
`sead::HeapMgr::instance()->getRootHeap(0)` (the relevant symbols are already
imported: `syms/100/sead/heap/seadHeapMgr.sym`, `seadExpHeap.sym`). Allocate the
2 MiB ExpHeap pre-orig (before the engine fragments the heap) — this is the
load-bearing ordering invariant that bit SMO seven times.

### RE item #2 — per-frame NVN command buffer + draw call-site (`drawDebugConsole()`)

**Good news from prior art (2026-06): this is NOT a Wonder-specific RE problem.**
The clean way to drive ImGui on any NVN Switch title is the **game-agnostic NVN
present hook**, not a per-game draw function:

  * Hook the SDK symbol `nvnBootstrapLoader` (resolvable via `installAtSym`,
    tagged `@sdk` — same as SMO's `nvn.sym`).
  * From it, intercept `nvnDeviceGetProcAddress`; when the game asks for
    `nvnCommandBufferInitialize` / `nvnQueuePresentTexture`, capture the
    `nvn::Device*` / `nvn::Queue*` / `nvn::CommandBuffer*` and return your own
    `presentTexture` shim.
  * Your `presentTexture` shim calls the ImGui draw **then** chains the original
    present. Because it runs from the present call, you already hold the command
    buffer — **no `HakoniwaSequence::drawMain` analog and no `Application`
    struct walk are needed**. SMO only used Odyssey's `Application->...
    ->getCommandBuffer()` for convenience; it is not the general pattern.

So the `NOTE(imgui-overlay)` at `PlayerTickLatch` in main.cpp stands (don't draw
from a logic tick), but the real call-site is the **present shim**, which you
install yourself — there is nothing to find in Ghidra for the draw path.

This exact pattern already lives in this repo's retired exlaunch tree, inherited
from wondar (where ImGui-in-Wonder is proven — see "Prior art" below):

  * `switch-mod/src/program/imgui_nvn.cpp` — `nvnBootstrapLoader` hook →
    `nvnDeviceGetProcAddress` intercept → captures device/queue/cmdbuf →
    `presentTexture()` shim calls `nvnImGui::procDraw()` then the original.
  * `switch-mod/src/imgui_backend/imgui_impl_nvn.{cpp,hpp}` + `ImguiShaderCompiler`
    + `MemoryBuffer`/`MemoryPoolMaker` — a complete self-contained NVN ImGui
    backend (shaders, font texture, vtx/idx buffers).

Two implementation options (pick one):

  * **(A) LibHakkun addon renderer + present-hook plumbing.** Keep
    `ApDebugConsole.cpp` as written against `hk::gfx::ImGuiBackendNvn`, but feed
    its `draw(drawData, cmdBuf)` from a wondar-style present shim (LibHakkun's
    `ImGuiBackendNvn` has **no auto-draw** — confirmed: `draw()` takes a
    caller-supplied `cmdBuf`, and `installHooks(bool)`'s bool only controls
    auto-*init*, not auto-draw). Capture `cmdBuf` via the GetProcAddress
    intercept above. This is the smallest delta from what's committed.
  * **(B) Port the retired exlaunch backend wholesale to hakkun.** Convert
    `HOOK_DEFINE_TRAMPOLINE` / `InstallAtSymbol("nvnBootstrapLoader")` to
    `HkTrampoline<…>::installAtSym<"nvnBootstrapLoader">()`, and have
    `ApDebugConsole`'s render body call `nvnImGui::addDrawFunc(...)` instead of
    `ImGuiBackendNvn`. Reuses Wonder-proven code; avoids LibHakkun's ImGui addon
    (so HAKKUN_ADDONS would only need `Nvn`, or none, depending on shader path).

### Prior art / reference implementations (from a 2026-06 web search)

  * **`fruityloops1/wondar`** — the base project SMBW's `switch-mod` was forked
    from; ships an ImGui-based in-game tool ("Peepa", `sd/Peepa/ImGuiData`). This
    is the upstream origin of the retired `src/program/imgui_nvn.cpp` +
    `src/imgui_backend/` here. **Proves ImGui rendering works in Wonder v1.0.0.**
    (exlaunch-based.)
  * **`Retinalogic/imgui-nvn`** — standalone, game-agnostic ImGui-for-NVN. Entry
    points `nvnImguiInitialize` / `nvnImguiCalc` (per-frame, returns `ImDrawData`).
    "Hooks nvn functions, so it might work with any executable." Confirms the
    present-hook approach is title-independent.
  * **`CraftyBoss/SMO-Exlaunch-Base`** / **`GLOSHSEP/s3_imgui_base`** (Splatoon 3)
    — same NVN-present-hook lineage applied to other titles.
  * LibHakkun addons live at `addons/{Nvn,ImGui,DebugRenderer}` upstream (→
    `sys/addons/...` after submodule checkout); `ImGuiBackendNvn` is the class
    `ApDebugConsole.cpp` is currently written against.

## Smoke test once wired
Boot SMBW in Ryujinx with the **SMBW Client closed**: after ~10 s the overlay
should appear showing `Bridge: DISCONNECTED` + the recent `[smbwap ...]` log
tail. Launch the client; on bridge `Ready` the overlay should vanish within a
frame. (Mirrors SMO's overlay smoke test.)
