# Narrator Studio - one-time setup for Windows.
# Run from this folder:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Test-Cmd($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }
function Install-WithWinget($id, $what) {
    if (-not (Test-Cmd winget)) { throw "$what is missing and winget isn't available. Install $what manually (see README.md)." }
    Write-Host "Installing $what with winget (you may see an admin prompt)..." -ForegroundColor Yellow
    winget install -e --id $id --accept-source-agreements --accept-package-agreements
}

# 1. Python 3.9-3.11 (Coqui TTS does not support 3.12+)
Write-Host "`n[1/4] Looking for Python 3.11 / 3.10 / 3.9..." -ForegroundColor Cyan
$py = $null
foreach ($v in "3.11", "3.10", "3.9") {
    if (Test-Cmd py) { try { & py "-$v" -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $py = @("py", "-$v"); break } } catch {} }
}
if (-not $py) {
    Install-WithWinget "Python.Python.3.11" "Python 3.11"
    $py = @("py", "-3.11")
}
Write-Host "Using: $($py -join ' ')"

# 2. ffmpeg
Write-Host "`n[2/4] Checking ffmpeg..." -ForegroundColor Cyan
if (Test-Cmd ffmpeg) { Write-Host "ffmpeg found." } else { Install-WithWinget "Gyan.FFmpeg" "ffmpeg" }

# 3. espeak-ng
Write-Host "`n[3/4] Checking espeak-ng..." -ForegroundColor Cyan
$espeak = (Test-Cmd espeak-ng) -or (Test-Path "C:\Program Files\eSpeak NG\espeak-ng.exe") -or (Test-Path "C:\Program Files (x86)\eSpeak NG\espeak-ng.exe")
if ($espeak) { Write-Host "espeak-ng found." } else { Install-WithWinget "eSpeak-NG.eSpeak-NG" "espeak-ng" }

# 4. Virtual environment + Python packages
Write-Host "`n[4/4] Creating virtual environment and installing packages (PyTorch is large, this takes a while)..." -ForegroundColor Cyan
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $py[0] $py[1] -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Package install failed." }

Write-Host "`nSetup complete. Start the app with:  .\run.ps1" -ForegroundColor Green
Write-Host "(If ffmpeg was just installed, open a new terminal first so it's on PATH.)"
