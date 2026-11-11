"""Predeclared strategy-family screen; research only, never imported by the bot.

Tests four distinct spot-long ideas against the current entry signals using a
shared conservative portfolio simulator. No parameter search is performed.
The fixed three-asset universe is survivorship-biased and all historical data
has been inspected in earlier project research; results are exploratory.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, EMAIndicator
from ta.volatility import AverageTrueRange

from backtest_strategy import (
    DATA, OUT, FEE_BPS, SLIPPAGE_BPS, STRESS_FEE_BPS,
    STRESS_SLIPPAGE_BPS, compute_signals, simulate,
    summarize as summarize_shared_simulator,
)

# Same investable universe as the deployed Testnet bot; avoids making the new
# candidate look better by expanding into hindsight-selected survivors.
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
HOUR_MS = 3_600_000


def base_frames():
    return {s: compute_signals(s, interval="1h", min_trend_adx=20.0, max_atr_pct=3.0)
            for s in SYMBOLS}


def add_indicators(frames):
    result = {}
    for symbol, frame in frames.items():
        raw = pd.read_csv(DATA / f"{symbol}_1h.csv").drop_duplicates("timestamp_ms").sort_values("timestamp_ms").reset_index(drop=True)
        close, high, low, volume = raw.close, raw.high, raw.low, raw.volume
        atr = AverageTrueRange(high, low, close, window=14).average_true_range()
        adx = ADXIndicator(high, low, close, window=14).adx()
        ema50 = EMAIndicator(close, window=50).ema_indicator()
        ema100 = EMAIndicator(close, window=100).ema_indicator()
        ema200 = EMAIndicator(close, window=200).ema_indicator()
        rsi = RSIIndicator(close, window=14).rsi()
        vol_avg = volume.rolling(20, min_periods=20).mean()
        seq_ok = raw.timestamp_ms.diff().eq(HOUR_MS).rolling(99, min_periods=99).sum().eq(99)
        out = raw[["timestamp_ms", "open", "high", "low", "close", "volume"]].copy()
        out["atr"] = atr
        out["adx"] = adx
        out["ema50"] = ema50
        out["ema100"] = ema100
        out["ema200"] = ema200
        out["rsi"] = rsi
        out["atr_pct"] = atr / close * 100
        out["volume_ok"] = vol_avg.isna() | (vol_avg <= 0) | (volume >= 0.3 * vol_avg)
        out["sequence_ok"] = seq_ok
        out["signal"] = frame.signal.to_numpy()
        result[symbol] = out
    return result


def build_donchian(frames):
    out = {}
    for symbol, f in frames.items():
        upper = f.high.shift(1).rolling(55, min_periods=55).max()
        prior_upper = upper.shift(1)
        lower = f.low.shift(1).rolling(20, min_periods=20).min()
        trend_up = (f.close > f.ema200) & (f.ema200 > f.ema200.shift(24))
        regime = (f.adx >= 20) & f.atr_pct.between(0.3, 3.0) & f.volume_ok & f.sequence_ok
        buy = (f.close > upper) & (f.close.shift(1) <= prior_upper) & trend_up & regime
        sell = (f.close < lower) & f.sequence_ok
        f = f.copy()
        f["signal"] = np.where(sell, "SELL", np.where(buy, "BUY", "HOLD"))
        out[symbol] = f
    return out


def build_volume_breakout(frames):
    """Predeclared 24-hour range break requiring expanding volume and EMA100 trend."""
    out = {}
    for symbol, f in frames.items():
        upper = f.high.shift(1).rolling(24, min_periods=24).max()
        lower = f.low.shift(1).rolling(12, min_periods=12).min()
        vol_avg = f.volume.rolling(20, min_periods=20).mean()
        trend_up = (f.close > f.ema100) & (f.ema100 > f.ema100.shift(24))
        regime = (f.adx >= 20) & f.atr_pct.between(0.3, 2.5) & f.sequence_ok
        buy = ((f.close > upper) & (f.close.shift(1) <= upper.shift(1)) & trend_up
               & regime & (f.volume >= 1.2 * vol_avg))
        sell = (f.close < lower) | (f.close < f.ema50)
        f = f.copy()
        f["signal"] = np.where(sell, "SELL", np.where(buy, "BUY", "HOLD"))
        out[symbol] = f
    return out


def build_mean_reversion(frames):
    out = {}
    for symbol, f in frames.items():
        mid = f.close.rolling(20, min_periods=20).mean()
        sd = f.close.rolling(20, min_periods=20).std(ddof=0)
        lower = mid - 2.0 * sd
        trend = f.close > f.ema200
        regime = f.atr_pct.between(0.3, 3.0) & f.sequence_ok
        buy = (f.close.shift(1) < lower.shift(1)) & (f.close >= lower) & (f.rsi.shift(1) < 35) & (f.rsi >= 35) & trend & regime
        sell = (f.close >= mid) | (f.close < f.ema200)
        f = f.copy()
        f["signal"] = np.where(sell, "SELL", np.where(buy, "BUY", "HOLD"))
        out[symbol] = f
    return out


def build_relative_momentum(frames):
    # Rank 28-day momentum only at Sunday 23:00 UTC, after the weekly candle is closed.
    indexed = {s: f.set_index("timestamp_ms") for s, f in frames.items()}
    common = sorted(set.intersection(*(set(f.index) for f in indexed.values())))
    close = pd.DataFrame({s: indexed[s].loc[common, "close"].to_numpy() for s in SYMBOLS}, index=common)
    timestamp = pd.to_datetime(close.index, unit="ms", utc=True)
    weekly_close = (timestamp.dayofweek == 6) & (timestamp.hour == 23)
    returns = close / close.shift(28 * 24) - 1.0
    rank = returns.rank(axis=1, ascending=False, method="first")
    out = {}
    for symbol, f in frames.items():
        f = f.copy()
        aligned_rank = pd.Series(rank[symbol].to_numpy(), index=common).reindex(f.timestamp_ms).to_numpy()
        aligned_return = pd.Series(returns[symbol].to_numpy(), index=common).reindex(f.timestamp_ms).to_numpy()
        aligned_week = pd.Series(weekly_close, index=common).reindex(f.timestamp_ms).fillna(False).to_numpy(dtype=bool)
        buy = aligned_week & (aligned_rank <= 2) & (aligned_return > 0) & (f.close.to_numpy() > f.ema200.to_numpy()) & f.sequence_ok.to_numpy()
        sell = (aligned_week & (aligned_rank > 4)) | (f.close.to_numpy() < f.ema200.to_numpy())
        f["signal"] = np.where(sell, "SELL", np.where(buy, "BUY", "HOLD"))
        out[symbol] = f
    return out


def main():
    raw = base_frames()
    frames = add_indicators(raw)
    candidates = {
        "current_2of3_adx20": raw,
        "donchian55_ema200_adx20": build_donchian(frames),
        "volume_confirmed_24h_ema100_breakout": build_volume_breakout(frames),
        "uptrend_rsi_bollinger_reversion": build_mean_reversion(frames),
        "weekly_28d_relative_momentum_top2": build_relative_momentum(frames),
    }
    # Deterministic chronological splits; note the entire archive was already used by earlier research.
    bounds = {
        "full_2021_2026_oct": (1609459200000, 1793491200000),
        "train_2021_2023": (1609459200000, 1704067200000),
        "validation_2024_2025": (1704067200000, 1767225600000),
        "recent_2026_jan_oct": (1767225600000, 1793491200000),
    }
    results = {"method": "predeclared research-only strategy-family comparison; 1h Binance Spot OHLCV; next-open entries/exits; no tuning; shared project simulator with 1% risk, 10% notional cap, max 3 positions, correlation guard, ATR stop, 3% target, break-even/trailing logic and conservative stop-first intrabar convention; global risk halts disabled to expose full-sample strategy behavior",
               "universe": list(SYMBOLS), "base_cost_bps_per_side": {"fee": FEE_BPS, "slippage": SLIPPAGE_BPS},
               "stress_cost_bps_per_side": {"fee": STRESS_FEE_BPS, "slippage": STRESS_SLIPPAGE_BPS},
               "sample_warning": "Fixed survivor universe and data previously reused in project research; recent 2026 segment is not a pristine holdout.",
               "strategies": {}}
    for name, strategy_frames in candidates.items():
        results["strategies"][name] = {}
        for scenario, fee, slip in (("base", FEE_BPS, SLIPPAGE_BPS), ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS)):
            print(f"Simulating {name} / {scenario} costs...", flush=True)
            sim = simulate(strategy_frames, fee, slip, enforce_risk_halts=False)
            for split_name, (start, end) in bounds.items():
                full_summary = summarize_shared_simulator(name, sim, start_ts=start, end_ts=end)
                results["strategies"][name].setdefault(split_name, {})[scenario] = {
                    key: full_summary[key] for key in (
                        "closed_trades", "wins", "losses", "win_rate_pct", "profit_factor",
                        "net_pnl_usdt", "avg_trade_pnl_usdt", "max_drawdown_pct",
                    )
                }
    out = OUT / "strategy_family_comparison.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    for name, segments in results["strategies"].items():
        recent = segments["recent_2026_jan_oct"]["base"]
        full = segments["full_2021_2026_oct"]["base"]
        stress = segments["full_2021_2026_oct"]["stress"]
        print(f"{name}: recent={recent}; full={full}; stress={stress}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
