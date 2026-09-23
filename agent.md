# AGENT.md — Context for AI assistants

Handoff note: everything in this project is specified in `finance-agent-plan.md`.
Read that first — it is the source of truth. This file only tracks **progress and
decisions** so a new session doesn't redo work or break conventions.

## What this project is

Self-hosted web app on a Raspberry Pi 5 (guest network, reachable via Tailscale).
Dad uploads his Yahoo Finance portfolio CSV in a browser → the Pi enriches it with
live market data + news → Groq LLM analysis → styled PDF briefing, returned as a
download. On-demand only, no scheduling.

Stack: Flask + Gunicorn, pandas, yfinance, feedparser, Groq
(`openai/gpt-oss-120b`), WeasyPrint, Docker Compose. Full details + file
structure in the plan.

## Progress

Build order (from the plan) and status:

| Step | Item | Status |
|---|---|---|
| 1 | `Dockerfile`, `docker-compose.yml`, `requirements.txt`, `config.py`, `.env` | ✅ done, with 2 open items below |
| 1+ | `.dockerignore`, `.gitignore` | ✅ done |
| 2 | `parse_csv.py` | ✅ done |
| 3 | `live_data.py` | ✅ done (tested live on dev machine) |
| 4 | `news.py` | ✅ done (tested live on dev machine) |
| 5 | `llm.py` | ✅ done (tested end-to-end on dev machine) |
| 6 | `report.py` + `templates/report.html` | ⬜ |
| 7 | `main.py` + `templates/upload.html` | ⬜ |
| 8 | `docker compose up -d --build` (first full run) | ⬜ |

### Open items

- **`requirements.txt` is NOT pinned** — plan says pin all versions. Owner was
  going to decide between (a) pinning now or (b) the plan's fallback (build on the
  Pi, then `pip freeze` to re-lock). Currently plain unpinned package names.
- **Git:** repo initialized; working tree clean. Commits exist for plan, Docker
  scaffolding, CSV parsing, live data, the news milestone (fetch + hardened
  Google News fallback, test-helper removal), and LLM analysis (Groq, JSON-mode
  prompt + retries + graceful degradation, dev one-shot).
- **Dev venv:** `.venv` (Python 3.14.3) on the Windows dev box now has
  pandas 3.0.6 + yfinance 1.7.0 — reference point when pinning `requirements.txt`.
- **Test CSV:** `test_portfolio.csv` now in repo root — `VUSA.DE` (13 @ €89.00),
  `ETL.PA` (20 @ €2.80), both EUR. `parse_csv.py` + `live_data.py` verified
  against it.
- `.env` exists with a real `GROQ_API_KEY`. Untracked (gitignore works). The key
  was pasted into chat at some point — rotation was suggested.

## Conventions & decisions (do not break)

- **Secrets:** `GROQ_API_KEY` lives only in `.env`, loaded at runtime via
  `python-dotenv`. `config.py` holds non-secret settings only. `.dockerignore`
  keeps `.env` out of the image — keep it that way.
- **`config.py` is the single source for knobs** (model name, timeouts, output
  dir, headline counts). New modules read from it; no magic numbers in module code.
- **The CSV's "Current Price" column is stale — never use it for P&L.** Live
  prices come from yfinance exclusively.
- **`parse_csv.py` API (data contract — later modules build against this):**
  - `parse_csv(source)` → `{"holdings": [{symbol, quantity, avg_cost, total_cost}], "warnings": [str]}`
    — `source` is a path or file-like; raises `ValueError` with user-facing text on bad input.
  - `compute_pnl(holdings, live)` → `{"rows": [...], "totals", "currency",
    "currency_mismatch", "subtotals", "missing"}`.
    - `live` must be a **dict keyed by symbol** with at least `last_price`,
      `previous_close`, `currency` (values may be `None`).
    - **`live_data.py` must return exactly this shape** (plus `year_high`,
      `year_low`, `history` per the plan, which `compute_pnl` ignores).
  - **Currency gate:** if holdings span >1 currency, `totals` is `None`,
    `currency_mismatch` is `True`, and `subtotals` has per-currency numbers.
    Never mix currencies in totals.
- **Failure isolation:** one bad ticker/row must never kill the report —
  catch, warn, continue (plan requirement; `parse_csv.py` already follows it).
- **Parallelism:** yfinance + RSS fetches use `ThreadPoolExecutor` (plan: saves
  ~10–15s).
- **yfinance:** use `ticker.fast_info` (`last_price`, `previous_close`,
  `year_high`, `year_low`, `currency`), **not** `.info`.
  - Verified against yfinance 1.7.0: `fast_info` string keys are `lastPrice`,
    `previousClose`, `currency`, `yearHigh`, `yearLow`; `history(period=...,` 
    `timeout=...)` accepts a timeout kwarg.
  - Bad symbols raise inside the worker (e.g. `KeyError: 'currentTradingPeriod'`)
    — caught per-ticker; the symbol is omitted from the result and
    `compute_pnl()` derives `missing` from absences. yfinance's own ERROR logs
    for bad symbols are unavoidable library noise.
