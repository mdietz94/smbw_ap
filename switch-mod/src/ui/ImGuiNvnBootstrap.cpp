// NVN present-hook bootstrap — see ImGuiNvnBootstrap.hpp.
//
// Hakkun port of src/program/imgui_nvn.cpp (retired exlaunch tree, inherited
// from fruityloops1/wondar where ImGui-in-Wonder is proven). Differences from
// the exlaunch original:
//   * HOOK_DEFINE_TRAMPOLINE / InstallAtSymbol  ->  HkTrampoline +
//     installAtSym<"nvnBootstrapLoader">() (the live hakkun idiom).
//   * drawQueue / addDrawFunc dropped — procDraw() calls drawDebugConsole()
//     directly (the overlay is the only draw client).
//   * HID input-disable hooks dropped — the overlay is display-only in this
//     iteration (matches the SMO overlay's "no input wiring" scope).
//   * ImGui allocator uses malloc/free (resolved at load via nnrtld) instead
//     of exlaunch's nn::init::GetAllocator().
//
// NOTE: this references the retired NVN renderer backend
// (src/imgui_backend/ImguiNvnBackend) and the NVN C++ bindings (nvn_Cpp.h).
// Both still need to be brought into the hakkun build — they pull NVN SDK
// headers, a glslc shader path, and exlaunch helpers. That is the remaining
// work tracked in docs/handoff-imgui-overlay.md. Until then this TU is gated
// out (SMBWAP_HAS_DEBUG_RENDERER undefined) and the live build is unchanged.

#include "ImGuiNvnBootstrap.hpp"

#ifdef SMBWAP_HAS_DEBUG_RENDERER

#include <cstdlib>
#include <cstring>

#include "hk/hook/Trampoline.h"

#include "imgui.h"
#include "nvn_Cpp.h"
#include "nvn_CppFuncPtrImpl.h"

#include "ApDebugConsole.hpp"
#include "EmbeddedFontKarla.hpp"
#include "../imgui_backend/imgui_impl_nvn.hpp"  // ImguiNvnBackend (retired; see note)
#include "../util/Log.hpp"

namespace smbwap::ui {

namespace {

// Captured NVN objects (set by the GetProcAddress intercepts below).
nvn::Device*        s_device  = nullptr;
nvn::Queue*         s_queue   = nullptr;
nvn::CommandBuffer* s_cmdBuf  = nullptr;

// Saved originals.
nvn::DeviceGetProcAddressFunc      s_origGetProcAddress = nullptr;
nvn::DeviceInitializeFunc          s_origDeviceInit     = nullptr;
nvn::QueueInitializeFunc           s_origQueueInit      = nullptr;
nvn::CommandBufferInitializeFunc   s_origCmdBufInit     = nullptr;
nvn::QueuePresentTextureFunc       s_origPresentTexture = nullptr;
nvn::CommandBufferSetViewportFunc  s_origSetViewport    = nullptr;

bool s_imguiInited = false;

bool initImGui() {
    if (!(s_device && s_queue && s_cmdBuf)) return false;

    IMGUI_CHECKVERSION();
    ImGui::SetAllocatorFunctions(
        [](std::size_t size, void*) -> void* { return std::malloc(size); },
        [](void* p, void*) -> void { std::free(p); },
        nullptr);
    ImGui::CreateContext();
    ImGui::StyleColorsDark();

    // Karla 22px instead of the blurry 13px ProggyClean bitmap. Added before
    // InitBackend so the backend bakes/uploads the right atlas.
    ImGuiIO& io = ImGui::GetIO();
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

    ImguiNvnBackend::NvnBackendInitInfo initInfo = {
        .device = s_device,
        .queue  = s_queue,
        .cmdBuf = s_cmdBuf,
    };
    ImguiNvnBackend::InitBackend(initInfo);
    SMBWAP_LOG_INFO("[overlay] ImGui NVN backend up (Karla 22px)");
    return true;
}

// Drive one ImGui frame. Runs from the present shim, so s_cmdBuf is live.
void procDraw() {
    ImguiNvnBackend::newFrame();
    ImGui::NewFrame();
    drawDebugConsole();  // emits the overlay window only if visible
    ImGui::Render();
    ImguiNvnBackend::renderDrawData(ImGui::GetDrawData());
}

// ---- NVN proc shims (returned to the game from getProc) ------------------

void setViewportShim(nvn::CommandBuffer* cmdBuf, int x, int y, int w, int h) {
    s_origSetViewport(cmdBuf, x, y, w, h);
    if (s_imguiInited) ImGui::GetIO().DisplaySize = ImVec2(w - x, h - y);
}

void presentTextureShim(nvn::Queue* queue, nvn::Window* window, int texIndex) {
    if (s_imguiInited) procDraw();
    s_origPresentTexture(queue, window, texIndex);
}

NVNboolean deviceInitShim(nvn::Device* device, const nvn::DeviceBuilder* builder) {
    NVNboolean result = s_origDeviceInit(device, builder);
    s_device = device;
    nvn::nvnLoadCPPProcs(s_device, s_origGetProcAddress);
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
    if (std::strcmp(procName, "nvnCommandBufferSetViewport") == 0) {
        s_origSetViewport = reinterpret_cast<nvn::CommandBufferSetViewportFunc>(ptr);
        return reinterpret_cast<nvn::GenericFuncPtrFunc>(&setViewportShim);
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
