cmake_minimum_required(VERSION 3.21)
project(subsdk)

## Error if not using switch toolchain file
if (NOT SWITCH)
  message(FATAL_ERROR "Not targeting switch, make sure to specify -DCMAKE_TOOLCHAIN_FILE=cmake/toolchain.cmake")
endif()

set(CMAKE_C_STANDARD 17)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
enable_language(ASM C CXX)

## Title Id
set(CONFIG_TITLE_ID 0x010015100B514000)
add_compile_definitions(EXL_PROGRAM_ID=${CONFIG_TITLE_ID} EXL_LOAD_KIND=Module EXL_LOAD_KIND_ENUM=2 NNSDK IMGUI_USER_CONFIG="imgui_backend/nvn_imgui_config.h" private=public protected=public)

## M4: LAN bridge discovery configuration.  BRIDGE_HOST_STRING seeds the
## /24 sweep performed by ap/ApDiscovery; default is a typical home LAN
## subnet -- override at configure time for other networks:
##   cmake ... -DBRIDGE_HOST_STRING="192.168.7.42"
if(NOT DEFINED BRIDGE_HOST_STRING)
    set(BRIDGE_HOST_STRING "192.168.1.1")
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

## subsdk9
set(CMAKE_EXECUTABLE_SUFFIX ".nss")
add_executable(subsdk9 ${SOURCES_ASM} ${SOURCES_C} ${SOURCES_CXX} ${IMGUI_SOURCES})


## Prepare subsdk ld
set(SUBSDK_LD_PATH ${CMAKE_CURRENT_SOURCE_DIR}/module/subsdk9.ld)
set(GAME_VERSION 100)
set(SYMS_PATH "${CMAKE_CURRENT_SOURCE_DIR}/syms/${GAME_VERSION}")

execute_process(
    COMMAND bash -c "find ${SYMS_PATH} -type f -name '*.sym' -exec cat {} + > ${CMAKE_BINARY_DIR}/syms.ld"
)

execute_process(COMMAND uname OUTPUT_VARIABLE uname)
if (uname MATCHES "^MINGW")
  string(REGEX REPLACE "^/([a-zA-Z])/" "\\1:/" SUBSDK_LD_PATH "${SUBSDK_LD_PATH}")
endif ()
configure_file(${CMAKE_CURRENT_SOURCE_DIR}/module/subsdk9.specs.template ${CMAKE_CURRENT_BINARY_DIR}/subsdk9.specs)

## Build nso & generate npdm
add_nso_target_subsdk(subsdk9)
set_target_properties(subsdk9 PROPERTIES CONFIG_JSON "${CMAKE_CURRENT_BINARY_DIR}/subsdk9.json")
__generate_npdm(subsdk9)
set_target_properties(subsdk9 PROPERTIES LINK_FLAGS "-specs ${CMAKE_BINARY_DIR}/subsdk9.specs -g ${ARCH} -nostartfiles -nostdlib")
