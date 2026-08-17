---
name: smbw-build-deploy
description: >-
  Build, deploy, run, and smoke-test the SMBW Archipelago Switch subsdk and
  Kivy client. Use whenever you need to compile switch-mod (subsdk9), deploy it
  to Ryujinx, launch the SMBW Client, tail the in-game [smbwap] log, do the
  first-time /setup, or run the inbound/outbound end-to-end smoke tests. Covers
  Windows (PowerShell) and Linux. Triggers: "build the mod", "deploy to
  Ryujinx", "rebuild subsdk9", "run the client", "tail the log", "smoke test".
---

# SMBW build / deploy / run

The Switch side is a **hakkun** subsdk (LLVM 19 + LibHakkun) under `switch-mod/`.
The PC side is a Kivy Archipelago client under `apworld/smbw_archipelago/client/`.
Target: **SMBW v1.0.0** (title ID `010015100b514000`), dev on **Ryujinx 1.3.3**.

> Paths below are repo-relative — run from the repo root. The Ryujinx deploy
> path is user-specific (`%APPDATA%` / `~/.config`). `cmake` may not be on PATH;
> on this machine it's at `C:\Program Files\CMake\bin\cmake.exe` — use that full
> path if bare `cmake` fails.

## Prereqs

LLVM 19.1.x (clang on PATH), CMake 3.16+, Ninja, Python 3.11+. On Windows a
`python3.exe` shim must sit beside the interpreter (LibHakkun's
`toolchain.cmake` shells out to bare `python3`). **devkitPro is not used** —
hakkun hardcodes clang/clang++.

The easiest path is the **`/setup` wizard** inside SMBW Client (probe → install
missing tools → junction → build → deploy). It auto-installs system tools on
Windows (winget + pinned LLVM 19.1.7); on Linux it prints what's missing and
leaves the package manager to you. See [docs/first-time-setup.md](../../../docs/first-time-setup.md)
for the user-facing first-run flow.

### Linux gotcha: the Archipelago AppImage shadows system libraries

When AP runs as an AppImage, its `AppRun` exports loader vars pointing into the
`/tmp/.mount_<name><rand>/` mount (`LD_LIBRARY_PATH`, `PYTHONHOME`, …). Anything
the wizard spawns inherits them, so a *host* binary loads the bundle's `.so`s and
dies when the bundled copy is older than what the host binary needs:

```
cmake: /tmp/.mount_archipMfEgoA/opt/Archipelago/lib/libssl.so.3:
    version `OPENSSL_3.2.0' not found (required by /usr/lib/libcurl.so.4)
