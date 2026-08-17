"""Free/keyless news & social sources for the Event Feed feature.

Each source implements a common fetch_new_items(seen, should_stop) ->
List[RawItem] interface so EventPoller (event_state.py) can treat them
uniformly. Kept separate from standalone_stock_analyzer.py, which this module
imports as a library rather than modifying.
"""
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from typing import Dict, List, Optional
from urllib.parse import quote

import requests

from app_util import data_path, gemini_post, gemini_response_text, get_gemini_api_key
from standalone_stock_analyzer import NewsAnalyzer

logger = logging.getLogger(__name__)

GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
TRUMPSTRUTH_RSS_URL = "https://www.trumpstruth.org/feed"
X_MUSK_PROFILE_URL = "https://x.com/elonmusk"
X_SESSION_STATE_FILE = data_path("x_session_state.json")

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    no_tags = _HTML_TAG_RE.sub(" ", text or "")
    return _WHITESPACE_RE.sub(" ", no_tags).strip()


@dataclass
class RawItem:
    source: str
    external_id: str
    published_at: str
    title: str
    text: str
    link: str
    query: Optional[str] = None


def _parse_rss_items(xml_bytes: bytes, source: str, query: Optional[str] = None) -> List[RawItem]:
    """Shared parsing for Google News and trumpstruth.org - both are plain
    RSS 2.0 with title/link/guid/pubDate/description."""
    items = []
    root = ET.fromstring(xml_bytes)
    for item_el in root.findall("./channel/item"):
        title = (item_el.findtext("title") or "").strip()
        link = (item_el.findtext("link") or "").strip()
        guid = (item_el.findtext("guid") or link or title).strip()
        description = _strip_html(item_el.findtext("description") or "")
        pub_date_raw = item_el.findtext("pubDate")
        try:
            published_at = parsedate_to_datetime(pub_date_raw).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            published_at = datetime.now(timezone.utc).isoformat()
        if not guid or not title:
            continue
        items.append(RawItem(
            source=source,
            external_id=guid,
            published_at=published_at,
            title=title,
            text=description or title,
            link=link,
            query=query,
        ))
    return items


class NewsSource:
    """Fetching and dedup-filtering are separate steps on purpose.

    The event pipeline wants only items it hasn't analyzed yet, but the raw
    posts panel wants whatever the feed currently holds - a post that was
    already analyzed is still the latest post. Collapsing the two meant the
    panel stayed empty forever on any install whose dedup state was already
    seeded.
    """

    name: str
    default_interval_seconds: int

    def fetch_items(self, should_stop=None) -> List[RawItem]:
        """Everything the source currently exposes, unfiltered.

        May raise - the poller catches per-source. `should_stop` is an optional
        predicate polled between slow steps so a shutdown doesn't have to wait
        out a multi-minute fetch.
        """
        raise NotImplementedError

    def filter_unseen(self, items: List[RawItem], seen) -> List[RawItem]:
        """Narrow to items not yet recorded. `seen` is a set of ids for the RSS
        sources; XMuskSource overrides this for its numeric watermark."""
        return [item for item in items if item.external_id not in seen]

    def fetch_new_items(self, seen, should_stop=None) -> List[RawItem]:
        """Fetch, then filter. Kept for callers that only want new items."""
        return self.filter_unseen(self.fetch_items(should_stop=should_stop), seen)


class GoogleNewsSource(NewsSource):
    name = "google_news"
    default_interval_seconds = 600

    QUERY_DELAY_SECONDS = 1.2
    REQUEST_TIMEOUT_SECONDS = 15

    def __init__(self, extra_queries_provider=None):
        # Reuse the existing per-symbol search phrases instead of duplicating them.
        self._search_terms = NewsAnalyzer().search_terms
        # A callable (not a captured list) so a future settings change to
        # google_news_extra_queries is picked up on the next check instead
        # of being silently stuck at whatever value existed at construction.
        self._extra_queries_provider = extra_queries_provider or (lambda: [])

    def fetch_items(self, should_stop=None) -> List[RawItem]:
        queries = list(self._search_terms.values()) + list(self._extra_queries_provider())
        items = []
        successes = 0
        attempted = 0
        last_error = None
        for i, query in enumerate(queries):
            # A dozen queries at up to 15s each plus the politeness delay can
            # occupy the single poller thread for minutes; bail out promptly
            # when shutdown has been signalled instead of running to the end.
            if should_stop is not None and should_stop():
                logger.info("Google News fetch interrupted after %d/%d queries", i, len(queries))
                break
            if i > 0:
                time.sleep(self.QUERY_DELAY_SECONDS)  # be polite across many queries
            attempted += 1
            url = f"{GOOGLE_NEWS_RSS_URL}?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"
            try:
                resp = requests.get(
                    url, timeout=self.REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": "Mozilla/5.0"}
                )
                resp.raise_for_status()
                items.extend(_parse_rss_items(resp.content, self.name, query=query))
                successes += 1
            except (requests.RequestException, ET.ParseError) as e:
                last_error = e
                continue
        if successes == 0 and attempted:
            raise RuntimeError(f"all {attempted} Google News queries failed: {last_error}")
        return items


