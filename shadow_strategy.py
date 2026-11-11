"""Read-only strategy candidate used only for market-check diagnostics.

This module deliberately returns plain metadata. It has no exchange, order,
portfolio, or persistence dependencies and cannot place trades.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from ta.trend import ADXIndicator, EMAIndicator
from ta.volatility import AverageTrueRange


CANDIDATE_ID = "volume_breakout_24h_ema100_v1"
# EMA100 plus its 24-bar slope comparison requires 125 closed bars.
LOOKBACK_CANDLES = 125


def evaluate_shadow_candidate(ohlcv: list) -> dict:
    """Evaluate a completed-candle breakout hypothesis; never execute it.

    BUY requires a close above the previous 24-hour range, rising EMA100,
    ADX >= 20, ATR between 0.3% and 2.5%, and volume >= 1.2x its 20-bar mean.
    SELL denotes an exit condition (close below the prior 12-hour low or EMA50).
    These are research signals, not calibrated probabilities or orders.
    """
    result = {"candidate_id": CANDIDATE_ID, "signal": "HOLD", "status": "ok"}
    if not isinstance(ohlcv, list) or len(ohlcv) < LOOKBACK_CANDLES:
        result.update(status="insufficient_data", reason=f"Needs 125 closed candles; received {len(ohlcv) if isinstance(ohlcv, list) else 0}")
        return result

    try:
        frame = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        numeric = frame[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce")
        if not numeric.notna().all().all() or not np.isfinite(numeric.to_numpy()).all():
            result.update(status="invalid_data", reason="Non-finite OHLCV data")
            return result
        if (
            (numeric[["open", "high", "low", "close"]] <= 0).any().any()
            or (numeric.volume < 0).any()
            or (numeric.high < numeric.low).any()
            or (numeric.high < numeric[["open", "close"]].max(axis=1)).any()
            or (numeric.low > numeric[["open", "close"]].min(axis=1)).any()
        ):
            result.update(status="invalid_data", reason="OHLCV values fail candle consistency checks")
            return result
        close, high, low, volume = (numeric[name] for name in ("close", "high", "low", "volume"))
        atr = AverageTrueRange(high, low, close, window=14).average_true_range()
        adx = ADXIndicator(high, low, close, window=14).adx()
        ema50 = EMAIndicator(close, window=50).ema_indicator()
        ema100 = EMAIndicator(close, window=100).ema_indicator()
        volume_mean = volume.rolling(20, min_periods=20).mean()
        prior_high = high.shift(1).rolling(24, min_periods=24).max()
        prior_low = low.shift(1).rolling(12, min_periods=12).min()

        i = len(frame) - 1
        vals = {
            "open": float(numeric.open.iloc[i]),
            "high": float(numeric.high.iloc[i]),
            "low": float(numeric.low.iloc[i]),
            "close": float(close.iloc[i]),
            "volume": float(volume.iloc[i]),
            "breakout_level": float(prior_high.iloc[i]),
            "exit_level": float(prior_low.iloc[i]),
            "adx": float(adx.iloc[i]),
            "atr_pct": float(atr.iloc[i] / close.iloc[i] * 100),
            "volume_ratio": float(volume.iloc[i] / volume_mean.iloc[i]) if volume_mean.iloc[i] > 0 else None,
            "ema100_rising": bool(ema100.iloc[i] > ema100.iloc[i - 24]),
        }
        if not all(math.isfinite(v) for v in vals.values() if isinstance(v, (int, float))):
            result.update(status="insufficient_indicators", reason="Indicators are not ready")
            return result

        previous_high = prior_high.iloc[i - 1]
        crossed_up = close.iloc[i] > prior_high.iloc[i] and close.iloc[i - 1] <= previous_high
        buy = (
            crossed_up and close.iloc[i] > ema100.iloc[i] and vals["ema100_rising"]
            and vals["adx"] >= 20 and 0.3 <= vals["atr_pct"] <= 2.5
            and vals["volume_ratio"] is not None and vals["volume_ratio"] >= 1.2
        )
        exit_signal = close.iloc[i] < prior_low.iloc[i] or close.iloc[i] < ema50.iloc[i]
        result["signal"] = "BUY" if buy else "SELL" if exit_signal else "HOLD"
        result["reason"] = (
            "24h breakout with trend, volatility, and volume confirmation" if buy else
            "Exit condition: below prior 12h low or EMA50" if exit_signal else
            "No qualifying 24h breakout or exit condition"
        )
        result["indicators"] = {
            key: round(value, 4) if isinstance(value, float) else value
            for key, value in vals.items()
        }
        return result
    except (IndexError, TypeError, ValueError, ZeroDivisionError):
        result.update(status="invalid_data", reason="Could not evaluate candidate indicators")
        return result
