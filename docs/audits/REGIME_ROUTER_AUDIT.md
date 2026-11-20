# Regime router audit — 2026-11-15

**Scope.** The `dynamic_regime` profile: a "multiple strategies behind a
market-condition gate" design. It classifies each completed 1h candle into a
regime, requires three identical classes for stability, and routes flat-symbol
entries to `macro_donchian_ema72` in stable uptrends and (previously) to
`downtrend_rsi_recovery_testnet` in stable downtrends. Range, high-volatility,
transition, and invalid-data states route to no entry.

## Evidence (pre-existing, reproduced in-repo)

### 1. The uptrend gate is net-negative

`research/results/dynamic_regime_router_results.json` — paired replay of the
EMA72 candidate with vs. without the three-bar uptrend gate (identical fills,
fees, stops, sizing, halts):

| Split / cost | Unfiltered PF | Routed PF | Unfiltered DD | Routed DD |
|---|---|---|---:|---:|
| 2021–2023 base | 1.575 | 1.291 | 6.44% | 9.06% |
| 2021–2023 stress | 1.362 | 1.126 | 7.53% | 10.20% |
| 2024–2025 base | 1.649 | 1.531 | 4.75% | 5.39% |
| 2024–2025 stress | 1.384 | 1.286 | 5.05% | 6.23% |
| 2026 Jan–Oct base | 1.496 | 1.605 | 2.90% | 2.43% |

Only the already-reused 2026 slice improves. On the two larger, more reliable
splits the gate lowers profit factor **and** raises drawdown. The gate removes
~17–24% of trades but not selectively the losers.

### 2. The downtrend recovery candidate is net-negative

`research/results/downtrend_rsi_recovery_results.json` — the executable recovery
rules (stable three-bar downtrend, RSI14 cross above 40, EMA20/RSI70 exits,
4%-capped swing stop, 2×ATR target, 0.20× risk):

| Period / universe | Base PF | Stress PF |
|---|---:|---:|
| 2021–2023 (3 pairs) | 0.095 | 0.047 |
| 2021–2023 (8 pairs) | 0.156 | 0.069 |
| 2024–2025 (3 pairs) | 0.765 | 0.515 |
| 2024–2025 (8 pairs) | 0.294 | 0.162 |
| 2026 Jan–Oct (3 pairs) | 0.000 | 0.000 |
| 2026 Jan–Oct (8 pairs) | 1.006 | 0.000 |

Profit factor is below 1 in every historical slice at base and stress costs.
The single 8-pair base breakeven reading is not reproducible under stress.

### 3. A range-regime RSI mean-reversion candidate also fails (new research)

`research/backtest_range_rsi_mean_reversion.py` replays a pre-registered range
candidate (stable 3-bar range = ADX14≤18 and ATR14≤3%; RSI14 cross up through 30;
exits on RSI14≥50 or close>EMA20; 4%-capped swing stop; 2×ATR target; 0.50× risk):

| Period | 3-symbol PF (base/stress) | 8-symbol PF (base/stress) |
|---|---:|---:|
| 2021–2023 | 1.862 / 1.209 | 0.447 / 0.332 |
| 2024–2025 | 2.447 / 1.338 | 1.317 / 0.863 |
| 2026 Jan–Oct | 0.000 / 0.000 | 0.923 / 0.612 |

It is positive only on the small three-symbol universe in train/validation,
net-negative on the broader eight-symbol universe, and zero-for-two in the 2026
review — with only 2–22 trades per split, so the positive readings are not
statistically meaningful. It does not clear the cost-adjusted screen.

## Issues found and fixed

### P0 — the unvalidated downtrend route placed orders and could halt the good route

The policy had `downtrend.entry_enabled = True`, so `dynamic_regime` submitted
real Testnet orders on a strategy with PF < 1 in every slice. Its losing runs
also feed the **portfolio-wide** consecutive-loss halt, which then blocks the
better EMA72 uptrend candidate too. A proven-losing experiment could shut down
the profitable route.

**Fix:** the downtrend route is now **entry-disabled by default**. It is
available only via the explicit opt-in
`ENABLE_DOWNTREND_RECOVERY_TESTNET=true` (validated to require
`STRATEGY=dynamic_regime`), for low-risk Testnet execution-evidence collection
only. The classifier and dashboard diagnostics are unchanged.

### P1 — policy and reason strings did not cite the negative evidence

The downtrend route's reason claimed it "may evaluate" without noting the PF < 1
history. Fixed: disabled-state reason now cites the PF evidence and the opt-in
flag. `entry_authorized` remains a fail-closed, always-false informational flag
(now documented); the router never authorizes an order — `entry_route_enabled`
only permits strategy evaluation.

## What was deliberately left unchanged

- The three-bar uptrend gate remains in `dynamic_regime` as a documented
  diagnostic. The default profile is `macro_donchian_ema72` (ungated), which is
  the evidence-preferred entry path. The router stays fail-closed and order-free.
- The classifier thresholds (ADX 25 up/down, ADX 18 range, ATR 3%, EMA200 slope
  24 bars) are fixed operational definitions, not fitted values, and are
  reproduced identically in production (`regime_router.py`) and research
  (`backtest_dynamic_regime_router.py`).

## Conclusion

Market-condition gating with these rules does not add value: it is net-negative
on the larger splits, and its downtrend constituent has no edge. A new
range-regime RSI mean-reversion candidate was researched and also failed. The
router is kept as a transparent, order-free diagnostic; entries flow only through
the candidate with the best cost-adjusted evidence (`macro_donchian_ema72`), and
the proven-negative downtrend route is gated off by default.
