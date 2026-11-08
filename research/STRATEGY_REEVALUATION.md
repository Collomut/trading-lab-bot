# Trading strategy reassessment

**Repository branch/commit reviewed:** `testnet-prelaunch-safeguards` at `617a05cd`
**Evaluation date:** 2026-11-08
**Market data:** Binance Spot 1-hour klines for BTCUSDT, ETHUSDT, SOLUSDT, 2021-01 through 2026-10 (51,106 bars per pair). Source: [Binance Public Data](https://github.com/binance/binance-public-data).

## Executive finding

The strategy is over-constrained for trade frequency, but relaxing its current filters did not improve the historical results. The current signal logic produced negative net returns in every tested time segment after fees and execution haircuts. **Do not loosen the gates or enable live trading based on the desire to see more trades.** None of the tested alternatives established a trading edge.

A three-day period without an entry is plausible for this hourly strategy. It is not, by itself, proof of a feed failure. The more important issue is that the existing signal rules show weak historical performance and have a persistent risk-halt behavior that can stop entries indefinitely.

## What the running rules actually mean

- EMA produces a vote only after the 9 EMA crosses the 21 EMA within the last two completed candles.
- MACD also needs a fresh crossover within the last two completed candles.
- RSI votes BUY only on a recovery across 40 after being below 40; it votes SELL above 70.
- The combined strategy needs at least two of the three votes in the same direction.
- For a BUY, price must also be at or above the 50-period EMA, ADX must be at least 18, ATR must be at most 3%, volume must pass, and the execution layer applies a 50% confidence floor and order-quality/risk checks.
- On the dashboard's 1-hour timeframe, the trend check is price versus an approximately 50-hour EMA. It checks the price's position relative to that EMA; it does not check the EMA's slope. SELL signals and protective exits are not blocked by this BUY trend gate.

EMA and MACD are both crossover indicators and are not independent confirmations. Requiring them to produce fresh events close together can be restrictive. The code comments also mention a 200 EMA in places, but the actual combined-strategy default is 50. The dashboard's separate “trend blocked” state describes a gate status; it is not necessarily the reason a particular candle returned HOLD.

## Signal funnel across the sample

For the current 2-of-3, two-candle rule, the three pairs had 153,318 pair-candles in total:

| Funnel stage | Count |
|---|---:|
| Candles with at least two BUY votes | 2,075 |
| Rejected by ADX/ATR regime gate | 828 |
| Rejected by EMA direction gate after regime passed | 646 |
| Rejected by volume gate | 0 |
| Eligible BUY signals after these gates | 601 |

These counts overlap in time because a fresh crossover can remain valid across adjacent candles. They show that both regime and EMA direction filters remove many candidate BUY checks. Volume barely filters anything at its current 30%-of-average threshold. In your screenshot, however, all three pairs had zero BUY votes, so those particular HOLDs were caused by no consensus, not an EMA veto.

## Historical simulation

The signal implementation was compared with the repository's actual `CombinedStrategy` on 1,500 sampled candles across the tested 2-of-3 variants; there were no signal mismatches. Entries were filled at the next hourly open to avoid using the signal candle's close as a fill. The base cost assumption was 10 bps fee plus 5 bps slippage per side. Risk sizing used the current 10% position cap, 1% risk setting, ATR-based stop, 3% target, 3-position cap, and 0.85 pairwise-correlation cap. If an hourly bar touched both stop and target, the simulation assumed the stop happened first.

The strategy-only comparison below removes the persistent risk halt to isolate the entry/exit rules. It is a diagnostic comparison, not the bot's exact deployed behavior.

| Rule set | Closed trades | Win rate | Profit factor | Net P&L on $10k | Max drawdown |
|---|---:|---:|---:|---:|---:|
| Current: 2-of-3, two-candle window, all gates | 402 | 39.55% | 0.823 | -$584 (-5.84%) | 7.40% |
| 2-of-3, four-candle window | 794 | 37.03% | 0.764 | -$1,506 (-15.06%) | 16.34% |
| 2-of-3, six-candle window | 1,167 | 36.59% | 0.729 | -$2,442 (-24.42%) | 26.14% |
| Remove EMA direction gate | 943 | 37.33% | 0.687 | -$2,413 (-24.13%) | 26.23% |
| Remove ADX regime gate | 827 | 40.39% | 0.888 | -$727 (-7.27%) | 9.90% |
| One vote only (unsafe frequency diagnostic) | 2,441 | 34.78% | 0.644 | -$5,194 (-51.94%) | 52.58% |

The current rule set was also negative in each isolated period: **-$259 in Feb 2021–2023, -$260 in 2024–2025, and -$113 in Jan–Oct 2026** in these simulations. The ADX-off variant was slightly less negative overall, but still lost money and lost more in 2026; that is not sufficient evidence to remove it.

### Important risk-halt result

When the code's three-consecutive-loss halt is applied, the current-rule simulation stopped opening positions in July 2021 after 28 trades. It then sat out the rest of the historical sample. Its resulting +$151 is therefore a consequence of stopping early, not evidence the strategy remained profitable. The current resume flow only permits clearing an emergency-stop halt after review; it does not provide a clear reviewed recovery path for the consecutive-loss halt. This should be treated as a separate safety/lifecycle defect.

## What the results say about the three-day gap

In the no-permanent-halt baseline simulation, the strategy generated 52 closed trades across the three pairs during January–October 2026, roughly five per month in aggregate. Thus a three-day gap is unsurprising for this rule set. The screenshot's latest checks also showed no indicator consensus. However, the historical edge is negative, so producing more trades by extending crossover windows or removing gates would likely increase losses, not solve the central problem.

Railway's current Supabase-backed deployment only started successfully on 2026-11-08 at 07:56 UTC; earlier migration attempts crashed. Therefore, the migrated deployment itself has not yet accumulated three uninterrupted days of runtime evidence.

## Limitations

This is a research backtest, not proof of future performance. Public hourly OHLCV does not provide historical order-book depth, exact spread, exchange filters, account-specific fees, or the bot's true 60-second stop/trailing-stop path. Costs were modeled with conservative haircuts, and the risk manager's hourly equity checks are approximated. Current-month November bars were excluded because the archive period ends at October. Results are not a promise or forecast.

## Recommendation

1. Keep the bot on Testnet/paper mode. The current strategy and tested relaxations are not supported for live deployment by this evidence.
2. Do not make the cross window longer, disable the EMA gate, or reduce 2-of-3 to 1-of-3 as a way to force activity.
3. Redesign and test a single coherent long-only setup with a clear directional definition and a pullback/re-entry trigger, instead of counting two highly related fresh crossovers as separate confirmation. Keep order-quality and capital-risk checks.
4. Fix the consecutive-loss halt recovery so an operator can review the reason, reconcile the exchange, and explicitly acknowledge/resume when safe; never auto-resume after a risk halt.
5. Compare the redesigned strategy on chronological train/validation/holdout data with the same fees, slippage, stops, position limits, and correlation checks. Require positive out-of-sample expectancy and acceptable drawdown under stress costs, then run Testnet forward observation before considering live use.

No strategy parameters were changed or deployed as part of this analysis.

## Additional candidate test: rising-trend pullback

A research-only EMA20 pullback reclaim with a rising EMA50 was tested after the first report draft. It used the same chronological period splits, next-bar fills, fees, slippage, exits, and portfolio constraints. The ordinary RSI45 version lost **$3,033** in 2021–2023 (PF 0.547), **$2,263** in 2024–2025 (PF 0.503), and **$844** in Jan–Oct 2026 (PF 0.417). Removing RSI entirely produced the same trades and losses. Requiring an RSI recovery through 40 made the candidate nearly inert: 3, 4, and 0 closed trades in those periods. Neither version is suitable to deploy.

This reinforces that the next step is research and forward validation, not another live parameter adjustment. The candidate implementation is retained only as a research script and is not imported by the trading application.

## Operational recovery fix

The operator recovery flow now supports the consecutive-loss halt as well as the existing emergency-stop halt. It requires a fresh clean exchange reconciliation, explicit dashboard confirmation, no open positions, and no unresolved order intents. Acknowledging a consecutive-loss halt resets only the consecutive-loss streak and records an audit event; daily realized loss and daily/rolling equity controls remain in force. Other halt classes cannot be cleared by this path. The dashboard now exposes the review button for this specific recoverable halt and keeps other active halts locked.

Strategy diagnostics now mark whether a BUY consensus exists. Logs show a failed trend/regime/volume gate as an active blocker only when a BUY setup was present; otherwise it is labeled as informational. This avoids interpreting a red gate indicator as the cause of every HOLD.
