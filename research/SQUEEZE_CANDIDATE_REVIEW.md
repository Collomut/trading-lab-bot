# Keltner squeeze candidate review

**Scope:** Independent replay of the volatility-compression breakout specified in `AI_AGENT_STRATEGY_HANDOVER3.md`. This is research-only. It was not added to the production strategy factory and does not route orders.

## Decision

**Reject this candidate for Testnet order execution.** It achieved the proposed trade frequency, but failed the cost-adjusted profitability gates in validation and the 2026 review period. The active strategy and order behavior are unchanged.

| Period | Costs | Closed trades | Trades/month | Profit factor | Net P&L on $10,000 | Max drawdown |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 2021-02–2023 | Base | 352 | 10.07 | 1.012 | +$56.35 | 6.74% |
| 2021-02–2023 | Stress | 351 | 10.04 | 0.810 | -$969.36 | 12.69% |
| 2024–2025 validation | Base | 281 | 11.70 | 0.848 | -$459.09 | 8.29% |
| 2024–2025 validation | Stress | 280 | 11.66 | 0.651 | -$1,191.28 | 13.31% |
| 2026-01–2026-10 review | Base | 85 | 8.51 | 0.705 | -$234.75 | 3.14% |
| 2026-01–2026-10 review | Stress | 85 | 8.51 | 0.511 | -$462.67 | 5.28% |

Only the training-period base case was marginally positive; its profit factor of 1.012 is below 1.35 and the result disappeared under stress costs. Validation and 2026 were negative at both cost levels. The candidate passes the frequency range but fails profitability and, in validation, the stress drawdown target. It fails 8 of the 11 encoded acceptance gates.

## Implementation choices

- Compression means Bollinger Bands (20, 2 standard deviations) are inside Keltner Channels (EMA20 ± 1.5 ATR20), with ADX14 below 20; at least one such state must occur among the prior 10 completed candles.
- Breakout requires a close above the current upper Keltner Channel, volume at least 1.2 times its 20-candle average, close above EMA50, a three-candle EMA50 slope above 0.05%, and ATR14/close at or below 3%.
- Entry is at the next candle open. The initial stop uses the lower of the prior five-candle low and entry minus 2 ATR, then caps the distance at 4% of entry. Position risk uses the shared simulator's 1% equity risk and 10% notional cap.
- The trail arms after a completed close is more than 1.5 entry ATR above entry; it trails the highest completed close by 2.5 current ATR and applies from the next candle. A 3.5 entry-ATR target is treated as a gap-only exit at the next open, matching the handover wording. A close below EMA50 exits at the next open.
- Base and stress costs follow the handover. Risk halts were disabled for the comparison to keep an early halt from hiding the strategy's full-period behavior; this is not deployed risk behavior.

## Limits

The 2026 period was previously viewed in this project and is **not a pristine holdout**, even though this exact candidate was just supplied. The fixed three-pair universe is survivorship-biased. Hourly OHLCV cannot recreate actual spreads, depth, queue position, or fills. The exact stop interpretation above is an explicit reading of the handover's ambiguous “swing low OR 2 ATR” phrase; changing that interpretation would be a new candidate and must be declared before any further test.

The handover's experiment archive also needs reconciliation before its older rows are used as evidence: it says “8 previously screened candidates” but lists nine rows including the baseline, and its weekly momentum trade count (148) differs from the repo's earlier family screen (229). The Donchian row is labeled 20-day here, while the earlier repo screen used 55 bars, so those results are not directly comparable.

## Operational diagnostics

The Testnet analysis now samples top bid/ask once for each newly completed candle and stores spread in basis points and sample status in each `market_check` event. This is observational only: it does not alter entry checks. Candle continuity rejections now log the expected and actual timestamps and persist gap metadata in `market_data_issue` events. These changes make future Testnet observations more useful; they do not prove that spread or candle gaps caused prior missed trades.

Reproduce the backtest with `python research\backtest_squeeze.py`. Machine-readable results are in `research/results/squeeze_results.json`.
