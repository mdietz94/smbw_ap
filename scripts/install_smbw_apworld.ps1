# install_smbw_apworld.ps1
#
# Dev-mode install: creates a Windows directory junction at
#   vendor/Archipelago/custom_worlds/smbw_archipelago
# pointing at
#   apworld/smbw_archipelago
# so Archipelago Launcher autodiscovers our apworld (including the
# "SMBW Client" Component) on every Launcher start.
#
# Idempotent.  Re-run after a fresh clone or after deleting the
# vendor/Archipelago submodule.

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Source   = Join-Path $RepoRoot "apworld\smbw_archipelago"
$CustomWorldsDir = Join-Path $RepoRoot "vendor\Archipelago\custom_worlds"
$Target   = Join-Path $CustomWorldsDir "smbw_archipelago"

if (-not (Test-Path $Source -PathType Container)) {
    Write-Error "apworld source not found: $Source"
    exit 1
}

if (-not (Test-Path $CustomWorldsDir -PathType Container)) {
    New-Item -ItemType Directory -Path $CustomWorldsDir -Force | Out-Null
}

if (Test-Path $Target) {
    $item = Get-Item $Target -Force
    $isLink = ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    if ($isLink) {
        Write-Host "junction already exists at $Target -- leaving as-is."
        exit 0
    } else {
        Write-Error "non-junction directory exists at $Target.  Move it aside first."
        exit 1
    }
}

cmd /c mklink /J "`"$Target`"" "`"$Source`"" | Out-Null
if (Test-Path $Target) {
    Write-Host "created junction: $Target -> $Source"
} else {
    Write-Error "mklink failed -- is Developer Mode enabled or are you running as Administrator?"
    exit 1
}
