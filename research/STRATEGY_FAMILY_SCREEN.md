# Strategy family screen

**Date:** 2026-11-11
**Purpose:** Search for a defensible alternative to the current Testnet strategy. This is a research comparison, not a promise of profitability or a live-trading recommendation.

## Result

No tested strategy family showed positive net results across the chronological periods. **Nothing from this screen is suitable to replace the active strategy.** The bot remains on Testnet, and live order placement remains disabled.

## Method

- Used Binance Spot 1-hour OHLCV for the same three pairs currently enabled in the bot: BTC/USDT, ETH/USDT, and SOL/USDT, January 2021 through October 2026.
- Compared the current 2-of-3 entry rules against four predeclared alternatives: 55-bar Donchian breakout with a rising EMA200 and ADX/ATR/volume checks; a volume-confirmed 24-hour breakout with a rising EMA200; an uptrend-filtered Bollinger/RSI mean-reversion setup; and weekly top-two 28-day relative momentum.
- No parameter optimization was performed. Signals use completed candles and fill at the next hourly open.
- Reused the project's portfolio simulator: 1% account risk sizing, 10% notional cap per position, three-position cap, correlation screen, ATR stop, 3% target, break-even/trailing behavior, and conservative stop-first treatment when both stop and target are inside a candle.
- Base costs are 10 bps fee and 5 bps slippage on each side. Stress costs double both. Global persistent risk halts are disabled in the comparison so early shutdown does not hide later outcomes; this makes the full-sample comparison more permissive than the deployed risk behavior.
- Chronological slices are 2021–2023, 2024–2025, and January–October 2026. The history has already been examined in earlier project research, so the last slice is **not an untouched holdout**. OHLCV cannot reconstruct historical order books, actual queue position, account fee tier, or exact intrabar execution.

## Full-period results

All results are on a simulated $10,000 account. A profit factor below 1 means gross losses exceeded gross gains.

| Strategy | Trades | Base net P&L | Base profit factor | Base max drawdown | Stress net P&L | Stress profit factor | Stress max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Current 2-of-3, ADX ≥ 20 | 287 | -$381 | 0.839 | 6.08% | -$1,172 | 0.576 | 13.06% |
| Donchian 55 / EMA200 | 1,083 | -$2,756 | 0.690 | 29.01% | -$4,722 | 0.487 | 47.47% |
| Volume-confirmed 24h breakout | 1,034 | -$2,199 | 0.738 | 24.72% | -$4,292 | 0.515 | 43.50% |
| Uptrend Bollinger/RSI reversion | 244 | -$1,096 | 0.464 | 11.22% | -$1,634 | 0.303 | 16.46% |
| Weekly 28-day relative momentum | 229 | -$468 | 0.765 | 4.68% | -$1,029 | 0.555 | 10.29% |

The current-strategy result reproduces the existing ADX20 research baseline. None of the alternatives beat it on net return, and none had a profit factor above 1. The Donchian breakout generated more activity, but with substantially larger losses and drawdown. More trades did not produce an edge.

The current strategy and three alternatives were net-negative in every chronological slice. The 24-hour breakout alternative was negative in 2021–2023 (-$1,220, PF 0.749) and 2024–2025 (-$1,056, PF 0.639), then positive in the already-reviewed January–October 2026 slice (+$78, PF 1.128) while drawing down 24.03%. Across the full period it lost $2,199 at base costs; stress costs increased the loss to $4,292. The recent result is not an untouched holdout and does not establish an edge. More signals came with substantially worse historical drawdown.

## Comparison with other bot projects

Freqtrade and Hummingbot are strategy frameworks, not single strategies with a universal verified return. Freqtrade supports custom user strategies, historical backtests, dry-run, and lookahead-bias analysis; its documentation warns that backtests can be distorted and recommends forward dry-run checks. [Freqtrade strategy quickstart](https://www.freqtrade.io/en/stable/strategy-101/), [lookahead analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/).

Hummingbot's official documentation describes user-configured strategies and distinguishes market-making/arbitrage workflows from general directional signal strategies. A market-making bot cannot be compared to this long-only spot signal bot by brand name or trade count; a fair comparison needs matched venue, pair universe, order types, fees, inventory risk, and verified account-level fills. [Hummingbot strategies](https://hummingbot.org/strategies/), [documentation](https://hummingbot.org/docs/).

There is no verified public return record that makes it safe to copy an advertised bot's strategy. Market-order execution also incurs uncertain slippage and fees; those costs must be measured per account and venue rather than assumed away. [CCXT manual: fees and market-order execution](https://github.com/ccxt/ccxt/wiki/Manual).

## Decision and next step

Do not switch the deployed strategy to any candidate in this screen, and do not loosen gates to force entries. The 24-hour volume-confirmed breakout is now recorded in **read-only shadow mode** for prospective observation because its already-reviewed 2026 segment differed from its earlier history. It remains a weak research hypothesis, not a recommended strategy: the service records its completed-candle signal and market values beside the active decision, and does not simulate fills or route orders. Any later forward evaluation must reconstruct next-bar entries and exits, include actual account fees and conservative slippage, cover different regimes, and account for drawdown. It must pass that evidence check before any Testnet order-path experiment; a historical or forward pass would still not guarantee future profit.

Reproduce the screen with:

```powershell
python research\backtest_strategy_families.py
```

Machine-readable output: `research/results/strategy_family_comparison.json`.
