// NVN present-hook bootstrap — see ImGuiNvnBootstrap.hpp.
//
// Renderer: LibHakkun's hk::gfx::ImGuiBackendNvn (the Nvn + ImGui addons),
// which vendors self-contained NVN bindings + an embedded ImGui shader (no SD
// card, no runtime glslc, no exlaunch backend to port). We drive it from our
// OWN single nvnBootstrapLoader trampoline rather than the addon's auto-hook
// path (ImGuiBackendNvn::installHooks), so there is exactly one bootstrap hook
// and no collision:
//
//   * We capture Device/Queue/CommandBuffer + the live texture/sampler pools
//     from the nvnDeviceGetProcAddress intercepts and feed them to the backend
//     via setDevice / setPrevTexturePool / setPrevSamplerPool.
//   * The backend's draw() records into a caller-supplied command buffer but
//     does NOT begin/submit (its "no auto-draw"), so the present shim wraps it
//     in BeginRecording / EndRecording / SubmitCommands on our captured trio —
//     the same self-contained submit the retired exlaunch backend did.
//
// Why this over porting the exlaunch src/imgui_backend: that path needs a
// precompiled imgui.bin shader we don't have (or runtime glslc + an SD read)
// plus de-exlaunching EXL_*/lib.hpp/FsHelper/InputHelper. The LibHakkun backend
// sidesteps all of it. See docs/handoff-imgui-overlay.md ("Chosen renderer").
//
// Whole TU is gated by SMBWAP_HAS_DEBUG_RENDERER (CMake defines it only when
// switch-mod/lib/imgui is checked out, which is also what enables the Nvn/ImGui
// addons). With it off this is a no-op and the live build is unchanged.

#include "ImGuiNvnBootstrap.hpp"

#ifdef SMBWAP_HAS_DEBUG_RENDERER

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>

#include "hk/hook/Trampoline.h"

#include "hk/nvn/nvn_Cpp.h"
#include "hk/nvn/nvn_CppFuncPtrBase.h"  // func typedefs + nvn::nvnLoadCPPProcs
#include "hk/nvn/nvn_CppMethods.h"      // inline CommandBuffer/Queue methods

#include "hk/gfx/ImGuiBackendNvn.h"
#include "imgui.h"

#include "ApDebugConsole.hpp"
#include "EmbeddedFontKarla.hpp"
#include "../util/Log.hpp"

