# Wider coin-universe shadow evaluation

**Reviewed:** 2026-11-11  
**Mode:** offline, read-only historical shadow evaluation; no orders sent  
**Decision:** do not add the candidate coins to the bot's active `TRADING_PAIRS`.

## Question and fixed setup

Does scanning five additional established Binance USDT spot markets create more completed trades while retaining acceptable results under the existing portfolio limits?

The baseline was BTC, ETH, and SOL. The predeclared research cohort was BNB, XRP, ADA, DOGE, and LINK. Both baskets used the production 1-hour combined rules and the deployed **ADX 20** setting, 2-of-3 vote consensus, EMA50 direction, ATR/volume/confidence gates, next-hour-open fills, and the same exit logic. Position size, 1% risk ceiling, 10% notional cap, three-position limit, 0.85 pairwise correlation cap, and loss/drawdown rules were not relaxed. New symbols were only used in this offline simulator.

## Results

The full-period comparison disables permanent risk halts to compare the entry universe across the same dates. Base costs are 10 bps fee plus 5 bps slippage per side; stress costs are 20 bps fee plus 10 bps slippage per side.

| Universe | Closed trades | Base net P&L | Base profit factor | Base max drawdown | Stress net P&L | Stress max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Current: BTC, ETH, SOL | 287 | -$381 | 0.839 | 6.08% | -$1,172 | 13.06% |
| Expanded: current + BNB, XRP, ADA, DOGE, LINK | 666 | -$1,693 | 0.706 | 18.87% | -$3,078 | 31.65% |

The expanded universe increased closed trades by **379 (+132%)**, but increased base-cost losses by about **$1,312** and max drawdown by **12.79 percentage points**. Under stress costs, the expanded basket lost about **$1,905 more** and drawdown rose by **18.59 points**. Profit factor stayed below 1 in both baskets and both cost scenarios.

The expanded basket lost money in each chronological segment. At base costs it lost $1,097 in 2021–2023, $486 in 2024–2025, and $190 in 2026 through October. That means the increased frequency did not come with evidence of improved trade quality.

The five added coins produced 565 eligible buy signals in total before portfolio constraints; the expanded account completed only 379 more trades than the baseline. Concurrent signals, the position cap, correlation checks, and symbol lockouts therefore prevent raw signal counts from translating one-for-one into trades.

Signal parity was checked against the production strategy on 200 deterministic samples for each of eight symbols: **zero mismatches**. This validates the shadow signal calculation on those samples; it does not validate profitability or actual execution.

## Data and limits

The data came from Binance's official [Spot monthly kline archives](https://github.com/binance/binance-public-data), covering January 2021 through October 2026: 70 monthly archives per symbol, no failed/missing archives, 51,106 hourly rows per symbol. Each symbol had seven timestamp gaps, the largest five hours. Production's contiguous-candle rule excludes signal windows crossing gaps.

The candidate set is a predeclared research cohort of established crypto assets, not the result of a verified current liquidity screen. It therefore has survivorship and selection bias. Historical OHLCV cannot recreate historical order-book depth, spreads, actual fees, latency, delistings, or exact stop execution. These tests use the same historical period already used in prior project research; this is not a fresh holdout. Stress costs are a model, not a guarantee of maximum losses.

This is a research comparison, not a forward Testnet shadow period. No exchange orders were submitted, and no live configuration, strategy, dashboard, or active pair list was changed.

## Next step

Do **not** enable this five-coin basket. The current strategy itself remains historically negative, and the broader basket materially worsened losses and drawdown. More symbols can add opportunity, but under these fixed rules and constraints they also amplified exposure to poor signals. Keep the active universe unchanged. Any future coin-universe candidate needs a new, predeclared rationale and a genuinely out-of-sample, read-only forward shadow review after checking current Binance trading status, rolling liquidity, spread/depth, and time-varying correlation. It must show acceptable net expectancy at stressed costs and drawdown before any operator considers adding it.

## Reproduction

Download the same official hourly market files:

```powershell
python research\download_interval_data.py --interval 1h --start-year 2021 --start-month 1 --end-year 2026 --end-month 10 --symbols BTCUSDT ETHUSDT SOLUSDT BNBUSDT XRPUSDT ADAUSDT DOGEUSDT LINKUSDT
```

Run the read-only comparison:

```powershell
python research\shadow_coin_universe.py
```

Machine-readable results: `research/results/coin_universe_shadow.json`. The research data directory remains git-ignored.
