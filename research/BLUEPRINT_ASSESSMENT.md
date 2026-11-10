# Trading algorithm blueprint assessment

**Reviewed:** 2026-11-10

**Decision:** retain the blueprint as a research candidate; do not switch the active Testnet strategy or timeframe based on this evidence. The live-order path remains disabled by the application.

## What the document proposes

- Use 15-minute candles instead of 1-hour candles.
- Buy when price is above EMA50, RSI(14) was below 40 at least once in the last five closed candles, current RSI is below 65, and MACD(12,26,9) crosses above its signal on the latest closed candle.
- Exit a long when the inverse trend/RSI/MACD setup occurs.
- Set a stop at the nearer of the prior three-candle swing low or 1.5% below entry, and target 2R.
- Permit only one open trade per asset.

## Feasibility and adaptations

The state-plus-trigger concept is coherent as a testable long-only setup: EMA50 supplies direction, RSI provides a recent pullback condition, and MACD supplies a fresh re-entry trigger. The RSI window avoids requiring three unrelated events on one candle. That is a reasonable research hypothesis, not evidence of an edge.

The document's short-entry rule cannot be used as written. This bot is Binance Spot-only and has no margin/futures shorting; a bearish setup can only signal an exit from an existing long. No short-selling route was added.

The 15-minute interval creates more completed-candle checks than hourly data, but that does not imply four times as many profitable trades. Signals, spread, fees, slippage, stop-outs, and market noise change with the interval. The blueprint gives no 15-minute historical evidence. Its frequency rationale is therefore treated as a hypothesis, not a guarantee.

The swing stop can land very close to entry and may be hit by ordinary noise. A 2R target is a planned price relationship; gaps, stop-market slippage, and fees can reduce the realized reward/risk ratio. The research replay rejects a next-open entry if it has already gapped below the proposed stop. Production stop behavior, ATR sizing, exposure caps, exchange filters, and order protection were not relaxed.

The proposal's “one trade per asset” is compatible with the existing duplicate-position check. Existing portfolio limits, account reconciliation, correlation checks, risk caps, volatility gate, volume gate, and Testnet-only configuration remain relevant. A diagnostic without ADX/volume gates was measured only to show their effect; it is not a deployment recommendation.

## Historical replay

The candidate was replayed on Binance Spot public **1-hour** BTC, ETH, and SOL klines from January 2021 through October 2026. Signals use completed candles and enter at the following open. The portfolio simulator retained the bot's sizing, maximum notional, three-position portfolio cap, correlation restriction, stops/targets, and exit logic. The blueprint candidate used the nearer (higher for a long) of the prior-three-candle swing low and 1.5% below the actual next-open entry, plus its 2R target. If the next-open price was already below the swing low, the entry was skipped. Strategy risk halts were disabled for this comparison so a permanent halt would not mask subsequent periods; this is a diagnostic, not the deployed run mode.

Base costs are modeled as 10 bps fee plus 5 bps slippage per side. Stress costs are 20 bps plus 10 bps per side. Base-cost results:

| Rules | 2021–2023 trades / net P&L / PF | 2024–2025 trades / net P&L / PF | 2026 through October trades / net P&L / PF |
| --- | --- | --- | --- |
| Current combined strategy | 197 / -$222 / 0.862 | 153 / -$254 / 0.804 | 52 / -$108 / 0.728 |
| Blueprint + existing ADX, ATR, volume gates | 131 / +$51 / 1.050 | 85 / -$270 / 0.657 | 34 / +$9 / 1.044 |
| Blueprint without ADX/volume (diagnostic) | 174 / -$112 / 0.919 | 117 / -$203 / 0.793 | 42 / -$34 / 0.880 |

All three strategies were negative in all three periods at the higher stress-cost assumption. The gated blueprint was positive only in two base-cost segments, and lost in 2024–2025; those small gains do not pass a robustness screen. Removing the regime/volume checks increased eligible setup counts but produced negative P&L in every base-cost segment and wider losses under stress.

Across all three assets on the same 1-hour history, the current strategy produced 601 eligible BUY signal candles. The blueprint produced 290 with the existing ADX/ATR/volume checks and 385 without those checks. Thus, on the tested interval, this particular setup did **not** increase signal frequency. This comparison does not estimate what it would do on 15-minute candles.

These are exploratory OHLCV simulations, not forecasts. They cannot recreate historical order books, actual account fees, order latency, or exact intrabar fills. The sample also has been used for prior strategy exploration, so it is not an untouched holdout.

## Changes made

- Added a reproducible, research-only implementation and replay at `research/backtest_blueprint.py`.
- Added machine-readable results at `research/results/blueprint_results.json`.
- Kept the candidate out of the application strategy factory and left Railway's active strategy/timeframe untouched.
- Interpreted the bearish mirror as exit-only; no futures, margin, or short-selling support was introduced.

## What is required before any Testnet strategy switch

Obtain equivalent, complete 15-minute data for the configured symbols, then evaluate chronological train/validation/untouched holdout periods, stressed fees and slippage, market regime splits, parameter sensitivity, and execution filters. Compare the exact candidate with the unchanged current strategy and require acceptable out-of-sample expectancy/drawdown before a supervised Testnet forward run. This assessment did not pass that gate.

Reproduce the current 1-hour experiment with:

```powershell
python research\backtest_blueprint.py
```
