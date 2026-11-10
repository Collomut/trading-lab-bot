# Trading Bot Engineering Handover — Current Unresolved Work

**Repository:** `Collomut/trial`  
**Updated:** 2026-11-08  
**Audited change branch:** `audit-hardening-unreleased`  
**Deployment boundary:** Spot Testnet/paper only. Live order placement remains blocked. This handover is not authorization to enable live trading.

Use this together with [`2026-11-08-audit-response.md`](2026-11-08-audit-response.md), which maps the original review checklist to the code and tests at that time. This is a historical snapshot; see the current pre-launch review before making release decisions.

## Current strengths to preserve

- Live order creation is intentionally blocked in configuration and exchange initialization.
- Account and order reconciliation, order intents, idempotency keys, partial-fill handling, persistent state, and cycle serialization are present.
- New entries use completed candles; this branch also rejects stale/discontinuous candles and stale/missing ticker timestamps.
- Protective initial stops are placed at the exchange. Stop replacement failures halt entries, and ambiguous replacement responses are reconciled by client order ID.
- The current strategy uses a fixed 50-period trend EMA, completed-candle votes, and a persisted risk halt. Its research results are not evidence of positive expectancy.
- The current Railway service is configured for one replica and points at `testnet-prelaunch-safeguards`.

## Priority 0 — verify before unattended Testnet operation

### 1. Run integration tests with the deployed database driver

The local environment lacked `psycopg`/`psycopg_pool`, so focused unit tests used import-only stubs. Database-backed paths, migrations, transaction behavior, and runtime auth integration still need execution with the pinned runtime dependencies. Add CI that installs the production dependency set and runs the state-store, migration, and dashboard middleware suites. Do not treat stubbed database imports as database verification.

### 2. Close the protective-stop cancel/fill race

Entry protection and replacement recovery are stronger, but exits still require a race-aware sequence: reconcile the stop's final state and remaining base balance after cancellation; verify that a sell fill and residual quantity match; ensure a correctly sized stop protects any remainder. Cover cancel timeout, stop-filled-during-cancel, partial exit, and store failure cases with deterministic tests. Until then, ambiguous outcomes require operator reconciliation.

### 3. Complete fee valuation and risk accounting

Entry and exit fees paid in the base or quote asset can be valued. Fees charged in a third asset (such as BNB) are intentionally treated as ambiguous and halt the bot. Add a trusted time-aligned conversion or keep the halt and make manual reconciliation explicit. Include estimated exit fees and execution costs in post-fill risk estimates where supportable. Never call a stop distance a guaranteed maximum loss.

## Priority 1 — reliability and account truth

### 4. Add single-worker ownership across replicas

The lock is process-local. Railway currently runs one replica, but a second instance could still race order decisions. Add a durable lease/leader mechanism before allowing multi-replica operation; otherwise retain and monitor the single-replica constraint.

### 5. Reconcile the entire account, not only configured pairs

Risk equity currently values USDT and configured pair assets. Define how unrelated assets, manual orders/trades, locked balances, and liabilities affect total equity and bot ownership. Continue to halt when account state cannot be reconciled; do not silently treat unknown inventory as bot-owned.

### 6. Exercise stop and order behavior against authenticated Spot Testnet

Unit tests cannot prove Binance trigger semantics, filter behavior, cancel races, or fill/fee payload shapes. After account authentication is working, conduct controlled, operator-observed Testnet lifecycle checks: entry, native stop acknowledgement, stop replacement, exit, partial/unknown response handling, restart recovery, and Testnet reset recovery. Do not use live credentials or enable live mode for these checks.

### 7. Validate account diagnostics and dashboard controls end to end

The source has fail-closed auth, same-origin checks for cookie mutations, rate limiting, and sanitized exchange errors. The aiohttp middleware integration suite stalled in this local sandbox; run it in CI and confirm missing-token requests fail closed, login cookies are Secure over Railway HTTPS, and mutating routes reject cross-origin cookie requests.

## Priority 2 — strategy evidence and operational maturity

### 8. Treat strategy performance as unproven

The research in `research/STRATEGY_REEVALUATION.md` reports negative historical results for the tested current rule set and variants. The crossover lookback/recovery rules are already in the current deployed baseline; do not reapply stale working-tree copies or relax gates solely to force trades. Any replacement strategy needs chronological train/validation/holdout tests, realistic costs, aligned multi-asset portfolio simulation, and Testnet forward observation. A profitable backtest is not guaranteed future performance.

### 9. Finish reproducible release controls

Pin/audit dependencies; add CI for deterministic tests, migration checks, and security checks; and document rollback and Testnet-reset recovery. Keep backups before state migrations and require unresolved-order review before resuming.

### 10. Keep operational controls understandable

Continue clearly separating exchange-hosted initial stop loss from app-monitored take-profit/trailing behavior. Report unavailable account data as unavailable, not as a simulated or estimated exchange balance. Keep logs redacted, bounded, and actionable without exposing credentials.

## Release boundary

This branch improves code safety and diagnostics but does **not** certify strategy edge, prove all external exchange behavior, or make the bot suitable for funded live trading. Keep live order creation disabled. Any future live-capable change requires a separate review of the remaining items, verified runtime/integration evidence, and an explicit release decision.
