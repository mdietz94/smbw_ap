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

option(SMBWAP_DEBUG_SD_LOG
    "Drain the in-memory log ring to sd:/smbwap_boot.log via a worker thread \
~5 s after hkMain. Required for the Phase 1 acceptance test on real hardware \
(Ryujinx already surfaces svcOutputDebugString in its log file)."
    OFF)
if(SMBWAP_DEBUG_SD_LOG)
    add_compile_definitions(SMBWAP_DEBUG_SD_LOG=1)
endif()

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
    ${PROJECT_SOURCE_DIR}/src/ap/ApClient.cpp
    ${PROJECT_SOURCE_DIR}/src/ap/ApDiscovery.cpp
    ${PROJECT_SOURCE_DIR}/src/ap/ApFrameBridge.cpp
    ${PROJECT_SOURCE_DIR}/src/ap/ApProtocol.cpp
    ${PROJECT_SOURCE_DIR}/src/ap/ApState.cpp
    ${PROJECT_SOURCE_DIR}/src/probe/ContainerA.cpp
    ${PROJECT_SOURCE_DIR}/src/probe/ContainerB.cpp
    ${PROJECT_SOURCE_DIR}/src/probe/ContainerC.cpp
    ${PROJECT_SOURCE_DIR}/src/probe/DeathLink.cpp
    ${PROJECT_SOURCE_DIR}/src/probe/Gates.cpp
    ${PROJECT_SOURCE_DIR}/src/probe/PerCourse.cpp
    ${PROJECT_SOURCE_DIR}/src/probe/Stubs.cpp
)
add_executable(${PROJECT_NAME} ${SOURCES_CXX})
target_include_directories(${PROJECT_NAME} PRIVATE ${PROJECT_SOURCE_DIR}/src)

# nn::os / nn::nifm / nn::socket / malloc / free / aligned_alloc are
# undefined at link time -- they're resolved at module-load via nnrtld
# walking the AutoLoadList (the .data anchor at `_ZN2nn2ro6detail15g_pAutoLoadListE`
# which our subsdk exports). With USE_SAIL=FALSE we don't have sail's
# fakesymbols.so to satisfy these at link time, so tell lld to leave them
# as dynamic relocations.
target_link_options(${PROJECT_NAME} PRIVATE
    -Wl,--unresolved-symbols=ignore-all)

include(sys/cmake/module.cmake)
include(sys/cmake/deploy.cmake)
include(sys/cmake/sail.cmake)
usesail(${PROJECT_NAME})
