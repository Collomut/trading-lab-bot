"""Predeclared conservative filter tests for the active 1-hour strategy."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_strategy import (
    DATA, OUT, FEE_BPS, SLIPPAGE_BPS, STRESS_FEE_BPS,
    STRESS_SLIPPAGE_BPS, compute_signals, simulate,
)

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
PERIODS = (
    ("2021_2023", None, int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)),
    ("2024_2025", int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000),
     int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000)),
    ("2026_through_oct", int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000), None),
)
VARIANTS = (
    ("current_baseline", {}),
    ("adx_min_20", {"min_trend_adx": 20.0}),
    ("rising_ema50_3bars", {"trend_slope_bars": 3}),
    ("atr_min_0_30_pct", {"min_atr_pct": 0.30}),
)


def period_metrics(sim: dict, start: int | None, end: int | None) -> dict:
    trades = [t for t in sim["trades"]
              if (start is None or t["exit_time"] >= start)
              and (end is None or t["exit_time"] < end)]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_wins = sum(t["pnl"] for t in wins)
    gross_losses = -sum(t["pnl"] for t in losses)
    equity = [(ts, value) for ts, value, _ in sim["equity"]
              if (start is None or ts >= start) and (end is None or ts < end)]
    before = [(ts, value) for ts, value, _ in sim["equity"]
              if start is not None and ts < start]
    baseline = before[-1][1] if before else 10_000.0
    path = np.asarray([baseline, *[value for _, value in equity]], dtype=float)
    peaks = np.maximum.accumulate(path)
    drawdown = float(np.max((peaks - path) / np.maximum(peaks, 1e-12) * 100.0)) if len(path) else 0.0
    pnl = sum(t["pnl"] for t in trades)
    return {
        "closed_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": round(100 * len(wins) / len(trades), 2) if trades else 0.0,
        "net_pnl_usdt": round(pnl, 2),
        "profit_factor": round(gross_wins / gross_losses, 3) if gross_losses else None,
        "segment_max_drawdown_pct": round(drawdown, 3),
        "ending_equity_usdt": round(equity[-1][1], 2) if equity else round(baseline, 2),
    }


def run_variant(name: str, params: dict) -> dict:
    frames = {symbol: compute_signals(symbol, interval="1h", **params) for symbol in SYMBOLS}
    funnel = {
        symbol: {
            "eligible_buy_signals": int(frame.signal.eq("BUY").sum()),
            "candidate_buy_signals": int(frame.candidate_buy.sum()),
            "buy_vetoed_by_regime": int(frame.regime_block.sum()),
            "buy_vetoed_by_trend": int(frame.trend_block.sum()),
            "buy_vetoed_by_volume": int(frame.volume_block.sum()),
            "buy_vetoed_by_confidence": int(frame.confidence_block.sum()),
        }
        for symbol, frame in frames.items()
    }
    costs = {}
    for cost_name, fee, slippage in (
        ("base", FEE_BPS, SLIPPAGE_BPS),
        ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS),
    ):
        diagnostic = simulate(frames, fee, slippage, enforce_risk_halts=False)
        risk_enforced = simulate(frames, fee, slippage, enforce_risk_halts=True)
        costs[cost_name] = {
            "no_permanent_halt": {
                "whole_period": period_metrics(diagnostic, None, None),
                "periods": {period: period_metrics(diagnostic, start, end)
                            for period, start, end in PERIODS},
            },
            "current_risk_halts": {
                "closed_trades": len(risk_enforced["trades"]),
                "net_pnl_usdt": round(sum(t["pnl"] for t in risk_enforced["trades"]), 2),
                "halt_reason": risk_enforced["halt_reason"],
                "halt_timestamp_ms": risk_enforced["halt_time"],
            },
        }
        print(f"completed {name} / {cost_name}", flush=True)
    return {"parameters": params, "funnel": funnel, "costs": costs}


def main():
    results = {
        "method": {
            "data": "Binance Spot monthly 1h BTC/ETH/SOL candles, Jan 2021-Oct 2026",
            "base_costs_per_side_bps": {"fee": FEE_BPS, "slippage": SLIPPAGE_BPS},
            "stress_costs_per_side_bps": {"fee": STRESS_FEE_BPS, "slippage": STRESS_SLIPPAGE_BPS},
            "sizing": "same 1% equity risk ceiling, 10% max notional, 3 open positions, ATR stop, 3% target, and correlation cap",
            "risk_halts": "same consecutive-loss, daily-loss, and rolling-drawdown controls; separate no-halt run is diagnostic only",
            "limitations": [
                "OHLCV cannot reproduce order-book fills, exact spread, latency, or intrabar stop sequencing.",
                "The dataset has been used in earlier research, so it is not a fresh out-of-sample holdout.",
                "Candidate filters reduce entries; they are not proof of a predictive edge.",
            ],
        },
        "variants": {name: run_variant(name, params) for name, params in VARIANTS},
    }
    path = OUT / "loss_reduction_results.json"
    OUT.mkdir(exist_ok=True)
    path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
