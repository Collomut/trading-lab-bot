"""Research-only replay of the supplied high-confluence trading blueprint.

This module is deliberately not imported by the trading application. It
replays completed Binance Spot 1-hour candles, enters at the next open, and
uses the repository's portfolio/risk/cost simulator. It does not validate the
blueprint's proposed 15-minute operating interval; that requires 15-minute
historical data and a separate evaluation.
"""
from __future__ import annotations

import json
import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, EMAIndicator, MACD
from ta.volatility import AverageTrueRange

from backtest_strategy import (
    DATA,
    OUT,
    FEE_BPS,
    SLIPPAGE_BPS,
    STRESS_FEE_BPS,
    STRESS_SLIPPAGE_BPS,
    compute_signals,
    simulate,
)


def compute_blueprint_signals(symbol: str, *, preserve_regime_and_volume_gates: bool = True) -> pd.DataFrame:
    """Build blueprint state+trigger signals on the supplied complete OHLCV rows.

    Short-pattern conditions are represented as SELL-to-exit signals only.
    The production connector is Spot-only and this experiment never opens a
    short position.
    """
    raw = pd.read_csv(DATA / f"{symbol}_1h.csv")
    raw = raw.drop_duplicates("timestamp_ms").sort_values("timestamp_ms").reset_index(drop=True)
    close, high, low, volume = raw.close, raw.high, raw.low, raw.volume

    ema50 = EMAIndicator(close, window=50).ema_indicator()
    rsi = RSIIndicator(close, window=14).rsi()
    macd_obj = MACD(close, window_fast=12, window_slow=26, window_sign=9)
    macd, macd_signal = macd_obj.macd(), macd_obj.macd_signal()
    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    adx = ADXIndicator(high, low, close, window=14).adx()
    volume_avg = volume.rolling(window=20, min_periods=20).mean()

    # Current candle is the just-completed candle. RSI's state window includes
    # it, matching the document's "within the last five candles" wording.
    rsi_was_below_40 = rsi.rolling(5, min_periods=5).min() < 40.0
    rsi_was_above_60 = rsi.rolling(5, min_periods=5).max() > 60.0
    macd_cross_up = (macd > macd_signal) & (macd.shift(1) <= macd_signal.shift(1))
    macd_cross_down = (macd < macd_signal) & (macd.shift(1) >= macd_signal.shift(1))

    buy_setup = (close > ema50) & rsi_was_below_40 & (rsi < 65.0) & macd_cross_up
    sell_setup = (close < ema50) & rsi_was_above_60 & (rsi > 35.0) & macd_cross_down

    atr_pct = atr / close * 100.0
    regime_ok = adx.notna() & atr_pct.notna() & (adx >= 18.0) & (atr_pct <= 3.0)
    volume_ok = volume_avg.isna() | (volume_avg <= 0) | (volume >= 0.3 * volume_avg)
    if preserve_regime_and_volume_gates:
        buy = buy_setup & regime_ok & volume_ok
    else:
        buy = buy_setup

    signal = np.full(len(raw), "HOLD", dtype=object)
    # Existing positions may exit on the blueprint's bearish mirror setup;
    # never turn this into an opening short in the Spot portfolio simulator.
    signal[sell_setup.to_numpy()] = "SELL"
    signal[(buy & ~sell_setup).to_numpy()] = "BUY"

    # Store the completed-candle swing level. The fixed 1.5% leg is calculated
    # from the actual next-open entry later in the portfolio simulator.
    swing_low = low.rolling(3, min_periods=3).min()
    return pd.DataFrame({
        "timestamp_ms": raw.timestamp_ms,
        "open": raw.open,
        "high": raw.high,
        "low": raw.low,
        "close": raw.close,
        "volume": raw.volume,
        "atr": atr,
        "atr_pct": atr_pct,
        "adx": adx,
        "ema50": ema50,
        "rsi": rsi,
        "macd": macd,
        "macd_signal": macd_signal,
        "buy_setup": buy_setup,
        "sell_setup": sell_setup,
        "regime_ok": regime_ok,
        "volume_ok": volume_ok,
        "swing_low_reference": swing_low,
        "signal": signal,
    })


