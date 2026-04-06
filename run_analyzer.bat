@echo off
REM Windows Batch Script to Run Standalone Stock Analyzer
REM Double-click this file to run the analyzer on Windows

echo ========================================
echo Standalone Stock Analyzer - Windows
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Python found! Checking dependencies...
echo.

REM Check and install dependencies
pip show yfinance >nul 2>&1
if errorlevel 1 (
    echo Installing required packages...
    pip install -r requirements.txt
    echo.
)

REM Run the analyzer
echo Running Stock Analyzer...
echo.
python standalone_stock_analyzer.py

pause

