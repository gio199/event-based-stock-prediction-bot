"""Job claiming, state snapshots, and the API surface."""
import threading

import pytest
from fastapi.testclient import TestClient

from web_state import AnalysisState, Settings, start_custom_job, start_watchlist_job


class FakeAnalyzer:
    """Analyzer stand-in: no network, optional blocking for race tests."""

    def __init__(self, gate=None):
        self.gate = gate
        self.seen = []

    def analyze_stock(self, symbol):
        self.seen.append(symbol)
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if symbol == "BAD":
            return {"error": f"Failed to analyze {symbol}"}
        return {"symbol": symbol, "score": len(symbol)}


# ------------------------------------------------------------ job claiming

def test_first_claim_wins_and_second_is_refused():
    state = AnalysisState()
    assert state.try_begin_job("watchlist_refresh", ["AAPL"]) is True
    assert state.try_begin_job("custom_analyze", ["MSFT"]) is False
    state.end_job()
    assert state.try_begin_job("custom_analyze", ["MSFT"]) is True


def test_end_job_clears_running_state():
    state = AnalysisState()
    state.try_begin_job("watchlist_refresh", ["AAPL"])
    assert state.status_snapshot()["job_running"] is True
    state.end_job()
    snapshot = state.status_snapshot()
    assert snapshot["job_running"] is False
    assert snapshot["job_type"] is None


def test_start_job_reports_refusal_rather_than_claiming_success():
    """The bug: the route answered 202 {"started": true} for a job that then
    silently returned without touching state, leaving the UI polling forever."""
    state = AnalysisState()
    gate = threading.Event()
    analyzer = FakeAnalyzer(gate=gate)

    assert start_watchlist_job(state, analyzer, ["AAPL"]) is True
    assert start_custom_job(state, analyzer, ["MSFT"]) is False  # honest refusal
    gate.set()


def test_job_runs_to_completion_and_sorts_by_score():
    state = AnalysisState()
    assert start_custom_job(state, FakeAnalyzer(), ["AA", "BBBB", "CCC"]) is True
    _wait_until(lambda: state.status_snapshot()["job_running"] is False)
    results = state.custom_snapshot()["results"]
    assert [r["symbol"] for r in results] == ["BBBB", "CCC", "AA"]


def test_per_symbol_errors_are_collected_separately():
    state = AnalysisState()
    start_custom_job(state, FakeAnalyzer(), ["AAPL", "BAD"])
    _wait_until(lambda: state.status_snapshot()["job_running"] is False)
    snapshot = state.custom_snapshot()
    assert [r["symbol"] for r in snapshot["results"]] == ["AAPL"]
    assert snapshot["errors"][0]["symbol"] == "BAD"


def test_slot_is_released_even_if_the_job_body_raises():
    """job_running must not stick at True after an unexpected failure."""
    class ExplodingAnalyzer:
        def analyze_stock(self, symbol):
            raise RuntimeError("boom")

    state = AnalysisState()
    start_custom_job(state, ExplodingAnalyzer(), ["AAPL"])
    _wait_until(lambda: state.status_snapshot()["job_running"] is False)
    assert state.try_begin_job("watchlist_refresh", ["AAPL"]) is True


def _wait_until(predicate, timeout=5.0):
    deadline = threading.Event()
    for _ in range(int(timeout * 100)):
        if predicate():
            return
        deadline.wait(0.01)
    raise AssertionError("condition not met in time")


# --------------------------------------------------------------- snapshots

def test_snapshots_return_copies():
    state = AnalysisState()
    state.finish_watchlist_job([{"symbol": "AAPL"}], [])
    snapshot = state.watchlist_snapshot()
    snapshot["results"].append({"symbol": "INJECTED"})
    assert len(state.watchlist_snapshot()["results"]) == 1


def test_watchlist_snapshot_is_internally_consistent():
    state = AnalysisState()
    state.finish_watchlist_job([{"symbol": "AAPL"}], [{"symbol": "BAD", "error": "x"}])
    snapshot = state.watchlist_snapshot()
    assert snapshot["last_updated"] is not None
    assert len(snapshot["results"]) == 1 and len(snapshot["errors"]) == 1


# ---------------------------------------------------------------- settings

