"""PDF briefing rendering (Jinja2 -> WeasyPrint).

Pipeline step 5 (see finance-agent-plan.md): takes the outputs of the
previous steps and produces the styled PDF briefing.

Inputs (exact outputs of the previous pipeline steps):

    pnl      = compute_pnl(holdings, live)       parse_csv.py
    live     = fetch_live_data(symbols)          live_data.py
    news     = fetch_news(symbols)               news.py
    analysis = analyze_portfolio(pnl, live, news)  llm.py, or None
             (llm.py raises LLMAnalysisError when nothing usable came
              back; a None analysis renders "AI analysis unavailable"
              instead of killing the briefing - a dead LLM never kills
              the report, same convention as the fetch steps)
    warnings = parse_csv()["warnings"] (optional; shown in a notice box)

Public contract:

    render_html(pnl, live, news, analysis=None, warnings=None) -> str
    render_pdf(pnl, live, news, analysis=None, warnings=None) -> bytes
    save_report(pnl, live, news, analysis=None, warnings=None) -> Path
        # writes OUTPUT_DIR/briefing_YYYYMMDD_HHMMSS.pdf (timestamped,
        # see the ops checklist in finance-agent-plan.md)

compute_pnl() hands over raw floats; all formatting/rounding happens
here (per the compute_pnl() docstring in parse_csv.py).

Note: weasyprint is imported inside render_pdf() on purpose - it needs
system libraries (pango/cairo, provided by the Docker image) that a dev
machine (e.g. Windows) may not have. Everything up to the WeasyPrint
call (template rendering, formatting helpers, sparklines) stays fully
testable without them.
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import OUTPUT_DIR, REPORT_FILENAME_PREFIX

logger = logging.getLogger("finance_agent.report")

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAME = "report.html"

# Display symbols for the currencies we expect to see; anything not listed
# falls back to the ISO code itself (e.g. "SEK 12.34").
CURRENCY_SYMBOLS = {
    "EUR": "€",
    "USD": "$",
    "GBP": "£",
    "CHF": "CHF ",
    "SEK": "kr ",
    "NOK": "kr ",
}

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),  # news titles / LLM text are user data
    trim_blocks=True,
    lstrip_blocks=True,
)


# ---------------------------------------------------------------------------
# Formatting helpers (also exposed to the template as `fmt.<name>`)
# ---------------------------------------------------------------------------

def _currency_symbol(currency):
    if not currency:
        return ""
    return CURRENCY_SYMBOLS.get(currency, currency + " ")


def format_money(value, currency=None):
    """1234.5 -> '€1,234.50' (currency symbol); None -> '—'."""
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}{_currency_symbol(currency)}{abs(value):,.2f}"


def format_price(value):
    """95.1 -> '95.10', 1.795 -> '1.795' (up to 3 decimals, at least 2)."""
    if value is None:
        return "—"
    text = f"{value:,.3f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".00"
    elif len(text.split(".")[-1]) == 1:
        text += "0"
    return text


def format_pct(value, signed=False):
    """42.1 -> '42.1%'; signed=True: '+42.1%' / '−42.1%' (real minus sign)."""
    if value is None:
        return "—"
    sign = "+" if (signed and value > 0) else ("−" if value < 0 else "")
    return f"{sign}{abs(value):,.1f}%"


def format_signed_money(value, currency=None):
    """12.5 -> '+€12.50', -12.5 -> '−€12.50', 0 -> '€0.00'; None -> '—'."""
    if value is None:
        return "—"
    sign = "+" if value > 0 else ("−" if value < 0 else "")
    return f"{sign}{format_money(abs(value), currency)}"


def format_quantity(value):
    """13 -> '13', 13.5 -> '13.5' (integers without a decimal point)."""
    if value is None:
        return "—"
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def format_date(iso):
    """'2026-09-23T03:59:11Z' -> '23 Sep 2026'; None -> '—'."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    return dt.strftime("%d %b %Y")


def sparkline_svg(history, width=240, height=56):
    """~1y daily closes (oldest first) -> inline <svg> polyline, or ''.

    WeasyPrint renders basic SVG (polyline/rect) natively, so no charting
    library is needed. Fewer than 2 points -> '' (the template then shows
    'no data' instead of an empty box).
    """
    points = [float(p) for p in (history or []) if p is not None and p > 0]
    if len(points) < 2:
        return ""
    lo, hi = min(points), max(points)
    span = (hi - lo) or max(abs(hi), 1.0)
    pad = 2
    step = (width - 2 * pad) / (len(points) - 1)
    coords = [
        f"{pad + i * step:.1f}:{height - pad - (p - lo) / span * (height - 2 * pad):.1f}"
        for i, p in enumerate(points)
    ]
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<polyline points="{" ".join(coords)}" fill="none" '
        f'stroke="#57606a" stroke-width="1.5"/></svg>'
    )


_fmt_helpers = {
    "money": format_money,
    "price": format_price,
    "pct": format_pct,
    "signed_money": format_signed_money,
    "quantity": format_quantity,
    "date": format_date,
}


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------

