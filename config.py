"""Application configuration.

Non-secret settings only. Secrets (GROQ_API_KEY) live in .env and are
loaded at runtime via python-dotenv — never import them here.
"""

import os

# --- LLM (Groq) ---
# llama-3.3-70b-versatile was retired by Groq (404 model_not_found,
# observed 2026-09-23); gpt-oss-120b is the current large general-purpose
# model on the account (verified end-to-end on test_portfolio.csv).
GROQ_MODEL = "openai/gpt-oss-120b"
LLM_TEMPERATURE = 0
LLM_MAX_RETRIES = 3          # retries on 429 / transient errors
LLM_RETRY_BACKOFF_SECONDS = 2   # base delay; doubles per attempt (2s, 4s, 8s)
LLM_TIMEOUT_SECONDS = 60
# Cap on the analysis response. The briefing is short by design; the cap
# also bounds worst-case latency and cost.
LLM_MAX_TOKENS = 1200

# --- Output ---
# Resolved relative to this file: /app/output inside Docker (bind-mounted
# to ./output on the Pi), or ./output next to the source in CLI mode.
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# briefing_2026-09-21_143022.pdf
REPORT_FILENAME_PREFIX = "briefing"

# --- Market data (yfinance) ---
HISTORY_PERIOD = "1y"        # sparkline data per ticker
TICKER_FETCH_TIMEOUT_SECONDS = 15

# --- News (feedparser) ---
NEWS_HEADLINES_PER_TICKER = 3
NEWS_TIMEOUT_SECONDS = 10
NEWS_MAX_RETRIES = 1          # extra attempts on HTTP 429 (feeds rate-limit)
NEWS_RETRY_BACKOFF_SECONDS = 1.5
