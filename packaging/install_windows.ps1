# EZ Game Downloader — Windows installer (run from the unpacked release folder).
#   powershell -ExecutionPolicy Bypass -File install_windows.ps1
# Installs the app into %LOCALAPPDATA%\Programs\EZGameDownloader, adds Start menu and desktop
# shortcuts, and installs 7-Zip with winget if no RAR extractor (7-Zip/WinRAR) is present.
# NOTE: written for Windows 10/11 but not tested on Windows by the developer.
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
if (-not (Test-Path (Join-Path $src "ezgd.exe"))) { throw "Run this script from the unpacked EZGameDownloader folder." }

$dest = Join-Path $env:LOCALAPPDATA "Programs\EZGameDownloader"
Write-Host "==> Installing to $dest"
if ((Resolve-Path $src).Path -ne $dest) {
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    Copy-Item -Recurse -Force $src $dest
}

$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "EZ Game Downloader.lnk"))
    $lnk.TargetPath = Join-Path $dest "ezgd.exe"
    $lnk.WorkingDirectory = $dest
    $lnk.Save()
}

$have7z = (Test-Path "$env:ProgramFiles\7-Zip\7z.exe") -or (Test-Path "${env:ProgramFiles(x86)}\7-Zip\7z.exe") -or
          (Test-Path "$env:ProgramFiles\WinRAR\UnRAR.exe")
if (-not $have7z) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "==> Installing 7-Zip (needed for RAR archives)"
        winget install --id 7zip.7zip -e --accept-source-agreements --accept-package-agreements
    } else {
        Write-Warning "Install 7-Zip from https://www.7-zip.org/ — it is needed to extract RAR archives."
    }
}
Write-Host "Done. Start 'EZ Game Downloader' from the Start menu."