def summarize_segment(name: str, sim: dict, start: int | None, end: int | None) -> dict:
    """Summarize a chronological slice from a continuous full-history replay."""
    trades = [trade for trade in sim["trades"]
              if (start is None or trade["exit_time"] >= start)
              and (end is None or trade["exit_time"] < end)]
    wins = [trade for trade in trades if trade["pnl"] > 0]
    losses = [trade for trade in trades if trade["pnl"] < 0]
    gross_win = sum(trade["pnl"] for trade in wins)
    gross_loss = -sum(trade["pnl"] for trade in losses)
    pnl = sum(trade["pnl"] for trade in trades)
    points = [value for ts, value, _ in sim["equity"]
              if (start is None or ts >= start) and (end is None or ts < end)]
    if points:
        path = np.asarray([points[0], *points], dtype=float)
        peaks = np.maximum.accumulate(path)
        max_dd = float(np.max((peaks - path) / np.maximum(peaks, 1e-12) * 100.0))
    else:
        max_dd = 0.0
    return {
        "variant": name,
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100.0, 2) if trades else 0.0,
        "net_pnl_usdt": round(pnl, 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else (None if not gross_win else "inf"),
        "max_drawdown_pct_in_segment": round(max_dd, 3),
        "ending_equity_usdt": round(points[-1], 2) if points else None,
        "halts_by_end": sim["halted"],
    }


def main() -> None:
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    cut_2024 = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)
    cut_2026 = int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000)
    periods = (
        ("2021_2023", None, cut_2024),
        ("2024_2025", cut_2024, cut_2026),
        ("2026_ytd_through_oct_2026", cut_2026, None),
    )
    variants = (
        ("current_combined_strategy", lambda symbol: compute_signals(symbol, 2, True, True, 2), False),
        ("blueprint_plus_existing_adx_atr_volume_gates", lambda symbol: compute_blueprint_signals(symbol, preserve_regime_and_volume_gates=True), True),
        ("blueprint_no_adx_or_volume_diagnostic", lambda symbol: compute_blueprint_signals(symbol, preserve_regime_and_volume_gates=False), True),
    )

    results = []
    funnels = {}
    for name, builder, blueprint_stops in variants:
        frames = {symbol: builder(symbol) for symbol in symbols}
        funnels[name] = {}
        for symbol, frame in frames.items():
            item = {"bars": int(len(frame)), "buy_signals": int((frame.signal == "BUY").sum()),
                    "sell_signals": int((frame.signal == "SELL").sum())}
            if "buy_setup" in frame:
                item.update({"raw_buy_setups": int(frame.buy_setup.sum()),
                             "sell_exit_setups": int(frame.sell_setup.sum()),
                             "buy_vetoed_by_regime_or_volume": int((frame.buy_setup & (frame.signal != "BUY")).sum())})
            funnels[name][symbol] = item

        for cost_name, fee_bps, slip_bps in (
            ("base_costs", FEE_BPS, SLIPPAGE_BPS),
            ("stress_costs", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS),
        ):
            sim = simulate(frames, fee_bps, slip_bps, enforce_risk_halts=False,
                           blueprint_stop_model=blueprint_stops)
            for period_name, start, end in periods:
                results.append({"cost_scenario": cost_name, "period": period_name,
                                **summarize_segment(name, sim, start, end)})
            print(f"completed {name} / {cost_name}", flush=True)

    OUT.mkdir(exist_ok=True)
    output = {
        "method": {
            "source": "Binance official Spot Public Data monthly 1h klines",
            "coverage": "2021-01 through 2026-10",
            "execution": "signal on completed candle, entry/strategy exit at next open",
            "blueprint_entry": "close > EMA50; RSI14 dipped below 40 at least once in rolling 5 completed candles and current RSI < 65; MACD(12,26,9) bullish crossover on the latest completed candle",
            "blueprint_sell": "close < EMA50; RSI14 exceeded 60 within rolling 5 completed candles and current RSI > 35; latest completed candle has bearish MACD crossover; SELL closes an existing Spot long only",
            "blueprint_stop_and_target": "stop is max(prior 3-candle low, 1.5% below actual next-open entry); target is 2R from entry; entry rejected if already at/below swing low",
            "portfolio_limits": "shared existing simulator: 1% max equity risk, 10% position notional cap, 3 positions, 0.85 rolling correlation gate",
            "base_costs_per_side_bps": {"fee": FEE_BPS, "slippage": SLIPPAGE_BPS},
            "stress_costs_per_side_bps": {"fee": STRESS_FEE_BPS, "slippage": STRESS_SLIPPAGE_BPS},
            "limitations": [
                "This uses 1h data; it does not establish results on the proposed 15m interval.",
                "The supplied document's frequency multiplier claim is not validated by this sample.",
                "OHLCV cannot model book depth, live spread, exact fees, latency, or precise intrabar stop ordering.",
                "No short positions are simulated because the production exchange connector is Spot-only.",
                "These are exploratory historical results, not evidence of future profitability.",
            ],
        },
        "funnels": funnels,
        "results": results,
    }
    path = OUT / "blueprint_results.json"
    path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
