# Build DesktopAudioStreamer.exe from the spec.
# Usage:  .\scripts\build.ps1
# Requires the repo .venv with dev deps + pyinstaller installed
# (pip install -r requirements.txt pyinstaller).
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "no .venv at $py - create it and pip install -r requirements.txt pyinstaller" }

Write-Host "Cleaning build/ and dist/..."
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

Write-Host "Building (this takes a few minutes)..."
& $py -m PyInstaller --noconfirm --clean DesktopAudioStreamer.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

$exe = Join-Path $repo "dist\DesktopAudioStreamer.exe"
if (Test-Path $exe) {
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "OK: $exe ($mb MB)"
} else {
    throw "build reported success but $exe is missing"
}
