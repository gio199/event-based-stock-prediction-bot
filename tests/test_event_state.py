"""Dedup, persistence, and the analysis-backoff scheduling.

The seen-store is where a sizing mistake once made 702 items reappear as new,
and where losing state silently discards a source's whole backlog.
"""
import json
from datetime import datetime, timedelta

import pytest

import app_util
import event_state
from event_state import (
    EventPoller,
    EventSettings,
    EventState,
    _SeenStore,
    _dedup_by_external_id,
    load_pending,
    save_pending,
)
from news_sources import RawItem


@pytest.fixture(autouse=True)
def isolated_state_files(tmp_path, monkeypatch):
    """Point every state file at a temp dir so tests never touch real state."""
    monkeypatch.setattr(event_state, "EVENT_SEEN_FILE", str(tmp_path / "seen.json"))
    monkeypatch.setattr(event_state, "EVENT_PENDING_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setattr(event_state, "EVENT_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(event_state, "SOCIAL_POSTS_FILE", str(tmp_path / "posts.json"))
    return tmp_path


def post(pid="p1", source="trumpstruth", published="2026-01-05T10:00:00+00:00", text="hello"):
    return {
        "id": pid, "source": source, "title": "t", "text": text,
        "link": "https://example.com", "published_at": published,
        "detected_at": "2026-01-05T10:00:05",
    }


def item(source="google_news", ext_id="1", title="t"):
    return RawItem(source=source, external_id=ext_id, published_at="2026-01-01T00:00:00",
                   title=title, text="body", link="https://example.com")


# ------------------------------------------------------------- seen store

def test_new_ids_are_recorded_and_persisted(isolated_state_files):
    store = _SeenStore()
    store.mark_seen([item(ext_id="a"), item(ext_id="b")])
    assert store.get("google_news") == {"a", "b"}
    assert _SeenStore().get("google_news") == {"a", "b"}  # survives a reload


def test_is_seeded_flips_after_first_items(isolated_state_files):
    store = _SeenStore()
    assert store.is_seeded("google_news") is False
    store.mark_seen([item(ext_id="a")])
    assert store.is_seeded("google_news") is True


def test_ring_eviction_keeps_the_newest(isolated_state_files, monkeypatch):
    monkeypatch.setattr(event_state, "SEEN_RING_SIZE", 3)
    store = _SeenStore()
    store._rings["google_news"] = __import__("collections").deque(maxlen=3)
    for i in "abcde":
        store.mark_seen([item(ext_id=i)])
    seen = store.get("google_news")
    assert seen == {"c", "d", "e"}
    assert "a" not in seen  # evicted, and would be re-reported as new


def test_x_musk_uses_a_monotonic_watermark(isolated_state_files):
    store = _SeenStore()
    store.mark_seen([item(source="x_musk", ext_id="100")])
    assert store.get("x_musk")["last_seen_tweet_id"] == "100"
    store.mark_seen([item(source="x_musk", ext_id="50")])  # older id must not lower it
    assert store.get("x_musk")["last_seen_tweet_id"] == "100"
    store.mark_seen([item(source="x_musk", ext_id="200")])
    assert store.get("x_musk")["last_seen_tweet_id"] == "200"


def test_non_numeric_tweet_id_is_skipped(isolated_state_files):
    store = _SeenStore()
    store.mark_seen([item(source="x_musk", ext_id="not-a-number")])
    assert store.get("x_musk")["last_seen_tweet_id"] is None


def test_corrupt_seen_file_does_not_raise(isolated_state_files):
    (isolated_state_files / "seen.json").write_text("{ this is not json")
    store = _SeenStore()
    assert store.get("google_news") == set()


# ----------------------------------------------------------------- dedup

def test_dedup_is_per_source():
    items = [item(ext_id="1"), item(ext_id="1"), item(source="trumpstruth", ext_id="1")]
    assert len(_dedup_by_external_id(items)) == 2


def test_dedup_keeps_first_occurrence():
    result = _dedup_by_external_id([item(ext_id="1", title="first"), item(ext_id="1", title="second")])
    assert result[0].title == "first"


# --------------------------------------------------------------- pending

def test_pending_roundtrip(isolated_state_files):
    save_pending([item(ext_id="1"), item(ext_id="2")])
    loaded = load_pending()
    assert [i.external_id for i in loaded] == ["1", "2"]


def test_pending_truncation_keeps_newest_and_warns(isolated_state_files, monkeypatch, caplog):
    """Trimmed items are already marked seen, so they are gone for good -
    the drop must at least be visible in the log."""
    monkeypatch.setattr(event_state, "MAX_PENDING", 3)
    save_pending([item(ext_id=str(i)) for i in range(6)])
    assert [i.external_id for i in load_pending()] == ["3", "4", "5"]
    assert "dropping 3" in caplog.text.lower()


def test_one_malformed_pending_entry_does_not_discard_the_rest(isolated_state_files):
    """A single bad record used to wipe the whole queue."""
    path = isolated_state_files / "pending.json"
    good = item(ext_id="1").__dict__
    path.write_text(json.dumps([good, {"unexpected_field": True}]))
    loaded = load_pending()
    assert [i.external_id for i in loaded] == ["1"]


def test_pending_file_that_is_not_a_list(isolated_state_files):
    (isolated_state_files / "pending.json").write_text('{"nope": 1}')
    assert load_pending() == []


# ------------------------------------------------------------ event state

def test_events_are_pruned_by_age_and_count():
    state = EventState()
    old = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    fresh = datetime.now().isoformat(timespec="seconds")
    state.add_events([{"detected_at": fresh, "source": "google_news", "id": "new"}])
    state.add_events([{"detected_at": old, "source": "google_news", "id": "old"}])
    ids = [e["id"] for e in state.events_snapshot()]
    assert "new" in ids and "old" not in ids


def test_events_snapshot_filters():
    state = EventState()
    state.add_events([
        {"detected_at": datetime.now().isoformat(), "source": "google_news", "ticker": "AAPL", "id": "1"},
        {"detected_at": datetime.now().isoformat(), "source": "trumpstruth", "ticker": "TSLA", "id": "2"},
    ])
    assert len(state.events_snapshot(source="google_news")) == 1
    assert len(state.events_snapshot(ticker="tsla")) == 1  # case-insensitive
    assert len(state.events_snapshot(limit=1)) == 1


def test_health_records_failures_then_clears_on_success():
    state = EventState()
    state.record_check_failure("google_news", "boom")
    state.record_check_failure("google_news", "boom again")
    assert state.source_status("google_news")["consecutive_failures"] == 2
    state.record_check_success("google_news", 5)
    assert state.source_status("google_news")["consecutive_failures"] == 0
    assert state.source_status("google_news")["last_new_items_count"] == 5


def test_source_status_returns_a_copy():
    state = EventState()
    snapshot = state.source_status("google_news")
    snapshot["consecutive_failures"] = 999
    assert state.source_status("google_news")["consecutive_failures"] == 0


# ----------------------------------------------------------- social posts

def test_posts_are_stored_newest_published_first():
    state = EventState()
    state.add_posts([
        post("old", published="2026-01-01T10:00:00+00:00"),
        post("new", published="2026-01-09T10:00:00+00:00"),
        post("mid", published="2026-01-05T10:00:00+00:00"),
    ])
    assert [p["id"] for p in state.posts_snapshot()] == ["new", "mid", "old"]


def test_mixed_timezone_formats_sort_together():
    """RSS emits '+00:00', X's <time> emits 'Z', fallbacks are naive - comparing
    those without normalising raises TypeError."""
    state = EventState()
    state.add_posts([
        post("rss", published="2026-01-05T10:00:00+00:00"),
        post("x", source="x_musk", published="2026-01-06T10:00:00Z"),
        post("naive", published="2026-01-04T10:00:00"),
        post("empty", published=None),
    ])
    assert [p["id"] for p in state.posts_snapshot()][:3] == ["x", "rss", "naive"]


def test_posts_are_deduped_by_id():
    state = EventState()
    state.add_posts([post("p1")])
    state.add_posts([post("p1"), post("p2")])
    assert len(state.posts_snapshot()) == 2


def test_posts_survive_a_restart(isolated_state_files):
    """Events are in-memory by design, but a source only reports genuinely new
    items - so without persistence this panel would be empty after every
    restart until someone next posted."""
    EventState().add_posts([post("p1"), post("p2")])
    assert len(EventState().posts_snapshot()) == 2


def test_posts_are_capped(monkeypatch):
    monkeypatch.setattr(event_state, "POSTS_MAX_COUNT", 3)
    state = EventState()
    state.add_posts([post(f"p{i}", published=f"2026-01-0{i}T10:00:00+00:00") for i in range(1, 6)])
    assert len(state.posts_snapshot(limit=100)) == 3


def test_old_posts_are_kept():
    """Bounded by count, not age: a source that goes quiet should still show
    its last posts rather than emptying the panel."""
    state = EventState()
    state.add_posts([post("ancient", published="2020-01-01T10:00:00+00:00")])
    assert [p["id"] for p in state.posts_snapshot()] == ["ancient"]


def test_posts_filter_by_source():
    state = EventState()
    state.add_posts([post("a", source="trumpstruth"), post("b", source="x_musk")])
    assert len(state.posts_snapshot(source="x_musk")) == 1
    assert len(state.posts_snapshot(source="trumpstruth")) == 1
    assert len(state.posts_snapshot()) == 2


def test_posts_respect_limit():
    state = EventState()
    state.add_posts([post(f"p{i}") for i in range(10)])
    assert len(state.posts_snapshot(limit=3)) == 3


def test_last_post_at_tracks_detection_not_publication():
    state = EventState()
    assert state.last_post_at() is None
    older = post("a"); older["detected_at"] = "2026-01-05T10:00:00"
    newer = post("b"); newer["detected_at"] = "2026-01-07T10:00:00"
    newer["published_at"] = "2020-01-01T00:00:00+00:00"  # backfilled, still news to the UI
    state.add_posts([older, newer])
    assert state.last_post_at() == "2026-01-07T10:00:00"


def test_corrupt_posts_file_does_not_raise(isolated_state_files):
    (isolated_state_files / "posts.json").write_text("{ not json")
    assert EventState().posts_snapshot() == []


def test_entries_without_an_id_are_dropped_on_load(isolated_state_files):
    (isolated_state_files / "posts.json").write_text(json.dumps([post("ok"), {"no": "id"}, "junk"]))
    assert [p["id"] for p in EventState().posts_snapshot()] == ["ok"]


def test_build_post_shape(poller):
    built = poller._build_post(item(source="trumpstruth", ext_id="42", title="Post title"))
    assert built["source"] == "trumpstruth"
    assert built["title"] == "Post title"
    assert built["id"] and built["detected_at"] and built["published_at"]
    assert "ticker" not in built  # raw item: no analysis attached


def test_build_post_truncates_long_text(poller):
    long_item = item(source="trumpstruth")
    long_item.text = "x" * 5000
    assert len(poller._build_post(long_item)["text"]) == event_state.POST_TEXT_LIMIT


def test_same_item_yields_a_stable_post_id(poller):
    a = poller._build_post(item(source="trumpstruth", ext_id="42"))
    b = poller._build_post(item(source="trumpstruth", ext_id="42"))
    assert a["id"] == b["id"]  # so dedup across restarts works


# ---------------------------------------------------------- gemini backoff

@pytest.fixture
def poller(monkeypatch):
    """An EventPoller with no real sources or network client."""
    monkeypatch.setattr(event_state, "GoogleNewsSource", lambda **kw: object())
    monkeypatch.setattr(event_state, "TrumpsTruthSource", lambda: object())
    monkeypatch.setattr(event_state, "XMuskSource", lambda: object())
    return EventPoller(EventState(), EventSettings(), gemini=object())


def test_gemini_ready_when_healthy(poller):
    assert poller._gemini_ready() is True


def test_gemini_backs_off_immediately_after_a_failure(poller):
    poller.state.record_gemini_failure("quota exceeded")
    assert poller._gemini_ready() is False


def test_gemini_backoff_expires(poller):
    poller.state.record_gemini_failure("quota exceeded")
    long_ago = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    poller.state.gemini_health["last_failure_at"] = long_ago
    assert poller._gemini_ready() is True


def test_backoff_grows_with_consecutive_failures(poller):
    """One failure tolerates a short wait; many must not retry that soon."""
    for _ in range(5):
        poller.state.record_gemini_failure("quota exceeded")
    two_minutes_ago = (datetime.now() - timedelta(minutes=2)).isoformat(timespec="seconds")
    poller.state.gemini_health["last_failure_at"] = two_minutes_ago
    assert poller._gemini_ready() is False  # 2min < the escalated delay


def test_success_clears_the_backoff(poller):
    poller.state.record_gemini_failure("quota exceeded")
    poller.state.record_gemini_success()
    assert poller._gemini_ready() is True


# --------------------------------------------------------- source backoff

def test_source_interval_doubles_per_failure(poller):
    base = 600
    assert poller._effective_interval("google_news", base) == base
    poller.state.record_check_failure("google_news", "boom")
    assert poller._effective_interval("google_news", base) == base * 2
    for _ in range(10):
        poller.state.record_check_failure("google_news", "boom")
    assert poller._effective_interval("google_news", base) == 3600  # capped


def test_is_due_on_first_check(poller):
    assert poller._is_due("google_news", 600) is True


def test_is_due_respects_the_interval(poller):
    poller.state.record_check_success("google_news", 0)
    assert poller._is_due("google_news", 600) is False


# ------------------------------------------------------------- atomic save

def test_save_json_file_is_atomic(tmp_path):
    """A failed write must leave the previous file intact rather than truncated."""
    path = tmp_path / "state.json"
    app_util.save_json_file(str(path), {"good": 1})
    app_util.save_json_file(str(path), {"bad": {1, 2, 3}})  # a set is not JSON-serialisable
    assert json.loads(path.read_text()) == {"good": 1}
    assert not (tmp_path / "state.json.tmp").exists()  # temp file cleaned up


def test_json_safe_replaces_non_finite_floats():
    payload = {"a": float("nan"), "b": [1.0, float("inf")], "c": {"d": float("-inf")}, "e": "ok"}
    assert app_util.json_safe(payload) == {"a": None, "b": [1.0, None], "c": {"d": None}, "e": "ok"}


def test_json_safe_is_json_serialisable_with_allow_nan_false():
    """The actual failure mode: Starlette renders with allow_nan=False."""
    cleaned = app_util.json_safe({"rsi": float("nan"), "price": 1.5})
    assert json.dumps(cleaned, allow_nan=False) == '{"rsi": null, "price": 1.5}'
