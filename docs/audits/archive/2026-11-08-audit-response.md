# Audit response: Collomut/trial

Reviewed checkout: `audit-hardening-unreleased`, based on `6d65573` (the currently reviewed local feature snapshot), compared with the pasted review's older `main` commit `e2605f9`. This review does not authorize deployment. Live order placement remains explicitly blocked, and this branch has not been deployed.

## Implemented in this audit

| Finding | Status | Current behavior / evidence |
|---|---|---|
| Dashboard auth fail-open when token is absent | **Fixed in code; middleware integration test remains open** | `dashboard_auth` returns 503 for protected routes if `DASHBOARD_TOKEN` is missing. `verify_token` rejects empty configuration. `dashboard.py` |
| Login/query-token exposure | **Fixed** | Removed query-string token authentication and the query-token-to-cookie exchange. Login cookies are HttpOnly, SameSite Strict, HTTPS Secure when forwarded/request scheme is HTTPS, and expire after 12h. `dashboard.py` |
| Dashboard CSRF and brute-force protection | **Implemented, integration verification open** | Cookie-authenticated mutations require a same-origin Origin/Referer; login is limited to 5 failures per IP per 15-minute window. API token auth remains supported. Rate-limit state is in-memory and resets on restart. `dashboard.py` |
| Trailing/break-even exchange stop replacement gap | **Fixed, fail-closed** | Old stop is canceled, its reference is cleared, replacement must be open and persisted; ambiguous create responses are reconciled by client order ID before halting. `bot_engine.py`, `risk_manager.py` |
| Entry and exit commission P&L | **Partly fixed** | Base- and quote-asset fees are converted to quote and entry plus exit fee is counted in realized P&L. Unknown third-asset fee (e.g. BNB) is not guessed; entries are protected then halted and exits restore protection/halt for reconciliation. Conversion of third-asset fees remains unresolved. `bot_engine.py`, `risk_manager.py` |
| Misleading protective-stop documentation | **Fixed for reviewed claims** | README, glossary, and position page now distinguish exchange-hosted initial stop-markets from process-monitored take-profit/trailing logic and describe fee-adjusted realized P&L versus gross estimated open P&L. `README.md`, `templates/glossary.html`, `templates/positions.html` |
| Strict boolean parsing | **Implemented before this audit** | `PAPER_TRADING` accepts only `true`/`false`; other switches accept explicit common boolean forms and reject malformed values. Live mode remains blocked even with acknowledgement. `config.py` |
| Stale ticker/candle inputs | **Fixed for entry-price and signal data** | Current-price reads require a positive finite ticker with a timestamp no older than 120 seconds; strategy candles must be closed, contiguous and recent. This does not cover every exchange/API health condition. `exchange.py` |
| Short-history trend weakening | **Fixed** | Combined strategy now uses a fixed configured EMA period and blocks BUY candidates when that many completed candles are unavailable; it no longer silently shortens the EMA. `strategies.py`, `bot_engine.py`, `README.md` |
| Post-fill stop risk above cap | **Fixed with halt** | Computes quote loss to the post-fill stop plus valued entry fee. If risk exceeds the pre-trade cap or cannot be validated, places the protective stop first, then halts entries and records an audit event. `bot_engine.py` |

## Checklist disposition against the current checkout

The pasted checklist targets an older code snapshot. “Already addressed” below means a corresponding safeguard is present in this checkout, not that the system is certified for funded live trading.

