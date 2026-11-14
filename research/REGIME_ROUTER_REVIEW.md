# Regime router implementation review

**Date:** 2026-11-13
**State:** Executable, fail-closed Binance Spot Testnet router (`dynamic_regime`); not a live-trading strategy.

## Decision

The predeclared family screen found every tested family net-negative after modeled fees and slippage; profit factors were below 1. The EMA72/Donchian candidate had positive strategy-only historical slices, but exceeded the 5% training drawdown limit and was stopped after few trades by the configured loss controls. Neither establishes a regime-specific edge, and the historical archive is not an untouched holdout.

The active `dynamic_regime` profile selects `macro_donchian_ema72` in a confirmed uptrend. A follow-up added `downtrend_rsi_recovery_testnet` as a Testnet-only experiment in confirmed downtrends, because the live router was observing downtrend on BTC, ETH, and SOL and had no entry candidate. The recovery route is capped at 0.20% of equity risk per trade, allows at most one recovery position, keeps the four-percent-capped swing stop and 2×ATR target, and does not use trailing stops. It has not passed historical stress costs and is not eligible for live trading. Range, high-volatility, transition, invalid-data, and unavailable-data states still generate no new entries. Open positions retain their entry-time exit policy. Existing risk checks, stops, loss halts, market-quality checks, and Testnet-only config restrictions remain authoritative.

## Downtrend recovery experiment

`python research/backtest_downtrend_rsi_recovery.py` reproduces the executable entry and exit rules with the shared portfolio simulator. It uses 1h closed candles, stable three-bar downtrend classification (ADX ≥25, ATR ≤3%, close below falling EMA200), RSI14 recovery across 40, EMA20/RSI70 exits, next-bar fills, a prior-five-bar swing low with a 4% maximum stop distance, a 2×ATR target, 0.20× normal risk sizing, current loss halts, and base/stress costs. It screens both the current three-pair universe and an eight-pair historical universe.

| Period | Universe | Base trades / net P&L / PF | Stress trades / net P&L / PF |
|---|---|---:|---:|
| 2021–2023 | 3 pairs | 4 / −$12.84 / 0.095 | 4 / −$15.20 / 0.047 |
| 2021–2023 | 8 pairs | 5 / −$12.87 / 0.156 | 5 / −$15.82 / 0.069 |
| 2024–2025 | 3 pairs | 17 / −$5.33 / 0.765 | 12 / −$9.83 / 0.515 |
| 2024–2025 | 8 pairs | 8 / −$11.98 / 0.294 | 8 / −$16.58 / 0.162 |
| Jan–Oct 2026 | 3 pairs | 3 / −$6.36 / 0.000 | 3 / −$8.13 / 0.000 |
| Jan–Oct 2026 | 8 pairs | 14 / +$0.06 / 1.006 | 3 / −$1.16 / 0.000 |

The recent eight-pair base result is approximately breakeven and its stress replay loses; older periods lose at both cost levels. It is not a profitable edge. This route exists only to gather low-risk Testnet order-path and forward evidence in a regime where the earlier router had no candidate. Do not copy this route to a live profile. Its exact state and selected candidate are included in sanitized dashboard market-check logs.

## Paired router replay

`python research/backtest_dynamic_regime_router.py` compares the candidate with no regime entry filter against the same candidate gated by the router's three-bar uptrend rule. Both variants use the same next-open fills, fees/slippage, candidate stop and target, sizing, portfolio constraints, and existing-loss-halt mode. The gate suppresses signals only while flat; SELL signals remain available to manage existing positions.

| Period / costs | No-halt trades | Routed trades | No-halt PF | Routed PF | No-halt P&L | Routed P&L | No-halt DD | Routed DD |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2021–2023 base | 116 | 96 | 1.575 | 1.291 | $1,108.79 | $516.34 | 6.438% | 9.061% |
| 2021–2023 stress | 116 | 96 | 1.362 | 1.126 | $760.68 | $243.68 | 7.529% | 10.195% |
| 2024–2025 base | 171 | 134 | 1.649 | 1.531 | $1,541.84 | $995.95 | 4.748% | 5.385% |
| 2024–2025 stress | 171 | 134 | 1.384 | 1.286 | $1,004.08 | $592.87 | 5.047% | 6.225% |
| 2026 Jan–Oct base | 42 | 32 | 1.496 | 1.605 | $244.97 | $239.45 | 2.898% | 2.428% |
| 2026 Jan–Oct stress | 42 | 32 | 1.214 | 1.318 | $123.01 | $145.43 | 3.587% | 2.812% |