def test_interval_is_clamped_to_the_allowed_range():
    settings = Settings()
    settings.update(refresh_interval_seconds=1)
    assert settings.snapshot()[1] == 60
    settings.update(refresh_interval_seconds=10**9)
    assert settings.snapshot()[1] == 24 * 60 * 60


def test_settings_update_is_partial():
    settings = Settings(auto_refresh_enabled=True, refresh_interval_seconds=600)
    settings.update(auto_refresh_enabled=False)
    assert settings.snapshot() == (False, 600)


# -------------------------------------------------------------------- API

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    import web_app

    monkeypatch.setattr(web_app, "analyzer", FakeAnalyzer())
    monkeypatch.setattr(web_app, "state", AnalysisState())
    # Don't start the background threads for API tests.
    monkeypatch.setattr(web_app.scheduler, "start", lambda: None)
    monkeypatch.setattr(web_app.scheduler, "stop", lambda *a, **k: None)
    monkeypatch.setattr(web_app.event_poller, "start", lambda: None)
    monkeypatch.setattr(web_app.event_poller, "stop", lambda *a, **k: None)
    with TestClient(web_app.app) as c:
        yield c


def test_static_index_is_served(client):
    """A relative StaticFiles directory made this fail at import from any
    working directory other than the repo root."""
    assert client.get("/").status_code == 200


@pytest.mark.parametrize("symbols,expected", [
    ([], 400),                      # nothing usable
    (["   "], 400),
    (["AAPL"], 202),
    (["aapl"], 202),                # upper-cased server-side
    (["BRK.B"], 202),               # dotted symbols are legitimate
    (["BF-B"], 202),
    (["../../etc/passwd"], 400),
    (["<script>"], 400),
    (["TOOLONGSYMBOL"], 400),
    (["A" * 11], 400),
])
def test_analyze_symbol_validation(client, symbols, expected):
    assert client.post("/api/analyze", json={"symbols": symbols}).status_code == expected


def test_too_many_symbols_is_rejected(client):
    resp = client.post("/api/analyze", json={"symbols": [f"SYM{i}" for i in range(30)]})
    assert resp.status_code == 400
    assert "Too many symbols" in resp.json()["detail"]


def test_events_limit_bounds(client):
    assert client.get("/api/events?limit=0").status_code == 422
    assert client.get("/api/events?limit=-1").status_code == 422
    assert client.get("/api/events?limit=100").status_code == 200


def test_settings_rejects_out_of_range_interval(client):
    assert client.post("/api/settings", json={"refresh_interval_seconds": 5}).status_code == 400
    assert client.post("/api/settings", json={"refresh_interval_seconds": 10**9}).status_code == 400
    assert client.post("/api/settings", json={"refresh_interval_seconds": 600}).status_code == 200


def test_unknown_event_source_is_404(client):
    assert client.post("/api/events/sources/nope", json={"enabled": True}).status_code == 404


def test_stocks_endpoint_shape(client):
    body = client.get("/api/stocks").json()
    assert set(body) == {"results", "errors", "last_updated"}


# --------------------------------------------------------- raw social posts

def test_posts_endpoint_shape(client):
    body = client.get("/api/posts").json()
    assert set(body) == {"posts", "count"}


def test_posts_accepts_social_sources(client):
    for source in ("trumpstruth", "x_musk"):
        assert client.get(f"/api/posts?source={source}").status_code == 200


def test_posts_rejects_non_social_sources(client):
    """google_news is excluded on volume - 1000+ headlines per check would bury
    the handful of posts this panel exists to surface."""
    resp = client.get("/api/posts?source=google_news")
    assert resp.status_code == 400
    assert "trumpstruth" in resp.json()["detail"]


def test_posts_rejects_unknown_source(client):
    assert client.get("/api/posts?source=nope").status_code == 400


def test_posts_limit_bounds(client):
    assert client.get("/api/posts?limit=0").status_code == 422
    assert client.get("/api/posts?limit=99999").status_code == 422
    assert client.get("/api/posts?limit=10").status_code == 200


def test_status_exposes_the_posts_change_marker(client):
    """The frontend polls this to know when to re-fetch the posts panel."""
    assert "posts_last_detected_at" in client.get("/api/status").json()
