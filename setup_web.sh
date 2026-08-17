#!/bin/bash
# Automated Setup Script for Stock Analyzer Web UI (Mac/Linux)

echo "========================================"
echo "Stock Analyzer Web UI Setup - Mac/Linux"
echo "========================================"
echo ""
echo "This script will:"
echo "1. Check if Python is installed"
echo "2. Install required packages (including the web UI)"
echo "3. Install the Playwright browser (used by the X/Twitter Event Feed source)"
echo "4. Test the installation"
echo ""
read -p "Press Enter to continue..."

echo "[1/4] Checking Python installation..."
if ! command -v python3 &> /dev/null
then
    echo "[ERROR] Python 3 is not installed!"
    echo ""
    echo "Please install Python from: https://www.python.org/downloads/"
    echo "Or use your package manager:"
    echo "  - Mac: brew install python3"
    echo "  - Ubuntu/Debian: sudo apt install python3 python3-pip"
    echo "  - Fedora: sudo dnf install python3 python3-pip"
    echo ""
    exit 1
fi
python3 --version
echo "[OK] Python is installed!"
echo ""

echo "[2/4] Installing required packages..."
echo "This may take a few minutes..."
echo ""
pip3 install --upgrade pip
pip3 install -r requirements.txt
echo ""
echo "[OK] Packages installed!"
echo ""

echo "[3/4] Installing Playwright browser (Chromium)..."
echo "This may take a few minutes on first run..."
echo ""
python3 -m playwright install chromium
echo ""
echo "[OK] Playwright browser installed!"
echo ""

echo "[4/4] Testing installation..."
echo ""
python3 -c "import yfinance, pandas, numpy, requests, fastapi, uvicorn, playwright; print('[OK] All packages (including web UI) are working!')"
if [ $? -ne 0 ]; then
    echo "[ERROR] Package test failed"
    echo "Please try running: pip3 install -r requirements.txt"
    exit 1
fi

chmod +x run_web.sh
echo "[OK] Made run_web.sh executable"

echo ""
echo "========================================"
echo "Setup Complete!"
echo "========================================"
echo ""
echo "You can now start the web interface:"
echo "  - Run: ./run_web.sh"
echo "  - Or: python3 -m uvicorn web_app:app --host 127.0.0.1 --port 8000"
echo ""
echo "To use the X/Twitter Event Feed source, first run once:"
echo "  python3 login_x_bot.py"
echo "(Google News and Truth Social sources work without this step.)"
echo ""
echo "See README.md for details"
echo ""
