# Build a self-contained Windows bundle in dist\EZGameDownloader\ and a zip.
# Requires 64-bit Python 3.12 (python.org installer or `py -3.12`).
# Usage (PowerShell):  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

.\.venv\Scripts\python.exe packaging\make_icons.py
.\.venv\Scripts\python.exe packaging\update_adblock.py   # bundle the newest ad blocklist (never fails the build)
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\ezgd.spec

Copy-Item packaging\install_windows.ps1 dist\EZGameDownloader\
Compress-Archive -Force -Path dist\EZGameDownloader -DestinationPath dist\EZGameDownloader-windows-x64.zip
Write-Host "Built dist\EZGameDownloader\ezgd.exe and dist\EZGameDownloader-windows-x64.zip"
Write-Host "Note: RAR extraction needs 7-Zip or WinRAR installed (or set the tool path in Settings)."
