"""Live market data fetch.

Turns a list of ticker symbols into a per-symbol dict of live quote data
(price, currency, 52-week range) plus ~1y of daily closes for the report
sparklines.

Public contract (consumed by compute_pnl() in parse_csv.py):

    live = fetch_live_data(["VUSA.DE", "ETL.PA"])
    # {
    #   "VUSA.DE": {
    #     "last_price": 95.1,
    #     "previous_close": 94.8,
    #     "currency": "EUR",
    #     "year_high": 98.0,
    #     "year_low": 82.5,
    #     "history": [89.0, 89.4, ...],   # ~1y daily closes, oldest first
    #   },
    #   ...
    # }

Only successfully fetched symbols are returned. Failures are logged as
warnings and omitted from the result — compute_pnl() derives its `missing`
list from whatever is absent in `live`, so one dead ticker never kills the
whole report.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import yfinance as yf

from config import HISTORY_PERIOD, TICKER_FETCH_TIMEOUT_SECONDS

logger = logging.getLogger("finance_agent.live_data")

_MAX_WORKERS = 4


def fetch_live_data(symbols: list[str]) -> dict[str, dict]:
    """Fetch live quote + 1y price history for *symbols*, in parallel.

    Returns a dict keyed by symbol (see module docstring for the shape).
    Symbols whose fetch fails (unknown ticker, network error, missing
    quote data, timeout) are logged as a warning and omitted.

    Note: per-ticker time is bounded by TICKER_FETCH_TIMEOUT_SECONDS. A
    future that exceeds it is abandoned for reporting purposes, but the
    underlying yfinance call (which has its own internal timeouts) will
    still finish before the thread pool shuts down.
    """
    live: dict[str, dict] = {}
    unique = list(dict.fromkeys(symbols))  # dedupe, keep order
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, sym): sym for sym in unique}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                live[sym] = future.result(timeout=TICKER_FETCH_TIMEOUT_SECONDS)
            except Exception as e:  # noqa: BLE001 - failure isolation by design
                logger.warning("Live data fetch failed for %s: %s", sym, e)
    return live


def _fetch_one(symbol: str) -> dict:
    """Fetch quote + history for a single symbol. Raises on any failure."""
    t = yf.Ticker(symbol)

    fi = t.fast_info
    last_price = float(fi["lastPrice"])
    if last_price <= 0:
        raise ValueError("no quote data returned")

    def _optional(name: str):
        v = fi[name]
        return float(v) if v is not None else None

    hist = t.history(
        period=HISTORY_PERIOD, interval="1d", auto_adjust=True,
        timeout=TICKER_FETCH_TIMEOUT_SECONDS,
    )
    history = []
    if hist is not None and not hist.empty:
        history = [float(p) for p in hist["Close"].dropna()]

    return {
        "last_price": last_price,
        "previous_close": _optional("previousClose"),
        "currency": fi["currency"],
        "year_high": _optional("yearHigh"),
        "year_low": _optional("yearLow"),
        "history": history,
    }


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    syms = sys.argv[1:] or ["VUSA.DE", "ETL.PA"]
    print(json.dumps(fetch_live_data(syms), indent=2))
