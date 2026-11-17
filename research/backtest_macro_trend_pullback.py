"""Independent replay of the Macro Trend Pullback (1H EMA20 pullback with EMA36 exit).

Research only. This script does not modify production configuration.
It reuses the project's hourly portfolio simulator and risk-halt implementation.
"""
from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pandas as pd
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange

from backtest_strategy import (
    DATA, OUT, FEE_BPS, SLIPPAGE_BPS, STRESS_FEE_BPS,
    STRESS_SLIPPAGE_BPS, simulate, summarize,
)

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
HOUR_MS = 3_600_000
SPLITS = {
    "train_2021_02_to_2023": (
        int(pd.Timestamp("2021-02-01", tz="UTC").timestamp() * 1000),
        int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000),
    ),
    "validation_2024_to_2025": (
        int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000),
        int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000),
    ),
    "review_2026_01_to_10": (
        int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000),
        int(pd.Timestamp("2026-11-01", tz="UTC").timestamp() * 1000),
    ),
}


def build_signals(symbol: str, max_atr_pct: float = 3.0) -> pd.DataFrame:
    path = DATA / f"{symbol}_1h.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist")
    raw = (pd.read_csv(path)
           .drop_duplicates("timestamp_ms")
           .sort_values("timestamp_ms")
           .reset_index(drop=True))
    required = {"timestamp_ms", "open", "high", "low", "close", "volume"}
    missing = required.difference(raw.columns)
    if missing:
        raise ValueError(f"{path} is missing columns: {sorted(missing)}")
    if raw.empty or raw[list(required)].isna().any().any():
        raise ValueError(f"{path} is empty or contains null required data")

    close, high, low, open_ = raw.close, raw.high, raw.low, raw.open
    ema9 = EMAIndicator(close=close, window=9).ema_indicator()
    ema20 = EMAIndicator(close=close, window=20).ema_indicator()
    ema50 = EMAIndicator(close=close, window=50).ema_indicator()
    ema36 = EMAIndicator(close=close, window=36).ema_indicator()
    ema200 = EMAIndicator(close=close, window=200).ema_indicator()
    atr = AverageTrueRange(high, low, close, window=14).average_true_range()

    contiguous = raw.timestamp_ms.diff().eq(HOUR_MS).rolling(600, min_periods=600).sum().eq(600)
    ready = contiguous & ema9.notna() & ema20.notna() & ema50.notna() & ema36.notna() & ema200.notna() & atr.notna()
    atr_pct = atr / close * 100.0

    macro_bullish = close.gt(ema200) & ema20.gt(ema50) & ema200.gt(ema200.shift(24))
    pulled_back = low.le(ema20) | low.shift(1).le(ema20.shift(1))
    reclaimed = close.gt(open_) & close.gt(ema9)
    atr_ok = atr_pct.le(max_atr_pct)

    buy = ready & macro_bullish & pulled_back & reclaimed & atr_ok
    sell = ready & close.lt(ema36)

    result = raw[["timestamp_ms", "open", "high", "low", "close", "volume"]].copy()
    result["atr"] = atr
    result["atr_pct"] = atr_pct
    result["ema20"] = ema20
    result["ema36"] = ema36
    result["ema50"] = ema50
    result["ema200"] = ema200
    result["swing_low_reference"] = low.shift(1).rolling(5, min_periods=5).min()
    result["sequence_ok"] = ready
    result["signal"] = np.where(sell, "SELL", np.where(buy, "BUY", "HOLD"))
    return result


def main() -> dict:
    frames = {symbol: build_signals(symbol) for symbol in SYMBOLS}
    result: dict = {
        "candidate": "MacroTrendPullback",
        "symbols": list(SYMBOLS),
        "timeframe": "1h",
        "splits": {},
    }
    cost_scenarios = (
        ("base", FEE_BPS, SLIPPAGE_BPS),
        ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS),
    )
    for split, (start, end) in SPLITS.items():
        result["splits"][split] = {}
        for cost_name, fee, slip in cost_scenarios:
            result["splits"][split][cost_name] = {}
            for halt_name, enforce, loss_limit in (
                ("no_halts", False, 3),
                ("halt_cap_5", True, 5),
            ):
                records = simulate(
                    frames, start_ms=start, end_ms=end,
                    fee_bps=fee, slippage_bps=slip,
                    enforce_halts=enforce,
                    consecutive_loss_limit=loss_limit,
                    stop_profile="swing_low_floor_4pct",
                    target_atr_multiple=100.0,
                    enable_breakeven_trailing=False,
                )
                stats = summarize(records, initial_equity=10000.0)
                result["splits"][split][cost_name][halt_name] = stats

    OUT.mkdir(parents=True, exist_ok=True)
    out_file = OUT / "macro_trend_pullback_results.json"
    with open(out_file, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Saved results to {out_file}")
    return result


if __name__ == "__main__":
    main()
