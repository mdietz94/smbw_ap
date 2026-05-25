# launch_client.ps1
#
# One-shot script: install deps if needed, build + install the SMBW
# apworld into vendor/Archipelago/custom_worlds/, then launch the
# Kivy client.  Idempotent -- safe to re-run after every code change.
#
# Usage (from anywhere; the script figures out the worktree root):
#   powershell -ExecutionPolicy Bypass -File scripts\launch_client.ps1
# Or if you're already in a pwsh prompt with execution policy relaxed:
#   .\scripts\launch_client.ps1

$ErrorActionPreference = "Stop"

# --- Resolve paths -----------------------------------------------------

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot
Write-Host "[launch_client] repo root: $RepoRoot" -ForegroundColor Cyan

$ApRoot = Join-Path $RepoRoot "vendor\Archipelago"
if (-not (Test-Path (Join-Path $ApRoot "CommonClient.py"))) {
    Write-Error "Archipelago not found at $ApRoot.  Run: git submodule update --init --recursive"
    exit 1
}

# --- Pick Python -------------------------------------------------------

# Use a project-local venv at .venv\ if one exists; otherwise fall back
# to whatever `python` is on PATH.  Reusing the venv across runs keeps
# Kivy install costs to a one-time hit.
$VenvDir = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "[launch_client] no .venv\ found -- creating one (this takes ~10s)..." -ForegroundColor Cyan
    python -m venv $VenvDir
    if (-not (Test-Path $VenvPython)) {
        Write-Error "venv creation failed -- is `python` on PATH?"
        exit 1
    }
}
$Py = $VenvPython
Write-Host "[launch_client] python: $Py" -ForegroundColor Cyan

# --- Install dependencies if Kivy is missing --------------------------

# Kivy check: PowerShell 5.1 wraps native-cmd stderr as ErrorRecord
# under $ErrorActionPreference="Stop", which would abort us on the
# expected ImportError.  Suspend the error preference for this one
# probe and swallow both streams.
$prev = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
$null = & $Py -c "import kivy" 2>&1
$kivyExit = $LASTEXITCODE
$ErrorActionPreference = $prev

if ($kivyExit -ne 0) {
    Write-Host "[launch_client] installing Archipelago dependencies (one-time, ~2 min)..." -ForegroundColor Cyan
    & $Py -m pip install --upgrade pip
    & $Py -m pip install -r (Join-Path $ApRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Write-Error "pip install failed -- check the output above.  Common cause on Python 3.13: Kivy 2.x doesn't ship 3.13 wheels yet; use Python 3.11 or 3.12."
        exit 1
    }
} else {
    Write-Host "[launch_client] Kivy already installed -- skipping pip install" -ForegroundColor DarkGray
}

# --- Build + install the apworld --------------------------------------

Write-Host "[launch_client] building apworld zip..." -ForegroundColor Cyan
& $Py (Join-Path $RepoRoot "scripts\build_apworld.py")
if ($LASTEXITCODE -ne 0) { Write-Error "build_apworld.py failed"; exit 1 }

$CustomWorldsDir = Join-Path $ApRoot "custom_worlds"
if (-not (Test-Path $CustomWorldsDir)) {
    New-Item -ItemType Directory -Path $CustomWorldsDir -Force | Out-Null
}
$ZipSrc = Join-Path $RepoRoot "build\smbw_archipelago.apworld"
$ZipDst = Join-Path $CustomWorldsDir "smbw_archipelago.apworld"
Copy-Item $ZipSrc -Destination $ZipDst -Force
Write-Host "[launch_client] installed: $ZipDst" -ForegroundColor Cyan

# --- Launch ------------------------------------------------------------

Write-Host "[launch_client] launching SMBW Client..." -ForegroundColor Green
Write-Host "                (close the Kivy window or Ctrl-C here to exit)" -ForegroundColor DarkGray
& $Py (Join-Path $RepoRoot "scripts\run_client.py") @args
