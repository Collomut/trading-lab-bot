"""Readiness analysis: per-symbol edge and take-profit sensitivity for the EMA72/Donchian candidate.

Research only, no orders. Uses the project's own simulator and committed CSV
archive. Answers two questions the pre-launch audit flags as open:

1. Is the 8-symbol universe net-positive in the 2026 holdout, or are specific
   coins dragging down profit factor and accuracy?
2. Does a finite ATR take-profit (vs the distant 100xATR approximation) improve
   win rate and drawdown without destroying profit factor?

Run: python research/backtest_readiness_analysis.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from backtest_ema72_donchian import SPLITS, build_signals
from backtest_strategy import FEE_BPS, SLIPPAGE_BPS, simulate, summarize

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT")
CORE = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def _stats(name, sim) -> dict:
    s = summarize(name, sim)
    return {
        "trades": s["closed_trades"],
        "wins": s["wins"],
        "win_rate_pct": s["win_rate_pct"],
        "pf": s["profit_factor"],
        "pnl": s["net_pnl_usdt"],
        "dd": s["max_drawdown_pct"],
    }


def _fmt(d: dict) -> str:
    pf = d["pf"]
    pf = f"{pf:.2f}" if isinstance(pf, (int, float)) else str(pf)
    return (f"trades={d['trades']:3d} wins={d['wins']:3d} "
            f"win%={d['win_rate_pct']:5.1f} PF={pf} PnL=${d['pnl']:8.2f} DD={d['dd']:5.2f}%")


def main() -> None:
    frames = {s: build_signals(s) for s in SYMBOLS}

    print("=== Per-symbol, base cost, no halts ===")
    for symbol in SYMBOLS:
        line = f"{symbol:9s} "
        for split, (start, end) in SPLITS.items():
            sim = simulate({symbol: frames[symbol]}, FEE_BPS, SLIPPAGE_BPS,
                           enforce_risk_halts=False, start_timestamp_ms=start, end_timestamp_ms=end,
                           initial_stop_model="swing_low_floor_4pct",
                           take_profit_atr_multiple=100.0, enable_breakeven_trailing=False)
            line += f"{split[:4]}: {_fmt(_stats(symbol, sim))} | "
        print(line)

    print()
    print("=== 8-symbol universe, take-profit sensitivity, base cost ===")
    for tp in (100.0, 3.0, 2.0):
        for halts in (False, True):
            label = f"TP={tp:5.1f}ATR halts={halts}"
            line = f"{label:26s} "
            for split, (start, end) in SPLITS.items():
                sim = simulate(frames, FEE_BPS, SLIPPAGE_BPS,
                               enforce_risk_halts=halts, start_timestamp_ms=start, end_timestamp_ms=end,
                               initial_stop_model="swing_low_floor_4pct",
                               take_profit_atr_multiple=tp, enable_breakeven_trailing=False)
                line += f"{split[:4]}: {_fmt(_stats('u', sim))} | "
            print(line)

    print()
    print("=== 3-symbol core universe, take-profit sensitivity, base cost ===")
    core_frames = {s: frames[s] for s in CORE}
    for tp in (100.0, 3.0, 2.0):
        line = f"TP={tp:5.1f}ATR            "
        for split, (start, end) in SPLITS.items():
            sim = simulate(core_frames, FEE_BPS, SLIPPAGE_BPS,
                           enforce_risk_halts=False, start_timestamp_ms=start, end_timestamp_ms=end,
                           initial_stop_model="swing_low_floor_4pct",
                           take_profit_atr_multiple=tp, enable_breakeven_trailing=False)
            line += f"{split[:4]}: {_fmt(_stats('core', sim))} | "
        print(line)


if __name__ == "__main__":
    main()
