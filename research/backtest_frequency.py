"""Compare the unchanged entry rules at hourly and 15-minute intervals."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from backtest_strategy import (
    DATA, OUT, FEE_BPS, SLIPPAGE_BPS, STRESS_FEE_BPS,
    STRESS_SLIPPAGE_BPS, compute_signals, simulate, summarize,
    validate_against_actual_strategy,
)


def run(interval: str) -> dict:
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    frames = {symbol: compute_signals(symbol, interval=interval) for symbol in symbols}
    correlation_window = 100 if interval == "1h" else 400  # same ~100-hour window
    data_rows = {}
    for symbol, frame in frames.items():
        data_rows[symbol] = {
            "bars": int(len(frame)),
            "buy_consensus_candidates": int(frame.candidate_buy.sum()),
            "eligible_buys": int((frame.signal == "BUY").sum()),
            "sell_consensus": int((frame.signal == "SELL").sum()),
            "buy_vetoed_by_regime": int(frame.regime_block.sum()),
            "buy_vetoed_by_trend": int(frame.trend_block.sum()),
            "buy_vetoed_by_volume": int(frame.volume_block.sum()),
            "buy_vetoed_by_confidence": int(frame.confidence_block.sum()),
        }

    checks = {
        symbol: validate_against_actual_strategy(
            symbol, frame, sample_count=200, interval=interval,
        )
        for symbol, frame in frames.items()
    }
    cost_runs = {}
    for name, fee, slippage in (
        ("base", FEE_BPS, SLIPPAGE_BPS),
        ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS),
    ):
        diagnostic = simulate(
            frames, fee, slippage, enforce_risk_halts=False,
            correlation_lookback_bars=correlation_window,
        )
        guarded = simulate(
            frames, fee, slippage, enforce_risk_halts=True,
            correlation_lookback_bars=correlation_window,
        )
        cut_2024 = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)
        cut_2026 = int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000)
        cost_runs[name] = {
            "diagnostic_without_permanent_halts": {
                "whole_period": summarize("candidate", diagnostic),
                "2021_2023": summarize("candidate", diagnostic, None, cut_2024),
                "2024_2025": summarize("candidate", diagnostic, cut_2024, cut_2026),
                "2026_through_oct": summarize("candidate", diagnostic, cut_2026, None),
            },
            "with_current_risk_halts": summarize("candidate", guarded),
        }
    return {
        "interval": interval,
        "assumptions": {
            "same_strategy_periods": "EMA 9/21, MACD 12/26/9, RSI 14 with existing vote rules; 2-of-3; two-candle crossover window",
            "same_entry_gates": "price >= EMA50; ADX >= 18; ATR <= 3%; existing volume and 50% confidence checks",
            "risk": "same 1% risk per trade, 10% notional cap, 3 concurrent positions, 3% target, ATR stop, same-symbol lockout",
            "correlation_window": f"{correlation_window} candles (~100 hours)",
            "risk_halting": "current simulator's three-loss, daily-loss, and rolling-drawdown stops; separate no-halt results isolate the strategy only",
            "costs_per_side_bps": {
                "base_fee": FEE_BPS, "base_slippage": SLIPPAGE_BPS,
                "stress_fee": STRESS_FEE_BPS, "stress_slippage": STRESS_SLIPPAGE_BPS,
            },
            "data": f"Binance Spot monthly klines, {interval}, January 2021 through October 2026",
            "limitations": [
                "Historical OHLCV cannot model exact order-book fills, spread, latency or intra-bar stop ordering.",
                "The same indicator periods on 15m bars represent shorter market horizons than on 1h bars.",
                "Historical periods have been used in earlier project research and are not a pristine holdout.",
                "The 15m candidate must still pass a forward Testnet soak before any live consideration.",
            ],
        },
        "funnel": data_rows,
        "signal_parity_vs_production_strategy": checks,
        "cost_scenarios": cost_runs,
    }


def compute_multitimeframe_signals(symbol: str):
    """Use 1h direction/regime with 15m consensus timing and unchanged sizing."""
    lower = compute_signals(symbol, interval="15m")
    higher = compute_signals(symbol, interval="1h")
    lower = lower.copy()
    higher = higher.copy()
    # A candle's information becomes available only at its close. The 15m
    # signal may use the 1h candle only when that 1h candle has also closed.
    lower["available_at"] = lower.timestamp_ms + 15 * 60_000
    higher["available_at"] = higher.timestamp_ms + 60 * 60_000
    higher["higher_timeframe_pass"] = (
        higher.sequence_ok & higher.trend_ok & higher.regime_ok
    )
    merged = pd.merge_asof(
        lower.sort_values("available_at"),
        higher[["available_at", "higher_timeframe_pass"]].sort_values("available_at"),
        on="available_at", direction="backward", allow_exact_matches=True,
    )
    entry_ok = (
        merged.candidate_buy & merged.volume_ok
        & (merged.buy_confidence >= 0.5)
        & merged.higher_timeframe_pass.fillna(False)
    )
    signal = pd.Series("HOLD", index=merged.index, dtype=object)
    signal.loc[merged.signal.eq("SELL")] = "SELL"
    signal.loc[entry_ok & ~merged.signal.eq("SELL")] = "BUY"
    merged["signal"] = signal.to_numpy()
    merged["higher_timeframe_pass"] = merged.higher_timeframe_pass.fillna(False)
    return merged


def run_multitimeframe() -> dict:
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    frames = {symbol: compute_multitimeframe_signals(symbol) for symbol in symbols}
    funnel = {
        symbol: {
            "15m_vote_candidates": int(frame.candidate_buy.sum()),
            "entries_after_1h_trend_regime_and_15m_volume_confidence": int((frame.signal == "BUY").sum()),
            "eligible_sell_exits": int((frame.signal == "SELL").sum()),
        }
        for symbol, frame in frames.items()
    }
    parity = {
        symbol: validate_against_actual_strategy(
            symbol, compute_signals(symbol, interval="15m"), sample_count=200, interval="15m",
        )
        for symbol in symbols
    }
    output = {
        "interval": "15m entries; 1h completed-candle regime confirmation",
        "funnel": funnel,
        "signal_parity_for_15m_component": parity,
        "correlation_lookback_hours": 100,
        "cost_scenarios": {},
    }
    cut_2024 = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)
    cut_2026 = int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000)
    for name, fee, slippage in (
        ("base", FEE_BPS, SLIPPAGE_BPS),
        ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS),
    ):
        diagnostic = simulate(
            frames, fee, slippage, enforce_risk_halts=False,
            correlation_lookback_bars=400,
        )
        guarded = simulate(
            frames, fee, slippage, enforce_risk_halts=True,
            correlation_lookback_bars=400,
        )
        output["cost_scenarios"][name] = {
            "diagnostic_without_permanent_halts": {
                "whole_period": summarize("1h_regime_15m_entry", diagnostic),
                "2021_2023": summarize("candidate", diagnostic, None, cut_2024),
                "2024_2025": summarize("candidate", diagnostic, cut_2024, cut_2026),
                "2026_through_oct": summarize("candidate", diagnostic, cut_2026, None),
            },
            "with_current_risk_halts": summarize("candidate", guarded),
        }
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--multi-timeframe-only", action="store_true",
                        help="run just the 1h-regime / 15m-entry candidate and append it to existing results")
    parser.add_argument("--report-only", action="store_true",
                        help="print a compact summary from existing results without rerunning simulations")
    args = parser.parse_args()
    results_path = OUT / "frequency_comparison.json"
    if args.report_only:
        if not results_path.exists():
            raise SystemExit("No frequency_comparison.json exists; run a backtest first")
        results = json.loads(results_path.read_text(encoding="utf-8"))
    elif args.multi_timeframe_only:
        if not results_path.exists():
            raise SystemExit("Run the full comparison first to create frequency_comparison.json")
        results = json.loads(results_path.read_text(encoding="utf-8"))
        results["1h_regime_15m_entry"] = run_multitimeframe()
    else:
        results = {interval: run(interval) for interval in ("1h", "15m")}
        results["1h_regime_15m_entry"] = run_multitimeframe()
    OUT.mkdir(exist_ok=True)
    results_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"wrote {results_path}")
    for interval, result in results.items():
        base = result["cost_scenarios"]["base"]
        full = base["diagnostic_without_permanent_halts"]["whole_period"]
        risk = base["with_current_risk_halts"]
        if interval == "1h_regime_15m_entry":
            eligible_buys = sum(x["entries_after_1h_trend_regime_and_15m_volume_confidence"]
                                for x in result["funnel"].values())
        else:
            eligible_buys = sum(x["eligible_buys"] for x in result["funnel"].values())
        parity = result.get("signal_parity_vs_production_strategy",
                            result.get("signal_parity_for_15m_component", {}))
        print(json.dumps({"interval": interval, "eligible_buys": eligible_buys,
                          "diagnostic_trades": full["closed_trades"], "net_pnl": full["net_pnl_usdt"],
                          "profit_factor": full["profit_factor"], "max_drawdown": full["max_drawdown_pct"],
                          "risk_halted_trades": risk["closed_trades"], "halt_reason": risk["halt_reason"],
                          "signal_parity_mismatches": sum(len(item["mismatches"]) for item in parity.values())}, indent=2))


if __name__ == "__main__":
    main()
