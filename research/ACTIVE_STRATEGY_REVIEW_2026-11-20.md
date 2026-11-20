# Active strategy review — 2026-11-20

## Finding

The service was online and checking market data, but Railway was running
`macro_trend_pullback`. Supabase market checks confirmed that the eight symbols
were returning HOLD because the profile requires a macro uptrend; seven symbols
were in a stable downtrend and XRP was in transition. The dynamic regime router
was marked `shadow_only`, so its downtrend route could not select an entry
candidate. The bot was not stuck waiting for data: it had a fresh hourly candle,
current spreads, and a successful Demo balance check.

At the latest check before the configuration change, entry mode was ACTIVE,
paper environment was Binance Demo, the risk manager was not halted, no tracked
positions were open, and all stored order intents were terminal FILLED states.
The current account balance was $4,978.15 USDT. No credentials were read or
changed.

## Action applied

Railway production variables for service `trial` were changed to:

| Setting | Value | Purpose |
| --- | --- | --- |
| `STRATEGY` | `dynamic_regime` | Route stable regimes to their configured paper candidate |
| `LOOKBACK_CANDLES` | `2500` | Satisfy the router's validated hourly history requirement |
| `ENABLE_DOWNTREND_RECOVERY_TESTNET` | `true` | Permit the low-risk recovery candidate to evaluate on Demo |
| `DOWNTREND_RSI_RECOVERY_LEVEL` | `25` | Require a deep RSI recovery trigger |

The deployment `c01e3156-a274-4090-9768-c1d936e2f0a4` completed successfully;
Railway's `/health/liveness` check passed. The service remains Binance Demo.
The downtrend route risks 0.20× the configured normal risk, is limited to one
recovery position across the portfolio, and still passes the normal sizing,
exchange reconciliation, stop, spread, order-quality, daily-loss, drawdown,
and consecutive-loss controls. Range, high-volatility, transition, and invalid
data still produce no entry.

This change repairs the intended dynamic routing and allows a Demo-only
experiment to evaluate setups. It does not guarantee an immediate trade or
improve the demonstrated strategy edge. A SELL in a spot strategy is an exit
for an existing tracked position; it is not a short entry. There were no bot
positions at the time of the change.

## Historical evidence

The exact RSI-25 downtrend candidate was replayed on official Binance Spot 1h
archives for the deployed eight-symbol universe (BTC, ETH, SOL, BNB, XRP, ADA,
DOGE, LINK), with the three-consecutive-loss halt and one-recovery-position
limit. The base cost model is 10 bps fee + 5 bps slippage per side; stress is
20 + 10 bps per side.

| Period | Cost | Trades | Net P&L on $10,000 | Profit factor | Max drawdown |
| --- | --- | ---: | ---: | ---: | ---: |
| 2021–2023 | Base | 6 | −$10.86 | 0.357 | 0.128% |
| 2021–2023 | Stress | 6 | −$14.33 | 0.278 | 0.160% |
| 2024–2025 | Base | 12 | −$0.39 | 0.978 | 0.108% |
| 2024–2025 | Stress | 12 | −$7.59 | 0.650 | 0.147% |
| Jan–Oct 2026 | Base | 6 | +$3.79 | 1.590 | 0.069% |
| Jan–Oct 2026 | Stress | 6 | +$0.48 | 1.058 | 0.084% |

This is a small, inconsistent sample: earlier periods lose and validation is
negative after stress costs. The modest 2026 result is not a proven edge and
the period has already been inspected during research.

The previously active `macro_trend_pullback` also failed the eight-symbol
replay's validation and 2026 review slices: without loss halts, 2024–2025 had
1,508 trades, PF 0.925, −$942.68 and 21.086% drawdown at base costs; Jan–Oct
2026 had 541 trades, PF 0.654, −$1,263.81 and 14.536% drawdown. Under the
three-loss halt, those same base-cost slices stopped after four closed trades
at −$38.09 and −$17.86 respectively. This reinforces that increasing trade
count alone is not a quality improvement.

## Limits and follow-up

The replay uses hourly OHLCV, next-bar fills, and conservative stop-first
intrabar assumptions. It cannot reproduce order-book queue position, actual
Demo fills, outages, or exact fee tiers. The eight-symbol sample also has
survivorship bias, and no historical simulation proves future profitability.
The archived data ends in October 2026; November conditions are evaluated only
through prospective Demo checks.

Monitor the Dashboard Logs for `regime_router` status, selected candidate,
candidate signal, and explicit entry block reasons. Confirm that each new
market check shows `dynamic_testnet` (the router's internal status name) and
that a stable downtrend selects `downtrend_rsi_recovery_testnet`; it may still
return HOLD until RSI14 crosses back above 25. Record order fills, fees, stop
behavior, and realized P&L before making any further frequency change. Keep
real-money entries disabled until prospective results, reconciliation, and
loss/drawdown behavior meet predeclared acceptance criteria.

Reproduce the active-strategy replays with:

```powershell
python research/backtest_macro_trend_pullback.py
python research/backtest_downtrend_rsi_recovery.py
```

Result artifacts are `results/macro_trend_pullback_results.json` and
`results/downtrend_rsi_recovery_level25_results.json`.
