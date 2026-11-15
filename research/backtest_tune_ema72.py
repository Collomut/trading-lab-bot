"""Pre-registered EMA72/Donchian tuning sweep (2026-11-15).

Reuses the project's hourly portfolio simulator and risk-halt implementation.
Every variant below was specified before running. The comparison is across the
three chronological splits (train / validation / review) at both base and stress
costs, with and without the deployed 3-loss halt. The goal is robustness across
splits and costs, not in-sample maximization on the reused review slice.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
from ta.trend import ADXIndicator, EMAIndicator
from ta.volatility import AverageTrueRange

from backtest_ema72_donchian import SPLITS, SYMBOLS
from backtest_strategy import (
    DATA, OUT, FEE_BPS, SLIPPAGE_BPS, STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS,
    simulate, summarize,
)

HOUR_MS = 3_600_000
MONTHS = {
    "train_2021_02_to_2023": 35.0,
    "validation_2024_to_2025": 24.0,
    "review_2026_01_to_10": 10.0,
}


def build_variant_signals(symbol: str, *, entry_ema: int = 480, exit_ema: int = 72,
                          channel: int = 168, use_volume_filter: bool = False,
                          volume_ratio: float = 1.2, use_adx_filter: bool = False,
                          adx_min: float = 20.0, max_atr_pct: float | None = None) -> pd.DataFrame:
    raw = (pd.read_csv(DATA / f"{symbol}_1h.csv")
           .drop_duplicates("timestamp_ms")
           .sort_values("timestamp_ms")
           .reset_index(drop=True))
    close, high, low, volume = raw.close, raw.high, raw.low, raw.volume
    macro_ema = EMAIndicator(close=close, window=entry_ema).ema_indicator()
    exit_ema = EMAIndicator(close=close, window=exit_ema).ema_indicator()
    entry_high = high.shift(1).rolling(channel, min_periods=channel).max()
    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    contiguous = raw.timestamp_ms.diff().eq(HOUR_MS).rolling(2500, min_periods=2500).sum().eq(2500)
    ready = contiguous & macro_ema.notna() & exit_ema.notna() & atr.notna()

    buy = ready & close.gt(entry_high) & close.gt(macro_ema)
    if use_volume_filter:
        vol_mean = volume.rolling(20, min_periods=20).mean()
        buy &= volume.gt(vol_mean * volume_ratio)
    if use_adx_filter:
        adx = ADXIndicator(high, low, close, window=14).adx()
        ready &= adx.notna()
        buy &= adx.ge(adx_min)
    if max_atr_pct is not None:
        atr_pct = atr / close * 100.0
        buy &= atr_pct.le(max_atr_pct)

    sell = ready & close.lt(exit_ema)

    result = raw[["timestamp_ms", "open", "high", "low", "close", "volume"]].copy()
    result["atr"] = atr
    result["swing_low_reference"] = low.shift(1).rolling(5, min_periods=5).min()
    result["sequence_ok"] = ready
    result["signal"] = np.where(sell, "SELL", np.where(buy, "BUY", "HOLD"))
    return result


VARIANTS = {
    "v0_baseline": dict(signal_kwargs={}, sim_kwargs=dict(
        initial_stop_model="swing_low_floor_4pct", enable_breakeven_trailing=False,
        take_profit_atr_multiple=100.0)),
    "v1_atr_capped_stop": dict(signal_kwargs={}, sim_kwargs=dict(
        initial_stop_model="swing_low_atr_capped", enable_breakeven_trailing=False,
        take_profit_atr_multiple=100.0)),
    "v2_volume_filter": dict(signal_kwargs={"use_volume_filter": True}, sim_kwargs=dict(
        initial_stop_model="swing_low_floor_4pct", enable_breakeven_trailing=False,
        take_profit_atr_multiple=100.0)),
    "v3_adx_filter": dict(signal_kwargs={"use_adx_filter": True, "adx_min": 20.0}, sim_kwargs=dict(
        initial_stop_model="swing_low_floor_4pct", enable_breakeven_trailing=False,
        take_profit_atr_multiple=100.0)),
    "v4_atr_band": dict(signal_kwargs={"max_atr_pct": 2.5}, sim_kwargs=dict(
        initial_stop_model="swing_low_floor_4pct", enable_breakeven_trailing=False,
        take_profit_atr_multiple=100.0)),
    "v5_quality_combo": dict(signal_kwargs={"use_volume_filter": True, "use_adx_filter": True,
                                             "adx_min": 20.0, "max_atr_pct": 2.5}, sim_kwargs=dict(
        initial_stop_model="swing_low_floor_4pct", enable_breakeven_trailing=False,
        take_profit_atr_multiple=100.0)),
    "v6_ema50_exit": dict(signal_kwargs={"exit_ema": 50}, sim_kwargs=dict(
        initial_stop_model="swing_low_floor_4pct", enable_breakeven_trailing=False,
        take_profit_atr_multiple=100.0)),
    "v7_trailing": dict(signal_kwargs={}, sim_kwargs=dict(
        initial_stop_model="swing_low_floor_4pct", enable_breakeven_trailing=True,
        breakeven_trigger_pct=0.015, take_profit_atr_multiple=100.0)),
    "v8_quality_fast": dict(signal_kwargs={"use_volume_filter": True, "use_adx_filter": True,
                                            "adx_min": 20.0, "max_atr_pct": 2.5, "exit_ema": 50}, sim_kwargs=dict(
        initial_stop_model="swing_low_atr_capped", enable_breakeven_trailing=False,
        take_profit_atr_multiple=100.0)),
}


def _key_stats(stats: dict) -> dict:
    return {
        "trades": stats["closed_trades"],
        "win_rate_pct": stats["win_rate_pct"],
        "profit_factor": stats["profit_factor"],
        "net_pnl_usdt": stats["net_pnl_usdt"],
        "max_drawdown_pct": stats["max_drawdown_pct"],
        "trades_per_month": stats.get("trades_per_month"),
        "halted": stats.get("halted", False),
        "halt_reason": stats.get("halt_reason", ""),
    }


def main() -> dict:
    result: dict = {"method": "pre-registered EMA72/Donchian tuning sweep; "
                              "same simulator, fills, fees, and splits as the accepted replay",
                    "symbols": list(SYMBOLS), "splits": list(SPLITS), "variants": {}}
    for name, spec in VARIANTS.items():
        frames = {s: build_variant_signals(s, **spec["signal_kwargs"]) for s in SYMBOLS}
        result["variants"][name] = {}
        for split, (start, end) in SPLITS.items():
            result["variants"][name][split] = {}
            for cost_name, fee, slip in (("base", FEE_BPS, SLIPPAGE_BPS),
                                         ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS)):
                sim = simulate(frames, fee, slip, enforce_risk_halts=False,
                               start_timestamp_ms=start, end_timestamp_ms=end,
                               **spec["sim_kwargs"])
                stats = summarize(name, sim)
                stats["trades_per_month"] = round(stats["closed_trades"] / MONTHS[split], 2)
                result["variants"][name][split][cost_name] = _key_stats(stats)
            # Existing-halt behavior at base cost (to show the halt problem).
            sim = simulate(frames, FEE_BPS, SLIPPAGE_BPS, enforce_risk_halts=True,
                           start_timestamp_ms=start, end_timestamp_ms=end,
                           **spec["sim_kwargs"])
            stats = summarize(name, sim)
            stats["trades_per_month"] = round(stats["closed_trades"] / MONTHS[split], 2)
            result["variants"][name][split]["existing_halts_base"] = _key_stats(stats)
        print(f"completed {name}", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    out_path = OUT / "ema72_tuning_2026_11_15.json"
    out_path.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Wrote {out_path}")
    return result


def print_table(result: dict):
    header = f"{'variant':<18} {'split':<26} {'base PF':>8} {'base DD%':>8} {'str PF':>8} {'trades':>7}"
    print(header)
    print("-" * len(header))
    for name, splits in result["variants"].items():
        for split in ("train_2021_02_to_2023", "validation_2024_to_2025", "review_2026_01_to_10"):
            base = splits[split]["base"]
            stress = splits[split]["stress"]
            print(f"{name:<18} {split:<26} {str(base['profit_factor']):>8} "
                  f"{str(base['max_drawdown_pct']):>8} {str(stress['profit_factor']):>8} "
                  f"{str(base['trades']):>7}")


if __name__ == "__main__":
    print_table(main())
