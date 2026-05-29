// Observability + new write-path primitives for the Wonder Seed RE re-open
// (2026-05-28).  Tracked work plan in
// [docs/wonder-seed-re-reopen-2026-05-28.md](../../../docs/wonder-seed-re-reopen-2026-05-28.md).
//
// This header surfaces three things to main.cpp / hkMain:
//
//   1. `probe::installSeedTraceHooks()` -- installs three new trampolines:
//      * ContainerBWrapper4Arg @ NSO +0x49EA24 -- the high-level container-B
//        wrapper we currently call from `grantContainerBBool` as a 3-arg
//        function.  An older static-analysis note (docs/static-analysis-
//        findings.md "iteration 7" / wonder-seed-observability-hook-prompt
//        line 4062-4065) claimed this function has a 4-arg OVERLOAD where
//        a 4th `bit_index` register feeds the per-course Wonder Seed
//        bitfield write at hash 0x60458608.  We never tested it.  This
//        trampoline logs all four registers on every call so we can see
//        whether (a) hash 0x60458608 ever flows through this wrapper from
//        the game itself, (b) the 4th register is a real bit_index when it
//        does, and (c) which game function is calling.  Filtered + budgeted
//        to avoid flooding the ring during save deserialization.
//      * PerCourseBitReader @ NSO +0x124134 -- per-course bit reader
//        (FUN_7100124134).  4-arg: (gmd, out*, hash, bit_index).  Filter
//        on hash == 0x60458608 to surface only Wonder Seed bit queries.
//        Tells us when the game popcounts the per-course bitfield (we
//        suspect world-map state recompute on world transitions).
//      * PerCourseBitfieldWriter @ NSO +0x1F2B354 -- per-course u32
//        bitfield writer (FUN_7101F2B354).  4-arg: (gmd, value, hash,
//        course_index).  Already invoked by `probe::setPerCourseBitfield-
//        Absolute` for course-clear / goal-seed bitmasks; trampolining
//        gives free observability of every game-natural call too.
//
//   2. `probe::setPerCourseWonderSeedBit(course_index, value)` -- the
//      Wonder-Seed write primitive.  REWRITTEN 2026-05-29 after the
//      decompile_container_chain.py run FALSIFIED the 4-arg overload
//      claim for FUN_710049EA24.  That function is strictly 3-arg
//      `(gmd, value, hash)` and masks `value` with `& 1` (bool coerce)
//      before delegating to FUN_7101F263FC -- it ignores any w3 register.
//      HOWEVER, the FUN_7100124134 reader decompile showed per-course
//      Wonder Seed bits live in CONTAINER C at hash 0x60458608, sharing
//      the SAME bucket structure as the M3.2 badge bitfield (gmd+0x78 +
//      bucket*0x40 + 0x28, walked as data[bit/32] & (1 << bit%32)).
//      So we route this primitive through the existing proven
//      `probe::setContainerCBit(0x60458608, course_index, value)` --
//      the M3.2 mechanism, just with a different hash.  Smoke test:
//      call once with a known-uncollected bit (e.g., course_index=70),
//      save+quit+reload, save-diff the per-course Wonder Seed flag
//      array (file offset 0x3AF8 per docs/save-diff-findings.md).  If
//      the right bit position flips 0->1, persistence is confirmed.
//
//   3. `probe::dumpGmdSubstructs()` -- one-shot pretty-print of the gmd
//      singleton's substruct base pointers + queue head/cap words.
//      Useful for verifying our documented offsets and for spotting any
//      unmapped containers at gmd+0xXX offsets we haven't yet RE'd.
//      Cheap; safe to call once at the first GmdContainerAWriter fire
//      (which is by induction post-save-deserialization).

#pragma once

#include <cstdint>

namespace probe {

void installSeedTraceHooks();

// Write a single per-course Wonder Seed bit via the container-C
// bucket-walk pattern (hash 0x60458608, same shape as the M3.2 badge
// bitfield).  Idempotent: write the same bit value any number of
// times for the same result.  Returns false if gmd is null OR the
// hash's container-C bucket doesn't exist (e.g., save not loaded).
// Logs via setContainerCBit's built-in log path.
bool setPerCourseWonderSeedBit(std::uint32_t course_index, std::uint32_t value);

// One-shot smoke test: write a known-uncollected bit, log before/after.
// Called from main.cpp's SetCourseClearFlagExecute hook (one-shot
// guarded) -- so the test fires the FIRST time the player clears any
// course after boot.  Player can then save+quit+reload and save-diff
// to confirm the bit persisted to disk.
void triggerWonderSeedSmokeTest();

// One-shot dump of gmd substruct base pointers + queue head/cap words.
// Idempotent at the call site (a static atomic guards against re-firing).
// Safe to call from any hook trampoline after the save has deserialized.
void dumpGmdSubstructsOnce(const char* via);

// One-shot dump of the per-world Wonder Seed PERSISTENCE map (2026-05-29).
//
// Resolves the three remaining unknowns before we can write the persistent
// per-world count store for zero-window AP authority:
//   (1) the container-D array INDEX the world-entry handler reads
//       (FUN_71006b5d6c reads container-D[regular_hash][idx]); we sweep
//       idx 0..15 and log where the data actually lives.
//   (2) the BASE counter 0x21f89ab1 contribution to the count formula
//       count = base + popcount(0x1faf41e5) + popcount(0xb9bd745d).
//   (3) the regular/special split per world.
//
// For each world_val 1..9 it reads container-D at both the regular and
// special seed-bitmask hashes (per-world descriptor table @ NSO
// +0x29f0b34, stride 0x70, +0x10/+0x14) via FUN_71000e258c, plus the live
// current-world working bitfields and the base/animated/result counters
// via FUN_710012ae94 / FUN_7100124134.  Correlate the logged values with
// the known per-world seed counts of the loaded save.
//
// Re-arms (up to a small attempt cap) if the save isn't fully loaded yet
// at the call site, so it reliably fires once container-D is populated.
void dumpSeedPersistenceMapOnce();

// One-shot WRITE TEST for the per-world persistent store (2026-05-29).
// Writes a known pattern into W6's (currently-empty) container-D seed
// bitmasks via the documented per-course writer FUN_7101F2B354, then
// reads it back live and logs.  Purpose: confirm (a) the write lands,
// (b) the count recomputes to the popcount on W6 entry, and (c) whether
// it survives save+reload or gets stripped by the serializer (the badge
// gotcha).  ⚠ MODIFIES THE SAVE when the player next saves -- back up
// game_data.sav first.  Remove before shipping the real bridge path.
void triggerSeedPersistWriteTestOnce();

}  // namespace probe
