"""Scoring and target maths for the analyzer core.

These are pure functions over plain numbers - no network, no yfinance - which
is exactly why the two worst bugs in this file's history lived here unnoticed.
"""
import pytest

from standalone_stock_analyzer import (
    StandaloneStockAnalyzer,
    _clamp_int,
    _unavailable_sentiment,
)


@pytest.fixture
def analyzer():
    """Bypass __init__ so no NewsAnalyzer/Gemini client is constructed."""
    return StandaloneStockAnalyzer.__new__(StandaloneStockAnalyzer)


def signal_for(analyzer, **overrides):
    kwargs = dict(
        price=100.0,
        rsi=50.0,
        macd_trend="Bullish",
        bb={"position": 0.5},
        sma_10=100.0,
        sma_20=100.0,
        sma_50=100.0,
        volume_ratio=1.0,
        week_change=0.0,
        month_change=0.0,
        news_sentiment=None,
    )
    kwargs.update(overrides)
    return analyzer._generate_signal(**kwargs)


# --------------------------------------------------------------- MACD (C2)

def test_bullish_macd_adds_points(analyzer):
    score, _, reasons = signal_for(analyzer, macd_trend="Bullish")
    assert score == 20
    assert any("MACD bullish" in r for r in reasons)


def test_bearish_macd_subtracts_points(analyzer):
    score, _, reasons = signal_for(analyzer, macd_trend="Bearish")
    assert score == -20
    assert any("MACD bearish" in r for r in reasons)


def test_neutral_macd_is_not_scored_as_bearish(analyzer):
    """calculate_macd() returns "Neutral" from its exception handler.

    Folding that into the bearish branch meant a *failed* indicator subtracted
    20 points and reported "MACD bearish" as fact - on its own enough to push a
    HOLD across the -25 boundary into SELL.
    """
    score, _, reasons = signal_for(analyzer, macd_trend="Neutral")
    assert score == 0
    assert not any("bearish" in r.lower() for r in reasons)


def test_unknown_macd_value_is_also_neutral(analyzer):
    score, _, _ = signal_for(analyzer, macd_trend="")
    assert score == 0


# ------------------------------------------------------- moving averages (Q2)

def test_full_ma_alignment_scores_strong_uptrend(analyzer):
    score, _, reasons = signal_for(
        analyzer, macd_trend="Neutral", price=110, sma_10=105, sma_20=102, sma_50=100
    )
    assert score == 30
    assert any("Strong uptrend" in r for r in reasons)


def test_missing_sma_50_still_allows_short_term_uptrend(analyzer):
    """sma_50 is None under 50 sessions of history.

    It used to be aliased to sma_20, which made the strict chain
    price > sma_10 > sma_20 > sma_50 unsatisfiable and silently downgraded a
    genuine strong uptrend from +30 to +20 with no indication why. None must
    skip the long-trend comparison rather than fail it.
    """
    score, _, reasons = signal_for(
        analyzer, macd_trend="Neutral", price=110, sma_10=105, sma_20=102, sma_50=None
    )
    assert score == 20
    assert any("Uptrend" in r for r in reasons)


def test_aliasing_sma_50_to_sma_20_would_lose_the_strong_signal(analyzer):
    """Regression guard: the old fallback value, scored explicitly."""
    aliased, _, _ = signal_for(
        analyzer, macd_trend="Neutral", price=110, sma_10=105, sma_20=102, sma_50=102
    )
    correct, _, _ = signal_for(
        analyzer, macd_trend="Neutral", price=110, sma_10=105, sma_20=102, sma_50=100
    )
    assert aliased == 20 and correct == 30


def test_full_downtrend_alignment(analyzer):
    score, _, reasons = signal_for(
        analyzer, macd_trend="Neutral", price=90, sma_10=95, sma_20=98, sma_50=100
    )
    assert score == -30
    assert any("Strong downtrend" in r for r in reasons)


# ------------------------------------------------------------------- RSI

@pytest.mark.parametrize("rsi,expected", [(25, 25), (35, 15), (50, 0), (65, -15), (75, -25)])
def test_rsi_bands(analyzer, rsi, expected):
    score, _, _ = signal_for(analyzer, macd_trend="Neutral", rsi=rsi)
    assert score == expected


# ------------------------------------------------------------- sentiment

