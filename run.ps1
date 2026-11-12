Set-Location $PSScriptRoot
Write-Host "Starting Coin Board at http://localhost:8000 (close this window to stop it)" -ForegroundColor Green
Start-Job { Start-Sleep 5; Start-Process "http://localhost:8000" } | Out-Null
& .\.venv\Scripts\python.exe -m uvicorn app.main:app
