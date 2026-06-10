# SMBW Archipelago — orientation for Claude Code

Project-overview doc. **For current state, always read [docs/handoff.md](docs/handoff.md)
first and [docs/milestones.md](docs/milestones.md) (the M1–M7 roadmap) second.**
Procedural and reference detail lives in **skills** (see the index below) — reach
for the matching skill rather than expanding this file.

> **Subsdk framework: hakkun (LLVM 19 + LibHakkun).** The earlier exlaunch path
> stopped booting SMBW on Atmosphere 1.11.1 + HATS-2026-05-11 (memory
> `[[smbwap-exlaunch-real-hw-broken]]`). The live build is driven by
> `switch-mod/CMakeLists.txt` + `sys/` (LibHakkun submodule) + `config/` +
> `src/{main.cpp, ap/, probe/, util/}`, and boots on real hardware. The retired
> exlaunch tree at `switch-mod/src/{program,lib}/` + `switch-mod/module/` is
> **excluded from the build** but kept on disk for reference. Hook installs are
> hakkun `HkTrampoline` + `installAtMainOffset` / `installAtSym<>` in
> `src/main.cpp` (NOT the exlaunch `Hook::InstallAtOffset` API some older docs
> still describe — see the **smbw-reverse-engineering** skill for the live idiom).

## What this project is

An Archipelago multiworld integration for **Super Mario Bros. Wonder** (SMBW
v1.0.0) on modded Switch + Ryujinx. A Switch subsdk hooks the game NSO and ships
events over LAN (`:17777`) to a PC-side Kivy Archipelago client, which bridges to
the AP server. Architecture mirrors the user's `smo_archipelago` (Super Mario
Odyssey) project at `C:\Users\maxwe\Documents\smo_archipelago\` — mirror its
`switch-mod/src/ap/ApClient.cpp` and `apworld/` layout when extending the bridge.

- **Dev target**: Ryujinx 1.3.3. **Production target**: real Switch + Atmosphere (M6).
- **Launches via**: Archipelago Launcher → "SMBW Client" button (Kivy GUI,
  subclasses `kvui.GameManager`). The old `python -m bridge` CLI is gone.

## Skills — reach for these first

| Skill | Use it for |
|---|---|
| **smbw-build-deploy** | Compile `subsdk9`, deploy to Ryujinx, run the client, tail the `[smbwap]` log, first-time `/setup`, end-to-end smoke tests (Win + Linux). |
| **smbw-logic** | Edit/reason about the apworld **logic** — `data/{items,locations,regions}.json`, `Rules.py`/`Regions.py`/`Options.py`/`DataValidation.py`. The seed-toll region model, the **badge progression-wall softlock rule**, adding region/location gates, generation + beatability testing. Bundles the logic-reconciliation record. |
| **smbw-reverse-engineering** | Find/install a hook (Ghidra), identify Nerves & game functions, the two hook patterns, NSO address space, the crash gotchas, Ghidra scripts. Bundles the **current-state RE map** (`reference/smbw-re-map.md` — the canonical offset/hash/struct/hook ledger; read it first) + the master RE decompile journal. |
| **smbw-romfs-datamining** | Resolve any named GameData flag → hash + container **category** **offline** from the RomFS (`GameDataList.Product.100`), no Ghidra. The murmur3 name-hash rule, BYML/SARC parsing, tracing world-map actors (cloud piranhas, castle fly-in) to their saved bool. Tooling at `scripts/romfs/`. |
| **smbw-save-data** | Grant AP items via the GameDataMgr API, pick the right `probe::` primitive, hash keys, container A/B/C/D layout, save-file format, save-survival/replay, the **Bool-vs-Int writer footgun**. |
| **smbw-release** | Tag a version, build the `smbwonder.apworld` bundle, the pre-push release-gate audit, manual `gh release` fallback. |

Skills are surfaced automatically — prefer invoking the relevant one over
searching `docs/`. Deep reference travels with its skill under
`.claude/skills/<skill>/reference/`.

## Repo layout

```
smbw_ap/                              ← this git repo
├── CLAUDE.md                          you are here
├── conftest.py                        puts vendor/Archipelago on sys.path for pytest
├── .claude/skills/                    ★ build-deploy / reverse-engineering / save-data / release
├── docs/                              living state + active-spike handoffs (see below)
├── apworld/smbw_archipelago/          the SMBWonder AP world + Kivy client
│   ├── __init__.py                    SMBWonderWorld + add_client_to_launcher()
│   ├── {Game,Items,Locations,Regions,Rules,Options,...}.py
│   ├── data/                          items.json / locations.json / regions.json
│   ├── _setup/                        the /setup wizard (probe → install → build → deploy)
│   └── client/                        ★ Kivy client + LAN bridge
│       ├── main.py · context.py (SMBWContext) · gui.py (SMBWManager)
│       ├── lan_server.py (:17777) · discovery.py (:17776)
│       ├── processor.py · play_report.py · state.py · protocol.py · wire.py
│       ├── badge_table.py · royal_seed_table.py · wonder_seed_table.py · coin_table.py
│       └── tests/                     pytest: client + wire + PlayReport decode
├── scripts/                           savediff.py, install_apworld.py, ghidra/ (NSO RE scripts), romfs/ (offline GameData datamining)
├── vendor/Archipelago/               git submodule (pinned)
└── switch-mod/                        subsdk source (tracked inline in this repo)
    ├── CMakeLists.txt                 hakkun-driven; sets its own toolchain before project()
    ├── config/config.cmake           module name, title ID, USE_SAIL, addons
    ├── sys/                           ★ submodule fruityloops1/LibHakkun (framework)
    ├── syms/100/                      Nintendo SDK + gmd/sead/main symbol maps (Ghidra import)
    ├── lib/imgui                      submodule (debug overlay, gated off)
    └── src/
        ├── main.cpp                   ★ hkMain entry + all hook installs
        ├── ap/                        LAN bridge + wire protocol (ApClient, ApFrameBridge, …)
        ├── probe/                     ★ gmd::GameDataMgr grant primitives (ContainerA/B/C,
        │                                PerCourse, SeedTrace, DeathLink, Gates, Diagnostics)
        ├── util/                      Log, Json
        └── {lib,program}/             retired exlaunch sources (excluded from build)
