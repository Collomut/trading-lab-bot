"""Predeclared frequency sensitivity screen for the active EMA72 breakout.

Research only: compares entry-channel lengths and the extra router gate while
holding the macro EMA, EMA72 exit, stop, sizing, fees, slippage, and portfolio
simulator constant. The 2026 review split was previously examined and is not a
clean holdout. Results do not authorize production routing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from ta.trend import ADXIndicator, EMAIndicator
from ta.volatility import AverageTrueRange

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
from backtest_ema72_donchian import HOUR_MS, SPLITS, SYMBOLS
from backtest_strategy import (
    DATA, OUT, FEE_BPS, SLIPPAGE_BPS, STRESS_FEE_BPS,
    STRESS_SLIPPAGE_BPS, simulate, summarize,
)

ENTRY_LOOKBACKS = (168, 120, 72)
GATE_PROFILES = ("production_3bar", "single_bar", "breakout_only")


def load_market(symbol: str) -> pd.DataFrame:
    path = DATA / f"{symbol}_1h.csv"
    raw = (pd.read_csv(path).drop_duplicates("timestamp_ms")
           .sort_values("timestamp_ms").reset_index(drop=True))
    required = {"timestamp_ms", "open", "high", "low", "close", "volume"}
    missing = required.difference(raw.columns)
    if missing:
        raise ValueError(f"{path} is missing columns: {sorted(missing)}")
    if raw.empty or raw[list(required)].isna().any().any():
        raise ValueError(f"{path} is empty or has null required data")
    return raw


def gate_masks(raw: pd.DataFrame) -> dict[str, np.ndarray]:
    close, high, low = raw.close, raw.high, raw.low
    adx = ADXIndicator(high, low, close, window=14).adx()
    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    ema200 = EMAIndicator(close, window=200).ema_indicator()
    slope = ema200 > ema200.shift(24)
    atr_pct = atr / close * 100.0
    valid = np.isfinite(adx) & np.isfinite(atr_pct) & np.isfinite(ema200) & np.isfinite(slope)
    uptrend = valid & (atr_pct <= 3.0) & (adx >= 25.0) & (close > ema200) & slope
    stable_three = (
        pd.Series(uptrend).rolling(3, min_periods=3).sum().eq(3).to_numpy()
    )
    contiguous = raw.timestamp_ms.diff().eq(HOUR_MS).rolling(2500, min_periods=2500).sum().eq(2500).to_numpy()
    production = stable_three & contiguous
    single = uptrend.to_numpy() & contiguous
    return {
        "production_3bar": production,
        "single_bar": single,
        "breakout_only": np.ones(len(raw), dtype=bool),
    }


def build_variant(raw: pd.DataFrame, entry_lookback: int, gate: np.ndarray) -> pd.DataFrame:
    close, high, low = raw.close, raw.high, raw.low
    macro_ema = EMAIndicator(close=close, window=480).ema_indicator()
    exit_ema = EMAIndicator(close=close, window=72).ema_indicator()
    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    entry_high = high.shift(1).rolling(entry_lookback, min_periods=entry_lookback).max()
    contiguous = raw.timestamp_ms.diff().eq(HOUR_MS).rolling(2500, min_periods=2500).sum().eq(2500)
    ready = contiguous & macro_ema.notna() & exit_ema.notna() & atr.notna()
    buy = ready & close.gt(entry_high) & close.gt(macro_ema) & gate
    sell = ready & close.lt(exit_ema)
    result = raw[["timestamp_ms", "open", "high", "low", "close", "volume"]].copy()
    result["atr"] = atr
    result["swing_low_reference"] = low.shift(1).rolling(5, min_periods=5).min()
    result["signal"] = np.where(sell, "SELL", np.where(buy, "BUY", "HOLD"))
    return result


def slice_for_split(frames: dict[str, pd.DataFrame], start: int, end: int) -> dict[str, pd.DataFrame]:
    # Keep sufficient pre-split history for the 2,500-hour readiness gate and
    # correlation check, but avoid replaying years that the split ignores.
    warmup_start = start - 2_700 * HOUR_MS
    return {
        symbol: frame.loc[
            (frame.timestamp_ms >= warmup_start) & (frame.timestamp_ms < end)
        ].reset_index(drop=True)
        for symbol, frame in frames.items()
    }


def main() -> dict:
    markets = {symbol: load_market(symbol) for symbol in SYMBOLS}
    masks = {symbol: gate_masks(raw) for symbol, raw in markets.items()}
    variants = {}
    raw_buy_signals = {}
    for lookback in ENTRY_LOOKBACKS:
        for gate_name in GATE_PROFILES:
            name = f"D{lookback}_M480_E72_{gate_name}"
            variants[name] = {
                symbol: build_variant(raw, lookback, masks[symbol][gate_name])
                for symbol, raw in markets.items()
            }
            raw_buy_signals[name] = {
                symbol: int(variants[name][symbol].signal.eq("BUY").sum())
                for symbol in SYMBOLS
            }

    result = {
        "method": {
            "source": "Local Binance Spot 1h archive, 2021-01 through 2026-10",
            "fixed_rules": "EMA480 macro trend, completed-candle signals, EMA72 close exit, prior-5-bar swing stop capped at 4%, 1% risk sizing, 10% notional cap, 3-position cap, 0.85 correlation cap",
            "varied_rules": {
                "entry_lookback_hours": list(ENTRY_LOOKBACKS),
                "production_3bar": "Current router: ADX >=25, ATR <=3%, close > rising EMA200, stable for 3 closed bars, 2,500 contiguous-hour warmup",
                "single_bar": "Same indicators and thresholds as production router, with no 3-bar persistence requirement",
                "breakout_only": "No extra regime gate; the existing EMA480 macro filter and breakout signal remain active",
            },
            "costs_per_side_bps": {
                "base": {"fee": FEE_BPS, "slippage": SLIPPAGE_BPS},
                "stress": {"fee": STRESS_FEE_BPS, "slippage": STRESS_SLIPPAGE_BPS},
            },
            "splits": {},
            "caveats": [
                "The 2026 review interval has already been examined in this project and is not a clean holdout.",
            "A 9-variant parameter screen creates selection bias; top results are exploratory only.",
                "Hourly OHLCV cannot model actual book depth, queue position, network delay, or exact stop fills.",
                "The fixed BTC/ETH/SOL universe is survivor-biased and excludes other valid markets.",
                "No-halt runs isolate strategy behavior; live frequency must be judged under configured halts.",
            ],
        },
        "signal_count_by_variant": raw_buy_signals,
    }
    for split_name, (start, end) in SPLITS.items():
        result["method"]["splits"][split_name] = {}
        months = (end - start) / (30.4375 * 24 * HOUR_MS)
        split_variants = {
            name: slice_for_split(frames, start, end)
            for name, frames in variants.items()
        }
        for cost_name, fee, slip in (
            ("base", FEE_BPS, SLIPPAGE_BPS),
            ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS),
        ):
            result["method"]["splits"][split_name][cost_name] = {}
            for variant_name, frames in variants.items():
                result["method"]["splits"][split_name][cost_name][variant_name] = {}
                # Frequency decisions must reflect current production halts;
                # omit no-halt runs that could overstate executable activity.
                for halt_name, enforce_halts in (("current_halts", True),):
                    sim = simulate(
                        split_variants[variant_name], fee, slip, enforce_risk_halts=enforce_halts,
                        start_timestamp_ms=start, end_timestamp_ms=end,
                        initial_stop_model="swing_low_floor_4pct",
                        take_profit_atr_multiple=100.0,
                        enable_breakeven_trailing=False,
                    )
                    stats = summarize(variant_name, sim)
                    stats["trades_per_month"] = round(stats["closed_trades"] / months, 3)
                    stats["halted"] = sim["halted"]
                    stats["halt_reason"] = sim["halt_reason"]
                    result["method"]["splits"][split_name][cost_name][variant_name][halt_name] = stats

    output = OUT / "frequency_variants_results.json"
    output.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Wrote {output}")
    for split_name, costs in result["method"]["splits"].items():
        print(f"[{split_name}]")
        for variant_name in variants:
            base = costs["base"][variant_name]
            stress = costs["stress"][variant_name]
            b = base["current_halts"]
            s = stress["current_halts"]
            print(
                f"{variant_name}: raw_BUY={sum(raw_buy_signals[variant_name].values())}, "
                f"base_halted={b['closed_trades']} ({b['trades_per_month']}/mo), "
                f"PnL={b['net_pnl_usdt']}, PF={b['profit_factor']}, DD={b['max_drawdown_pct']}%, "
                f"stress_halted={s['closed_trades']} ({s['trades_per_month']}/mo), "
                f"PnL={s['net_pnl_usdt']}, PF={s['profit_factor']}, DD={s['max_drawdown_pct']}%"
            )
    return result


if __name__ == "__main__":
    main()
