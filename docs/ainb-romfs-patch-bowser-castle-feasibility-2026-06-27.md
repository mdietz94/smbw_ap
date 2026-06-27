# Feasibility: AINB RomFS patch as an alternative for entering Bowser's Castle — 2026-06-27

**Question.** Instead of granting GameData flags from the subsdk, could we patch the
**AINB logic graph** in the RomFS so the Bowser's-Castle entry point appears? More
generally — is AINB graph patching (trigger cutscenes / detect block hits) a useful
alternative to the hook-and-grant approach?

**Verdict for the castle entry: no — strictly worse than the deployed subsdk path.**
AINB patching has a real niche elsewhere, but the castle entrance is the *least*
suitable possible candidate for it. The short reasons: a RomFS patch can't be gated
on AP progression state, there's no race/cutscene to avoid here, the toolchain has no
AINB *writer*, and it wouldn't touch the genuinely hard multi-flag parts.

Note: the actual `.ainb`/RomFS isn't reachable from this environment (it lives on the
maintainer's Windows box, `smbw_re_tmp\romfs\`). This analysis rests on the
fully-mapped mechanism from `docs/grand-propeller-flower-reveal-re-2026-06-08.md`, not
a fresh byte inspection — and the conclusion does not depend on re-reading the bytes,
because the graph's role is already known exactly.

## The mechanism we'd be patching

The Castle entry point is the actor `WObjCommonMiniKoopaTeleportFlowerA` on the Petal
Isles map (`World002.bcett.byml`). It is **Create-linked** (via a `LogicalSignalORTag`)
from the controller actor `WorldMapObjKoopaCastleEntranceGround`, whose **AINB graph**
reads one saved bool through an `ActorPropertyBinder` / `GetEnumFromGameData` node:

- `WorldMapKoopaCastleEntranceDemoInfo.IsAppear` = `0xc06bd61e` (Bool[245], save=0).

So the AINB graph's whole job here is: *read `IsAppear` → if set, spawn the entrance.*
That node re-evaluates from saved GameData on **every world-map load**.

The deployed subsdk approach (`probe::applyOpenWorldEntry`, `SeedTrace.cpp`) simply
sets that bool — `grantContainerBBool(0xc06bd61e, 1)` — alongside the six
`WorldMapCloudPackun*.IsVanish*` bools and the six Royal-Seed bools, **latched and
gated on `kCastleMaskBit` (bit 8)**, which the client only sets once the AP
`palaces_required` threshold is met.

An AINB patch alternative would edit `WorldMapObjKoopaCastleEntranceGround.root.ainb`
so the `IsAppear` check is bypassed (e.g. feed the conditional a constant `true`),
making the entrance spawn without the flag.

## Why that's worse than the subsdk grant

1. **A RomFS patch can't be conditional on AP state — this is the killer.** The
   castle entrance MUST appear *only* when AP says the palace threshold is met
   (`context._bowser_opened` latch → `kCastleMaskBit`). A baked-in graph edit is
   either always-on or always-off. Patch it to ignore `IsAppear` and the player walks
   into Bowser from the first minute of every seed, regardless of palace count —
   defeating the goal logic. The subsdk wins *precisely because* it flips the bool at
   the exact moment AP authorizes it.

2. **There is no race or cutscene to avoid here.** The general argument for AINB
   patching ("cleaner than racing a transient runtime hook") applies to demos/cutscenes
   that fire on a timing window. The castle entrance is not transient — it's a
   state-driven node rebuilt from a saved Bool on every map load. The subsdk sets the
   Bool; the game's own (unmodified) AINB then draws the entrance correctly on the next
   reload. No timing, no race, no cutscene trigger. The one scenario where AINB
   patching would be cleaner simply doesn't exist for this actor.

3. **The entrance node is not the hard part.** "Getting into Bowser" is the cloud
   piranhas + the route roads + the Royal Seeds + this entrance bool — a constellation
   of saved flags, several of them live-state-driven route gates
   (`ProcessWorldMapRouteGate`). AINB patching would address only the single entrance
   actor and would have to be replicated across many graphs, each re-introducing the
   unconditional-spawn problem from (1).

4. **No AINB writer in the toolchain; high risk, zero benefit.** `scripts/romfs/`
   *reads* AINB (`sarc_extract.py` unpacks the SARC; we parse `.ainb` to find bindings)
   but there is **no AINB re-serializer/repacker**. Replacing one already-deployed
   `grantContainerBBool` call would require: add round-trip AINB serialization for
   SMBW's AINB variant (unproven — a malformed graph is a silent crash, not a clean
   error), repack the SARC, recompress zstd, and ship a LayeredFS RomFS mod as a new
   build artifact pinned to v1.0.0. Deeply negative cost/benefit.

## Where AINB patching *would* be the right tool (and why the castle isn't it)

AINB patching is the RomFS-side answer only when a gate is **hardcoded in graph logic
with no saved GameData flag to flip** — there's nothing for the subsdk to write, so the
graph itself is the only lever. The castle entrance is the exact opposite: it exposes a
clean saved Bool (`IsAppear`), which is *why* the subsdk approach is a one-liner. This
makes it the worst possible test case for AINB patching.

The general idea still has a niche worth keeping in mind:
- **M5 (in-game grant suppression / forcing).** Statically neutering an in-game
  writer/gate can be cleaner than a runtime hook that has to win a race — *if* the
  thing has no saved flag we already control.
- **A cutscene with no GameData gate.** If a story demo were fired purely by
  graph-internal logic (no `*DemoInfo.IsAppear`-style bool), editing the graph would be
  the only RomFS-side trigger. (The W3-reveal work shows most demos *are* flag/route
  gated, so even there the flag route usually wins.)

Block-hit *detection* remains off the table regardless: it's compiled actor/Nerve
logic, and even if observable in a graph it couldn't cross the LAN bridge to the AP
client without a subsdk hook anyway.

## Bottom line

For Bowser's Castle entry, keep the deployed subsdk grant. It's a single conditional
Bool write, state-driven, idempotent, race-free, and — critically — gateable on AP
palace progression, which a static RomFS/AINB patch can never be. Reach for AINB
patching only for an in-game gate that has **no** saved flag to flip; that is not the
castle entrance.

See also: `docs/grand-propeller-flower-reveal-re-2026-06-08.md` (the full castle /
piranha / route RE), the **smbw-romfs-datamining** skill (AINB read workflow), and
`switch-mod/src/probe/SeedTrace.cpp` (`applyOpenWorldEntry`, the live grant).
