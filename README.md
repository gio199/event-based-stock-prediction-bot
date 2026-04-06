# Stock analyzer

Single entry point: `standalone_stock_analyzer.py` — technical indicators (RSI, MACD, moving averages, Bollinger Bands), optional AI news sentiment via Google Gemini, and BUY/SELL/HOLD-style signals using Yahoo Finance data.

## Setup

```bash
pip install -r requirements.txt
```

Optional: create a `.env` file in this folder with your Gemini key so news sentiment runs (technical analysis works without it):

```
GEMINI_API_KEY=your_key_here
```

Get a key from [Google AI Studio](https://aistudio.google.com/app/apikey).

## Run

```bash
python standalone_stock_analyzer.py
```

On Windows you can use `setup.bat` then `run_analyzer.bat`. On Mac/Linux, `setup.sh` then `./run_analyzer.sh`.

## Disclaimer

For education and information only, not financial advice.
