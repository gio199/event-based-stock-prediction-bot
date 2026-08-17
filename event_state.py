"""Background poller, state, and persisted settings for the Event Feed
feature. Mirrors web_state.py's threading/persistence shape (lock +
wake-event scheduler, JSON-file settings) so the two subsystems read
consistently, but polls continuously across multiple independent sources
instead of running one-shot jobs against a single watchlist.
"""
import hashlib
import logging
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

from app_util import WakeableDaemon, data_path, load_json_file, now_iso, save_json_file
from news_sources import (
    EventGeminiClient,
    GoogleNewsSource,
    TrumpsTruthSource,
    XMuskSource,
    resolve_ticker,
)

logger = logging.getLogger(__name__)

EVENT_POLL_TICK_SECONDS = 30  # internal loop granularity, far finer than any source's own interval
EVENT_MAX_COUNT = 500
EVENT_MAX_AGE_DAYS = 7
# GoogleNewsSource alone can return 1000+ items in a single check (12 default
# queries x up to ~100 results each), and Google's own "top 100" results skew
# stale (median item age ~6.6 days) so the same items recur across checks.
# The ring must comfortably outlast a full check's volume or items get
# evicted and misread as "new" again next tick - see the 500-vs-1200 bug
# this caught during testing (702 items falsely reappeared as new).
SEEN_RING_SIZE = 5000
# Sized well above a realistic single check (see above) because items are
# marked seen *before* they are queued: anything trimmed here is gone for
# good, never re-fetched and never analyzed. Truncation is logged rather than
# silent for the same reason.
MAX_PENDING = 2000

# Analysis backoff, mirroring the per-source fetch backoff below. Without it a
# quota-exhausted key meant the whole pending queue was re-sent on every 30s
# tick forever - thousands of rejected calls a day against the very free-tier
# limit the rest of the design works to protect.
GEMINI_RETRY_BASE_SECONDS = 60
GEMINI_RETRY_MAX_SECONDS = 3600

EVENT_SETTINGS_FILE = data_path("event_settings.json")
EVENT_SEEN_FILE = data_path("event_seen.json")
EVENT_PENDING_FILE = data_path("event_pending.json")
SOCIAL_POSTS_FILE = data_path("social_posts.json")

SOURCE_NAMES = ("google_news", "trumpstruth", "x_musk")
MIN_INTERVAL_SECONDS = {"google_news": 60, "trumpstruth": 60, "x_musk": 300}

# Sources whose raw items are worth showing verbatim. Google News is excluded
# on volume alone - a single check can return 1000+ headlines, which would bury
# the handful of posts this panel exists to surface.
SOCIAL_SOURCES = ("trumpstruth", "x_musk")
# Bounded by count only, deliberately not by age: this panel answers "what did
# they post most recently", so if a source goes quiet for a fortnight the right
# answer is still its last posts (each card carries its own timestamp), not an
# empty panel.
POSTS_MAX_COUNT = 200
POST_TEXT_LIMIT = 1000


def _safe_parse(iso_str):
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except (TypeError, ValueError):
        return None


def _as_utc(dt):
    """Normalise to tz-aware UTC so mixed feed formats stay comparable.

    RSS pubDate arrives as '+00:00', X's <time datetime> as 'Z', and the
    fallbacks are naive - sorting or comparing those against each other raises
    TypeError without this.
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _published_sort_key(post):
    parsed = _as_utc(_safe_parse(post.get("published_at")))
    return parsed or datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- settings

class EventSettings:
    """`_lock` guards concurrent access: a FastAPI request thread writes
    these fields via `update_source()` while EventPoller's background
    thread reads them via `source_snapshot()`/`get_extra_queries()` every
    tick - without a lock those are two threads touching the same nested
    dicts with no synchronization at all."""

    def __init__(self):
        self._lock = threading.Lock()
        self.sources = {
            "google_news": {"enabled": True, "interval_seconds": 600},
            "trumpstruth": {"enabled": True, "interval_seconds": 300},
            "x_musk": {"enabled": False, "interval_seconds": 600},  # riskiest source - opt-in only
        }
        self.google_news_extra_queries = []

    def to_dict(self):
        with self._lock:
            return {
                "sources": {k: dict(v) for k, v in self.sources.items()},
                "google_news_extra_queries": list(self.google_news_extra_queries),
            }

    def source_snapshot(self, name):
        """A copy of one source's config, read atomically."""
        with self._lock:
            cfg = self.sources.get(name)
            return dict(cfg) if cfg else None

    def get_extra_queries(self):
        with self._lock:
            return list(self.google_news_extra_queries)

    def update_source(self, name, enabled=None, interval_seconds=None):
        with self._lock:
            if name not in self.sources:
                raise ValueError(f"unknown source: {name}")
            if enabled is not None:
                self.sources[name]["enabled"] = bool(enabled)
            if interval_seconds is not None:
                floor = MIN_INTERVAL_SECONDS.get(name, 60)
                self.sources[name]["interval_seconds"] = max(floor, int(interval_seconds))


