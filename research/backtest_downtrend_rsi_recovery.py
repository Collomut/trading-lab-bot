"""Replay the downtrend RSI-recovery experiment; research only, no orders.

Matches the Testnet implementation: stable three-hour downtrend regime,
completed-candle RSI(14) cross above 40, EMA20/RSI70 signal exits, prior-five
bar swing stop with a 4% maximum gap, 2x ATR target, and 0.20x base risk.
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


def build_signals(symbol: str) -> pd.DataFrame:
    raw = (pd.read_csv(DATA / f"{symbol}_1h.csv").drop_duplicates("timestamp_ms")
           .sort_values("timestamp_ms").reset_index(drop=True))
    close, high, low = raw.close, raw.high, raw.low
    rsi = RSIIndicator(close, window=14).rsi()
    ema20 = EMAIndicator(close, window=20).ema_indicator()
    ema200 = EMAIndicator(close, window=200).ema_indicator()
    adx = ADXIndicator(high, low, close, window=14).adx()
    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    swing_low = low.shift(1).rolling(5, min_periods=5).min()
    contiguous = raw.timestamp_ms.diff().eq(HOUR_MS).rolling(2499, min_periods=2499).sum().eq(2499)
    valid = contiguous & adx.notna() & atr.notna() & ema200.notna() & ema200.shift(24).notna()
    atr_pct = atr / close * 100.0
    raw_down = valid & (adx >= 25.0) & (atr_pct <= 3.0) & (close < ema200) & (ema200 < ema200.shift(24))
    stable_down = raw_down & raw_down.shift(1, fill_value=False) & raw_down.shift(2, fill_value=False)
    buy = stable_down & (rsi.shift(1) < 40.0) & (rsi >= 40.0)
    sell = (close > ema20) | (rsi > 70.0)
    result = raw[["timestamp_ms", "open", "high", "low", "close", "volume"]].copy()
    result["atr"] = atr
    result["swing_low_reference"] = swing_low
    result["risk_multiplier"] = 0.20
    result["signal"] = np.where(sell, "SELL", np.where(buy, "BUY", "HOLD"))
    return result


def run_universe(all_frames: dict[str, pd.DataFrame], symbols: tuple[str, ...], start: int, end: int, fee: float, slip: float) -> dict:
    frames = {symbol: all_frames[symbol] for symbol in symbols}
    sim = simulate(
        frames, fee, slip, enforce_risk_halts=True,
        start_timestamp_ms=start, end_timestamp_ms=end,
        initial_stop_model="swing_low_floor_4pct",
        take_profit_atr_multiple=2.0,
        sizing_multiplier_col="risk_multiplier",
        enable_breakeven_trailing=False,
    )
    result = summarize("downtrend_rsi_recovery_testnet", sim)
    result["halt_reason"] = sim["halt_reason"]
    result["halt_timestamp_ms"] = sim["halt_time"]
    return result


def main() -> None:
    all_frames = {symbol: build_signals(symbol) for symbol in EXPANDED_SYMBOLS}
    payload = {
        "candidate": "downtrend_rsi_recovery_testnet",
        "implementation": "Three consecutive completed hourly downtrend classifications; RSI14 crosses above 40; EMA20/RSI70 exits; swing-low stop capped at 4%; 2x ATR take-profit; 0.20x normal risk budget; same shared simulator portfolio caps and halts.",
        "costs_per_side_bps": {"base": {"fee": FEE_BPS, "slippage": SLIPPAGE_BPS},
                               "stress": {"fee": STRESS_FEE_BPS, "slippage": STRESS_SLIPPAGE_BPS}},
        "limitations": ["Fixed 8-coin universe is survivorship-biased", "2026 review data has already been used in project research", "OHLCV cannot recreate spread, queue position, or exact intrabar execution", "Positive historical sample does not establish an edge"],
        "results": {},
    }
    for split, (start, end) in SPLITS.items():
        payload["results"][split] = {}
        for cost, fee, slip in (("base", FEE_BPS, SLIPPAGE_BPS),
                                ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS)):
            payload["results"][split][cost] = {
                "three_symbols": run_universe(all_frames, BASE_SYMBOLS, start, end, fee, slip),
                "eight_symbols": run_universe(all_frames, EXPANDED_SYMBOLS, start, end, fee, slip),
            }
    OUT.mkdir(exist_ok=True)
    output = OUT / "downtrend_rsi_recovery_results.json"
    output.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    for split, costs in payload["results"].items():
        print(split)
        for cost, universes in costs.items():
            for universe, stats in universes.items():
                print(f"  {cost} {universe}: trades={stats['closed_trades']}, P&L=${stats['net_pnl_usdt']}, PF={stats['profit_factor']}, DD={stats['max_drawdown_pct']}%, halt={stats['halt_reason']}")
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
