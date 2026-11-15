# Downtrend / oversold mean-reversion sweep — 2026-11-15

Question: can a gated long-only Spot strategy add trades in downtrends while
keeping a profit factor above 1? Swept through the project's own hourly
portfolio simulator (same fills, costs, caps, and halts as every candidate).

Reproduce:

```
python research/backtest_downtrend_variants.py review   # fast shortlist
python research/backtest_downtrend_variants.py          # full train/val/review
```

## Candidates (all long-only Spot)

| Variant | Trigger |
|---|---|
| rsi40_cross | baseline (existing audited experiment): stable downtrend + RSI14 crosses 40 |
| rsi30_cross / rsi25_cross | stable downtrend + RSI14 crosses 30 / 25 |
| rsi30_confirm / rsi25_confirm | RSI14 crosses 30 / 25 + close > prior bar high |
| rsi30_regime_confirm | stable downtrend + RSI14 crosses 30 + confirmation |
| deep15_confirm / deep10_confirm | close ≥15% / ≥10% below EMA200 + RSI14 crosses 35 + confirmation |
| bb_confirm | close below lower Bollinger(20, 2.2) + RSI < 40 + confirmation |

All share: EMA20 or RSI exit, prior-five-bar swing stop capped at 4%, 2x ATR
target, 0.20-0.25x base risk budget.

## Results (8-symbol universe, base cost, halts enabled)

| Variant | Train 21-23 | Validation 24-25 | Review 2026 | Verdict |
|---|---|---|---|---|
| rsi40_cross | PF 0.16 (5 tr) | PF 0.29 (8 tr) | PF 1.01 (14 tr) | fails |
| rsi30_cross | PF 0.00 (3 tr) | PF 1.48 (16 tr) | PF 0.72 (9 tr) | fails |
| rsi25_cross | PF 0.00 (3 tr) | PF 1.26 (12 tr) | PF 1.55 (7 tr) | fails train |
| rsi25_confirm | PF 1.20 (21 tr) | PF 0.91 (11 tr) | PF 0.66 (7 tr) | fails val+review |

## Conclusion

**No long-only downtrend/oversold dip-buyer passes the profit-factor screen in
all three periods.** The deeper RSI(25) trigger is the best recent setting
(validation PF 1.26 vs 0.29, review PF 1.55 vs 1.01) but at the cost of fewer
entries — a quality/quantity tradeoff, not a free lunch. "Thriving in a
downtrend" with a long-only Spot bot is not achievable from this data; that
would require short-side execution (futures/margin), which this bot cannot do.

The portfolio-wide 3-consecutive-loss halt is the other half of the story: any
experimental downtrend losses halt the *better* EMA72 uptrend route too
(see REGIME_ROUTER_REVIEW.md). A route-scoped loss budget would be the next
lever to raise trade count without sacrificing the uptrend route's PF.

## Change shipped

`DowntrendRsiRecoveryTestnetStrategy` now takes a configurable
`rsi_recovery_level` (default **25**, was hard-coded 40), wired through
`DOWNTREND_RSI_RECOVERY_LEVEL` (valid 10-45) in config and `bot_engine`. The
route remains entry-disabled by default (`ENABLE_DOWNTEND_RECOVERY_TESTNET`,
`STRATEGY=dynamic_regime`). The original 40 threshold is recoverable via the
env var.
