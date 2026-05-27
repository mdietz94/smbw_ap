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

# Phase 1: no addons. HeapSourceDynamic is needed only once we allocate via
# new/malloc through the game's heap (none of Phase 1's code does). Nvn /
# ImGui / DebugRenderer come back when we restore the debug overlay.
set(HAKKUN_ADDONS )
