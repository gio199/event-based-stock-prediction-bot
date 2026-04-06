@echo off
REM Automated Setup Script for Standalone Stock Analyzer (Windows)
REM This will install all dependencies automatically

echo ========================================
echo Stock Analyzer Setup - Windows
echo ========================================
echo.
echo This script will:
echo 1. Check if Python is installed
echo 2. Install required packages
echo 3. Test the analyzer
echo.
pause

REM Check Python
echo [1/3] Checking Python installation...
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

REM Install packages
echo [2/3] Installing required packages...
echo This may take a few minutes...
echo.
pip install --upgrade pip
pip install -r requirements.txt
echo.
echo [OK] Packages installed!
echo.

REM Test installation
echo [3/3] Testing installation...
echo.
python -c "import yfinance, pandas, numpy, requests; print('[OK] All packages are working!')"
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
echo You can now run the analyzer:
echo   - Double-click: run_analyzer.bat
echo   - Or run: python standalone_stock_analyzer.py
echo.
echo See README.md for details
echo.
pause

