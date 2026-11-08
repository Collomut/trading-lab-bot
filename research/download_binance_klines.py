from __future__ import annotations
import concurrent.futures as cf
import csv
import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

OUT = Path(__file__).parent / "data"
OUT.mkdir(exist_ok=True)
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
START_YEAR, START_MONTH = 2021, 1
END_YEAR, END_MONTH = 2026, 10  # completed months only
BASE = "https://data.binance.vision/data/spot/monthly/klines/{symbol}/1h/{symbol}-1h-{year:04d}-{month:02d}.zip"

def months():
    y, m = START_YEAR, START_MONTH
    while (y, m) <= (END_YEAR, END_MONTH):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

def fetch_one(item):
    symbol, year, month = item
    url = BASE.format(symbol=symbol, year=year, month=month)
    request = urllib.request.Request(url, headers={"User-Agent": "strategy-audit/1.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                payload = response.read()
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                if not names:
                    raise ValueError(f"Archive contains no CSV: {url}")
                raw = zf.read(names[0]).decode("utf-8-sig")
            rows = []
            for row in csv.reader(io.StringIO(raw)):
                if len(row) < 6:
                    continue
                try:
                    ts = int(row[0])
                    vals = [float(v) for v in row[1:6]]
                    if ts > 10**15:  # Binance changed Spot archive timestamps to microseconds in 2025
                        ts //= 1000
                    rows.append([ts, *vals])
                except (ValueError, TypeError):
                    continue
            return {"symbol": symbol, "year": year, "month": month, "rows": rows, "url": url, "bytes": len(payload), "error": None}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"symbol": symbol, "year": year, "month": month, "rows": [], "url": url, "bytes": 0, "error": "404"}
            if attempt == 3:
                return {"symbol": symbol, "year": year, "month": month, "rows": [], "url": url, "bytes": 0, "error": f"HTTP {exc.code}"}
            time.sleep(1.5 * (attempt + 1))
        except Exception as exc:
            if attempt == 3:
                return {"symbol": symbol, "year": year, "month": month, "rows": [], "url": url, "bytes": 0, "error": f"{type(exc).__name__}: {exc}"}
            time.sleep(1.5 * (attempt + 1))

items = [(symbol, year, month) for symbol in SYMBOLS for year, month in months()]
results = []
with cf.ThreadPoolExecutor(max_workers=6) as pool:
    for i, result in enumerate(pool.map(fetch_one, items), 1):
        results.append(result)
        if i % 15 == 0 or i == len(items):
            print(f"Fetched archives {i}/{len(items)}")

manifest = {"source": "Binance official Spot Public Data monthly klines", "interval": "1h", "completed_months_through": f"{END_YEAR:04d}-{END_MONTH:02d}", "requested_symbols": SYMBOLS, "archives": []}
for symbol in SYMBOLS:
    rows = [row for result in results if result["symbol"] == symbol for row in result["rows"]]
    rows.sort(key=lambda x: x[0])
    # Deduplicate by opening timestamp and write canonical OHLCV only.
    dedup = {row[0]: row for row in rows}
    rows = [dedup[k] for k in sorted(dedup)]
    path = OUT / f"{symbol}_1h.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_ms", "open", "high", "low", "close", "volume"])
        writer.writerows(rows)
    symbol_results = [r for r in results if r["symbol"] == symbol]
    errors = [r for r in symbol_results if r["error"] and r["error"] != "404"]
    missing = [r for r in symbol_results if r["error"] == "404"]
    manifest["archives"].append({"symbol": symbol, "row_count": len(rows), "first_timestamp_ms": rows[0][0] if rows else None, "last_timestamp_ms": rows[-1][0] if rows else None, "archive_count": len(symbol_results)-len(missing)-len(errors), "missing_archive_count": len(missing), "errors": errors})
(OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
for item in manifest["archives"]:
    print(json.dumps(item))
