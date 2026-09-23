"""Groq LLM analysis for the portfolio briefing.

Pipeline step 4 (see finance-agent-plan.md). One Groq call per briefing:
the merged P&L table, live market data and news headlines go in, and a
short verdict per stock plus an overall commentary comes back as JSON.

Public contract (see agent.md):

    analyze_portfolio(pnl, live, news) -> dict

    pnl:    compute_pnl(holdings, live) result from parse_csv.py
            (dict with "rows", "totals", "missing", ...)
    live:   fetch_live_data() result (used for 1-year high/low, which
            compute_pnl does not carry)
    news:   fetch_news() result

    Returns on success:
        {
          "stocks": [
            {"symbol": "VUSA.DE", "verdict": "...", "reasoning": "..."},
            ...
          ],
          "commentary": "2-3 sentences on the portfolio as a whole",
        }

    Raises LLMAnalysisError when no usable analysis is available (missing
    API key, non-transient API error, exhausted retries on 429/timeouts,
    or a response without usable JSON). Callers must catch it and render
    "Analysis unavailable" in the report - a dead LLM never kills the
    briefing.

Design notes:
  * The 1y close history is NEVER sent to the LLM (~252 points of noise);
    only derived stats (last price, prev close, 1y high/low) go in.
  * News URLs are dropped before the call: useless to the model, and they
    burn tokens.
  * response_format={"type": "json_object"} is requested, plus defensive
    parsing as a backstop (markdown fences, hallucinated or duplicate
    symbols are dropped).
  * Retries with exponential backoff on 429 / connection / 5xx errors,
    bounded by the LLM_* knobs in config.py.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from groq import APIConnectionError, APIStatusError, Groq, RateLimitError

from config import (
    GROQ_MODEL,
    LLM_MAX_RETRIES,
    LLM_MAX_TOKENS,
    LLM_RETRY_BACKOFF_SECONDS,
    LLM_TEMPERATURE,
    LLM_TIMEOUT_SECONDS,
)

logger = logging.getLogger("finance_agent.llm")


class LLMAnalysisError(Exception):
    """No usable LLM analysis available; the report must degrade gracefully."""


_SYSTEM_PROMPT = """\
You are a concise financial analyst writing a daily portfolio briefing for a private investor.
The user message is JSON describing the portfolio: per-holding cost basis, live price, P&L, allocation, 1-year high/low, recent news headlines, and - when all holdings share one currency - portfolio totals.
Reply with ONLY a JSON object - no markdown, no text outside the JSON - in exactly this shape:
{"stocks":[{"symbol":"...","verdict":"...","reasoning":"..."}],"commentary":"..."}
Rules:
- One entry in "stocks" for every holding in the input, in input order, with the symbol exactly as given.
- "verdict": 1-2 short sentences (e.g. "Steady - no action needed." / "News-driven dip; fundamentals look intact.").
- "reasoning": 1-2 sentences grounded in the numbers provided: price vs cost basis and 1-year range, the daily move, and the specific news headlines. If a holding has no news, say so briefly.
- "commentary": 2-3 sentences on the portfolio as a whole (concentration, overall P&L, currency).
- If live data is missing for a holding, say the verdict rests on cost basis and news only.
- Be factual and direct. No disclaimers, no tables, no extra keys.
"""


def _round(value, digits):
    """Round for the prompt payload; None stays None (missing is signal)."""
    return None if value is None else round(float(value), digits)


def _build_prompt(pnl: dict, live: dict, news: dict) -> tuple[str, str]:
    """Assemble (system, user) messages from the pipeline data."""
    holdings = []
    for row in pnl.get("rows", []):
        symbol = row.get("symbol")
        quote = live.get(symbol) or {}
        holdings.append({
            "symbol": symbol,
            "quantity": row.get("quantity"),
            "avg_cost": _round(row.get("avg_cost"), 2),
            "total_cost": _round(row.get("total_cost"), 2),
            "currency": row.get("currency"),
            "live_price": _round(row.get("live_price"), 2),
            "previous_close": _round(row.get("previous_close"), 2),
            "value": _round(row.get("value"), 2),
            "pnl": _round(row.get("pnl"), 2),
            "pnl_pct": _round(row.get("pnl_pct"), 1),
            "day_pnl": _round(row.get("day_pnl"), 2),
            "allocation_pct": _round(row.get("allocation_pct"), 1),
            "year_high": _round(quote.get("year_high"), 2),
            "year_low": _round(quote.get("year_low"), 2),
            "news": [
                {"title": h.get("title"), "published": h.get("published")}
                for h in (news.get(symbol) or [])
            ],
        })

    payload = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "currency_mismatch": bool(pnl.get("currency_mismatch")),
        "missing_live_data": pnl.get("missing") or [],
        "holdings": holdings,
    }
    # Headline totals only exist when the currency gate passed.
    totals = pnl.get("totals") if not pnl.get("currency_mismatch") else None
    if totals:
        payload["totals"] = {
            "value": _round(totals.get("value"), 2),
            "cost_basis": _round(totals.get("cost_basis"), 2),
            "day_pnl": _round(totals.get("day_pnl"), 2),
        }

    return _SYSTEM_PROMPT, json.dumps(payload, indent=2)


def _parse_analysis(text: str, known_symbols: list[str]) -> dict:
    """Parse and validate the model's JSON response.

    Drops hallucinated and duplicate symbols; raises LLMAnalysisError if
    nothing usable survives.
    """
    text = (text or "").strip()
    if text.startswith("```"):  # fence backstop, json_object mode normally prevents this
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMAnalysisError("LLM response contains no JSON object")
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as e:
            raise LLMAnalysisError(f"LLM response is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise LLMAnalysisError("LLM response is not a JSON object")

    known = {s.upper() for s in known_symbols}
    stocks = []
    seen = set()
    for item in data.get("stocks") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol or symbol not in known or symbol in seen:
            continue  # hallucinated / duplicate - drop silently
        seen.add(symbol)
        stocks.append({
            "symbol": symbol,
            "verdict": str(item.get("verdict") or "").strip(),
            "reasoning": str(item.get("reasoning") or "").strip(),
        })
    if not stocks:
        raise LLMAnalysisError("LLM response has no usable per-stock verdicts")

    return {"stocks": stocks, "commentary": str(data.get("commentary") or "").strip()}


def analyze_portfolio(pnl: dict, live: dict, news: dict) -> dict:
    """Send the portfolio to Groq and return the structured analysis.

    See the module docstring for the full contract.
    """
    load_dotenv()  # idempotent; .env holds GROQ_API_KEY (never committed)
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise LLMAnalysisError("GROQ_API_KEY is not set (is .env in place?)")

    known_symbols = [
        row.get("symbol") for row in pnl.get("rows", []) if row.get("symbol")
    ]
    if not known_symbols:
        raise LLMAnalysisError("Nothing to analyze: empty portfolio")

    system, user = _build_prompt(pnl, live, news)

    # max_retries=0: this module owns the retry policy (config knobs).
    client = Groq(api_key=api_key, timeout=LLM_TIMEOUT_SECONDS, max_retries=0)

    attempts = LLM_MAX_RETRIES + 1
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = client.chat.completions.create(
                model=GROQ_MODEL,
                temperature=LLM_TEMPERATURE,
                max_tokens=LLM_MAX_TOKENS,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            text = response.choices[0].message.content if response.choices else ""
            return _parse_analysis(text, known_symbols)
        except (RateLimitError, APIConnectionError) as exc:
            last_error = exc  # 429 / timeout / network - retryable
        except APIStatusError as exc:
            if 500 <= exc.status_code < 600:
                last_error = exc  # server-side blip - retryable
            else:
                raise LLMAnalysisError(
                    f"Groq API error {exc.status_code}: {exc.message}"
                ) from exc
        if attempt < attempts:
            delay = LLM_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "LLM attempt %d/%d failed (%s); retrying in %.0fs",
                attempt, attempts, type(last_error).__name__, delay,
            )
            time.sleep(delay)

    raise LLMAnalysisError(
        f"LLM analysis failed after {attempts} attempts: {last_error}"
    )


if __name__ == "__main__":
    # Dev one-shot: python llm.py <portfolio.csv>
    import sys

    import parse_csv as csv_mod
    from live_data import fetch_live_data
    from news import fetch_news

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if len(sys.argv) < 2:
        print("usage: python llm.py <portfolio.csv>")
        sys.exit(1)

    parsed = csv_mod.parse_csv(sys.argv[1])
    symbols = [h["symbol"] for h in parsed["holdings"]]
    live = fetch_live_data(symbols)
    pnl = csv_mod.compute_pnl(parsed["holdings"], live)
    news = fetch_news(symbols)
    try:
        print(json.dumps(analyze_portfolio(pnl, live, news), indent=2))
    except LLMAnalysisError as e:
        print(f"LLMAnalysisError: {e}", file=sys.stderr)
        sys.exit(1)
