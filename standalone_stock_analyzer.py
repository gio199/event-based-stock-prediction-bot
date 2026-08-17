#!/usr/bin/env python3
"""
Standalone Stock Analyzer with AI News Analysis
================================================
A complete stock analysis tool with AI-powered news sentiment analysis.
Install: pip install -r requirements.txt. Optional: GEMINI_API_KEY in `.env` for AI news sentiment.

Usage:
    python standalone_stock_analyzer.py

Features:
- Analyzes top stocks automatically
- Technical analysis (RSI, MACD, Moving Averages, Bollinger Bands)
- AI-powered news sentiment analysis (Gemini AI)
- Clear BUY/SELL/HOLD signals
- Combined technical + sentiment signals
"""

import json
import logging
import sys
import warnings
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Suppress warnings for cleaner output
warnings.filterwarnings('ignore')

# Check and import required packages
try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
    import requests
except ImportError as e:
    print("ERROR: Missing required package!")
    print("\nPlease install required packages:")
    print("  pip install yfinance pandas numpy requests")
    print(f"\nMissing: {e.name}")
    sys.exit(1)

# Imported after the check above so a missing `requests` still produces the
# friendly message rather than a traceback out of app_util.
from app_util import (  # noqa: E402
    configure_logging,
    gemini_post,
    gemini_response_text,
    get_gemini_api_key,
    json_safe,
    load_env_once,
)

load_env_once()

# Library code logs; only main() below prints, since that is the CLI's own
# presentation layer. Analysis runs on background threads under the web app,
# where interleaved bare print() output is unreadable and untimestamped.
logger = logging.getLogger(__name__)


def _unavailable_sentiment(summary: str) -> Dict:
    """The single shape every failed/absent-sentiment path returns.

    `has_news` False is the important part: it is what keeps _generate_signal
    from scoring a failure as a real reading, and what makes the web UI render
    "no news" instead of a confident-looking neutral 0/100. Every early return
    below goes through here so a new failure path cannot forget it again.
    """
    return {
        'sentiment_score': 0,
        'confidence': 0,
        'summary': summary,
        'market_sentiment': 'neutral',
        'has_news': False,
        'articles_count': 0,
    }


class GeminiAIClient:
    """Simple Gemini AI client for sentiment analysis"""

    MODEL = "gemini-flash-latest"

    @property
    def api_key(self) -> str:
        """Read live rather than captured in __init__, so a client constructed
        before .env was loaded still picks the key up."""
        return get_gemini_api_key()

    def analyze_sentiment(self, news_text: str, symbol: str) -> Dict:
        """Analyze sentiment of news articles using Gemini AI.

        Never raises. Every non-success path returns _unavailable_sentiment(),
        so callers can trust that a dict with has_news=True came from a real
        response.
        """
        if not self.api_key:
            return _unavailable_sentiment(
                'Set GEMINI_API_KEY for AI news sentiment (technical signals still work)'
            )

        prompt = f"""
        Analyze the sentiment of the following news articles for {symbol} stock investment purposes.

        News Articles:
        {news_text}

        Respond with a single JSON object shaped exactly as:
        {{"sentiment_score": <int -100 (very negative) to 100 (very positive)>,
          "confidence": <int 0 to 100>,
          "market_sentiment": <"bullish" | "bearish" | "neutral">,
          "summary": <2-3 sentence string describing the key sentiment drivers>}}
        """

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 500,
                "topP": 0.8,
                "topK": 10,
                # Asking for JSON directly replaces a line-prefix text parser
                # that silently returned all-zeros whenever the model wrapped
                # its labels in markdown or reworded them. EventGeminiClient
                # already took this approach; both now match.
                "responseMimeType": "application/json",
            },
        }

        try:
            response = gemini_post(self.MODEL, payload, timeout=30)

            if response.status_code == 429:
                return _unavailable_sentiment(
                    'API quota exceeded - Get free key at: aistudio.google.com/app/apikey'
                )
            if response.status_code != 200:
                logger.warning("Gemini sentiment call for %s returned HTTP %s", symbol, response.status_code)
                return _unavailable_sentiment('AI analysis unavailable')

            text = gemini_response_text(response.json())
            if not text:
                return _unavailable_sentiment('AI analysis unavailable')
            return self._parse_sentiment(text)

        except Exception as e:
            # Network error, malformed JSON, unexpected shape - all degrade to
            # technical-analysis-only rather than propagating.
            logger.warning("Gemini sentiment call for %s failed: %s", symbol, e)
            return _unavailable_sentiment('Sentiment analysis unavailable')

    def _parse_sentiment(self, response: str) -> Dict:
        """Parse the JSON sentiment object, clamped to its documented ranges."""
        try:
            data = json.loads(response)
        except ValueError:
            logger.warning("Gemini sentiment response was not valid JSON")
            return _unavailable_sentiment('AI analysis unavailable')

        if not isinstance(data, dict):
            return _unavailable_sentiment('AI analysis unavailable')

        market_sentiment = str(data.get('market_sentiment', '')).strip().lower()
        if market_sentiment not in ('bullish', 'bearish', 'neutral'):
            market_sentiment = 'neutral'

        return {
            'sentiment_score': _clamp_int(data.get('sentiment_score'), -100, 100),
            'confidence': _clamp_int(data.get('confidence'), 0, 100),
            'summary': str(data.get('summary', '')).strip(),
            'market_sentiment': market_sentiment,
        }


