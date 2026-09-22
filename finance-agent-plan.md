# Finance Briefing Agent — Project Plan

## What this project is
A self-hosted web app running on a Raspberry Pi 5. Dad uploads his Yahoo Finance portfolio CSV via browser, the Pi enriches it with live market data and news, runs AI analysis, and returns a styled PDF briefing — all on demand, no scheduling needed.

---

## User flow
1. Dad opens `http://<tailscale-ip>:5000` on any device
2. Uploads `portfolio.csv` (exported from Yahoo Finance)
3. A spinner shows while the Pi processes (~20–35s)
4. PDF downloads automatically

---

## Tech stack

| Component | Tool | Notes |
|---|---|---|
| Web server | Gunicorn + Flask | Dev server won't bind correctly; gunicorn needed |
| CSV parsing | pandas | Group by symbol, weighted-avg purchase price |
| Live market data | yfinance (`fast_info`) | Use `fast_info` not `.info` — more stable |
| News headlines | feedparser | Yahoo RSS with Google News RSS as fallback |
| AI analysis | Groq API — `llama-3.3-70b-versatile` | Free tier, no credit card, ~2–5s response |
| PDF generation | WeasyPrint | Needs system apt libraries (see below) |
| Containerisation | Docker + Docker Compose | Handles all WeasyPrint apt deps cleanly |
| Hosting | Raspberry Pi 5 via Tailscale | Guest network, no port forwarding needed |

---

## Processing pipeline

```
POST /upload (CSV file)
  → parse_csv.py    — group by symbol, weighted-avg buy price, calc P&L + allocation %
  → live_data.py    — ThreadPoolExecutor: fetch yfinance fast_info + history per ticker in parallel
  → news.py         — ThreadPoolExecutor: Yahoo RSS per ticker, fallback to Google News RSS
  → llm.py          — single Groq call, JSON response: {stocks: [{symbol, verdict, reasoning}], commentary}
  → report.py       — render Jinja2 HTML → WeasyPrint → PDF
  → Flask response  — return PDF as file download
```

Parallelising yfinance + RSS calls with `ThreadPoolExecutor` saves ~10–15s.

---

## File structure

```
finance-agent/
├── Dockerfile           # python:3.11-slim-bookworm base, apt deps, pip install
├── docker-compose.yml   # port mapping, .env passthrough, output/ volume mount
├── .env                 # GROQ_API_KEY=... (never baked into image, never committed)
├── .gitignore           # .env, output/, __pycache__, venv/
├── main.py              # Gunicorn entry — /upload endpoint, orchestrates pipeline
├── config.py            # Model name, output directory (NO secrets here)
├── parse_csv.py         # Load CSV, group by symbol, compute P&L + allocation
├── live_data.py         # yfinance fast_info + 1y history per ticker, wrapped in try/except
├── news.py              # feedparser — Yahoo RSS with Google News fallback per ticker
├── llm.py               # Groq API call, structured JSON output, retry on 429
├── report.py            # WeasyPrint — renders HTML template to PDF
├── templates/
│   ├── upload.html      # Upload UI with JS fetch + spinner (no blank-tab wait)
│   └── report.html      # Jinja2 PDF template with @page rules
├── output/              # Mounted as Docker volume — PDFs persist across restarts
└── requirements.txt     # All deps pinned to specific versions
```

---

## PDF report contents

| Section | Details |
|---|---|
| Header | Date, total portfolio value (EUR), total day P&L |
| Holdings table | Symbol, qty, avg buy price, live price, value, P&L €, P&L % |
| Per-stock section | Live price, 52w high/low, currency, top 3 news headlines + links, AI verdict (Buy / Hold / Sell + reasoning) |
| Portfolio summary | 1-paragraph AI commentary on overall portfolio |
| Footer | "Not investment advice" disclaimer |

Day P&L = Σ qty × (live price − previous_close). Uses live prices from yfinance, not the stale "Current Price" in the CSV.

---

## Input CSV format (Yahoo Finance export)

```
Symbol, Current Price, Date, Time, Change, Open, High, Low, Volume,
Trade Date, Purchase Price, Quantity, Commission, High Limit, Low Limit, Comment, Transaction Type
```

**Important:** same symbol can appear multiple times (tranches). Must group by symbol, sum quantities, weighted-average the purchase price. Number formats can be locale-dependent; cells can be `N/A`.

