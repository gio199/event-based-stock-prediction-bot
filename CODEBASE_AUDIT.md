# Codebase Audit — Event-Based Stock Prediction Bot

**Date:** 2026-08-17
**Scope:** Full repository — 2,230 lines Python, 1,224 lines frontend, build/run scripts, dependency and ignore config.
**Method:** Static read-only review, followed by a remediation pass.

---

## Remediation Status

All findings below were fixed on 2026-08-17 **except one**, noted at the end. The
detailed sections are retained as the record of *why* each change was made.

**Verified by:** 124 tests (`pytest`), a live end-to-end run against Yahoo Finance
and Gemini, and a mutation check — the C2 and C3 fixes were each temporarily
reverted to confirm the new tests fail on the original defects (C3's test
reproduced the exact `KeyError: 'has_news'`).

| Tier | Finding | Status |
|---|---|---|
| High | C1 relative `static` path | Fixed — anchored to `__file__`; verified importing from a foreign CWD |
| High | C2 MACD failure scored bearish | Fixed — three-way branch; `"Neutral"` scores 0 |
| High | C3 failed AI call marked as real sentiment | Fixed — all failure paths funnel through `_unavailable_sentiment()` |
| High | C4 no Gemini backoff | Fixed — capped exponential backoff, 60s → 1h |
| High | C5 silent pending truncation | Fixed — cap raised to 2000, drops now logged |
| High | C6 NaN 500s whole endpoint | Fixed — `dropna` at fetch + `json_safe()` at the boundary |
| High | S1 `javascript:` URLs from feeds | Fixed — `safeHttpUrl()` http(s) allowlist |
| Medium | C7 `202` for dropped jobs | Fixed — `try_begin_job()` makes the claim the check |
| Medium | S5 non-atomic writes | Fixed — temp file + `os.replace()` |
| Medium | P1 unlocked state reads | Fixed — `watchlist_snapshot()` / `custom_snapshot()` |
| Medium | P3 `lru_cache` poisoned by transient errors | Fixed — errors raise past the cache; only real misses are memoised |
| Medium | P4 multi-minute blocking tick; no join | Fixed — `should_stop` polling + `stop()` joins |
| Medium | P5 panel rebuild wipes input | Fixed — in-place text updates, focused controls left alone |
| Medium | P6 multi-worker breakage | Documented — single-worker requirement in module docstring and README |
| Medium | Q2 `sma_50` fallback | Fixed — stays `None`, comparison skipped |
| Medium | Q11 no tests | Fixed — 124 tests across 5 files, no network required |
| Low | Q1, Q3–Q10, Q12–Q14, S2, S4, S6, P2 | Fixed — see per-item detail below |

**Not done — needs your decision:**

- **S3 (unused `OPENAI_API_KEY` in `.env`).** Left in place. Deleting it destroys
  the only copy of a credential that cannot be retrieved after creation, and it
  is your key, not the code's. It is still unreferenced anywhere in the
  codebase, and `.env` remains untracked. Recommendation: delete the line and
  rotate the key if it was ever live.
- **S6 (authentication).** The input-validation half is done — symbols are now
  format-checked, and `limit` / `refresh_interval_seconds` are bounded. No auth
  layer was added: the app is localhost-only by design, and bolting on tokens
  would change the UX without a stated requirement. The README now warns
  explicitly that exposing the bind address requires putting something
  authenticating in front of it.

### Notable structural changes

- **New `data/` directory** for all runtime state, replacing files written into
  the repo root. Existing files (including the 309 KB dedup store, 1,299 seen
  ids) are migrated automatically on first run and were verified intact.
  `APP_DATA_DIR` overrides the location.
- **New `app_util` helpers** shared by all three subsystems: `data_path()`,
  `json_safe()`, `gemini_post()` / `gemini_response_text()`, `configure_logging()`,
  `load_env_once()`.
- **`.env` is now loaded relative to the source file**, not the working
  directory — the same CWD-dependency class as C1, found while verifying it.

---

## 1. Architecture Overview & Map

### Tech stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.12 (`.venv`), Windows-primary with POSIX scripts |
| Web framework | FastAPI + uvicorn, Pydantic models, Starlette `StaticFiles` |
| Data / math | `yfinance` (prices, news, ticker search), `pandas`, `numpy` |
| AI | Google Gemini REST API called directly via `requests` — no SDK |
| Browser automation | Playwright (Chromium, headless) for the X/Twitter source |
| Frontend | Vanilla JS (IIFE, no modules), plain CSS, `<template>` cloning. No build step, no framework, no bundler |
| Persistence | JSON files in the repo root. No database |
| Tests | **None** |

### Entry points

1. **`standalone_stock_analyzer.py`** — the original interactive CLI (`main()` at `:786`). The only file tracked in git out of the current working set.
2. **`web_app.py`** — the ASGI app. Run via `python -m uvicorn web_app:app --host 127.0.0.1 --port 8000`, or the `run_web.bat` / `run_web.sh` wrappers.
3. **`login_x_bot.py`** — one-shot operator utility that opens a visible browser, lets a human log into X, and saves the session to `x_session_state.json`.

### Module map