def _clamp_int(value, low: int, high: int) -> int:
    """Coerce a model-supplied number into an int within [low, high]; 0 if unusable."""
    try:
        return max(low, min(high, int(float(value))))
    except (TypeError, ValueError):
        return 0


class NewsAnalyzer:
    """Fetch and analyze news for stocks"""
    
    def __init__(self):
        self.gemini = GeminiAIClient()
        # Search terms mapping for different symbols
        self.search_terms = {
            'AAPL': 'Apple Inc stock',
            'MSFT': 'Microsoft stock',
            'GOOGL': 'Google Alphabet stock',
            'AMZN': 'Amazon stock',
            'NVDA': 'NVIDIA stock',
            'TSLA': 'Tesla stock',
            'META': 'Meta Facebook stock',
            'JPM': 'JPMorgan Chase stock',
            'V': 'Visa stock',
            'WMT': 'Walmart stock',
            'SPY': 'S&P 500 market',
            'QQQ': 'NASDAQ market'
        }
    
    def get_news_sentiment(self, symbol: str) -> Dict:
        """Get news sentiment for a symbol"""
        try:
            # Get company info and news from yfinance
            ticker = yf.Ticker(symbol)
            
            # Try to get news from yfinance
            news_items = []
            try:
                news = ticker.news
                if news:
                    news_items = news[:3]  # Top 3 most recent news items (saves API quota)
            except Exception:
                pass
            
            if not news_items:
                return _unavailable_sentiment('No recent news available')

            # Build news text
            news_text = ""
            for i, item in enumerate(news_items, 1):
                # yfinance nests article fields under 'content' in current versions;
                # fall back to the old flat shape in case an older yfinance is installed.
                fields = item.get('content', item)
                title = fields.get('title', '')
                summary = fields.get('summary', '') or fields.get('description', '')
                news_text += f"{i}. {title}\n{summary}\n\n"

            # Limit text length (shorter for faster AI processing)
            if len(news_text) > 2000:
                news_text = news_text[:2000] + "..."

            sentiment = self.gemini.analyze_sentiment(news_text, symbol)
            # A failed call already carries has_news=False from
            # _unavailable_sentiment(); only a real response lacks the key, so
            # setdefault marks exactly the successful path. This used to be
            # wrong for the network-exception path, which returned a dict with
            # no has_news at all - so a timeout was promoted to "real neutral
            # sentiment", scored as such, and shown in the UI as a genuine
            # 0/100 reading.
            sentiment.setdefault('has_news', True)
            sentiment['articles_count'] = len(news_items) if sentiment['has_news'] else 0
            return sentiment

        except Exception as e:
            logger.warning("Could not fetch news for %s: %s", symbol, e)
            return _unavailable_sentiment('Error fetching news')


