# Stamp the subsdk build identity into a generated header.
#
# Run in script mode (`cmake -P`) from the `smbwap_build_id` custom target
# in switch-mod/CMakeLists.txt, so it executes on EVERY `cmake --build`,
# not just at configure time.  That matters because the setup wizard
# skips the configure step when build/ is already configured -- a
# configure-time stamp would go stale across rebuilds and defeat the
# purpose (telling which subsdk9 a player's log came from).
#
# Inputs (passed as -D):
#   SOURCE_DIR  switch-mod/ source root
#   OUT         path of the header to (re)generate
#
# Output header defines:
#   SMBWAP_SOURCE_ID  what the sources were:
#                       - <VERSION file contents>   when SOURCE_DIR/VERSION
#                         exists.  scripts/install_apworld.py --bundle-mod
#                         writes that file into the apworld bundle
#                         ("<world_version>+g<hash>[-dirty]"); the wizard
#                         builds from the extracted bundle, which has no
#                         .git, so this is the only identity available there.
#                       - g<git short hash>[-dirty]  in a git checkout
#                         (dirty = uncommitted changes under switch-mod/).
#                       - unknown                     otherwise.
#   SMBWAP_BUILD_DATE local build date, YYYYMMDD.
#   SMBWAP_BUILD_ID   "<SOURCE_ID>@<BUILD_DATE>" -- the string hkMain logs
#                     in its boot banner and ApClient sends as Hello
#                     `mod_ver`.  The PC client parses this shape
#                     (apworld/smbw_archipelago/_setup/build_id.py) to warn
#                     when the mod was built from different sources than the
#                     apworld it is talking to; keep the grammar in sync.
#
# The header is rewritten only when its content changes, so an unchanged
# identity does not dirty the TUs that include it (no needless recompiles
# of main.cpp / ApClient.cpp / ApDiscovery.cpp on every build).

if(NOT DEFINED SOURCE_DIR OR NOT DEFINED OUT)
    message(FATAL_ERROR "BuildId.cmake: SOURCE_DIR and OUT are required")
endif()

set(_source "")

# 1. Bundled sources: VERSION file written by install_apworld.py.
set(_version_file "${SOURCE_DIR}/VERSION")
if(EXISTS "${_version_file}")
    file(STRINGS "${_version_file}" _version_lines LIMIT_COUNT 1)
    if(_version_lines)
        list(GET _version_lines 0 _source)
        string(STRIP "${_source}" _source)
    endif()
endif()

# 2. Git checkout: short hash + dirty flag scoped to switch-mod/.
if(_source STREQUAL "")
    find_package(Git QUIET)
    if(GIT_FOUND)
        execute_process(
            COMMAND "${GIT_EXECUTABLE}" -C "${SOURCE_DIR}" rev-parse --short=7 HEAD
            OUTPUT_VARIABLE _hash
            OUTPUT_STRIP_TRAILING_WHITESPACE
            RESULT_VARIABLE _hash_rc
            ERROR_QUIET)
        if(_hash_rc EQUAL 0 AND NOT _hash STREQUAL "")
            set(_source "g${_hash}")
            execute_process(
                COMMAND "${GIT_EXECUTABLE}" -C "${SOURCE_DIR}" status --porcelain --untracked-files=no -- .
                OUTPUT_VARIABLE _status
                RESULT_VARIABLE _status_rc
                ERROR_QUIET)
            string(STRIP "${_status}" _status)
            if(_status_rc EQUAL 0 AND NOT _status STREQUAL "")
                string(APPEND _source "-dirty")
            endif()
        endif()
    endif()
endif()

# 3. Fallback.
if(_source STREQUAL "")
    set(_source "unknown")
endif()

# The value lands inside a C string literal and a JSON string on the wire:
# keep it to a conservative character set and length.  (kModVerCap in
# src/ap/ApProtocol.hpp bounds the Hello field; 40 + 1 + 8 stays well
# under it.)
string(REGEX REPLACE "[^A-Za-z0-9._+-]" "" _source "${_source}")
string(LENGTH "${_source}" _source_len)
if(_source_len GREATER 40)
    string(SUBSTRING "${_source}" 0 40 _source)
endif()
if(_source STREQUAL "")
    set(_source "unknown")
endif()

string(TIMESTAMP _date "%Y%m%d")
set(_build_id "${_source}@${_date}")

set(_content "// Generated at build time by switch-mod/cmake/BuildId.cmake -- do not edit.
#pragma once
#define SMBWAP_SOURCE_ID \"${_source}\"
#define SMBWAP_BUILD_DATE \"${_date}\"
#define SMBWAP_BUILD_ID \"${_build_id}\"
")

set(_old "")
if(EXISTS "${OUT}")
    file(READ "${OUT}" _old)
endif()
if(NOT _old STREQUAL _content)
    get_filename_component(_out_dir "${OUT}" DIRECTORY)
    file(MAKE_DIRECTORY "${_out_dir}")
    file(WRITE "${OUT}" "${_content}")
    message(STATUS "smbwap: build id = ${_build_id} (header updated)")
else()
    message(STATUS "smbwap: build id = ${_build_id}")
endif()
