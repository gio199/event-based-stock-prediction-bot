"""Small shared helpers used by the analyzer core (standalone_stock_analyzer.py),
the watchlist (web_state.py) and the event feed (event_state.py).

Holds the plumbing all three need in common: environment loading, atomic JSON
persistence under a dedicated data directory, JSON sanitising, the background
thread skeleton, and the shared Gemini HTTP transport.
"""
import json
import logging
import math
import os
import threading
from datetime import datetime
from typing import Any

import requests

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Runtime state lives here rather than beside the source. Override with
# APP_DATA_DIR to keep state outside the checkout entirely.
DATA_DIR = os.environ.get("APP_DATA_DIR") or os.path.join(_BASE_DIR, "data")

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

_dotenv_loaded = False


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def configure_logging(level=logging.INFO):
    """Idempotent console logging for entry points (CLI and server alike).

    A no-op when handlers already exist, so uvicorn's own logging config wins
    when we're running under it.
    """
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(level)


def load_env_once():
    """Load .env exactly once.

    Previously this only ran as an import side effect of
    standalone_stock_analyzer, so anything reading GEMINI_API_KEY depended on
    that module having been imported first. Every key read now goes through
    here instead, which makes the dependency explicit and order-independent.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    # Anchored to this file rather than the process CWD: python-dotenv's
    # default search starts at the working directory, so launching the server
    # from anywhere else silently lost the API key.
    local_env = os.path.join(_BASE_DIR, ".env")
    if os.path.exists(local_env):
        load_dotenv(local_env)
    else:
        load_dotenv()


def get_gemini_api_key() -> str:
    load_env_once()
    return (os.environ.get("GEMINI_API_KEY") or "").strip()


# ------------------------------------------------------------- persistence

def data_path(filename: str) -> str:
    """Absolute path for a state file inside DATA_DIR.

    Migrates a pre-existing repo-root copy on first use so an upgrade doesn't
    silently start from empty dedup/settings state. Falls back to the legacy
    location if the move fails - keeping the old file readable always beats
    losing it.
    """
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except OSError as e:
        logger.warning("Could not create data dir %s (%s); using repo root", DATA_DIR, e)
        return os.path.join(_BASE_DIR, filename)

    target = os.path.join(DATA_DIR, filename)
    legacy = os.path.join(_BASE_DIR, filename)
    if not os.path.exists(target) and os.path.exists(legacy):
        try:
            os.replace(legacy, target)
            logger.info("Migrated %s into %s", filename, DATA_DIR)
        except OSError as e:
            logger.warning("Could not migrate %s (%s); still reading it in place", filename, e)
            return legacy
    return target


def load_json_file(path, on_missing):
    """Load a JSON file, returning on_missing() if it doesn't exist or is
    invalid. Never raises - callers get a safe default instead."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return on_missing()
    except (ValueError, OSError) as e:
        # Distinct from "not there yet": an unreadable/corrupt file means we
        # are about to silently discard real state, which is worth a line in
        # the log rather than looking like a clean first run.
        logger.warning("Could not read %s (%s); falling back to empty state", path, e)
        return on_missing()


def save_json_file(path, data):
    """Atomically write JSON: full write to a temp file, then os.replace().

    A plain open(path, "w") truncates immediately, so a crash mid-dump leaves
    a corrupt file that load_json_file() then reads as empty - which for the
    dedup store means silently discarding every seen id. os.replace() is
    atomic on both Windows and POSIX, so a reader sees either the old file or
    the new one, never a partial one.
    """
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as e:
        logger.warning("Could not save %s: %s", path, e)
        try:
            os.remove(tmp)
        except OSError:
            pass


def json_safe(value: Any) -> Any:
    """Recursively replace non-finite floats (NaN, inf) with None.

    Starlette renders responses with json.dumps(allow_nan=False), so a single
    NaN anywhere in a payload raises and turns the whole endpoint into a 500 -
    taking down every other symbol's perfectly good results with it. yfinance
    does return NaN rows for halted/illiquid tickers, so this is reachable.
    """
    if isinstance(value, float):  # numpy.float64 subclasses float, so it lands here too
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


# ----------------------------------------------------------------- gemini

def gemini_post(model: str, payload: dict, timeout: int):
    """POST to a Gemini generateContent endpoint; returns the raw Response.

    Shared transport for GeminiAIClient (watchlist sentiment) and
    EventGeminiClient (event extraction). Those two deliberately target
    different models so they draw on separate free-tier quota buckets - only
    the key/header/URL plumbing was duplicated, and only that is shared here.

    The key is read per call rather than captured at construction, so a client
    built before .env was loaded still picks it up.
    """
    return requests.post(
        f"{GEMINI_BASE_URL}/{model}:generateContent",
        headers={"Content-Type": "application/json", "X-goog-api-key": get_gemini_api_key()},
        json=payload,
        timeout=timeout,
    )


def gemini_response_text(data: dict) -> str:
    """Pull the generated text out of a Gemini response, '' if absent.

    Walks the nesting defensively - the direct
    data['candidates'][0]['content']['parts'][0]['text'] chain raises
    KeyError/IndexError on a safety block or an empty candidate list, both of
    which are ordinary API responses rather than bugs.
    """
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return parts[0].get("text", "") if parts else ""


# ----------------------------------------------------------------- daemon

class WakeableDaemon:
    """Shared start/stop/notify skeleton for a lock+wake-event background
    thread. Subclasses own their own tick logic via `_loop()` - RefreshScheduler
    and EventPoller differ enough there (enable/disable branching vs. a fixed
    always-on cadence) that unifying it would hurt readability more than the
    duplication it would remove; this only extracts the truly identical
    thread-lifecycle plumbing.
    """

    def __init__(self):
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread = None

    def start(self):
        # Guard against a double start (e.g. a --reload cycle running the
        # lifespan hook twice), which would otherwise orphan the first thread.
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._wake_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def should_stop(self) -> bool:
        """For long-running work inside a tick to poll between steps, so
        shutdown doesn't have to wait out a multi-minute fetch."""
        return self._stop_event.is_set()

    def notify_settings_changed(self):
        """Call after updating settings so the loop re-evaluates immediately
        instead of waiting out whatever it was previously blocked on."""
        self._wake_event.set()

    def stop(self, timeout=10.0):
        """Signal the loop and wait for it to unwind.

        The join matters: without it the daemon thread is killed wherever it
        happened to be at interpreter exit, which for the Playwright source
        means `browser.close()` never runs and a Chromium process is orphaned.
        """
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("%s did not shut down within %.0fs", type(self).__name__, timeout)

    def _loop(self):
        raise NotImplementedError
