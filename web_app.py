"""FastAPI web interface for the stock analyzer.

Wraps StandaloneStockAnalyzer with in-memory state, a background auto-refresh
thread, and a small JSON API. Serves the static frontend from ./static
alongside the API on a single local process.

Run with: python -m uvicorn web_app:app --host 127.0.0.1 --port 8000

SINGLE WORKER ONLY. The state, scheduler and event poller below are
module-level singletons, so `--workers N` would give every worker its own
copy: N pollers making N times the outbound API calls, N dedup stores racing
over the same file, and /api/settings answering differently depending on which
worker got the request. Scale by making the poller a separate process, not by
adding workers.
"""
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app_util import configure_logging, load_env_once
from standalone_stock_analyzer import StandaloneStockAnalyzer
from web_state import (
    AnalysisState,
    RefreshScheduler,
    load_settings,
    save_settings,
    start_watchlist_job,
    start_custom_job,
    METRIC_GLOSSARY,
    MAX_CUSTOM_SYMBOLS,
    MAX_REFRESH_INTERVAL_SECONDS,
    MIN_REFRESH_INTERVAL_SECONDS,
)
from event_state import (
    EventPoller,
    EventState,
    load_event_settings,
    save_event_settings,
    EVENT_MAX_COUNT,
    POSTS_MAX_COUNT,
    SOCIAL_SOURCES,
    SOURCE_NAMES as EVENT_SOURCE_NAMES,
    MIN_INTERVAL_SECONDS as EVENT_MIN_INTERVAL_SECONDS,
)
from news_sources import X_SESSION_STATE_FILE

configure_logging()
load_env_once()  # explicit, rather than relying on an import side effect

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(_BASE_DIR, "static")

# Ticker symbols are echoed into outbound yfinance URLs, so constrain them to
# the shape a real symbol has (AAPL, BRK.B, BF-B) rather than forwarding
# arbitrary user input.
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")

# Module-level singletons: constructed once at import time so the
# ENABLED/DISABLED banner is logged once at startup, not per request.
analyzer = StandaloneStockAnalyzer()
state = AnalysisState()
settings = load_settings()
scheduler = RefreshScheduler(state, analyzer, settings)

event_state = EventState()
event_settings = load_event_settings()
event_poller = EventPoller(event_state, event_settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    event_poller.start()
    yield
    # stop() now joins, so an in-flight Playwright browser gets a chance to
    # close instead of being killed mid-navigation at interpreter exit.
    scheduler.stop()
    event_poller.stop()


app = FastAPI(title="Stock Analyzer Web UI", lifespan=lifespan)


class AnalyzeRequest(BaseModel):
    symbols: List[str]


class SettingsUpdate(BaseModel):
    auto_refresh_enabled: Optional[bool] = None
    refresh_interval_seconds: Optional[int] = None


class EventSourceUpdate(BaseModel):
    enabled: Optional[bool] = None
    interval_seconds: Optional[int] = None


@app.get("/api/config")
def get_config():
    return {
        "default_stocks": StandaloneStockAnalyzer.DEFAULT_STOCKS,
        "gemini_configured": bool(analyzer.news_analyzer.gemini.api_key),
        "max_custom_symbols": MAX_CUSTOM_SYMBOLS,
        "min_refresh_interval_seconds": MIN_REFRESH_INTERVAL_SECONDS,
        "x_scraper_configured": os.path.exists(X_SESSION_STATE_FILE),
    }


@app.get("/api/settings")
def get_settings():
    return settings.to_dict()


@app.post("/api/settings")
def post_settings(req: SettingsUpdate):
    if req.refresh_interval_seconds is not None and not (
        MIN_REFRESH_INTERVAL_SECONDS <= req.refresh_interval_seconds <= MAX_REFRESH_INTERVAL_SECONDS
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"refresh_interval_seconds must be between {MIN_REFRESH_INTERVAL_SECONDS} "
                f"and {MAX_REFRESH_INTERVAL_SECONDS}"
            ),
        )
    settings.update(
        auto_refresh_enabled=req.auto_refresh_enabled,
        refresh_interval_seconds=req.refresh_interval_seconds,
    )
    save_settings(settings)
    scheduler.notify_settings_changed()
    return settings.to_dict()


