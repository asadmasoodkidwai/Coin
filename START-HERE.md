# Coin Board — final package

Extract this folder before running commands. Install Python 3.12 or newer for the core app (Python 3.12 recommended if using optional AI packages).

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://localhost:8000. Later, run run.bat. The included trained Chair checkpoint loads into a fresh database automatically. Optional service settings can be placed in .env using .env.example. No keys are needed for the demo mode. Read README.md and TRAINING.md for details.

The package includes the complete app source, frontend, dependencies, Windows launch/setup scripts, tests and trained checkpoint. It does not include a Python environment, private keys or an existing account database.