```

`switch-mod/` was a separate repo (fork of `mdietz94/wondar`); it's now tracked
inline so the subsdk ships with the apworld in one release. Only `sys/` (LibHakkun)
and `lib/imgui` remain submodules under it.

## docs/ — what lives there now

`docs/` holds **living state** and **active-spike handoffs** only; durable
reference moved into the skills (a redirect stub marks each moved file).

- `handoff.md` (READ FIRST) · `milestones.md` (M1–M7 roadmap) — canonical state.
- **Active spikes** (in-progress RE — leave these as the session entry points):
  Wonder-Seed persistence (`handoff-2026-05-29-ws-persistence.md`,
  `wonder-seed-re-reopen-2026-05-28.md`, `wonder-seed-observability-hook-prompt.md`);
  Royal-Seed gate-entry / check-loss (`gate-entry-session3-handoff.md`,
  `royal-seed-gate-entry-design.md`, `royal-seed-check-loss-re-findings.md`,
  `royal-seed-phase-a-findings.md`).
- `first-time-setup.md` · `release-process.md` · `m2.2-runbook.md` ·
  `m4-runbook.md` — sources behind the build-deploy/release skills (the
  m4-runbook's `python -m bridge` command is stale; the client is now
  `python -m apworld.smbw_archipelago.client.main`).
- `LOGIC_COMPARISON.md` (root) — now a redirect stub; the logic-PDF
  reconciliation record moved into the **smbw-logic** skill
  (`.claude/skills/smbw-logic/reference/logic-reconciliation.md`).

## Game artifacts

- **SMBW v1.0.0**, BID `CD6E42AEE7934F4D`, codename `Secred.nss`.
- Extracted NSO: `C:\Users\maxwe\Desktop\Roms\Switch\Super Mario Bros. Wonder\main.nso`
  (hactool at `…\Desktop\Switch\hactool.exe`, keys at `~/.switch/prod.keys`).
- **Do not apply the v1.0.1 update in Ryujinx** — every offset is pinned to v1.0.0.

## Critical gotchas — these crash the game (don't relearn)

Full detail + the live hakkun hook idiom are in the **smbw-reverse-engineering**
skill; the load-bearing ones, kept visible here:

1. **Never `thread_local` in subsdk code** — no TLS allocator is registered;
   `SetMemoryAllocatorForThreadLocal` aborts at load (Result `0xCA8`). Use
   `static std::atomic<…>` + manual TID check.
2. **Inline hooks patch the first ~5 instructions** — a PC-relative insn (adrp,
   ldr-literal, b/bl) in those bytes corrupts the trampoline, with *delayed*
   symptoms. Verify a clean prologue, or hook a shared inner helper and filter by
   caller identity (why we trap `FUN_7100559f7c`, not per-Nerve slot 8).
3. **Don't hook `nn::prepo::PlayReport` beyond ctor + `SetEventId`** — `Save()`/
   `Add()` trampolines trigger a delayed abort on an SDK validator thread. Drop to
   the IPC layer `CmifProxyImpl<IPrepoService>::_nn_sf_sync_SaveReport{,WithUser}`.
4. **NSO and SDK have independent bases** — `installAtMainOffset` for NSO-relative
   game code; `installAtSym<>` resolves SDK + game symbols transparently.
5. **Import the whole `switch-mod/syms/100/` tree into Ghidra**, not just
   `sdk.sym` — that's where `gmd::GameDataMgr::sInstance @ +0x0363F0F0` lives.
   Run `scripts/ghidra/import_sdk_symbols.py`.

## Status (snapshot — handoff.md is authoritative)

Shipped: M1 (Wonder-Seed + Course-Clear nerves, 330/663 checks), M2.4–2.6
(PlayReport capture + Python decoder + bridge), M3.2 (badges), M3.3/3.3b
(container-A counters + container-B bool grants), M3.7 (game-completion goal),
M3.8 (DeathLink), M4 + M4.5 (LAN bridge + AP-authoritative replay). The grant
primitives are ported into the hakkun build (`src/probe/*`).

Active: Wonder-Seed **per-course persistence** (Container D writer) and the
Royal-Seed **gate-entry / check-loss** spike — both tracked in the `docs/`
handoffs listed above. Deferred: M2.2 (10-coin), M3.1/M3.4 (power-ups/chars,
precollected for now), M5 (in-game grant suppression), M6 (real-hw deploy), M7.
