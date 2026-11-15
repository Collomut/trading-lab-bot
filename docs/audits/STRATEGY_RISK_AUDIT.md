# Strategy & risk audit — 2026-11-15

**Scope:** full static audit of the trading bot's strategy, risk, execution,
persistence, and evidence pipeline, plus a cross-check of the AI handover against
the `3ff7187` tree. This document records the findings and the remediation that
was implemented and tested (commits `78a2e29`…).

## Verdict

The execution layer is careful (pre-submit journaling, stable client-order IDs,
cancel-before-sell, restore-on-failure, strict reconciliation, fee-currency
fail-closed). The defects were concentrated in **halt/recovery semantics** and
**evidence design**:

1. Halt state was broken in both directions — most halts could never be cleared
   through any control, yet a restart after UTC midnight silently erased
   "manual review required" latches.
2. The deployed configuration was mathematically incapable of producing the
   evidence needed to justify it: the 3-consecutive-loss halt terminated every
   historical split after 4–8 trades, sampling the left tail.

## Evidence (independent replay, 1h, BTC/ETH/SOL, 10bps fee + 5bps slippage base)

| Split | Trades | Win% | PF (base) | PF (stress) | MaxDD base |
|---|---|---|---|---|---|
| train 2021-02→2023 | 116 | 35.3 | 1.575 | 1.362 | 6.44% |
| validation 2024–2025 | 171 | 41.5 | 1.649 | 1.384 | 4.75% |
| review 2026-01→10 | 42 | 31.0 | 1.496 | 1.214 | 2.90% |

With the **deployed** halt policy, every split ends halted after 4–8 trades with
negative P&L. `E[trades until 3 consecutive losses] ≈ 6.4–9.1` at these win
rates. The regime router was **worse** on PF and drawdown in train and
validation and only "won" the reused 2026 slice — in-sample selection.

## Remediation implemented (this session)

- **Halt taxonomy** (`risk_manager.py`): single source of truth
  (`DAILY_RESET_HALT_PREFIXES`, `REVIEWED_CLEAR_HALT_PREFIXES`,
  `ROLLING_DRAWDOWN_HALT_PREFIX`) with `is_daily_reset_halt`,
  `is_reviewed_clear_halt`, `is_rolling_drawdown_halt`. Removed three duplicated
  prefix lists in `bot_engine.py` and `dashboard.py`.
- **Durable halts across midnight**: `_load_daily_state` restores non-daily
  halts regardless of the persisted date.
- **Recoverable rolling-drawdown latch**: operator acknowledgement rebases
  `_rolling_peak_equity` to reconciled equity and emits an audit event.
- **Stop-fill reconciliation base-leak fix**: `_reconcile_exchange_state` derives
  the base asset from the position's symbol instead of the last configured pair.
- **Trade attribution**: `TradeRecord`/`Position` carry `strategy`, `route`,
  `regime`, `r_multiple`, `order_id`, `simulated`; SQLite gained a `trades`
  table (`append_trade`/`list_trades`) for parity with Postgres; trade history
  hydrates from the durable store on restart.
- **Single risk basis**: entries size and gate on managed equity (cash +
  tracked positions) instead of free cash.
- **Signal-to-fill divergence guard** (`max_signal_fill_divergence_bps`): a fill
  far from the generating signal's close halts for review after the stop is
  placed.
- **Global risk caps** tightened to the candidate-profile floor (1% risk, 10%
  position, 3 positions, 3% daily, 5% rolling, 3 consecutive losses) for every
  profile.
- **Default strategy → `macro_donchian_ema72`** (highest cost-adjusted PF),
  documented as an experimental Testnet candidate; live orders remain blocked.
- **Shadow ledger** (`shadow_ledger.py`): persisted, halt-free, order-free
  forward expectancy ledger with next-bar-open execution, stop-first intrabar
  handling, and per-candidate PF/win-rate/R reporting. Wired into
  `analyze_market` and exposed via `/api/status`.
- **Ops/docs**: rotating log handler, `.env.example` completeness, BNB fee
  discount warning, Testnet runbook filename fix, stale EMA72 caveat corrected,
  handover committed.

## Remaining launch blockers (unchanged)

Live order placement is blocked in code. Before any real-money rollout, the
operator must independently verify: a real Testnet stop round-trip, exchange
permissions/IP restrictions, BNB fee discount disabled, 14-day soak, minimum-size
burn-in, external dead-man alerting, and a pre-registered decision rule driven by
the shadow ledger. None of this can be proven by the test suite (179 tests, all
passing) alone.
