// M3.3 Phase B verification probe (Phase 2g port).
//
// Read-only diagnostic: dereferences the singleton pointer at
// NSO+`base_nso_offset` (e.g. 0x3632e88 sub-singleton, 0x363f0f0 gmd::
// sInstance), walks to byte offset `field_offset` within the dereferenced
// struct, dumps an assumed SaveDataField header layout:
//     +0x00  uint64_t vtable_ptr
//     +0x08  uint64_t type_desc
//     +0x10  uint32_t count_or_state
//     +0x18  uint32_t* data_ptr
//     +0x20  uint64_t
//     +0x28  uint64_t
// Plus an inline 256-byte hex dump of the field struct and, if data_ptr
// looks like a heap address, the first 81 u32s pointed to (= SMBW course
// count per FUN_71003D4110's Murmur3 hash table).
//
// `base_nso_offset == 0` (captured-save-struct path from the legacy
// SaveDeserializerHook) is NOT ported in Phase 2g -- the SaveDeserializer
// trampoline hasn't migrated yet.  Returns false with a log line if the
// sentinel is passed.  The active code paths (Phase B verification
// dumps with non-zero base) work as before.
//
// Direct port of switch-mod/src/program/main.cpp:1368-1486.

#include <atomic>
#include <cstdint>
#include <cstdio>

#include "probe/Gmd.hpp"
#include "util/Log.hpp"

namespace probe {

bool dumpSaveField(std::uint32_t base_nso_offset, std::uint32_t field_offset) {
    const std::uintptr_t base = mainBase();
    std::uintptr_t sd = 0;

    if (base_nso_offset == 0) {
        SMBWAP_LOG_INFO(
            "dumpSaveField(base=0 [captured], off=0x%x): captured-save-"
            "struct path not yet ported to hakkun (SaveDeserializerHook "
            "TBD; pass a non-zero singleton offset instead)",
            field_offset);
        return false;
    }

    const std::uintptr_t singleton_ptr_addr = base + base_nso_offset;
    sd = *reinterpret_cast<std::uintptr_t*>(singleton_ptr_addr);

    if (sd == 0) {
        SMBWAP_LOG_INFO(
            "dumpSaveField(base=NSO+0x%x, off=0x%x): singleton ptr is "
            "null (save not loaded? wrong base?)",
            base_nso_offset, field_offset);
        return false;
    }

    const std::uintptr_t field_addr = sd + field_offset;
    const std::uint64_t vtable      = *reinterpret_cast<std::uint64_t*>(field_addr + 0x00);
    const std::uint64_t type_desc   = *reinterpret_cast<std::uint64_t*>(field_addr + 0x08);
    const std::uint64_t count_state = *reinterpret_cast<std::uint64_t*>(field_addr + 0x10);
    const std::uint64_t data_ptr    = *reinterpret_cast<std::uint64_t*>(field_addr + 0x18);
    const std::uint64_t f20         = *reinterpret_cast<std::uint64_t*>(field_addr + 0x20);
    const std::uint64_t f28         = *reinterpret_cast<std::uint64_t*>(field_addr + 0x28);

    SMBWAP_LOG_INFO(
        "dumpSaveField(base=NSO+0x%x, off=0x%x): sd=%p field=%p "
        "vtable=0x%016llx type=0x%016llx",
        base_nso_offset, field_offset,
        reinterpret_cast<void*>(sd),
        reinterpret_cast<void*>(field_addr),
        static_cast<unsigned long long>(vtable),
        static_cast<unsigned long long>(type_desc));
    SMBWAP_LOG_INFO(
        "dumpSaveField(off=0x%x): count/state=0x%016llx data_ptr=0x%016llx "
        "f+0x20=0x%016llx f+0x28=0x%016llx",
        field_offset,
        static_cast<unsigned long long>(count_state),
        static_cast<unsigned long long>(data_ptr),
        static_cast<unsigned long long>(f20),
        static_cast<unsigned long long>(f28));

    // Inline dump: 256 bytes from field+0 as 8 u32s per row.  The 48-byte
    // header turned out NOT to be a vtable+u32-array layout (it's a
    // manager with sub-pointers), so the per-course u32 array might live
    // inline past the header -- dump enough bytes to spot the pattern.
    {
        constexpr std::size_t kInlineDumpBytes = 256;
        SMBWAP_LOG_INFO(
            "dumpSaveField(off=0x%x): inline dump of %zu bytes at field=0x%llx",
            field_offset, kInlineDumpBytes,
            static_cast<unsigned long long>(field_addr));
        char buf[256];
        for (std::size_t row = 0; row < kInlineDumpBytes; row += 32) {
            int pos = std::snprintf(buf, sizeof(buf),
                                    "  inline[+0x%03zx]:", row);
            for (std::size_t i = 0; i < 8 && (row + i * 4) < kInlineDumpBytes
                                && pos < static_cast<int>(sizeof(buf)) - 12;
                                ++i) {
                const std::uint32_t v = *reinterpret_cast<std::uint32_t*>(
                    field_addr + row + i * 4);
                pos += std::snprintf(buf + pos, sizeof(buf) - pos,
                                     " %08x", v);
            }
            SMBWAP_LOG_INFO("%s", buf);
        }
    }

    // Sanity guard: data_ptr must look like a Switch heap address.
    if (data_ptr < 0x2000000000ULL || data_ptr >= 0x2200000000ULL) {
        SMBWAP_LOG_INFO(
            "dumpSaveField(off=0x%x): data_ptr=0x%016llx outside expected "
            "heap range [0x2000000000, 0x2200000000); skipping data dump",
            field_offset, static_cast<unsigned long long>(data_ptr));
        return true;
    }

    // 81 u32s = SMBW course count.
    constexpr std::size_t kDumpCount = 81;
    SMBWAP_LOG_INFO(
        "dumpSaveField(off=0x%x): dumping %zu u32s at data_ptr=0x%016llx",
        field_offset, kDumpCount,
        static_cast<unsigned long long>(data_ptr));

    char buf[256];
    for (std::size_t row = 0; row < kDumpCount; row += 8) {
        std::size_t end = row + 8;
        if (end > kDumpCount) end = kDumpCount;
        int pos = std::snprintf(buf, sizeof(buf), "  data[%2zu..%2zu]:",
                                row, end - 1);
        for (std::size_t i = row; i < end
                            && pos < static_cast<int>(sizeof(buf)) - 12;
                            ++i) {
            const std::uint32_t v = *reinterpret_cast<std::uint32_t*>(
                data_ptr + i * 4);
            pos += std::snprintf(buf + pos, sizeof(buf) - pos, " %08x", v);
        }
        SMBWAP_LOG_INFO("%s", buf);
    }
    return true;
}

}  // namespace probe
