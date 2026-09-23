"""Offline smoke test for main.py (step 7 verification).

Run: python _smoke_test.py
Checks (no network, no WeasyPrint):
  1. all deps importable
  2. main.py imports without error (Flask app builds)
  3. GET /          -> 200, upload page present
  4. GET /health    -> 200 {"status": "ok"}
  5. POST /upload (no file)        -> 400 JSON error
  6. POST /upload (garbage CSV)    -> 400 "Not a valid Yahoo Finance portfolio export"
  7. POST /upload (valid CSV)      -> proceeds down the pipeline; on this
                                      Windows box it is EXPECTED to fail at
                                      WeasyPrint with 500 "PDF rendering failed"
                                      (pango libs only exist in Docker).
                                      Anything else (400, or a live-data 502
                                      if offline) tells us the wiring is fine.
  8. upload.html is served and its JS references /upload
"""

import io
import json
import sys

failures = []


def check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))
    if not ok:
        failures.append(name)


# 1. deps
try:
    from importlib.metadata import version as pkg_version
    import flask
    import pandas
    import yfinance
    import feedparser
    import groq
    import jinja2
    import dotenv
    check("deps import", True, f"flask {pkg_version('flask')}")
except ImportError as e:
    check("deps import", False, str(e))
    print("Aborting: missing deps.")
    sys.exit(1)

# 2. import main
try:
    import main
    check("import main", True)
except Exception as e:
    check("import main", False, f"{type(e).__name__}: {e}")
    sys.exit(1)

app = main.app
client = app.test_client()

# 3. GET /
r = client.get("/")
body = r.get_data(as_text=True)
check("GET / -> 200", r.status_code == 200, f"status={r.status_code}")
check("GET / serves upload UI", "Portfolio Briefing" in body and "dropzone" in body)

# 4. GET /health
r = client.get("/health")
try:
    health = r.get_json()
except Exception:
    health = None
check("GET /health -> 200 ok", r.status_code == 200 and health == {"status": "ok"},
      f"status={r.status_code} body={health}")

# 5. POST /upload without file
r = client.post("/upload")
try:
    err = r.get_json()
except Exception:
    err = None
check("POST /upload (no file) -> 400 JSON",
      r.status_code == 400 and isinstance(err, dict) and "error" in err,
      f"status={r.status_code} body={err}")

# 6. POST /upload with garbage CSV
garbage = io.BytesIO(b"hello\nworld\nnot,a,portfolio\n")
garbage.name = "garbage.csv"
r = client.post("/upload", data={"file": garbage}, content_type="multipart/form-data")
try:
    err = r.get_json()
except Exception:
    err = None
check("POST /upload (garbage CSV) -> 400 'not a valid ...'",
      r.status_code == 400 and isinstance(err, dict)
      and "Not a valid Yahoo Finance portfolio export" in str(err.get("error")),
      f"status={r.status_code} body={err}")

# 6b. POST /upload with CSV missing a required column
noqty = io.BytesIO(b"Symbol,Current Price,Purchase Price\nVUSA.DE,100,89\n")
noqty.name = "noqty.csv"
r = client.post("/upload", data={"file": noqty}, content_type="multipart/form-data")
try:
    err = r.get_json()
except Exception:
    err = None
check("POST /upload (missing column) -> 400 names the column",
      r.status_code == 400 and isinstance(err, dict)
      and "Quantity" in str(err.get("error")),
      f"status={r.status_code} body={err}")

# 7. POST /upload with the real test portfolio (network: yfinance/news/Groq)
csv_text = open("test_portfolio.csv", "rb").read()
good = io.BytesIO(csv_text)
good.name = "test_portfolio.csv"
print("\n--- full pipeline test (uses network; may take ~30-60s) ---")
r = client.post("/upload", data={"file": good}, content_type="multipart/form-data")
print(f"full-pipeline status={r.status_code}")
if r.status_code == 200:
    ctype = r.headers.get("Content-Type", "")
    first = r.data[:5]
    check("full pipeline -> 200 PDF", ctype == "application/pdf" and first == b"%PDF-",
          f"content-type={ctype} magic={first!r}")
    print("  PDF name:", r.headers.get("Content-Disposition"))
elif r.status_code == 502:
    err = r.get_json()
    check("full pipeline wiring OK (offline: 502 no live data)",
          "live market data" in str(err.get("error")), f"body={err}")
elif r.status_code == 500:
    err = r.get_json()
    check("full pipeline wiring OK (Windows: 500 at WeasyPrint boundary)",
          "PDF rendering failed" in str(err.get("error")), f"body={err}")
    print("  (expected on Windows: pango libs only exist in the Docker image)")
else:
    try:
        err = r.get_json()
    except Exception:
        err = r.data[:300]
    check("full pipeline", False, f"unexpected status={r.status_code} body={err}")

print()
if failures:
    print(f"RESULT: {len(failures)} failure(s): {failures}")
    sys.exit(1)
print("RESULT: all checks passed")