def _build_context(pnl, live, news, analysis, warnings):
    """Merge the four pipeline outputs into a flat template context."""
    analysis = analysis or None  # {} -> None (no usable analysis)
    analysis_by_symbol = {
        s["symbol"]: s for s in (analysis or {}).get("stocks", []) if s.get("symbol")
    }

    rows = []
    for row in pnl.get("rows", []):
        symbol = row.get("symbol")
        quote = live.get(symbol) or {}
        rows.append({
            **row,
            "year_high": quote.get("year_high"),
            "year_low": quote.get("year_low"),
            "sparkline": sparkline_svg(quote.get("history")),
            "headlines": (news.get(symbol) or [])[:3],
            "ai": analysis_by_symbol.get(symbol),
        })

    # Per-currency subtotals are always available (currency gate or not);
    # the template shows them as a fallback when `totals` is None.
    subtotals = []
    for currency, numbers in (pnl.get("subtotals") or {}).items():
        subtotals.append({
            "currency": currency,
            "value": numbers.get("value"),
            "day_pnl": numbers.get("day_pnl"),
            "cost_basis": numbers.get("cost_basis"),
        })

    now = datetime.now(timezone.utc)
    return {
        "fmt": _fmt_helpers,
        "generated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
        "generated_iso": now.isoformat(),
        "rows": rows,
        "totals": pnl.get("totals"),          # None on currency mismatch
        "currency": pnl.get("currency"),
        "currency_mismatch": bool(pnl.get("currency_mismatch")),
        "subtotals": subtotals,
        "missing": pnl.get("missing") or [],
        "warnings": list(warnings or []),
        "analysis": analysis,
        "commentary": (analysis or {}).get("commentary") or None,
        "has_holdings": bool(rows),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_html(pnl, live, news, analysis=None, warnings=None):
    """Render the briefing template to an HTML string (no WeasyPrint needed)."""
    template = _env.get_template(TEMPLATE_NAME)
    return template.render(**_build_context(pnl, live, news, analysis, warnings))


def render_pdf(pnl, live, news, analysis=None, warnings=None):
    """Render the briefing to PDF bytes (WeasyPrint)."""
    import weasyprint  # lazy: needs pango/cairo system libs (Docker image)

    html = render_html(pnl, live, news, analysis, warnings)
    return weasyprint.HTML(string=html, base_url=str(TEMPLATE_DIR)).write_pdf()


def save_report(pnl, live, news, analysis=None, warnings=None,
                output_dir=None) -> Path:
    """Render the briefing and write it to a timestamped PDF.

    Filename: briefing_YYYYMMDD_HHMMSS.pdf (UTC), in `output_dir`
    (default OUTPUT_DIR from config.py).
    """
    directory = Path(output_dir) if output_dir else Path(OUTPUT_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = directory / f"{REPORT_FILENAME_PREFIX}_{stamp}.pdf"
    path.write_bytes(render_pdf(pnl, live, news, analysis, warnings))
    logger.info("Report saved: %s", path)
    return path


def _strip_history(live: dict) -> dict:
    """Copy of `live` without the ~1y close arrays (compact for dev output)."""
    return {
        symbol: {k: v for k, v in quote.items() if k != "history"}
        for symbol, quote in (live or {}).items()
    }


def _validate_html(html: str, analysis: dict | None) -> None:
    """Dev-only sanity checks on the rendered HTML (see __main__)."""
    if not html.rstrip().endswith("</html>"):
        raise SystemExit("HTML check failed: markup truncated")
    if "<!--" in html:
        logger.warning("HTML check: template comment leaked into output")
    # Empty portfolios render no per-holding sections at all, so the
    # notice is (correctly) absent there too - only check it otherwise.
    if (
        analysis is None
        and "No holdings to report" not in html
        and "AI analysis unavailable" not in html
    ):
        raise SystemExit("HTML check failed: analysis-unavailable notice missing")


# ---------------------------------------------------------------------------
# Dev one-shot: python report.py <portfolio.csv>
# Runs the full pipeline (parse -> live -> news -> llm) and writes the PDF.
# Same pattern as the `python llm.py <csv>` dev hook.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    import parse_csv as csv_mod
    from live_data import fetch_live_data
    from llm import LLMAnalysisError, analyze_portfolio
    from news import fetch_news

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if len(sys.argv) < 2:
        print("usage: python report.py <portfolio.csv>")
        sys.exit(1)

    parsed = csv_mod.parse_csv(sys.argv[1])
    symbols = [h["symbol"] for h in parsed["holdings"]]
    live = fetch_live_data(symbols)
    pnl = csv_mod.compute_pnl(parsed["holdings"], live)
    news = fetch_news(symbols)

    analysis = None
    try:
        analysis = analyze_portfolio(pnl, live, news)
    except LLMAnalysisError as e:
        logger.error("LLM analysis unavailable: %s", e)

    # Dev convenience: show what the LLM saw and what it answered, then
    # render the PDF.
    print("--- analysis ---")
    print(json.dumps(analysis, indent=2))
    print("--- live data (without history) ---")
    print(json.dumps(_strip_history(live), indent=2, default=str))

    html = render_html(pnl, live, news, analysis, parsed["warnings"])
    _validate_html(html, analysis)
    Path("last_report.html").write_text(html, encoding="utf-8")
    print("--- wrote last_report.html (HTML preview) ---")

    try:
        path = save_report(pnl, live, news, analysis, parsed["warnings"])
    except Exception as e:  # noqa: BLE001 - dev one-shot: surface, don't hide
        print(f"PDF rendering failed (WeasyPrint/system libs?): {e}", file=sys.stderr)
        sys.exit(1)
    print(f"PDF written: {path}")
