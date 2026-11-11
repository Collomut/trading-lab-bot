"""Reproduce the AI handover's trend-pullback proposal as research only.

No production strategy, configuration, exchange, or order path is imported.
Signals use completed 1h bars and the shared project portfolio simulator.
No parameter optimization is performed.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, EMAIndicator
from ta.volatility import AverageTrueRange

from backtest_strategy import (
    DATA, OUT, FEE_BPS, SLIPPAGE_BPS, STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS,
    simulate, summarize,
)

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
HOUR_MS = 3_600_000
WARMUP_END = int(pd.Timestamp("2021-02-01", tz="UTC").timestamp() * 1000)
CUT_2024 = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)
CUT_2026 = int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000)
END_2026 = int(pd.Timestamp("2026-11-01", tz="UTC").timestamp() * 1000)


def build_signals(symbol: str) -> pd.DataFrame:
    """Implement handover v2 pseudocode with explicit one-bar-close timing."""
    raw = (pd.read_csv(DATA / f"{symbol}_1h.csv")
           .drop_duplicates("timestamp_ms").sort_values("timestamp_ms").reset_index(drop=True))
    close, high, low, volume = raw.close, raw.high, raw.low, raw.volume
    ema20 = EMAIndicator(close, window=20).ema_indicator()
    ema50 = EMAIndicator(close, window=50).ema_indicator()
    rsi2 = RSIIndicator(close, window=2).rsi()
    rsi14 = RSIIndicator(close, window=14).rsi()
    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    adx = ADXIndicator(high, low, close, window=14).adx()
    slope_base = ema50.shift(4)
    slope_pct = (ema50 - slope_base) / slope_base * 100.0

    # The production fetch rejects a rolling history containing any candle gap.
    sequence_ok = raw.timestamp_ms.diff().eq(HOUR_MS).rolling(149, min_periods=149).sum().eq(149)
    strong_up = (adx >= 22.0) & (close > ema50) & (slope_pct > 0.05)
    ranging = adx < 22.0
    downtrend = (adx >= 22.0) & (close < ema50)
    neutral = ~(strong_up | ranging | downtrend)
    sizing_multiplier = pd.Series(np.select([strong_up, neutral], [1.0, 0.5], default=0.0), index=raw.index)

    pullback = (low <= ema20 * 1.003) | (rsi2 < 15.0)
    confirmed_bounce = close > ema20
    buy = (sizing_multiplier > 0.0) & pullback & confirmed_bounce & sequence_ok
    sell = ((close < ema50) | (rsi14 > 80.0)) & sequence_ok

    # Module B returns BUY before evaluating either SELL condition; preserve that precedence.
    signal = np.full(len(raw), "HOLD", dtype=object)
    signal[sell.to_numpy()] = "SELL"
    signal[buy.to_numpy()] = "BUY"

    return pd.DataFrame({
        "timestamp_ms": raw.timestamp_ms, "open": raw.open, "high": high,
        "low": low, "close": close, "volume": volume, "atr": atr,
        "signal": signal, "candidate_buy": buy, "candidate_sell": sell,
        "strong_trend_up": strong_up & sequence_ok,
        "neutral_regime": neutral & sequence_ok,
        "sizing_multiplier": sizing_multiplier,
        "ema20": ema20, "ema50": ema50, "rsi2": rsi2, "rsi14": rsi14,
        "adx": adx, "atr_pct": atr / close * 100.0,
        "sequence_ok": sequence_ok,
    })


def compact(summary: dict) -> dict:
    keys = ("closed_trades", "wins", "losses", "win_rate_pct", "profit_factor",
            "net_pnl_usdt", "avg_trade_pnl_usdt", "max_drawdown_pct", "ending_equity_usdt",
            "halts_by_end", "halt_reason", "halt_timestamp_ms")
    return {key: summary[key] for key in keys}


def run_variant(name: str, frames: dict, fee: float, slip: float,
                adaptive_stops: bool, apply_regime_multiplier: bool) -> dict:
    print(f"Simulating {name} (fee={fee:g} bps, slip={slip:g} bps)...", flush=True)
    extra = {}
    if adaptive_stops:
        extra = {
            "stop_min_pct": 0.005,
            "stop_max_pct": 0.045,
            "take_profit_atr_multiple": 3.0,
            "trailing_atr_multiple": 2.0,
            # Handover v2 changes the trail distance but omits its activation rule.
            # Keep the current bot's +1.5% break-even trigger for this test.
            "breakeven_trigger_pct": 0.015,
        }
    if apply_regime_multiplier:
        extra["sizing_multiplier_col"] = "sizing_multiplier"

    full = simulate(frames, fee, slip, enforce_risk_halts=False, **extra)
    print(f"  {name}: completed full no-halt replay", flush=True)
    full_halted = simulate(frames, fee, slip, enforce_risk_halts=True, **extra)
    print(f"  {name}: completed full configured-halt replay", flush=True)
    periods = {
        "train_2021_feb_2023": (WARMUP_END, CUT_2024),
        "validation_2024_2025": (CUT_2024, CUT_2026),
        "recent_2026_jan_oct_reused": (CUT_2026, END_2026),
    }
    return {
        "full_no_halts": compact(summarize(name, full)),
        "full_configured_halts": compact(summarize(name, full_halted)),
        "periods_no_halts": {
            label: compact(summarize(name, simulate(
                frames, fee, slip, enforce_risk_halts=False,
                start_timestamp_ms=start, end_timestamp_ms=end, **extra,
            )))
            for label, (start, end) in periods.items()
        },
    }


def main() -> None:
    frames = {symbol: build_signals(symbol) for symbol in SYMBOLS}
    funnel = {}
    for symbol, frame in frames.items():
        funnel[symbol] = {
            "bars": int(len(frame)),
            "contiguous_150_bar_checks": int(frame.sequence_ok.sum()),
            "eligible_buy_signals": int((frame.signal == "BUY").sum()),
            "sell_signals": int((frame.signal == "SELL").sum()),
            "strong_up_bars": int(frame.strong_trend_up.sum()),
            "neutral_bars": int(frame.neutral_regime.sum()),
        }

    scenarios = {}
    for fee_name, fee, slip in (("base", FEE_BPS, SLIPPAGE_BPS),
                                ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS)):
        scenarios[fee_name] = {
            "costs_bps_per_side": {"fee": fee, "slippage": slip},
            "variants": {
                "handover_signals_existing_risk": run_variant(
                    "handover_signals_existing_risk", frames, fee, slip,
                    adaptive_stops=False, apply_regime_multiplier=False,
                ),
                "handover_atr_stops_existing_size": run_variant(
                    "handover_atr_stops_existing_size", frames, fee, slip,
                    adaptive_stops=True, apply_regime_multiplier=False,
                ),
                "handover_atr_stops_half_neutral_size": run_variant(
                    "handover_atr_stops_half_neutral_size", frames, fee, slip,
                    adaptive_stops=True, apply_regime_multiplier=True,
                ),
            },
        }

    result = {
        "method": "Literal handover v2 pullback signal, 1h Binance Spot OHLCV, 2021-01 through 2026-10; completed-candle signals fill next hourly open; no parameter search.",
        "universe": list(SYMBOLS),
        "shared_simulator": {
            "initial_equity_usdt": 10000,
            "risk_per_trade_pct": 1.0,
            "position_cap_pct": 10.0,
            "max_positions": 3,
            "correlation_cap": 0.85,
            "daily_loss_halt_pct": 3.0,
            "rolling_drawdown_halt_pct": 5.0,
            "configured_consecutive_loss_halt": 3,
            "same_bar_stop_target_rule": "stop first",
        },
        "candidate_definition": {
            "regime": "ADX14 >= 22; strong up if close > EMA50 and EMA50 4-bar slope > 0.05%; neutral multiplier 0.5 otherwise if not ranging/down; no entries when ADX < 22 or close < EMA50.",
            "entry": "Current low <= EMA20 * 1.003 OR RSI2 < 15, and current close > EMA20.",
            "exit": "Current close < EMA50 OR RSI14 > 80. BUY precedence retained from handover pseudocode.",
            "candidate_signal_guards": "150-bar contiguous hourly sequence; all signals use closed bars.",
            "atr_risk_test": "Initial stop 1.5 ATR clamped to 0.5%-4.5%; target 3 ATR; retain current +1.5% break-even activation, then trail 2 ATR using current closed-bar ATR. The handover did not specify a new activation threshold.",
            "sizing_note": "Handover code returns 0.5 neutral multiplier but does not wire it into position sizing; a separate sensitivity variant applies it.",
        },
        "limitations": [
            "Fixed BTC/ETH/SOL universe; OHLCV cannot reconstruct historical spread, queue position, fees, exact slippage, or within-bar path.",
            "2026 data was examined in earlier project research and is not a pristine holdout.",
            "The neutral half-size and trailing activation details are ambiguous in the handover; explicitly tested as separate documented assumptions.",
            "A historical simulation is not evidence of future profitability or live readiness.",
        ],
        "signal_funnel": funnel,
        "scenarios": scenarios,
    }
    output = OUT / "handover_pullback_backtest.json"
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "funnel": funnel,
        "base_full": {k: v["full_no_halts"] for k, v in scenarios["base"]["variants"].items()},
        "stress_full": {k: v["full_no_halts"] for k, v in scenarios["stress"]["variants"].items()},
        "base_periods": {k: v["periods_no_halts"] for k, v in scenarios["base"]["variants"].items()},
    }, indent=2))


if __name__ == "__main__":
    main()
