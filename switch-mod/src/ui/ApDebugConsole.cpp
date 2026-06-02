// On-Switch ImGui debug overlay — see ApDebugConsole.hpp for the contract.
//
// Init flow (when SMBWAP_HAS_DEBUG_RENDERER is defined; mirrors the SMO port
// of Kgamer77/SMOO-Plus-Hakkun's pattern):
//   1. hkMain calls ImGuiBackendNvn::instance()->installHooks(false) so the
//      Nvn addon's bootstrap trampoline does NOT auto-init ImGui when NVN
//      comes up — we do it ourselves.
//   2. initDebugConsole() (pre-orig from an early-init hook) creates the
//      ImGui ExpHeap, wires the allocator, and calls tryInitialize().
//   3. drawDebugConsole() (per-frame, render chokepoint): NewFrame → render
//      our window → Render → backend draws into the current command buffer.
//
// Without the addon, initDebugConsole() just stamps the boot tick + logs and
// drawDebugConsole() is a no-op, so the live SMBW build is unchanged.

#include "ApDebugConsole.hpp"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstdio>

#include "hk/svc/cpu.h"  // hk::svc::getSystemTick (matches probe/DeathLink.cpp)

#include "../ap/ApState.hpp"
#include "../util/Log.hpp"

#ifdef SMBWAP_HAS_DEBUG_RENDERER
#  include "imgui.h"
#  include "hk/gfx/ImGuiBackendNvn.h"
// TODO(imgui-overlay, RE item #1 — heap source): SMBW is NOT the Odyssey
// `al::` engine, so there is no al::getStationedHeap(). Replace the heap
// acquisition in ensureSetup() with SMBW's sead root/stationed heap
// (syms/100/sead/heap/seadHeapMgr.sym + seadExpHeap.sym are imported — most
// likely sead::HeapMgr::instance()->getRootHeap(0)). See the handoff doc.
#  include <sead/heap/seadExpHeap.h>
// TODO(imgui-overlay, RE item #2 — command buffer): SMBW has no Odyssey
// Application::mDrawSystemInfo->drawContext. Find SMBW's per-frame NVN
// command buffer (and the present/draw chokepoint to call this from).
#  include "EmbeddedFontKarla.hpp"
#endif

