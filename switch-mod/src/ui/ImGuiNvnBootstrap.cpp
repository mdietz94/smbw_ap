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

#include <cstddef>
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
nvn::CommandBuffer* s_cmdBuf  = nullptr;

// Saved originals.
nvn::DeviceGetProcAddressFunc        s_origGetProcAddress = nullptr;
nvn::DeviceInitializeFunc            s_origDeviceInit     = nullptr;
nvn::QueueInitializeFunc             s_origQueueInit      = nullptr;
nvn::CommandBufferInitializeFunc     s_origCmdBufInit     = nullptr;
nvn::QueuePresentTextureFunc         s_origPresentTexture = nullptr;
nvn::CommandBufferSetTexturePoolFunc s_origSetTexturePool = nullptr;
nvn::CommandBufferSetSamplerPoolFunc s_origSetSamplerPool = nullptr;
nvn::WindowSetCropFunc               s_origWindowSetCrop  = nullptr;

bool s_imguiInited = false;

hk::gfx::ImGuiBackendNvn* backend() { return hk::gfx::ImGuiBackendNvn::instance(); }

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
    if (!(s_device && s_queue && s_cmdBuf)) return false;

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

// Drive one ImGui frame from the present shim, where s_cmdBuf is idle (the game
// already submitted this frame) and the swapchain texture is still the bound
// render target — the wondar/exlaunch present-hook invariant.
void procDraw() {
    auto* bd = backend();

    ImGuiIO& io = ImGui::GetIO();
    io.DeltaTime = 1.0f / 60.0f;  // display-only; a fixed step keeps NewFrame happy

    bd->update();
    ImGui::NewFrame();
    drawDebugConsole();  // emits the overlay window only when visible
    ImGui::Render();

    // The backend records ImGui draws but neither begins nor submits — we own
    // the recording lifecycle on the captured command buffer + queue.
    s_cmdBuf->BeginRecording();
    bd->draw(ImGui::GetDrawData(), s_cmdBuf);
    nvn::CommandHandle handle = s_cmdBuf->EndRecording();
    s_queue->SubmitCommands(1, &handle);
}

// ---- NVN proc shims (returned to the game from getProc) ------------------

void setTexturePoolShim(nvn::CommandBuffer* cmdBuf, const nvn::TexturePool* pool) {
    s_origSetTexturePool(cmdBuf, pool);
    backend()->setPrevTexturePool(const_cast<nvn::TexturePool*>(pool));
}

void setSamplerPoolShim(nvn::CommandBuffer* cmdBuf, const nvn::SamplerPool* pool) {
    s_origSetSamplerPool(cmdBuf, pool);
    backend()->setPrevSamplerPool(const_cast<nvn::SamplerPool*>(pool));
}

void windowSetCropShim(nvn::Window* window, int x, int y, int w, int h) {
    s_origWindowSetCrop(window, x, y, w, h);
    if (w > 0 && h > 0) {
        backend()->setResolution({ static_cast<float>(w), static_cast<float>(h) });
    }
}

void presentTextureShim(nvn::Queue* queue, nvn::Window* window, int texIndex) {
    if (s_imguiInited) procDraw();
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

NVNboolean cmdBufInitShim(nvn::CommandBuffer* buffer, nvn::Device* device) {
    NVNboolean result = s_origCmdBufInit(buffer, device);
    s_cmdBuf = buffer;
    if (!s_imguiInited) s_imguiInited = initImGui();
    return result;
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
    if (std::strcmp(procName, "nvnQueuePresentTexture") == 0) {
        s_origPresentTexture = reinterpret_cast<nvn::QueuePresentTextureFunc>(ptr);
        return reinterpret_cast<nvn::GenericFuncPtrFunc>(&presentTextureShim);
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
    nvnBootstrapHook.installAtSym<"nvnBootstrapLoader">();
    SMBWAP_LOG_INFO("[overlay] NVN bootstrap hook installed");
}

}  // namespace smbwap::ui

#else  // SMBWAP_HAS_DEBUG_RENDERER

namespace smbwap::ui {
void installNvnImGuiHooks() {}  // no renderer compiled in
}  // namespace smbwap::ui

#endif  // SMBWAP_HAS_DEBUG_RENDERER
