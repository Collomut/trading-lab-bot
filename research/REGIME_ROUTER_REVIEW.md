# Regime router implementation review

**Date:** 2026-11-13
**State:** Read-only Testnet research observer; not an executable strategy router.

## Decision

Market-regime routing is technically feasible, but the available project evidence does not justify allowing it to select order-producing strategies. The existing predeclared family screen found every tested family net-negative after modeled fees and slippage; profit factors were below 1. The active EMA72/Donchian candidate had positive strategy-only historical slices in a separate replay, but exceeded the 5% training drawdown limit and was stopped after few trades by the configured loss controls. Neither result validates a regime-specific edge. The whole historical archive was already used in prior research, so the later slices are not untouched holdouts.

The implemented observer therefore does **not** switch the active strategy, submit orders, alter position exits, or alter risk controls. It only labels hourly market conditions beside the existing strategy decision. For uptrends it can identify the already-configured `macro_donchian_ema72` candidate for observation. For ranges, downtrends, elevated volatility, and transitions it selects no strategy because no tested alternative has passed the project's evidence screen.

## Classification rules

- Input must contain at least 2,501 valid, contiguous, closed 1-hour OHLCV candles. Invalid values, duplicates, gaps, or incomplete indicator warmup fail closed to `uncertain`.
- High volatility: ATR(14) greater than 3% of close.
- Uptrend: ADX(14) at least 25, close above EMA(200), and EMA(200) rising over 24 bars.
- Downtrend: ADX(14) at least 25, close below EMA(200), and EMA(200) falling over 24 bars.
- Range: ADX(14) at most 18 and ATR(14) no greater than 3%.
- Other conditions are transitions/uncertain. A stable label requires the same raw classification on three consecutive completed hourly candles, avoiding immediate strategy flips on one bar.

These simple rules are deliberately transparent and fixed rather than fitted to historical outcomes. The thresholds are operational definitions, not empirically proven optimal levels. Classification can lag real turning points and can mislabel noisy markets.

## Research basis and limitations

Regime-based investment models are an established research approach, but regime states are not directly observable and false switches can add trades and transaction costs. Blanchard and Goffard (2025) explicitly discuss this false-signal/cost tradeoff and the use of smoothing to improve regime persistence: [article](https://onlinelibrary.wiley.com/doi/10.1002/asmb.70058).

Trying many strategies or thresholds and retaining the best historical result creates selection bias. Bailey et al. describe the probability-of-backtest-overfitting problem: [paper](https://escholarship.org/uc/item/4w1110bb). The project's existing strategy-family screen also found that more activity from breakout alternatives came with worse net results and drawdown. Freqtrade's official documentation likewise warns that OHLCV backtests cannot know intrabar price ordering and recommends forward dry-run comparison: [backtesting limitations](https://www.freqtrade.io/en/stable/backtesting/), [lookahead analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/).

## Promotion gates before any executable routing

1. Freeze regime definitions and strategy versions before the next evaluation; do not tune against the same reviewed archive.
2. Collect a prospective shadow sample with per-candle regime, proposed strategy, exact signal, and hypothetical next-bar entry/exit. Keep active decisions, positions, exits, and risk limits unchanged.
3. Compare the router to every constituent strategy and the active baseline on identical data, account resets, fees, slippage, fill rules, exposure limits, and risk halts. Include a predeclared untouched time period and multiple-testing controls.
4. Require positive cost-adjusted out-of-sample expectancy with uncertainty bounds, stable results across assets and regimes, drawdown within existing limits, no data leakage, and enough independent trades to make those bounds informative. If no strategy qualifies for a regime, route to no new entry.
5. Only after independent review, run a separately gated Testnet order-path trial. Existing positions must retain the exit policy chosen at entry; a router may select entries for flat symbols only. Risk manager limits remain dominant and cannot be bypassed.

Even a successful historical and Testnet evaluation would not prove future profitability or make real-money deployment risk-free.
