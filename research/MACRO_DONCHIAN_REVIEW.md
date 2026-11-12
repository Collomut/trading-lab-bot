# Independent review: macro Donchian handover

**Candidate:** `macro_donchian_7d_20d_v1` from `AI_AGENT_STRATEGY_HANDOVER4.md`  
**Scope:** Independent historical replay only. No production strategy, order, or risk configuration changed.

## Decision

The candidate merits further observation, but it is **not ready to replace the active strategy or enter the order path**. The independent no-halt replay was net positive across all three periods and both cost cases, but results did not match the handover's reported trade counts or returns. It also fails the proposed 6–18 trades/month frequency gate in both validation and the 2026 review period. The 2026 period is not a pristine holdout.

| Period | Costs | Trades | Trades/month | Profit factor | Net P&L on $10,000 | Max drawdown |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 2021-02–2023 | Base | 181 | 5.18 | 1.909 | +$5,702.77 | 9.25% |
| 2021-02–2023 | Stress | 181 | 5.18 | 1.755 | +$4,981.90 | 10.15% |
| 2024–2025 | Base | 129 | 5.37 | 1.725 | +$1,889.44 | 4.99% |
| 2024–2025 | Stress | 129 | 5.37 | 1.527 | +$1,481.91 | 5.52% |
| 2026-01–2026-10 | Base | 32 | 3.20 | 1.970 | +$431.47 | 2.71% |
| 2026-01–2026-10 | Stress | 32 | 3.20 | 1.687 | +$339.18 | 3.26% |

The replay passes 10 of 12 encoded gates. The two failures are trade frequency: 5.37/month in validation and 3.20/month in the 2026 period, below the specified minimum of 6. The candidate therefore does not meet its own complete acceptance criteria.

## Comparison with the current ADX20 CombinedStrategy

Both strategies below use the same candles, split boundaries, $10,000 reset account per split, fills, costs, sizing limits, and portfolio simulator. “No halts” isolates signal and exit performance; it does not represent the bot's full operating behavior.

| Period / costs | Current trades | Current PF | Current P&L | Donchian trades | Donchian PF | Donchian P&L |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2021–2023 base, no halts | 143 | 0.824 | -$215 | 181 | 1.909 | +$5,703 |
| 2021–2023 stress, no halts | 142 | 0.595 | -$575 | 181 | 1.755 | +$4,982 |
| 2024–2025 base, no halts | 107 | 0.843 | -$140 | 129 | 1.725 | +$1,889 |
| 2024–2025 stress, no halts | 107 | 0.596 | -$422 | 129 | 1.527 | +$1,482 |
| 2026 review base, no halts | 36 | 0.792 | -$57 | 32 | 1.970 | +$431 |
| 2026 review stress, no halts | 37 | 0.374 | -$246 | 32 | 1.687 | +$339 |

**On this no-halt comparison, Donchian is a clear historical improvement in net results and profit factor.** It generated more trades in train and validation, but fewer in the 2026 review. It still missed the 6-per-month floor in validation (5.37) and 2026 (3.20). Drawdown was higher for Donchian in all three base/stress period pairs.

Under the current 3-loss halt, the picture changes. Validation: current strategy 28 trades / -$50 base versus Donchian 4 / -$80. Holdout: current 9 / -$10 versus Donchian 6 / -$56. Stress-cost comparisons are also negative for both; Donchian lost more in validation and holdout. The strategy-only edge does not currently survive the project's deployed halt behavior in these slices.

## Independent reproduction and discrepancies

The replay uses completed 1-hour candles, an entry above the prior 168-hour high while above EMA480, an exit below the prior 72-hour low, next-open fills, 10 bps fee plus 5 bps slippage per side (stress 20 + 10), the prior five-bar swing low/4% stop floor, 1% equity risk, 10% notional cap, three-position maximum, and the shared 0.85/100-bar correlation screen. Each chronological split starts with its own $10,000 account; these P&Ls must not be added and described as one continuous account return. It disables the legacy 1.5% break-even/trailing stop because that conflicts with the stated channel exit. A 100×ATR14 target is finite; it approximates an uncapped target but is not mathematically unlimited.

Results differ materially from the handover's reported counts (247/168/40 versus 181/129/32) and P&L. The handover's reproduction script and JSON were not present in this repository, so its exact timing, accounting, and portfolio-reset conventions could not be checked. The independent script and output are [backtest_macro_donchian.py](backtest_macro_donchian.py) and [macro_donchian_results.json](results/macro_donchian_results.json).

The handover's gate list is also inconsistent: the displayed JSON contains 12 booleans including `candidate_passes_all_gates`, while the prose claims 11/11. It does not include a holdout-frequency gate, even though the table shows 4 trades/month against the stated 6/month minimum. The payoff illustration has a sign-formatting error: expected value should be written `0.25 × 2.6% + 0.75 × (−2.5%) = −1.225%`.

## Existing risk controls change the outcome

With repository-default 3-loss halts and the shared 3% daily / 5% rolling drawdown checks enabled, the replay becomes negative and stops after very few trades in validation and 2026. Raising the loss-streak limit to 10 does not remove the daily/rolling halts: training stops after five trades, validation after 15, and only the 2026 slice continues. These early-stop results are not comparable to the no-halt strategy results and do not justify raising the deployed loss limit. The handover's headline positive returns use the no-halt comparison; its risk-limit behavior needs a separate, faithful account-level simulation.

| Period | Loss-streak cap | Trades before stop | Base P&L | Halt reason |
| --- | ---: | ---: | ---: | --- |
| Train | 3 | 5 | +$2,662.70 | Daily loss limit |
| Train | 10 | 5 | +$2,662.70 | Daily loss limit |
| Validation | 3 | 4 | -$80.03 | Three consecutive losses |
| Validation | 10 | 15 | +$679.60 | Daily loss limit |
| 2026 review | 3 | 6 | -$56.14 | Three consecutive losses |
| 2026 review | 10 | 32 | +$431.47 | No halt in sample |

The apparent positive halted results arise from very small samples and, for some runs, a few outsized winners before the bot stopped taking new entries. They are not a reliable estimate of ongoing strategy performance.

## Integration blockers

- The active service currently requests 150 candles by default; EMA480 requires at least 480 complete candles, so it needs a 500-candle request and corresponding runtime configuration.
- The live risk manager still creates ATR-clamped 1–3% stops, a fixed 3% take-profit, and a 1.5% trailing stop. Those exits conflict with this candidate's swing-low/4% stop and 72-hour channel exit. Registering only a strategy class would not implement the tested system.
- Changing the loss-streak limit from 3 to 10 weakens a safety control. No evidence here justifies that change, and the configured-halt replay shows that other guards dominate anyway.
- The candidate would first need a read-only forward shadow phase with explicit signal/exit logging, followed by a dedicated Testnet order-path test only if shadow fills, actual costs, and risk behavior match the simulation.

## Method limits

The fixed BTC/ETH/SOL universe is survivorship-biased. The whole history, including 2026, has already been used for other strategy research; this is not an untouched holdout. OHLCV cannot reconstruct spreads, depth, queue position, or exact exchange fills. Risk-halt simulations are hourly approximations, and the no-halt strategy comparison intentionally does not model deployed circuit breakers. None of these results establish future profitability.
