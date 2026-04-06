# Event-based stock prediction bot

Markets do not move only on charts—they move on **headlines, policy shocks, and high-impact posts** that land in real time. This project is a stepping stone toward tooling that treats those moments as first-class inputs alongside technicals.

![Intraday move in major U.S. indices around tariff-related headlines (Yahoo Finance illustration).](./docs/market-event-chart.png)

The chart above (Yahoo Finance) shows how fast narratives can reprice risk: a long stretch of calm can end when a single macro or policy story hits. An **event-aware** workflow tries to pair:

- **What happened** (text of the news or post, timing, asset)
- **How price reacted** (volatility, gaps, trend breaks)
- **What to do next** (signal strength, confidence, optional AI summary of sentiment)

## What this repo contains today

Single entry point: `standalone_stock_analyzer.py` — Yahoo Finance data, technical indicators (RSI, MACD, moving averages, Bollinger Bands), optional **Gemini** news sentiment, and BUY/SELL/HOLD-style signals. Use it as the baseline stack while you add richer event feeds (RSS, social APIs, economic calendars, etc.).

## Setup

```bash
pip install -r requirements.txt
```

Optional: create a `.env` file in this folder with your Gemini key so AI news sentiment runs (technical analysis still works without it):

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

For education and information only, not financial advice. Past reactions to events are not a guarantee of future performance.