def load_event_settings() -> EventSettings:
    settings = EventSettings()
    data = load_json_file(EVENT_SETTINGS_FILE, on_missing=dict)
    # Direct dict mutation is fine here: this only ever runs at startup,
    # before `settings` is shared with any other thread.
    for name, cfg in (data.get("sources") or {}).items():
        if name in settings.sources:
            settings.sources[name].update(cfg)
    settings.google_news_extra_queries = data.get("google_news_extra_queries", [])
    return settings


def save_event_settings(settings: EventSettings):
    save_json_file(EVENT_SETTINGS_FILE, settings.to_dict())


# ------------------------------------------------------------- dedup/seen

class _SeenStore:
    """Bounded per-source dedup state, persisted to disk so a restart
    doesn't reprocess old items. RSS sources use a ring of seen ids
    (RSS delivery order isn't guaranteed); x_musk uses a numeric
    high-water-mark since tweet ids are monotonically increasing."""

    def __init__(self):
        self._lock = threading.Lock()
        self._rings = {name: deque(maxlen=SEEN_RING_SIZE) for name in ("google_news", "trumpstruth")}
        self._sets = {name: set() for name in ("google_news", "trumpstruth")}
        self._x_state = {"last_seen_tweet_id": None}
        self._load()

    def _load(self):
        data = load_json_file(EVENT_SEEN_FILE, on_missing=dict)
        for name in ("google_news", "trumpstruth"):
            ids = data.get(name, {}).get("seen_ids", [])
            self._rings[name] = deque(ids, maxlen=SEEN_RING_SIZE)
            self._sets[name] = set(ids)
        self._x_state["last_seen_tweet_id"] = data.get("x_musk", {}).get("last_seen_tweet_id")

    def _save(self):
        data = {name: {"seen_ids": list(self._rings[name])} for name in ("google_news", "trumpstruth")}
        data["x_musk"] = {"last_seen_tweet_id": self._x_state["last_seen_tweet_id"]}
        save_json_file(EVENT_SEEN_FILE, data)

    def get(self, source_name):
        with self._lock:
            if source_name == "x_musk":
                return dict(self._x_state)
            return set(self._sets.get(source_name, set()))

    def is_seeded(self, source_name):
        """False only on a source's very first-ever check (nothing recorded
        yet, whether from this run or a persisted prior one)."""
        with self._lock:
            if source_name == "x_musk":
                return self._x_state.get("last_seen_tweet_id") is not None
            return len(self._sets.get(source_name, set())) > 0

    def mark_seen(self, items):
        """items: List[RawItem]. Called right after fetch, before Gemini,
        so a source never re-scrapes something it already pulled even if
        the downstream Gemini call later fails."""
        if not items:
            return
        changed = False
        with self._lock:
            for item in items:
                if item.source == "x_musk":
                    try:
                        tid = int(item.external_id)
                    except ValueError:
                        continue
                    current = self._x_state.get("last_seen_tweet_id")
                    if current is None or tid > int(current):
                        self._x_state["last_seen_tweet_id"] = str(tid)
                        changed = True
                elif item.source in self._rings:
                    if item.external_id not in self._sets[item.source]:
                        self._rings[item.source].append(item.external_id)
                        changed = True
            if changed:
                for name in self._rings:
                    self._sets[name] = set(self._rings[name])
                self._save()


def _dedup_by_external_id(items):
    seen_keys = set()
    result = []
    for item in items:
        key = (item.source, item.external_id)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        result.append(item)
    return result


def load_pending():
    """Rehydrate the queue, skipping individual malformed entries.

    Per-item tolerance matters: one entry written by an older schema used to
    discard the entire backlog, which (since these are already marked seen) is
    unrecoverable data loss triggered by a cosmetic mismatch.
    """
    from news_sources import RawItem

    raw = load_json_file(EVENT_PENDING_FILE, on_missing=list)
    if not isinstance(raw, list):
        return []
    items = []
    skipped = 0
    for entry in raw:
        try:
            items.append(RawItem(**entry))
        except TypeError:
            skipped += 1
    if skipped:
        logger.warning("Skipped %d malformed pending item(s)", skipped)
    return items