namespace smbwap::ui {

namespace {

// 19.2 MHz ARM generic timer (matches probe/Gates.cpp + DeathLink.cpp). SMBW
// dropped ApState::nowMs(), so derive a millisecond clock from the tick.
constexpr std::uint64_t kTicksPerMs = 19'200'000ULL / 1000ULL;  // 19'200

std::int64_t nowMs() {
    return static_cast<std::int64_t>(hk::svc::getSystemTick() / kTicksPerMs);
}

// Boot-time + disconnect grace windows (ms).
constexpr std::int64_t kBootGraceMs       = 5000;
constexpr std::int64_t kDisconnectGraceMs = 5000;

// Boot timestamp, stamped once by initDebugConsole(). 0 until then.
std::atomic<std::int64_t> s_boot_ms{0};

// Self-latched disconnect timestamp. Updated each draw frame from the
// observed ap::connState() so we never have to touch ApClient. 0 means
// "never seen connected yet"; in that case we fall back to since-boot.
std::atomic<std::int64_t> s_disconnect_ms{0};
std::atomic<bool>         s_was_connected{false};

// True once the bridge handshake has fully completed (TCP up + HELLO).
bool connectedNow() {
    return ap::connState().load(std::memory_order_acquire) == ap::ConnState::Ready;
}

// Sample the connection state and maintain s_disconnect_ms. Call once per
// frame from drawDebugConsole(). Returns the current connected-ness.
bool sampleConnection() {
    const bool connected = connectedNow();
    const bool was = s_was_connected.exchange(connected, std::memory_order_acq_rel);
    if (connected != was) {
        if (!connected) {
            s_disconnect_ms.store(nowMs(), std::memory_order_relaxed);
        }
        SMBWAP_LOG_INFO("[overlay] bridge %s -> %s",
                        was ? "up" : "down",
                        connected ? "up" : "down");
    }
    return connected;
}

bool overlayShouldShow(bool connected) {
    const std::int64_t boot_ms = s_boot_ms.load(std::memory_order_relaxed);
    if (boot_ms == 0) return false;
    const std::int64_t now = nowMs();
    const std::int64_t since_boot = now - boot_ms;
    if (since_boot < kBootGraceMs) return false;
    if (connected) return false;
    const std::int64_t disc = s_disconnect_ms.load(std::memory_order_relaxed);
    const std::int64_t since_disconnect = (disc == 0) ? since_boot : (now - disc);
    return since_disconnect > kDisconnectGraceMs;
}

#ifdef SMBWAP_HAS_DEBUG_RENDERER

sead::Heap* s_imgui_heap = nullptr;
bool        s_setup_done = false;
bool        s_setup_failed = false;

const char* connLabel() {
    switch (ap::connState().load(std::memory_order_acquire)) {
        case ap::ConnState::Disconnected: return "DISCONNECTED";
        case ap::ConnState::Connecting:   return "connecting...";
        case ap::ConnState::Hello:        return "handshaking...";
        case ap::ConnState::Ready:        return "OK (bridge up)";
    }
    return "?";
}

// Lazy setup: 2 MiB ExpHeap + allocator wire-up + tryInitialize + Karla atlas
// swap. Called from initDebugConsole() (pre-orig) and retried from the first
// eligible draw if NVN wasn't up yet.
bool ensureSetup() {
    if (s_setup_done) return true;
    if (s_setup_failed) return false;

    // TODO(imgui-overlay, RE item #1): swap this for SMBW's sead heap source.
    // al::getStationedHeap() does not exist in SMBW. Until resolved, this TU
    // is gated out of the build (SMBWAP_HAS_DEBUG_RENDERER undefined).
    sead::Heap* parent = nullptr;  // <-- e.g. sead::HeapMgr::instance()->getRootHeap(0)
    if (!parent) {
        SMBWAP_LOG_ERROR("[overlay] no ImGui heap source wired; overlay disabled");
        s_setup_failed = true;
        return false;
    }
    s_imgui_heap = sead::ExpHeap::create(
        2 * 1024 * 1024, "ApImGuiHeap", parent,
        8, sead::Heap::cHeapDirection_Forward, false);
    if (!s_imgui_heap) {
        SMBWAP_LOG_ERROR("[overlay] sead::ExpHeap::create failed; overlay disabled");
        s_setup_failed = true;
        return false;
    }

    auto* backend = hk::gfx::ImGuiBackendNvn::instance();
    backend->setAllocator({
        [](::size sz, ::size align) -> void* {
            return s_imgui_heap->tryAlloc(sz, align);
        },
        [](void* p) -> void {
            if (s_imgui_heap) s_imgui_heap->free(p);
        },
    });
    if (!backend->tryInitialize()) {
        SMBWAP_LOG_ERROR("[overlay] ImGuiBackendNvn::tryInitialize failed; overlay disabled");
        s_setup_failed = true;
        return false;
    }

    // Swap ProggyClean (13px bitmap — blurry on a TV) for Karla at 22px.
    auto& io = ImGui::GetIO();
    io.Fonts->Clear();
    ImFontConfig cfg;
    cfg.FontDataOwnedByAtlas = false;  // backing array is our static const
    cfg.OversampleH = 2;
    cfg.OversampleV = 1;
    cfg.PixelSnapH  = false;
    ImFont* font = io.Fonts->AddFontFromMemoryTTF(
        const_cast<unsigned char*>(kKarlaRegularTtfData),
        static_cast<int>(kKarlaRegularTtfSize),
        22.0f, &cfg);
    if (!font) {
        SMBWAP_LOG_WARN("[overlay] AddFontFromMemoryTTF null; falling back to default");
        io.Fonts->AddFontDefault();
    }
    backend->initTexture(false);  // re-bake atlas + re-upload to NVN
    s_setup_done = true;
    SMBWAP_LOG_INFO("[overlay] ImGui NVN backend ready (Karla 22px)");
    return true;
}

void renderOverlayWindow() {
    const std::int64_t now = nowMs();
    const std::int64_t since_boot = now - s_boot_ms.load(std::memory_order_relaxed);

    ImGui::SetNextWindowPos(ImVec2(20, 20), ImGuiCond_Always);
    ImGui::SetNextWindowSize(ImVec2(900, 500), ImGuiCond_FirstUseEver);
    constexpr int kFlags = ImGuiWindowFlags_NoMove
                         | ImGuiWindowFlags_NoCollapse
                         | ImGuiWindowFlags_NoFocusOnAppearing
                         | ImGuiWindowFlags_NoNav
                         | ImGuiWindowFlags_NoSavedSettings;
    if (!ImGui::Begin("SMBW Archipelago  -- debug", nullptr, kFlags)) {
        ImGui::End();
        return;
    }

    ImGui::Text("Bridge: %s    uptime %llds",
                connLabel(),
                static_cast<long long>(since_boot / 1000));
    ImGui::TextUnformatted("SMBW Client (PC) unreachable. Check the Archipelago");
    ImGui::TextUnformatted("Launcher -> SMBW Client is running on the same LAN.");
    ImGui::Separator();
    ImGui::Text("Recent log:");

    // 16 KiB scratch at file scope keeps the frame-thread stack small.
    static char s_log_buf[16 * 1024];
    std::size_t log_len = 0;
    util::snapshotRecentLogs(s_log_buf, sizeof(s_log_buf) - 1, &log_len);
    s_log_buf[log_len] = '\0';

    ImGui::BeginChild("log_scroll", ImVec2(0, 0), false,
                      ImGuiWindowFlags_HorizontalScrollbar);
    ImGui::TextUnformatted(s_log_buf, s_log_buf + log_len);
    if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 10.0f) {
        ImGui::SetScrollHereY(1.0f);
    }
    ImGui::EndChild();

