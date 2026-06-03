set(LINKFLAGS -nodefaultlibs)
set(LLDFLAGS --no-demangle --gc-sections)

set(OPTIMIZE_OPTIONS_DEBUG -O2 -gdwarf-4)
# Conservative codegen — matching smo_archipelago's rationale: -O3 + LLVM 19
# can emit aggressive instruction sequences that Ryujinx's ARMeilleure JIT may
# mistranslate. Stay at -O2.
set(OPTIMIZE_OPTIONS_RELEASE -O2 -fno-strict-aliasing)
set(WARN_OPTIONS -Werror=return-type -Wno-invalid-offsetof)

set(INCLUDES include)

set(ASM_OPTIONS "")
set(C_OPTIONS -ffunction-sections -fdata-sections)
set(CXX_OPTIONS "")
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED TRUE)

set(IS_32_BIT FALSE)
set(TARGET_IS_STATIC FALSE)
set(MODULE_NAME smbw_archipelago)
# Super Mario Bros. Wonder v1.0.0 (BID CD6E42AEE7934F4D, codename Secred.nss).
set(TITLE_ID 0x010015100B514000)
# subsdk9 is the Atmosphère exefs slot SMBW Archipelago lands in.
set(MODULE_BINARY subsdk9)
set(SDK_PAST_1900 FALSE)
# Phase 1: USE_SAIL FALSE so we don't need to populate config/VersionList.sym
# with SMBW NSO hashes yet. hkMain references no main.nso symbols. Sail will
# be re-enabled in Phase 2 when we port the hooks.
set(USE_SAIL FALSE)

set(TRAMPOLINE_POOL_SIZE 0x40)
set(BAKE_SYMBOLS FALSE)

# Debug overlay (docs/handoff-imgui-overlay.md): the on-Switch ImGui overlay
# renders through LibHakkun's NVN backend (hk::gfx::ImGuiBackendNvn), which
# ships embedded shaders + self-contained NVN bindings — no SD card, no glslc,
# no exlaunch backend port. The ImGui addon depends on Nvn.
#
# Gate the addons on lib/imgui being checked out: the ImGui addon's CMakeLists
# FATAL_ERRORs without it, and CMakeLists.txt likewise gates
# SMBWAP_HAS_DEBUG_RENDERER on imgui.h existing. So with the submodule absent
# (the default for CI / fresh clones) NO addon is enabled and the live build is
# byte-for-byte unchanged; checking out switch-mod/lib/imgui opts the overlay in.
#
# USE_SAIL stays FALSE: the PlayReport hooks in src/main.cpp already prove
# installAtSym<mangled>() resolves SDK symbols at runtime via hk::ro::lookupSymbol
# under HK_DISABLE_SAIL, so the nvnBootstrapLoader @sdk symbol resolves the same
# way — no VersionList.sym population needed.
if(EXISTS ${CMAKE_CURRENT_LIST_DIR}/../lib/imgui/imgui.h)
    set(HAKKUN_ADDONS Nvn ImGui)
else()
    set(HAKKUN_ADDONS )
endif()
