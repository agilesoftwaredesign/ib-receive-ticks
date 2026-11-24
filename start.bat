@echo off
rem Start ib-receive-ticks. Creates venv and installs requirements on first run.
rem All arguments are passed through to main.py, e.g. start.bat --only-new-instrument --days 7
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo venv not found, creating it...
    if exist venv rmdir /s /q venv
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 -m venv venv
    ) else (
        python -m venv venv
    )
    if errorlevel 1 (
        echo Error: could not create venv. Is Python 3.9+ installed and on PATH? 1>&2
        exit /b 1
    )
    venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Error: dependency installation failed, removing venv so the next run retries. 1>&2
        rmdir /s /q venv
        exit /b 1
    )
)

call venv\Scripts\activate.bat
python main.py %*
exit /b %errorlevel%
