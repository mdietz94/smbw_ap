// On-Switch ImGui debug overlay — see ApDebugConsole.hpp for the contract.
//
// Option B (wondar/exlaunch lineage, ported to hakkun): the per-frame ImGui
// frame lifecycle (NewFrame / Render / renderDrawData) is owned by the NVN
// present hook in ImGuiNvnBootstrap.cpp. This file owns only (a) the
// visibility gate and (b) the overlay's widgets. drawDebugConsole() is called
// by the present shim between ImGui::NewFrame() and ImGui::Render(), so it
// must NOT call NewFrame/Render/draw itself — it just emits the window.
//
// Without the renderer (SMBWAP_HAS_DEBUG_RENDERER undefined) the whole thing
// compiles to: initDebugConsole() stamps the boot tick + logs, and
// drawDebugConsole() is a no-op. The live SMBW build is unchanged.

#include "ApDebugConsole.hpp"

#include <atomic>
#include <cstddef>
#include <cstdint>

#include "hk/svc/cpu.h"  // hk::svc::getSystemTick (matches probe/DeathLink.cpp)

#include "../ap/ApState.hpp"
#include "../util/Log.hpp"

#ifdef SMBWAP_HAS_DEBUG_RENDERER
#  include "imgui.h"
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

// Self-latched disconnect timestamp, maintained from the observed
// ap::connState() so we never have to touch ApClient. 0 means "never seen
// connected yet"; in that case we fall back to since-boot.
std::atomic<std::int64_t> s_disconnect_ms{0};
std::atomic<bool>         s_was_connected{false};

// Transient overlay notice (level-entry death-gate countdown). Written by
// the network rx thread via setOverlayNotice(); read by the render thread.
// The expiry timestamp is the authoritative "active" signal (publish via
// release / observe via acquire). The text buffer itself races benignly --
// a torn read is at worst one garbled frame of a debug overlay, and the
// writer always null-terminates it -- so it needs no lock.
constexpr std::size_t kNoticeCap = 192;
char              s_notice_text[kNoticeCap] = {};
std::atomic<std::int64_t> s_notice_expiry_ms{0};

bool noticeActive(std::int64_t now) {
    const std::int64_t exp = s_notice_expiry_ms.load(std::memory_order_acquire);
    return exp != 0 && now < exp;
}

// True once the bridge handshake has fully completed (TCP up + HELLO).
bool connectedNow() {
    return ap::connState().load(std::memory_order_acquire) == ap::ConnState::Ready;
}

// Sample the connection state and maintain s_disconnect_ms. Returns the
// current connected-ness.
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
    // A death-gate countdown notice forces the overlay up even while the
    // bridge is connected, so the player sees why they're being bounced.
    if (noticeActive(now)) return true;
    if (connected) return false;
    const std::int64_t disc = s_disconnect_ms.load(std::memory_order_relaxed);
    const std::int64_t since_disconnect = (disc == 0) ? since_boot : (now - disc);
    return since_disconnect > kDisconnectGraceMs;
}

#ifdef SMBWAP_HAS_DEBUG_RENDERER

const char* connLabel() {
    switch (ap::connState().load(std::memory_order_acquire)) {
        case ap::ConnState::Disconnected: return "DISCONNECTED";
        case ap::ConnState::Connecting:   return "connecting...";
        case ap::ConnState::Hello:        return "handshaking...";
        case ap::ConnState::Ready:        return "OK (bridge up)";
    }
    return "?";
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

    // Death-gate countdown notice. Forces the overlay visible (even while
    // connected) and is rendered first, in a warning color, so it's the
    // first thing the player sees. Single benign-racy copy of the rx-thread
    // buffer into a local; the writer keeps s_notice_text null-terminated.
    if (noticeActive(now)) {
        char notice[kNoticeCap];
        std::size_t i = 0;
        for (; i + 1 < sizeof(notice) && s_notice_text[i] != '\0'; ++i) {
            notice[i] = s_notice_text[i];
        }
        notice[i] = '\0';
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(1.0f, 0.45f, 0.30f, 1.0f));
        ImGui::TextUnformatted(notice);
        ImGui::PopStyleColor();
        ImGui::Separator();
    }

    const bool connected = connectedNow();
    ImGui::Text("Bridge: %s    uptime %llds",
                connLabel(),
                static_cast<long long>(since_boot / 1000));
    if (!connected) {
        ImGui::TextUnformatted("SMBW Client (PC) unreachable. Check the Archipelago");
        ImGui::TextUnformatted("Launcher -> SMBW Client is running on the same LAN.");
    }
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
    // Option B: ImGui context + NVN backend are created lazily by the present
    // hook (ImGuiNvnBootstrap.cpp) once NVN is up — not here. All we do is
    // stamp the boot tick that the visibility gate keys off of.
    s_boot_ms.store(nowMs(), std::memory_order_release);
#ifndef SMBWAP_HAS_DEBUG_RENDERER
    SMBWAP_LOG_INFO("[overlay] built without SMBWAP_HAS_DEBUG_RENDERER — overlay disabled");
#endif
}

void setOverlayNotice(const char* text, int ttl_ms) {
    if (text == nullptr || text[0] == '\0' || ttl_ms <= 0) {
        // Clear: a reader observing expiry == 0 treats the notice as gone.
        s_notice_expiry_ms.store(0, std::memory_order_release);
        return;
    }
    // Write the text first, then publish the expiry (release) so a reader
    // that observes an active expiry (acquire) sees the matching text.
    std::size_t i = 0;
    for (; i + 1 < kNoticeCap && text[i] != '\0'; ++i) s_notice_text[i] = text[i];
    s_notice_text[i] = '\0';
    s_notice_expiry_ms.store(nowMs() + static_cast<std::int64_t>(ttl_ms),
                             std::memory_order_release);
}

void drawDebugConsole() {
#ifdef SMBWAP_HAS_DEBUG_RENDERER
    // Called by the NVN present shim between ImGui::NewFrame() and
    // ImGui::Render() — emit widgets only, no frame-lifecycle calls.
    const bool connected = sampleConnection();
    if (!overlayShouldShow(connected)) return;
    renderOverlayWindow();
#else
    // No renderer compiled in: true no-op. The (void) references keep the
    // gating helpers from tripping -Wunused-function while the NVN backend
    // port is still outstanding (docs/handoff-imgui-overlay.md).
    (void)overlayShouldShow;
    (void)sampleConnection;
#endif
}

}  // namespace smbwap::ui