namespace smbwap::ui {

namespace {

// Captured NVN objects (set by the GetProcAddress intercepts below).
nvn::Device*        s_device  = nullptr;
nvn::Queue*         s_queue   = nullptr;

// The command buffer we record the overlay into.  This is the GAME's own
// command buffer, pinned at the first nvnCommandBufferInitialize and reused
// for the process lifetime (the proven exlaunch-lineage recipe — see
// src/program/imgui_backend/imgui_impl_nvn.cpp: the backend captured the first
// game cmdBuf at InitBackend and called BeginRecording/EndRecording/Submit on
// it every present, never owning its own command memory).
//
// Why NOT a dedicated cmdBuf we allocate ourselves: Ryujinx's NVN emulation
// rejects BeginRecording on any cmdBuf whose command-memory pool we set up via
// AddCommandMemory (heap OR BSS) — it aborts inside nvnCommandBufferBeginRecording
// (nnSdk:0x442b80).  Only the game's cmdBufs, whose command memory the game
// registered through paths the emulator accepts, survive BeginRecording.
//
// Why pin + re-pin (not track-latest): the game finalizes (frees) its cmdBufs
// at scene transitions (title -> save selector).  Tracking the *latest* one
// left a dangling pointer when that cmdBuf was freed mid-overlay, faulting in
// BeginRecording on freed memory (nnSdk:0x442ad0).  We instead pin the cmdBuf
// and DROP it the instant the game finalizes it (cmdBufFinalizeShim), re-pinning
// on the next nvnCommandBufferInitialize.  procDraw null-checks every frame, so
// the brief gap where no cmdBuf is pinned just skips a frame — no dangle.
//
// Atomic because cmdBufFinalizeShim (main/scene thread) can null it while
// presentTextureShim (presentation thread) reads it.
std::atomic<nvn::CommandBuffer*> s_imguiCmdBuf{nullptr};

// Saved originals.
nvn::DeviceGetProcAddressFunc        s_origGetProcAddress = nullptr;
nvn::DeviceInitializeFunc            s_origDeviceInit     = nullptr;
nvn::QueueInitializeFunc             s_origQueueInit      = nullptr;
nvn::CommandBufferInitializeFunc     s_origCmdBufInit     = nullptr;
nvn::QueuePresentTextureFunc         s_origPresentTexture = nullptr;
nvn::CommandBufferSetTexturePoolFunc s_origSetTexturePool = nullptr;
nvn::CommandBufferSetSamplerPoolFunc s_origSetSamplerPool = nullptr;
nvn::WindowSetCropFunc               s_origWindowSetCrop  = nullptr;
nvn::CommandBufferSetViewportFunc    s_origSetViewport    = nullptr;
// Finalize originals — hooked so we can drop our captured Queue/CommandBuffer
// pointers the instant the game tears them down (see the scene-transition
// crash note on s_drawArmed below).
nvn::CommandBufferFinalizeFunc            s_origCmdBufFinalize    = nullptr;
nvn::QueueFinalizeFunc                    s_origQueueFinalize     = nullptr;

// Draw-gate state. init-readiness (the backend/context/atlas are up) is NOT
// the same as draw-readiness (it is actually safe to record + submit a frame),
// and conflating the two is what crashed boot: the first nvnQueuePresentTexture
// could fire before the game had bound its texture/sampler pools, so the
// backend's draw() recorded against a null pool and faulted inside the NVN
// driver (nnSdk) on the Presentation Thread — intermittently, depending on
// whether the pool-set calls happened to land before that first present.
//
// All of these are written from the NVN init / set-pool / present shims, which
// in practice run on the single render thread; we still use std::atomic per the
// subsdk no-thread_local rule (CLAUDE.md gotcha #1) and to make the ordering
// explicit. The captured Device/Queue/CommandBuffer above are set once during
// graphics bring-up and only read after s_imguiInited is observed true.
std::atomic<bool> s_imguiInited{false};
// Both pools must have been seen at least once before draw() is safe — these
// flip when the game calls nvnCommandBufferSet{Texture,Sampler}Pool, which is
// also when we forward the live pool to the backend (set*PoolShim below).
std::atomic<bool> s_texturePoolSeen{false};
std::atomic<bool> s_samplerPoolSeen{false};
// Warm-up: even once everything is wired, hold off drawing for a stretch of
// presents so we never paint into a half-settled swapchain during boot. The
// overlay is a debug aid with no urgency (per maintainer: fine to gate "until
// safe"), so we err heavily toward late. Counting presents is the natural
// render-thread clock — there is no cheap wall-clock here. ~300 presents is
// roughly 5s at 60fps (longer at 30fps; that is fine).
constexpr int32_t kWarmupPresents = 300;
std::atomic<int32_t> s_warmupPresents{0};
// Latched true once the full gate first passes; thereafter the present shim
// takes a cheap fast path. Never cleared (the captured objects live for the
// process), which also avoids the warm-up counter wrapping.
std::atomic<bool> s_drawArmed{false};

hk::gfx::ImGuiBackendNvn* backend() { return hk::gfx::ImGuiBackendNvn::instance(); }

// Gate the per-frame overlay draw. Returns true only when it is safe to record
// and submit an ImGui frame on the captured NVN objects. Once it returns true
// it latches, so the steady-state cost is a single relaxed atomic load.
bool overlayDrawReady() {
    if (s_drawArmed.load(std::memory_order_acquire)) return true;

    // Backend + context + atlas are up.
    if (!s_imguiInited.load(std::memory_order_acquire)) return false;
    // A game cmdBuf must be pinned; the present queue comes from the caller.
    if (s_device == nullptr ||
        s_imguiCmdBuf.load(std::memory_order_acquire) == nullptr) return false;
    if (!backend()->isInitialized()) return false;
    // Pool preconditions (s_texturePoolSeen / s_samplerPoolSeen) are intentionally
    // omitted here. On Ryujinx the emulated NVN never routes through our
    // setTexturePool/setSamplerPool shims, so those flags stay false forever and
    // the draw gate would never arm. The pool shims still forward pool pointers to
    // the backend when they DO fire (real hardware), so draw() gets live pools
    // there. On Ryujinx the backend handles null pools without crashing (the
    // original boot-crash was the dangling cmdBuf, fixed by the finalize hooks).

    // Everything is wired — start (or continue) the warm-up countdown. The
    // counter only advances from this known-good state, so the warm-up measures
    // settled presents rather than presents-since-boot.
    const int32_t seen =
        s_warmupPresents.fetch_add(1, std::memory_order_relaxed) + 1;
    if (seen < kWarmupPresents) return false;

    s_drawArmed.store(true, std::memory_order_release);
    SMBWAP_LOG_INFO("[overlay] draw gate armed (%d warm-up presents elapsed)",
                    static_cast<int>(seen));
    return true;
}

// ImGui + the NVN backend allocate GPU-mappable memory (vertex/index/shader/
// texture pools) through this allocator; the pool allocations demand page
// alignment, so back it with aligned_alloc. aligned_alloc requires a
// power-of-two alignment and a size that is a multiple of it.
void* imguiAlloc(::size sz, ::size align) {
    if (align < alignof(std::max_align_t)) align = alignof(std::max_align_t);
    const ::size rounded = (sz + align - 1) & ~(align - 1);
    return std::aligned_alloc(align, rounded);
}
void imguiFree(void* p) { std::free(p); }

bool initImGui() {
    if (!s_device) return false;

    auto* bd = backend();
    bd->setAllocator({ &imguiAlloc, &imguiFree });
    bd->setDevice(s_device);

    // tryInitialize() sets ImGui's allocators (we built with
    // IMGUI_DISABLE_DEFAULT_ALLOCATORS), creates the context, and bakes the
    // default font atlas + uploads the embedded shader. Returns false if it was
    // already initialized.
    if (!bd->tryInitialize()) {
        SMBWAP_LOG_WARN("[overlay] backend tryInitialize() returned false");
        return bd->isInitialized();
    }

    // Swap the blurry 13px ProggyClean bitmap for Karla 22px, then re-bake the
    // atlas. The backend binds the font texture by fallback (ImDrawCmd carries
    // no TexID), so re-baking is all that's needed — no SetTexID dance.
    ImGuiIO& io = ImGui::GetIO();
    io.Fonts->Clear();
    ImFontConfig cfg;
    cfg.FontDataOwnedByAtlas = false;  // backing array is our static const
    cfg.OversampleH = 2;
    cfg.OversampleV = 1;
    cfg.PixelSnapH  = false;
    if (!io.Fonts->AddFontFromMemoryTTF(
            const_cast<unsigned char*>(kKarlaRegularTtfData),
            static_cast<int>(kKarlaRegularTtfSize), 22.0f, &cfg)) {
        SMBWAP_LOG_WARN("[overlay] Karla load failed; using default font");
        io.Fonts->AddFontDefault();
    }
    bd->initTexture(/*useLinearFilter=*/false);

    SMBWAP_LOG_INFO("[overlay] ImGui NVN backend up (Karla 22px)");
    return true;
}

// Drive one ImGui frame on the pinned game cmdBuf.  At present time the game
// has finished + submitted its frame, so the cmdBuf is in INITIAL state and
// safe to BeginRecording; our commands draw on top of the game's frame and are
// submitted before the original present chains.  The queue comes from
// presentTextureShim.  We load the pinned cmdBuf once and null-check it here so
// a finalize on another thread (scene transition) cleanly skips the frame.
void procDraw(nvn::Queue* queue) {
    auto* bd     = backend();
    auto* cmdBuf = s_imguiCmdBuf.load(std::memory_order_acquire);
    if (bd == nullptr || cmdBuf == nullptr || queue == nullptr) return;
    if (!bd->isInitialized()) return;

    ImGuiIO& io = ImGui::GetIO();
    io.DeltaTime = 1.0f / 60.0f;

    bd->update();
    ImGui::NewFrame();
    drawDebugConsole();
    ImGui::Render();

    const auto* drawData = ImGui::GetDrawData();
    cmdBuf->BeginRecording();
    if (drawData) bd->draw(drawData, cmdBuf);
    nvn::CommandHandle handle = cmdBuf->EndRecording();
    queue->SubmitCommands(1, &handle);
}

// ---- NVN proc shims (returned to the game from getProc) ------------------

void setTexturePoolShim(nvn::CommandBuffer* cmdBuf, const nvn::TexturePool* pool) {
    // Null-guard: the ImGui backend calls SetTexturePool(null) at the end of
    // draw() to restore mPrevTexturePool — null when Ryujinx never routes the
    // game's SetTexturePool calls through our shim.  Setting a null pool aborts
    // NVN on both emulator and hardware.  Skip null; only forward valid pools.
    if (pool == nullptr) return;
    s_origSetTexturePool(cmdBuf, pool);
    backend()->setPrevTexturePool(const_cast<nvn::TexturePool*>(pool));
    s_texturePoolSeen.store(true, std::memory_order_release);
}

void setSamplerPoolShim(nvn::CommandBuffer* cmdBuf, const nvn::SamplerPool* pool) {
    if (pool == nullptr) return;  // same null-guard as setTexturePoolShim
    s_origSetSamplerPool(cmdBuf, pool);
    backend()->setPrevSamplerPool(const_cast<nvn::SamplerPool*>(pool));
    s_samplerPoolSeen.store(true, std::memory_order_release);
}

void windowSetCropShim(nvn::Window* window, int x, int y, int w, int h) {
    s_origWindowSetCrop(window, x, y, w, h);
    if (w > 0 && h > 0) {
        backend()->setResolution({ static_cast<float>(w), static_cast<float>(h) });
    }
}

// The viewport set by the game's command buffer gives us the display size
// that ImGui must render into. This is the reliable source: nvnWindowSetCrop
// may never fire in SMBW, so we can't rely on it alone. Mirror the original
// exlaunch approach (src/program/imgui_nvn.cpp). Only update for "full-screen"
// viewports (both w and h >= 400) so shadow-map / post-process sub-passes
// don't corrupt the display size we're accumulating.
void setViewportShim(nvn::CommandBuffer* cmdBuf, int x, int y, int w, int h) {
    s_origSetViewport(cmdBuf, x, y, w, h);
    if (w >= 400 && h >= 400) {
        backend()->setResolution({ static_cast<float>(w - x), static_cast<float>(h - y) });
    }
}

void presentTextureShim(nvn::Queue* queue, nvn::Window* window, int texIndex) {
    if (overlayDrawReady()) procDraw(queue);
    s_origPresentTexture(queue, window, texIndex);
}

NVNboolean deviceInitShim(nvn::Device* device, const nvn::DeviceBuilder* builder) {
    NVNboolean result = s_origDeviceInit(device, builder);
    s_device = device;
    // Populate the nvn:: C++ method function pointers (BeginRecording, draw
    // state, etc.) so the backend's inline method calls resolve.
    nvn::nvnLoadCPPProcs(s_device, s_origGetProcAddress);
    backend()->setDevice(s_device);
    return result;
}

NVNboolean queueInitShim(nvn::Queue* queue, const nvn::QueueBuilder* builder) {
    NVNboolean result = s_origQueueInit(queue, builder);
    s_queue = queue;
    return result;
}

// Rate-limited lifecycle logging (saturating-CAS budget, same idiom as the WS
// reader substitute) so a chatty boot/transition doesn't flood the log.
bool lifecycleLogBudget() {
    static std::atomic<int32_t> budget{64};
    int32_t b = budget.load(std::memory_order_relaxed);
    while (b > 0 && !budget.compare_exchange_weak(b, b - 1, std::memory_order_relaxed)) {}
    return b > 0;
}

NVNboolean cmdBufInitShim(nvn::CommandBuffer* buffer, nvn::Device* device) {
    NVNboolean result = s_origCmdBufInit(buffer, device);
    // Pin the game's cmdBuf for overlay recording.  We pin the FIRST one the
    // game creates and reuse it for the process lifetime (proven exlaunch
    // recipe).  If it is later finalized at a scene transition,
    // cmdBufFinalizeShim clears the pin and we re-pin the next one here.
    nvn::CommandBuffer* expected = nullptr;
    if (s_imguiCmdBuf.compare_exchange_strong(expected, buffer,
                                              std::memory_order_acq_rel)) {
        if (lifecycleLogBudget()) {
            SMBWAP_LOG_INFO("[overlay] pinned game cmdBuf %p for overlay recording",
                            buffer);
        }
    }
    if (!s_imguiInited.load(std::memory_order_acquire)) {
        if (initImGui()) s_imguiInited.store(true, std::memory_order_release);
    }
    return result;
}

// The game finalizes (frees) its command buffers at scene transitions —
// notably title -> save-data selector. If our pinned overlay cmdBuf is the one
// being torn down, drop the pin BEFORE the game frees it so the presentation
// thread's procDraw() skips cleanly (null-checked each frame) instead of
// recording into freed memory (the nnSdk:0x442ad0 BeginRecording fault).
// cmdBufInitShim re-pins the next cmdBuf the game creates.
void cmdBufFinalizeShim(nvn::CommandBuffer* buffer) {
    nvn::CommandBuffer* pinned = buffer;
    if (s_imguiCmdBuf.compare_exchange_strong(pinned, nullptr,
                                              std::memory_order_acq_rel)) {
        if (lifecycleLogBudget()) {
            SMBWAP_LOG_INFO("[overlay] cmdBufFinalize: dropped pinned cmdBuf %p", buffer);
        }
    }
    s_origCmdBufFinalize(buffer);
}

void queueFinalizeShim(nvn::Queue* queue) {
    if (queue == s_queue) {
        s_queue = nullptr;
        if (lifecycleLogBudget()) {
            SMBWAP_LOG_INFO("[overlay] queueFinalize: dropped captured s_queue %p", queue);
        }
    }
    s_origQueueFinalize(queue);
}

nvn::GenericFuncPtrFunc getProcShim(nvn::Device* device, const char* procName) {
    nvn::GenericFuncPtrFunc ptr = s_origGetProcAddress(device, procName);
    if (std::strcmp(procName, "nvnQueueInitialize") == 0) {
        s_origQueueInit = reinterpret_cast<nvn::QueueInitializeFunc>(ptr);
        return reinterpret_cast<nvn::GenericFuncPtrFunc>(&queueInitShim);
    }
    if (std::strcmp(procName, "nvnCommandBufferInitialize") == 0) {
        s_origCmdBufInit = reinterpret_cast<nvn::CommandBufferInitializeFunc>(ptr);
        return reinterpret_cast<nvn::GenericFuncPtrFunc>(&cmdBufInitShim);
    }
    if (std::strcmp(procName, "nvnCommandBufferSetTexturePool") == 0) {
        s_origSetTexturePool = reinterpret_cast<nvn::CommandBufferSetTexturePoolFunc>(ptr);
        return reinterpret_cast<nvn::GenericFuncPtrFunc>(&setTexturePoolShim);
    }
    if (std::strcmp(procName, "nvnCommandBufferSetSamplerPool") == 0) {
        s_origSetSamplerPool = reinterpret_cast<nvn::CommandBufferSetSamplerPoolFunc>(ptr);
        return reinterpret_cast<nvn::GenericFuncPtrFunc>(&setSamplerPoolShim);
    }
    if (std::strcmp(procName, "nvnWindowSetCrop") == 0) {
        s_origWindowSetCrop = reinterpret_cast<nvn::WindowSetCropFunc>(ptr);
        return reinterpret_cast<nvn::GenericFuncPtrFunc>(&windowSetCropShim);
    }
    if (std::strcmp(procName, "nvnCommandBufferSetViewport") == 0) {
        s_origSetViewport = reinterpret_cast<nvn::CommandBufferSetViewportFunc>(ptr);
        return reinterpret_cast<nvn::GenericFuncPtrFunc>(&setViewportShim);
    }
    if (std::strcmp(procName, "nvnQueuePresentTexture") == 0) {
        s_origPresentTexture = reinterpret_cast<nvn::QueuePresentTextureFunc>(ptr);
        SMBWAP_LOG_INFO("[overlay] nvnQueuePresentTexture hooked (orig=%p)", ptr);
        return reinterpret_cast<nvn::GenericFuncPtrFunc>(&presentTextureShim);
    }
    if (std::strcmp(procName, "nvnCommandBufferFinalize") == 0) {
        s_origCmdBufFinalize = reinterpret_cast<nvn::CommandBufferFinalizeFunc>(ptr);
        return reinterpret_cast<nvn::GenericFuncPtrFunc>(&cmdBufFinalizeShim);
    }
    if (std::strcmp(procName, "nvnQueueFinalize") == 0) {
        s_origQueueFinalize = reinterpret_cast<nvn::QueueFinalizeFunc>(ptr);
        return reinterpret_cast<nvn::GenericFuncPtrFunc>(&queueFinalizeShim);
    }
    return ptr;
}

// ---- nvnBootstrapLoader trampoline ---------------------------------------

HkTrampoline<nvn::GenericFuncPtrFunc, const char*> nvnBootstrapHook =
    hk::hook::trampoline([](const char* funcName) -> nvn::GenericFuncPtrFunc {
        nvn::GenericFuncPtrFunc result = nvnBootstrapHook.orig(funcName);
        if (std::strcmp(funcName, "nvnDeviceInitialize") == 0) {
            s_origDeviceInit = reinterpret_cast<nvn::DeviceInitializeFunc>(result);
            return reinterpret_cast<nvn::GenericFuncPtrFunc>(&deviceInitShim);
        }
        if (std::strcmp(funcName, "nvnDeviceGetProcAddress") == 0) {
            s_origGetProcAddress = reinterpret_cast<nvn::DeviceGetProcAddressFunc>(result);
            return reinterpret_cast<nvn::GenericFuncPtrFunc>(&getProcShim);
        }
        return result;
    });

}  // namespace

void installNvnImGuiHooks() {
    const hk::Result rc = nvnBootstrapHook.installAtSym<"nvnBootstrapLoader">();
    if (rc.failed()) {
        SMBWAP_LOG_ERROR(
            "[overlay] nvnBootstrapLoader hook install FAILED rc=0x%x "
            "(symbol unresolved under HK_DISABLE_SAIL?) — overlay disabled",
            static_cast<unsigned>(rc.getValue()));
    } else {
        SMBWAP_LOG_INFO("[overlay] NVN bootstrap hook installed OK");
    }
}

}  // namespace smbwap::ui

#else  // SMBWAP_HAS_DEBUG_RENDERER

namespace smbwap::ui {
void installNvnImGuiHooks() {}  // no renderer compiled in
}  // namespace smbwap::ui

#endif  // SMBWAP_HAS_DEBUG_RENDERER
