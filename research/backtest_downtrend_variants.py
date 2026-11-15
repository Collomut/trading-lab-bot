"""Sweep gated downtrend / oversold mean-reversion candidates; research only, no orders.

Every candidate is long-only Spot and shares the same simulator, fills, costs,
portfolio caps, and halts as the rest of the repository. The baseline
(rsi40_cross) is the already-audited downtrend RSI-recovery experiment; the other
variants test whether a deeper oversold trigger and/or a one-bar reversal
confirmation can lift the profit factor above 1 without gutting trade count.

Run `python research/backtest_downtrend_variants.py` (full) or
`python research/backtest_downtrend_variants.py review` (fast shortlist on the
2026 review period only).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, EMAIndicator
from ta.volatility import AverageTrueRange, BollingerBands

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from backtest_ema72_donchian import SPLITS
from backtest_strategy import (
    DATA, OUT, FEE_BPS, SLIPPAGE_BPS, STRESS_FEE_BPS,
    STRESS_SLIPPAGE_BPS, simulate, summarize,
)

HOUR_MS = 3_600_000
BASE_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
EXPANDED_SYMBOLS = BASE_SYMBOLS + ("BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT")
CANDIDATE = "downtrend_oversold_mean_reversion"

# Each variant: regime=True requires a stable (3-hour) downtrend per regime_router.
# rsi_cross: entry when RSI14 crosses up through this level.
# confirm: additionally require the close to exceed the prior bar's high (reversal).
# deep_pct: require close at least this far below EMA200 (oversold extension).
# bb_sigma: require close below the lower Bollinger band (20, sigma).
# exit_rsi / exit_ema: signal exits (close > EMA(exit_ema) OR rsi > exit_rsi).
# risk: sizing_multiplier_col value (fraction of the base 1% risk budget).
VARIANTS = {
    "rsi40_cross":        dict(regime=True,  rsi_cross=40, confirm=False, deep_pct=0.0, bb_sigma=None, exit_rsi=70, exit_ema=20, risk=0.20),
    "rsi30_cross":        dict(regime=True,  rsi_cross=30, confirm=False, deep_pct=0.0, bb_sigma=None, exit_rsi=60, exit_ema=20, risk=0.25),
    "rsi25_cross":        dict(regime=True,  rsi_cross=25, confirm=False, deep_pct=0.0, bb_sigma=None, exit_rsi=55, exit_ema=20, risk=0.25),
    "rsi30_confirm":      dict(regime=False, rsi_cross=30, confirm=True,  deep_pct=0.0, bb_sigma=None, exit_rsi=60, exit_ema=20, risk=0.25),
    "rsi25_confirm":      dict(regime=False, rsi_cross=25, confirm=True,  deep_pct=0.0, bb_sigma=None, exit_rsi=55, exit_ema=20, risk=0.25),
    "rsi30_regime_confirm": dict(regime=True, rsi_cross=30, confirm=True, deep_pct=0.0, bb_sigma=None, exit_rsi=60, exit_ema=20, risk=0.25),
    "deep15_confirm":     dict(regime=False, rsi_cross=None, confirm=True, deep_pct=15.0, bb_sigma=None, exit_rsi=60, exit_ema=20, risk=0.25),
    "deep10_confirm":     dict(regime=False, rsi_cross=None, confirm=True, deep_pct=10.0, bb_sigma=None, exit_rsi=60, exit_ema=20, risk=0.25),
    "bb_confirm":         dict(regime=False, rsi_cross=None, confirm=True, deep_pct=0.0, bb_sigma=2.2, exit_rsi=60, exit_ema=20, risk=0.25),
}


def build_signals(symbol: str, params: dict) -> pd.DataFrame:
    raw = (pd.read_csv(DATA / f"{symbol}_1h.csv").drop_duplicates("timestamp_ms")
           .sort_values("timestamp_ms").reset_index(drop=True))
    close, high, low = raw.close, raw.high, raw.low
    rsi = RSIIndicator(close, window=14).rsi()
    ema20 = EMAIndicator(close, window=20).ema_indicator()
    ema200 = EMAIndicator(close, window=200).ema_indicator()
    adx = ADXIndicator(high, low, close, window=14).adx()
    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    swing_low = low.shift(1).rolling(5, min_periods=5).min()
    contiguous = raw.timestamp_ms.diff().eq(HOUR_MS).rolling(2499, min_periods=2499).sum().eq(2499)

    atr_pct = atr / close * 100.0
    if params["regime"]:
        regime = (contiguous & (adx >= 25.0) & (atr_pct <= 3.0)
                  & (close < ema200) & (ema200 < ema200.shift(24)))
        regime = regime & regime.shift(1, fill_value=False) & regime.shift(2, fill_value=False)
    else:
        regime = contiguous

    rsi_cross = params.get("rsi_cross")
    deep_pct = params.get("deep_pct")
    bb_sigma = params.get("bb_sigma")
    if rsi_cross is not None:
        entry = (rsi.shift(1) < rsi_cross) & (rsi >= rsi_cross)
    elif deep_pct:
        deep = close < ema200 * (1.0 - deep_pct / 100.0)
        entry = deep & (rsi.shift(1) < 35.0) & (rsi >= 35.0)
    elif bb_sigma is not None:
        bands = BollingerBands(close, window=20, window_dev=bb_sigma)
        entry = (close < bands.bollinger_lband()) & (rsi < 40.0)
    else:
        entry = pd.Series(False, index=raw.index)

    if params["confirm"]:
        entry &= close.gt(high.shift(1))

    sell = (close > ema20) | (rsi > params["exit_rsi"])

    result = raw[["timestamp_ms", "open", "high", "low", "close", "volume"]].copy()
    result["atr"] = atr
    result["swing_low_reference"] = swing_low
    result["risk_multiplier"] = params["risk"]
    result["signal"] = np.where(sell, "SELL", np.where(regime & entry, "BUY", "HOLD"))
    return result


def run_universe(frames, symbols, start, end, fee, slip) -> dict:
    selected = {symbol: frames[symbol] for symbol in symbols}
    sim = simulate(
        selected, fee, slip, enforce_risk_halts=True,
        start_timestamp_ms=start, end_timestamp_ms=end,
        initial_stop_model="swing_low_floor_4pct",
        take_profit_atr_multiple=2.0,
        sizing_multiplier_col="risk_multiplier",
        enable_breakeven_trailing=False,
    )
    result = summarize(CANDIDATE, sim)
    result["halt_reason"] = sim["halt_reason"]
    result["halt_timestamp_ms"] = sim["halt_time"]
    return result


def main() -> None:
    quick = len(sys.argv) > 1 and sys.argv[1] == "review"
    payload = {
        "candidate": CANDIDATE,
        "implementation": "Long-only Spot. Optional stable-downtrend gate; optional deep-oversold / Bollinger trigger; optional one-bar reversal confirmation (close > prior high); EMA20 or RSI exit; prior-five-bar swing stop capped at 4%; 2x ATR target; 0.20-0.25x base risk budget.",
        "costs_per_side_bps": {"base": {"fee": FEE_BPS, "slippage": SLIPPAGE_BPS},
                               "stress": {"fee": STRESS_FEE_BPS, "slippage": STRESS_SLIPPAGE_BPS}},
        "limitations": [
            "Fixed 8-coin universe is survivorship-biased",
            "2026 review data has already been used in project research",
            "OHLCV cannot recreate spread, queue position, or exact intrabar execution",
            "Positive historical sample does not establish an edge",
        ],
        "results": {},
    }
    for variant, params in VARIANTS.items():
        frames = {symbol: build_signals(symbol, params) for symbol in EXPANDED_SYMBOLS}
        payload["results"][variant] = {}
        for split, (start, end) in SPLITS.items():
            if quick and split != "review_2026_01_to_10":
                continue
            payload["results"][variant][split] = {}
            for cost, fee, slip in (("base", FEE_BPS, SLIPPAGE_BPS),
                                    ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS)):
                if quick and cost == "stress":
                    continue
                payload["results"][variant][split][cost] = {
                    "three_symbols": run_universe(frames, BASE_SYMBOLS, start, end, fee, slip),
                    "eight_symbols": run_universe(frames, EXPANDED_SYMBOLS, start, end, fee, slip),
                }
    OUT.mkdir(exist_ok=True)
    output = OUT / "downtrend_oversold_variants_results.json"
    output.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    for variant, splits in payload["results"].items():
        for split, costs in splits.items():
            for cost, universes in costs.items():
                for universe, stats in universes.items():
                    pf = stats["profit_factor"]
                    pf = f"{pf:.2f}" if isinstance(pf, (int, float)) else str(pf)
                    print(f"{variant:22s} {split:22s} {cost:6s} {universe:14s} "
                          f"trades={stats['closed_trades']:3d} wins={stats['wins']:3d} "
                          f"PF={pf} PnL=${stats['net_pnl_usdt']:8.2f} "
                          f"DD={stats['max_drawdown_pct']:5.2f}% halt={stats['halt_reason'] or '-'}")
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()