class StandaloneStockAnalyzer:
    """Complete stock analyzer that works standalone without external APIs"""
    
    # Popular stocks to analyze (can be customized)
    DEFAULT_STOCKS = [
        'AAPL',   # Apple
        'MSFT',   # Microsoft
        'GOOGL',  # Google
        'AMZN',   # Amazon
        'NVDA',   # NVIDIA
        'TSLA',   # Tesla
        'META',   # Meta (Facebook)
        'JPM',    # JPMorgan
        'V',      # Visa
        'WMT',    # Walmart
        'SPY',    # S&P 500 ETF
        'QQQ',    # NASDAQ ETF
    ]
    
    def __init__(self):
        """Initialize the analyzer"""
        self.results = []
        self.news_analyzer = NewsAnalyzer()
        if self.news_analyzer.gemini.api_key:
            logger.info("AI News Analysis: ENABLED (Gemini). If quota is exceeded, "
                        "get a free key at aistudio.google.com/app/apikey")
        else:
            logger.info("AI News Analysis: DISABLED (no GEMINI_API_KEY set - technical signals "
                        "still work). Get a free key at aistudio.google.com/app/apikey")

    def calculate_rsi(self, prices: pd.Series, period: int = 14) -> float:
        """Calculate Relative Strength Index"""
        try:
            delta = prices.diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
            
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            value = rsi.iloc[-1]
            return value if pd.notna(value) else 50  # Flat window (0/0) -> neutral
        except Exception:
            return 50  # Neutral if calculation fails
    
    def calculate_macd(self, prices: pd.Series) -> Tuple[float, float, str]:
        """Calculate MACD (Moving Average Convergence Divergence)"""
        try:
            exp1 = prices.ewm(span=12, adjust=False).mean()
            exp2 = prices.ewm(span=26, adjust=False).mean()
            macd = exp1 - exp2
            signal = macd.ewm(span=9, adjust=False).mean()
            
            macd_val = macd.iloc[-1]
            signal_val = signal.iloc[-1]
            
            if macd_val > signal_val:
                trend = "Bullish"
            else:
                trend = "Bearish"
                
            return macd_val, signal_val, trend
        except Exception:
            return 0, 0, "Neutral"
    
    def calculate_bollinger_bands(self, prices: pd.Series, period: int = 20) -> Dict:
        """Calculate Bollinger Bands"""
        try:
            sma = prices.rolling(window=period).mean()
            std = prices.rolling(window=period).std()
            
            upper_band = sma + (std * 2)
            lower_band = sma - (std * 2)
            
            current_price = prices.iloc[-1]
            current_sma = sma.iloc[-1]
            current_upper = upper_band.iloc[-1]
            current_lower = lower_band.iloc[-1]
            
            # Calculate position within bands (0 = lower band, 1 = upper band)
            band_width = current_upper - current_lower
            bb_position = (current_price - current_lower) / band_width if band_width > 0 else 0.5

            return {
                'upper': current_upper,
                'middle': current_sma,
                'lower': current_lower,
                'position': bb_position,
                'current': current_price
            }
        except Exception:
            return {'upper': 0, 'middle': 0, 'lower': 0, 'position': 0.5, 'current': 0}
    
    def analyze_stock(self, symbol: str) -> Dict:
        """Perform comprehensive analysis on a single stock"""
        try:
            # Download stock data (90 days for good technical analysis)
            stock = yf.Ticker(symbol)
            df = stock.history(period='3mo')

            # Drop rows with no close price before measuring length: yfinance
            # returns NaN rows for halted/illiquid sessions, and a NaN
            # propagating through the rolling means below reaches the API
            # response, where Starlette's allow_nan=False turns the whole
            # endpoint into a 500 - losing every other symbol's results too.
            if not df.empty:
                df = df.dropna(subset=['Close'])

            if df.empty or len(df) < 20:
                return {'error': f'Insufficient data for {symbol}'}

            # Get current price and basic info
            current_price = df['Close'].iloc[-1]
            prev_close = df['Close'].iloc[-2]
            daily_change = ((current_price - prev_close) / prev_close) * 100

            # Calculate week and month changes. iloc[-6] is 5 trading sessions
            # back, matching the "~5 trading days (1 week)" the glossary
            # advertises; iloc[-5] was one session short. No length guard is
            # needed - the len(df) < 20 check above already covers both.
            week_ago_price = df['Close'].iloc[-6]
            month_ago_price = df['Close'].iloc[-21] if len(df) >= 21 else df['Close'].iloc[0]

            week_change = ((current_price - week_ago_price) / week_ago_price) * 100
            month_change = ((current_price - month_ago_price) / month_ago_price) * 100

            # Calculate moving averages. sma_50 stays None on short history
            # rather than aliasing sma_20: the strict chain in _generate_signal
            # (price > sma_10 > sma_20 > sma_50) can never hold when the last
            # two are the same number, which silently downgraded a genuine
            # strong uptrend from +30 to +20 with no indication why.
            sma_10 = df['Close'].rolling(window=10).mean().iloc[-1]
            sma_20 = df['Close'].rolling(window=20).mean().iloc[-1]
            sma_50 = df['Close'].rolling(window=50).mean().iloc[-1] if len(df) >= 50 else None

            # Technical indicators
            rsi = self.calculate_rsi(df['Close'])
            macd_val, signal_val, macd_trend = self.calculate_macd(df['Close'])
            bb = self.calculate_bollinger_bands(df['Close'])
            
            # Calculate volume analysis
            avg_volume = df['Volume'].mean()
            current_volume = df['Volume'].iloc[-1]
            volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1
            
            # Calculate support and resistance from the 3-month window we fetched
            # (NOT a true 52-week high/low - we only pull period='3mo' of history)
            high_3mo = df['High'].max()
            low_3mo = df['Low'].min()
            
            # Fetch news sentiment (with AI)
            news_sentiment = self.news_analyzer.get_news_sentiment(symbol)
            if news_sentiment.get('has_news'):
                logger.info("%s: AI news sentiment from %d article(s)",
                            symbol, news_sentiment.get('articles_count', 0))
            else:
                logger.info("%s: no AI news sentiment (%s)",
                            symbol, news_sentiment.get('summary', 'no news'))

            # Generate trading score and signal (includes sentiment)
            score, signal, reasons = self._generate_signal(
                current_price, rsi, macd_trend, bb, 
                sma_10, sma_20, sma_50, volume_ratio,
                week_change, month_change, news_sentiment
            )
            
            # Calculate targets and stop loss
            targets = self._calculate_targets(current_price, signal, bb, low_3mo, high_3mo)
            
            # json_safe() is the last line of defence for C6: anything
            # non-finite that still slipped through becomes null rather than a
            # NaN literal that Starlette refuses to serialise.
            return json_safe({
                'symbol': symbol,
                'current_price': round(current_price, 2),
                'daily_change': round(daily_change, 2),
                'week_change': round(week_change, 2),
                'month_change': round(month_change, 2),
                'signal': signal,
                'score': score,
                'confidence': self._calculate_confidence(score, volume_ratio),
                'reasons': reasons,
                'technical': {
                    'rsi': round(rsi, 1),
                    'macd_trend': macd_trend,
                    'sma_10': round(sma_10, 2),
                    'sma_20': round(sma_20, 2),
                    'sma_50': round(sma_50, 2) if sma_50 is not None else None,
                    'bb_position': round(bb['position'], 2),
                    'volume_ratio': round(volume_ratio, 2),
                    '3mo_high': round(high_3mo, 2),
                    '3mo_low': round(low_3mo, 2)
                },
                'sentiment': news_sentiment,
                'targets': targets,
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            })

        except Exception as e:
            logger.warning("Analysis failed for %s: %s", symbol, e)
            return {'error': f'Failed to analyze {symbol}: {str(e)}'}
    
    def _generate_signal(self, price, rsi, macd_trend, bb, sma_10, sma_20,
                        sma_50: Optional[float],
                        volume_ratio, week_change, month_change,
                        news_sentiment: Optional[Dict] = None) -> Tuple[int, str, List[str]]:
        """Generate buy/sell/hold signal based on technical analysis + news sentiment"""
        score = 0
        reasons = []
        
        # RSI Analysis (25 points)
        if rsi < 30:
            score += 25
            reasons.append(f"[+] RSI oversold ({rsi:.1f}) - Strong buy signal")
        elif rsi < 40:
            score += 15
            reasons.append(f"[+] RSI approaching oversold ({rsi:.1f})")
        elif rsi > 70:
            score -= 25
            reasons.append(f"[-] RSI overbought ({rsi:.1f}) - Potential reversal")
        elif rsi > 60:
            score -= 15
            reasons.append(f"[-] RSI approaching overbought ({rsi:.1f})")
        else:
            reasons.append(f"[=] RSI neutral ({rsi:.1f})")
        
        # MACD Analysis (20 points)
        # Three-way on purpose. calculate_macd() returns "Neutral" from its
        # exception handler, and folding that into the bearish `else` meant a
        # *failed* calculation subtracted 20 points and told the user "MACD
        # bearish" as fact - enough on its own to push a HOLD to a SELL.
        # calculate_rsi and calculate_bollinger_bands both degrade to a
        # genuinely neutral, zero-scoring value; MACD now matches them.
        if macd_trend == "Bullish":
            score += 20
            reasons.append("[+] MACD bullish crossover")
        elif macd_trend == "Bearish":
            score -= 20
            reasons.append("[-] MACD bearish")
        else:
            reasons.append("[=] MACD unavailable")

        # Moving Average Analysis (30 points)
        # sma_50 is None when there isn't 50 sessions of history; the
        # long-trend comparisons are skipped rather than compared against a
        # stand-in value that can never satisfy them.
        has_sma_50 = sma_50 is not None
        if has_sma_50 and price > sma_10 > sma_20 > sma_50:
            score += 30
            reasons.append("[+] Strong uptrend - All MAs aligned")
        elif price > sma_10 > sma_20:
            score += 20
            reasons.append("[+] Uptrend - Above short-term MAs")
        elif has_sma_50 and price < sma_10 < sma_20 < sma_50:
            score -= 30
            reasons.append("[-] Strong downtrend - All MAs aligned")
        elif price < sma_10 < sma_20:
            score -= 20
            reasons.append("[-] Downtrend - Below short-term MAs")
        else:
            reasons.append("[=] Mixed trend signals")
        
        # Bollinger Bands Analysis (15 points)
        if bb['position'] < 0.2:
            score += 15
            reasons.append("[+] Near lower Bollinger Band - Potential bounce")
        elif bb['position'] > 0.8:
            score -= 15
            reasons.append("[-] Near upper Bollinger Band - Potential reversal")
        
        # Volume Analysis (10 points)
        if volume_ratio > 1.5:
            score += 10
            reasons.append(f"[+] High volume ({volume_ratio:.1f}x avg) confirms move")
        elif volume_ratio < 0.5:
            score -= 5
            reasons.append(f"[=] Low volume ({volume_ratio:.1f}x avg) - Weak signal")
        
        # Momentum Analysis (15 points)
        if week_change > 5 and month_change > 10:
            score += 15
            reasons.append(f"[+] Strong positive momentum (Week: {week_change:.1f}%, Month: {month_change:.1f}%)")
        elif week_change < -5 and month_change < -10:
            score -= 15
            reasons.append(f"[-] Strong negative momentum (Week: {week_change:.1f}%, Month: {month_change:.1f}%)")
        
        # News Sentiment Analysis (20 points max)
        if news_sentiment and news_sentiment.get('has_news'):
            sentiment_score = news_sentiment.get('sentiment_score', 0)
            sentiment_conf = news_sentiment.get('confidence', 0)
            articles = news_sentiment.get('articles_count', 0)
            
            # Scale sentiment impact based on confidence
            sentiment_impact = (sentiment_score / 100) * 20 * (sentiment_conf / 100)
            score += round(sentiment_impact)
            
            if sentiment_score > 50:
                reasons.append(f"[+] Positive news sentiment ({sentiment_score}/100, {articles} articles)")
            elif sentiment_score < -50:
                reasons.append(f"[-] Negative news sentiment ({sentiment_score}/100, {articles} articles)")
            elif sentiment_score != 0:
                reasons.append(f"[=] Mixed news sentiment ({sentiment_score}/100, {articles} articles)")
            
            # Add AI summary if available
            if news_sentiment.get('summary') and 'No recent news' not in news_sentiment['summary']:
                reasons.append(f"[AI] {news_sentiment['summary'][:80]}")
        
        # Determine signal based on score
        if score >= 50:
            signal = "STRONG BUY"
        elif score >= 25:
            signal = "BUY"
        elif score >= -25:
            signal = "HOLD"
        elif score >= -50:
            signal = "SELL"
        else:
            signal = "STRONG SELL"
        
        return score, signal, reasons
    
    def _calculate_confidence(self, score: int, volume_ratio: float) -> int:
        """Calculate confidence level (0-100)"""
        base_confidence = min(abs(score) * 0.8, 100)
        
        # Adjust for volume
        if volume_ratio > 1.5:
            base_confidence *= 1.1
        elif volume_ratio < 0.5:
            base_confidence *= 0.8
        
        return min(int(base_confidence), 100)
    
    def _calculate_targets(self, current_price: float, signal: str, bb: Dict,
                          low_3mo: float, high_3mo: float) -> Dict:
        """Calculate target prices and stop loss"""
        if 'BUY' in signal:
            stop_loss = max(bb['lower'] * 0.98, current_price * 0.93)
            target_1 = min(bb['upper'] * 0.98, current_price * 1.08)
            target_2 = current_price * 1.15
        elif 'SELL' in signal:
            stop_loss = min(bb['upper'] * 1.02, current_price * 1.07)
            target_1 = max(bb['lower'] * 1.02, current_price * 0.92)
            target_2 = current_price * 0.85
        else:
            # HOLD: neutral trading range, not a directional bet -
            # don't reuse the SELL-oriented (bearish) target math here.
            stop_loss = max(bb['lower'], current_price * 0.95)
            target_1 = min(bb['upper'], current_price * 1.05)
            target_2 = current_price * 1.08

        price_risk = abs(current_price - stop_loss)
        risk_reward = abs(target_1 - current_price) / price_risk if price_risk > 0 else 0

        return {
            'stop_loss': round(stop_loss, 2),
            'target_1': round(target_1, 2),
            'target_2': round(target_2, 2),
            'risk_reward': round(risk_reward, 2)
        }
    
    def analyze_portfolio(self, symbols: List[str] = None) -> List[Dict]:
        """Analyze multiple stocks"""
        if symbols is None:
            symbols = self.DEFAULT_STOCKS
        
        print(f"\n{'='*80}")
        print(f"STANDALONE STOCK ANALYZER - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*80}")
        print(f"Analyzing {len(symbols)} stocks...\n")
        
        results = []
        for i, symbol in enumerate(symbols, 1):
            print(f"[{i}/{len(symbols)}] Analyzing {symbol}...", end=' ')
            result = self.analyze_stock(symbol)
            
            if 'error' not in result:
                results.append(result)
                print(f"[OK] {result['signal']}")
            else:
                print(f"[ERROR] {result['error']}")
        
        self.results = results
        return results
    
    def print_summary(self, results: List[Dict] = None):
        """Print formatted analysis summary"""
        if results is None:
            results = self.results
        
        if not results:
            print("\nNo results to display.")
            return
        
        # Sort by score (best opportunities first)
        results.sort(key=lambda x: x['score'], reverse=True)
        
        print(f"\n{'='*80}")
        print(f"ANALYSIS SUMMARY - Top Opportunities")
        print(f"{'='*80}\n")
        
        # Summary statistics
        buy_signals = [r for r in results if 'BUY' in r['signal']]
        sell_signals = [r for r in results if 'SELL' in r['signal']]
        hold_signals = [r for r in results if r['signal'] == 'HOLD']
        
        print(f"[*] Signal Distribution:")
        print(f"   BUY signals:  {len(buy_signals)} stocks")
        print(f"   SELL signals: {len(sell_signals)} stocks")
        print(f"   HOLD signals: {len(hold_signals)} stocks\n")
        
        # Top opportunities
        print(f"{'='*80}")
        print(f"[*] TOP BUY OPPORTUNITIES")
        print(f"{'='*80}\n")
        
        top_buys = [r for r in results if 'BUY' in r['signal']][:5]
        
        if top_buys:
            for i, result in enumerate(top_buys, 1):
                self._print_stock_detail(result, i)
        else:
            print("No strong buy signals found at this time.\n")
        
        # Show all results in table format
        print(f"\n{'='*80}")
        print(f"[*] COMPLETE ANALYSIS")
        print(f"{'='*80}\n")
        
        # Header
        print(f"{'Symbol':<8} {'Price':<10} {'Signal':<15} {'Score':<8} {'Conf':<6} {'RSI':<6} {'Trend':<10}")
        print(f"{'-'*80}")
        
        for result in results:
            symbol = result['symbol']
            price = f"${result['current_price']:.2f}"
            signal = result['signal']
            score = f"{result['score']}"
            confidence = f"{result['confidence']}%"
            rsi = f"{result['technical']['rsi']:.0f}"
            trend = result['technical']['macd_trend']
            
            # Color code signal
            if 'BUY' in signal:
                signal_display = f"[BUY] {signal}"
            elif 'SELL' in signal:
                signal_display = f"[SELL] {signal}"
            else:
                signal_display = f"[HOLD] {signal}"
            
            print(f"{symbol:<8} {price:<10} {signal_display:<15} {score:<8} {confidence:<6} {rsi:<6} {trend:<10}")
        
        print(f"\n{'='*80}\n")
    
    def _print_stock_detail(self, result: Dict, rank: int = None):
        """Print detailed analysis for a single stock"""
        prefix = f"#{rank} " if rank else ""
        
        print(f"{prefix}{'-'*75}")
        print(f"[STOCK] {result['symbol']} - ${result['current_price']:.2f}")
        print(f"{'-'*75}")
        
        # Signal
        signal_prefix = "[BUY]" if 'BUY' in result['signal'] else "[SELL]" if 'SELL' in result['signal'] else "[HOLD]"
        print(f"{signal_prefix} Signal: {result['signal']} (Score: {result['score']}, Confidence: {result['confidence']}%)")
        
        # Performance
        print(f"\n[*] Performance:")
        print(f"   Daily:  {result['daily_change']:+.2f}%")
        print(f"   Weekly: {result['week_change']:+.2f}%")
        print(f"   Monthly: {result['month_change']:+.2f}%")
        
        # Technical Indicators
        tech = result['technical']
        print(f"\n[*] Technical Indicators:")
        print(f"   RSI: {tech['rsi']:.1f}")
        print(f"   MACD: {tech['macd_trend']}")
        print(f"   SMA-10: ${tech['sma_10']:.2f}")
        print(f"   SMA-20: ${tech['sma_20']:.2f}")
        # None when the symbol has under 50 sessions of history
        sma_50 = tech['sma_50']
        print(f"   SMA-50: ${sma_50:.2f}" if sma_50 is not None else "   SMA-50: n/a (under 50 sessions)")
        print(f"   Volume: {tech['volume_ratio']:.2f}x average")
        print(f"   3-Month Range: ${tech['3mo_low']:.2f} - ${tech['3mo_high']:.2f}")
        
        # News Sentiment (if available)
        if 'sentiment' in result and result['sentiment'].get('has_news'):
            sent = result['sentiment']
            print(f"\n[*] AI News Sentiment:")
            print(f"   Sentiment Score: {sent['sentiment_score']}/100")
            print(f"   Market Mood: {sent['market_sentiment'].upper()}")
            print(f"   Confidence: {sent['confidence']}%")
            print(f"   Articles: {sent['articles_count']}")
            if sent.get('summary') and 'No recent news' not in sent['summary']:
                print(f"   Summary: {sent['summary'][:100]}...")
        elif 'sentiment' in result:
            print(f"\n[*] AI News Sentiment:")
            print(f"   No recent news articles available")
        
        # Targets
        targets = result['targets']
        print(f"\n[*] Price Targets:")
        print(f"   Stop Loss: ${targets['stop_loss']:.2f}")
        print(f"   Target 1:  ${targets['target_1']:.2f}")
        print(f"   Target 2:  ${targets['target_2']:.2f}")
        print(f"   Risk/Reward: {targets['risk_reward']:.2f}:1")
        
        # Reasons
        print(f"\n[*] Key Factors:")
        for reason in result['reasons'][:5]:  # Top 5 reasons
            print(f"   {reason}")
        
        print()
    
    def export_to_csv(self, filename: str = None):
        """Export results to CSV file"""
        if not self.results:
            print("No results to export.")
            return
        
        if filename is None:
            filename = f"stock_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        try:
            df_data = []
            for r in self.results:
                df_data.append({
                    'Symbol': r['symbol'],
                    'Price': r['current_price'],
                    'Signal': r['signal'],
                    'Score': r['score'],
                    'Confidence': r['confidence'],
                    'Daily_Change': r['daily_change'],
                    'Week_Change': r['week_change'],
                    'Month_Change': r['month_change'],
                    'RSI': r['technical']['rsi'],
                    'MACD_Trend': r['technical']['macd_trend'],
                    'Volume_Ratio': r['technical']['volume_ratio'],
                    'Stop_Loss': r['targets']['stop_loss'],
                    'Target_1': r['targets']['target_1'],
                    'Target_2': r['targets']['target_2'],
                    'Risk_Reward': r['targets']['risk_reward']
                })
            
            df = pd.DataFrame(df_data)
            df.to_csv(filename, index=False)
            print(f"[OK] Results exported to: {filename}")
            
        except Exception as e:
            print(f"Error exporting to CSV: {e}")


