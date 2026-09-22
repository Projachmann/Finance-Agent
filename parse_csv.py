"""CSV parsing for Yahoo Finance portfolio exports.

Pipeline step 1 (see finance-agent-plan.md):

    parse_csv(source)             -> {"holdings": [...], "warnings": [...]}
    compute_pnl(holdings, live)   -> P&L rows + totals with currency gate

Rules:
  * The same symbol can appear in multiple tranches -> grouped by symbol,
    quantities summed, purchase price weighted-averaged.
  * The CSV's "Current Price" column is NEVER used for P&L (it is stale).
  * All rows are assumed to be buy tranches (standard for Yahoo portfolio
    exports). Rows without a usable quantity/price (e.g. dividends) are
    skipped with a warning.
  * Numbers can be locale-formatted ("1.234,56" or "1,234.56") and cells
    can be "N/A".

Dependencies: pandas (see requirements.txt).
"""

import io
import math
import os
import re
from pathlib import Path

import pandas as pd

REQUIRED_COLUMNS = ["Symbol", "Purchase Price", "Quantity"]

_NON_NUMERICS = {"N/A", "NA", "--", "-", "\u2014", "NONE", "NULL", ""}


# ---------------------------------------------------------------------------
# Numeric cleaning
# ---------------------------------------------------------------------------

def _single_sep(s, sep):
    """Decide whether a lone separator in a number is decimal or thousands.

    "1,234" -> thousands (3 trailing digits, short head)
    "3,50"  -> decimal
    "0,123" -> decimal (head is "0", so not a thousands group)
    """
    head, _, tail = s.partition(sep)
    if sep in tail:  # multiple groups: 1,234,567 -> thousands
        return None, sep
    if len(tail) == 3 and 1 <= len(head) <= 3 and not head.startswith("0"):
        return None, sep
    return sep, None


