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
add_compile_definitions(
    BRIDGE_HOST_STRING="${BRIDGE_HOST_STRING}"
    DISCOVERY_PORT_VALUE=17776
)

## M5 real-hardware bisect: each hook group has its own bit so a real-hw
## build can be reduced to a minimal set without commenting code.  See
## main.cpp::exl_main and the SMBWAP_HOOK_BIT_* macros below for the
## per-bit groupings.  Default 0x3F = all six groups enabled (matches
## previous behavior).  For a minimal first-boot probe on real hardware,
## pass `-DSMBWAP_HOOK_MASK=0x01` (CORE_INIT only — installs only the
## boot-time framework hooks needed to drain the in-memory log ring to
## sd:/smbwap_boot.log via CreateFileDeviceMgr::Callback).
if(NOT DEFINED SMBWAP_HOOK_MASK)
    set(SMBWAP_HOOK_MASK "0x3F")
endif()
add_compile_definitions(SMBWAP_HOOK_MASK=${SMBWAP_HOOK_MASK})
message(STATUS "SMBWAP_HOOK_MASK = ${SMBWAP_HOOK_MASK}")

set(NN_SDK_MAJOR 16) # idk
set(NN_SDK_MINOR 0) # idk
set(NN_SDK_PATCH 0) # idk
set(NN_SDK_TYPE "Release")
set(NN_WARE_MAJOR 16) # idk
set(NN_WARE_MINOR 0) # idk
set(NN_WARE_PATCH 0) # idk

configure_file(${CMAKE_CURRENT_SOURCE_DIR}/module/subsdk9.json.template ${CMAKE_CURRENT_BINARY_DIR}/subsdk9.json)

## wondar's include/{sead,nn,nvn,vapours} are POSIX symlinks in git; on Windows
## checkouts they materialize as text files containing the target path, which
## breaks the build. We materialize them at configure time into the build dir
## (NOT the source tree, to keep `git status` clean) and prepend that to the
## include path. Best-effort directory symlink, falling back to recursive copy
## on platforms without symlink privileges (Windows w/o Developer Mode).
set(_SYMLINK_SHIM_DIR "${CMAKE_BINARY_DIR}/symlink-shims")
file(MAKE_DIRECTORY "${_SYMLINK_SHIM_DIR}")
set(_INCLUDE_LINKS
  "sead=${PROJECT_SOURCE_DIR}/lib/sead/include"
  "nn=${PROJECT_SOURCE_DIR}/lib/NintendoSDK/include/nn"
  "nvn=${PROJECT_SOURCE_DIR}/lib/NintendoSDK/include/nvn"
  "vapours=${PROJECT_SOURCE_DIR}/lib/NintendoSDK/include/vapours"
)
foreach(_pair IN LISTS _INCLUDE_LINKS)
  string(REPLACE "=" ";" _pair "${_pair}")
  list(GET _pair 0 _name)
  list(GET _pair 1 _target)
  set(_link "${_SYMLINK_SHIM_DIR}/${_name}")
  if(EXISTS "${_link}")
    file(REMOVE_RECURSE "${_link}")
  endif()
  file(CREATE_LINK "${_target}" "${_link}" SYMBOLIC RESULT _link_result)
  if(NOT _link_result EQUAL 0)
    file(MAKE_DIRECTORY "${_link}")
    file(COPY "${_target}/" DESTINATION "${_link}")
  endif()
endforeach()

## Project sources
set (INCLUDE_DIRS
  ${_SYMLINK_SHIM_DIR}                  # materialized stand-ins for include/{sead,nn,nvn,vapours}
  ${PROJECT_SOURCE_DIR}/src/
  ${PROJECT_SOURCE_DIR}/src/lib
  ${PROJECT_SOURCE_DIR}/src/program     # so smbwap-side code can use "util/Log.hpp" etc.
  ${PROJECT_SOURCE_DIR}/include
  ${PROJECT_SOURCE_DIR}/lib/sead/include
  ${PROJECT_SOURCE_DIR}/lib/NintendoSDK/include
  ${PROJECT_SOURCE_DIR}/lib/NintendoSDK/include/nvn
  ${PROJECT_SOURCE_DIR}/lib/imgui_backend
  ${PROJECT_SOURCE_DIR}/lib/imgui
)
include_directories(${INCLUDE_DIRS})

file(GLOB_RECURSE SOURCES_ASM ${PROJECT_SOURCE_DIR}/src/*.s)
file(GLOB_RECURSE SOURCES_C ${PROJECT_SOURCE_DIR}/src/*.c)
file(GLOB_RECURSE SOURCES_CXX ${PROJECT_SOURCE_DIR}/src/*.cpp)

file(GLOB IMGUI_SOURCES ${PROJECT_SOURCE_DIR}/lib/imgui/*.cpp)

# exlaunch's typed_storage.hpp invokes std::construct_at with a const-T pointer
# in patcher_impl / RwPages init paths; libstdc++ 15.x's construct_at strips
# const through reinterpret to void* and emits a hard error. Every TU transitively
# pulls this in via lib.hpp, so -fpermissive is set globally rather than per-file.
# Mirrors the same fix in smo_archipelago/switch-mod/CMakeLists.txt.
add_compile_options(-fpermissive -Wno-deprecated-declarations)

## Include nx tools
include(${CMAKE_SOURCE_DIR}/cmake/SwitchTools.cmake)

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
