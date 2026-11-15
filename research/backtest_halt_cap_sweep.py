"""Consecutive-loss cap sensitivity for the EMA72 candidate (2026-11-15).

Shows how the deployed 3-loss halt truncates the strategy versus a recoverable
streak. Compares the baseline and the ATR-band-filtered variant (the only tweak
that improved drawdown without hurting PF in the tuning sweep).
"""

from __future__ import annotations

import json

from backtest_ema72_donchian import SPLITS, SYMBOLS
from backtest_strategy import (
    DATA, OUT, FEE_BPS, SLIPPAGE_BPS, STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS,
    simulate, summarize,
)
from backtest_tune_ema72 import build_variant_signals, MONTHS

CAPS = [3, 4, 5, 6, 8]
BASE_KW = dict(initial_stop_model="swing_low_floor_4pct", enable_breakeven_trailing=False,
               take_profit_atr_multiple=100.0)


def main() -> dict:
    frames = {
        "baseline": {s: build_variant_signals(s) for s in SYMBOLS},
        "atr_band_2_5": {s: build_variant_signals(s, max_atr_pct=2.5) for s in SYMBOLS},
    }
    out = {"symbols": list(SYMBOLS), "caps": CAPS, "variants": {}}
    for name, fr in frames.items():
        out["variants"][name] = {}
        for cap in CAPS:
            out["variants"][name][str(cap)] = {}
            for split, (start, end) in SPLITS.items():
                out["variants"][name][str(cap)][split] = {}
                for cost, fee, slip in (("base", FEE_BPS, SLIPPAGE_BPS),
                                        ("stress", STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS)):
                    sim = simulate(fr, fee, slip, enforce_risk_halts=True,
                                   start_timestamp_ms=start, end_timestamp_ms=end,
                                   max_consecutive_losses=cap, **BASE_KW)
                    s = summarize(name, sim)
                    out["variants"][name][str(cap)][split][cost] = {
                        "trades": s["closed_trades"], "win_rate_pct": s["win_rate_pct"],
                        "profit_factor": s["profit_factor"], "net_pnl_usdt": s["net_pnl_usdt"],
                        "max_drawdown_pct": s["max_drawdown_pct"],
                        "trades_per_month": round(s["closed_trades"] / MONTHS[split], 2),
                        "halted": sim["halted"], "halt_reason": sim["halt_reason"],
                    }
            print(f"{name} cap={cap} done", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "ema72_halt_cap_sweep_2026_11_15.json"
    path.write_text(json.dumps(out, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Wrote {path}")
    return out


if __name__ == "__main__":
    r = main()
    print(f"{'variant':<14} {'cap':<4} {'split':<24} {'base PF':>8} {'base P&L':>9} {'str PF':>8} {'trades':>7} {'halt':>20}")
    for name, caps in r["variants"].items():
        for cap, splits in caps.items():
            for split in ("train_2021_02_to_2023", "validation_2024_to_2025", "review_2026_01_to_10"):
                b = splits[split]["base"]; st = splits[split]["stress"]
                print(f"{name:<14} {cap:<4} {split:<24} {str(b['profit_factor']):>8} "
                      f"{str(b['net_pnl_usdt']):>9} {str(st['profit_factor']):>8} "
                      f"{str(b['trades']):>7} {str(b['halt_reason'])[:20]:>20}")
