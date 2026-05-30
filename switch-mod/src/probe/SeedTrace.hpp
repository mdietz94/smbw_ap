// Container-D (per-world Wonder Seed) persistence writer.
//
// The Wonder Seed RE re-open (2026-05-28..29) lived here as an observability
// module (trampolines + boot dumps + a write-test).  That scaffolding was
// removed once the data model was confirmed; the production code that
// remains is in probe/SeedTrace.cpp.
//
// The single entry point, probe::pushWonderSeedContainerDCounts(), is
// declared in ap/ApFrameBridge.hpp (alongside the other Wonder Seed grant
// helpers) and called from smbwap::ap::drainInbound().  Nothing includes
// this header anymore; it is retained only as the .cpp's companion.

#pragma once
