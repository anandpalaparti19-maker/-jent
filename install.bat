@echo off
setlocal enabledelayedexpansion

title JENT Job Search Agent — Installer

echo.
echo  ========================================================
echo   JENT — Continuous Job Search Agent  v3
echo   Windows Setup Script
echo  ========================================================
echo.

:: ── Check Python ────────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python is not installed or not on PATH.
    echo          Download it from https://python.org/downloads/
    echo          Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('python --version 2^>^&1') do set PY_VER=%%i
echo  [OK] Found %PY_VER%

:: ── Create virtual environment ───────────────────────────────────────────────
echo.
echo  Creating virtual environment (.venv)...
if exist ".venv" (
    echo  [SKIP] .venv already exists.
) else (
    python -m venv .venv
    if errorlevel 1 (
        echo  [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo  [OK] .venv created.
)

:: ── Install dependencies ─────────────────────────────────────────────────────
echo.
echo  Installing dependencies (this may take a moment)...
.venv\Scripts\pip install --upgrade pip --quiet
.venv\Scripts\pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo  [ERROR] pip install failed. Check requirements.txt and your internet connection.
    pause
    exit /b 1
)
echo  [OK] All dependencies installed.

:: ── Copy .env.example ───────────────────────────────────────────────────────
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo  [OK] Copied .env.example to .env — edit it with your settings.
    )
)

:: ── Create log directory ─────────────────────────────────────────────────────
if not exist "log" mkdir log
echo  [OK] log\ directory ready.

:: ── Summary ──────────────────────────────────────────────────────────────────
echo.
echo  ========================================================
echo   Setup complete!
echo  ========================================================
echo.
echo   Next steps:
echo   1. Edit config.yaml with your settings (Gmail, thresholds, etc.)
echo   2. Drop your resume.pdf or resume.docx in this folder
echo   3. Test the agent:
echo        .venv\Scripts\python job_search_agent.py --once --dry-run
echo   4. Run for real:
echo        .venv\Scripts\python job_search_agent.py --once
echo   5. Start the dashboard:
echo        .venv\Scripts\python dashboard\app.py
echo      Then open http://localhost:8765
echo.

:: ── Optionally register Windows Task Scheduler task ─────────────────────────
set /p REGISTER_TASK=  Register a Windows Task Scheduler task to run every 2 hours? [y/N]: 
if /i "!REGISTER_TASK!"=="y" (
    set AGENT_PATH=%~dp0job_search_agent.py
    set PYTHON_PATH=%~dp0.venv\Scripts\python.exe
    set WORKDIR=%~dp0

    :: Remove trailing backslash from WORKDIR
    if "!WORKDIR:~-1!"=="\" set WORKDIR=!WORKDIR:~0,-1!

    schtasks /create ^
        /tn "JENT Job Search Agent" ^
        /tr "\"!PYTHON_PATH!\" \"!AGENT_PATH!\" --once" ^
        /sc HOURLY /mo 2 ^
        /sd %DATE% ^
        /st %TIME:~0,5% ^
        /ru "%USERNAME%" ^
        /f >nul 2>&1

    if errorlevel 1 (
        echo  [WARN] Could not register task — try running this script as Administrator.
    ) else (
        echo  [OK] Task "JENT Job Search Agent" registered (runs every 2 hours).
        echo       Manage it in Task Scheduler (taskschd.msc).
    )
)

echo.
echo  Done! Press any key to exit.
pause >nul
