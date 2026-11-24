# Start Narrator Studio (run setup.ps1 once first).
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Not set up yet - run:  powershell -ExecutionPolicy Bypass -File .\setup.ps1" -ForegroundColor Yellow
    exit 1
}
& .\.venv\Scripts\python.exe app.py
