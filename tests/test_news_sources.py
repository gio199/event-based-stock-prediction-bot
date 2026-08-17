"""RSS parsing, ticker resolution caching, and batched event extraction."""
import pytest

import news_sources
from news_sources import (
    EventGeminiClient,
    GoogleNewsSource,
    _parse_rss_items,
    _strip_html,
    resolve_ticker,
)

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Apple beats earnings</title>
    <link>https://example.com/a</link>
    <guid>guid-a</guid>
    <pubDate>Mon, 05 Jan 2026 10:00:00 GMT</pubDate>
    <description>&lt;p&gt;Strong &lt;b&gt;quarter&lt;/b&gt;&lt;/p&gt;</description>
  </item>
  <item>
    <title>No guid here</title>
    <link>https://example.com/b</link>
    <pubDate>garbage date</pubDate>
    <description>Body</description>
  </item>
  <item>
    <title></title>
    <guid>guid-c</guid>
  </item>
</channel></rss>"""


# ------------------------------------------------------------ rss parsing

def test_parses_items_and_strips_html():
    items = _parse_rss_items(RSS, "google_news", query="apple")
    assert len(items) == 2  # the titleless third item is dropped
    assert items[0].external_id == "guid-a"
    assert items[0].text == "Strong quarter"
    assert items[0].query == "apple"


def test_missing_guid_falls_back_to_link():
    items = _parse_rss_items(RSS, "google_news")
    assert items[1].external_id == "https://example.com/b"


def test_unparseable_date_falls_back_to_now():
    items = _parse_rss_items(RSS, "google_news")
    assert items[1].published_at  # populated rather than blank or raising


def test_malformed_xml_raises_for_the_poller_to_catch():
    with pytest.raises(Exception):
        _parse_rss_items(b"<not-xml", "google_news")


@pytest.mark.parametrize("raw,expected", [
    ("<p>hi</p>", "hi"), ("a   b", "a b"), ("", ""), (None, ""),
])
def test_strip_html(raw, expected):
    assert _strip_html(raw) == expected


# --------------------------------------------------- google news fetching

class FakeResponse:
    def __init__(self, content=RSS, status=200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code != 200:
            raise news_sources.requests.RequestException(f"HTTP {self.status_code}")


def test_seen_ids_are_filtered_out(monkeypatch):
    monkeypatch.setattr(news_sources.requests, "get", lambda *a, **k: FakeResponse())
    monkeypatch.setattr(GoogleNewsSource, "QUERY_DELAY_SECONDS", 0)
    source = GoogleNewsSource(extra_queries_provider=lambda: ["only-one-query"])
    source._search_terms = {}
    assert [i.external_id for i in source.fetch_new_items(set())] == ["guid-a", "https://example.com/b"]
    assert source.fetch_new_items({"guid-a"})[0].external_id == "https://example.com/b"


def test_all_queries_failing_raises(monkeypatch):
    def boom(*a, **k):
        raise news_sources.requests.RequestException("network down")

    monkeypatch.setattr(news_sources.requests, "get", boom)
    monkeypatch.setattr(GoogleNewsSource, "QUERY_DELAY_SECONDS", 0)
    source = GoogleNewsSource(extra_queries_provider=lambda: ["q1", "q2"])
    source._search_terms = {}
    with pytest.raises(RuntimeError, match="all 2 Google News queries failed"):
        source.fetch_new_items(set())


def test_partial_failure_still_returns_items(monkeypatch):
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise news_sources.requests.RequestException("timeout")
        return FakeResponse()

    monkeypatch.setattr(news_sources.requests, "get", flaky)
    monkeypatch.setattr(GoogleNewsSource, "QUERY_DELAY_SECONDS", 0)
    source = GoogleNewsSource(extra_queries_provider=lambda: ["q1", "q2"])
    source._search_terms = {}
    assert len(source.fetch_new_items(set())) == 2


def test_should_stop_interrupts_the_query_loop(monkeypatch):
    """A dozen queries at 15s each can hold the poller thread for minutes;
    shutdown must not have to wait that out."""
    calls = {"n": 0}

    def counted(*a, **k):
        calls["n"] += 1
        return FakeResponse()

    monkeypatch.setattr(news_sources.requests, "get", counted)
    monkeypatch.setattr(GoogleNewsSource, "QUERY_DELAY_SECONDS", 0)
    source = GoogleNewsSource(extra_queries_provider=lambda: ["q1", "q2", "q3", "q4"])
    source._search_terms = {}
    source.fetch_new_items(set(), should_stop=lambda: calls["n"] >= 2)
    assert calls["n"] == 2  # stopped early rather than running all four


def test_stopping_before_any_query_does_not_raise(monkeypatch):
    """Zero attempts is a clean interruption, not an all-queries-failed error."""
    monkeypatch.setattr(news_sources.requests, "get", lambda *a, **k: FakeResponse())
    source = GoogleNewsSource(extra_queries_provider=lambda: ["q1"])
    source._search_terms = {}
    assert source.fetch_new_items(set(), should_stop=lambda: True) == []


# -------------------------------------------------- fetch vs. filter split

def test_fetch_items_ignores_seen_state(monkeypatch):
    """The posts panel needs the whole current feed; a post that was already
    analyzed is still one of the latest posts."""
    monkeypatch.setattr(news_sources.requests, "get", lambda *a, **k: FakeResponse())
    monkeypatch.setattr(GoogleNewsSource, "QUERY_DELAY_SECONDS", 0)
    source = GoogleNewsSource(extra_queries_provider=lambda: ["q"])
    source._search_terms = {}
    assert len(source.fetch_items()) == 2  # unfiltered


def test_filter_unseen_narrows_to_new_items():
    source = news_sources.TrumpsTruthSource()
    items = _parse_rss_items(RSS, "trumpstruth")
    assert len(source.filter_unseen(items, set())) == 2
    assert len(source.filter_unseen(items, {"guid-a"})) == 1
    assert source.filter_unseen(items, {"guid-a", "https://example.com/b"}) == []


def test_fetch_new_items_still_composes_both(monkeypatch):
    """The convenience path the event pipeline uses must stay equivalent."""
    monkeypatch.setattr(news_sources.requests, "get", lambda *a, **k: FakeResponse())
    source = news_sources.TrumpsTruthSource()
    assert len(source.fetch_new_items(set())) == 2
    assert len(source.fetch_new_items({"guid-a"})) == 1


def x_item(tweet_id):
    return news_sources.RawItem(source="x_musk", external_id=tweet_id, published_at="",
                                title="@elonmusk on X", text="hi", link="https://x.com/x")


def test_x_musk_filters_by_watermark():
    source = news_sources.XMuskSource()
    items = [x_item("100"), x_item("200"), x_item("300")]
    assert len(source.filter_unseen(items, {"last_seen_tweet_id": None})) == 3
    kept = source.filter_unseen(items, {"last_seen_tweet_id": "150"})
    assert [i.external_id for i in kept] == ["200", "300"]


def test_x_musk_corrupt_watermark_is_ignored_not_fatal():
    source = news_sources.XMuskSource()
    items = [x_item("100")]
    assert len(source.filter_unseen(items, {"last_seen_tweet_id": "garbage"})) == 1


# ------------------------------------------------------- ticker resolution

def test_transient_failure_is_not_cached(monkeypatch):
    """A network blip used to poison the cache entry permanently, marking every
    future event about that company "unverified" for the process lifetime."""
    resolve_ticker.__wrapped__ if hasattr(resolve_ticker, "__wrapped__") else None
    news_sources._resolve_ticker_cached.cache_clear()
    calls = {"n": 0}

    class FakeSearch:
        def __init__(self, query, max_results=5):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("network down")
            self.quotes = [{"symbol": "AAPL", "quoteType": "EQUITY", "longname": "Apple Inc."}]

    monkeypatch.setattr(news_sources, "yfinance", None, raising=False)
    import yfinance
    monkeypatch.setattr(yfinance, "Search", FakeSearch)

    assert resolve_ticker("AAPL", "Apple") is None       # transient failure
    assert resolve_ticker("AAPL", "Apple") == {          # retried, not cached
        "symbol": "AAPL", "company_name": "Apple Inc."
    }


def test_genuine_no_match_is_cached(monkeypatch):
    news_sources._resolve_ticker_cached.cache_clear()
    calls = {"n": 0}

    class EmptySearch:
        def __init__(self, query, max_results=5):
            calls["n"] += 1
            self.quotes = []

    import yfinance
    monkeypatch.setattr(yfinance, "Search", EmptySearch)

    assert resolve_ticker(None, "Nonexistent Corp") is None
    assert resolve_ticker(None, "Nonexistent Corp") is None
    assert calls["n"] == 1  # second call served from cache


def test_empty_guesses_resolve_to_none():
    assert resolve_ticker(None, None) is None
    assert resolve_ticker("", "  ") is None


def test_exact_ticker_match_wins_over_top_result(monkeypatch):
    news_sources._resolve_ticker_cached.cache_clear()

    class MultiSearch:
        def __init__(self, query, max_results=5):
            self.quotes = [
                {"symbol": "APLE", "quoteType": "EQUITY", "shortname": "Apple Hospitality"},
                {"symbol": "AAPL", "quoteType": "EQUITY", "longname": "Apple Inc."},
            ]

    import yfinance
    monkeypatch.setattr(yfinance, "Search", MultiSearch)
    assert resolve_ticker("AAPL", "Apple")["symbol"] == "AAPL"


# ------------------------------------------------------ batched extraction

def gemini_payload(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


class FakeGeminiResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def make_items(n):
    return [
        news_sources.RawItem(source="google_news", external_id=str(i), published_at="",
                             title=f"t{i}", text="body", link="https://example.com")
        for i in range(n)
    ]


@pytest.fixture
def event_client(monkeypatch):
    monkeypatch.setattr(news_sources, "get_gemini_api_key", lambda: "test-key")
    return EventGeminiClient()


def test_result_count_always_matches_input_count(event_client, monkeypatch):
    body = '[{"item_index": 0, "companies": [{"company_guess": "Apple"}]}]'
    monkeypatch.setattr(news_sources, "gemini_post",
                        lambda *a, **k: FakeGeminiResponse(gemini_payload(body)))
    items = make_items(5)
    results = event_client.analyze_events_batch(items)
    assert len(results) == 5
    assert results[0]["companies"][0]["company_guess"] == "Apple"
    assert results[3]["companies"] == []  # absent index -> empty, not missing


def test_item_index_maps_to_the_right_item(event_client, monkeypatch):
    body = '[{"item_index": 2, "companies": [{"company_guess": "Third"}]}]'
    monkeypatch.setattr(news_sources, "gemini_post",
                        lambda *a, **k: FakeGeminiResponse(gemini_payload(body)))
    results = event_client.analyze_events_batch(make_items(3))
    assert results[2]["companies"][0]["company_guess"] == "Third"
    assert results[0]["companies"] == []


def test_no_api_key_marks_everything_failed(monkeypatch):
    monkeypatch.setattr(news_sources, "get_gemini_api_key", lambda: "")
    results = EventGeminiClient().analyze_events_batch(make_items(3))
    assert all(r["failed"] for r in results)
    assert results[0]["reason"] == "no GEMINI_API_KEY"


@pytest.mark.parametrize("payload,status", [
    (gemini_payload("not json"), 200),
    (gemini_payload('{"not": "a list"}'), 200),
    ({"candidates": []}, 200),
    ({}, 429),
    ({}, 500),
])
def test_bad_responses_mark_the_chunk_failed(event_client, monkeypatch, payload, status):
    monkeypatch.setattr(news_sources, "gemini_post",
                        lambda *a, **k: FakeGeminiResponse(payload, status))
    results = event_client.analyze_events_batch(make_items(3))
    assert all(r["failed"] for r in results)
    assert len(results) == 3


def test_empty_input_returns_empty(event_client):
    assert event_client.analyze_events_batch([]) == []


def test_batch_size_shrinks_after_a_full_chunk_failure(event_client, monkeypatch):
    monkeypatch.setattr(news_sources, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))
    monkeypatch.setattr(news_sources, "gemini_post",
                        lambda *a, **k: FakeGeminiResponse({}, 500))
    before = event_client._batch_size
    event_client.analyze_events_batch(make_items(before + 1))
    assert event_client._batch_size < before
    assert event_client._batch_size >= EventGeminiClient.MIN_BATCH_SIZE