def save_pending(items):
    """Persist the analysis backlog, newest-first-preserving.

    Items reach here already marked seen, so the queue is their only remaining
    home - trimming drops them permanently. The tail is kept because for a
    recent-activity feed the newest items are the valuable ones, but any drop
    is logged: silently discarding hundreds of items looked identical to
    processing them.
    """
    dropped = len(items) - MAX_PENDING
    if dropped > 0:
        logger.warning(
            "Pending queue over capacity: dropping %d oldest item(s) beyond MAX_PENDING=%d. "
            "These were already marked seen and will not be re-fetched.",
            dropped, MAX_PENDING,
        )
    trimmed = items[-MAX_PENDING:]
    save_json_file(EVENT_PENDING_FILE, [item.__dict__ for item in trimmed])


# --------------------------------------------------------------- state

class EventState:
    """Rolling in-memory event feed plus per-source health. Events are not
    persisted to disk on purpose - this is a recent-activity feed, not a
    permanent record; a restart starting with an empty feed is fine.

    `posts` is the exception, and is persisted - see add_posts().
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.events = []  # newest-first
        self.posts = self._load_posts()  # raw social posts, newest-first
        self.source_health = {
            name: {
                "last_checked_at": None,
                "last_success_at": None,
                "consecutive_failures": 0,
                "last_error": None,
                "last_new_items_count": 0,
            }
            for name in SOURCE_NAMES
        }
        # Separate from source_health: a source can fetch successfully while
        # Gemini analysis of what it found keeps failing (bad key, quota,
        # response-format drift) - that failure mode was previously
        # invisible, since only fetch failures were ever recorded anywhere.
        self.gemini_health = {
            "consecutive_failures": 0,
            "last_error": None,
            "last_failure_at": None,
            "last_success_at": None,
        }

    def record_check_success(self, name, new_items_count):
        with self._lock:
            now = now_iso()
            h = self.source_health[name]
            h["last_checked_at"] = now
            h["last_success_at"] = now
            h["consecutive_failures"] = 0
            h["last_error"] = None
            h["last_new_items_count"] = new_items_count

    def record_check_failure(self, name, error_str):
        with self._lock:
            h = self.source_health[name]
            h["last_checked_at"] = now_iso()
            h["consecutive_failures"] += 1
            h["last_error"] = str(error_str)[:300]

    def record_gemini_success(self):
        with self._lock:
            self.gemini_health["consecutive_failures"] = 0
            self.gemini_health["last_success_at"] = now_iso()

    def record_gemini_failure(self, error_str):
        with self._lock:
            self.gemini_health["consecutive_failures"] += 1
            self.gemini_health["last_error"] = str(error_str)[:300]
            self.gemini_health["last_failure_at"] = now_iso()

    def source_status(self, name) -> dict:
        """A locked copy of one source's health, for the poller's scheduling
        decisions. Reading self.source_health directly bypassed the lock every
        other accessor in this class holds."""
        with self._lock:
            return dict(self.source_health[name])

    def gemini_status(self) -> dict:
        with self._lock:
            return dict(self.gemini_health)

    def add_events(self, new_events):
        if not new_events:
            return
        with self._lock:
            self.events = new_events + self.events
            self._prune_locked()

    def _prune_locked(self):
        cutoff = datetime.now() - timedelta(days=EVENT_MAX_AGE_DAYS)
        kept = []
        for e in self.events:
            detected = _safe_parse(e.get("detected_at"))  # parsed once, and .get throughout
            if detected is None or detected >= cutoff:
                kept.append(e)
            if len(kept) >= EVENT_MAX_COUNT:
                break
        self.events = kept

    def events_snapshot(self, limit=100, source=None, ticker=None):
        with self._lock:
            events = list(self.events)
        if source:
            events = [e for e in events if e["source"] == source]
        if ticker:
            events = [e for e in events if (e.get("ticker") or "").upper() == ticker.upper()]
        return events[:limit]

    def health_snapshot(self, settings: EventSettings):
        with self._lock:
            health = {k: dict(v) for k, v in self.source_health.items()}
            health["_gemini"] = dict(self.gemini_health)
        for name in SOURCE_NAMES:
            cfg = settings.source_snapshot(name) or {}
            health[name]["enabled"] = cfg.get("enabled", False)
            health[name]["interval_seconds"] = cfg.get("interval_seconds", 600)
        return health

    def last_detected_at(self):
        with self._lock:
            return self.events[0]["detected_at"] if self.events else None

    # ------------------------------------------------------- social posts

    @staticmethod
    def _load_posts():
        raw = load_json_file(SOCIAL_POSTS_FILE, on_missing=list)
        if not isinstance(raw, list):
            return []
        return [p for p in raw if isinstance(p, dict) and p.get("id")]

    def add_posts(self, new_posts):
        """Record raw social posts, newest-published-first.

        Deliberately separate from `events`: an event only exists where the AI
        matched an item to a company, so a Truth Social post about policy - or
        any post it read as market-irrelevant - produced nothing visible at all.
        These are the source items themselves, shown whether or not they
        yielded a signal.

        Unlike events these *are* persisted. A source only ever reports
        genuinely new items, so an in-memory-only list would leave this panel
        empty after every restart until the next time someone happened to post.
        """
        if not new_posts:
            return
        with self._lock:
            known = {p["id"] for p in self.posts}
            fresh = [p for p in new_posts if p["id"] not in known]
            if not fresh:
                return
            self.posts = sorted(fresh + self.posts, key=_published_sort_key, reverse=True)
            self._prune_posts_locked()
            snapshot = list(self.posts)
        # Written outside the lock: readers shouldn't block on file I/O.
        save_json_file(SOCIAL_POSTS_FILE, snapshot)

    def _prune_posts_locked(self):
        del self.posts[POSTS_MAX_COUNT:]

    def posts_snapshot(self, limit=50, source=None):
        with self._lock:
            posts = list(self.posts)
        if source:
            posts = [p for p in posts if p["source"] == source]
        return posts[:limit]

    def last_post_at(self):
        """Newest detection timestamp, for the frontend's change-poll.

        Detection order, not publication order: a backfilled older post is
        still news to the UI.
        """
        with self._lock:
            return max((p.get("detected_at") or "" for p in self.posts), default=None) or None


# --------------------------------------------------------------- poller

class EventPoller(WakeableDaemon):
    """Single background thread covering all sources - see plan doc for why
    one thread (not one per source): at a 5-10 min cadence there's no
    throughput need for concurrency, and one thread makes it trivial to
    batch a whole tick's new items into as few Gemini calls as possible."""

    def __init__(self, event_state: EventState, event_settings: EventSettings, gemini=None):
        super().__init__()
        self.state = event_state
        self.settings = event_settings
        self.gemini = gemini or EventGeminiClient()
        self.seen_store = _SeenStore()
        self.sources = {
            "google_news": GoogleNewsSource(extra_queries_provider=event_settings.get_extra_queries),
            "trumpstruth": TrumpsTruthSource(),
            "x_musk": XMuskSource(),
        }

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                # belt-and-suspenders - a bug here must never kill the daemon
                # thread, but it should not vanish without a trace either
                logger.exception("Event poll tick failed")
            self._wake_event.wait(timeout=EVENT_POLL_TICK_SECONDS)
            self._wake_event.clear()

    def _effective_interval(self, name, base_interval_seconds):
        failures = self.state.source_status(name)["consecutive_failures"]
        return min(base_interval_seconds * (2 ** min(failures, 4)), 3600)  # capped exponential backoff

    def _is_due(self, name, base_interval_seconds):
        last_checked = self.state.source_status(name)["last_checked_at"]
        if not last_checked:
            return True
        last = _safe_parse(last_checked)
        if last is None:
            return True
        elapsed = (datetime.now() - last).total_seconds()
        return elapsed >= self._effective_interval(name, base_interval_seconds)

    def _gemini_ready(self):
        """Whether the analysis backoff has elapsed since the last failure.

        Fetch failures back off per source; analysis failures did not, so a
        bad or quota-exhausted key meant the entire pending queue was re-sent
        every single tick, indefinitely. The failure count that drives this is
        the same one already surfaced in the UI's health banner.
        """
        health = self.state.gemini_status()
        failures = health["consecutive_failures"]
        if not failures:
            return True
        last_failure = _safe_parse(health["last_failure_at"])
        if last_failure is None:
            return True
        delay = min(
            GEMINI_RETRY_BASE_SECONDS * (2 ** min(failures - 1, 5)),
            GEMINI_RETRY_MAX_SECONDS,
        )
        return (datetime.now() - last_failure).total_seconds() >= delay

    def _tick(self):
        newly_fetched = []
        for name, source in self.sources.items():
            if self.should_stop():
                return
            cfg = self.settings.source_snapshot(name)
            if not cfg or not cfg["enabled"]:
                continue
            if not self._is_due(name, cfg["interval_seconds"]):
                continue
            try:
                seen = self.seen_store.get(name)
                is_first_check = not self.seen_store.is_seeded(name)
                fetched = source.fetch_items(should_stop=self.should_stop)
                if name in SOCIAL_SOURCES:
                    # Every item on the feed, not just the unseen ones. A post
                    # that was already analyzed is still one of the latest
                    # posts, so filtering here would leave the panel empty on
                    # any install whose dedup state was already seeded - which
                    # is every install after the first check. add_posts()
                    # dedups by id, so re-offering the same feed is a no-op.
                    self.state.add_posts([self._build_post(i) for i in fetched])
                items = source.filter_unseen(fetched, seen)
                self.seen_store.mark_seen(items)
                if is_first_check:
                    # Cold start: a source's very first check can return its
                    # entire current backlog (e.g. ~100 RSS items) as "new"
                    # since nothing has been seen yet. Seed the dedup state
                    # from it without spending Gemini calls on the backlog -
                    # only genuinely new items from here on get analyzed.
                    # Report 0 new: counting the seeded backlog made the UI
                    # claim "702 new last check" while no events appeared.
                    self.state.record_check_success(name, 0)
                    logger.info("Seeded %s dedup state from %d backlog item(s)", name, len(items))
                    continue
                self.state.record_check_success(name, len(items))
                newly_fetched.extend(items)
            except Exception as e:
                logger.warning("Source %s failed: %s", name, e)
                self.state.record_check_failure(name, e)

        pending = load_pending()
        batch = _dedup_by_external_id(pending + newly_fetched)
        if not batch:
            return
        # Persist the full batch as pending *before* calling Gemini (which
        # can take tens of seconds across several chunks). mark_seen() above
        # already excludes these from being re-fetched, so without this,
        # a crash/kill during the Gemini call would silently drop them
        # forever - not re-discoverable (already seen) and never queued
        # (pending file not written yet). _analyze_and_store overwrites this
        # with the reduced still-failed set once it knows what happened.
        save_pending(batch)
        if not self._gemini_ready():
            return  # still backing off; the batch stays queued for a later tick
        self._analyze_and_store(batch)

    def _analyze_and_store(self, batch):
        results = self.gemini.analyze_events_batch(batch)
        still_pending = []
        new_events = []
        failure_reason = None
        for item, result in zip(batch, results):
            if result.get("failed"):
                still_pending.append(item)
                failure_reason = failure_reason or result.get("reason")
                continue
            for company in result.get("companies", []):
                new_events.append(self._build_event(item, company))

        if failure_reason:
            self.state.record_gemini_failure(failure_reason)
        else:
            self.state.record_gemini_success()

        save_pending(still_pending)
        if new_events:
            self.state.add_events(new_events)

    def _build_post(self, item):
        """A raw source item, unanalyzed. No ticker or sentiment - that is the
        point; those only exist once the AI has matched the item."""
        return {
            "id": hashlib.sha256(f"{item.source}|{item.external_id}".encode("utf-8")).hexdigest()[:16],
            "source": item.source,
            "title": item.title,
            "text": (item.text or "")[:POST_TEXT_LIMIT],
            "link": item.link,
            "published_at": item.published_at,
            "detected_at": now_iso(),
        }

    def _build_event(self, item, company):
        ticker_guess = company.get("ticker_guess")
        company_guess = company.get("company_guess")
        resolved = resolve_ticker(ticker_guess, company_guess)
        event_id = hashlib.sha256(
            f"{item.source}|{item.external_id}|{ticker_guess or company_guess}".encode("utf-8")
        ).hexdigest()[:16]
        return {
            "id": event_id,
            "detected_at": now_iso(),
            "published_at": item.published_at,
            "source": item.source,
            "title": item.title,
            "text_snippet": item.text[:280],
            "link": item.link,
            "ticker": resolved["symbol"] if resolved else None,
            "company_name": resolved["company_name"] if resolved else company_guess,
            "ticker_verified": resolved is not None,
            "sentiment_score": company.get("sentiment_score", 0),
            "confidence": company.get("confidence", 0),
            "reasoning": company.get("reasoning", ""),
        }
