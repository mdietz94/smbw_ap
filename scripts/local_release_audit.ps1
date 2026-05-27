# Pre-tag release-gate harness. Fires the live wizard install e2e test
# (apworld/smbw_archipelago/_setup/tests/test_wizard_e2e_live.py) which:
#   1. Sandboxes APPDATA + LOCALAPPDATA into a tempdir
#   2. Runs install_apworld --bundle-mod (real)
#   3. Runs wizard_cli.run_probe to confirm prereqs detect green
#   4. Runs wizard_cli.run_build to compile switch-mod end-to-end
#   5. Asserts subsdk9 + subsdk9.npdm exist post-build
#
# Total wall time ~5-10 min for the build phase. User state never
# touched (sandbox env vars route writes into the tempdir).
#
# Invoked automatically by .githooks/pre-push on `git push origin v*`.
# Run standalone with: powershell -File scripts\local_release_audit.ps1
#
# ASCII-only: PowerShell 5.1 reads .ps1 files as the active ANSI codepage
# when there is no BOM. Stick to plain ASCII characters in this file.

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path "$PSScriptRoot\..").Path

function Write-Step($msg) { Write-Host "[release-audit] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[release-audit] $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "[release-audit] $msg" -ForegroundColor Red }

Write-Step "running test_wizard_e2e_live.py (~5-10 min for cold build) ..."

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
  throw "python not found on PATH -- install Python 3.11 (the wizard's prereq install handles this)."
}

# Locate the repo-root .venv at the main checkout if running from a
# worktree (where the venv isn't symlinked in). Falls back to the system
# python if the venv isn't there.
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
  # Probe the main checkout (worktrees live under .claude/worktrees/...).
  $maybeMain = Join-Path $repoRoot "..\..\..\.venv\Scripts\python.exe"
  if (Test-Path $maybeMain) {
    $venvPython = (Resolve-Path $maybeMain).Path
  } else {
    $venvPython = $pythonCmd.Source
  }
}
Write-Host "  Python: $venvPython"

$env:SMBWAP_LIVE_INSTALL = "1"
try {
  & $venvPython -m pytest `
    (Join-Path $repoRoot "apworld\smbw_archipelago\_setup\tests\test_wizard_e2e_live.py") `
    -v -s
  $rc = $LASTEXITCODE
} finally {
  Remove-Item Env:\SMBWAP_LIVE_INSTALL -ErrorAction SilentlyContinue
}

if ($rc -eq 0) {
  Write-Ok "release audit PASSED."
  exit 0
} else {
  Write-Fail "release audit FAILED (pytest rc=$rc)."
  exit $rc
}
