#!/bin/bash
# Shell Script to Run Standalone Stock Analyzer
# For Mac and Linux users

echo "========================================"
echo "Standalone Stock Analyzer - Mac/Linux"
echo "========================================"
echo ""

# Check if Python is installed
if ! command -v python3 &> /dev/null
then
    echo "ERROR: Python 3 is not installed"
    echo "Please install Python 3 from https://www.python.org/downloads/"
    exit 1
fi

echo "Python found! Checking dependencies..."
echo ""

# Check and install dependencies
if ! python3 -c "import yfinance" &> /dev/null
then
    echo "Installing required packages..."
    pip3 install -r requirements.txt
    echo ""
fi

# Run the analyzer
echo "Running Stock Analyzer..."
echo ""
python3 standalone_stock_analyzer.py

echo ""
echo "Done!"

