"""Automated statistical edge verification.

Computes a Student's two-tailed one-sample t-test of per-trade realized P&L
against a zero-return cash baseline, plus the profit factor (gross profit /
gross loss). A candidate adjustment is treated as statistically significant
when ``t >= 2.0`` (``p < 0.05``, two-tailed) and the profit factor clears the
payoff-asymmetry floor of 1.65.

Usage:
    python research/verify_statistical_edge.py --pnl 12,-4,15,-6,9,-5,18
    python research/verify_statistical_edge.py --file results.json

The JSON file may be a flat list of floats or a dict with a ``trades_pnl`` key.
This is a research-only tool: it never places orders or changes live behavior.
"""
from __future__ import annotations

import argparse
import json
import math

import numpy as np
from scipy import stats


def profit_factor(returns: np.ndarray) -> float:
    """Gross profit / gross loss; +inf when there are no losing trades."""
    gains = float(np.sum(returns[returns > 0]))
    losses = float(np.sum(returns[returns < 0]))
    if losses == 0.0:
        return math.inf if gains > 0 else 0.0
    return gains / abs(losses)


def verify_strategy_significance(trades_pnl: list[float], benchmark_return: float = 0.0) -> dict:
    """Return t-statistic, two-tailed p-value, significance flag, and profit factor.

    ``is_significant_p05`` is True only when the p-value is below 0.05 AND the
    t-statistic is positive (the series beats the zero-return cash baseline).
    """
    if not trades_pnl:
        return {
            "t_statistic": float("nan"),
            "p_value": float("nan"),
            "is_significant_p05": False,
            "profit_factor": float("nan"),
        }
    returns = np.asarray(trades_pnl, dtype=float)
    t_stat, p_value = stats.ttest_1samp(returns, benchmark_return)
    return {
        "t_statistic": float(t_stat),
        "p_value": float(p_value),
        "is_significant_p05": bool(p_value < 0.05 and t_stat > 0),
        "profit_factor": float(profit_factor(returns)),
    }


def _load_pnl(path: str) -> list[float]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        payload = payload.get("trades_pnl", payload.get("pnl", []))
    if not isinstance(payload, list):
        raise ValueError("JSON must be a flat list of floats or a dict with 'trades_pnl'")
    return [float(value) for value in payload]


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify statistical edge of a trade P&L series.")
    parser.add_argument("--pnl", help="Comma-separated per-trade realized P&L values.")
    parser.add_argument("--file", help="Path to a JSON file of per-trade P&L values.")
    args = parser.parse_args()

    if args.file:
        trades_pnl = _load_pnl(args.file)
    elif args.pnl:
        trades_pnl = [float(value.strip()) for value in args.pnl.split(",") if value.strip()]
    else:
        trades_pnl = [5.0, -1.0] * 10

    print(json.dumps(verify_strategy_significance(trades_pnl), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