```
standalone_stock_analyzer.py   ← analysis core; imported as a library, deliberately never modified
  ├─ GeminiAIClient            REST client, free-text sentiment prompt
  ├─ NewsAnalyzer              yfinance news → Gemini → sentiment dict
  └─ StandaloneStockAnalyzer   RSI/MACD/Bollinger/SMA → score → signal → targets
        ▲                    ▲
        │                    │
app_util.py ──────────────┐  │   shared helpers: now_iso, JSON load/save,
  └─ WakeableDaemon       │  │   get_gemini_api_key, WakeableDaemon base class
        ▲                 │  │
        │                 │  │
web_state.py              │  │   watchlist jobs
  ├─ Settings             │  │
  ├─ AnalysisState        │  │
  ├─ run_watchlist_job    │  │
  ├─ run_custom_job       │  │
  ├─ RefreshScheduler ────┘  │
  └─ METRIC_GLOSSARY         │
        ▲                    │
        │            news_sources.py ─── RawItem, GoogleNewsSource,
        │              ▲                 TrumpsTruthSource, XMuskSource,
        │              │                 resolve_ticker, EventGeminiClient
        │        event_state.py          event feed
        │          ├─ EventSettings
        │          ├─ _SeenStore
        │          ├─ EventState
        │          └─ EventPoller
        │              ▲
        └──────────────┴──── web_app.py ──── static/{index.html, app.js, style.css}
```

### Execution flows

**Watchlist / custom analysis (on demand).**
`POST /api/refresh` or `/api/analyze` → check `state.lock.locked()` → spawn a daemon `Thread` → `run_watchlist_job` / `run_custom_job` acquires `state.lock` non-blockingly → `_run_symbols` iterates symbols serially, calling `analyzer.analyze_stock(symbol)` → each call downloads 3 months of history from Yahoo, computes indicators, fetches up to 3 news articles, and makes one Gemini call → results sorted by score → written to `AnalysisState`. The frontend polls `/api/status` every 1 s while a job runs and re-fetches `/api/stocks` when the `last_updated` timestamp changes.

**Event feed (continuous).**
`EventPoller` runs one daemon thread on a 30 s tick. Each tick, for every enabled source whose own interval has elapsed (with capped exponential backoff on failure): fetch new items → `mark_seen()` immediately → on a source's very first check, seed dedup state and skip analysis → otherwise accumulate. All new items across all sources are merged with any previously-failed `pending` items, deduped, persisted, then sent to `EventGeminiClient.analyze_events_batch` in adaptive chunks (3–15 items). Gemini returns company/ticker guesses, which are cross-checked against `yfinance.Search` before being labeled verified. Results land in a bounded in-memory ring (500 events / 7 days) — deliberately not persisted.

**Data models.** Two informal shapes and one dataclass:
- The **analysis result dict** (`standalone_stock_analyzer.py:423-447`) — nested `technical` / `sentiment` / `targets` sub-dicts, produced by the analyzer and consumed unchanged by `app.js:333`. Never validated by a schema.
- The **event dict** (`event_state.py:416-430`) — flat, SHA-256-derived id.
- `RawItem` (`news_sources.py:39-48`) — the only real dataclass; the common currency between sources and the poller.

### Architectural patterns

