"""Download completed Binance Spot monthly kline archives for research."""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

OUT = Path(__file__).resolve().parent / "data"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def months(start_year: int, start_month: int, end_year: int, end_month: int):
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def fetch(item):
    symbol, interval, year, month = item
    url = ("https://data.binance.vision/data/spot/monthly/klines/"
           f"{symbol}/{interval}/{symbol}-{interval}-{year:04d}-{month:02d}.zip")
    request = urllib.request.Request(url, headers={"User-Agent": "strategy-research/1.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = response.read()
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                csv_name = next(name for name in archive.namelist() if name.lower().endswith(".csv"))
                text = archive.read(csv_name).decode("utf-8-sig")
            rows = []
            for row in csv.reader(io.StringIO(text)):
                if len(row) < 6:
                    continue
                try:
                    timestamp = int(row[0])
                    if timestamp > 10**15:
                        timestamp //= 1000
                    values = [float(value) for value in row[1:6]]
                    rows.append([timestamp, *values])
                except (TypeError, ValueError):
                    continue
            return {"symbol": symbol, "year": year, "month": month,
                    "rows": rows, "url": url, "error": None}
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return {"symbol": symbol, "year": year, "month": month,
                        "rows": [], "url": url, "error": "404"}
            message = f"HTTP {error.code}"
        except Exception as error:  # retry transient archive/network failures
            message = f"{type(error).__name__}: {error}"
        if attempt == 3:
            return {"symbol": symbol, "year": year, "month": month,
                    "rows": [], "url": url, "error": message}
        time.sleep(4.0 * (attempt + 1))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", default="15m", choices=("15m", "1h"))
    parser.add_argument("--start-year", type=int, default=2021)
    parser.add_argument("--start-month", type=int, default=1)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--end-month", type=int, default=10)
    args = parser.parse_args()
    items = [(symbol, args.interval, year, month)
             for symbol in SYMBOLS
             for year, month in months(args.start_year, args.start_month,
                                       args.end_year, args.end_month)]
    # Keep outbound requests low: aggressive parallel connections can trigger
    # transient DNS/proxy failures in constrained research environments.
    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(fetch, items))

    manifest = {"source": "Binance official Spot Public Data monthly klines",
                "interval": args.interval,
                "requested_range": f"{args.start_year:04d}-{args.start_month:02d} through {args.end_year:04d}-{args.end_month:02d}",
                "requested_symbols": SYMBOLS, "archives": []}
    OUT.mkdir(exist_ok=True)
    for symbol in SYMBOLS:
        symbol_results = [result for result in results if result["symbol"] == symbol]
        by_timestamp = {row[0]: row for result in symbol_results for row in result["rows"]}
        rows = [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]
        path = OUT / f"{symbol}_{args.interval}.csv"
        with path.open("w", newline="", encoding="utf-8") as output:
            writer = csv.writer(output)
            writer.writerow(["timestamp_ms", "open", "high", "low", "close", "volume"])
            writer.writerows(rows)
        manifest["archives"].append({
            "symbol": symbol, "row_count": len(rows),
            "first_timestamp_ms": rows[0][0] if rows else None,
            "last_timestamp_ms": rows[-1][0] if rows else None,
            "downloaded_months": sum(not item["error"] for item in symbol_results),
            "missing_months": sum(item["error"] == "404" for item in symbol_results),
            "errors": [{"month": f"{item['year']:04d}-{item['month']:02d}",
                        "error": item["error"], "url": item["url"]}
                       for item in symbol_results if item["error"] and item["error"] != "404"],
        })
    manifest_path = OUT / f"manifest_{args.interval}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    failed = [item["symbol"] for item in manifest["archives"]
              if item["row_count"] == 0 or item["errors"]]
    if failed:
        raise SystemExit(f"Incomplete market data for: {', '.join(failed)}; see {manifest_path}")


if __name__ == "__main__":
    main()
