# Trade-frequency study

**Reviewed:** 2026-11-10

**Decision:** do not change the active strategy or timeframe. The tested ways to raise trade count materially worsened historical net performance and drawdown; the current strategy also lacks a positive historical edge.

## Question and candidates

The objective was to increase completed trades without relaxing per-trade risk controls. I compared:

1. The current combined entry rules on 1-hour candles.
2. The exact same rule periods and gates on 15-minute candles.
3. A mixed-timeframe candidate: 15-minute 2-of-3 signals for entry timing, but only while the latest completed 1-hour candle passes the existing EMA50 direction, ADX, and ATR regime gates. The 15-minute volume and confidence checks remain. Bearish signals only exit Spot longs.

The mixed-timeframe test uses only information available when each candle closes; fills are at the next candle open. All variants keep the same $10,000 research account, 1% risk ceiling, 10% notional cap, 3-position cap, 3% target, ATR stop, one position per asset, and correlation check using approximately 100 hours of returns.

## Data and checks

The 15-minute sample contains 204,410 completed candles per symbol from Binance Spot public monthly archives, January 2021 through October 2026. All 70 requested monthly archives were present for BTCUSDT, ETHUSDT, and SOLUSDT. Seven gaps (90 to 285 minutes) were found in each symbol's archive. The replay now blocks signals unless the full 100-candle input sequence is contiguous, matching the application's candle-sequence guard.

For each symbol and interval, 200 deterministic sample points were compared against the production `CombinedStrategy`: **zero signal mismatches**. This checks implementation parity, not the quality or profitability of the strategy.

Base costs are 10 bps fee plus 5 bps slippage per side; stress costs are 20 bps plus 10 bps per side. OHLCV cannot reconstruct historical order books, exact spread, latency, fees, or intrabar stop order. The hourly and 15-minute indicator periods represent different time horizons. The historical data has also been used for earlier project research, so it is not a pristine holdout.

## Results

The following performance runs disable persistent risk halts to compare entry rules over the full period. The 1% per-trade risk ceiling, notional and position limits, stop/target rules, and portfolio simulator remain active. Results with the bot's current permanent halt behavior are reported separately because the strategy halts after three consecutive losing trades.

| Variant | Eligible buy candles | Closed trades | Base net P&L | Base profit factor | Base max drawdown | Stress net P&L | Stress max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Current rules, 1h | 592 | 396 | -$521 | 0.840 | 7.39% | -$1,510 | 16.62% |
| Same rules, 15m | 2,222 | 1,483 | -$3,186 | 0.654 | 32.44% | -$5,566 | 55.99% |
| 1h regime + 15m entries | 2,450 | 1,551 | -$3,401 | 0.621 | 34.70% | -$5,904 | 59.20% |

All three variants lost in every chronological segment at base and stress costs. The 15-minute entry method produced about 3.75 times the current strategy's buy-signal count, but also about six times its base-cost loss and over four times its drawdown. Adding the hourly regime gate did not fix the result.

| Current halt behavior | Closed trades before persistent halt | Halt |
| --- | ---: | --- |
| 1h current rules | 24 | Three consecutive losing trades |
| 15m same rules | 7 | Three consecutive losing trades |
| 1h regime + 15m entries | 11 | Three consecutive losing trades |

The positive base-cost P&L in the halted hourly run is not a strategy edge: the account stopped opening trades early and sat out the remainder of the historical sample. The 15-minute and mixed-timeframe halted runs likewise ended after very few trades. Do not remove or weaken this halt to obtain more activity.

## Decision and next step

No tested candidate meets the requested condition of more trades without materially worse risk. I left the application's strategy, runtime variables, position sizing, and Railway deployment unchanged. Do not move this bot to 15-minute trading or real-money use based on these results.

The next useful activity test is a **read-only shadow run** on Testnet: calculate both the existing and candidate signals, record which gates would allow entries and hypothetical fills, and submit no orders until a candidate shows positive net expectancy after fees/slippage across forward observation and stable drawdown. Do not use the current losing 15-minute candidates for live orders.

## Reproduction

Download the official public monthly archives:

```powershell
python research\download_interval_data.py --interval 15m --start-year 2021 --start-month 1 --end-year 2026 --end-month 10
```

Run the full 1h/15m comparison and the mixed-timeframe candidate:

```powershell
python research\backtest_frequency.py
```

The JSON output is `research/results/frequency_comparison.json`. Research data is local and ignored by Git. Sources: [Binance Spot public-data repository](https://github.com/binance/binance-public-data), [Binance Spot kline glossary](https://developers.binance.com/en/docs/products/spot/faqs/spot_glossary), and Bailey et al., [The Probability of Backtest Overfitting](https://escholarship.org/uc/item/4w1110bb).
