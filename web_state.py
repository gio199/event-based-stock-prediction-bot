"""Shared in-memory state, locking, and background job runners for the web UI.

Kept separate from web_app.py so the threading/state logic can be reasoned
about (and tested) independently of FastAPI route wiring.
"""
import logging
import os
import threading
from datetime import datetime, timedelta

from app_util import WakeableDaemon, data_path, load_json_file, now_iso, save_json_file

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_INTERVAL_SECONDS = int(os.environ.get("WEB_REFRESH_INTERVAL_SECONDS", 900))  # 15 min
MIN_REFRESH_INTERVAL_SECONDS = 60
MAX_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60  # a day; beyond this "auto" is meaningless
MAX_CUSTOM_SYMBOLS = 25
SETTINGS_FILE = data_path("web_settings.json")


class Settings:
    """User-configurable behavior, persisted to disk so it survives restarts.

    Auto-refresh defaults to OFF: by default the watchlist is only analyzed
    when the user explicitly clicks "Refresh Now" (or a custom Analyze),
    since a periodic 12-stock burst can trip Gemini's free-tier rate limit.

    `_lock` guards concurrent access: a FastAPI request thread writes these
    fields via `update()` while RefreshScheduler's background thread reads
    them via `snapshot()` - without a lock those are two threads touching
    the same attributes with no synchronization at all.
    """

    def __init__(self, auto_refresh_enabled=False, refresh_interval_seconds=None):
        self._lock = threading.Lock()
        self.auto_refresh_enabled = bool(auto_refresh_enabled)
        self.refresh_interval_seconds = int(refresh_interval_seconds or DEFAULT_REFRESH_INTERVAL_SECONDS)

    def to_dict(self):
        with self._lock:
            return {
                "auto_refresh_enabled": self.auto_refresh_enabled,
                "refresh_interval_seconds": self.refresh_interval_seconds,
            }

    def snapshot(self):
        """(auto_refresh_enabled, refresh_interval_seconds) read together atomically."""
        with self._lock:
            return self.auto_refresh_enabled, self.refresh_interval_seconds

    def update(self, auto_refresh_enabled=None, refresh_interval_seconds=None):
        with self._lock:
            if auto_refresh_enabled is not None:
                self.auto_refresh_enabled = bool(auto_refresh_enabled)
            if refresh_interval_seconds is not None:
                self.refresh_interval_seconds = min(
                    MAX_REFRESH_INTERVAL_SECONDS,
                    max(MIN_REFRESH_INTERVAL_SECONDS, int(refresh_interval_seconds)),
                )


def load_settings() -> Settings:
    data = load_json_file(SETTINGS_FILE, on_missing=dict)
    return Settings(
        auto_refresh_enabled=data.get("auto_refresh_enabled", False),
        refresh_interval_seconds=data.get("refresh_interval_seconds"),
    )


def save_settings(settings: Settings):
    save_json_file(SETTINGS_FILE, settings.to_dict())