- **`news.py` API (data contract — llm.py / report.py build against this):**
  - `fetch_news(symbols)` → `{symbol: [{title, url, published}, ...]}`.
    `published` is an ISO 8601 UTC string (e.g. `2026-09-23T03:59:11Z`),
    normalized from the parsed feed timestamp, or `None` if the feed gives
    no date.
  - Empty list = no news (both feeds empty or both failed) — report renders
    "no recent news available". Workers never raise; failures are logged.
  - Sources, in order: Yahoo Finance RSS per symbol, then Google News RSS
    for the **quoted** symbol. Quoted on purpose — an unquoted query
    fuzzy-matches badly ("VUSA" → visa/photography articles). Quoted is still
    not exact (Google matches against full article text), so the fallback
    additionally drops entries whose title and summary never mention the
    symbol. Nothing surviving (or an empty result) is honestly reported as
    "no news" (e.g. VUSA.DE has little coverage).
  - Headlines deduped by URL, sorted most-recent-first (via feedparser's
    `published_parsed`; undated entries sink to the end), then truncated to
    `NEWS_HEADLINES_PER_TICKER`. Truncation happens after sorting.
  - HTTP 429 gets `NEWS_MAX_RETRIES` extra attempts with
    `NEWS_RETRY_BACKOFF_SECONDS` sleep (feeds rate-limit aggressively).
  - Observed on dev: Yahoo RSS 429s from the Windows dev IP (IP throttling) —
    the Google fallback covers it; the Pi's residential IP is likely fine.
    Google News links are long redirect URLs → `overflow-wrap` in the template
    is mandatory.
- **`llm.py` API (data contract — report.py builds against this):**
  - `analyze_portfolio(pnl, live, news)` → `{"stocks": [{symbol, verdict,
    reasoning}, ...], "commentary": str}` — one entry per holding, in input
    order. `live` is needed for 1y high/low (`compute_pnl` rows don't carry
    them).
  - Raises `LLMAnalysisError` when no usable analysis is available (missing
    API key, non-transient API error, retries exhausted on 429/timeouts,
    response without usable JSON). **Callers must catch it and render
    "Analysis unavailable"** — a dead LLM never kills the briefing.
  - Model: `config.GROQ_MODEL` = `openai/gpt-oss-120b`. Note:
    `llama-3.3-70b-versatile` was retired by Groq (404 `model_not_found`,
    observed 2026-09-23; absent from the account's model list) — don't
    restore it.
  - The 1y close history is never sent to the LLM (noise); only derived
    stats go in. News URLs are dropped before the call (useless to the
    model, they burn tokens).
  - `response_format={"type": "json_object"}` is requested, plus defensive
    parsing as a backstop (markdown fences, hallucinated/duplicate symbols
    dropped).
  - Retries: exponential backoff on 429 / connection / 5xx, bounded by the
    `LLM_*` config knobs; the client is built with `max_retries=0` so llm.py
    owns the retry policy.
  - Dev one-shot: `python llm.py <portfolio.csv>` runs the full pipeline
    (parse → live → news → analysis) and prints the JSON — how the
    end-to-end verification was done on the dev machine.
- **WeasyPrint CSS:** `@page` rules, `page-break-inside: avoid` per stock section,
  `overflow-wrap: break-word` on URLs (news links blow out page width otherwise).
- **PDFs:** timestamped filenames `briefing_YYYY-MM-DD_HHMMSS.pdf` into
  `config.OUTPUT_DIR` (bind-mounted to `./output` on the Pi).
- **Server:** Gunicorn `-w 1 -b 0.0.0.0:5000 --timeout 120` (30s default kills
  workers mid-pipeline).
- **Ops checklist items** (plan): file logging, 30-day cleanup cron for
  `output/`, `/health` endpoint, CLI one-shot `python generate.py portfolio.csv`.

## Environment notes

- Docker is installed on the Pi; user added `pi` to the `docker` group.
- Tailscale = the only access path (guest network). Address = Pi's Tailscale IP.
- Dev happens on the owner's Windows PC (this folder); the Pi is the deployment
  target — files transfer via git or `scp -r`.
- `docker compose build` works anytime; **`docker compose up` only after
  `main.py` exists** (gunicorn targets `main:app`, container would crash-loop).
- Local shell here is Windows (cmd) — no bash syntax in commands.
  - Quirk: the shell wrapper mangles quoted paths/strings with spaces — avoid
    `"paths with spaces"` and inline `python -c "..."`; use script files instead.
  - `cd` with no args works (cwd is already the project folder); `cd /d` fails.
