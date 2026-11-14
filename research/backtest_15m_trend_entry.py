"""Test whether a finer execution timeframe raises activity without weakening gates.

Research only. Keeps the active 168-hour breakout, 480-hour macro EMA,
72-hour exit EMA, uptrend regime policy, position sizing, stops, and halts.
The 15m variant uses equivalent time windows and a causal hourly regime gate.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from ta.trend import ADXIndicator, EMAIndicator
from ta.volatility import AverageTrueRange

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from backtest_ema72_donchian import SPLITS, SYMBOLS
from backtest_strategy import (
    DATA, OUT, FEE_BPS, SLIPPAGE_BPS, STRESS_FEE_BPS,
    STRESS_SLIPPAGE_BPS, simulate, summarize,
)

HOUR = 3_600_000
QUARTER = 900_000


def stable_hourly_gate(symbol: str) -> pd.DataFrame:
    raw = pd.read_csv(DATA / f"{symbol}_15m.csv").sort_values("timestamp_ms").reset_index(drop=True)
    # Exchange candle timestamps mark opens. An hourly bar is available only
    # after all four 15m bars have closed.
    raw["hour"] = raw.timestamp_ms // HOUR * HOUR
    hourly = raw.groupby("hour", sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"), count=("close", "size"),
    ).reset_index()
    contiguous = hourly.hour.diff().eq(HOUR).rolling(2500, min_periods=2500).sum().eq(2500)
    valid_hour = hourly["count"].eq(4)
    adx = ADXIndicator(hourly.high, hourly.low, hourly.close, window=14).adx()
    atr = AverageTrueRange(hourly.high, hourly.low, hourly.close, window=14).average_true_range()
    ema = EMAIndicator(hourly.close, window=200).ema_indicator()
    up = (valid_hour & contiguous & (adx >= 25) & (atr / hourly.close * 100 <= 3)
          & (hourly.close > ema) & (ema > ema.shift(24)))
    stable = up & up.shift(1, fill_value=False) & up.shift(2, fill_value=False)
    return pd.DataFrame({"decision_ts": hourly.hour + HOUR, "stable_uptrend": stable})


def build_15m(symbol: str) -> pd.DataFrame:
    raw = pd.read_csv(DATA / f"{symbol}_15m.csv").sort_values("timestamp_ms").reset_index(drop=True)
    close, high, low = raw.close, raw.high, raw.low
    # Preserve the active strategy's durations in wall-clock time.
    macro = EMAIndicator(close, window=480 * 4).ema_indicator()
    exit_ema = EMAIndicator(close, window=72 * 4).ema_indicator()
    channel = high.shift(1).rolling(168 * 4, min_periods=168 * 4).max()
    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    contiguous = raw.timestamp_ms.diff().eq(QUARTER).rolling(2500, min_periods=2500).sum().eq(2500)
    ready = contiguous & macro.notna() & exit_ema.notna() & atr.notna()
    gate = stable_hourly_gate(symbol)
    bars = pd.DataFrame({"decision_ts": raw.timestamp_ms + QUARTER})
    bars = pd.merge_asof(bars, gate.sort_values("decision_ts"), on="decision_ts", direction="backward")
    allow = bars.stable_uptrend.fillna(False).to_numpy(dtype=bool)
    buy = ready & (close > channel) & (close > macro) & allow
    sell = ready & (close < exit_ema)
    result = raw[["timestamp_ms", "open", "high", "low", "close", "volume"]].copy()
    result["atr"] = atr
    result["swing_low_reference"] = low.shift(1).rolling(5, min_periods=5).min()
    result["signal"] = np.where(sell, "SELL", np.where(buy, "BUY", "HOLD"))
    return result


def main() -> None:
    frames = {s: build_15m(s) for s in SYMBOLS}
    results = {}
    for split, (start, end) in SPLITS.items():
        results[split] = {}
        months = (end - start) / (30.4375 * 24 * HOUR)
        for label, fee, slip in (("base", FEE_BPS, SLIPPAGE_BPS), ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS)):
            sim = simulate(frames, fee, slip, enforce_risk_halts=True,
                           start_timestamp_ms=start, end_timestamp_ms=end,
                           initial_stop_model="swing_low_floor_4pct",
                           take_profit_atr_multiple=100.0,
                           enable_breakeven_trailing=False)
            stats = summarize(f"15m_{split}_{label}", sim)
            stats["trades_per_month"] = round(stats["closed_trades"] / months, 3)
            stats["halt_reason"] = sim["halt_reason"]
            results[split][label] = stats
    payload = {
        "method": "15m execution candidate; exact 168h channel, 480h EMA, 72h exit, stable hourly uptrend regime; same costs, stops, sizing, correlation and halts as hourly production replay.",
        "symbols": list(SYMBOLS),
        "limitations": ["15m archive has only BTC/ETH/SOL", "historical period is reused and not a clean holdout", "intrabar spread and fills are not reconstructed"],
        "results": results,
    }
    out = OUT / "15m_trend_entry_results.json"
    out.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(results, indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
