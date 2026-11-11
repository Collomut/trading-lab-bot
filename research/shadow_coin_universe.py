"""Read-only historical shadow comparison for a broader Binance Spot universe.

This research script never submits orders and does not modify bot configuration.
It applies the deployed 1h combined rules and ADX 20 gate while holding the
portfolio sizing, position count, and correlation limit constant.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from backtest_strategy import (
    DATA, OUT, FEE_BPS, SLIPPAGE_BPS, STRESS_FEE_BPS,
    STRESS_SLIPPAGE_BPS, compute_signals, simulate, summarize,
    validate_against_actual_strategy,
)

BASELINE = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
# Predeclared liquid, established Binance Spot USDT candidates. This is a
# research cohort only; listing/liquidity must be rechecked before any forward
# shadow integration. Excludes stablecoins and newly listed/speculative tokens.
CANDIDATES = ("BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT")
ADX_MIN = 20.0  # Match the current Testnet default, not the older ADX 18 study.
CUTS = {
    "2021_2023": (int(pd.Timestamp("2021-02-01", tz="UTC").timestamp() * 1000),
                  int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)),
    "2024_2025": (int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000),
                  int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000)),
    "2026_through_october": (int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000),
                              None),
}


def data_quality(symbol: str) -> dict:
    path = DATA / f"{symbol}_1h.csv"
    raw = pd.read_csv(path).drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
    step = raw.timestamp_ms.diff().dropna()
    gaps = step[step.ne(3_600_000)]
    manifest_path = DATA / "manifest_1h.json"
    if not manifest_path.exists():
        manifest_path = DATA / "manifest.json"
    manifest_symbol = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_symbol = next((row for row in manifest.get("archives", [])
                                if row.get("symbol") == symbol), {})
    return {
        "bars": int(len(raw)),
        "first_utc": pd.to_datetime(raw.timestamp_ms.iloc[0], unit="ms", utc=True).isoformat(),
        "last_utc": pd.to_datetime(raw.timestamp_ms.iloc[-1], unit="ms", utc=True).isoformat(),
        "non_hourly_gaps": int(len(gaps)),
        "largest_gap_minutes": int(gaps.max() / 60_000) if len(gaps) else 0,
        "missing_archives": manifest_symbol.get("missing_months", manifest_symbol.get("missing_archive_count", 0)),
        "archive_errors": manifest_symbol.get("errors", []),
    }


def run_variant(name: str, symbols: tuple[str, ...], frames: dict) -> dict:
    selected = {symbol: frames[symbol] for symbol in symbols}
    row = {"symbols": list(symbols), "funnel": {}, "scenarios": {}}
    for symbol, frame in selected.items():
        row["funnel"][symbol] = {
            "consensus_buy_candidates": int(frame.candidate_buy.sum()),
            "eligible_buy_signals": int(frame.signal.eq("BUY").sum()),
            "sell_consensus": int(frame.signal.eq("SELL").sum()),
            "blocked_regime": int(frame.regime_block.sum()),
            "blocked_trend": int(frame.trend_block.sum()),
        }

    for scenario, fee, slippage in (
        ("base", FEE_BPS, SLIPPAGE_BPS),
        ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS),
    ):
        diagnostic = simulate(
            selected, fee, slippage, enforce_risk_halts=False,
            correlation_lookback_bars=100,
        )
        row["scenarios"][scenario] = {
            "without_permanent_halts_diagnostic": summarize(name, diagnostic),
            "segments_without_permanent_halts": {
                label: summarize(name, diagnostic, start, end)
                for label, (start, end) in CUTS.items()
            },
        }
    return row


def main() -> None:
    symbols = BASELINE + CANDIDATES
    quality = {symbol: data_quality(symbol) for symbol in symbols}
    frames = {
        symbol: compute_signals(symbol, interval="1h", min_trend_adx=ADX_MIN)
        for symbol in symbols
    }
    parity = {
        symbol: validate_against_actual_strategy(
            symbol, frames[symbol], sample_count=200, interval="1h",
            min_trend_adx=ADX_MIN,
        )
        for symbol in symbols
    }

    variants = {
        "current_universe": BASELINE,
        "expanded_shadow_universe": symbols,
    }
    results = {
        name: run_variant(name, selected_symbols, frames)
        for name, selected_symbols in variants.items()
    }

    output = {
        "mode": "offline_read_only_shadow_research_no_orders",
        "interval": "1h",
        "baseline": list(BASELINE),
        "candidate_cohort": list(CANDIDATES),
        "deployed_entry_gate": f"ADX >= {ADX_MIN:g}; existing EMA50, ATR, volume, 2-of-3 consensus, confidence gates",
        "risk_limits_held_constant": {
            "risk_per_trade_pct": 1.0,
            "position_notional_cap_pct": 10.0,
            "max_concurrent_positions": 3,
            "max_pairwise_correlation": 0.85,
            "same_stop_target_and_halt_logic": True,
        },
        "costs_bps_each_side": {
            "base": {"fee": FEE_BPS, "slippage": SLIPPAGE_BPS},
            "stress": {"fee": STRESS_FEE_BPS, "slippage": STRESS_SLIPPAGE_BPS},
        },
        "data": quality,
        "signal_parity_vs_actual_strategy": parity,
        "variants": results,
        "limitations": [
            "This is historical simulation, not a forward shadow run or a claim of profitability.",
            "Symbols were selected using present-day liquidity and must be treated as a survivorship-biased cohort.",
            "Historical OHLCV cannot reconstruct historical spread, depth, latency, outages, or exact fills.",
            "The same rule periods and historical data have been used in earlier project research; results are not pristine holdout evidence.",
            "The simulator's correlation and hourly stop/trailing behavior are approximations.",
            "Additional symbols do not raise the 3-position limit or per-trade risk budget in this comparison.",
        ],
    }
    path = OUT / "coin_universe_shadow.json"
    path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "output": str(path),
        "parity_mismatches": {s: len(v["mismatches"]) for s, v in parity.items()},
        "comparison": {
            name: {
                cost: {
                    "trades": item["without_permanent_halts_diagnostic"]["closed_trades"],
                    "net_pnl": item["without_permanent_halts_diagnostic"]["net_pnl_usdt"],
                    "profit_factor": item["without_permanent_halts_diagnostic"]["profit_factor"],
                    "max_drawdown_pct": item["without_permanent_halts_diagnostic"]["max_drawdown_pct"],
                }
                for cost, item in run["scenarios"].items()
            }
            for name, run in results.items()
        },
    }, indent=2))


if __name__ == "__main__":
    main()
