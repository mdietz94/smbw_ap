---
name: smbw-release
description: >-
  Cut a release of the SMBW Archipelago apworld (maintainer-only). Use when
  tagging a new version, building/inspecting the bundled smbwonder.apworld,
  running the pre-push release-gate audit, dry-running the GitHub release
  workflow, recovering with a manual gh release, or deciding the semver bump.
  Triggers: "cut a release", "tag a version", "release audit", "build the
  apworld bundle", "publish to GitHub".
---

# SMBW release process (maintainers)

The release artifact is a single `smbwonder.apworld` (a zip) + a sidecar
`smbwonder.apworld.sha256`. The zip bundles the apworld AND the `switch-mod/`
C++ source (+ `sys/` LibHakkun and `lib/imgui` submodules) — the wizard
**rebuilds `subsdk9` locally on each user's machine**; we never ship a compiled
Switch binary (keeps CI off the LLVM cross-compile toolchain).

## TL;DR — tag and push

```pwsh
git tag -a v0.1.0-alpha1 -m "v0.1.0-alpha1"
git push origin v0.1.0-alpha1
```

`.github/workflows/release.yml` then builds the bundle, computes the SHA-256,
stamps the version from the tag, and creates a GitHub Release with both files +
auto-generated notes. The pre-push hook runs the release-gate audit first.

## Tag conventions

| Tag | Result |
|---|---|
| `v1.0.0` | full release, marked "Latest" |
| any tag with `-` (`v1.0.0-rc1`, `v0.2.0-alpha2`) | pre-release, doesn't update "Latest" |
| anything not matching `v*.*.*` | CI does not trigger |

The `Stamp version from tag` step rewrites `__version__` in
`apworld/smbw_archipelago/__init__.py` and `world_version` in
`apworld/smbw_archipelago/archipelago.json`. Dev checkouts and
`workflow_dispatch` runs keep the `0.0.0+dev` sentinel.

## Semver

- **MAJOR** — wire-protocol break (Switch mod ↔ SMBW Client); forces users to
  re-run `/setup`. Flag prominently in notes.
- **MINOR** — user-visible feature (logic options, items, wizard pages, commands).
- **PATCH** — bug fixes, dep bumps, doc-only.

## Pre-release checklist

- `python -m pytest apworld/smbw_archipelago` green.
- `scripts\local_release_audit.ps1` exits clean.
- `docs/first-time-setup.md` reflects any prereq change.
- `CLAUDE.md` + the active plan/handoff updated if architecture shifted.

## Pre-push release-gate audit

The tracked `.githooks/pre-push` hook runs an end-to-end live wizard install
test before allowing a tag push (bundle build, prereq probe, cold switch-mod
build, sandboxed into a tempdir so real `%APPDATA%` is untouched).

```pwsh
# One-time after a fresh clone — point git at the tracked hooks:
powershell -ExecutionPolicy Bypass -File scripts\install_hooks.ps1     # sets core.hooksPath=.githooks

# Run the audit standalone any time:
powershell -ExecutionPolicy Bypass -File scripts\local_release_audit.ps1
```

Wall time: cold build ~5-10 min, warm ~30-60 s. The gate does NOT auto-install
clang/cmake/ninja/git/python — they must be on PATH (the wizard installs them on
a user's machine; assumed present on a maintainer's). Missing → the test skips
with a clear message. Bypass with `git push --no-verify` only after a manual audit.

## Dry-run before tagging

`release.yml` has `workflow_dispatch` — fire it from the Actions UI with no tag;
`build-apworld` runs end-to-end (skips version-stamping), `publish-release`
self-skips. Or locally:

```pwsh
git submodule update --init --recursive
python scripts/install_apworld.py --bundle-mod
# → vendor/Archipelago/custom_worlds/smbwonder.apworld
python -c "import zipfile; zipfile.ZipFile('vendor/Archipelago/custom_worlds/smbwonder.apworld').printdir()"
```

## Manual release (CI broken)

```pwsh
python scripts/install_apworld.py --bundle-mod
$hash = (Get-FileHash vendor\Archipelago\custom_worlds\smbwonder.apworld -Algorithm SHA256).Hash.ToLower()
"$hash  smbwonder.apworld" | Out-File -Encoding ascii smbwonder.apworld.sha256
git tag -a v0.1.0 -m "v0.1.0"; git push origin v0.1.0
gh release create v0.1.0 `
    vendor/Archipelago/custom_worlds/smbwonder.apworld smbwonder.apworld.sha256 `
    --title "v0.1.0" --generate-notes
```

Full prose (what the e2e gate asserts step-by-step) is in
[docs/release-process.md](../../../docs/release-process.md).
