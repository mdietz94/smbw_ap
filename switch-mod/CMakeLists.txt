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

# Phase 1 source list — explicit, NOT GLOB_RECURSE. The legacy exlaunch
# source tree at src/program/, src/lib/, lib/{imgui,sead,NintendoSDK} is
# intentionally orphaned from the build; Phase 2 will pull individual files
# back in as they get ported to hakkun.
set(SOURCES_CXX
    ${PROJECT_SOURCE_DIR}/src/main.cpp
    ${PROJECT_SOURCE_DIR}/src/util/Log.cpp
)
add_executable(${PROJECT_NAME} ${SOURCES_CXX})
target_include_directories(${PROJECT_NAME} PRIVATE ${PROJECT_SOURCE_DIR}/src)

include(sys/cmake/module.cmake)
include(sys/cmake/deploy.cmake)
include(sys/cmake/sail.cmake)
usesail(${PROJECT_NAME})
