@echo off
setlocal
cd /d "%~dp0"
title Noob Helper

rem --- Python installed? (the Microsoft Store "python" shortcut does not count)
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found.
    echo Install Python 3.11+ from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" in the installer, then run this again.
    goto :fail
)

rem --- Python environment: create it, or rebuild it if an earlier attempt left it broken
if not exist ".venv\Scripts\python.exe" (
    if exist .venv rmdir /s /q .venv
    echo Creating the Python environment...
    python -m venv .venv
    if errorlevel 1 goto :fail
)

rem --- dependencies: install them if any is missing
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, httpx, websockets, psutil, dotenv, pystray, PIL" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
)

rem --- settings file
if not exist .env (
    copy .env.example .env >nul
    echo Created the .env file - fill in RIOT_API_KEY, save it, then run this again.
    notepad .env
    exit /b 0
)

rem --- no terminal window: the app lives in the tray icon (bottom right), its messages go to the data folder (noob-helper.log)
start "" ".venv\Scripts\pythonw.exe" -m coach.tray
exit /b 0

:fail
echo.
echo Something went wrong - read the messages above.
pause
exit /b 1