def _to_number(raw):
    """Parse one CSV cell into a float, or None if not a usable number.

    Handles: N/A / empty cells, currency symbols, thousands separators in
    either locale order, and parentheses for negatives.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        raw = float(raw)
        if math.isnan(raw) or math.isinf(raw):
            return None
        return raw

    s = str(raw).strip()
    if s.upper() in _NON_NUMERICS:
        return None

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    if s.startswith("-"):
        negative = True
        s = s[1:].strip()
    elif s.startswith("+"):
        s = s[1:].strip()

    # Drop everything that is not a digit, dot or comma (currency symbols etc.)
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        return None

    if "," in s and "." in s:
        # Both present: the one that appears LAST is the decimal separator.
        decimal = "," if s.rfind(",") > s.rfind(".") else "."
        thousand = "." if decimal == "," else ","
    elif "," in s:
        decimal, thousand = _single_sep(s, ",")
    elif "." in s:
        decimal, thousand = _single_sep(s, ".")
    else:
        decimal, thousand = None, None

    if thousand:
        s = s.replace(thousand, "")
    if decimal:
        s = s.replace(decimal, ".")

    try:
        value = float(s)
    except ValueError:
        return None
    return -value if negative else value


def _load_text(source):
    """Read a path or file-like object into text (UTF-8, CP1252 fallback)."""
    if isinstance(source, (str, os.PathLike)):
        data = Path(source).read_bytes()
    else:
        data = source.read()
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # German Yahoo exports can carry umlauts encoded as CP1252
        return data.decode("cp1252", errors="replace")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_csv(source):
    """Parse a Yahoo Finance portfolio CSV into per-holding cost basis.

    source: file path (str/Path) or file-like object with .read()
            (e.g. Flask's request.files["file"]).

    Returns:
        {"holdings": [{"symbol", "quantity", "avg_cost", "total_cost"}],
         "warnings": [str, ...]}

    Raises:
        ValueError with a user-facing message if the file is not a usable
        portfolio export (missing columns / no valid rows).
    """
    text = _load_text(source)
    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False,
                     skipinitialspace=True)
    df.columns = [str(c).strip() for c in df.columns]

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            "Not a valid Yahoo Finance portfolio export (missing columns: "
            + ", ".join(missing) + ")."
        )

    groups = {}  # symbol -> list of (qty, price) tranches
    warnings = []
    for i, row in df.iterrows():
        csv_line = i + 2  # 1-based line number, +1 for the header row
        symbol = str(row["Symbol"]).strip().upper()
        qty = _to_number(row["Quantity"])
        price = _to_number(row["Purchase Price"])

        if not symbol:
            warnings.append(f"Line {csv_line}: skipped - no symbol.")
            continue
        if qty is None or price is None or qty <= 0 or price < 0:
            warnings.append(
                f"Line {csv_line}: skipped {symbol} - missing/invalid "
                f"quantity or purchase price."
            )
            continue
        groups.setdefault(symbol, []).append((qty, price))

    if not groups:
        raise ValueError("No valid holdings found in the CSV.")

    holdings = []
    for symbol in sorted(groups):
        tranches = groups[symbol]
        quantity = sum(q for q, _ in tranches)
        total_cost = sum(q * p for q, p in tranches)
        if quantity <= 0:
            warnings.append(f"{symbol}: skipped - net quantity is not positive.")
            continue
        holdings.append({
            "symbol": symbol,
            "quantity": quantity,
            "avg_cost": total_cost / quantity,
            "total_cost": total_cost,
        })

    return {"holdings": holdings, "warnings": warnings}


def compute_pnl(holdings, live):
    """Merge cost basis with live market data.

    holdings: parse_csv()["holdings"].
    live:     dict keyed by symbol as built by live_data.py, e.g.
              {"VUSA.DE": {"last_price": 95.1, "previous_close": 94.8,
                           "currency": "EUR", ...}}
              Fields may be None; a symbol absent from `live` entirely is
              reported in the returned "missing" list.

    Currency gate: totals are only summed when every holding shares one
    currency. Otherwise totals is None and "subtotals" carries per-currency
    numbers, so the report shows a warning instead of mixing currencies
    silently.

    All amounts are raw floats; formatting/rounding is report.py's job.
    """
    rows = []
    missing = []
    for h in holdings:
        ld = live.get(h["symbol"]) or {}
        price = ld.get("last_price")
        prev_close = ld.get("previous_close")
        currency = ld.get("currency")

        if price is None:
            missing.append(h["symbol"])

        value = h["quantity"] * price if price is not None else None
        pnl = (value - h["total_cost"]) if value is not None else None
        pnl_pct = ((pnl / h["total_cost"]) * 100.0
                   if (pnl is not None and h["total_cost"]) else None)
        day_pnl = (h["quantity"] * (price - prev_close)
                   if (price is not None and prev_close is not None) else None)

        rows.append({
            **h,
            "live_price": price,
            "previous_close": prev_close,
            "currency": currency,
            "value": value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "day_pnl": day_pnl,
            "allocation_pct": None,
        })

    currencies = sorted({r["currency"] for r in rows if r["currency"]})
    mismatch = len(currencies) > 1

    # Per-currency subtotals; allocation % is only meaningful within one currency.
    subtotals = {}
    for row in rows:
        subtotals.setdefault(row["currency"] or "UNKNOWN", []).append(row)

    for cur, cur_rows in subtotals.items():
        total_value = sum(r["value"] for r in cur_rows if r["value"] is not None)
        total_day_pnl = sum(r["day_pnl"] for r in cur_rows if r["day_pnl"] is not None)
        for r in cur_rows:
            r["allocation_pct"] = ((r["value"] / total_value) * 100.0
                                   if (r["value"] is not None and total_value > 0)
                                   else None)
        subtotals[cur] = {
            "value": total_value,
            "day_pnl": total_day_pnl,
            "cost_basis": sum(r["total_cost"] for r in cur_rows),
        }

    # Headline totals: only valid when there is a single currency (or none known).
    totals = None
    if not mismatch:
        totals = subtotals.get(currencies[0] if currencies else "UNKNOWN")

    return {
        "rows": rows,
        "totals": totals,
        "currency": currencies[0] if len(currencies) == 1 else None,
        "currency_mismatch": mismatch,
        "subtotals": subtotals,
        "missing": missing,
    }


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("usage: python parse_csv.py <portfolio.csv>")
        sys.exit(1)
    print(json.dumps(parse_csv(sys.argv[1]), indent=2))
