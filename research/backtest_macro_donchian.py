"""Independent replay of handover 4's macro-filtered Donchian candidate."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange

from backtest_strategy import (
    DATA, OUT, FEE_BPS, SLIPPAGE_BPS, STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS,
    compute_signals, simulate, summarize,
)

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
HOUR_MS = 3_600_000
SPLITS = {
    "train_2021_02_to_2023": (int(pd.Timestamp("2021-02-01", tz="UTC").timestamp() * 1000), int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)),
    "validation_2024_to_2025": (int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000), int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000)),
    "holdout_2026_01_to_10": (int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000), int(pd.Timestamp("2026-11-01", tz="UTC").timestamp() * 1000)),
}


def build_signals(symbol: str) -> pd.DataFrame:
    raw = (pd.read_csv(DATA / f"{symbol}_1h.csv")
           .drop_duplicates("timestamp_ms").sort_values("timestamp_ms").reset_index(drop=True))
    close, high, low = raw.close, raw.high, raw.low
    ema480 = EMAIndicator(close, window=480).ema_indicator()
    entry_channel = high.shift(1).rolling(168, min_periods=168).max()
    exit_channel = low.shift(1).rolling(72, min_periods=72).min()
    atr14 = AverageTrueRange(high, low, close, window=14).average_true_range()
    sequence_ok = raw.timestamp_ms.diff().eq(HOUR_MS).rolling(149, min_periods=149).sum().eq(149)
    buy = close.gt(entry_channel) & close.gt(ema480) & sequence_ok
    sell = close.lt(exit_channel) & sequence_ok
    result = raw[["timestamp_ms", "open", "high", "low", "close", "volume"]].copy()
    result["ema480"] = ema480
    result["entry_channel"] = entry_channel
    result["exit_channel"] = exit_channel
    result["atr"] = atr14
    result["sequence_ok"] = sequence_ok
    result["swing_low_reference"] = low.shift(1).rolling(5, min_periods=5).min()
    result["signal"] = np.where(sell, "SELL", np.where(buy, "BUY", "HOLD"))
    return result


def run() -> dict:
    frames = {symbol: build_signals(symbol) for symbol in SYMBOLS}
    current_frames = {
        symbol: compute_signals(symbol, min_trend_adx=20.0, max_atr_pct=3.0)
        for symbol in SYMBOLS
    }
    all_signal_counts = {
        symbol: {
            "buy_bars": int(frame.signal.eq("BUY").sum()),
            "sell_bars": int(frame.signal.eq("SELL").sum()),
            "valid_contiguous_bars": int(frame.sequence_ok.sum()),
        }
        for symbol, frame in frames.items()
    }
    out = {
        "method": {
            "candidate": "macro_donchian_7d_20d_v1",
            "baseline": "current CombinedStrategy 2-of-3 with ADX >= 20",
            "spec_source": "AI_AGENT_STRATEGY_HANDOVER4.md",
            "symbols": list(SYMBOLS),
            "timeframe": "1h",
            "signals": "close > prior 168-hour high and EMA480 for entry; close < prior 72-hour low for exit",
            "execution": "signal after completed close; fill at next open with adverse slippage",
            "initial_stop": "max(prior 5-bar low, entry * 0.96); positions with invalid/non-below-entry stop rejected",
            "take_profit_approximation": "100 * ATR14 above entry, matching the handover's take_profit_atr_multiple=100; this is finite, not truly uncapped",
            "break_even_and_trailing": "disabled for this candidate, since its exit specification is channel-based",
            "risk_sizing": "shared simulator 1% equity risk, 10% notional cap, max 3 positions and 0.85 pairwise correlation cap",
            "period_accounting": "each chronological split starts from an independent $10,000 account; do not add split P&Ls and call it one continuous account return",
            "costs_per_side_bps": {
                "base": {"fee": FEE_BPS, "slippage": SLIPPAGE_BPS},
                "stress": {"fee": STRESS_FEE_BPS, "slippage": STRESS_SLIPPAGE_BPS},
            },
            "risk_halts": "strategy metrics shown without halts; separate configured-halt simulations use 3% daily loss, 5% rolling drawdown, and compare the current 3-loss limit against the proposed 10-loss limit",
            "sample_caveat": "The 2026 period has been examined in prior project research, so it is not a pristine holdout. This exact candidate was specified before this independent replay.",
            "data_caveats": [
                "Fixed BTC/ETH/SOL universe is survivorship-biased.",
                "Historical OHLCV does not model actual spread, order-book depth, queue position, or exact fills.",
                "Hourly simulation cannot reproduce operational exchange stop behavior or all intrabar paths.",
                "The reported win/loss ratio is based on average net winning trade divided by average net losing trade magnitude.",
            ],
            "signal_counts": all_signal_counts,
            "splits": {},
        },
        "results": {},
    }
    scenarios = (("base", FEE_BPS, SLIPPAGE_BPS), ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS))
    for split, (start, end) in SPLITS.items():
        months = (end - start) / (30.4375 * 24 * HOUR_MS)
        out["method"]["splits"][split] = {
            "start_utc": pd.to_datetime(start, unit="ms", utc=True).isoformat(),
            "end_utc_exclusive": pd.to_datetime(end, unit="ms", utc=True).isoformat(),
            "duration_months": round(months, 2),
        }
        out["results"][split] = {}
        for scenario, fee, slip in scenarios:
            for risk_mode, enforce_halts, loss_limit in (
                ("no_halts", False, 3),
                ("configured_halts_loss3", True, 3),
                ("configured_halts_loss10", True, 10),
            ):
                sim = simulate(
                    frames, fee, slip, enforce_risk_halts=enforce_halts,
                    start_timestamp_ms=start, end_timestamp_ms=end,
                    initial_stop_model="swing_low_floor_4pct",
                    take_profit_atr_multiple=100.0,
                    enable_breakeven_trailing=False,
                    max_consecutive_losses=loss_limit,
                )
                stats = summarize(f"{split}_{scenario}_{risk_mode}", sim)
                stats["trades_per_month"] = round(stats["closed_trades"] / months, 2) if months else 0
                trades = sim["trades"]
                win_returns = [t["pnl"] for t in trades if t["pnl"] > 0]
                loss_returns = [-t["pnl"] for t in trades if t["pnl"] < 0]
                stats["average_win_loss_ratio"] = round((np.mean(win_returns) / np.mean(loss_returns)), 3) if win_returns and loss_returns else None
                stats["exit_reasons"] = {str(k): int(v) for k, v in pd.Series([t["reason"] for t in trades]).value_counts().items()}
                stats["halted"] = sim["halted"]
                stats["halt_reason"] = sim["halt_reason"]
                out["results"][split].setdefault(scenario, {})[risk_mode] = stats
                print(f"{split}/{scenario}/{risk_mode}: {stats}", flush=True)

            comparison = out["results"][split][scenario].setdefault("strategy_comparison", {})
            comparison["macro_donchian"] = {
                "no_halts": dict(out["results"][split][scenario]["no_halts"]),
                "current_halts_loss3": dict(out["results"][split][scenario]["configured_halts_loss3"]),
            }
            for risk_mode, enforce_halts in (("no_halts", False), ("current_halts_loss3", True)):
                sim = simulate(
                    current_frames, fee, slip, enforce_risk_halts=enforce_halts,
                    start_timestamp_ms=start, end_timestamp_ms=end,
                    max_consecutive_losses=3,
                )
                stats = summarize(f"current_adx20_{split}_{scenario}_{risk_mode}", sim)
                stats["trades_per_month"] = round(stats["closed_trades"] / months, 2) if months else 0
                stats["halted"] = sim["halted"]
                stats["halt_reason"] = sim["halt_reason"]
                comparison.setdefault("current_adx20", {})[risk_mode] = stats

    train = out["results"]["train_2021_02_to_2023"]
    validation = out["results"]["validation_2024_to_2025"]
    holdout = out["results"]["holdout_2026_01_to_10"]
    gates = {
        "train_base_pf_ge_1_0": (train["base"]["no_halts"]["profit_factor"] or 0) >= 1.0,
        "train_stress_pf_ge_1_0": (train["stress"]["no_halts"]["profit_factor"] or 0) >= 1.0,
        "validation_base_pf_ge_1_35": (validation["base"]["no_halts"]["profit_factor"] or 0) >= 1.35,
        "validation_stress_pf_ge_1_10": (validation["stress"]["no_halts"]["profit_factor"] or 0) >= 1.10,
        "validation_max_dd_le_12_pct": max(validation["base"]["no_halts"]["max_drawdown_pct"], validation["stress"]["no_halts"]["max_drawdown_pct"]) <= 12.0,
        "validation_frequency_6_to_18_month": 6 <= validation["base"]["no_halts"]["trades_per_month"] <= 18,
        "holdout_positive_base_pnl": holdout["base"]["no_halts"]["net_pnl_usdt"] > 0,
        "holdout_positive_stress_pnl": holdout["stress"]["no_halts"]["net_pnl_usdt"] > 0,
        "holdout_base_pf_ge_1_35": (holdout["base"]["no_halts"]["profit_factor"] or 0) >= 1.35,
        "holdout_stress_pf_ge_1_10": (holdout["stress"]["no_halts"]["profit_factor"] or 0) >= 1.10,
        "holdout_max_dd_le_12_pct": max(holdout["base"]["no_halts"]["max_drawdown_pct"], holdout["stress"]["no_halts"]["max_drawdown_pct"]) <= 12.0,
        "holdout_frequency_6_to_18_month": 6 <= holdout["base"]["no_halts"]["trades_per_month"] <= 18,
    }
    out["acceptance_gates"] = gates
    out["acceptance_gate_count"] = {"passed": sum(gates.values()), "total": len(gates)}
    out["candidate_passes_all_gates"] = all(gates.values())
    path = OUT / "macro_donchian_results.json"
    path.write_text(json.dumps(out, indent=2, allow_nan=False, default=str), encoding="utf-8")
    print(f"Acceptance gates passed: {sum(gates.values())}/{len(gates)}; wrote {path}")
    return out


if __name__ == "__main__":
    run()