@app.get("/api/stocks")
def get_stocks():
    # Read under the state lock rather than touching the attributes directly,
    # so results/errors/last_updated are always from the same run.
    return state.watchlist_snapshot()


@app.get("/api/custom-results")
def get_custom_results():
    return state.custom_snapshot()


@app.get("/api/status")
def get_status():
    snapshot = state.status_snapshot()
    snapshot["events_last_detected_at"] = event_state.last_detected_at()
    snapshot["posts_last_detected_at"] = event_state.last_post_at()
    return snapshot


@app.get("/api/posts")
def get_posts(
    limit: int = Query(50, ge=1, le=POSTS_MAX_COUNT),
    source: Optional[str] = None,
):
    """Raw social posts, unfiltered by whether the AI found a ticker in them.

    /api/events only carries items that resolved to a company, so a post the
    model read as market-irrelevant never appeared anywhere. This is the
    source material itself.
    """
    if source is not None and source not in SOCIAL_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"source must be one of: {', '.join(SOCIAL_SOURCES)}",
        )
    posts = event_state.posts_snapshot(limit=limit, source=source)
    return {"posts": posts, "count": len(posts)}


@app.get("/api/events")
def get_events(
    limit: int = Query(100, ge=1, le=EVENT_MAX_COUNT),
    source: Optional[str] = None,
    ticker: Optional[str] = None,
):
    events = event_state.events_snapshot(limit=limit, source=source, ticker=ticker)
    return {"events": events, "count": len(events)}


@app.get("/api/events/sources")
def get_event_sources():
    return event_state.health_snapshot(event_settings)


@app.post("/api/events/sources/{name}")
def post_event_source(name: str, req: EventSourceUpdate):
    if name not in EVENT_SOURCE_NAMES:
        raise HTTPException(status_code=404, detail=f"unknown source: {name}")
    if req.interval_seconds is not None and req.interval_seconds < EVENT_MIN_INTERVAL_SECONDS.get(name, 60):
        raise HTTPException(
            status_code=400,
            detail=f"interval_seconds must be at least {EVENT_MIN_INTERVAL_SECONDS.get(name, 60)} for {name}",
        )
    if name == "x_musk" and req.enabled and not os.path.exists(X_SESSION_STATE_FILE):
        raise HTTPException(
            status_code=400,
            detail="no X session found - run `python login_x_bot.py` once before enabling this source",
        )
    event_settings.update_source(name, enabled=req.enabled, interval_seconds=req.interval_seconds)
    save_event_settings(event_settings)
    event_poller.notify_settings_changed()
    return event_state.health_snapshot(event_settings)


@app.get("/api/glossary")
def get_glossary():
    return METRIC_GLOSSARY


@app.post("/api/refresh", status_code=202)
def post_refresh():
    # start_*_job claims the job slot synchronously and reports whether it won,
    # so a 202 here means the work really is queued. The old shape checked
    # `state.lock.locked()` and then spawned a thread that re-checked, which
    # could answer 202 for a job that silently never ran.
    if not start_watchlist_job(state, analyzer):
        raise HTTPException(
            status_code=409,
            detail={"message": "Analysis already in progress", "status": state.status_snapshot()},
        )
    return {"started": True, "job_type": "watchlist_refresh", "symbols": StandaloneStockAnalyzer.DEFAULT_STOCKS}


@app.post("/api/analyze", status_code=202)
def post_analyze(req: AnalyzeRequest):
    symbols = list(dict.fromkeys(s.strip().upper() for s in req.symbols if s.strip()))
    if not symbols:
        raise HTTPException(status_code=400, detail="No valid symbols provided")
    if len(symbols) > MAX_CUSTOM_SYMBOLS:
        raise HTTPException(status_code=400, detail=f"Too many symbols (max {MAX_CUSTOM_SYMBOLS})")
    invalid = [s for s in symbols if not SYMBOL_RE.match(s)]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid symbol(s): {', '.join(invalid[:5])}",
        )
    if not start_custom_job(state, analyzer, symbols):
        raise HTTPException(
            status_code=409,
            detail={"message": "Analysis already in progress", "status": state.status_snapshot()},
        )
    return {"started": True, "job_type": "custom_analyze", "symbols": symbols}


# Registered LAST so /api/* routes above take priority over this catch-all.
# Anchored to this file's directory, not the process CWD - a relative "static"
# made `uvicorn web_app:app` crash at import from any other working directory.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
