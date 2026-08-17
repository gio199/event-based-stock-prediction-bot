(() => {
  "use strict";

  let glossary = null;
  let lastWatchlistUpdated = null;
  let lastCustomCompleted = null;
  let lastEventsDetectedAt = null;
  let lastPostsDetectedAt = null;
  let postSourceFilter = "";  // "" = all social sources

  // The backend keeps 200; the panel deliberately shows only the newest few so
  // the tab stays scannable rather than turning into an endless wall of text.
  const POSTS_VISIBLE = 5;
  let cachedSettings = { auto_refresh_enabled: false, refresh_interval_seconds: 900 };
  let xScraperConfigured = false;

  const SOURCE_META = {
    google_news: { label: "Google News", badgeClass: "source-badge-google_news" },
    trumpstruth: { label: "Truth Social (trumpstruth.org mirror)", badgeClass: "source-badge-trumpstruth" },
    x_musk: { label: "X / Twitter (@elonmusk)", badgeClass: "source-badge-x_musk" },
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // Live source-health rows, keyed by source name, so the status poll can
  // refresh their text without tearing down the controls the user is editing.
  const sourceRows = new Map();

  // Feed <link> values are third-party content (Google News, trumpstruth.org).
  // Assigning a "javascript:" URL to href executes it in this origin on click;
  // target="_blank" and rel="noopener" do not prevent that. Only http(s) URLs
  // are allowed through.
  function safeHttpUrl(raw) {
    if (!raw) return null;
    try {
      const url = new URL(raw, window.location.origin);
      return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
    } catch (_) {
      return null;
    }
  }

  // "<label> <strong><value></strong>" without innerHTML.
  function labelledValue(el, label, value) {
    el.textContent = `${label} `;
    const strong = document.createElement("strong");
    strong.textContent = value;
    el.appendChild(strong);
  }

  // Build a label/value row as DOM nodes. Values here are model-derived, so
  // they never go through innerHTML - textContent cannot be coerced into markup.
  function metricRow(label, value, metricKey) {
    const row = document.createElement("div");
    row.className = "metric-row";
    if (metricKey) row.dataset.metric = metricKey;
    const labelEl = document.createElement("span");
    labelEl.textContent = label;
    const valueEl = document.createElement("span");
    valueEl.textContent = value;
    row.append(labelEl, valueEl);
    return row;
  }

  async function fetchJSON(url, options) {
    const res = await fetch(url, options);
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = body.detail?.message || body.detail || detail;
      } catch (_) { /* ignore parse failure */ }
      const err = new Error(detail);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  // ---------- Tabs ----------
  function setupTabs() {
    $$(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        $$(".tab-btn").forEach((b) => {
          b.classList.remove("is-active");
          b.setAttribute("aria-selected", "false");
        });
        btn.classList.add("is-active");
        btn.setAttribute("aria-selected", "true");

        $$(".tab-panel").forEach((p) => {
          p.classList.remove("is-active");
          p.hidden = true;
        });
        const panel = document.getElementById(`panel-${btn.dataset.tab}`);
        panel.classList.add("is-active");
        panel.hidden = false;
      });
    });
  }

  // ---------- Config / settings / glossary ----------
  let minRefreshIntervalSeconds = 60;

  async function loadConfig() {
    const badge = $("#gemini-badge");
    try {
      const cfg = await fetchJSON("/api/config");
      $("#custom-max").textContent = cfg.max_custom_symbols;
      minRefreshIntervalSeconds = cfg.min_refresh_interval_seconds;
      $("#refresh-interval-input").min = Math.ceil(minRefreshIntervalSeconds / 60);
      $("#min-interval-minutes").textContent = Math.ceil(minRefreshIntervalSeconds / 60);
      xScraperConfigured = !!cfg.x_scraper_configured;
      if (cfg.gemini_configured) {
        badge.textContent = "AI News Sentiment: Enabled";
        badge.className = "badge badge-good";
      } else {
        badge.textContent = "AI News Sentiment: Disabled (set GEMINI_API_KEY)";
        badge.className = "badge badge-muted";
      }
    } catch (e) {
      badge.textContent = "Config unavailable";
      badge.className = "badge badge-muted";
    }
  }

  function renderAutoRefreshMeta(settingsData, nextScheduledAt) {
    const el = $("#watchlist-next");
    if (!settingsData.auto_refresh_enabled) {
      el.textContent = "Auto-refresh: off";
      return;
    }
    const mins = Math.round(settingsData.refresh_interval_seconds / 60);
    el.textContent = nextScheduledAt
      ? `Auto-refresh: every ${mins} min (next: ${nextScheduledAt})`
      : `Auto-refresh: every ${mins} min`;
  }

  async function loadSettings() {
    const data = await fetchJSON("/api/settings");
    cachedSettings = data;
    $("#auto-refresh-toggle").checked = data.auto_refresh_enabled;
    $("#refresh-interval-input").value = Math.round(data.refresh_interval_seconds / 60);
    $("#interval-row").classList.toggle("is-disabled", !data.auto_refresh_enabled);
    $("#refresh-interval-input").disabled = !data.auto_refresh_enabled;
    renderAutoRefreshMeta(data, null);
    return data;
  }

  async function saveSettings() {
    const auto_refresh_enabled = $("#auto-refresh-toggle").checked;
    const minutes = parseInt($("#refresh-interval-input").value, 10);
    const refresh_interval_seconds = Number.isFinite(minutes)
      ? Math.max(minRefreshIntervalSeconds, minutes * 60)
      : undefined;
    try {
      const data = await fetchJSON("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ auto_refresh_enabled, refresh_interval_seconds }),
      });
      cachedSettings = data;
      $("#refresh-interval-input").disabled = !data.auto_refresh_enabled;
      renderAutoRefreshMeta(data, null);
      const saved = $("#settings-saved");
      saved.hidden = false;
      setTimeout(() => { saved.hidden = true; }, 2000);
    } catch (e) {
      showTransientNotice($("#settings-saved"), e.message || "Failed to save");
    }
  }

  // ---------- Event Feed ----------
  function sourceStatusBadge(name, health) {
    if (!health.enabled) return { text: "Disabled", cls: "badge-muted" };
    if (health.consecutive_failures >= 3) return { text: "Broken", cls: "badge-critical" };
    if (health.consecutive_failures > 0) return { text: "Degraded", cls: "badge-muted" };
    if (!health.last_success_at) return { text: "Waiting for first check", cls: "badge-muted" };
    return { text: "OK", cls: "badge-good" };
  }

  function buildSourceHealthRow(name, health) {
    const tpl = document.getElementById("source-health-row-template");
    const node = tpl.content.cloneNode(true);
    const row = node.querySelector(".source-health-row");
    const meta = SOURCE_META[name] || { label: name };

    row.querySelector(".source-name-label").textContent = meta.label;

    if (name === "x_musk" && !xScraperConfigured) {
      const hint = document.createElement("p");
      hint.className = "hint";
      hint.textContent = 'Run "python login_x_bot.py" once (see README) before enabling this source.';
      row.querySelector(".settings-row-text").appendChild(hint);
    }

    const toggle = row.querySelector(".source-enabled-toggle");
    const intervalInput = row.querySelector(".source-interval-input");
    const save = () => saveEventSource(name, toggle.checked, intervalInput.value);
    toggle.addEventListener("change", () => {
      intervalInput.disabled = !toggle.checked;
      save();
    });
    intervalInput.addEventListener("change", () => {
      if (toggle.checked) save();
    });

    const entry = { row, toggle, intervalInput };
    sourceRows.set(name, entry);
    updateSourceHealthRow(entry, name, health);
    return row;
  }

  // Refreshes only the text/badges. The status poll runs every 4s while this
  // tab is open, and rebuilding the rows each time destroyed the interval
  // input mid-edit - a two-digit value could not be typed before its element
  // was replaced. Form controls are therefore left alone whenever the user is
  // interacting with them.
  function updateSourceHealthRow(entry, name, health) {
    const { row, toggle, intervalInput } = entry;

    const statusBadge = row.querySelector(".source-status-badge");
    const status = sourceStatusBadge(name, health);
    statusBadge.textContent = status.text;
    statusBadge.className = `badge source-status-badge ${status.cls}`;

    const metaParts = [`Checks every ${Math.round(health.interval_seconds / 60)} min`];
    if (health.last_checked_at) metaParts.push(`last checked ${health.last_checked_at}`);
    if (health.last_new_items_count) metaParts.push(`${health.last_new_items_count} new last check`);
    row.querySelector(".source-meta-line").textContent = metaParts.join(" · ");

    const errorLine = row.querySelector(".source-error-line");
    errorLine.hidden = !health.last_error;
    errorLine.textContent = health.last_error || "";

    if (document.activeElement !== toggle) toggle.checked = health.enabled;
    if (document.activeElement !== intervalInput) {
      intervalInput.value = Math.round(health.interval_seconds / 60);
      intervalInput.disabled = !health.enabled;
    }
  }

  async function saveEventSource(name, enabled, minutesStr) {
    const minutes = parseInt(minutesStr, 10);
    const body = { enabled };
    if (Number.isFinite(minutes)) body.interval_seconds = minutes * 60;
    try {
      await fetchJSON(`/api/events/sources/${name}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch (e) {
      showTransientNotice($("#events-notice"), e.message || "Failed to update source");
    } finally {
      await loadEventSources();
    }
  }

  function renderGeminiHealthBanner(gemini) {
    const banner = $("#gemini-health-banner");
    if (!gemini || gemini.consecutive_failures < 2) {
      banner.hidden = true;
      return;
    }
    // A source can fetch fine while AI analysis of what it found keeps
    // failing (bad key, quota, response format) - that's otherwise
    // invisible, since the per-source rows only reflect fetch health.
    banner.hidden = false;
    banner.textContent =
      `AI analysis has failed ${gemini.consecutive_failures} times in a row` +
      (gemini.last_error ? `: ${gemini.last_error}` : "") +
      " — fetched items are queued and will retry, but no new events will appear until this clears.";
  }

  async function loadEventSources() {
    const health = await fetchJSON("/api/events/sources");
    renderGeminiHealthBanner(health._gemini);
    const panel = $("#source-health-panel");
    Object.keys(SOURCE_META).forEach((name) => {
      if (!health[name]) return;
      const existing = sourceRows.get(name);
      if (existing) updateSourceHealthRow(existing, name, health[name]);
      else panel.appendChild(buildSourceHealthRow(name, health[name]));
    });
  }

  function buildEventCard(event) {
    const tpl = document.getElementById("event-card-template");
    const node = tpl.content.cloneNode(true);
    const card = node.querySelector(".event-card");
    const meta = SOURCE_META[event.source] || { label: event.source, badgeClass: "" };

    const sourceBadge = card.querySelector(".source-badge");
    sourceBadge.textContent = meta.label;
    sourceBadge.classList.add(meta.badgeClass);

    const signalBadge = card.querySelector(".event-signal-badge");
    const score = event.sentiment_score || 0;
    if (score > 20) { signalBadge.textContent = "Bullish"; signalBadge.classList.add("signal-buy"); }
    else if (score < -20) { signalBadge.textContent = "Bearish"; signalBadge.classList.add("signal-sell"); }
    else { signalBadge.textContent = "Neutral"; signalBadge.classList.add("signal-hold"); }

    card.querySelector(".event-ticker").textContent = event.ticker || "";
    card.querySelector(".event-company").textContent = event.company_name || "";
    if (!event.ticker_verified) card.querySelector(".unverified-badge").hidden = false;

    card.querySelector(".event-confidence").textContent =
      `Score ${score} · Confidence ${event.confidence || 0}%`;
    card.querySelector(".event-title").textContent = event.title || "";
    card.querySelector(".event-snippet").textContent = event.text_snippet || "";
    card.querySelector(".event-reasoning").textContent = event.reasoning || "";

    const link = card.querySelector(".event-link");
    const href = safeHttpUrl(event.link);
    if (href) link.href = href;
    else link.style.display = "none";

    card.querySelector(".event-timestamp").textContent = `Detected ${event.detected_at}`;

    return card;
  }

  async function loadEvents() {
    const data = await fetchJSON("/api/events");
    const list = $("#events-list");
    list.innerHTML = "";
    if (!data.events || data.events.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "No events detected yet.";
      list.appendChild(empty);
      return;
    }
    // Read after the guard above, not before it.
    lastEventsDetectedAt = data.events[0].detected_at;
    data.events.forEach((e) => list.appendChild(buildEventCard(e)));
  }

  // ---------- Raw social posts ----------
  // Renders a wall-clock string from an ISO timestamp, tolerating the mixed
  // formats the feeds emit ("+00:00" from RSS, "Z" from X's <time> element).
  function formatTimestamp(iso) {
    if (!iso) return "";
    const parsed = new Date(iso);
    if (Number.isNaN(parsed.getTime())) return iso;
    return parsed.toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  }

  function buildPostCard(post) {
    const tpl = document.getElementById("post-card-template");
    const card = tpl.content.cloneNode(true).querySelector(".post-card");
    const meta = SOURCE_META[post.source] || { label: post.source, badgeClass: "" };

    const sourceBadge = card.querySelector(".source-badge");
    sourceBadge.textContent = meta.label;
    if (meta.badgeClass) sourceBadge.classList.add(meta.badgeClass);

    // trumpstruth puts the post itself in the title and repeats it in the
    // body; x_musk uses a fixed "@elonmusk on X" title with the tweet as text.
    // Show the title as a byline only when it isn't just the text again.
    const title = post.title || "";
    const text = post.text || "";
    card.querySelector(".post-author").textContent =
      title && !text.startsWith(title.slice(0, 40)) ? title : "";
    card.querySelector(".post-text").textContent = text || title || "(no text)";
    card.querySelector(".post-published").textContent = formatTimestamp(post.published_at);

    const link = card.querySelector(".post-link");
    const href = safeHttpUrl(post.link);
    if (href) link.href = href;
    else link.style.display = "none";

    return card;
  }

  async function loadPosts() {
    const params = new URLSearchParams({ limit: POSTS_VISIBLE });
    if (postSourceFilter) params.set("source", postSourceFilter);
    const data = await fetchJSON(`/api/posts?${params}`);
    const list = $("#posts-list");
    list.innerHTML = "";
    if (!data.posts || data.posts.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = postSourceFilter === "x_musk" && !xScraperConfigured
        ? 'No posts yet — run "python login_x_bot.py" once, then enable the X source above.'
        : "No posts fetched yet. Sources are checked on their own interval — see above.";
      list.appendChild(empty);
      return;
    }
    data.posts.forEach((p) => list.appendChild(buildPostCard(p)));
  }

  function setupPostFilters() {
    $$("[data-post-source]").forEach((btn) => {
      btn.addEventListener("click", () => {
        $$("[data-post-source]").forEach((b) => b.classList.remove("is-active"));
        btn.classList.add("is-active");
        postSourceFilter = btn.dataset.postSource;
        loadPosts();
      });
    });
  }

  async function loadGlossary() {
    if (glossary) return glossary;
    glossary = await fetchJSON("/api/glossary");
    renderGlossaryTable();
    applyTooltips();
    return glossary;
  }

  function renderGlossaryTable() {
    const tbody = $("#glossary-table tbody");
    tbody.innerHTML = "";
    Object.values(glossary).forEach((entry) => {
      const tr = document.createElement("tr");
      const th = document.createElement("td");
      th.textContent = entry.label;
      const td = document.createElement("td");
      td.textContent = entry.description;
      tr.append(th, td);
      tbody.appendChild(tr);
    });
  }

  function applyTooltips() {
    if (!glossary) return;
    $$("[data-metric]").forEach((el) => {
      const key = el.dataset.metric;
      const entry = glossary[key];
      if (entry) el.setAttribute("data-tooltip", entry.description);
    });
  }

  // ---------- Formatting helpers ----------
  const fmtPrice = (v) => `$${Number(v).toFixed(2)}`;
  const fmtPct = (v) => `${v > 0 ? "+" : ""}${Number(v).toFixed(2)}%`;

  function signalClass(signal) {
    if (signal.includes("BUY")) return "signal-buy";
    if (signal.includes("SELL")) return "signal-sell";
    return "signal-hold";
  }

  function reasonClass(reason) {
    if (reason.startsWith("[+]")) return "reason-plus";
    if (reason.startsWith("[-]")) return "reason-minus";
    if (reason.startsWith("[AI]")) return "reason-ai";
    return "reason-equal";
  }

  function stripReasonPrefix(reason) {
    return reason.replace(/^\[(\+|-|=|AI)\]\s*/, "");
  }

  // ---------- Card rendering ----------
  function buildStockCard(result) {
    const tpl = document.getElementById("stock-card-template");
    const node = tpl.content.cloneNode(true);
    const card = node.querySelector(".stock-card");

    card.querySelector(".card-symbol").textContent = result.symbol;
    card.querySelector(".card-price").textContent = fmtPrice(result.current_price);

    const badge = card.querySelector(".signal-badge");
    badge.textContent = result.signal;
    badge.classList.add(signalClass(result.signal));

    labelledValue(card.querySelector(".score-value"), "Score", `${result.score}`);
    labelledValue(card.querySelector(".confidence-value"), "Confidence", `${result.confidence}%`);

    const changes = [
      ["daily", result.daily_change],
      ["weekly", result.week_change],
      ["monthly", result.month_change],
    ];
    changes.forEach(([field, value]) => {
      const el = card.querySelector(`[data-field="${field}"]`);
      el.textContent = fmtPct(value);
      if (value > 0) el.classList.add("is-up");
      else if (value < 0) el.classList.add("is-down");
    });

    const tech = result.technical;

    const rsiFill = card.querySelector('[data-field="rsi-fill"]');
    rsiFill.style.width = `${Math.max(0, Math.min(100, tech.rsi))}%`;
    card.querySelector('[data-field="rsi-value"]').textContent = tech.rsi.toFixed(1);

    card.querySelector('[data-field="macd"]').textContent = tech.macd_trend;
    // sma_50 is null for symbols with under 50 sessions of history
    card.querySelector('[data-field="sma"]').textContent =
      `${fmtPrice(tech.sma_10)} / ${fmtPrice(tech.sma_20)} / ${tech.sma_50 == null ? "n/a" : fmtPrice(tech.sma_50)}`;
    card.querySelector('[data-field="volume"]').textContent = `${tech.volume_ratio.toFixed(2)}x avg`;
    card.querySelector('[data-field="range"]').textContent =
      `${fmtPrice(tech["3mo_low"])} - ${fmtPrice(tech["3mo_high"])}`;

    const bbPct = Math.max(0, Math.min(1, tech.bb_position)) * 100;
    card.querySelector('[data-field="bb-fill"]').style.width = `${bbPct}%`;
    card.querySelector('[data-field="bb-value"]').textContent = tech.bb_position.toFixed(2);

    // Sentiment. Built from DOM nodes rather than an innerHTML template: every
    // value here originates in a Gemini response, and the only thing that kept
    // the old string interpolation safe was int-coercion happening in a
    // different file, in another language.
    const sentBody = card.querySelector(".sentiment-body");
    sentBody.textContent = "";
    const sent = result.sentiment || {};
    if (sent.has_news) {
      sentBody.append(
        metricRow("Sentiment Score", `${sent.sentiment_score}/100`, "sentiment_score"),
        metricRow("Market Mood", String(sent.market_sentiment || "neutral").toUpperCase(), "market_sentiment"),
        metricRow("Confidence", `${sent.confidence}%`),
        metricRow("Articles", `${sent.articles_count || 0}`)
      );
      if (sent.summary) {
        const summary = document.createElement("div");
        summary.className = "sentiment-summary";
        summary.textContent = sent.summary;
        sentBody.appendChild(summary);
      }
    } else {
      const none = document.createElement("div");
      none.className = "sentiment-none";
      none.textContent = sent.summary || "No recent news available";
      sentBody.appendChild(none);
    }

    // Targets
    const targets = result.targets;
    card.querySelector('[data-field="stop-loss"]').textContent = fmtPrice(targets.stop_loss);
    card.querySelector('[data-field="targets"]').textContent =
      `${fmtPrice(targets.target_1)} / ${fmtPrice(targets.target_2)}`;
    card.querySelector('[data-field="risk-reward"]').textContent = `${targets.risk_reward.toFixed(2)}:1`;

    // Reasons
    const list = card.querySelector(".reasons-list");
    (result.reasons || []).forEach((reason) => {
      const li = document.createElement("li");
      li.className = reasonClass(reason);
      li.textContent = stripReasonPrefix(reason);
      list.appendChild(li);
    });

    card.querySelector(".card-timestamp").textContent = `As of ${result.timestamp}`;

    return card;
  }

  function renderGrid(gridEl, results, emptyMessage) {
    gridEl.innerHTML = "";
    if (!results || results.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = emptyMessage;
      gridEl.appendChild(empty);
      return;
    }
    results.forEach((r) => gridEl.appendChild(buildStockCard(r)));
    applyTooltips();
  }

  function renderErrors(container, errors) {
    container.textContent = "";
    if (!errors || errors.length === 0) {
      container.hidden = true;
      return;
    }
    container.hidden = false;
    container.append(`Could not analyze ${errors.length} symbol${errors.length > 1 ? "s" : ""}:`);
    const list = document.createElement("ul");
    errors.forEach((e) => {
      const li = document.createElement("li");
      li.textContent = `${e.symbol}: ${e.error}`;
      list.appendChild(li);
    });
    container.appendChild(list);
  }

  // ---------- Data loading ----------
  async function loadWatchlist() {
    const data = await fetchJSON("/api/stocks");
    lastWatchlistUpdated = data.last_updated;
    $("#watchlist-updated").textContent = data.last_updated
      ? `Last updated: ${data.last_updated}`
      : "Last updated: not yet run";
    renderGrid($("#watchlist-grid"), data.results, "No analysis yet — click Refresh Now to analyze the watchlist.");
    renderErrors($("#watchlist-errors"), data.errors);
  }

  async function loadCustomResults() {
    const data = await fetchJSON("/api/custom-results");
    lastCustomCompleted = data.completed_at;
    renderGrid($("#custom-grid"), data.results, "Enter symbols above and click Analyze.");
    renderErrors($("#custom-errors"), data.errors);
  }

  // ---------- Actions ----------
  async function refreshNow() {
    const btn = $("#refresh-btn");
    btn.disabled = true;
    try {
      await fetchJSON("/api/refresh", { method: "POST" });
    } catch (e) {
      if (e.status === 409) {
        showTransientNotice($("#watchlist-progress"), "Already running…");
      }
    } finally {
      btn.disabled = false;
    }
  }

  async function analyzeCustom() {
    const input = $("#custom-symbols");
    const symbols = input.value.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean);
    if (symbols.length === 0) return;
    const btn = $("#analyze-btn");
    btn.disabled = true;
    try {
      await fetchJSON("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols }),
      });
    } catch (e) {
      const msg = e.status === 409 ? "Already running…" : (e.message || "Request failed");
      showTransientNotice($("#custom-progress"), msg);
    } finally {
      btn.disabled = false;
    }
  }

  function showTransientNotice(el, text) {
    el.hidden = false;
    el.textContent = text;
    setTimeout(() => { if (el.textContent === text) el.hidden = true; }, 4000);
  }

  // ---------- Status polling ----------
  async function pollStatus() {
    let delay = 4000;
    try {
      const status = await fetchJSON("/api/status");

      if (status.job_running) {
        delay = 1000;
        const line = `Analyzing ${status.job_completed}/${status.job_total}: ${status.job_current_symbol || ""}…`;
        const target = status.job_type === "custom_analyze" ? $("#custom-progress") : $("#watchlist-progress");
        target.hidden = false;
        target.textContent = line;
      } else {
        $("#watchlist-progress").hidden = true;
        $("#custom-progress").hidden = true;
      }

      renderAutoRefreshMeta(cachedSettings, status.next_scheduled_refresh_at);

      if (status.watchlist_last_updated && status.watchlist_last_updated !== lastWatchlistUpdated) {
        await loadWatchlist();
      }
      if (status.custom_completed_at && status.custom_completed_at !== lastCustomCompleted) {
        await loadCustomResults();
      }
      if (status.events_last_detected_at && status.events_last_detected_at !== lastEventsDetectedAt) {
        await loadEvents();
      }
      if (status.posts_last_detected_at && status.posts_last_detected_at !== lastPostsDetectedAt) {
        lastPostsDetectedAt = status.posts_last_detected_at;
        await loadPosts();
      }
      const eventsPanel = document.getElementById("panel-events");
      if (eventsPanel && !eventsPanel.hidden) {
        await loadEventSources(); // keep source health/timestamps live while this tab is open
      }
    } catch (e) {
      delay = 5000;
    } finally {
      setTimeout(pollStatus, delay);
    }
  }

  // ---------- Init ----------
  document.addEventListener("DOMContentLoaded", async () => {
    setupTabs();
    setupPostFilters();
    $("#refresh-btn").addEventListener("click", refreshNow);
    $("#analyze-btn").addEventListener("click", analyzeCustom);
    $("#custom-symbols").addEventListener("keydown", (e) => {
      if (e.key === "Enter") analyzeCustom();
    });
    $("#auto-refresh-toggle").addEventListener("change", saveSettings);
    $("#refresh-interval-input").addEventListener("change", () => {
      if ($("#auto-refresh-toggle").checked) saveSettings();
    });

    await loadConfig();
    await loadSettings();
    await loadGlossary();
    await loadWatchlist();
    await loadCustomResults();
    await loadEventSources();
    await loadPosts();
    await loadEvents();
    pollStatus();
  });
})();
