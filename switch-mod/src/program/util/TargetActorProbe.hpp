// M1.3 probe: when an actor of a known-interesting class is constructed,
// emit a SMBWAP_LOG_INFO line carrying its vtable address (NSO-relative)
// and a sampling of virtual-slot offsets. That replaces the "open Ghidra,
// find the string xref, walk to the vtable" path from the plan — we read
// the live vtable straight off the constructed object.
//
// Target list lives here (header-only) so ActorBrowser.cpp can drop a single
// call into its existing ActorBase::ctor hook without dragging additional
// engine includes.

#pragma once

namespace engine::actor { class IActor; }

namespace smbwap::probe {

void onActorConstructed(engine::actor::IActor* actor, void* x1);

}  // namespace smbwap::probe