| Pasted finding | Status in current checkout | Evidence / remaining limitation |
|---|---|---|
| Exchange-hosted protective stop | **Partly fixed** | Native market stop is placed and verified on entry; trailing-stop replacement now fails closed. A stop-market trigger does not guarantee its trigger price as fill price; exchange support, network/API availability, order rejection, and operator monitoring remain relevant. Take-profit is process-monitored. |
| Exchange/local state reconciliation and durable storage | **Partly fixed** | PostgreSQL/Supabase and SQLite stores, startup/cycle reconciliation, order intents, and balance baselining exist. Multi-process ownership/leader leasing is absent; external/manual activity and unsupported balances can need reconciliation. |
| Order lifecycle, fills, partials, idempotency | **Partly fixed** | Deterministic client IDs, confirmed fill checks, partial/rejected handling, and reconciliation exist. Third-asset fee valuation remains unresolved; full fault-injection coverage is still needed. |
| Concurrent cycles | **Already addressed in-process** | A bot cycle lock serializes worker and manual cycle entry points. No distributed lock prevents two replicas from trading simultaneously; deployment must remain single-replica. |
| Dashboard authentication and controls | **Fixed/partly fixed** | Auth covers dashboard data and controls; missing token now fails closed; login has basic rate limiting and cookie CSRF defense. Rate-limit state is not distributed, and deployment/proxy header assumptions require validation. |
| Live mode opt-in | **Already addressed** | Live trading is rejected in config and exchange construction. Keep this invariant and do not deploy a bypass. |
| Daily loss/equity risk accounting | **Partly fixed** | Daily state persists and drawdown checks run during cycles. Accurate account-wide equity (including external assets/liabilities and fee marks) is not fully authoritative; a host-independent circuit breaker is absent. |
| Market precision, amount and notional constraints | **Already addressed in current exchange/order path** | Market metadata/precision and preflight checks exist. Must retain behavior and verify on every configured symbol/environment; exchange metadata changes are external. |
| Closed-candle signals | **Already addressed** | Incomplete candles are filtered before signal calculation. Timestamp continuity/staleness remains a separate open issue. |
| Indicator periods and strategy claims | **Partly fixed** | Stale 200 EMA claims and shortened-period fallback are fixed. The selected 50 EMA, ADX and volume thresholds still require broader out-of-sample calibration; do not infer profitability from these controls. |
| Volume confirmation | **Partly fixed** | Completed-candle flow exists. Market-specific volume units/threshold calibration and regime validation remain to be evidenced. |
| Risk sizing versus actual fill/stop | **Partly fixed** | Post-fill stop risk and valued entry fee are compared against the pre-trade cap after the native stop is placed; breaches halt new entries. Slippage through the stop and exit fees remain outside any guaranteed loss cap. |
| Stale/invalid market data and API failures | **Partly fixed** | Positive finite ticker timestamps are limited to 120 seconds; closed candles must be recent and contiguous; missing data blocks entry. Repeated broader API failures and market-data service health still need end-to-end fault tests. |
| Configuration bounds | **Partly fixed** | Many values and pair/timeframe settings are validated. Audit all runtime timing, order sizing and optional values; do not assume exhaustive validation. |
| Atomic persistence and halt on failure | **Partly fixed** | State writes and order-state persistence are guarded; position changes fail closed in critical paths. Atomic filesystem durability/recovery across abrupt power loss still needs validation; SQLite/Postgres behavior depends on selected store. |
| Failed exit retries and alerts | **Partly fixed** | Exit failures restore native protection and halt on ambiguous order outcomes. Alert delivery depends on configured external channels and is not guaranteed. |
| P&L/performance reporting | **Partly fixed** | Persisted trade events and dashboard records exist. Entry/exit quote/base fees now net into recorded closed P&L; third-asset fee conversion, slippage, taxes, and full exchange-derived account equity remain outside the reported metric. |
| Backtest realism and multi-asset portfolio simulation | **Unresolved** | Existing replay/backtest is exploratory and does not establish strategy edge. It is not a fully aligned multi-asset, exchange-realistic walk-forward evaluation. |
| Deterministic safety tests | **Partly fixed** | Expanded safety tests exist and new fee/auth tests were added here. Full suite could not yet complete in this environment because psycopg/psycopg_pool are unavailable and the sandbox default temp folder is unwritable; rerun using workspace-local temp storage and project dependencies. |
| Separate dashboard and worker / multi-instance ownership | **Unresolved** | Dashboard and trading worker share one process. Cycle lock is process-local and does not solve multi-replica ownership. Keep one replica; production-grade isolation requires architectural work. |
| Bounded request work / manual run-cycle | **Partly fixed** | Cycles serialize in-process, but the dashboard still exposes a cycle trigger that can occupy executor capacity; a bounded queue/worker interface remains preferable. |
| Logging reliability, rotation and sensitive data | **Partly fixed** | Structured audit events and redaction exist. File rotation/retention and concurrent durable log guarantees remain to verify. |
| Dependency pinning and deployment CI | **Unresolved** | Dependency locking and release gating need a separate reproducible build/CI change. |
| Safety wording and read-only CLI commands | **Partly fixed** | README keeps explicit live block and exploratory-backtest disclaimers; this audit corrected the obsolete claim that there are no exchange stops. Review all user-facing pages and ensure `--once` is clearly trade-capable in paper mode. |

## Verification

- `python -m compileall -q config.py dashboard.py risk_manager.py bot_engine.py` — passed.
- Focused config, strategy, risk-management, order-lifecycle, and exchange connector suites — **76 tests passed** using temporary import-only PostgreSQL stubs and a workspace-local temp directory. The driver is not installed here, so database-backed integration tests were not run. Dashboard test fixture setup stalled; auth middleware integration remains unverified.
- New deterministic tests cover: empty/query-token rejection, base/quote fee conversion, unknown fee-currency refusal, and realized P&L including entry plus exit fees. The fee, stale-data, candle-continuity, EMA-history, post-fill-risk, and stop-recovery tests passed in the focused suites (76 tests total), plus a separate auth-helper test (1 test). Dashboard middleware end-to-end integration remains unverified because the dashboard test fixture stalled during setup.
- No exchange orders were placed. No deployment or Git push was performed.

## Release position

This audit does **not** establish that the strategy has positive expectancy or that the bot is ready for live trading. Live trading remains blocked. Outstanding material work includes third-asset fee conversion, multi-replica ownership/isolation, aligned realistic portfolio backtests, complete dependency/CI controls, dashboard middleware end-to-end verification, and a clean full deterministic integration run. A stop-market order can execute below its trigger and cannot guarantee a maximum loss; CCXT documents trigger-to-market behavior in its [manual](https://github.com/ccxt/ccxt/wiki/manual), and Binance documents that stop-loss triggers an order based on the stop price in its [Spot API glossary](https://developers.binance.com/en/docs/products/spot/faqs/spot_glossary).
