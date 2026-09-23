"""Recent news headlines per symbol (feedparser).

Primary source:  Yahoo Finance RSS  https://feeds.finance.yahoo.com/rss/2.0/headline?s=<SYMBOL>
Fallback:        Google News RSS search for the QUOTED symbol (more reliable
                 for European tickers). The query is quoted on purpose: an
                 unquoted search fuzzy-matches ("VUSA" -> visa articles).
                 A quoted search that finds nothing is reported as "no news".
Both sources retry once on HTTP 429.

Data contract (consumed by llm.py / report.py):

    fetch_news(symbols) -> {symbol: [headline, ...]}

    Each headline is a dict:
        {"title": str, "url": str, "published": str | None}   # ISO 8601 date

    A symbol with no news — or whose feeds both fail — maps to an empty
    list. One bad ticker never kills the batch (failure isolation, same
    convention as live_data.py).
"""

import calendar
import concurrent.futures
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

import feedparser

from config import (
    NEWS_HEADLINES_PER_TICKER,
    NEWS_MAX_RETRIES,
    NEWS_RETRY_BACKOFF_SECONDS,
    NEWS_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

_YAHOO_HEADLINE_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}"
_GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_MAX_WORKERS = 8


def _download(url: str) -> bytes:
    """Download a feed with an explicit timeout, a browser-ish UA, and a
    short retry on HTTP 429 (feed providers rate-limit aggressively)."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    attempts = NEWS_MAX_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=NEWS_TIMEOUT_SECONDS) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < attempts:
                time.sleep(NEWS_RETRY_BACKOFF_SECONDS)
                continue
            raise


def _parse_entries(raw: bytes):
    """Return the feed's entries, or [] for a missing/broken feed."""
    feed = feedparser.parse(raw)
    if feed.bozo and not feed.entries:
        logger.debug("Unparseable feed: %s", getattr(feed, "bozo_exception", "?"))
        return []
    return feed.entries


def _entry_timestamp(entry):
    """Entry date as a UTC epoch, or None if the feed gives no date."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    return calendar.timegm(parsed)


def _extract_headlines(entries, limit: int) -> list:
    """Shape raw feed entries into headline dicts.

    Dedupes by URL, sorts most-recent-first (undated entries keep feed
    order, at the end), then truncates to `limit` — truncating after
    sorting so an older but prominent item can't crowd out newer news.
    """
    deduped = []
    seen_urls = set()
    for entry in entries:
        url = (entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        if not title or not url or url in seen_urls:
            continue
        seen_urls.add(url)
        deduped.append((
            _entry_timestamp(entry),
            {
                "title": title,
                "url": url,
                "published": entry.get("published") or entry.get("updated"),
            },
        ))

    # Stable sort: dated entries by recency; None timestamps sink to the end.
    deduped.sort(key=lambda pair: pair[0] if pair[0] is not None else 0,
                 reverse=True)
    return [headline for _, headline in deduped[:limit]]


def _fetch_for_symbol(symbol: str) -> list:
    """Headlines for one symbol: Yahoo RSS first, Google News as fallback."""
    try:
        raw = _download(_YAHOO_HEADLINE_RSS.format(symbol=urllib.parse.quote(symbol)))
        headlines = _extract_headlines(_parse_entries(raw), NEWS_HEADLINES_PER_TICKER)
        if headlines:
            return headlines
    except Exception as exc:  # noqa: BLE001 - per-ticker failure isolation
        logger.warning("Yahoo news feed failed for %s: %s", symbol, exc)

    try:
        query = urllib.parse.quote(f'"{symbol}"')
        raw = _download(_GOOGLE_NEWS_RSS.format(query=query))
        headlines = _extract_headlines(_parse_entries(raw), NEWS_HEADLINES_PER_TICKER)
        if headlines:
            return headlines
        logger.info("No news found for %s (both feeds empty)", symbol)
    except Exception as exc:  # noqa: BLE001 - per-ticker failure isolation
        logger.warning("Google News feed failed for %s: %s", symbol, exc)

    return []


def fetch_news(symbols) -> dict:
    """Fetch headlines for all symbols in parallel.

    Args:
        symbols: iterable of ticker symbols (e.g. ["VUSA.DE", "ETL.PA"]).

    Returns:
        Dict keyed by symbol, each value a (possibly empty) list of
        headline dicts, most recent first.
    """
    symbols = list(dict.fromkeys(symbols))  # dedupe, keep order
    if not symbols:
        return {}

    max_workers = min(_MAX_WORKERS, len(symbols))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {symbol: pool.submit(_fetch_for_symbol, symbol) for symbol in symbols}
        return {
            symbol: future.result()  # workers never raise; worst case []
            for symbol, future in futures.items()
        }
