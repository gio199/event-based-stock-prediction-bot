@echo off
REM Automated Setup Script for Stock Analyzer Web UI (Windows)

echo ========================================
echo Stock Analyzer Web UI Setup - Windows
echo ========================================
echo.
echo This script will:
echo 1. Check if Python is installed
echo 2. Install required packages (including the web UI)
echo 3. Install the Playwright browser (used by the X/Twitter Event Feed source)
echo 4. Test the installation
echo.
pause

echo [1/4] Checking Python installation...
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed!
    echo.
    echo Please install Python from: https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation
    echo.
    pause
    exit /b 1
)
python --version
echo [OK] Python is installed!
echo.

echo [2/4] Installing required packages...
echo This may take a few minutes...
echo.
pip install --upgrade pip
pip install -r requirements.txt
echo.
echo [OK] Packages installed!
echo.

echo [3/4] Installing Playwright browser (Chromium)...
echo This may take a few minutes on first run...
echo.
python -m playwright install chromium
echo.
echo [OK] Playwright browser installed!
echo.

echo [4/4] Testing installation...
echo.
python -c "import yfinance, pandas, numpy, requests, fastapi, uvicorn, playwright; print('[OK] All packages (including web UI) are working!')"
if errorlevel 1 (
    echo [ERROR] Package test failed
    echo Please try running: pip install -r requirements.txt
    pause
    exit /b 1
)

echo.
echo ========================================
echo Setup Complete!
echo ========================================
echo.
echo You can now start the web interface:
echo   - Double-click: run_web.bat
echo   - Or run: python -m uvicorn web_app:app --host 127.0.0.1 --port 8000
echo.
echo To use the X/Twitter Event Feed source, first run once:
echo   python login_x_bot.py
echo (Google News and Truth Social sources work without this step.)
echo.
echo See README.md for details
echo.
pause
