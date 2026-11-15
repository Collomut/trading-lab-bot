# November 2026 trade-count forecast — macro-Donchian / EMA72 (8 pairs, 1h, paper-demo)

Dated 2026-11-15. Forecast of how many round-trips the live `MACRO_DONCHIAN_EMA72`
bot will close by 2026-11-30 23:59 UTC, grounded in the live production state and
the project's own hourly portfolio simulator. No production configuration, order,
or risk setting was changed.

## TL;DR

| | Value |
|---|---|
| November round-trips already booked | 1 (XRP/USDT, −$8.15, 14 Nov 12:25 → 17:59 UTC) — from the **now-disabled** downtrend-RSI-recovery route |
| Additional round-trips by 30 Nov | **median 3**, 80% range **1–5** |
| **Total November round-trips** | **best guess 4**, 80% range **2–6**, 90% range **1–7** |
| P(no further trade at all) | ≈ 8% |
| P(≥3 total) | ≈ 67% |

Counting only the **currently deployed** `MACRO_DONCHIAN_EMA72` code path, the
month-to-date figure is **0** (the single November trade came from the disabled
route), so "by the active strategy" the number is **0 + ≈3 ≈ 3**.

## Live state (last closed 1h bar 2026-11-15 07:00 UTC)

- `halted=false`, no `halt_reason`, `resume_available=false`, **0 open positions**.
- `total_trades=1`, paper `demo`, `strategy=MACRO_DONCHIAN_EMA72`, `max_positions=3`.
- Risk config: `MAX_CONSECUTIVE_LOSSES=3`, `MAX_DAILY_LOSS_PCT=3`,
  `MAX_ROLLING_DRAWDOWN_PCT=5`, `POSITION_SIZE_PCT=10`, 8 pairs, 1h.
- Breadth (symbols above EMA480) has been **0 for 320 of 336 November hours**.
- Distance to the nearest possible entry (`max(EMA480, prior 168-bar high) / close − 1`):

| Pair | Close | EMA480 | 168-bar high | Move to entry |
|---|---:|---:|---:|---:|
| BTC/USDT | 64,463 | 75,101 | 76,200 | +18.2% |
| ETH/USDT | 1,792.6 | 2,168.5 | 2,246.0 | +25.3% |
| SOL/USDT | 70.46 | 89.30 | 90.01 | +27.7% |
| BNB/USDT | 590.75 | 685.70 | 702.00 | +18.8% |
| XRP/USDT | 0.9588 | 1.1571 | 1.1699 | +22.0% |
| ADA/USDT | 0.1502 | 0.1854 | 0.1833 | +23.4% |
| DOGE/USDT | 0.0579 | 0.0714 | 0.0717 | +23.8% |
| LINK/USDT | 9.1100 | 10.7754 | 10.8300 | +18.9% |

No entry is possible until at least one pair closes above **both** its EMA480 and
its prior 168-bar high with ATR14 ≤ 2.5%. Exactly **one** current-rule BUY bar has
occurred in November (ETH/USDT, 2026-11-04 15:00 UTC) and the bot did not take it.

## Reproduce

```powershell
# Live state snapshot
$tok = (railway variables --json | ConvertFrom-Json).DASHBOARD_TOKEN
Invoke-RestMethod -Uri 'https://trial-production-7ce1.up.railway.app/api/status' -Headers @{'X-Dashboard-Token'=$tok}
```

Historical estimates use the committed `research/data/*_1h.csv` archive (through
2026-10-31) and `research/backtest_ema72_donchian.build_signals` +
`research/backtest_strategy.simulate` with the deployed stop profile
(`swing_low_floor_4pct`, 100×ATR distant target, no breakeven/trailing, 10 bps fee
+ 5 bps slippage, 3 positions, 0.85 correlation cap, 3-loss halt). Live-candle
breadth was recomputed from Binance public 1h klines.

## Historical frequency evidence

| Estimator | Sample | Trades / 16 days |
|---|---|---|
| Unconditional, 8 pairs, halts off | 2026-01…10 (97 trades) | mean 4.9, median 4 (≈9.7/month) |
| Documented 3-pair replay (`MACRO_DONCHIAN_REVIEW.md`) | 2026-01…10 (32 trades) | 3.20/month (≈1.8/16 d at 3 pairs) |
| **Conditioned on today's state** (breadth 0 ≥ 7 days AND avg distance ≥ 18%), 3-loss halt on | 25 windows / ~11 distinct episodes | median 3 entries, median 3 realized, P(0)=0%, max 8, halt in 28% |
| Same conditioning, halts off | 25 windows | median 3, mean 3.3 — halts do not drive the count; the breadth collapse does |
| Recent analogs only (Nov 2025, Feb 2026) | 11 windows | median 2.5, range 2–4 |

The conditioning set is the closest historical analogue to today (zero breadth
since 5 Nov, avg distance ≈ 22%). Nearest anchors: 2026-02-10 (run 283 h, dist
18.2% → 2–3 trades) and 2025-11-22 (run 225 h → 4 trades, halted).

## Distribution for the remainder of November (15.7 days)

De-duplicated conditional episodes (11 samples), re-weighted with 8% mass for
"no bounce before month end":

| Additional closed trades | 0 | 1 | 2 | 3 | 4–5 | 6 | 7–8 |
|---|---|---|---|---|---|---|---|
| Probability | 8% | 8% | 17% | 25% | 25% | 8% | 8% |

Adding the one booked trade → **total November = 2 (P25) / 4 (P50) / 5 (P75)**,
with ~16% chance of finishing at 1–2 and ~8% chance of 7+.

## Preconditions and halt risk

1. **Breadth must recover** first: a pair must close above EMA480 *and* its prior
   168-bar high with ATR14 ≤ 2.5%. All 8 fail today by 18–28%.
2. The 168-bar channel and EMA480 both roll down ~1%/day in a flat market, so after
   ~5–7 days of basing the barrier drops to "highest close of the last week + a few
   %". Past deep states produced entries this way without a full V-recovery.
3. **3-position + 0.85 correlation caps** usually limit a synchronized bounce to
   2–3 simultaneous entries (already in the simulator).
4. **Halt risk**: a third consecutive loss stops new entries and requires the manual
   dashboard review to resume. 28–38% of historical 16-day windows from this state
   halted. The 14 Nov XRP stop-out very likely leaves the streak at **1**, so only
   **two** more losses would halt — the counter is not exposed via `/api/status`.
5. These trades are historically **net losers** from this state (2026 episodes
   −$62 to −$86 per window), so more trades ≠ more profit.

## Assumptions and limitations

- "Trade" = closed round trip (BUY+SELL = 2 orders = 1 trade), matching dashboard
  `total_trades`; positions still open at month end are excluded.
- Simulator starts flat, matching live (0 positions, not halted).
- The current November decline is **not** in the research CSVs (which end
  2026-10-31); the conditioned sample is the closest analogue and is dominated by
  the 2022 bear (~11 independent episodes), so the true interval is wider.
- `MAX_CONSECUTIVE_LOSSES=3` halts are not auto-resumed; a manual resume after a
  halt would shift the upside case up by ~1–2 trades (upper bound ~6–8 additional).
- Railway `logs` / `deployment list` returned no usable output; month-to-date
  counts come from `/api/status.trades` (XRP round trip only).

