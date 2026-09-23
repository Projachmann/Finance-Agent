"""Flask entry point and pipeline orchestration.

Pipeline (see finance-agent-plan.md):

    POST /upload (CSV)
      -> parse_csv.py    group by symbol, weighted-avg cost basis, P&L
      -> live_data.py    parallel yfinance quotes + 1y history
      -> news.py         parallel RSS headlines per ticker
      -> llm.py          single Groq call -> per-stock verdicts + commentary
      -> report.py       Jinja2 -> WeasyPrint -> PDF (saved into output/)
      -> PDF returned as a file download

Run inside the container via Gunicorn (see Dockerfile):

    gunicorn -w 1 -b 0.0.0.0:5000 --timeout 120 main:app

Failure policy (agent.md conventions):
  * bad / unusable CSV           -> 400 with a user-facing message
  * ALL live-data fetches fail   -> 502 (a report with zero prices is useless)
  * one bad ticker / empty news  -> carry on (the report degrades gracefully)
  * LLM failure (LLMAnalysisError) -> analysis=None ("AI analysis unavailable")
  * PDF rendering failure        -> 500
"""

import logging
import os
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from config import MAX_UPLOAD_BYTES, OUTPUT_DIR
from live_data import fetch_live_data
from llm import LLMAnalysisError, analyze_portfolio
from news import fetch_news
from parse_csv import compute_pnl, parse_csv
from report import save_report

logger = logging.getLogger("finance_agent.main")


def _setup_logging():
    """Console + file logging (output/app.log).

    The log file lives in the output/ directory (Docker bind mount), so it
    persists across container rebuilds - the ops checklist in
    finance-agent-plan.md calls this out for "dad says it didn't work".
    """
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(),
                    logging.INFO)
    handlers = [logging.StreamHandler()]
    try:
        Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(Path(OUTPUT_DIR) / "app.log",
                                            encoding="utf-8"))
    except OSError as e:
        # Unwritable log location (read-only fs etc.) - console is still there.
        print(f"WARNING: file logging disabled: {e}")
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    # yfinance logs per-ticker INFO noise; warnings and up are the signal.
    logging.getLogger("yfinance").setLevel(logging.WARNING)


_setup_logging()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


def _error(message: str, status: int):
    """JSON error body the upload UI can display verbatim."""
    logger.warning("%d: %s", status, message)
    return jsonify({"error": message}), status


@app.get("/")
def index():
    return render_template("upload.html")


@app.get("/health")
def health():
    """Aliveness check (ops checklist in finance-agent-plan.md)."""
    return jsonify({"status": "ok"}), 200


@app.post("/upload")
def upload():
    file = request.files.get("file")
    if file is None or not file.filename:
        return _error("No CSV file received. Select a Yahoo Finance portfolio "
                      "export and try again.", 400)

    started = time.monotonic()
    timings = {}

    # 1. Parse the CSV (ValueError messages are user-facing by contract).
    t0 = time.monotonic()
    try:
        parsed = parse_csv(file)
    except ValueError as e:
        return _error(str(e), 400)
    except Exception as e:  # noqa: BLE001 - anything else is a server-side fault
        logger.exception("CSV parsing failed")
        return _error(f"Could not read the uploaded CSV: {e}", 400)
    timings["parse"] = time.monotonic() - t0

    holdings = parsed["holdings"]
    symbols = [h["symbol"] for h in holdings]
    logger.info("Parsed %d holding(s): %s", len(holdings), ", ".join(symbols))

    # 2. Live market data. Parallel; a failed ticker is omitted + warned by
    #    live_data.py, and compute_pnl() turns omissions into "missing".
    t0 = time.monotonic()
    live = fetch_live_data(symbols)
    timings["live"] = time.monotonic() - t0
    if not live:
        return _error("No live market data could be fetched for any holding. "
                      "Check the Pi's internet connection and try again in a "
                      "minute.", 502)

    # 3. News. Never raises (per-ticker failure isolation); empty list = none.
    t0 = time.monotonic()
    news = fetch_news(symbols)
    timings["news"] = time.monotonic() - t0

    pnl = compute_pnl(holdings, live)

    # 4. AI analysis. A dead LLM never kills the briefing: render the report
    #    with analysis=None and it shows "AI analysis unavailable".
    t0 = time.monotonic()
    analysis = None
    try:
        analysis = analyze_portfolio(pnl, live, news)
    except LLMAnalysisError as e:
        logger.error("LLM analysis unavailable: %s", e)
    timings["llm"] = time.monotonic() - t0

    # 5. Render + save the PDF (timestamped name in OUTPUT_DIR, which Docker
    #    bind-mounts to ./output on the host, so reports persist).
    t0 = time.monotonic()
    try:
        path = save_report(pnl, live, news, analysis, parsed["warnings"])
    except Exception as e:  # noqa: BLE001 - e.g. WeasyPrint system libs missing
        logger.exception("PDF rendering failed")
        return _error(f"PDF rendering failed: {e}", 500)
    timings["pdf"] = time.monotonic() - t0

    total = time.monotonic() - started
    logger.info(
        "Briefing ready: %s (%.1fs total - parse %.2fs, live %.1fs, news %.1fs, "
        "llm %.1fs, pdf %.1fs)",
        path.name, total, timings["parse"], timings["live"],
        timings["news"], timings["llm"], timings["pdf"],
    )
    return send_file(path, as_attachment=True, download_name=path.name,
                     mimetype="application/pdf")


if __name__ == "__main__":
    # Local dev only (the container always runs Gunicorn; see Dockerfile).
    app.run(host="127.0.0.1", port=5000, debug=True)