def main():
    """Main function to run the analyzer"""
    configure_logging()  # surface the library-level logger on the console
    print("\n" + "="*80)
    print("STANDALONE STOCK ANALYZER")
    print("="*80)
    print("This tool analyzes stocks using free data from Yahoo Finance.")
    print("No API keys or additional setup required!")
    print("="*80 + "\n")
    
    # Initialize analyzer
    analyzer = StandaloneStockAnalyzer()
    
    # Option to customize stock list
    print("Options:")
    print("1. Analyze default stocks (AAPL, MSFT, GOOGL, etc.)")
    print("2. Enter custom stock symbols")
    
    try:
        choice = input("\nEnter choice (1 or 2) [default: 1]: ").strip()
        
        if choice == '2':
            symbols_input = input("Enter stock symbols (comma-separated, e.g., AAPL,TSLA,NVDA): ").strip()
            symbols = [s.strip().upper() for s in symbols_input.split(',') if s.strip()]
            
            if not symbols:
                print("No valid symbols entered. Using defaults.")
                symbols = None
        else:
            symbols = None
            
    except (KeyboardInterrupt, EOFError):
        print("\n\nUsing default stock list...")
        symbols = None
    
    # Analyze stocks
    results = analyzer.analyze_portfolio(symbols)
    
    # Print summary
    analyzer.print_summary(results)
    
    # Option to export
    if results:
        try:
            export = input("Export results to CSV? (y/n) [default: n]: ").strip().lower()
            if export == 'y':
                analyzer.export_to_csv()
        except (KeyboardInterrupt, EOFError):
            print("\n")
    
    print("\n[OK] Analysis complete!")
    print("\nDISCLAIMER: This is for educational purposes only.")
    print("Not financial advice. Always do your own research before investing.\n")


if __name__ == "__main__":
    main()

