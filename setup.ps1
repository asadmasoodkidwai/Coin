$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
Write-Host "Coin Board setup. This can take 10 to 30 minutes the first time (PyTorch is large)." -ForegroundColor Cyan

# 1. Find a suitable Python (3.12 / 3.11 / 3.10 work best with PyTorch)
$ver = $null
foreach ($v in "3.12","3.11","3.10") { py "-$v" -c "pass" 2>$null; if ($LASTEXITCODE -eq 0) { $ver = $v; break } }
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
  Write-Host "Python was not found. Install Python 3.12 from python.org (tick 'Add python.exe to PATH'), then run setup.bat again." -ForegroundColor Red
  Read-Host "Press Enter to close"; exit 1 }
if (-not $ver) { Write-Host "Python 3.10 to 3.12 not found. Continuing with your default Python; the AI model libraries may fail to install." -ForegroundColor Yellow }

# 2. Virtual environment (rebuild if a previous attempt left it empty)
if (-not (Test-Path ".venv\Scripts\python.exe")) {
  if (Test-Path ".venv") { Remove-Item ".venv" -Recurse -Force }
  if ($ver) { py "-$ver" -m venv .venv } else { py -m venv .venv }
}
if (-not (Test-Path ".venv\Scripts\python.exe")) { Write-Host "Could not create the virtual environment. Move this folder to C:\coin-board and try again." -ForegroundColor Red; Read-Host "Press Enter to close"; exit 1 }
$py = ".\.venv\Scripts\python.exe"

# 3. App + model libraries
& $py -m pip install --upgrade pip
& $py -m pip install -r requirements.txt
& $py -m pip install -r requirements-models.txt
if ($LASTEXITCODE -ne 0) { Write-Host "Model libraries failed to install (often an unsupported Python version). The app still runs, with labelled baselines." -ForegroundColor Yellow }

# 4. Kronos
$kr = Join-Path (Split-Path $PSScriptRoot -Parent) "Kronos"
if (-not (Test-Path $kr)) {
  if (Get-Command git -ErrorAction SilentlyContinue) { git clone https://github.com/shiyu-coder/Kronos $kr }
  else { Write-Host "Git not found. Install it from git-scm.com to enable Kronos. Skipping." -ForegroundColor Yellow } }
if (Test-Path "$kr\requirements.txt") { & $py -m pip install -r "$kr\requirements.txt" }

# 5. Settings file
if (-not (Test-Path ".env")) {
  $key = Read-Host "Paste your Groq API key (starts with gsk_), or press Enter to skip"
  $lines = @()
  if (Test-Path $kr) { $lines += "KRONOS_REPO=$kr" }
  if ($key) { $lines += "CHAIR_BASE_URL=https://api.groq.com/openai/v1", "CHAIR_MODEL=openai/gpt-oss-120b", "CHAIR_API_KEY=$key" }
  $lines | Set-Content ".env" -Encoding ASCII
}

# 6. Start
Write-Host "Starting Coin Board at http://localhost:8000 (close this window to stop it)" -ForegroundColor Green
Start-Job { Start-Sleep 8; Start-Process "http://localhost:8000" } | Out-Null
& $py -m uvicorn app.main:app