class AnalysisState:
    """Holds the latest results plus in-progress job status.

    `lock` is the authoritative gate: only one analysis job (watchlist
    refresh or custom analyze) may run at a time. `_state_lock` separately
    guards the individual field reads/writes below so `/api/status` never
    observes a half-updated snapshot while a job thread is writing.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self._state_lock = threading.Lock()

        self.job_running = False
        self.job_type = None  # "watchlist_refresh" | "custom_analyze"
        self.job_total = 0
        self.job_completed = 0
        self.job_current_symbol = None
        self.job_started_at = None

        self.watchlist_results = []
        self.watchlist_errors = []  # [{symbol, error}]
        self.watchlist_last_updated = None

        self.custom_results = []
        self.custom_errors = []  # [{symbol, error}]
        self.custom_requested_symbols = []
        self.custom_completed_at = None

        self.next_scheduled_refresh_at = None

    def try_begin_job(self, job_type, symbols) -> bool:
        """Atomically claim the job slot, returning False if one is running.

        Acquiring `lock` *is* the check. The previous shape - an
        `if lock.locked()` guard in the route, then `acquire(blocking=False)`
        in the worker thread - was a check-then-act: losing that race meant
        the API had already answered 202 {"started": true} for a job that then
        returned silently without touching any state, leaving the UI polling
        for a timestamp that would never change.

        Every successful call must be paired with end_job().
        """
        if not self.lock.acquire(blocking=False):
            return False
        with self._state_lock:
            self.job_running = True
            self.job_type = job_type
            self.job_total = len(symbols)
            self.job_completed = 0
            self.job_current_symbol = symbols[0] if symbols else None
            self.job_started_at = now_iso()
        return True

    def end_job(self):
        """Release the job slot. Call exactly once per successful try_begin_job()."""
        self.mark_job_finished_if_stuck()
        self.lock.release()

    def update_progress(self, completed, symbol):
        with self._state_lock:
            self.job_completed = completed
            self.job_current_symbol = symbol

    def finish_watchlist_job(self, results, errors):
        with self._state_lock:
            self.watchlist_results = results
            self.watchlist_errors = errors
            self.watchlist_last_updated = now_iso()
            self.job_running = False
            self.job_type = None
            self.job_current_symbol = None

    def finish_custom_job(self, results, errors, requested_symbols):
        with self._state_lock:
            self.custom_results = results
            self.custom_errors = errors
            self.custom_requested_symbols = requested_symbols
            self.custom_completed_at = now_iso()
            self.job_running = False
            self.job_type = None
            self.job_current_symbol = None

    def set_next_scheduled_refresh(self, when_iso):
        with self._state_lock:
            self.next_scheduled_refresh_at = when_iso

    def mark_job_finished_if_stuck(self):
        """Safety net for a `finally` around the whole job body. If an
        unexpected exception happens between begin_job() and finish_*_job(),
        the `lock` still releases fine (its own finally handles that) but
        job_running/job_type/job_current_symbol would otherwise stay stuck
        at their in-progress values forever, since only finish_*_job() ever
        resets them. A no-op if the job already finished normally."""
        with self._state_lock:
            if self.job_running:
                self.job_running = False
                self.job_type = None
                self.job_current_symbol = None

    def status_snapshot(self) -> dict:
        with self._state_lock:
            return {
                "job_running": self.job_running,
                "job_type": self.job_type,
                "job_total": self.job_total,
                "job_completed": self.job_completed,
                "job_current_symbol": self.job_current_symbol,
                "job_started_at": self.job_started_at,
                "watchlist_last_updated": self.watchlist_last_updated,
                "custom_completed_at": self.custom_completed_at,
                "next_scheduled_refresh_at": self.next_scheduled_refresh_at,
            }

    def watchlist_snapshot(self) -> dict:
        """Results, errors and timestamp read together under the lock.

        The routes used to read these three attributes directly, which is the
        same half-updated-snapshot problem `_state_lock` exists to prevent:
        finish_watchlist_job() writes all three under the lock, so an
        unsynchronised reader can land mid-update and pair fresh results with
        the previous run's errors.
        """
        with self._state_lock:
            return {
                "results": list(self.watchlist_results),
                "errors": list(self.watchlist_errors),
                "last_updated": self.watchlist_last_updated,
            }

    def custom_snapshot(self) -> dict:
        with self._state_lock:
            return {
                "results": list(self.custom_results),
                "errors": list(self.custom_errors),
                "requested_symbols": list(self.custom_requested_symbols),
                "completed_at": self.custom_completed_at,
            }


def _run_symbols(state: AnalysisState, analyzer, symbols):
    """Analyze each symbol in order, reporting progress. Never raises on a
    bad symbol - analyze_stock() already returns {'error': ...} for those.
    """
    results = []
    errors = []
    for i, symbol in enumerate(symbols, 1):
        state.update_progress(i - 1, symbol)
        result = analyzer.analyze_stock(symbol)
        state.update_progress(i, symbol)
        if "error" in result:
            errors.append({"symbol": symbol, "error": result["error"]})
        else:
            results.append(result)
    results.sort(key=lambda r: r["score"], reverse=True)
    return results, errors


def start_watchlist_job(state: AnalysisState, analyzer, symbols=None) -> bool:
    """Claim the job slot, then run the watchlist on a background thread.

    The claim happens synchronously here, so the returned bool is authoritative
    for the caller: True means this job really is going to run. Callers must
    not pre-check `state.lock` - that reintroduces the check-then-act race
    try_begin_job() exists to close.
    """
    from standalone_stock_analyzer import StandaloneStockAnalyzer

    symbols = list(symbols or StandaloneStockAnalyzer.DEFAULT_STOCKS)
    if not state.try_begin_job("watchlist_refresh", symbols):
        return False
    threading.Thread(
        target=_run_claimed_job, args=(state, analyzer, symbols, "watchlist"), daemon=True
    ).start()
    return True


def start_custom_job(state: AnalysisState, analyzer, symbols) -> bool:
    """Claim the job slot, then analyze `symbols` on a background thread."""
    symbols = list(symbols)
    if not state.try_begin_job("custom_analyze", symbols):
        return False
    threading.Thread(
        target=_run_claimed_job, args=(state, analyzer, symbols, "custom"), daemon=True
    ).start()
    return True


def _run_claimed_job(state: AnalysisState, analyzer, symbols, kind):
    """Body of an already-claimed job. Always releases the slot."""
    try:
        results, errors = _run_symbols(state, analyzer, symbols)
        if kind == "watchlist":
            state.finish_watchlist_job(results, errors)
        else:
            state.finish_custom_job(results, errors, symbols)
    except Exception:
        # _run_symbols swallows per-symbol failures, so reaching here means
        # something unexpected broke. Log it rather than losing it to a bare
        # thread death, then let `finally` clear job_running.
        logger.exception("%s job failed", kind)
    finally:
        # Runs even if the try block above raised somewhere unexpected -
        # without this, job_running could stay stuck at True forever, since
        # only finish_*_job() ever resets it.
        state.end_job()


class RefreshScheduler(WakeableDaemon):
    """Background timer thread that re-runs the watchlist on an interval,
    but only while settings.auto_refresh_enabled is True. Disabled by
    default - the watchlist otherwise only updates on a manual trigger.
    """

    def __init__(self, state: AnalysisState, analyzer, settings: Settings):
        super().__init__()
        self.state = state
        self.analyzer = analyzer
        self.settings = settings

    def _loop(self):
        while not self._stop_event.is_set():
            enabled, interval = self.settings.snapshot()
            if enabled:
                self._tick(interval)
                self._wake_event.wait(timeout=interval)
            else:
                self.state.set_next_scheduled_refresh(None)
                self._wake_event.wait()  # block until a settings change or stop wakes us
            self._wake_event.clear()

    def _tick(self, interval_seconds):
        next_at = datetime.now() + timedelta(seconds=interval_seconds)
        self.state.set_next_scheduled_refresh(next_at.isoformat(timespec="seconds"))
        # start_watchlist_job() claims the slot atomically and reports whether
        # it won, so there's no separate lock.locked() pre-check to race with.
        if not start_watchlist_job(self.state, self.analyzer):
            logger.debug("Scheduled refresh skipped - an analysis is already running")


METRIC_GLOSSARY = {
    "signal": {
        "label": "Signal",
        "description": "STRONG BUY / BUY / HOLD / SELL / STRONG SELL - a bucketed read of the overall score.",
    },
    "score": {
        "label": "Score",
        "description": "Unbounded point total combining technicals, momentum, and news sentiment. Higher = more bullish, lower = more bearish. Drives the signal label.",
    },
    "confidence": {
        "label": "Confidence",
        "description": "0-100%. How strongly the underlying signals agree with each other; higher means the signal is less likely to be noise.",
    },
    "daily_change": {
        "label": "Daily / Weekly / Monthly Change",
        "description": "Percent price change over 1 day, ~5 trading days (1 week), and ~20 trading days (1 month).",
    },
    "rsi": {
        "label": "RSI (Relative Strength Index)",
        "description": "0-100 momentum gauge. Below 30 = oversold (potential buy). Above 70 = overbought (potential sell/reversal). 30-70 = neutral.",
    },
    "macd_trend": {
        "label": "MACD Trend",
        "description": "Compares fast vs. slow moving averages of price. 'Bullish' = short-term momentum is turning up; 'Bearish' = turning down.",
    },
    "sma_10": {
        "label": "SMA-10 / SMA-20 / SMA-50",
        "description": "Simple moving averages over 10/20/50 trading days. Price above all three, in ascending order, signals a strong uptrend; the reverse signals a downtrend.",
    },
    "bb_position": {
        "label": "Bollinger Band Position",
        "description": "0-1 scale: 0 = price at the lower band (possibly oversold), 1 = price at the upper band (possibly overbought), 0.5 = at the middle band.",
    },
    "volume_ratio": {
        "label": "Volume Ratio",
        "description": "Today's volume vs. the 3-month average. Above 1.5x = unusually high volume, adds confidence to a price move. Below 0.5x = low volume, weak signal.",
    },
    "3mo_high": {
        "label": "3-Month High / Low",
        "description": "Highest/lowest closing price observed in the trailing ~3 months of data (not a true 52-week range).",
    },
    "sentiment_score": {
        "label": "News Sentiment Score",
        "description": "-100 (very negative news) to +100 (very positive news), from AI analysis of recent headlines.",
    },
    "market_sentiment": {
        "label": "Market Sentiment",
        "description": "Bullish / bearish / neutral summary label from the AI news analysis.",
    },
    "stop_loss": {
        "label": "Stop Loss",
        "description": "Suggested exit price to cap downside if the trade moves against the signal direction.",
    },
    "target_1": {
        "label": "Target 1 / Target 2",
        "description": "Suggested take-profit price levels if the trade moves in the signal's favor.",
    },
    "risk_reward": {
        "label": "Risk/Reward Ratio",
        "description": "Potential gain to Target 1 divided by potential loss to the stop loss. Above 1 means the upside outweighs the downside.",
    },
}