def test_failed_sentiment_is_not_scored(analyzer):
    """has_news=False must keep a failed AI call out of the score entirely."""
    score, _, reasons = signal_for(
        analyzer,
        macd_trend="Neutral",
        news_sentiment=_unavailable_sentiment("Sentiment analysis unavailable"),
    )
    assert score == 0
    assert not any("[AI]" in r for r in reasons)


def test_real_sentiment_is_scaled_by_confidence(analyzer):
    score, _, _ = signal_for(
        analyzer,
        macd_trend="Neutral",
        news_sentiment={
            "has_news": True, "sentiment_score": 100, "confidence": 100, "articles_count": 3,
            "summary": "", "market_sentiment": "bullish",
        },
    )
    assert score == 20  # full weight at full confidence

    half, _, _ = signal_for(
        analyzer,
        macd_trend="Neutral",
        news_sentiment={
            "has_news": True, "sentiment_score": 100, "confidence": 50, "articles_count": 3,
            "summary": "", "market_sentiment": "bullish",
        },
    )
    assert half == 10


# --------------------------------------------------------------- thresholds

@pytest.mark.parametrize("case,expected_score,expected_signal", [
    # RSI 25 (+25) + MACD bullish (+20) + full MA alignment (+30)
    (dict(rsi=25, macd_trend="Bullish", price=110, sma_10=105, sma_20=102, sma_50=100), 75, "STRONG BUY"),
    # RSI 35 (+15) + MACD bullish (+20)
    (dict(rsi=35, macd_trend="Bullish"), 35, "BUY"),
    # everything neutral
    (dict(rsi=50, macd_trend="Neutral"), 0, "HOLD"),
    # RSI 75 (-25) + MACD bearish (-20)
    (dict(rsi=75, macd_trend="Bearish"), -45, "SELL"),
    # RSI 75 (-25) + MACD bearish (-20) + full downtrend (-30)
    (dict(rsi=75, macd_trend="Bearish", price=90, sma_10=95, sma_20=98, sma_50=100), -75, "STRONG SELL"),
])
def test_signal_thresholds(analyzer, case, expected_score, expected_signal):
    """Drive real inputs through _generate_signal and check the bucket."""
    score, signal, _ = signal_for(analyzer, **case)
    assert (score, signal) == (expected_score, expected_signal)


# ------------------------------------------------------------------ targets

def test_buy_targets_are_above_price_and_stop_below(analyzer):
    t = analyzer._calculate_targets(100.0, "BUY", {"lower": 95.0, "upper": 106.0}, 90.0, 110.0)
    assert t["stop_loss"] < 100 < t["target_1"] < t["target_2"]
    assert t["risk_reward"] > 0


def test_sell_targets_are_below_price_and_stop_above(analyzer):
    t = analyzer._calculate_targets(100.0, "SELL", {"lower": 94.0, "upper": 105.0}, 90.0, 110.0)
    assert t["target_2"] < t["target_1"] < 100 < t["stop_loss"]


def test_hold_targets_are_not_bearish(analyzer):
    """HOLD is a neutral range, not a directional bet - target_1 must sit above
    the current price rather than reusing the SELL branch's maths."""
    t = analyzer._calculate_targets(100.0, "HOLD", {"lower": 96.0, "upper": 104.0}, 90.0, 110.0)
    assert t["stop_loss"] < 100 < t["target_1"]


def test_zero_risk_does_not_divide_by_zero(analyzer):
    t = analyzer._calculate_targets(100.0, "HOLD", {"lower": 100.0, "upper": 100.0}, 100.0, 100.0)
    assert t["risk_reward"] == 0


# ------------------------------------------------------------- confidence

def test_confidence_is_capped_at_100(analyzer):
    assert analyzer._calculate_confidence(500, 2.0) == 100


def test_confidence_scales_with_volume(analyzer):
    high = analyzer._calculate_confidence(50, 2.0)   # >1.5x average
    normal = analyzer._calculate_confidence(50, 1.0)
    low = analyzer._calculate_confidence(50, 0.1)    # <0.5x average
    assert low < normal < high


# ------------------------------------------------------------------ clamps

@pytest.mark.parametrize("raw,expected", [
    (50, 50), ("50", 50), (50.7, 50), (999, 100), (-999, -100),
    (None, 0), ("abc", 0), ([], 0),
])
def test_clamp_int(raw, expected):
    assert _clamp_int(raw, -100, 100) == expected
