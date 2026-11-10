# Loss-reduction follow-up

**Reviewed:** 2026-11-10

**Change:** raise the default minimum ADX entry gate from 18 to 20 on the Testnet branch. This keeps the 2-of-3 entry rule, candle interval, EMA50 gate, ATR ceiling, volume/confidence checks, position sizing, stops, targets, and loss halts unchanged. It is a conservative filter test; it does not establish profitability or live readiness.

## Why this change

I compared three small, predeclared filters on 1-hour Binance Spot BTC, ETH, and SOL data from January 2021 through October 2026: ADX ≥20, require EMA50 to rise over three completed candles, and require ATR ≥0.30%. The base scenario models 10 bps fee plus 5 bps slippage per side; stress doubles both. The simulator retains the current position sizing, notional cap, position count, correlation limit, stop/target model, and entry/exit behavior.

ADX ≥20 was the only candidate that reduced losing-trade count and segment drawdown consistently in the recent validation windows while also reducing total historical loss. It rejects some entries that passed ADX18 in weaker trend conditions.

## Results

The full-period comparisons disable permanent halts only to compare the rule set across all dates. The deployed consecutive-loss, daily-loss, and rolling-drawdown halts are shown separately.

| Rule set | Closed trades | Wins / losses | Base P&L | Base PF | Base drawdown | Stress P&L | Stress drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ADX ≥18 | 396 | 157 / 239 | -$521 | 0.840 | 7.39% | -$1,510 | 16.62% |
| ADX ≥20 | 287 | 118 / 169 | -$381 | 0.839 | 6.08% | -$1,172 | 13.06% |

| Segment | ADX18 P&L / losses / PF | ADX20 P&L / losses / PF |
| --- | --- | --- |
| 2021–2023 | -$156 / 110 / 0.899 | -$188 / 84 / 0.847 |
| 2024–2025 | -$256 / 91 / 0.804 | -$137 / 60 / 0.843 |
| 2026 through October | -$108 / 38 / 0.728 | -$56 / 25 / 0.792 |

At stress costs the ADX20 rule lost less in all three segments. At base costs it performed worse in 2021–2023, then better in 2024–2025 and 2026. **Both rules lost money in every segment and have profit factors below 1.** ADX20 reduced the total losing-trade count by 70 in the full sample, but the win/loss ratio was not transformed into a profitable strategy. The three-loss halt still activates in the replay; changing the gate does not remove the need for operator review.

The historical sample has been used in earlier strategy research. It is not a fresh holdout, and selecting ADX20 after comparing alternatives introduces selection bias. The replay also has rare disagreements with the production strategy at borderline signals: 448 of 450 deterministic 1-hour samples matched at ADX18 and 449 of 450 matched at ADX20. These isolated discrepancies are due to indicator initialization on a rolling 100-candle production window versus vectorized full-history indicator calculations; they do not make the result exact. The strategy still needs forward Testnet observation.

## November 2026 trade-count estimate

Assuming the same 1-hour interval, the same three symbols, uninterrupted operation, and no risk halt, ADX20 completed 36 trades from January through October 2026 (3.6 per month). A simple daily-rate projection for November 11–December 1 is **about 2.4 completed trades**.

For the same 20-day calendar window in the five preceding Novembers, ADX20 produced 4, 3, 3, 0, and 1 completed trades: mean 2.2, median 3, range 0–4. So the useful planning estimate is **roughly 2 trades, with 0–4 a plausible historical range**. This is an estimate, not a guarantee. With ADX18, the corresponding historical mean was 3.4, median 3, and range 2–6. Raising the threshold trades less often by design.

Latest Railway runtime logs on November 10 showed normal cycles, a $10,000 Testnet balance, and 0/3 open positions. They do not expose enough strategy detail to prove why each signal was held, and the deployment has only about one day of recent runtime in these logs. The estimate therefore comes from historical counts, not the current dashboard alone.

## Release limit

ADX20 is applied only as a Testnet evaluation default. Live exchange mode remains blocked by the application. Do **not** treat this change as permission to trade real money: the tested strategy remains historically unprofitable. Keep risk limits and the manual risk-halt review in place, and collect forward Testnet results before considering any further change.

Reproduce:

```powershell
python research\backtest_loss_reduction.py
python research\estimate_month_end.py
```

Machine-readable results: `research/results/loss_reduction_results.json` and `research/results/month_end_trade_estimate.json`.
