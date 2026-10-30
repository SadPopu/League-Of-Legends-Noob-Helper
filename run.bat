@echo off
cd /d "%~dp0"
if not exist .venv (
    echo Creating the Python environment...
    python -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist .env (
    copy .env.example .env >nul
    echo Created the .env file - fill in RIOT_API_KEY and RIOT_ID, then run this again.
    notepad .env
    exit /b
)
start "" http://127.0.0.1:8765
.venv\Scripts\python -m uvicorn coach.main:app --host 127.0.0.1 --port 8765
