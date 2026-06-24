cmake_minimum_required(VERSION 3.16)

set(CMAKE_TOOLCHAIN_FILE "${CMAKE_SOURCE_DIR}/sys/cmake/toolchain.cmake")
project(smbw_archipelago VERSION 0.1 LANGUAGES ASM C CXX)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

include(config/config.cmake)

# Phase 1 of the hakkun migration: the only goal is to prove the hakkun
# subsdk loader boots SMBW (Ryujinx + real Switch). Zero functional hooks
# installed; hkMain just logs START/END so we can observe whether init runs.
# Phase 2 will restore the M1-M4 hook surface (course clears, Wonder Seeds,
# PlayReport capture, badge/seed grants). The exlaunch sources under
# src/program/ are left on disk but excluded from this build.
#
# See docs/hakkun-migration-plan.md.

# Source list — explicit, NOT GLOB_RECURSE. The legacy exlaunch source
# tree at src/program/, src/lib/, lib/{imgui,sead,NintendoSDK} is
# intentionally orphaned from the build; we pull individual files back in
# as they get ported to hakkun. Phase 2f brings the ap/ subsystem (LAN
# bridge + protocol) back online; its files moved from src/program/ap/
# to src/ap/ to fit the new tree.
set(SOURCES_CXX
    ${PROJECT_SOURCE_DIR}/src/main.cpp
    ${PROJECT_SOURCE_DIR}/src/util/Log.cpp
    ${PROJECT_SOURCE_DIR}/src/util/Json.cpp
    ${PROJECT_SOURCE_DIR}/src/ui/ApDebugConsole.cpp
    ${PROJECT_SOURCE_DIR}/src/ui/ImGuiNvnBootstrap.cpp
    ${PROJECT_SOURCE_DIR}/src/ap/ApClient.cpp
    ${PROJECT_SOURCE_DIR}/src/ap/ApDiscovery.cpp
    ${PROJECT_SOURCE_DIR}/src/ap/ApFrameBridge.cpp
    ${PROJECT_SOURCE_DIR}/src/ap/ApProtocol.cpp
    ${PROJECT_SOURCE_DIR}/src/ap/ApState.cpp
    ${PROJECT_SOURCE_DIR}/src/probe/ContainerA.cpp
    ${PROJECT_SOURCE_DIR}/src/probe/ContainerB.cpp
    ${PROJECT_SOURCE_DIR}/src/probe/ContainerC.cpp
    ${PROJECT_SOURCE_DIR}/src/probe/BadgeShop.cpp
    ${PROJECT_SOURCE_DIR}/src/probe/DeathLink.cpp
    ${PROJECT_SOURCE_DIR}/src/probe/Diagnostics.cpp
    ${PROJECT_SOURCE_DIR}/src/probe/Gates.cpp
    ${PROJECT_SOURCE_DIR}/src/probe/ItemGetGate.cpp
    ${PROJECT_SOURCE_DIR}/src/probe/PerCourse.cpp
    ${PROJECT_SOURCE_DIR}/src/probe/SeedTrace.cpp
)
add_executable(${PROJECT_NAME} ${SOURCES_CXX})
target_include_directories(${PROJECT_NAME} PRIVATE ${PROJECT_SOURCE_DIR}/src)

# Bridge-discovery subnet seed. The Switch subsdk has no broadcast probe;
# after a loopback attempt (Ryujinx-on-same-host) it sweeps the /24 that
# this IP falls in, looking for the PC client (src/ap/ApDiscovery.cpp). The
# default seeds a 192.168.1.0/24 sweep; override at configure time when the
# play network is on a different subnet:
#     -DBRIDGE_HOST_STRING="10.0.0.5"
# The value only needs to be SOME address on the right /24 — it is a sweep
# seed, not a connection target. ApDiscovery.cpp also #defines the same
# default as a safety net for non-cmake builds; forwarding it here is what
# actually lets the configure-time override reach the compiler.
set(BRIDGE_HOST_STRING "192.168.1.1" CACHE STRING
    "Subnet seed for the Switch bridge-discovery /24 sweep")
target_compile_definitions(${PROJECT_NAME} PRIVATE
    BRIDGE_HOST_STRING="${BRIDGE_HOST_STRING}")
message(STATUS "smbwap: bridge-discovery sweep seed = ${BRIDGE_HOST_STRING}")

