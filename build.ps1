# Build entry point: makes the project .venv if needed, installs deps, runs build.py in it.
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1
# ASCII only on purpose: Windows PowerShell 5.1 reads BOM-less .ps1 files as ANSI.
# Everything runs in the project's own .venv, never in the global Python.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$root = $PSScriptRoot

$py = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating .venv" -ForegroundColor Cyan
    py -3 -m venv "$root\.venv"
    if ($LASTEXITCODE) { python -m venv "$root\.venv" }
    if ($LASTEXITCODE) { throw "venv failed" }
}
& $py -m pip install --disable-pip-version-check -q -r "$root\requirements-dev.txt"
if ($LASTEXITCODE) { throw "pip install failed" }

& $py "$root\build.py"
if ($LASTEXITCODE) { throw "build.py failed" }
