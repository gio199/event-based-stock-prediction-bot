"""Sentiment parsing and, above all, the failure paths.

The bug this file exists to prevent: a failed Gemini call being promoted to
"real neutral sentiment" and rendered to the user as a genuine 0/100 reading.
"""
import pytest

from standalone_stock_analyzer import GeminiAIClient, NewsAnalyzer, _unavailable_sentiment


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def gemini_payload(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("standalone_stock_analyzer.get_gemini_api_key", lambda: "test-key")
    return GeminiAIClient()


# ------------------------------------------------------- failure paths (C3)

def test_every_failure_path_sets_has_news_false(client, monkeypatch):
    """The invariant that keeps failures out of the score and out of the UI."""
    cases = {
        "http 429": FakeResponse(status_code=429),
        "http 500": FakeResponse(status_code=500),
        "empty candidates": FakeResponse(payload={"candidates": []}),
        "safety-blocked": FakeResponse(payload={"candidates": [{"content": {}}]}),
        "non-json body": FakeResponse(payload=gemini_payload("not json at all")),
    }
    for label, response in cases.items():
        monkeypatch.setattr("standalone_stock_analyzer.gemini_post", lambda *a, **k: response)
        result = client.analyze_sentiment("some news", "AAPL")
        assert result["has_news"] is False, f"{label} must not report has_news"
        assert result["sentiment_score"] == 0


def test_network_exception_sets_has_news_false(client, monkeypatch):
    """This is the exact path that regressed: it returned a dict with no
    has_news key at all, so the caller's setdefault(True) marked a timeout as
    genuine sentiment."""
    def boom(*a, **k):
        raise ConnectionError("network down")

    monkeypatch.setattr("standalone_stock_analyzer.gemini_post", boom)
    result = client.analyze_sentiment("some news", "AAPL")
    assert result["has_news"] is False
    assert result["articles_count"] == 0


def test_missing_api_key_short_circuits(monkeypatch):
    monkeypatch.setattr("standalone_stock_analyzer.get_gemini_api_key", lambda: "")

    def must_not_call(*a, **k):
        raise AssertionError("no HTTP call should be made without a key")

    monkeypatch.setattr("standalone_stock_analyzer.gemini_post", must_not_call)
    result = GeminiAIClient().analyze_sentiment("some news", "AAPL")
    assert result["has_news"] is False
    assert "GEMINI_API_KEY" in result["summary"]


# ------------------------------------------------------------ success path

def test_successful_response_is_parsed(client, monkeypatch):
    body = '{"sentiment_score": 72, "confidence": 88, "market_sentiment": "bullish", "summary": "Strong quarter."}'
    monkeypatch.setattr(
        "standalone_stock_analyzer.gemini_post", lambda *a, **k: FakeResponse(payload=gemini_payload(body))
    )
    result = client.analyze_sentiment("some news", "AAPL")
    assert result["sentiment_score"] == 72
    assert result["confidence"] == 88
    assert result["market_sentiment"] == "bullish"
    assert result["summary"] == "Strong quarter."
    assert "has_news" not in result  # the caller stamps this only on success


def test_out_of_range_values_are_clamped(client, monkeypatch):
    body = '{"sentiment_score": 5000, "confidence": -20, "market_sentiment": "wildly bullish"}'
    monkeypatch.setattr(
        "standalone_stock_analyzer.gemini_post", lambda *a, **k: FakeResponse(payload=gemini_payload(body))
    )
    result = client.analyze_sentiment("some news", "AAPL")
    assert result["sentiment_score"] == 100
    assert result["confidence"] == 0
    assert result["market_sentiment"] == "neutral"  # unrecognised label falls back


def test_json_array_instead_of_object_degrades(client, monkeypatch):
    monkeypatch.setattr(
        "standalone_stock_analyzer.gemini_post", lambda *a, **k: FakeResponse(payload=gemini_payload("[1, 2, 3]"))
    )
    assert client.analyze_sentiment("news", "AAPL")["has_news"] is False


# ------------------------------------------------- NewsAnalyzer integration

class FakeTicker:
    def __init__(self, news):
        self.news = news


def test_no_news_reports_has_news_false(monkeypatch):
    analyzer = NewsAnalyzer()
    monkeypatch.setattr("standalone_stock_analyzer.yf.Ticker", lambda s: FakeTicker([]))
    result = analyzer.get_news_sentiment("AAPL")
    assert result["has_news"] is False
    assert result["articles_count"] == 0


def test_successful_call_is_marked_has_news(monkeypatch):
    analyzer = NewsAnalyzer()
    news = [{"content": {"title": "Up", "summary": "Good"}} for _ in range(3)]
    monkeypatch.setattr("standalone_stock_analyzer.yf.Ticker", lambda s: FakeTicker(news))
    monkeypatch.setattr(
        analyzer.gemini, "analyze_sentiment",
        lambda text, symbol: {"sentiment_score": 40, "confidence": 60,
                              "summary": "ok", "market_sentiment": "bullish"},
    )
    result = analyzer.get_news_sentiment("AAPL")
    assert result["has_news"] is True
    assert result["articles_count"] == 3


def test_failed_call_keeps_article_count_at_zero(monkeypatch):
    """Articles were fetched, but nothing was successfully analyzed - claiming
    "3 articles" next to a zero score misrepresents a failure as a reading."""
    analyzer = NewsAnalyzer()
    news = [{"content": {"title": "Up", "summary": "Good"}} for _ in range(3)]
    monkeypatch.setattr("standalone_stock_analyzer.yf.Ticker", lambda s: FakeTicker(news))
    monkeypatch.setattr(
        analyzer.gemini, "analyze_sentiment",
        lambda text, symbol: _unavailable_sentiment("Sentiment analysis unavailable"),
    )
    result = analyzer.get_news_sentiment("AAPL")
    assert result["has_news"] is False
    assert result["articles_count"] == 0


def test_yfinance_failure_degrades_cleanly(monkeypatch):
    analyzer = NewsAnalyzer()

    def boom(symbol):
        raise RuntimeError("yfinance exploded")

    monkeypatch.setattr("standalone_stock_analyzer.yf.Ticker", boom)
    result = analyzer.get_news_sentiment("AAPL")
    assert result["has_news"] is False
    assert result["sentiment_score"] == 0