# Interpreter for the build-time Python tools (deploy.py -> import lz4.block,
# elf2nso.py -> import elftools). LibHakkun's deploy.cmake / generate_exefs.cmake
# call bare `python`; patch_hakkun.py rewrites those to `${SMBWAP_PYTHON}` so the
# wizard can point them at the exact interpreter it installed lz4/pyelftools into
# (build.py passes -DSMBWAP_PYTHON=<resolved>). On a box with more than one Python
# bare `python` can resolve to an interpreter missing those --user deps, and the
# build dies with ModuleNotFoundError right after linking. Defaults to `python` so
# a manual cmake build (no -D) keeps the legacy behavior.
set(SMBWAP_PYTHON "python" CACHE STRING
    "Python interpreter for build-time tools (deploy.py, elf2nso.py)")
message(STATUS "smbwap: build-time Python = ${SMBWAP_PYTHON}")

# nn::os / nn::nifm / nn::socket / malloc / free / aligned_alloc are
# undefined at link time -- they're resolved at module-load via nnrtld
# walking the AutoLoadList (the .data anchor at `_ZN2nn2ro6detail15g_pAutoLoadListE`
# which our subsdk exports). With USE_SAIL=FALSE we don't have sail's
# fakesymbols.so to satisfy these at link time, so tell lld to leave them
# as dynamic relocations.
target_link_options(${PROJECT_NAME} PRIVATE
    -Wl,--unresolved-symbols=ignore-all)

# On-Switch ImGui debug overlay (src/ui/ApDebugConsole.cpp +
# src/ui/ImGuiNvnBootstrap.cpp). The overlay renders through LibHakkun's NVN
# backend (hk::gfx::ImGuiBackendNvn, the Nvn + ImGui addons enabled in
# config.cmake) — embedded shaders, self-contained NVN bindings, no SD card.
# Our present hook (ImGuiNvnBootstrap.cpp) owns the single nvnBootstrapLoader
# trampoline and feeds the backend; we do NOT use the addon's auto-hook path.
# See docs/handoff-imgui-overlay.md for the chosen-approach writeup.
#
# The overlay is MANDATORY (maintainer decision 2026-06): SMBWAP_HAS_DEBUG_RENDERER
# is always defined. config.cmake FATAL_ERRORs if lib/imgui is missing, so by the
# time we get here imgui.h exists.
#
# The main TUs that #include "imgui.h" (ApDebugConsole.cpp + ImGuiNvnBootstrap)
# MUST compile imgui with the SAME config the ImGui addon uses, or the ImGuiIO /
# ImDrawData struct layouts diverge across the TU boundary (ABI mismatch ->
# crash). The addon sets IMGUI_DISABLE_DEFAULT_ALLOCATORS +
# IMGUI_USER_CONFIG=<addon>/include/hk/gfx/ImGuiConfig.h; mirror both here. The
# addon include dir (for hk/gfx/ImGuiBackendNvn.h) is added to this target by
# enable_addons() in sys/cmake/module.cmake.
if(NOT EXISTS ${CMAKE_SOURCE_DIR}/lib/imgui/imgui.h)
    message(FATAL_ERROR
        "switch-mod/lib/imgui missing — the debug overlay is mandatory. "
        "Run: git submodule update --init switch-mod/lib/imgui")
endif()
# lib/imgui for "imgui.h"; sys/addons/Nvn/include/hk so the addon's NVN
# headers can resolve their own internal "nvn/..." includes (enable_addons
# only puts each addon's include/ root on this target, not include/hk —
# that's only added to the addon's own build).
target_include_directories(${PROJECT_NAME} PRIVATE
    lib/imgui
    ${CMAKE_SOURCE_DIR}/sys/addons/Nvn/include/hk)
target_compile_definitions(${PROJECT_NAME} PRIVATE
    SMBWAP_HAS_DEBUG_RENDERER=1
    IMGUI_DISABLE_DEFAULT_ALLOCATORS
    IMGUI_USER_CONFIG="${CMAKE_SOURCE_DIR}/sys/addons/ImGui/include/hk/gfx/ImGuiConfig.h")
message(STATUS "smbwap: ImGui debug overlay ENABLED (mandatory; LibHakkun NVN backend)")

include(sys/cmake/module.cmake)
include(sys/cmake/deploy.cmake)
include(sys/cmake/sail.cmake)
usesail(${PROJECT_NAME})