Current test holdings:
- `VUSA.DE` — Vanguard S&P 500 ETF (Frankfurt), 13 units, bought at €89.00
- `ETL.PA` — Eutelsat (Paris), 20 units, bought at €2.80

**Currency note:** both are EUR today. If a USD ticker is ever added, flag the mismatch — don't silently mix currencies in totals.

---

## Networking

- Pi 5 is on a **guest network** (isolated from main LAN)
- **Tailscale** handles access — dad installs Tailscale on his device, hits the Pi's Tailscale IP
- No port forwarding, no public domain, no DuckDNS needed
- Gunicorn binds to `0.0.0.0:5000` (not 127.0.0.1 — that's Flask's default and won't be reachable)

---

## Groq API

- Sign up at `console.groq.com` — no credit card required
- Model: `llama-3.3-70b-versatile`
- Free limits: 30 RPM — more than enough for on-demand use
- Use `response_format={"type": "json_object"}` for structured output
- Prompt must say "use only the provided data, do not invent facts", temperature 0
- Retry logic for 429s; single call covers all tickers + overall commentary
- Store key in `.env`, load with `python-dotenv`

---

## Pi setup

### One-time Docker install (run on the Pi)
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker pi
# log out and back in for the group to take effect
```

### `Dockerfile`
```dockerfile
FROM python:3.11-slim-bookworm

# WeasyPrint system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
    libffi-dev libcairo2 libjpeg62-turbo-dev fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p output

CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "--timeout", "120", "main:app"]
```

### `docker-compose.yml`
```yaml
services:
  finance-agent:
    build: .
    ports:
      - "5000:5000"
    env_file:
      - .env
    volumes:
      - ./output:/app/output
    restart: unless-stopped
```

- `restart: unless-stopped` — survives Pi reboots without needing systemd
- `./output:/app/output` — PDFs persist on the host even if the container is rebuilt
- `.env` is passed in at runtime, never baked into the image

### Run
```bash
# First time / after code changes:
docker compose up -d --build

# Check logs:
docker compose logs -f

# Stop:
docker compose down
```

### Gunicorn flags (inside the container)
- `-w 1` — one worker is correct; pipeline is IO-bound per request
- `--timeout 120` — default 30s kills the worker mid-processing

---

## Key implementation notes

### parse_csv.py
- Group rows by Symbol, sum Quantity, weighted-average Purchase Price
- Handle `N/A` cells and locale decimal formats
- Do not use CSV's "Current Price" for any P&L — it's stale

### live_data.py
- Use `ticker.fast_info` fields: `last_price`, `previous_close`, `year_high`, `year_low`, `currency`
- Use `ticker.history(period="1y")` for sparkline data
- Wrap every ticker in try/except — one failure must not kill the whole report
- Fetch all tickers in parallel with `ThreadPoolExecutor`

### news.py
- Primary: `https://feeds.finance.yahoo.com/rss/2.0/headline?s=<SYMBOL>`
- Fallback: Google News RSS — more reliable for European tickers (VUSA.DE, ETL.PA)
- Render "no recent news available" gracefully if both fail

### report.html (WeasyPrint CSS)
- Use `@page` rules for margins
- `page-break-inside: avoid` on each per-stock section
- `overflow-wrap: break-word` on URLs (news headlines will blow out page width otherwise)

---

## Build order
1. `Dockerfile` + `docker-compose.yml` + `requirements.txt` + `config.py` + `.env`
2. `parse_csv.py`
3. `live_data.py`
4. `news.py`
5. `llm.py`
6. `report.py` + `templates/report.html`
7. `main.py` + `templates/upload.html` (JS spinner via fetch API)
8. `docker compose up -d --build` — first full run

---

## Requirements (pin all versions)
```
flask
gunicorn
pandas
yfinance
feedparser
groq
weasyprint
jinja2
python-dotenv
```

> Pin all versions. If a pin conflicts with the Pi's Python version, install unpinned, then run `pip freeze > requirements.txt` to re-lock.

---

## Ops checklist
- [ ] File logging — you will need it when dad says "it didn't work"
- [ ] Timestamped PDF filenames (`briefing_2026-09-21_143022.pdf`)
- [ ] Cron to clean `output/` older than 30 days
- [ ] `/health` endpoint returning 200 — easy aliveness check
- [ ] CLI one-shot mode: `python generate.py portfolio.csv` for testing without the web server