```

That is *not* an OpenSSL-too-old problem — the system OpenSSL is being shadowed.
[`_setup/child_env.py`](../../../apworld/smbw_archipelago/_setup/child_env.py)
strips those vars from every child the wizard spawns (probes, installers, cmake),
while leaving them intact for commands that resolve *inside* the mount. Reaching
for the same thing by hand: `env -u LD_LIBRARY_PATH -u PYTHONHOME cmake …`.

Two related Linux wrinkles the wizard now handles:
- **Side-by-side LLVM 19**: `clang` on PATH is the distro's current release (22
  on Arch today). `apt install clang-19` → `/usr/lib/llvm-19/bin`, AUR `llvm19` →
  `/usr/lib/llvm19/bin`; the prereq probes those roots and pins the build's PATH
  to whichever one answers to bare `clang` at 19.1.x.
- **PEP 668**: `pip install --user lz4` is refused outright on distro-managed
  interpreters, so the installers retry with `--break-system-packages` (still
  user-site only) and then without `--user` for venvs.

## Build

```pwsh
# Windows — incremental build (after the one-time configure below)
& "C:\Program Files\CMake\bin\cmake.exe" --build "switch-mod\build"
```
```bash
# Linux
cmake --build "switch-mod/build"
```

**First-time configure** (only after a fresh clone or after deleting `build/`):

```pwsh
& "C:\Program Files\CMake\bin\cmake.exe" -S "switch-mod" -B "switch-mod\build" -G Ninja
```
```bash
cmake -S "switch-mod" -B "switch-mod/build" -G Ninja
```

⚠️ **Do NOT pass `-DCMAKE_TOOLCHAIN_FILE`.** `switch-mod/CMakeLists.txt` sets
`CMAKE_TOOLCHAIN_FILE` to `sys/cmake/toolchain.cmake` before `project()`, which
wins over (and conflicts with) a command-line `-D`.

Build artifacts land in `switch-mod/build/`: `subsdk9` + `subsdk9.npdm`.

## Deploy to Ryujinx

```pwsh
$dst = "$env:APPDATA\Ryujinx\mods\contents\010015100b514000\smbwap\exefs"
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item "switch-mod\build\subsdk9"      -Destination $dst -Force
Copy-Item "switch-mod\build\subsdk9.npdm" -Destination "$dst\main.npdm" -Force   # NOTE: renamed to main.npdm
```
```bash
dst="${XDG_CONFIG_HOME:-$HOME/.config}/Ryujinx/mods/contents/010015100b514000/smbwap/exefs"
mkdir -p "$dst"
cp "switch-mod/build/subsdk9"      "$dst/"
cp "switch-mod/build/subsdk9.npdm" "$dst/main.npdm"
```

## Run the SMBW Client

The client owns the LAN socket (`:17777`) and UDP discovery (`:17776`) — nothing
else runs PC-side besides the AP server, this client, and Ryujinx.

```pwsh
# Via Archipelago Launcher (chrome + "SMBW Client" button)
python vendor/Archipelago/Launcher.py

# Or direct module invocation (skips Launcher; add --nogui for headless CI)
python -m apworld.smbw_archipelago.client.main --connect=localhost:38281 --name=Mario
```

Dev-mode autodiscovery needs a junction under `vendor/Archipelago/custom_worlds/`
(one-time): `python scripts/install_smbw_apworld.py` (or run `/setup`, which does
this as one phase). `scripts/launch_client.ps1` is an all-in-one
install-deps → build → install-apworld → launch.

## Tail the in-game log

Our lines are prefixed `[smbwap inf]` / `[smbwap dbg]`. Ryujinx writes
`svcOutputDebugString` to its file log with **embedded NUL bytes** — strip them
for offline parsing (`tr -d '\0'`).

```pwsh
$latest = Get-ChildItem "C:\Users\maxwe\Desktop\Switch\ryujinx-1.3.3\Logs\Ryujinx_*.log" |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content -Wait $latest.FullName | Select-String '\[smbwap'
```
```bash
ls -t "$HOME/.config/Ryujinx/Logs/Ryujinx_"*.log | head -1 |
    xargs -I{} tail -F {} | tr -d '\0' | grep '\[smbwap'
```

## End-to-end smoke tests

After build + deploy, with the AP server + SMBW Client + Ryujinx all running
(full runbook with the older `python -m bridge` invocation is in
[docs/m4-runbook.md](../../../docs/m4-runbook.md) — the *client* command is now
`python -m apworld.smbw_archipelago.client.main`):

| Test | Action | Expect in `[smbwap]` log / game |
|---|---|---|
| **Inbound grant** | AP `/send <Player> Spring Feet Badge` | badge appears live in the equip menu within ~2 s (container-C absolute overwrite tick) |
| **Outbound check** | enter W1-1, touch the flagpole | `COURSE_CLEARED` nerve fires → location check sent to AP |
| **Wonder Seed** | grab a Wonder Seed | `WONDER_SEED_AWARDED` nerve fires → location check |
| **Reconnect** | kill + restart the client, re-enter a course | Switch `HelloMsg` replays badges/seeds; grants survive |

For grant internals (which primitive each item routes to, container layout, the
hash-key table) see the **smbw-save-data** skill. For adding new hooks/checks,
see **smbw-reverse-engineering**. To cut a release, see **smbw-release**.

## Python tests

```pwsh
python -m pytest apworld/smbw_archipelago        # client + wire + decode suite
```
`conftest.py` puts `vendor/Archipelago` on `sys.path`.
