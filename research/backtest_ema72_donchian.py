"""Independent replay of the 7d Donchian / EMA480 entry with EMA72 exit.

Research only. This script does not import or modify production strategy code.
It reuses the project's hourly portfolio simulator and risk-halt implementation.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange

from backtest_strategy import (
    DATA, OUT, FEE_BPS, SLIPPAGE_BPS, STRESS_FEE_BPS,
    STRESS_SLIPPAGE_BPS, compute_signals, simulate, summarize,
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


def build_signals(symbol: str) -> pd.DataFrame:
    path = DATA / f"{symbol}_1h.csv"
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

    close, high, low = raw.close, raw.high, raw.low
    macro_ema = EMAIndicator(close=close, window=480).ema_indicator()
    exit_ema = EMAIndicator(close=close, window=72).ema_indicator()
    entry_high = high.shift(1).rolling(168, min_periods=168).max()
    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    contiguous = raw.timestamp_ms.diff().eq(HOUR_MS).rolling(2500, min_periods=2500).sum().eq(2500)
    ready = contiguous & macro_ema.notna() & exit_ema.notna() & atr.notna()
    buy = ready & close.gt(entry_high) & close.gt(macro_ema)
    sell = ready & close.lt(exit_ema)

    result = raw[["timestamp_ms", "open", "high", "low", "close", "volume"]].copy()
    result["atr"] = atr
    result["ema480"] = macro_ema
    result["ema72"] = exit_ema
    result["entry_channel"] = entry_high
    result["swing_low_reference"] = low.shift(1).rolling(5, min_periods=5).min()
    result["sequence_ok"] = ready
    # SELL is an exit condition; the simulator only applies it when a position exists.
    result["signal"] = np.where(sell, "SELL", np.where(buy, "BUY", "HOLD"))
    return result


def main() -> dict:
    frames = {symbol: build_signals(symbol) for symbol in SYMBOLS}
    baseline = {
        symbol: compute_signals(symbol, min_trend_adx=20.0, max_atr_pct=3.0)
        for symbol in SYMBOLS
    }
    result: dict = {
        "method": {
            "candidate": "D168_M480_E72",
            "symbols": list(SYMBOLS),
            "timeframe": "1h",
            "entry": "completed close > prior 168-bar high and > EMA480",
            "signal_exit": "completed close < EMA72; filled at next bar open",
            "warmup": "2,500 contiguous completed hourly bars; runtime requests 2,501 raw rows because the latest hour is incomplete",
            "initial_stop": "max(prior 5-bar low, entry fill * 0.96); invalid stops are rejected",
            "target": "100 * ATR14 above entry, finite approximation of uncapped target",
            "break_even_trailing": "disabled to isolate the stated strategy exit",
            "portfolio": "shared simulator: 1% equity risk, 10% notional cap, max 3 positions, 0.85 pairwise correlation cap",
            "fill_model": "signal after completed close, entry/exit at next hourly open with adverse slippage; stop gaps and intrabar stop-first handling in shared simulator",
            "costs_per_side_bps": {
                "base": {"fee": FEE_BPS, "slippage": SLIPPAGE_BPS},
                "stress": {"fee": STRESS_FEE_BPS, "slippage": STRESS_SLIPPAGE_BPS},
            },
            "split_accounting": "independent $10,000 account for each split; split P&Ls are not added as one account return",
            "halted_replay": "also run with simulator's 3% daily, 5% peak drawdown and consecutive-loss halt; compare loss caps 3 and 10",
            "data_warning": "2026 has been used in prior project research and is not a pristine holdout; fixed BTC/ETH/SOL universe and hourly OHLCV have execution limitations",
            "results": {},
        },
        "candidate_vs_current": {},
    }
    cost_scenarios = (
        ("base", FEE_BPS, SLIPPAGE_BPS),
        ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS),
    )
    for split, (start, end) in SPLITS.items():
        months = (end - start) / (30.4375 * 24 * HOUR_MS)
        result["method"]["results"][split] = {}
        result["candidate_vs_current"][split] = {}
        for cost_name, fee, slip in cost_scenarios:
            result["method"]["results"][split][cost_name] = {}
            for halt_name, enforce, loss_limit in (
                ("strategy_only_no_halts", False, 3),
                ("current_halts_loss_cap_3", True, 3),
                ("sensitivity_halts_loss_cap_10", True, 10),
            ):
                sim = simulate(
                    frames, fee, slip, enforce_risk_halts=enforce,
                    start_timestamp_ms=start, end_timestamp_ms=end,
                    initial_stop_model="swing_low_floor_4pct",
                    take_profit_atr_multiple=100.0,
                    enable_breakeven_trailing=False,
                    max_consecutive_losses=loss_limit,
                )
                stats = summarize(f"{split}_{cost_name}_{halt_name}", sim)
                stats["trades_per_month"] = round(stats["closed_trades"] / months, 2)
                stats["halted"] = sim["halted"]
                stats["halt_reason"] = sim["halt_reason"]
                stats["halt_time_ms"] = sim["halt_time"]
                result["method"]["results"][split][cost_name][halt_name] = stats

            current_sim = simulate(
                baseline, fee, slip, enforce_risk_halts=False,
                start_timestamp_ms=start, end_timestamp_ms=end,
            )
            current_stats = summarize(f"current_adx20_{split}_{cost_name}", current_sim)
            current_stats["trades_per_month"] = round(current_stats["closed_trades"] / months, 2)
            result["candidate_vs_current"][split][cost_name] = {
                "candidate": result["method"]["results"][split][cost_name]["strategy_only_no_halts"],
                "current_adx20": current_stats,
            }

    result["acceptance_assessment"] = {
        "train_stress_pf_at_least_1": result["method"]["results"]["train_2021_02_to_2023"]["stress"]["strategy_only_no_halts"]["profit_factor"] >= 1.0,
        "validation_stress_pf_at_least_1": result["method"]["results"]["validation_2024_to_2025"]["stress"]["strategy_only_no_halts"]["profit_factor"] >= 1.0,
        "review_2026_stress_pf_at_least_1": result["method"]["results"]["review_2026_01_to_10"]["stress"]["strategy_only_no_halts"]["profit_factor"] >= 1.0,
        "validation_frequency_at_least_6_per_month": result["method"]["results"]["validation_2024_to_2025"]["base"]["strategy_only_no_halts"]["trades_per_month"] >= 6,
        "review_2026_frequency_at_least_6_per_month": result["method"]["results"]["review_2026_01_to_10"]["base"]["strategy_only_no_halts"]["trades_per_month"] >= 6,
        "caution": "These minimum gates are a screening aid, not proof of edge. Reused history is not independent holdout evidence.",
    }
    result["acceptance_assessment"]["all_screening_gates_pass"] = all(
        value for key, value in result["acceptance_assessment"].items()
        if key != "caution"
    )
    OUT.mkdir(parents=True, exist_ok=True)
    output = OUT / "ema72_donchian_results.json"
    output.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Wrote {output}")
    print(json.dumps(result["acceptance_assessment"], indent=2))
    for split, costs in result["method"]["results"].items():
        for cost, modes in costs.items():
            base = modes["strategy_only_no_halts"]
            halted = modes["current_halts_loss_cap_3"]
            print(
                f"{split}/{cost}: no-halt trades={base['closed_trades']} "
                f"freq={base['trades_per_month']} PF={base['profit_factor']} "
                f"PnL={base['net_pnl_usdt']} DD={base['max_drawdown_pct']}%; "
                f"current-halt trades={halted['closed_trades']} "
                f"PnL={halted['net_pnl_usdt']} halt={halted['halt_reason']}"
            )
    return result


if __name__ == "__main__":
    main()
