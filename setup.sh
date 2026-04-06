#!/bin/bash
# Automated Setup Script for Standalone Stock Analyzer (Mac/Linux)
# This will install all dependencies automatically

echo "========================================"
echo "Stock Analyzer Setup - Mac/Linux"
echo "========================================"
echo ""
echo "This script will:"
echo "1. Check if Python is installed"
echo "2. Install required packages"
echo "3. Test the analyzer"
echo ""
read -p "Press Enter to continue..."

# Check Python
echo "[1/3] Checking Python installation..."
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

# Install packages
echo "[2/3] Installing required packages..."
echo "This may take a few minutes..."
echo ""
pip3 install --upgrade pip
pip3 install -r requirements.txt
echo ""
echo "[OK] Packages installed!"
echo ""

# Test installation
echo "[3/3] Testing installation..."
echo ""
python3 -c "import yfinance, pandas, numpy, requests; print('[OK] All packages are working!')"
if [ $? -ne 0 ]; then
    echo "[ERROR] Package test failed"
    echo "Please try running: pip3 install -r requirements.txt"
    exit 1
fi

# Make run script executable
chmod +x run_analyzer.sh
echo "[OK] Made run_analyzer.sh executable"

echo ""
echo "========================================"
echo "Setup Complete!"
echo "========================================"
echo ""
echo "You can now run the analyzer:"
echo "  - Run: ./run_analyzer.sh"
echo "  - Or: python3 standalone_stock_analyzer.py"
echo ""
echo "See README.md for details"
echo ""

