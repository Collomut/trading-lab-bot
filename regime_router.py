"""Causal hourly regime classifier and guarded Testnet strategy router.

This module is deliberately unable to place orders. Regime labels are a
diagnostic hypothesis, not evidence that a strategy has an edge. Dynamic
routing currently selects only the EMA72 trend candidate after a stable
uptrend classification. Other regimes deliberately route to cash because no
alternative has passed the project's cost-adjusted screen.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from ta.trend import ADXIndicator, EMAIndicator
from ta.volatility import AverageTrueRange


ROUTER_ID = "rule_dynamic_regime_router_v1"
MIN_CANDLES = 2501
HOUR_MS = 3_600_000
ACTIVE_CANDIDATE = "macro_donchian_ema72"
DYNAMIC_PROFILE = "dynamic_regime"
ROUTER_ERROR_MESSAGES = {
    "non_finite_ohlcv": "Market data contains an invalid number; this check was skipped.",
    "invalid_timestamp": "Market data has an invalid candle time; this check was skipped.",
    "non_positive_price": "Market data contains a zero or negative price; this check was skipped.",
    "negative_volume": "Market data contains negative volume; this check was skipped.",
    "high_below_low": "A candle high is below its low; this check was skipped.",
    "high_below_open_or_close": "A candle high is below its open or close; this check was skipped.",
    "low_above_open_or_close": "A candle low is above its open or close; this check was skipped.",
    "hourly_sequence_gap_or_duplicate": "Hourly candles have a gap or duplicate; this check was skipped.",
    "malformed_ohlcv": "Market data is incomplete or malformed; this check was skipped.",
    "indicator_warmup_incomplete": "Indicators need more completed hourly candles to warm up.",
}


def _readable_router_error(error: str) -> str:
    parts = error.split("_")
    if len(parts) == 7 and parts[0] == "needs" and parts[2:6] == ["closed", "hourly", "candles", "received"]:
        try:
            required, received = int(parts[1]), int(parts[6])
            return f"Need {required:,} completed hourly candles; received {received:,}."
        except ValueError:
            pass
    return ROUTER_ERROR_MESSAGES.get(error, "Market data is unavailable; this check was skipped.")


def _validate_ohlcv(ohlcv: list) -> tuple[pd.DataFrame | None, str | None]:
    if not isinstance(ohlcv, list) or len(ohlcv) < MIN_CANDLES:
        count = len(ohlcv) if isinstance(ohlcv, list) else 0
        return None, f"needs_{MIN_CANDLES}_closed_hourly_candles_received_{count}"
    try:
        frame = pd.DataFrame(
            ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        values = frame.apply(pd.to_numeric, errors="raise")
        array = values.to_numpy(dtype=float)
        if not np.isfinite(array).all():
            return None, "non_finite_ohlcv"
        if (values.timestamp <= 0).any():
            return None, "invalid_timestamp"
        if (values[["open", "high", "low", "close"]] <= 0).any().any():
            return None, "non_positive_price"
        if (values.volume < 0).any():
            return None, "negative_volume"
        if (values.high < values.low).any():
            return None, "high_below_low"
        if (values.high < values[["open", "close"]].max(axis=1)).any():
            return None, "high_below_open_or_close"
        if (values.low > values[["open", "close"]].min(axis=1)).any():
            return None, "low_above_open_or_close"
        timestamps = values.timestamp.to_numpy(dtype=np.int64)
        if np.any(np.diff(timestamps) != HOUR_MS):
            return None, "hourly_sequence_gap_or_duplicate"
        values.timestamp = timestamps
        return values, None
    except (TypeError, ValueError, OverflowError, KeyError):
        return None, "malformed_ohlcv"


def _classify_closed_bar(close, index: int, adx, atr, ema200) -> tuple[str, dict] | None:
    """Read indicators at `index`; each indicator is causal and uses no future bars."""
    close_now = float(close.iloc[index])
    adx_now = float(adx.iloc[index])
    atr_pct = float(atr.iloc[index]) / close_now * 100.0
    ema_now = float(ema200.iloc[index])
    ema_24_bars_ago = float(ema200.iloc[index - 24])
    if not all(math.isfinite(value) for value in (close_now, adx_now, atr_pct, ema_now, ema_24_bars_ago)):
        return None

    if atr_pct > 3.0:
        regime = "high_volatility"
    elif adx_now >= 25.0 and close_now > ema_now and ema_now > ema_24_bars_ago:
        regime = "uptrend"
    elif adx_now >= 25.0 and close_now < ema_now and ema_now < ema_24_bars_ago:
        regime = "downtrend"
    elif adx_now <= 18.0:
        regime = "range"
    else:
        regime = "transition"
    return regime, {
        "adx": round(adx_now, 2),
        "atr_pct": round(atr_pct, 4),
        "close": round(close_now, 8),
        "ema200": round(ema_now, 8),
        "ema200_24h_slope_pct": round((ema_now / ema_24_bars_ago - 1) * 100, 4),
    }


def evaluate_regime_router(ohlcv: list, active_strategy: str) -> dict:
    """Return a fail-closed regime observation and optional Testnet entry route.

    Three consecutive completed hourly bars must classify identically before a
    regime is considered stable. `entry_route_enabled` only permits evaluation
    of a candidate strategy; its signal and the independent risk/execution gates
    are still required before an order can be submitted.
    """
    dynamic_mode = str(active_strategy).strip().lower() == DYNAMIC_PROFILE
    result = {
        "router_id": ROUTER_ID,
        "status": "dynamic_testnet" if dynamic_mode else "shadow_only",
        "regime": "uncertain",
        "raw_regimes": [],
        "candidate_strategy": None,
        "selected_strategy": None,
        "route": "no_validated_strategy",
        "entry_route_enabled": False,
        "entry_authorized": False,
        "reason": "No validated strategy is available for this regime.",
    }
    frame, error = _validate_ohlcv(ohlcv)
    if error:
        result.update(
            status="unavailable",
            reason=_readable_router_error(error),
            reason_code=error,
        )
        return result

    close, high, low = frame.close, frame.high, frame.low
    adx = ADXIndicator(high, low, close, window=14).adx()
    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    ema200 = EMAIndicator(close, window=200).ema_indicator()
    observations = [_classify_closed_bar(close, index, adx, atr, ema200)
                    for index in range(len(frame) - 3, len(frame))]
    if any(item is None for item in observations):
        result.update(
            status="unavailable",
            reason=ROUTER_ERROR_MESSAGES["indicator_warmup_incomplete"],
            reason_code="indicator_warmup_incomplete",
        )
        return result

    raw_regimes = [item[0] for item in observations]
    result["raw_regimes"] = raw_regimes
    result["indicators"] = observations[-1][1]
    if len(set(raw_regimes)) != 1:
        result.update(
            regime="transition",
            reason="Regime has not remained unchanged for three completed hourly candles.",
        )
        return result

    regime = raw_regimes[-1]
    result["regime"] = regime
    if regime == "uptrend" and dynamic_mode:
        result.update(
            candidate_strategy=ACTIVE_CANDIDATE,
            selected_strategy=ACTIVE_CANDIDATE,
            route=ACTIVE_CANDIDATE,
            entry_route_enabled=True,
            reason="Stable uptrend confirmed; EMA72 trend candidate may evaluate an entry. Risk checks still control order placement.",
        )
    elif regime == "uptrend" and str(active_strategy).strip().lower() == ACTIVE_CANDIDATE:
        result.update(
            candidate_strategy=ACTIVE_CANDIDATE,
            selected_strategy=ACTIVE_CANDIDATE,
            route="observe_existing_strategy_only",
            reason="The configured Testnet strategy is the only trend candidate; its existing rules still decide signals.",
        )
    elif regime == "uptrend":
        result["reason"] = "The uptrend candidate is not enabled; no strategy change is made in shadow mode."
    elif regime == "range":
        result["reason"] = "The tested range strategy did not pass cost-adjusted research; no entry strategy is selected."
    elif regime == "downtrend":
        result["reason"] = "No validated long-only entry strategy is available in a downtrend; existing position exits remain unchanged."
    elif regime == "high_volatility":
        result["reason"] = "Volatility is above the configured 3% ATR threshold; no new-entry strategy is selected."
    else:
        result["reason"] = "Market conditions are between the defined regimes; no strategy is selected."
    return result
