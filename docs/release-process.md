# Release process

For maintainers cutting a new SMBW Archipelago release.

## TL;DR

```pwsh
git tag -a v0.1.0-alpha1 -m "v0.1.0-alpha1"
git push origin v0.1.0-alpha1
```

`.github/workflows/release.yml` does the rest: builds the bundled
`smbwonder.apworld`, computes a SHA-256, creates a GitHub Release with both
files attached, auto-generates the release notes from commit messages.

## What gets shipped

The release artifact is a single file, `smbwonder.apworld`, plus a sidecar
`smbwonder.apworld.sha256` for download verification. The `.apworld` is a
normal zip with two logical regions:

| Inside the zip | Source | What for |
|---|---|---|
| `smbwonder/...` | `apworld/smbw_archipelago/` minus `tests/`, `__pycache__/`, dev-only caches | The apworld itself (items, locations, regions, hooks, client/, _setup/) |
| `smbwonder/_setup/switch_mod/...` | `switch-mod/` + the three lib submodules (`imgui`, `NintendoSDK`, `sead`) | C++ source tree the wizard compiles on the user's machine |

Size budget: ~13 MB compressed (the lib submodules — particularly imgui's
demo collection — are the bulk of the weight).

The wizard rebuilds `subsdk9` locally from the bundled sources on every
fresh user install; we do NOT ship a precompiled Switch binary. This
keeps CI off of devkitA64 (no toolchain in the runner) and avoids
distributing a compiled artifact that depends on Nintendo SDK headers.

## Tag conventions

| Tag pattern | Result |
|---|---|
| `v1.0.0` | Full release. Marked stable in the GitHub Releases UI. |
| `v1.0.0-rc1`, `v0.2.0-alpha2`, etc. (any tag containing `-`) | Pre-release. Shown lower in the Releases UI; doesn't update "Latest". |
| Anything not matching `v*.*.*` | CI doesn't trigger. |

Initial first release: `v0.1.0-alpha1`.

## Dry-running before tagging

The release workflow has `workflow_dispatch` enabled, so you can fire it
manually from the GitHub Actions UI without pushing a tag. The
`build-apworld` job runs end-to-end (skipping the version-stamping step
because there's no tag); the `publish-release` job skips itself when
there's no tag. Download the produced `smbwonder-apworld` artifact from
the run page to inspect it.

To dry-run locally:

```pwsh
# Make sure submodules are populated
git submodule update --init --recursive

# Build the full release zip
python scripts/install_apworld.py --bundle-mod

# Output ends up at vendor/Archipelago/custom_worlds/smbwonder.apworld
# Inspect with:
python -c "import zipfile; zipfile.ZipFile('vendor/Archipelago/custom_worlds/smbwonder.apworld').printdir()"
```

## Pre-tag release-gate audit

The pre-push hook (`.githooks/pre-push`) runs an end-to-end live-network
wizard install test on the maintainer's machine before allowing a tag
push, exercising the bundle build, prereq probe, and a cold switch-mod
build.

**What the gate actually does**
([apworld/smbw_archipelago/_setup/tests/test_wizard_e2e_live.py](../apworld/smbw_archipelago/_setup/tests/test_wizard_e2e_live.py)):

1. Sandboxes `%APPDATA%\SMBWArchipelago\` + `%LOCALAPPDATA%\SMBWArchipelago\`
   into a tempdir at `C:\smbwape2e-XXXX\` — your real filesystem state is
   untouched.
2. Builds the apworld zip (`install_apworld.py --bundle-mod`) and asserts
   the bundle contains both regions and all three lib submodules.
3. Confirms `SMBWAP_APPDATA_ROOT` / `SMBWAP_LOCALAPPDATA_ROOT` overrides
   are in effect so wizard writes can't leak to real %APPDATA%.
4. Runs `wizard_cli.run_probe()` and prints per-prereq status.
5. If every build-side prereq is detected (devkitPro, cmake, ninja,
   python311, both submodules initialized), runs a real cold switch-mod
   build and asserts `subsdk9` + `subsdk9.npdm` exist post-build.
6. Restores the pre-test `smbwonder.apworld` (if any) so dev junctions
   stay intact.

**One-time setup** (after each fresh clone):

```pwsh
powershell -ExecutionPolicy Bypass -File scripts\install_hooks.ps1
```

Sets `core.hooksPath = .githooks` so the tracked `pre-push` hook fires.

**What happens on tag push:** `git push origin v0.X.Y-alpha` triggers
`.githooks/pre-push`, which runs `scripts\local_release_audit.ps1` →
which invokes the e2e test via `pytest -m smbwap_live_install`. The push
is blocked if any step fails. Wall time:

| State | Time |
|---|---|
| Cold build (first run on a machine, or after dependency bump) | ~5-10 min |
| Warm build (subsequent runs, cmake cache reused) | ~30-60 s |

**Run standalone any time:**

```pwsh
powershell -ExecutionPolicy Bypass -File scripts\local_release_audit.ps1
# or directly:
$env:SMBWAP_LIVE_INSTALL = "1"; python -m pytest `
  apworld\smbw_archipelago\_setup\tests\test_wizard_e2e_live.py -v -s
```

**Bypass (use sparingly — only if you've already audited manually):**

```pwsh
git push --no-verify origin v0.X.Y-alpha
```

**Requirements the gate does not auto-install:** `devkitPro`, `cmake`,
`ninja`, `git`, and `python` must already be on PATH (auto-installed by
the wizard on a user's machine; expected to already be present on a
maintainer's). If missing, the test skips with a clear message.

## Pre-release checklist

Before pushing a release tag, verify:

- `python -m pytest apworld/smbw_archipelago` is green
- `scripts\local_release_audit.ps1` exits clean (the pre-push hook runs
  this automatically; this line is for when you want to validate before
  even tagging)
- `docs/first-time-setup.md` reflects any prereq changes
- `CLAUDE.md` and the active plan file have been updated if architecture
  shifted

## Versioning

Semantic versioning where:

- **MAJOR** bumps on wire-protocol breaks (Switch mod ↔ SMBW Client) — these
  force users to re-run setup, so flag prominently in release notes
- **MINOR** bumps on user-visible features (new logic options, new
  apworld items, new wizard pages, new commands)
- **PATCH** bumps on bug fixes, dependency updates, doc-only changes

The current version is implicit in the most-recent tag. The release
workflow's `Stamp version from tag` step rewrites:

- `apworld/smbw_archipelago/__init__.py` `__version__`
- `apworld/smbw_archipelago/archipelago.json` `world_version`

…to match the tag. Dev checkouts and `workflow_dispatch` runs leave the
`"0.0.0+dev"` sentinel in place so user-reported logs from a released
artifact can never be confused with a dev build.

## Manual release (if CI is broken)

```pwsh
# Build the artifact locally as above
python scripts/install_apworld.py --bundle-mod

# Generate checksum
$hash = (Get-FileHash vendor\Archipelago\custom_worlds\smbwonder.apworld -Algorithm SHA256).Hash.ToLower()
"$hash  smbwonder.apworld" | Out-File -Encoding ascii smbwonder.apworld.sha256

# Tag + push
git tag -a v0.1.0 -m "v0.1.0"
git push origin v0.1.0

# Create the release manually with gh
gh release create v0.1.0 `
    vendor/Archipelago/custom_worlds/smbwonder.apworld smbwonder.apworld.sha256 `
    --title "v0.1.0" `
    --generate-notes
```
