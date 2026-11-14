"""Sensitivity screen for downtrend RSI recovery thresholds; research only."""
from __future__ import annotations

import json
from pathlib import Path

from backtest_downtrend_rsi_recovery import (
    BASE_SYMBOLS, EXPANDED_SYMBOLS, build_signals, run_universe,
)
from backtest_ema72_donchian import SPLITS
from backtest_strategy import FEE_BPS, OUT, SLIPPAGE_BPS, STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS


def main() -> None:
    levels = (30.0, 35.0, 40.0, 45.0, 50.0)
    payload = {
        "purpose": "Sensitivity analysis only; do not select a threshold from reused history as proof of edge.",
        "levels": list(levels),
        "fixed_controls": "Same stable downtrend regime, 0.20x risk, four-percent-capped swing stop, 2xATR target, EMA20/RSI70 exits, portfolio limits, loss halts, and costs as the deployed Testnet candidate.",
        "results": {},
    }
    for level in levels:
        frames = {symbol: build_signals(symbol, level) for symbol in EXPANDED_SYMBOLS}
        payload["results"][str(int(level))] = {}
        for split, (start, end) in SPLITS.items():
            payload["results"][str(int(level))][split] = {}
            for cost, fee, slip in (
                ("base", FEE_BPS, SLIPPAGE_BPS),
                ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS),
            ):
                payload["results"][str(int(level))][split][cost] = {
                    "three_symbols": run_universe(frames, BASE_SYMBOLS, start, end, fee, slip),
                    "eight_symbols": run_universe(frames, EXPANDED_SYMBOLS, start, end, fee, slip),
                }
    OUT.mkdir(exist_ok=True)
    output = OUT / "rsi_recovery_thresholds.json"
    output.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    for level, splits in payload["results"].items():
        recent = splits["review_2026_01_to_10"]
        for cost in ("base", "stress"):
            row = recent[cost]["eight_symbols"]
            print(f"RSI>{level} {cost}: trades={row['closed_trades']}, P&L=${row['net_pnl_usdt']}, PF={row['profit_factor']}, DD={row['max_drawdown_pct']}%")
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
