"""Paired replay of the EMA72 candidate with and without the regime entry gate.

Research-only. The only difference between the paired variants is that the
dynamic variant accepts fresh buys after three matching hourly classifications
and only in uptrends. Existing-position SELL signals and every simulator risk,
fill, fee, stop, and sizing setting are identical.
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
from backtest_ema72_donchian import SPLITS, SYMBOLS, build_signals
from backtest_strategy import (
    DATA, OUT, FEE_BPS, SLIPPAGE_BPS, STRESS_FEE_BPS,
    STRESS_SLIPPAGE_BPS, simulate, summarize,
)

HOUR_MS = 3_600_000


def dynamic_entry_gate(symbol: str) -> pd.DataFrame:
    raw = (pd.read_csv(DATA / f"{symbol}_1h.csv")
           .drop_duplicates("timestamp_ms")
           .sort_values("timestamp_ms")
           .reset_index(drop=True))
    close, high, low = raw.close, raw.high, raw.low
    adx = ADXIndicator(high, low, close, window=14).adx()
    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    ema200 = EMAIndicator(close, window=200).ema_indicator()
    slope = ema200 / ema200.shift(24)
    atr_pct = atr / close * 100.0

    raw_regime = np.full(len(raw), "transition", dtype=object)
    valid = np.isfinite(adx) & np.isfinite(atr_pct) & np.isfinite(ema200) & np.isfinite(slope)
    raw_regime[valid & (atr_pct > 3.0)] = "high_volatility"
    up = valid & (atr_pct <= 3.0) & (adx >= 25.0) & (close > ema200) & (ema200 > ema200.shift(24))
    down = valid & (atr_pct <= 3.0) & (adx >= 25.0) & (close < ema200) & (ema200 < ema200.shift(24))
    side = valid & (atr_pct <= 3.0) & (adx <= 18.0)
    raw_regime[up] = "uptrend"
    raw_regime[down] = "downtrend"
    raw_regime[side] = "range"
    raw_regime_series = pd.Series(raw_regime)
    stable_uptrend = (
        raw_regime_series.eq("uptrend")
        & raw_regime_series.shift(1).eq("uptrend")
        & raw_regime_series.shift(2).eq("uptrend")
    )
    timestamps = raw.timestamp_ms
    contiguous = timestamps.diff().eq(HOUR_MS).rolling(2500, min_periods=2500).sum().eq(2500)
    return pd.DataFrame({"timestamp_ms": timestamps, "stable_uptrend": stable_uptrend & contiguous})


def main() -> dict:
    unfiltered = {symbol: build_signals(symbol) for symbol in SYMBOLS}
    dynamic = {}
    for symbol in SYMBOLS:
        frame = unfiltered[symbol].copy()
        gate = dynamic_entry_gate(symbol).stable_uptrend.to_numpy(dtype=bool)
        frame.loc[frame.signal.eq("BUY") & ~gate, "signal"] = "HOLD"
        dynamic[symbol] = frame

    result = {
        "method": "Paired EMA72/Donchian historical replay; only dynamic entry variant applies the production three-closed-bar uptrend gate; existing-position exits unchanged; same next-open fills, fees, candidate stop/target, risk sizing and portfolio constraints.",
        "symbols": list(SYMBOLS),
        "warmup": "2,500 contiguous hourly bars; regime classification uses causal ADX14, ATR14, EMA200 slope over 24 bars; three identical completed-bar classes required.",
        "costs_per_side_bps": {"base": {"fee": FEE_BPS, "slippage": SLIPPAGE_BPS}, "stress": {"fee": STRESS_FEE_BPS, "slippage": STRESS_SLIPPAGE_BPS}},
        "risk_halts": "Existing simulator halts: 3% daily, 5% peak drawdown, 3 consecutive losses; separate no-halt strategy isolation is also reported.",
        "caveats": "Fixed BTC/ETH/SOL survivor universe; archive previously used in project research, not an untouched holdout. OHLCV cannot reconstruct order-book fills. Results do not prove future edge.",
        "splits": {},
    }
    for split_name, (start, end) in SPLITS.items():
        result["splits"][split_name] = {}
        for cost_name, fee, slip in (("base", FEE_BPS, SLIPPAGE_BPS), ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS)):
            result["splits"][split_name][cost_name] = {}
            for halt_name, enforce_halts in (("no_halts", False), ("existing_halts", True)):
                result["splits"][split_name][cost_name][halt_name] = {}
                for variant_name, frames in (("unfiltered_candidate", unfiltered), ("dynamic_router", dynamic)):
                    sim = simulate(
                        frames, fee, slip, enforce_risk_halts=enforce_halts,
                        start_timestamp_ms=start, end_timestamp_ms=end,
                        initial_stop_model="swing_low_floor_4pct",
                        take_profit_atr_multiple=100.0,
                        enable_breakeven_trailing=False,
                    )
                    stats = summarize(variant_name, sim)
                    stats["halt_reason"] = sim["halt_reason"]
                    stats["halt_timestamp_ms"] = sim["halt_time"]
                    result["splits"][split_name][cost_name][halt_name][variant_name] = stats
    OUT.mkdir(parents=True, exist_ok=True)
    output = OUT / "dynamic_regime_router_results.json"
    output.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Wrote {output}")
    for split_name, costs in result["splits"].items():
        for cost_name, modes in costs.items():
            plain = modes["no_halts"]
            guarded = modes["existing_halts"]
            print(
                f"{split_name}/{cost_name}: "
                f"no-halt trades {plain['unfiltered_candidate']['closed_trades']}→{plain['dynamic_router']['closed_trades']}, "
                f"PF {plain['unfiltered_candidate']['profit_factor']}→{plain['dynamic_router']['profit_factor']}, "
                f"P&L ${plain['unfiltered_candidate']['net_pnl_usdt']}→${plain['dynamic_router']['net_pnl_usdt']}; "
                f"existing-halt trades {guarded['unfiltered_candidate']['closed_trades']}→{guarded['dynamic_router']['closed_trades']}"
            )
    return result


if __name__ == "__main__":
    main()
