# Strategy handover review — 2026-11-14

## Decision

Do not add the handover's pullback or ATR-stop variants to the order path, do not raise the loss-streak limit, and do not loosen the active regime gate to force more trades. The exact handover-style pullback replay lost money in each chronological period and produced severe drawdown. The active EMA72/Donchian candidate is still a Testnet research candidate, not a validated live strategy.

## Reproduction

Run `python research/backtest_handover_pullback.py` with the pinned application dependencies and the local Binance hourly archive under `research/data/`. The script evaluates completed 1-hour candles, checks contiguous input, enters at the next open through the shared portfolio simulator, and reports base costs (10 bps fee + 5 bps slippage per side) and stress costs (20 + 10 bps per side). The whole-sample runs disable persistent halts to measure the signal/exit variants over the same full archive; separate configured-halt runs are also emitted in `research/results/handover_pullback_backtest.json`.

### Full-period variants

| Variant | Costs | Closed trades | Profit factor | Net P&L on $10,000 | Max drawdown |
| --- | --- | ---: | ---: | ---: | ---: |
| Handover signals with existing exits | Base | 3,730 | 0.697 | −$6,381 | 64.08% |
| Handover signals with proposed ATR exits | Base | 3,767 | 0.681 | −$6,617 | 67.00% |
| ATR exits and half-size neutral regime | Base | 3,767 | 0.682 | −$6,067 | 61.57% |
| Handover signals with existing exits | Stress | 3,723 | 0.498 | −$8,760 | 87.67% |

All three base-cost variants also lost in the 2021–2023, 2024–2025, and previously reviewed January–October 2026 slices. The 2026 data has been used in project research and is not a fresh holdout. Smaller neutral-regime sizing reduced losses somewhat but did not create an edge. These results reject this specification; they do not establish that every pullback strategy fails.

## Existing strategy family and route comparison

The independently rerun family screen found these full-history results at base costs:

| Candidate | Closed trades | Profit factor | Net P&L | Max drawdown |
| --- | ---: | ---: | ---: | ---: |
| Legacy 2-of-3 EMA/RSI/MACD | 287 | 0.839 | −$381 | 6.08% |
| Donchian 55 / EMA200 | 1,083 | 0.690 | −$2,756 | 29.01% |
| Volume breakout / EMA100 | 1,088 | 0.731 | −$2,342 | 26.04% |
| Uptrend Bollinger/RSI reversion | 244 | 0.464 | −$1,096 | 11.22% |
| Weekly relative momentum | 229 | 0.765 | −$468 | 4.68% |

More signals therefore did not mean more profitable trades in the tested alternatives. The active EMA72/Donchian candidate had positive no-halt PF in the reviewed periods, but failed its drawdown/frequency screen and frequently stopped after the existing three-loss control. A paired replay also found that the current three-bar regime filter reduced trades from 116 to 96 in 2021–2023 and from 171 to 134 in 2024–2025; it improved PF only in the already-reviewed 2026 slice. The evidence does not justify automatically removing that gate.

The live `dynamic_regime` implementation is a regime selector with **one** Testnet candidate route: EMA72/Donchian in a stable uptrend. It is not currently a portfolio of multiple executable strategies. Range, downtrend, high-volatility, transition, and invalid-data states route to no new entries. That is intentional because no candidate for those regimes has passed the cost-adjusted review. Each future strategy must have its own entry/exit policy, regime assignment, and independent validation; correlated indicators should not be counted as independent votes. Trying many variants and selecting the historical winner raises selection bias and backtest-overfitting risk ([Bailey et al.](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)).

## Changes made

- Converted the router's implicit branch logic into an explicit regime-to-strategy allowlist. Only the uptrend candidate can be selected; all other regimes remain fail-closed.
- Added tests that assert there is exactly one enabled route and that range/high-volatility remain blocked.
- Corrected the research README so it accurately describes the executable Testnet router rather than calling it shadow-only.
- Preserved the existing risk limits. In particular, the evidence does not support raising the three-loss halt or routing the tested high-frequency variants to orders.

## Readiness

The application remains Testnet-only and live order placement is blocked by configuration validation. The router and system tests passing show that the guards behave as coded; they do not establish profitable expectancy. Do not move to real funds based on this audit. Continue Testnet/shadow observation and require a frozen strategy specification plus positive, reproducible, cost-adjusted evidence from genuinely new forward data before considering any live-order path.