class TrumpsTruthSource(NewsSource):
    name = "trumpstruth"
    default_interval_seconds = 300

    # The mirror serves a ~140KB, 100-item feed and regularly takes 20-40s to
    # respond; at the previous 15s it failed intermittently for no reason other
    # than the timeout being tighter than the source's normal latency.
    REQUEST_TIMEOUT_SECONDS = 45

    def fetch_items(self, should_stop=None) -> List[RawItem]:
        resp = requests.get(
            TRUMPSTRUTH_RSS_URL,
            timeout=self.REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        return _parse_rss_items(resp.content, self.name)


class XMuskSource(NewsSource):
    """Playwright-based, logged in via a saved session (see login_x_bot.py).
    Fundamentally more fragile than the RSS sources - X actively fights
    automation. Every failure mode here must degrade cleanly, never crash
    the shared poller thread."""

    name = "x_musk"
    default_interval_seconds = 600

    def filter_unseen(self, items: List[RawItem], seen) -> List[RawItem]:
        """Watermark rather than set membership - tweet ids increase monotonically."""
        last_seen_id = seen.get("last_seen_tweet_id") if isinstance(seen, dict) else None
        if not last_seen_id:
            return items
        try:
            watermark = int(last_seen_id)
        except (TypeError, ValueError):
            # Corrupt persisted watermark: treat as no watermark rather than
            # failing the whole check on every tick from here on.
            logger.warning("Ignoring unparseable last_seen_tweet_id %r", last_seen_id)
            return items
        return [i for i in items if int(i.external_id) > watermark]

    def fetch_items(self, should_stop=None) -> List[RawItem]:
        from playwright.sync_api import sync_playwright  # optional dependency - only needed if this source runs

        if not os.path.exists(X_SESSION_STATE_FILE):
            raise RuntimeError("no session saved - run `python login_x_bot.py` once to log in")

        posts = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(storage_state=X_SESSION_STATE_FILE)
                page = context.new_page()
                page.goto(X_MUSK_PROFILE_URL, wait_until="domcontentloaded", timeout=30000)
                if "/login" in page.url or "/i/flow/" in page.url:
                    raise RuntimeError("session expired - rerun `python login_x_bot.py`")
                page.wait_for_selector('[data-testid="tweet"]', timeout=15000)
                # A pinned tweet (if any) occupies the first slot regardless of
                # recency; grab a few extra so a pin doesn't crowd out genuinely
                # new posts lower on the page.
                tweet_els = page.query_selector_all('[data-testid="tweet"]')[:8]
                for el in tweet_els:
                    link_el = el.query_selector('a[href*="/status/"]')
                    if not link_el:
                        continue
                    href = link_el.get_attribute("href") or ""
                    m = re.search(r"/status/(\d+)", href)
                    if not m:
                        continue
                    tweet_id = m.group(1)
                    text_el = el.query_selector('[data-testid="tweetText"]')
                    text = text_el.inner_text() if text_el else ""
                    time_el = el.query_selector("time")
                    published_at = (
                        time_el.get_attribute("datetime") if time_el else datetime.now(timezone.utc).isoformat()
                    )
                    posts.append({
                        "tweet_id": tweet_id,
                        "text": text,
                        "published_at": published_at,
                        "link": f"https://x.com{href}" if href.startswith("/") else href,
                    })
            finally:
                browser.close()

        posts.reverse()  # page order is newest-first; process chronologically
        return [
            RawItem(
                source=self.name,
                external_id=post["tweet_id"],
                published_at=post["published_at"],
                title="@elonmusk on X",
                text=post["text"],
                link=post["link"],
            )
            for post in posts
        ]


def resolve_ticker(ticker_guess: Optional[str], company_guess: Optional[str]) -> Optional[Dict]:
    """Free/keyless ticker resolution & hallucination guard via yfinance
    Search. Returns {'symbol', 'company_name'} or None. Never raises.

    The try/except lives out here, outside the cache, on purpose - see
    _resolve_ticker_cached.
    """
    try:
        return _resolve_ticker_cached((ticker_guess or "").upper(), (company_guess or "").strip().lower())
    except Exception as e:
        logger.debug("Ticker lookup failed for %r/%r: %s", ticker_guess, company_guess, e)
        return None


@lru_cache(maxsize=2000)
def _resolve_ticker_cached(ticker_guess_norm: str, company_guess_norm: str) -> Optional[Dict]:
    """Look up a symbol, raising on transient failure rather than returning None.

    lru_cache is thread-safe and, unlike a hand-rolled dict, bounded - company
    names recur constantly across news items, but this runs for the life of
    the process, so an unbounded cache would grow forever.

    Crucially, lru_cache does *not* memoise a call that raised. Letting network
    errors propagate to the caller is therefore what keeps one blip from
    poisoning the entry forever: previously the except-and-return-None sat
    inside this function, so a single timeout permanently marked every future
    event about that company "unverified" until the process restarted. Only a
    genuine "no match" (an explicit None return) gets cached.
    """
    query = company_guess_norm or ticker_guess_norm
    if not query:
        return None

    import yfinance as yf

    search = yf.Search(query, max_results=5)
    quotes = [q for q in (search.quotes or []) if q.get("quoteType") in ("EQUITY", "ETF")]
    if ticker_guess_norm:
        match = next((q for q in quotes if q.get("symbol", "").upper() == ticker_guess_norm), None)
        if match:
            return {
                "symbol": match["symbol"],
                "company_name": match.get("longname") or match.get("shortname") or match["symbol"],
            }
    if quotes:
        top = quotes[0]
        return {
            "symbol": top["symbol"],
            "company_name": top.get("longname") or top.get("shortname") or top["symbol"],
        }
    return None


class EventGeminiClient:
    """Sibling to GeminiAIClient (standalone_stock_analyzer.py) - same
    api-key/header conventions, but for batched, multi-item, unknown-ticker
    event extraction rather than single-symbol sentiment. Never raises: any
    failure returns per-item 'failed' markers so the poller can re-queue
    them instead of losing them."""

    MAX_BATCH_SIZE_CEILING = 15
    MIN_BATCH_SIZE = 3
    # Deliberately a *different* model than GeminiAIClient's gemini-flash-latest:
    # separate free-tier quota bucket, so continuous event polling can't starve
    # the on-demand watchlist analysis (or vice versa) of its own daily quota.
    MODEL = "gemini-flash-lite-latest"

    def __init__(self):
        # Adaptive: shrinks when a whole chunk fails (e.g. hitting the token
        # cap on a large, verbose batch), grows back gradually on success.
        # Single background thread owns this client, so no lock needed.
        self._batch_size = self.MAX_BATCH_SIZE_CEILING

    @property
    def api_key(self) -> str:
        """Read live rather than captured in __init__, so a client constructed
        before .env was loaded still picks the key up."""
        return get_gemini_api_key()

    def analyze_events_batch(self, items: List[RawItem]) -> List[Dict]:
        """Returns one result dict per input item, same order/length as
        `items`: {'companies': [...], 'failed': bool}."""
        if not items:
            return []
        if not self.api_key:
            return [{"companies": [], "failed": True, "reason": "no GEMINI_API_KEY"} for _ in items]

        results = []
        i = 0
        while i < len(items):
            chunk = items[i:i + self._batch_size]
            chunk_results = self._analyze_chunk(chunk)
            if len(chunk) > self.MIN_BATCH_SIZE and all(r.get("failed") for r in chunk_results):
                self._batch_size = max(self.MIN_BATCH_SIZE, self._batch_size // 2)
            elif not any(r.get("failed") for r in chunk_results):
                self._batch_size = min(self.MAX_BATCH_SIZE_CEILING, self._batch_size + 1)
            results.extend(chunk_results)
            i += len(chunk)
            if i < len(items):
                time.sleep(1.0)  # small gap between sequential calls on an oversized batch
        return results

    def _analyze_chunk(self, chunk: List[RawItem]) -> List[Dict]:
        numbered = "\n\n".join(
            f"{i}. SOURCE: {item.source}\nTITLE: {item.title}\nTEXT: {item.text[:600]}"
            for i, item in enumerate(chunk)
        )
        prompt = f"""
        You are a financial news analyst. For each numbered item below, identify any
        publicly-traded companies it concerns and how the news would likely move that
        company's stock price. If an item mentions no company with plausible market
        relevance, return an empty companies list for it.

        Items:
        {numbered}

        Respond with a JSON array, one object per item, in the same order, each shaped as:
        {{"item_index": <int>, "companies": [{{"company_guess": <string>, "ticker_guess": <string or null>, "sentiment_score": <int -100 to 100>, "confidence": <int 0 to 100>, "reasoning": <short string>}}]}}
        """
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": min(150 * len(chunk) + 300, 4096),
                "responseMimeType": "application/json",
            },
        }
        try:
            response = gemini_post(self.MODEL, payload, timeout=45)
            if response.status_code != 200:
                logger.warning("Gemini event batch returned HTTP %s", response.status_code)
                return [{"companies": [], "failed": True, "reason": f"http {response.status_code}"} for _ in chunk]
            text = gemini_response_text(response.json())
            if not text:
                return [{"companies": [], "failed": True, "reason": "empty response"} for _ in chunk]
            parsed = json.loads(text)
            if not isinstance(parsed, list):
                return [{"companies": [], "failed": True, "reason": "unexpected response shape"} for _ in chunk]
            by_index = {}
            for i, entry in enumerate(parsed):
                if not isinstance(entry, dict):
                    continue
                try:
                    by_index[int(entry.get("item_index", i))] = entry
                except (TypeError, ValueError):
                    by_index[i] = entry
            return [
                {"companies": by_index.get(i, {}).get("companies") or [], "failed": False}
                for i in range(len(chunk))
            ]
        except Exception as e:
            logger.warning("Gemini event batch failed: %s", e)
            return [{"companies": [], "failed": True, "reason": "parse/network error"} for _ in chunk]
