// Build identity of this subsdk.
//
// SMBWAP_BUILD_ID is stamped at build time by switch-mod/cmake/BuildId.cmake
// into <build>/generated/smbwap_build_id.h (shape "<source>@<YYYYMMDD>",
// where <source> is the bundle VERSION file, g<git-hash>[-dirty], or
// "unknown").  The generated dir is on the include path via CMakeLists.txt;
// the __has_include guard keeps a non-cmake / IDE parse compiling, in which
// case the id degrades to "unknown".
//
// Consumers: main.cpp (hkMain boot banner), ap/ApClient.cpp (Hello
// mod_ver), ap/ApDiscovery.cpp (discovery probe mod_ver).  The PC client
// parses the shape to warn on a stale subsdk9 -- keep the grammar in sync
// with apworld/smbw_archipelago/_setup/build_id.py.
#pragma once

#if defined(__has_include)
#  if __has_include("smbwap_build_id.h")
#    include "smbwap_build_id.h"
#  endif
#endif

#ifndef SMBWAP_BUILD_ID
#define SMBWAP_BUILD_ID "unknown"
#endif
#ifndef SMBWAP_SOURCE_ID
#define SMBWAP_SOURCE_ID "unknown"
#endif
#ifndef SMBWAP_BUILD_DATE
#define SMBWAP_BUILD_DATE ""
#endif

namespace smbwap::util {

inline constexpr const char* kBuildId = SMBWAP_BUILD_ID;

}  // namespace smbwap::util
