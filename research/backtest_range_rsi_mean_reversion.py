"""Replay a range-regime RSI mean-reversion candidate; research only, no orders.

Pre-registered rules (fixed before running, not fitted to the archive):
  - Regime: three consecutive completed hourly bars classified "range"
    (ADX14 <= 18 and ATR14 <= 3% of close), mirroring regime_router.py.
  - Entry: RSI(14) crosses up through 30 while the range regime is stable.
  - Exit: RSI(14) >= 50 (revert toward neutral) OR close > EMA(20).
  - Stop: prior-five-bar swing low capped at 4% distance; 2x ATR target.
  - Risk: 0.50x base risk (mean-reversion is noisier than the trend candidate).

Uses the shared hourly portfolio simulator with the same fees/slippage, halts,
next-open fills, stop-first intrabar assumption, and portfolio constraints as
every other candidate in this repository.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, EMAIndicator
from ta.volatility import AverageTrueRange

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from backtest_ema72_donchian import SPLITS
from backtest_strategy import (
    DATA, OUT, FEE_BPS, SLIPPAGE_BPS, STRESS_FEE_BPS,
    STRESS_SLIPPAGE_BPS, simulate, summarize,
)

HOUR_MS = 3_600_000
BASE_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
EXPANDED_SYMBOLS = BASE_SYMBOLS + ("BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT")
CANDIDATE = "range_rsi_mean_reversion"


def build_signals(symbol: str) -> pd.DataFrame:
    raw = (pd.read_csv(DATA / f"{symbol}_1h.csv")
           .drop_duplicates("timestamp_ms")
           .sort_values("timestamp_ms")
           .reset_index(drop=True))
    close, high, low = raw.close, raw.high, raw.low
    rsi = RSIIndicator(close, window=14).rsi()
    ema20 = EMAIndicator(close, window=20).ema_indicator()
    adx = ADXIndicator(high, low, close, window=14).adx()
    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    swing_low = low.shift(1).rolling(5, min_periods=5).min()
    contiguous = raw.timestamp_ms.diff().eq(HOUR_MS).rolling(2499, min_periods=2499).sum().eq(2499)
    valid = contiguous & adx.notna() & atr.notna() & rsi.notna() & ema20.notna()
    atr_pct = atr / close * 100.0
    raw_range = valid & (adx <= 18.0) & (atr_pct <= 3.0)
    stable_range = raw_range & raw_range.shift(1, fill_value=False) & raw_range.shift(2, fill_value=False)
    buy = stable_range & (rsi.shift(1) < 30.0) & (rsi >= 30.0)
    sell = (rsi >= 50.0) | (close > ema20)

    result = raw[["timestamp_ms", "open", "high", "low", "close", "volume"]].copy()
    result["atr"] = atr
    result["swing_low_reference"] = swing_low
    result["risk_multiplier"] = 0.50
    result["signal"] = np.where(sell, "SELL", np.where(buy, "BUY", "HOLD"))
    return result

def run_universe(all_frames, symbols, start, end, fee, slip, enforce_halts) -> dict:
    frames = {symbol: all_frames[symbol] for symbol in symbols}
    sim = simulate(
        frames, fee, slip, enforce_risk_halts=enforce_halts,
        start_timestamp_ms=start, end_timestamp_ms=end,
        initial_stop_model="swing_low_floor_4pct",
        take_profit_atr_multiple=2.0,
        sizing_multiplier_col="risk_multiplier",
        enable_breakeven_trailing=False,
    )
    result = summarize(CANDIDATE, sim)
    result["halt_reason"] = sim["halt_reason"]
    result["halt_timestamp_ms"] = sim["halt_time"]
    return result


def main() -> None:
    all_frames = {symbol: build_signals(symbol) for symbol in EXPANDED_SYMBOLS}
    payload = {
        "candidate": CANDIDATE,
        "implementation": (
            "Three consecutive completed hourly range classifications (ADX14<=18, ATR14<=3%); "
            "RSI14 crosses up through 30; exits on RSI14>=50 or close>EMA20; prior-five-bar swing "
            "stop capped at 4%; 2x ATR target; 0.50x base risk; shared simulator caps and halts."
        ),
        "costs_per_side_bps": {"base": {"fee": FEE_BPS, "slippage": SLIPPAGE_BPS},
                               "stress": {"fee": STRESS_FEE_BPS, "slippage": STRESS_SLIPPAGE_BPS}},
        "limitations": [
            "Fixed 8-coin universe is survivorship-biased",
            "2026 review data has already been used in project research",
            "OHLCV cannot recreate spread, queue position, or exact intrabar execution",
            "Positive historical sample does not establish an edge",
        ],
        "results": {},
    }
    for split, (start, end) in SPLITS.items():
        payload["results"][split] = {}
        for cost, fee, slip in (("base", FEE_BPS, SLIPPAGE_BPS),
                                ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS)):
            payload["results"][split][cost] = {
                "no_halts": {
                    "three_symbols": run_universe(all_frames, BASE_SYMBOLS, start, end, fee, slip, False),
                    "eight_symbols": run_universe(all_frames, EXPANDED_SYMBOLS, start, end, fee, slip, False),
                },
                "existing_halts": {
                    "three_symbols": run_universe(all_frames, BASE_SYMBOLS, start, end, fee, slip, True),
                    "eight_symbols": run_universe(all_frames, EXPANDED_SYMBOLS, start, end, fee, slip, True),
                },
            }
    OUT.mkdir(exist_ok=True)
    output = OUT / "range_rsi_mean_reversion_results.json"
    output.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    for split, costs in payload["results"].items():
        print(split)
        for cost, modes in costs.items():
            for mode, universes in modes.items():
                for universe, stats in universes.items():
                    print(f"  {cost} {mode} {universe}: trades={stats['closed_trades']}, "
                          f"P&L=${stats['net_pnl_usdt']}, PF={stats['profit_factor']}, "
                          f"DD={stats['max_drawdown_pct']}%, halt={stats['halt_reason']}")
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
