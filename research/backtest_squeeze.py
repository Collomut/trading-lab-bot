"""Research-only Keltner squeeze breakout replay; never imported by the bot."""
from __future__ import annotations

import json
import numpy as np
import pandas as pd
from ta.trend import ADXIndicator, EMAIndicator
from ta.volatility import AverageTrueRange

from backtest_strategy import (
    DATA, OUT, FEE_BPS, SLIPPAGE_BPS, STRESS_FEE_BPS,
    STRESS_SLIPPAGE_BPS, simulate, summarize,
)

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
HOUR_MS = 3_600_000
WARMUP_START = int(pd.Timestamp("2021-02-01", tz="UTC").timestamp() * 1000)
SPLITS = {
    "train_2021_02_to_2023": (WARMUP_START, int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)),
    "validation_2024_to_2025": (int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000), int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000)),
    "holdout_2026_01_to_10": (int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000), int(pd.Timestamp("2026-11-01", tz="UTC").timestamp() * 1000)),
}


def build_signals(symbol: str) -> pd.DataFrame:
    """Build signals from completed candles only; entry is filled next bar."""
    raw = (pd.read_csv(DATA / f"{symbol}_1h.csv")
           .drop_duplicates("timestamp_ms").sort_values("timestamp_ms").reset_index(drop=True))
    close, high, low, volume = raw.close, raw.high, raw.low, raw.volume

    ema20 = EMAIndicator(close, window=20).ema_indicator()
    atr20 = AverageTrueRange(high, low, close, window=20).average_true_range()
    middle_bb = close.rolling(20, min_periods=20).mean()
    std_bb = close.rolling(20, min_periods=20).std(ddof=0)
    upper_bb, lower_bb = middle_bb + 2.0 * std_bb, middle_bb - 2.0 * std_bb
    upper_kc, lower_kc = ema20 + 1.5 * atr20, ema20 - 1.5 * atr20
    squeeze = (upper_bb <= upper_kc) & (lower_bb >= lower_kc)
    ema50 = EMAIndicator(close, window=50).ema_indicator()
    slope_base = ema50.shift(3)
    slope_pct = (ema50 - slope_base) / slope_base * 100.0
    volume_sma = volume.rolling(20, min_periods=20).mean()
    atr14 = AverageTrueRange(high, low, close, window=14).average_true_range()
    adx14 = ADXIndicator(high, low, close, window=14).adx()
    atr_pct = atr14 / close * 100.0

    # Match the service's 150-bar contiguous-history guard. Reject the signal
    # window around missing bars rather than silently bridging discontinuities.
    contiguous = raw.timestamp_ms.diff().eq(HOUR_MS)
    sequence_ok = contiguous.rolling(149, min_periods=149).sum().eq(149)
    volume_ok = volume_sma.gt(0) & volume.ge(1.2 * volume_sma)
    # The handover defines ADX < 20 as part of the compression state. It does
    # not require a separate ADX threshold at the breakout candle.
    squeeze_state = squeeze & adx14.lt(20)
    prior_compression = squeeze_state.shift(1).rolling(10, min_periods=10).max().fillna(0).gt(0)
    buy = (
        prior_compression
        & close.gt(upper_kc)
        & volume_ok
        & close.gt(ema50)
        & slope_pct.gt(0.05)
        & atr_pct.le(3.0)
        & sequence_ok
    )
    sell = close.lt(ema50) & sequence_ok
    signal = np.where(sell, "SELL", np.where(buy, "BUY", "HOLD"))

    result = raw[["timestamp_ms", "open", "high", "low", "close", "volume"]].copy()
    result["atr"] = atr14
    result["adx"] = adx14
    result["atr_pct"] = atr_pct
    result["ema50"] = ema50
    result["squeeze"] = squeeze_state
    result["prior_compression"] = prior_compression
    result["volume_ratio"] = volume / volume_sma
    result["trend_slope_pct"] = slope_pct
    result["sequence_ok"] = sequence_ok
    result["swing_low_reference"] = low.shift(1).rolling(5, min_periods=5).min()
    result["signal"] = signal
    return result


