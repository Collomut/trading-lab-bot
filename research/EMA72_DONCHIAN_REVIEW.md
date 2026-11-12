# Independent replay: Donchian breakout with EMA72 exit

**Date:** 2026-11-12  
**Candidate:** `D168_M480_E72` from `AI_AGENT_STRATEGY_HANDOVER7.md`  
**Scope:** Historical research plus a guarded Binance Spot Testnet implementation. The runtime candidate is selectable only with explicit Testnet configuration; these results do not establish a live-trading edge.

## Decision

This candidate is a **Testnet-only forward-observation hypothesis**, not a strategy that has established a deployable edge. It beat the current ADX20 combined strategy in all three strategy-only historical slices and stayed above PF 1 under the stated stress costs. But train drawdown was 6.44% base / 7.53% stress, beyond the project's 5% rolling-drawdown setting. It traded only 3.32 times/month in training and 4.21 times/month in the reviewed 2026 slice, below the proposed six/month floor. With current halt behavior, it stopped taking new entries after four closed trades in train/validation and eight in 2026, all due to the three-loss control. The 2026 period has already been used in prior project research and is not an untouched test. Forward Testnet results must not be used as proof of live profitability.

The independent replay did **not** reproduce the handover's reported candidate counts (311 / 213 / 47). The runtime-aligned 2,500-candle warmup replay produced 116 / 171 / 42 with the rules and simulator specified below. An earlier 480-bar warmup replay produced 209 / 171 / 42; the train difference shows how warmup choice affects the result. Remaining discrepancies must be resolved before relying on the handover's candidate metrics.

## Same-simulator strategy-only comparison

Each split starts with a separate $10,000 account. P&Ls across splits must not be added as a single account return. The comparison disables persistent halts to isolate entry/exit performance; it is not the deployed operating result.

| Split / costs | Current strategy: trades / PF / P&L / DD | EMA72 Donchian: trades / PF / P&L / DD | Candidate trades/month |
| --- | --- | --- | ---: |
| 2021–2023 base | 143 / 0.824 / −$215 / 4.56% | 116 / 1.575 / +$1,109 / 6.44% | 3.32 |
| 2021–2023 stress | 142 / 0.595 / −$575 / 7.20% | 116 / 1.362 / +$761 / 7.53% | 3.32 |
| 2024–2025 base | 107 / 0.843 / −$140 / 2.37% | 171 / 1.649 / +$1,542 / 4.75% | 7.12 |
| 2024–2025 stress | 107 / 0.596 / −$422 / 4.34% | 171 / 1.384 / +$1,004 / 5.05% | 7.12 |
| 2026 Jan–Oct base | 36 / 0.792 / −$57 / 0.90% | 42 / 1.496 / +$245 / 2.90% | 4.21 |
| 2026 Jan–Oct stress | 37 / 0.374 / −$246 / 2.63% | 42 / 1.214 / +$123 / 3.59% | 4.21 |

Base costs are 10 bps fee and 5 bps slippage per side; stress costs are 20 bps and 10 bps per side. Both strategies use identical data, period boundaries, next-open fills, account reset, shared sizing, position caps, and correlation checks. Candidate stop/target assumptions differ from the current strategy as declared below.

## Current halt behavior

The candidate replay with the shared simulator's 3% daily loss, 5% peak drawdown, and three-consecutive-loss controls stopped taking entries early:

| Split / costs | Trades closed before/at halt | P&L | Halt reason |
| --- | ---: | ---: | --- |
| Train base | 4 | −$122 | Three consecutive losing trades |
| Train stress | 4 | −$132 | Three consecutive losing trades |
| Validation base | 4 | −$44 | Three consecutive losing trades |
| Validation stress | 4 | −$55 | Three consecutive losing trades |
| 2026 review base | 8 | −$34 | Three consecutive losing trades |
| 2026 review stress | 8 | −$57 | Three consecutive losing trades |

These small, halt-truncated samples are not valid estimates of sustained profitability. Raising the consecutive-loss limit does not address the train drawdown failure; do not raise it based on these results.

## Reproduction and assumptions

Run `python research/backtest_ema72_donchian.py`. Machine-readable output is `research/results/ema72_donchian_results.json`. The replay currently relies on local uncommitted additions to `research/backtest_strategy.py` for the swing-low/4% stop and disabling legacy trailing. A clean checkout at the current commit will not reproduce this run until those research-simulator changes are included. They do not affect runtime trading code.

- Symbols: BTCUSDT, ETHUSDT, SOLUSDT, hourly Binance Spot OHLCV.
- Entry: completed close above the prior 168-bar high and EMA480; enter at the next bar's open.
- Exit: completed close below EMA72; exit at the next bar's open.
- Stop: `max(prior five-bar low, fill * 0.96)`.
- Target: 100 × ATR14 above entry, a finite approximation of uncapped upside. This is not literally uncapped and can differ from a no-target model.
- Break-even/trailing: disabled for the strategy-only candidate replay to isolate the stated EMA72 exit.
- Warmup: requires 2,500 contiguous completed hourly candles. Runtime requests 2,501 raw candles because the latest hourly bar is incomplete; pagination fetches these in requests capped at 1,000. This leaves roughly 2,020 EMA updates after the 480-period initialization, reducing seed error versus a 500-bar fetch.
- Signal generation is causal and uses shifted prior-channel highs; the simulator fills only at the next bar. Intrabar OHLC still cannot reproduce actual order book execution or exact price paths.

The replay enforces the longer runtime warmup, unlike the handover's inconsistent 149/480-bar descriptions. The target is the same finite 100×ATR14 model as this independent replay; the runtime candidate uses this target alongside its EMA72 signal exit. Exact stop, target, EMA initialization, split-boundary, and risk-halt treatment must match before comparing to the handover's reported results.

## Limits and next step

This is reused BTC/ETH/SOL history, fixed-universe selection, and OHLCV execution approximation. It does not establish a future edge. The bot can trial this candidate only on Spot Testnet with the 1% risk cap, 10% notional cap, three-position limit, daily/rolling halts, and three-loss halt unchanged. The operational replay stops after very few trades, so a Testnet run may halt quickly; do not weaken these controls to force activity. Resolve the remaining trade-count discrepancy, rerun the complete predeclared strategy-family comparison with identical risk controls and independently audited accounting, and assess parameter stability and uncertainty before considering anything beyond Testnet.