    ImGui::End();
}

#endif  // SMBWAP_HAS_DEBUG_RENDERER

}  // namespace

void initDebugConsole() {
    s_boot_ms.store(nowMs(), std::memory_order_release);
#ifdef SMBWAP_HAS_DEBUG_RENDERER
    // Pre-orig setup: build the ImGui backend BEFORE the engine brings up NVN,
    // so the addon's device hook hands off to an already-initialized backend
    // (the load-bearing pre-orig ordering invariant from the SMO port).
    if (!ensureSetup()) {
        SMBWAP_LOG_WARN("[overlay] initDebugConsole: ensureSetup failed");
    }
#else
    SMBWAP_LOG_INFO("[overlay] built without SMBWAP_HAS_DEBUG_RENDERER — overlay disabled");
#endif
}

void drawDebugConsole() {
#ifdef SMBWAP_HAS_DEBUG_RENDERER
    const bool connected = sampleConnection();
    if (!overlayShouldShow(connected)) return;

    if (!ensureSetup()) return;  // retry until NVN is up

    // TODO(imgui-overlay, RE item #2 — command buffer): acquire SMBW's current
    // NVN command buffer here and pass it to backend->draw(). The SMO original
    // used Application::instance()->mDrawSystemInfo->drawContext->...; SMBW's
    // equivalent is the open RE item. Until then the draw call below is a
    // placeholder and this TU stays gated out of the build.
    auto* backend = hk::gfx::ImGuiBackendNvn::instance();
    ImGui::NewFrame();
    renderOverlayWindow();
    ImGui::Render();
    // backend->draw(ImGui::GetDrawData(), /* SMBW NVN command buffer */ nullptr);
    (void)backend;
#else
    // No renderer compiled in: true no-op. The (void) references keep the
    // gating helpers from tripping -Wunused-function while the overlay's NVN
    // backend wiring is still an open RE item (docs/handoff-imgui-overlay.md).
    (void)overlayShouldShow;
    (void)sampleConnection;
#endif
}

}  // namespace smbwap::ui
