#!/bin/bash
# Shell Script to Run the Stock Analyzer Web UI
# For Mac and Linux users

echo "========================================"
echo "Stock Analyzer Web UI - Mac/Linux"
echo "========================================"
echo ""

if ! command -v python3 &> /dev/null
then
    echo "ERROR: Python 3 is not installed"
    echo "Please install Python 3 from https://www.python.org/downloads/"
    exit 1
fi

echo "Python found! Checking dependencies..."
echo ""

if ! python3 -c "import fastapi" &> /dev/null
then
    echo "Installing required packages..."
    pip3 install -r requirements.txt
    echo ""
fi

echo "Starting web server at http://127.0.0.1:8000 ..."
echo "Press Ctrl+C to stop the server."
echo ""

if command -v xdg-open &> /dev/null; then
    xdg-open http://127.0.0.1:8000 &
elif command -v open &> /dev/null; then
    open http://127.0.0.1:8000 &
fi

python3 -m uvicorn web_app:app --host 127.0.0.1 --port 8000

echo ""
echo "Done!"
