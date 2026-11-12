# Coin Board updater. Run from the extracted coin-board folder in Windows PowerShell.
# Keeps your existing .env, coin.db, and .venv; only updates application code/assets.
$ErrorActionPreference = 'Stop'
$proj = 'C:\coin-board'
$source = $PSScriptRoot

if (-not (Test-Path (Join-Path $source 'app\main.py')) -or -not (Test-Path (Join-Path $source 'static\index.html'))) {
    throw 'Run this script from the extracted coin-board folder (app and static required).'
}
New-Item -ItemType Directory -Path $proj -Force | Out-Null

# Keep a backup of the existing source files before updating.
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupRoot = Join-Path $proj ('backups\before-' + $stamp)
foreach ($folder in @('app', 'static', 'tests')) {
    $old = Join-Path $proj $folder
    if (Test-Path $old) {
        New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
        Copy-Item -LiteralPath $old -Destination $backupRoot -Recurse -Force
    }
}

foreach ($folder in @('app', 'static', 'tests')) {
    $from = Join-Path $source $folder
    $to = Join-Path $proj $folder
    if (Test-Path $to) { Remove-Item -LiteralPath $to -Recurse -Force }
    Copy-Item -LiteralPath $from -Destination $proj -Recurse -Force
}
foreach ($name in @('README.md','LICENSE','requirements.txt','requirements-models.txt','setup.ps1','setup.bat','run.ps1','run.bat','.env.example')) {
    $from = Join-Path $source $name
    if (Test-Path $from) { Copy-Item -LiteralPath $from -Destination $proj -Force }
}

Write-Host 'Your existing .env, coin.db, and .venv are preserved.' -ForegroundColor Green
Write-Host ('Source backup: ' + $backupRoot)
Set-Location $proj

$py = Join-Path $proj '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        throw 'Python launcher not found. Install Python 3.12 from python.org.'
    }
    & py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Python 3.12 virtual environment.' }
}

& $py -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $py -m pytest -q tests
if ($LASTEXITCODE -ne 0) { throw 'Tests failed. Review the output before starting Coin Board.' }

Write-Host 'Coin Board ready. Open http://localhost:8000 in your browser.' -ForegroundColor Green
& $py -m uvicorn app.main:app --host 127.0.0.1 --port 8000
