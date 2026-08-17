@echo off
REM Windows Batch Script to Run the Stock Analyzer Web UI
REM Double-click this file to start the local web server

echo ========================================
echo Stock Analyzer Web UI - Windows
echo ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Python found! Checking dependencies...
echo.

pip show fastapi >nul 2>&1
if errorlevel 1 (
    echo Installing required packages...
    pip install -r requirements.txt
    echo.
)

echo Starting web server at http://127.0.0.1:8000 ...
echo Press Ctrl+C to stop the server.
echo.
start http://127.0.0.1:8000
python -m uvicorn web_app:app --host 127.0.0.1 --port 8000

pause
