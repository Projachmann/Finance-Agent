r"""Dev helper: fetch news for the test portfolio tickers (and a fake one).

Usage: .venv\Scripts\python test_news.py
Not part of the app; safe to delete.
"""

import json
import logging
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from news import fetch_news

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

symbols = ["VUSA.DE", "ETL.PA", "ZZFAKE123"]

start = time.time()
results = fetch_news(symbols)
elapsed = time.time() - start

print(f"\n--- {elapsed:.1f}s total ---")
print(json.dumps(results, indent=2, ensure_ascii=False))
