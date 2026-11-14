# Regime router implementation review

**Date:** 2026-11-13
**State:** Executable, fail-closed Binance Spot Testnet router (`dynamic_regime`); not a live-trading strategy.

## Decision

The predeclared family screen found every tested family net-negative after modeled fees and slippage; profit factors were below 1. The EMA72/Donchian candidate had positive strategy-only historical slices, but exceeded the 5% training drawdown limit and was stopped after few trades by the configured loss controls. Neither establishes a regime-specific edge, and the historical archive is not an untouched holdout.

The active `dynamic_regime` profile now uses the stable hourly classifier to select `macro_donchian_ema72` for entries only in a confirmed uptrend. In a range, downtrend, high-volatility, transition, invalid-data, or unavailable-data state it generates no new entries. Open positions continue using their entry-time exit policy regardless of regime changes. Existing risk checks, stops, loss halts, market-quality checks, and Testnet-only config restrictions remain authoritative. This is an executable dynamic router, but currently only one strategy candidate has a route; alternative regimes remain cash until another strategy passes prospective and cost-adjusted evaluation.

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