def run() -> dict:
    frames = {symbol: build_signals(symbol) for symbol in SYMBOLS}
    results = {
        "method": {
            "strategy": "Keltner squeeze breakout as specified in AI_AGENT_STRATEGY_HANDOVER3.md",
            "candidate_id": "keltner_squeeze_breakout_v1",
            "symbols": list(SYMBOLS),
            "timeframe": "1h",
            "signal_timing": "completed candle close; entry at next candle open",
            "base_cost_bps_per_side": {"fee": FEE_BPS, "slippage": SLIPPAGE_BPS},
            "stress_cost_bps_per_side": {"fee": STRESS_FEE_BPS, "slippage": STRESS_SLIPPAGE_BPS},
            "sizing": "shared simulator: 1% equity risk, 10% notional cap, max 3 positions, correlation guard",
            "risk_exits": "initial stop at farther of prior 5-bar low or entry minus 2 ATR, capped at 4%; trail arms after a completed close > 1.5 entry ATR profit, then follows highest close minus 2.5 current ATR; EMA50 close exit; gap exit at 3.5 entry ATR",
            "intrabar_assumption": "existing shared simulator: stop-first if stop touched; newly tightened trailing stop applies next bar",
            "risk_halts": "disabled for strategy-family comparison; never use this as deployed risk behavior",
            "holdout_warning": "2026 was already examined in prior project research. Candidate rules were provided before this run, but market-period selection is not pristine and this is not independent proof of edge.",
            "known_limitations": [
                "OHLCV cannot reconstruct actual historical spread, depth, queue position, or exact fills.",
                "The three-asset universe is fixed and survivorship-biased.",
                "Shared portfolio simulator approximates risk monitoring at hourly close.",
                "Keltner/ATR warmup and gap guard require contiguous candles; rejected windows are not imputed.",
            ],
            "splits": {},
            "per_symbol_signal_counts": {},
        },
        "results": {},
    }
    for symbol, frame in frames.items():
        results["method"]["per_symbol_signal_counts"][symbol] = {
            "buy_bars": int(frame.signal.eq("BUY").sum()),
            "sell_bars": int(frame.signal.eq("SELL").sum()),
            "valid_contiguous_bars": int(frame.sequence_ok.sum()),
        }
    scenarios = (("base", FEE_BPS, SLIPPAGE_BPS), ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS))
    for split_name, (start, end) in SPLITS.items():
        results["method"]["splits"][split_name] = {
            "start_utc": pd.to_datetime(start, unit="ms", utc=True).isoformat(),
            "end_utc_exclusive": pd.to_datetime(end, unit="ms", utc=True).isoformat(),
        }
        results["results"][split_name] = {}
        for scenario, fee, slip in scenarios:
            sim = simulate(
                frames, fee, slip, enforce_risk_halts=False,
                start_timestamp_ms=start, end_timestamp_ms=end,
                initial_stop_model="swing_low_atr_capped",
                trailing_atr_multiple=2.5,
                trailing_activation_atr_multiple=1.5,
                trailing_uses_close=True,
                gap_exit_atr_multiple=3.5,
            )
            stats = summarize(f"squeeze_{scenario}_{split_name}", sim)
            duration_months = (end - start) / (30.4375 * 24 * HOUR_MS)
            stats["trades_per_month"] = round(stats["closed_trades"] / duration_months, 2) if duration_months else 0
            stats["halted"] = sim["halted"]
            results["results"][split_name][scenario] = stats
            print(f"{split_name}/{scenario}: {stats}", flush=True)

    validation = results["results"]["validation_2024_to_2025"]
    holdout = results["results"]["holdout_2026_01_to_10"]
    gates = {
        "validation_base_pf_ge_1_35": (validation["base"]["profit_factor"] or 0) >= 1.35,
        "validation_stress_pf_ge_1_10": (validation["stress"]["profit_factor"] or 0) >= 1.10,
        "validation_max_dd_le_12_pct": max(validation["base"]["max_drawdown_pct"], validation["stress"]["max_drawdown_pct"]) <= 12.0,
        "validation_frequency_6_to_18_month": 6 <= validation["base"]["trades_per_month"] <= 18,
        "holdout_positive_base_pnl": holdout["base"]["net_pnl_usdt"] > 0,
        "holdout_stress_positive_pnl": holdout["stress"]["net_pnl_usdt"] > 0,
        "holdout_base_pf_ge_1_35": (holdout["base"]["profit_factor"] or 0) >= 1.35,
        "holdout_stress_pf_ge_1_10": (holdout["stress"]["profit_factor"] or 0) >= 1.10,
        "holdout_max_dd_le_12_pct": max(holdout["base"]["max_drawdown_pct"], holdout["stress"]["max_drawdown_pct"]) <= 12.0,
        "holdout_frequency_6_to_18_month": 6 <= holdout["base"]["trades_per_month"] <= 18,
    }
    results["acceptance_gates"] = gates
    results["candidate_passes_all_gates"] = all(gates.values())
    output = OUT / "squeeze_results.json"
    output.write_text(json.dumps(results, indent=2, allow_nan=False, default=str), encoding="utf-8")
    print(f"Candidate passes all gates: {all(gates.values())}; wrote {output}")
    return results


if __name__ == "__main__":
    run()
