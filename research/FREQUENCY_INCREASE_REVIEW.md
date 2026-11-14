# Trade frequency review

**Date:** 2026-11-14

**Scope:** Test safer ways to increase completed trades without weakening the bot's position sizing, stop model, daily/rolling loss limits, three-loss halt, or portfolio limits. This is a historical simulation review, not a promise of future trade count or profitability.

## What was compared

The replay used Binance Spot one-hour OHLCV from January 2021 through October 2026. Signals use completed candles and next-bar-open fills. It retained the candidate's EMA480 market filter, EMA72 close exit, prior-five-bar swing stop capped at 4%, 1% risk sizing, 10% notional cap, three-position cap, and 0.85 correlation cap. The active risk halts remained on for reported results.

The entry channel was compared at 168, 120, and 72 hours. The router gate was compared at the production three-bar stable-uptrend gate, the same trend gate without the three-bar persistence requirement, and no extra regime gate. Costs were 10 bps fee plus 5 bps slippage per side in the base case, and 20 plus 10 bps per side in the stress case. A second replay compared the current three-coin set with an eight-coin set (BTC, ETH, SOL, BNB, XRP, ADA, DOGE, LINK). All eight data files cover the same archived period; this is still a survivorship-biased fixed universe and not a current liquidity screen.

## Results with current risk halts

| Variant | Train: Feb 2021–Dec 2023 | Validation: 2024–2025 | Review: Jan–Oct 2026 |
|---|---:|---:|---:|
| Current 168h entry + production gate, 3 coins | 5 trades; +$54.78; PF 1.87 | 17; +$298.94; PF 2.49 | 6; -$94.03; PF 0.31 |
| Remove 3-bar persistence, 168h entry, 3 coins | 5; +$68.52; PF 2.09 | 18; +$307.09; PF 2.29 | 8; -$82.93; PF 0.39 |
| 72h entry + single-bar trend gate, 3 coins | 9; -$2.48; PF 0.98 | 3; -$39.43; PF 0.00 | 10; -$88.59; PF 0.49 |
| No extra regime gate, 168h entry, 3 coins | 4; -$121.72; PF 0.00 | 4; -$44.39; PF 0.53 | 8; -$34.27; PF 0.65 |
| Current 168h entry + production gate, 8 coins | 3; -$110.26; PF 0.00 | 5; -$78.56; PF 0.39 | 10; +$9.45; PF 1.06 |

PF is profit factor; PF below 1 means gross losing trades exceeded gross winners in that simulated period. The 2026 review interval has already been examined in prior project work, so it is not an untouched holdout. In the 2026 stress-cost replay, the 8-coin current-gate variant returned -$17.74 (PF 0.89); the 72h single-bar variant returned -$116.25 (PF 0.39). The less restrictive variants also failed on one or both earlier periods. Trade counts are low because the existing three-consecutive-loss safety halt stops new entries for the rest of each simulated period; the halt was not disabled to inflate frequency.

The complete nine-variant entry/gate matrix is in [`results/frequency_variants_results.json`](results/frequency_variants_results.json). Reproduce it with `python research/backtest_frequency_variants.py`. The expanded-universe figures above came from the same simulator and cost/risk settings.

## Decision

No strategy-rule change earned promotion into the executable strategy. Removing a gate or broadening the coin list raised the 2026 count only modestly and did not show a stable improvement across time periods and stressed costs. Shortening the breakout window increased signals but worsened risk-adjusted results. Disabling or loosening loss halts would create more trades by removing a safety control, so it is not an acceptable way to pursue frequency. A separate runtime mismatch was found in the dashboard's 2,500-of-2,501-candle warmup status and fixed: the router now accepts the 2,500 contiguous closed candles that the active strategy requires, while the exchange connector continues to account for the open raw candle. This can remove a false data-readiness block; it does not change the entry strategy or promise additional trades.

Keep the current strategy and risk limits unchanged while gathering valid Testnet observations. If frequency remains too low, the next defensible work is a separately specified strategy family or a point-in-time liquidity-ranked universe, evaluated on fresh out-of-sample data with realistic fills, delisted assets, and the same portfolio/risk simulator. A higher order count alone is not evidence of a better system.

### Follow-up experiment: stable downtrend recovery

The live router was subsequently observed in a stable downtrend on BTC, ETH, and SOL, where the long-only uptrend candidate correctly had no entries. A separate RSI14 recovery-through-40 candidate was implemented for Binance Testnet only, with EMA20/RSI70 signal exits, a four-percent-capped swing stop, a 2×ATR target, one open recovery position maximum, and 0.20× configured risk. The backtest is in `REGIME_ROUTER_REVIEW.md` and `results/downtrend_rsi_recovery_results.json`: all three-pair periods were negative; the eight-pair recent base sample was nearly flat and failed stress costs. This raises the chance of seeing a Testnet order during a downtrend, but it is not a validated edge and must not be enabled with real funds. The 15-minute trend candidate was also tested in `backtest_15m_trend_entry.py`; it did not improve risk-adjusted results.

The Testnet watchlist was then expanded from three to eight liquid USDT pairs (BTC, ETH, SOL, BNB, XRP, ADA, DOGE, LINK) without changing the per-trade risk cap or the three-position portfolio cap. The historical eight-pair review had 14 base-cost entries versus 3 for the three-pair recovery candidate during Jan–Oct 2026, but netted only $0.06 and lost $1.16 under stress costs. RSI threshold sensitivity showed no threshold with robust positive results across periods. After deployment, the worker selected the downtrend recovery route across the expanded markets and completed one XRP Testnet buy with a persisted position and native protective stop. This confirms the order path and adds forward observations; it does not validate a profitable edge.

## Limits

- Historical candle fills cannot recreate queue position, spread changes, market impact, outages, or exact stop execution.
- The fixed asset lists are survivorship-biased; selection does not use historical point-in-time liquidity or exchange listing state.
- A threshold screen of multiple variants incurs selection bias; the best row is exploratory, not a validated edge.
- These results do not estimate trades through the end of November 2026 and are not a live-market forecast.