These strategy-only results show that the current regime gate reduces activity and lowers returns while increasing drawdown in the 2021–2023 and 2024–2025 splits; the already-reviewed 2026 split is marginally better on PF/DD, with nearly unchanged base P&L. Under current halts the sample outcomes vary sharply by split (for example, validation base goes from 4 closed trades / −$44 to 17 / +$299, while the 2026 base goes from 8 / −$34 to 6 / −$94). This is unstable and not evidence the router improves expected returns. The router is being activated on Testnet to implement the requested guarded architecture and gather prospective behavior, not because this replay demonstrates an edge. Do not describe this switch as a performance improvement or a live-readiness pass.

## Classification rules

- Input must contain at least 2,500 valid, contiguous, closed 1-hour OHLCV candles. The exchange connector requests one extra raw row internally before excluding the still-open bar. Invalid values, duplicates, gaps, or incomplete indicator warmup fail closed to `uncertain`.
- High volatility: ATR(14) greater than 3% of close.
- Uptrend: ADX(14) at least 25, close above EMA(200), and EMA(200) rising over 24 bars.
- Downtrend: ADX(14) at least 25, close below EMA(200), and EMA(200) falling over 24 bars.
- Range: ADX(14) at most 18 and ATR(14) no greater than 3%.
- Other conditions are transitions/uncertain. A stable label requires the same raw classification on three consecutive completed hourly candles, avoiding immediate strategy flips on one bar.

These simple rules are deliberately transparent and fixed rather than fitted to historical outcomes. The thresholds are operational definitions, not empirically proven optimal levels. They are used by the executable Testnet router only for entry eligibility; they do not override open-position exits. Classification can lag real turning points and can mislabel noisy markets.

## Research basis and limitations

Regime-based investment models are an established research approach, but regime states are not directly observable and false switches can add trades and transaction costs. Blanchard and Goffard (2025) explicitly discuss this false-signal/cost tradeoff and the use of smoothing to improve regime persistence: [article](https://onlinelibrary.wiley.com/doi/10.1002/asmb.70058).

Trying many strategies or thresholds and retaining the best historical result creates selection bias. Bailey et al. describe the probability-of-backtest-overfitting problem: [paper](https://escholarship.org/uc/item/4w1110bb). The project's existing strategy-family screen also found that more activity from breakout alternatives came with worse net results and drawdown. Freqtrade's official documentation likewise warns that OHLCV backtests cannot know intrabar price ordering and recommends forward dry-run comparison: [backtesting limitations](https://www.freqtrade.io/en/stable/backtesting/), [lookahead analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/).

## Gates before adding another strategy or considering live orders

1. Freeze regime definitions and strategy versions before the next evaluation; do not tune against the same reviewed archive.
2. Collect a prospective shadow sample with per-candle regime, proposed strategy, exact signal, and hypothetical next-bar entry/exit. Keep active decisions, positions, exits, and risk limits unchanged.
3. Compare the router to every constituent strategy and the active baseline on identical data, account resets, fees, slippage, fill rules, exposure limits, and risk halts. Include a predeclared untouched time period and multiple-testing controls.
4. Require positive cost-adjusted out-of-sample expectancy with uncertainty bounds, stable results across assets and regimes, drawdown within existing limits, no data leakage, and enough independent trades to make those bounds informative. If no strategy qualifies for a regime, route to no new entry.
5. The current route is restricted to Binance Spot Testnet. Before adding a second executable route, require independent out-of-sample validation for that strategy in its assigned regime, with the same costs, risk controls, portfolio simulator, and prospective shadow evidence. Existing positions must retain the exit policy chosen at entry; the router selects entries for flat symbols only. Risk manager limits remain dominant and cannot be bypassed.

Even a successful historical and Testnet evaluation would not prove future profitability or make real-money deployment risk-free.