- **Strangler / additive layering.** The committed CLI is treated as immutable; the web and event subsystems import it as a library. Every new module's docstring restates this. Clean in intent — but it forces the web layer to inherit the CLI's `print()`-to-stdout and silent-fallback habits (findings #24, #2, #3).
- **Shared daemon skeleton.** `WakeableDaemon` (`app_util.py:38`) factors out only thread lifecycle plus a wake `Event`; each subclass owns its `_loop()`. The wake-event pattern lets a settings POST interrupt a sleeping scheduler instead of waiting out the interval. Both loops re-read settings at the top of each iteration, which makes the classic lost-wakeup race benign here.
- **Two-lock state objects.** `AnalysisState` separates `lock` (job mutual exclusion) from `_state_lock` (field consistency). Correct in design — but the HTTP layer bypasses `_state_lock` on two endpoints (finding #14).
- **Module-level singletons** constructed at import time (`web_app.py:42-49`), with `lifespan` only starting/stopping threads. Correct for one worker, wrong for more (finding #19).
- **Fail-soft everywhere.** Nearly every I/O path swallows exceptions and returns a neutral default. This keeps the daemon alive, but it is also the root cause of the two highest-impact correctness bugs: a *failed* computation is rendered as a *confident neutral or bearish* trading signal.

---

## 2. Critical & Major Bugs

### 🔴 C1 — Server crashes on startup unless launched from the repo root
**[web_app.py:200](web_app.py#L200)**

```python
app.mount("/", StaticFiles(directory="static", html=True), name="static")
```

`StaticFiles` resolves `"static"` against the **process working directory**, and raises `RuntimeError: Directory 'static' does not exist` at import time if it isn't there. Every other path in the codebase is anchored to the module file — [`web_state.py:15`](web_state.py#L15), [`event_state.py:34-37`](event_state.py#L34-L37), [`news_sources.py:28`](news_sources.py#L28) all use `os.path.dirname(os.path.abspath(__file__))`. This one is the outlier.

Any launch that isn't `cd`-ed into the repo root — a systemd unit, a Docker `WORKDIR` mismatch, a shortcut, `uvicorn` invoked from a parent directory — fails immediately with no useful diagnostic.

**Fix:** anchor it like every sibling: `StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), html=True)`.

---

### 🔴 C2 — An indicator *failure* is scored as a bearish trading signal
**[standalone_stock_analyzer.py:475-480](standalone_stock_analyzer.py#L475-L480)**, with **[:326-327](standalone_stock_analyzer.py#L326-L327)**

`calculate_macd` returns `"Neutral"` from its exception handler:

```python
except Exception:
    return 0, 0, "Neutral"
```

But `_generate_signal` only tests for the bullish case, and lets everything else fall through to the bearish branch:

```python
if macd_trend == "Bullish":
    score += 20
    reasons.append("[+] MACD bullish crossover")
else:
    score -= 20
    reasons.append("[-] MACD bearish")
```

`"Neutral"` hits the `else`. A MACD computation that *crashed* subtracts 20 points and tells the user "MACD bearish" as a stated fact. Twenty points is the difference between HOLD and SELL at the `-25` boundary ([`:548-551`](standalone_stock_analyzer.py#L548-L551)). Note that `calculate_rsi` and `calculate_bollinger_bands` both handle this correctly, returning genuinely neutral values (50, position 0.5) that score zero — MACD is the only indicator whose failure mode is directional.

**Fix:** make the branch three-way — `elif macd_trend == "Bearish": score -= 20` — and leave `"Neutral"` unscored, matching the other two indicators.

---

### 🔴 C3 — A failed AI call is presented to the user as real sentiment data
**[standalone_stock_analyzer.py:136-138](standalone_stock_analyzer.py#L136-L138)** and **[:236-238](standalone_stock_analyzer.py#L236-L238)**

`analyze_sentiment` has four exit paths. Three of them correctly set `'has_news': False` (the 429 quota path at `:117`, the generic non-200 path at `:127`, and the missing-`candidates` fallthrough). The fourth — the network/exception path — does not:

```python
except Exception as e:
    # Silently fail and use technical analysis only
    return {'sentiment_score': 0, 'confidence': 0, 'summary': 'Sentiment analysis unavailable', 'market_sentiment': 'neutral'}
```

The caller then fills in the gap with the wrong default:

```python
sentiment.setdefault('has_news', True)
```

So on a timeout or connection error, `has_news` becomes `True`. Three things follow:

1. `_generate_signal` ([`:523`](standalone_stock_analyzer.py#L523)) enters the news-sentiment branch on garbage.
2. [`:540-541`](standalone_stock_analyzer.py#L540-L541) appends `[AI] Sentiment analysis unavailable` to `reasons` — an error string rendered in the UI's "Key Factors" list styled as an AI insight ([`app.js:324`](static/app.js#L324) maps the `[AI]` prefix to the `reason-ai` class).
3. [`app.js:380-387`](static/app.js#L380-L387) takes the `sent.has_news` branch and renders "Sentiment Score 0/100 · Market Mood NEUTRAL · Confidence 0%" — indistinguishable from a real neutral reading.

The comment on `:236-237` states the intent exactly right ("Only mark as real AI sentiment if the call actually succeeded — error/quota paths already set has_news: False themselves"). One path just doesn't hold up its end.

**Fix:** add `'has_news': False, 'articles_count': 0` to the exception return, matching the other three paths. The `setdefault` then becomes correct.

---

### 🔴 C4 — Failed Gemini analysis retries every 30 seconds forever, with no backoff
**[event_state.py:373-385](event_state.py#L373-L385)** and **[:387-407](event_state.py#L387-L407)**

Every tick unconditionally reloads the pending queue and re-sends it:

```python
pending = load_pending()
batch = _dedup_by_external_id(pending + newly_fetched)
if not batch:
    return
save_pending(batch)
self._analyze_and_store(batch)
```

Items that fail analysis go back into `still_pending` ([`:394`](event_state.py#L394)) and are retried on the very next 30 s tick — **regardless of why they failed**. Per-source *fetch* failures get proper capped exponential backoff ([`_effective_interval`, :337-339](event_state.py#L337-L339)), but the Gemini path has none.

With a quota-exhausted or invalid API key returning HTTP 429/403, a full pending queue of `MAX_PENDING = 100` items chunks into ~7 HTTP requests, fired **every 30 seconds indefinitely** — roughly 20,000 rejected requests per day against a rate-limited free-tier endpoint. This is exactly the scenario the codebase already worries about: `web_state.py:22-23` explains that auto-refresh is off by default specifically to avoid tripping the free-tier limit, and `news_sources.py:256-258` picks a separate model to isolate quota buckets. Both defenses are undone by an unthrottled retry loop.

The state to fix this is already collected and already displayed — `gemini_health.consecutive_failures` ([`:264-268`](event_state.py#L264-L268)) drives the UI warning banner at [`app.js:204-218`](static/app.js#L204-L218) — it is simply never consulted for scheduling.

**Fix:** gate `_analyze_and_store` on the same backoff shape used for sources — skip the analysis step until `now >= last_failure_at + min(base * 2**failures, cap)`. The banner text already promises "fetched items are queued and will retry"; backoff keeps that promise without the hammering.

---

### 🔴 C5 — Silent, unrecoverable item loss when a tick fetches more than 100 items
**[event_state.py:206-208](event_state.py#L206-L208)**, with **[:356-360](event_state.py#L356-L360)**

```python
def save_pending(items):
    trimmed = items[-MAX_PENDING:]
    save_json_file(EVENT_PENDING_FILE, [item.__dict__ for item in trimmed])
```

Items are marked seen *before* they are persisted:

```python
items = source.fetch_new_items(seen)
self.seen_store.mark_seen(items)      # ← now permanently excluded from re-fetch
```

So a fetched item has exactly two possible homes: the pending file, or the analyzed event list. The silent `[-MAX_PENDING:]` truncation removes the first without providing the second. If the process dies during the Gemini call, everything past the newest 100 is gone — not re-discoverable (already seen), not queued (trimmed away).

This isn't hypothetical volume. The module's own comment at [`:25-31`](event_state.py#L25-L31) documents that "GoogleNewsSource alone can return 1000+ items in a single check" and cites a prior bug where "702 items falsely reappeared as new." A 702-item tick would silently discard 602 of them.

The `save_pending(batch)` call at [`:384`](event_state.py#L384) carries a five-line comment explaining that this write exists precisely to prevent silent loss. The truncation inside the function it calls defeats that.

**Fix:** raise `MAX_PENDING` well above a realistic single-tick volume, and — more importantly — `log`/record a warning when truncation actually drops items, so the loss is never silent. Trimming the *newest* items (`[-N:]` keeps the tail) is also backwards if the intent is to process oldest-first.

---

### 🟠 C6 — A single NaN returns HTTP 500 for the *entire* watchlist endpoint
**[standalone_stock_analyzer.py:433-443](standalone_stock_analyzer.py#L433-L443)** → **[web_app.py:110-116](web_app.py#L110-L116)**

Starlette's `JSONResponse` serializes with `allow_nan=False` (verified in the installed package, `.venv/Lib/site-packages/starlette/responses.py:198`). Any `float('nan')` reaching the response raises `ValueError: Out of range float values are not JSON compliant` — a 500 for the whole payload.

`analyze_stock` guards `len(df) < 20` and handles the division-by-zero cases, but does not guard against **NaN values inside an adequately-long price series**, which yfinance does return for halted, illiquid, or newly-listed tickers. A NaN in `Close` propagates through `rolling().mean()` into `sma_10` / `sma_20` / `sma_50` ([`:380-382`](standalone_stock_analyzer.py#L380-L382)) and through `calculate_bollinger_bands` into `targets` (`min(nan, x)` returns `nan` in Python). `round(nan, 2)` is still `nan`.

The blast radius is what makes this notable: the per-symbol error path at [`:449-450`](standalone_stock_analyzer.py#L449-L450) is designed so one bad symbol degrades into a listed error while the other eleven render fine. A NaN bypasses that design entirely — one bad symbol in a custom Analyze request takes down the response for **all** of them, and the frontend's `fetchJSON` surfaces only a bare status message.

**Fix:** sanitize at the boundary — a small `_clean(v)` that maps non-finite floats to `None` before the result dict is returned, or `df = df.dropna(subset=['Close'])` right after the fetch. Either keeps a bad symbol on the intended per-symbol error path.

---

### 🟠 C7 — `202 Accepted` returned for jobs that silently never run
**[web_app.py:172-180](web_app.py#L172-L180)** and **[:183-196](web_app.py#L183-L196)**, with **[web_state.py:186-187](web_state.py#L186-L187)**

Both endpoints check, then act, without holding anything in between:

```python
if state.lock.locked():
    raise HTTPException(status_code=409, ...)
threading.Thread(target=run_watchlist_job, args=(state, analyzer), daemon=True).start()
return {"started": True, "job_type": "watchlist_refresh", ...}
```

The spawned job then tries a non-blocking acquire and **returns silently** if it loses:

```python
if not state.lock.acquire(blocking=False):
    return
```

Between the `locked()` check and the acquire, the `RefreshScheduler` tick ([`web_state.py:238-242`](web_state.py#L238-L242)) or a concurrent request can take the lock. The API has already responded `202 {"started": true}`. The job evaporates without touching any state, so `watchlist_last_updated` / `custom_completed_at` never change, and the frontend's polling loop ([`app.js:522-527`](static/app.js#L522-L527)) waits for a timestamp change that will never come. The user sees a button that did nothing.

The window is small but reachable — the scheduler's own `_tick` uses the identical check-then-act shape, so an auto-refresh firing at the same moment as a manual click is the natural trigger.

**Fix:** move the decision into the job. Have `run_*_job` acquire the lock and *report* the outcome, or have the endpoint acquire the lock itself and hand ownership to the thread. A `202` should never be returned for work that was dropped.

---

## 3. Security & Performance Findings

### Security

#### 🟠 S1 — `javascript:` URLs from third-party feeds are rendered as clickable links
**[static/app.js:256-258](static/app.js#L256-L258)**

```javascript
const link = card.querySelector(".event-link");
if (event.link) link.href = event.link;
```

`event.link` originates in `<link>` elements of externally-controlled RSS feeds ([`news_sources.py:57`](news_sources.py#L57)) — `news.google.com` and, notably, `trumpstruth.org`, which the README itself flags as "an independent third party [that] could break or disappear without notice." No scheme validation happens anywhere between the feed and the DOM.

A `javascript:` URL in a feed's `<link>` executes in the page origin on click. `target="_blank"` and `rel="noopener noreferrer"` ([`index.html:197`](static/index.html#L197)) do not prevent this — they govern window/referrer isolation, not scheme handling. In this app the origin is `127.0.0.1:8000`, which means script execution with full access to the settings-mutation endpoints.

**Fix:** allowlist the scheme before assignment — parse with `new URL(event.link, location.origin)` and assign only when the protocol is `http:` or `https:`, otherwise hide the link as the `else` branch already does.

#### 🟡 S2 — `innerHTML` template with unescaped model-derived values
**[static/app.js:381-390](static/app.js#L381-L390)**

The sentiment block interpolates `sent.sentiment_score`, `sent.market_sentiment`, `sent.confidence`, and `sent.articles_count` directly into an `innerHTML` string, while correctly wrapping only `sent.summary` in `escapeHtml`. Today those four are safe by construction: `_parse_sentiment` coerces two through `int()` ([`:157`](standalone_stock_analyzer.py#L157), [`:165`](standalone_stock_analyzer.py#L165)) and constrains `market_sentiment` to three literals ([`:170-175`](standalone_stock_analyzer.py#L170-L175)). But that safety lives in a *different file, in a different language*, in a parser explicitly noted below as brittle (#Q4). If the parser is ever loosened to pass a string through, this becomes stored XSS with no local signal that anything changed.

**Fix:** build the block with `textContent` like the rest of the codebase does, or escape all interpolations uniformly.

#### 🟡 S3 — Unused `OPENAI_API_KEY` sitting in `.env`
`.env` contains an `OPENAI_API_KEY` with **zero references** anywhere in the repository — verified by grepping all `.py`, `.js`, and `.md` files. A live credential on disk that no code path needs is pure downside: it widens the blast radius of any file disclosure while providing nothing.

**Fix:** remove it, and rotate it if it was ever real.

*Positive finding:* `.env` is correctly ignored and confirmed **untracked** (`git ls-files --error-unmatch .env` → not found). No secret is in git history.

#### 🟡 S4 — Blanket `*.json` ignore rule
**[.gitignore](.gitignore)**

The rule does its critical job — `x_session_state.json` holds a live X session cookie that `login_x_bot.py:10-12` correctly describes as password-equivalent, and it is excluded. But `*.json` with no negation is indiscriminate: any future `package.json`, `tsconfig.json`, schema file, or fixture will be silently untracked, and the failure mode is a teammate cloning a repo that doesn't run.

**Fix:** ignore the specific state files by name (`x_session_state.json`, `event_seen.json`, `event_pending.json`, `*_settings.json`), or keep the blanket rule with explicit `!` negations for config that must be committed.

#### 🟡 S5 — Non-atomic writes can silently erase all dedup state
**[app_util.py:29-35](app_util.py#L29-L35)**

```python
def save_json_file(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass
```

Opening with `"w"` truncates immediately; a crash, power loss, or kill mid-`dump` leaves a truncated file. `load_json_file` then catches the `ValueError` and returns `{}` — silently. For `event_seen.json` (currently **309 KB**, ~5,000 ids per source) the consequence chain is: dedup state lost → `is_seeded()` returns `False` ([`:148-154`](event_state.py#L148-L154)) → the poller treats the next check as a cold start → the entire fetched backlog is **discarded without analysis** ([`:362-368`](event_state.py#L362-L368)). Days of feed history vanish with no error surfaced anywhere.

**Fix:** write to `path + ".tmp"` then `os.replace()` — atomic on both Windows and POSIX. Worth a one-line log on the `except` paths too; `pass` on a save failure is silent by design and shouldn't be.

#### 🟢 S6 — No authentication or rate limiting on mutating endpoints
`POST /api/settings`, `/api/events/sources/{name}`, `/api/refresh`, and `/api/analyze` are unauthenticated. Localhost binding is enforced by *convention* — the run scripts pass `--host 127.0.0.1`, and the README states it as a property — but nothing in `web_app.py` requires it. A user who runs `uvicorn web_app:app --host 0.0.0.0` to reach the dashboard from a phone exposes full settings control plus an unmetered request amplifier: [`:185-189`](web_app.py#L185-L189) accepts up to 25 arbitrary symbols with no format validation, each triggering a Yahoo Finance fetch and a Gemini call against the user's quota.

Given the stated local-only design this is a hardening item, not a live vulnerability — but it becomes one the moment the README's Cloudflare hosting plan ([README:53](README.md#L53)) is acted on.

**Fix:** validate symbol format (`^[A-Z0-9.\-]{1,10}$`), and add a token check gated on a non-loopback bind.

### Performance & Concurrency

#### 🟠 P1 — Two endpoints bypass the state lock the rest of the class holds
**[web_app.py:110-126](web_app.py#L110-L126)**

`AnalysisState` is carefully built around `_state_lock`, and its docstring ([`web_state.py:72-74`](web_state.py#L72-L74)) states the invariant: "`/api/status` never observes a half-updated snapshot while a job thread is writing." `status_snapshot()` honors it. These two endpoints do not:

```python
@app.get("/api/stocks")
def get_stocks():
    return {
        "results": state.watchlist_results,
        "errors": state.watchlist_errors,
        "last_updated": state.watchlist_last_updated,
    }
```

Three unsynchronized reads against `finish_watchlist_job`'s three locked writes ([`web_state.py:113-121`](web_state.py#L113-L121)). The GIL prevents corruption of any individual reference, but nothing prevents the reader from interleaving mid-update and returning **new results paired with the previous run's errors** — precisely the half-updated snapshot the class was designed to prevent. The frontend calls this endpoint immediately after noticing a timestamp change ([`app.js:522-524`](static/app.js#L522-L524)), i.e. right at the moment the writer was active.

**Fix:** add `results_snapshot()` / `custom_snapshot()` methods on `AnalysisState` that read under `_state_lock`, mirroring `status_snapshot()`.

#### 🟡 P2 — Poller reads source health outside the state lock
**[event_state.py:337-346](event_state.py#L337-L346)**

`_effective_interval` and `_is_due` reach into `self.state.source_health[name][...]` directly, while every other accessor in `EventState` — including the `record_check_failure` that mutates `consecutive_failures` ([`:252-257`](event_state.py#L252-L257)) — holds `self._lock`. Low practical impact (single reader thread, GIL-atomic dict reads), but it breaks the class's stated encapsulation and is the kind of exception that gets copied.

**Fix:** expose a small locked `source_status(name)` accessor and use it.

#### 🟡 P3 — `lru_cache` permanently caches transient failures
**[news_sources.py:213-241](news_sources.py#L213-L241)**

`_resolve_ticker_cached` swallows every exception and returns `None`:

```python
except Exception:
    pass
return None
```

Because `lru_cache` memoizes return values without distinguishing "no match" from "the network was down," a single transient yfinance error **poisons that (ticker, company) key for the entire process lifetime**. Every subsequent event about that company is marked `ticker_verified: False` and rendered with the "unverified" badge ([`app.js:248`](static/app.js#L248)) — for a long-running server, potentially forever, with no way to clear it short of a restart.

The docstring correctly reasons about bounding the cache; it just doesn't separate the two failure modes.

**Fix:** catch network/transient exceptions separately and re-raise or return a sentinel that isn't cached — e.g. do the lookup in an uncached inner function and only memoize successful resolutions.

#### 🟡 P4 — A single tick can block the poller thread for minutes, and shutdown never joins
**[news_sources.py:101-121](news_sources.py#L101-L121)**, **[app_util.py:61-63](app_util.py#L61-L63)**

`GoogleNewsSource.fetch_new_items` loops 12+ queries with a 15 s timeout and a 1.2 s sleep each — a worst case near **195 seconds inside one `_tick()`**, on the single thread that also serves the other two sources. Add `XMuskSource`'s full Chromium launch (30 s navigation + 15 s selector wait) and `analyze_events_batch`'s chunked calls with `time.sleep(1.0)` between them, and the nominal 30 s tick cadence becomes fictional under load.

Compounding it, `stop()` only sets two `Event`s and never joins:

```python
def stop(self):
    self._stop_event.set()
    self._wake_event.set()
```

Neither long fetch checks `_stop_event`, so there is no cooperative cancellation. On shutdown the daemon thread is killed mid-operation at interpreter exit — which for `XMuskSource` can leave the Chromium subprocess orphaned, since `browser.close()` in the `finally` never runs.

**Fix:** pass `self._stop_event` into fetches and check it between queries; have `stop()` join with a timeout so Playwright teardown gets a chance to complete.

#### 🟡 P5 — Source-health panel wipes the user's input every 4 seconds
**[static/app.js:531-534](static/app.js#L531-L534)** and **[:220-228](static/app.js#L220-L228)**

The status poll re-renders the whole panel whenever the Event Feed tab is visible:

```javascript
const eventsPanel = document.getElementById("panel-events");
if (eventsPanel && !eventsPanel.hidden) {
  await loadEventSources();
}
```

and `loadEventSources` does `panel.innerHTML = ""` before rebuilding every row from the template. The rows contain a `<input type="number">` for the polling interval ([`index.html:214`](static/index.html#L214)). Typing a two-digit interval takes longer than the 4 s poll, so the input is destroyed and recreated mid-edit — losing focus, the caret, and any partially-typed value. The comment says the goal is to "keep source health/timestamps live"; that only needs the text nodes updated, not the controls replaced.

**Fix:** update the existing rows in place (text and badges only) and rebuild the DOM only when the set of sources actually changes.

#### 🟡 P6 — Module-level singletons break under multiple workers or `--reload`
**[web_app.py:42-49](web_app.py#L42-L49)**

`analyzer`, `state`, `settings`, `scheduler`, `event_state`, `event_settings`, and `event_poller` are all constructed at import. With `uvicorn --workers 2`, each process gets its own copy: two `EventPoller`s doubling every outbound API call, two `_SeenStore`s racing to write the same `event_seen.json` non-atomically (see S5), and `/api/settings` results that flip depending on which worker answers. `RefreshScheduler.start()` also assigns a fresh `self._thread` on each call without joining the old one, so a `--reload` cycle that runs `lifespan` twice leaks a thread.

**Fix:** document single-worker as a requirement in the README and run scripts, or move the pollers behind a lock file / separate process if multi-worker is ever wanted.

---

## 4. Refactoring & Code Quality Suggestions

#### Q1 — Dead branch, and an off-by-one in the "week" window
**[standalone_stock_analyzer.py:373-374](standalone_stock_analyzer.py#L373-L374)**

```python
week_ago_price = df['Close'].iloc[-5] if len(df) >= 5 else prev_close
month_ago_price = df['Close'].iloc[-20] if len(df) >= 20 else prev_close
```

`:364` already returns early unless `len(df) >= 20`, so both `else` branches are unreachable. Separately, `iloc[-5]` is **4** trading days back, not 5 — the glossary ([`web_state.py:259-260`](web_state.py#L259-L260)) advertises "~5 trading days (1 week)". `iloc[-6]` / `iloc[-21]` would match the documented semantics.

#### Q2 — `sma_50` fallback makes the strongest bullish signal unreachable
**[standalone_stock_analyzer.py:382](standalone_stock_analyzer.py#L382)**

```python
sma_50 = df['Close'].rolling(window=50).mean().iloc[-1] if len(df) >= 50 else sma_20
```

When history is short, `sma_50` becomes exactly `sma_20`. The top-tier condition at [`:483`](standalone_stock_analyzer.py#L483) is a *strict* chain — `price > sma_10 > sma_20 > sma_50` — so `sma_20 > sma_20` is `False` and the check can never pass. A recent IPO in a textbook uptrend silently scores +20 instead of +30, and reports "Uptrend" rather than "Strong uptrend," with nothing indicating the metric was substituted. Better to propagate `None` and skip the 50-day comparison explicitly.

#### Q3 — Duplicated Gemini client conventions
`GeminiAIClient` ([`standalone_stock_analyzer.py:50-59`](standalone_stock_analyzer.py#L50-L59)) and `EventGeminiClient` ([`news_sources.py:244-264`](news_sources.py#L244-L264)) independently reimplement the same key-reading, header-building, and base-URL construction. `standalone_stock_analyzer.py:54` also re-implements `app_util.get_gemini_api_key()` inline. The two clients *should* differ in model and prompting — that separation is deliberate and well-reasoned — but the transport boilerplate is copy-paste. A shared `_gemini_post(model, payload)` helper would remove it without collapsing the intentional distinction.

#### Q4 — Brittle free-text response parser, when a JSON mode already exists in-repo
**[standalone_stock_analyzer.py:140-179](standalone_stock_analyzer.py#L140-L179)**

`_parse_sentiment` matches on literal substrings (`'SENTIMENT_SCORE:' in line`) against a free-text LLM response. Any deviation — markdown bolding (`**SENTIMENT_SCORE**:`), a leading bullet, a reworded label — silently yields all zeros, which then flows into scoring as genuine neutral sentiment. `EventGeminiClient` already solves this properly with `"responseMimeType": "application/json"` ([`news_sources.py:311`](news_sources.py#L311)) and a declared response shape. Applying the same to `analyze_sentiment` would delete the parser entirely.

#### Q5 — Library code prints to stdout from background threads
**[standalone_stock_analyzer.py:400-411](standalone_stock_analyzer.py#L400-L411)**, **[:287-293](standalone_stock_analyzer.py#L287-L293)**

`analyze_stock` writes progress with `print(..., end=' ')`. Fine for the CLI it was written for; in the web app it runs on job threads, so partial lines from concurrent work interleave into uvicorn's log with no timestamp or level. Routing through `logging` would preserve the CLI output (a `StreamHandler` at the entry point) while making server logs parseable.

#### Q6 — Environment loading depends on implicit import order
**[standalone_stock_analyzer.py:27-31](standalone_stock_analyzer.py#L27-L31)**

`load_dotenv()` runs as an import side-effect of the analyzer module only. `app_util.get_gemini_api_key()` reads `os.environ` and works purely because every current import chain happens to pull in `standalone_stock_analyzer` first. Nothing enforces that. Combined with `EventGeminiClient.__init__` snapshotting the key once at construction ([`news_sources.py:255`](news_sources.py#L255)), a future import reordering yields a silently key-less client whose only symptom is "no GEMINI_API_KEY" markers on every event. Call `load_dotenv()` explicitly at each entry point (`web_app.py`, `login_x_bot.py`), or inside `get_gemini_api_key()`.

#### Q7 — Unvalidated `limit` query parameter
**[web_app.py:137](web_app.py#L137)** — `limit: int = 100` accepts negatives; `events[:limit]` with `limit=-1` returns "all but the newest," which is a confusing response rather than an error. `Query(100, ge=1, le=500)` is the one-line fix.

#### Q8 — Null guard placed after the dereference it protects
**[static/app.js:267-276](static/app.js#L267-L276)** — `data.events[0]` is read on line 267; the `if (!data.events || ...)` guard is on line 270. Harmless given the current API contract, but inverted.

#### Q9 — Cold-start check reports items it never analyzes
**[event_state.py:361-368](event_state.py#L361-L368)** — `record_check_success(name, len(items))` runs *before* the `continue` that skips a first check's backlog. The UI then shows "702 new last check" ([`app.js:153`](static/app.js#L153)) while zero events appear, with no explanation. Reporting `0` on the seeding pass, or adding a "seeded" status, would match what actually happened.

#### Q10 — Redundant parsing and inconsistent key access
**[event_state.py:277-282](event_state.py#L277-L282)** — `_prune_locked` calls `_safe_parse` twice per event per prune, and mixes `e.get("detected_at")` with `e["detected_at"]` on the same key in a single expression. Parse once into a local.

#### Q11 — No test coverage at all
No test files, no `pytest.ini`, no `pyproject.toml`, no CI. This is the highest-leverage gap in the report: the pure, dependency-free functions are exactly the ones where the correctness bugs live.

Best candidates, all testable without network access:

| Target | Why it matters |
|---|---|
| `_generate_signal` | Would have caught C2 (`"Neutral"` → −20) with one parametrized case |
| `NewsAnalyzer.get_news_sentiment` | Would have caught C3 by asserting `has_news is False` on every failure path |
| `_calculate_targets` | Money math; three branches, zero coverage |
| `_SeenStore.mark_seen` / ring eviction | The subsystem's core invariant; the module comment cites a real 702-item bug here |
| `EventGeminiClient._analyze_chunk` | Index-mapping via `by_index` is easy to get subtly wrong |
| `_parse_rss_items` | Pure `bytes → List[RawItem]`; feed-shape drift is the expected failure mode |
| `_parse_sentiment` | Q4's brittleness becomes visible the moment it's tested |

#### Q12 — Unpinned dependencies against a known-unstable API
**[requirements.txt](requirements.txt)** — all eight entries use `>=` with no upper bound and no lockfile. `yfinance` in particular changes response shapes between minor releases; the code already carries a compatibility shim for exactly that ([`standalone_stock_analyzer.py:222-224`](standalone_stock_analyzer.py#L222-L224), handling the `content` nesting change). Add upper bounds (`yfinance>=0.2,<0.3`) or commit a lockfile.

#### Q13 — Runtime state written into the source tree
`event_seen.json` (309 KB), `event_pending.json`, `web_settings.json`, `event_settings.json`, and `x_session_state.json` are all written to the repo root. A `data/` directory (with the path anchored via `_BASE_DIR`, as the constants at [`event_state.py:34-37`](event_state.py#L34-L37) already do) would separate code from state, simplify the `.gitignore` situation in S4, and make backup/reset a single `rm -rf`.

#### Q14 — Typos on the repo's front page
**[README.md:11](README.md#L11)** — "repo contaion", "we plant to add", "event based traiding". Cosmetic, but it's the first paragraph a reader sees.

---

## 5. Prioritized Action Plan

### High — fix first

| # | Finding | File | Why it's first |
|---|---|---|---|
| 1 | Relative `static` path | [web_app.py:200](web_app.py#L200) | Hard startup crash; one-line fix |
| 2 | MACD failure scored bearish | [standalone_stock_analyzer.py:475](standalone_stock_analyzer.py#L475) | Produces a **wrong trading signal** from an internal error |
| 3 | Failed AI call marked `has_news: True` | [standalone_stock_analyzer.py:136](standalone_stock_analyzer.py#L136) | Presents failure as data; ~4-word fix |
| 4 | No Gemini backoff | [event_state.py:373](event_state.py#L373) | Burns the API quota the rest of the design works to protect |
| 5 | `MAX_PENDING` silent truncation | [event_state.py:206](event_state.py#L206) | Unrecoverable data loss at documented volumes |
| 6 | NaN → 500 on whole endpoint | [standalone_stock_analyzer.py:433](standalone_stock_analyzer.py#L433) | One bad symbol breaks all results |
| 8 | `javascript:` URLs from feeds | [static/app.js:257](static/app.js#L257) | Only externally-reachable code-execution path |

Items 2, 3, and 6 share a root cause worth addressing as one pass: **failure paths are indistinguishable from valid neutral results.** A consistent convention — failures are `None`/absent, never zero — would close all three and prevent the next one.

### Medium — fix next

| # | Finding | File |
|---|---|---|
| 7 | `202` for silently dropped jobs | [web_app.py:172](web_app.py#L172) |
| S5 | Non-atomic JSON writes | [app_util.py:29](app_util.py#L29) |
| P1 | Unlocked state reads on 2 endpoints | [web_app.py:110](web_app.py#L110) |
| P3 | `lru_cache` poisoned by transient errors | [news_sources.py:213](news_sources.py#L213) |
| P4 | Multi-minute tick blocking; `stop()` never joins | [news_sources.py:101](news_sources.py#L101), [app_util.py:61](app_util.py#L61) |
| P5 | Panel rebuild wipes input mid-edit | [static/app.js:531](static/app.js#L531) |
| P6 | Singletons break multi-worker | [web_app.py:42](web_app.py#L42) |
| Q2 | `sma_50` fallback blocks the +30 signal | [standalone_stock_analyzer.py:382](standalone_stock_analyzer.py#L382) |
| Q11 | **Establish test scaffolding** — start with `_generate_signal`, `get_news_sentiment`, `_SeenStore` | — |

Q11 belongs at the top of this tier in practice: adding pytest plus the first three test targets locks in every High-tier fix and makes the rest safe to change.

### Low — cleanup

`S2` (innerHTML escaping) · `S3` (remove unused `OPENAI_API_KEY`) · `S4` (narrow the `*.json` ignore) · `S6` (symbol validation + auth-on-non-loopback) · `P2` (locked health accessor) · `Q1` (dead branch, week off-by-one) · `Q3` (shared Gemini transport) · `Q4` (JSON response mode) · `Q5` (`logging` over `print`) · `Q6` (explicit `load_dotenv`) · `Q7` (`limit` bounds) · `Q8` (guard ordering) · `Q9` (cold-start count) · `Q10` (double parse) · `Q12` (pin dependencies) · `Q13` (`data/` directory) · `Q14` (README typos)

---

### Closing note

The newer modules are markedly more careful than the code they wrap — explicit lock ownership, documented invariants, comments that record *why* a constant has its value (the `SEEN_RING_SIZE` note citing a real 702-item bug is a good example of institutional memory in a comment). Most High-tier findings are not carelessness but a single inherited convention: **the original CLI's fail-soft-and-return-zero habit, carried into a context where zero is a meaningful trading signal rather than a harmless placeholder.** Fixing that convention once, at the boundary, resolves the majority of the severe findings and is cheaper than fixing each downstream symptom.
