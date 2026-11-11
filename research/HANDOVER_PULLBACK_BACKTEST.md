# AI handover pullback strategy: backtest

**Scope:** Research-only evaluation of the rules in `AI_AGENT_STRATEGY_HANDOVER.md` v2. No production strategy, config, or exchange behavior was changed.

## Finding

**Reject this candidate for Testnet order execution.** It produced many more trades than the current strategy, but lost money in every tested historical period and under both cost scenarios. The higher frequency did not create an edge.

The literal signal with existing risk controls produced 3,730 closed trades, a 0.697 profit factor, a $6,381 loss on a simulated $10,000, and 64.08% maximum drawdown at base costs. Under stress costs it lost $8,760 with 87.67% drawdown.

The proposed ATR stop/target/trailing version produced 3,767 trades, a 0.681 profit factor, a $6,617 loss, and 67.00% drawdown at base costs. Under stress costs it lost $8,846 with 88.72% drawdown. Applying the intended half-size neutral-regime multiplier reduced the base-cost loss to $6,067, but profit factor remained 0.682 and drawdown 61.57%.

| Variant | Cost case | Trades | Win rate | Profit factor | Net P&L | Max drawdown |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Handover signals, existing risk | Base | 3,730 | 33.30% | 0.697 | -$6,381 | 64.08% |
| Handover signals, existing risk | Stress | 3,723 | 28.87% | 0.498 | -$8,760 | 87.67% |
| Handover ATR stops, existing sizing | Base | 3,767 | 26.71% | 0.681 | -$6,617 | 67.00% |
| Handover ATR stops, existing sizing | Stress | 3,769 | 23.83% | 0.497 | -$8,846 | 88.72% |
| Handover ATR stops, half-size neutral | Base | 3,767 | 26.71% | 0.682 | -$6,067 | 61.57% |
| Handover ATR stops, half-size neutral | Stress | 3,769 | 23.83% | 0.498 | -$8,417 | 84.52% |

The ATR-stop variant was negative in each chronological slice at base costs:

| Period | Trades | Net P&L | Profit factor | Max drawdown |
| --- | ---: | ---: | ---: | ---: |
| Feb 2021–2023 | 1,861 | -$3,360 | 0.749 | 35.71% |
| 2024–2025 | 1,401 | -$3,857 | 0.579 | 38.62% |
| Jan–Oct 2026* | 445 | -$1,341 | 0.532 | 14.31% |

\*The 2026 data was already used in earlier project research, so it is not a clean holdout.

The same ATR-stop variant was also negative in every period at stress costs:

| Period | Trades | Net P&L | Profit factor | Max drawdown |
| --- | ---: | ---: | ---: | ---: |
| Feb 2021–2023 | 1,867 | -$6,172 | 0.538 | 62.26% |
| 2024–2025 | 1,401 | -$5,775 | 0.401 | 57.77% |
| Jan–Oct 2026* | 442 | -$2,378 | 0.315 | 24.05% |

## Method

- Used Binance Spot 1-hour OHLCV for BTC, ETH, and SOL from January 2021 through October 2026.
- Implemented the handover’s literal regime and entry/exit pseudocode: ADX(14) threshold 22; EMA50 slope over four bars; EMA20 pullback condition or RSI(2) below 15; close above EMA20; exit below EMA50 or RSI(14) above 80. BUY precedence follows the supplied code.
- Refused signals unless the preceding 150 hourly candles were contiguous, matching the production data guard. Signals are calculated after candle close and enter at the next hourly open.
- Compared existing risk controls with the proposed 1.5×ATR stop clamped to 0.5–4.5%, 3×ATR target, and a 2×ATR trail. Since the handover does not specify when to activate its new trail, the simulation retains the current 1.5% break-even trigger and then uses the proposed ATR trail.
- Used the shared portfolio simulator: $10,000 initial equity, 1% risk per trade, 10% notional cap, three open positions, 0.85 pairwise correlation cap, stop-first same-bar resolution, and the existing daily/rolling drawdown rules. Main comparisons disable persistent halts to expose strategy results; a second run enables the configured halts.
- Base modeled costs are 10 bps fee and 5 bps slippage per side; stress costs are 20 bps and 10 bps per side. Historical OHLCV cannot model Testnet order-book spreads or actual fills.
- No parameter search was run. The neutral regime returns a 0.5 size multiplier in the handover, but its strategy pseudocode never applies that multiplier. It is therefore shown as a separate sizing sensitivity, not silently assumed.

## Important result: the halt masks the loss

With configured three-consecutive-loss halts enabled, the base-cost runs appeared slightly profitable only because they stopped extremely early: the existing-risk version halted after 12 trades; the ATR version halted after 6. Under stress costs, the existing-risk variant lost $135 after 30 trades; the ATR variant showed a misleading $176 gain after only 6 trades. These halted samples are not evidence of a viable strategy. Without the halt, the candidate lost heavily across the full period and all chronological slices.

The literal v2 entry condition generated 13,743 per-candle BUY signals across the three pairs; these are signal bars, not unique trade setups. The simulator closed roughly 3,700 trades over the full sample. This is a high-turnover signal as written.

## Handover issues that affect interpretation

- The proposed 1-hour default is already the repo default.
- The current stop is already ATR-derived and clamped to 1–3%; the take-profit remains a fixed percentage. The proposed stop is not purely volatility-adaptive because it also clamps, and its 3×ATR target is not always a 2:1 reward-to-risk ratio when the stop is clamped.
- The v2 strategy omits the volume confirmation and ATR-entry filter described in the earlier handover. Those were not added to this test.
- The asserted 50–300 bps Testnet spread and the claim that it blocks all organic trades are unverified. Recent market checks had not produced BUY signals to reach the spread preflight, so increasing `MAX_SPREAD_BPS` would not address that observed bottleneck. Keep the limit until actual spread measurements show a need; a wider limit can hide poor execution.
- The probability of three losses in a specific three-trade block is not the probability of eventually halting over a multi-year run. Increasing the halt threshold does not improve expectancy.

## Decision

Do not replace the active strategy with this candidate, do not loosen the spread guard, and do not change risk parameters on the basis of this handover. The candidate fails the handover’s own positive-expectancy requirement. Keep it out of Testnet order execution; only a materially different, predeclared candidate with positive out-of-sample and stress-cost evidence should proceed to shadow observation.

Reproduce with:

```powershell
python research\backtest_handover_pullback.py
```

Machine-readable results: `research/results/handover_pullback_backtest.json`.
