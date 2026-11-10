"""Historical trade-count estimate for the remaining November calendar window."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from backtest_strategy import DATA, OUT, FEE_BPS, SLIPPAGE_BPS, compute_signals, simulate

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
WINDOW_START = datetime(2026, 11, 11, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 12, 1, tzinfo=timezone.utc)


def timestamp_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def month_count(trades: list[dict], year: int, month: int, start_day: int = 1) -> int:
    start = timestamp_ms(datetime(year, month, start_day, tzinfo=timezone.utc))
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
    end = timestamp_ms(datetime(end_year, end_month, 1, tzinfo=timezone.utc))
    return sum(start <= trade["exit_time"] < end for trade in trades)


def summarize(name: str, min_adx: float) -> dict:
    frames = {symbol: compute_signals(symbol, interval="1h", min_trend_adx=min_adx)
              for symbol in SYMBOLS}
    sim = simulate(frames, FEE_BPS, SLIPPAGE_BPS, enforce_risk_halts=False)
    trades = sim["trades"]
    monthly = {f"{year}-{month:02d}": month_count(trades, year, month)
               for year in range(2021, 2027) for month in range(1, 13)
               if (year, month) <= (2026, 10)}
    nov_windows = {
        str(year): sum(timestamp_ms(datetime(year, 11, 11, tzinfo=timezone.utc)) <= t["exit_time"]
                       < timestamp_ms(datetime(year, 12, 1, tzinfo=timezone.utc)) for t in trades)
        for year in range(2021, 2026)
    }
    ytd_start = timestamp_ms(datetime(2026, 1, 1, tzinfo=timezone.utc))
    ytd_end = timestamp_ms(datetime(2026, 11, 1, tzinfo=timezone.utc))
    ytd_trades = sum(ytd_start <= t["exit_time"] < ytd_end for t in trades)
    active_days = (datetime(2026, 11, 1, tzinfo=timezone.utc)
                   - datetime(2026, 1, 1, tzinfo=timezone.utc)).days
    days_remaining = (WINDOW_END - WINDOW_START).total_seconds() / 86400
    ytd_daily_projection = ytd_trades / active_days * days_remaining
    november_counts = list(nov_windows.values())
    return {
        "strategy": name,
        "min_adx": min_adx,
        "symbols": SYMBOLS,
        "remaining_window_utc": [WINDOW_START.isoformat(), WINDOW_END.isoformat()],
        "historical_november_11_to_december_1_trade_counts": nov_windows,
        "historical_november_window_mean": round(sum(november_counts) / len(november_counts), 2),
        "historical_november_window_median": sorted(november_counts)[len(november_counts) // 2],
        "historical_november_window_range": [min(november_counts), max(november_counts)],
        "historical_monthly_trade_counts": monthly,
        "2026_jan_through_oct_completed_trades": ytd_trades,
        "2026_jan_through_oct_mean_per_month": round(ytd_trades / 10, 2),
        "simple_2026_daily_rate_projection_for_remaining_window": round(ytd_daily_projection, 2),
        "all_sample_completed_trades": len(trades),
        "limitations": [
            "Forecast is a historical count-rate estimate, not a promise or trade signal.",
            "The current deployment has about one day of observed recent cycles with no open positions; logs do not expose enough detail to infer a reliable live hazard rate.",
            "November samples are only five years and old data has been used for strategy development.",
            "Assumes the same 1h interval, three symbols, strategy, and uninterrupted operation without a manual/risk halt.",
            "Counts positions on close; the live dashboard may count entries and exits differently.",
        ],
    }


def main():
    output = {
        "source": "Binance public Spot monthly 1h klines, January 2021 through October 2026",
        "base_costs_per_side_bps": {"fee": FEE_BPS, "slippage": SLIPPAGE_BPS},
        "risk_halts": "Disabled for the rate projection; the current consecutive-loss halt would invalidate a simple constant-rate forecast if triggered.",
        "variants": [summarize("current_ADX18", 18.0), summarize("candidate_ADX20", 20.0)],
    }
    OUT.mkdir(exist_ok=True)
    path = OUT / "month_end_trade_estimate.json"
    path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    for variant in output["variants"]:
        print(json.dumps({k: variant[k] for k in (
            "strategy", "historical_november_11_to_december_1_trade_counts",
            "historical_november_window_mean", "historical_november_window_median",
            "historical_november_window_range", "2026_jan_through_oct_completed_trades",
            "2026_jan_through_oct_mean_per_month",
            "simple_2026_daily_rate_projection_for_remaining_window",
        )}, indent=2))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
