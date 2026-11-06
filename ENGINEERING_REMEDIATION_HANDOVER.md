# Trading Bot Engineering Weakness Audit and Remediation Handover

**Purpose:** Give a follow-on coding agent a prioritized, evidence-based remediation backlog for the current repository.

**Reviewed checkout:** `testnet-prelaunch-safeguards`, commit `f77dde5` (`Read CCXT response metadata for account errors`).

**Scope:** Current code and the existing pre-launch audit, with operation set to Binance Spot Testnet / paper trading. This review does not assume Demo API credentials exist, does not authorize live trading, and does not claim an exchange order has been successfully tested. Live initialization is intentionally blocked in `exchange.py`. The last Railway runtime evidence available during this review was from 2026-11-06: service startup succeeded, but the authenticated Binance account request was rejected and the bot blocked new orders. Railway runtime was not re-queried for this static handover.

**Review method:** Static inspection of application, strategy, risk, exchange, persistence, dashboard, deployment, test, and documentation files; targeted review against current official Binance/CCXT documentation and OWASP guidance. No exchange credentials were read, changed, or included. No trades were submitted. This audit document is the only intended output of this task; code was not changed.

## Executive summary

The repository has meaningful safety work already: live exchange initialization is refused, order intents are journaled, entry pauses persist, exchange/account reconciliation is fail-closed, configured entries are checked against spread/depth/market limits, and SQLite backs core state for a single worker. Those are useful foundations.

The system is **not validated for real-money use**, and it is not currently operationally validated even for Testnet because authenticated balance/open-order access has not succeeded. There are several important code-level gaps that should be addressed before relying on Testnet automation: an accepted buy can be left untracked/unprotected after local persistence or fee parsing fails; exits remove the exchange stop before the replacement market exit is confirmed; local trailing/break-even changes do not update the exchange stop; the same synchronous CCXT object is called from multiple worker threads; and account error diagnosis is still generic because the diagnostic call sites use the wrong CCXT response attributes/status field.

Do not respond to this backlog by enabling live mode. Fix and validate in isolated stages on Spot Testnet after account access works. Testnet is useful for API/order-flow rehearsals; it is not evidence of realistic fills or a profitable trading edge.

## Priority definitions

- **P0 — Blocker:** Prevents safe operation/validation or can leave accepted exchange exposure untracked or unprotected. Resolve before a Testnet unattended run.
- **P1 — High:** Can materially invalidate account state, risk controls, fills, or operator actions. Resolve before extended Testnet soak.
- **P2 — Medium:** Reliability, security, observability, testing, and accounting quality issues. Resolve before treating the system as operationally mature.
- **P3 — Low:** Maintainability/documentation improvements that reduce future defect risk.

Priority is about engineering order, not a probability estimate of loss. All priorities assume the currently configured paper/Testnet mode.

## P0 — blockers

### F-01. Account-authentication diagnosis is wired to the wrong CCXT response fields

**Evidence:** `exchange.py:22-64`, `exchange.py:171-199`. The handler accepts a response body and an integer HTTP status, but its call sites pass `lastJsonResponse`, `lastHttpResponse`, and `exchange.status`. In the Python CCXT version inspected locally, the actual response properties are snake case (`last_json_response`, `last_http_response`); camel-case attributes are separate and remain unset. `exchange.status` is CCXT's exchange service-status object/dict, not the HTTP response status code. `enableLastJsonResponse` is false by default, while the raw HTTP response is the field normally retained. As a result, the safe diagnostic generally has no parsed Binance code/status and emits generic text. This matches the last observed Railway logs, which still did not identify the rejection reason.

**Impact:** The operator cannot distinguish a malformed key, invalid signature, revoked permission/IP restriction, timestamp issue, or another endpoint rejection. This has already prolonged an unresolved account-access failure.

**Required remediation:** Capture response metadata using the installed CCXT Python API correctly. Prefer enabling/parsing the JSON response when supported and safely capture numeric HTTP status at the transport boundary if needed. Surface only an allowlisted Binance code, HTTP status, and mapped non-sensitive explanation. Never log full response bodies, signed URLs, request headers, key values, or signatures. Pin the CCXT version used by the deployment so the diagnostic contract is tested against it.

**Acceptance criteria:** Unit tests exercise Binance `-2014`, `-2015`, `-1022`, `-1021`, HTTP 401/403, malformed/non-JSON responses, and an unrelated network failure; tests prove no credentials, signatures, full URLs, or unfiltered response bodies can appear in output. On Railway, a fresh balance request produces a specific safe diagnosis, or a successful balance snapshot. Do not mark the account issue solved merely because the web process is healthy.

### F-02. A confirmed buy can be left unprotected if local position persistence fails

**Evidence:** `bot_engine.py:486-646`, especially the post-fill sequence around `open_position`, the `_is_halted` check, and `_place_and_persist_native_stop`; `risk_manager.py:381-404`.

**Impact:** After Binance accepts and fills a buy, `RiskManager.open_position()` returns the in-memory position even if `_save_positions()` failed. The caller sees the halt and returns **before** placing the exchange-native stop. The exchange can therefore hold a real Testnet asset while the durable local position is missing and no protective stop exists. Reconciliation is not a substitute for protection after the buy has already occurred.

**Required remediation:** Design an explicit entry state machine that accounts for the exchange fill first, records enough recoverable order/fill data before any further action, and has a safe recovery path if persistence or stop placement fails. The code must make best effort to protect/close the confirmed fill and must halt further entries; it must not silently abandon an unprotected position. State that cannot be made atomic across the exchange and SQLite must be reconciled on restart using the order journal and exchange state.

**Acceptance criteria:** Fault-injection tests cover database failure after exchange acceptance, after local position write, during stop submission, and during stop-reference persistence. In each case assert the exact remote order/position, local position, unresolved intent, halt state, and operator recovery instructions. A test must prove there is no path that returns with a filled untracked quantity and no open protective order without an explicit unresolved incident state.

### F-03. Missing/incomplete buy fee data halts before recording/protecting the filled asset

**Evidence:** `bot_engine.py:421-444` and `bot_engine.py:595-605`.

**Impact:** `_net_base_filled()` returns `None` when CCXT's order response has no fee array and no Binance `info.fills`. The caller then halts and returns before recording a position or placing its stop, even though the order may have filled. This is a particularly dangerous failure mode because the bot explicitly treats fee ambiguity as a stop condition but does not first preserve/protect the acquired quantity.

**Required remediation:** Treat missing fee/fill data as an unresolved, potentially filled order, not as “no position.” Query authoritative order/trade/account data using the stable client/order ID, account for commission asset and quantity, and establish protection or an explicit operator-managed state. Keep entry blocked until the fill and balance reconcile.

**Acceptance criteria:** Tests simulate a full buy with omitted `fee`, omitted `fees`, omitted `info.fills`, fee in base, quote, and third asset; partial fill; and failed trade lookup. The bot must not report no position or allow another entry while exposure is uncertain.

### F-04. Exit paths cancel the protective stop before the replacement sell is confirmed

**Evidence:** `bot_engine.py:649-710` and `bot_engine.py:715-770`; `exchange.py:370-380`.

**Impact:** Strategy exits, take-profit exits, trailing/local-stop exits, and related market exits cancel the exchange stop first. If the market sell then fails, times out, is unknown, is rejected, or cannot be fully filled, the bot halts but the original protective stop may already be gone. A halted process does not restore protection. `cancel_order()` returns `True` whenever the cancel call returns without an exception; it does not independently read the final order status/balance before the bot proceeds.

**Required remediation:** Build a race-aware cancel/replace protocol. Reconcile the stop's final state and current free/locked base quantity before submitting the market exit. If the exit is not confirmed, restore/verify a correctly sized stop for remaining inventory or enter an explicit urgent operator recovery state that records exact exposure. Never assume “cancel request returned” means the order was still open and no fill raced with cancellation.

**Acceptance criteria:** Simulate stop filled during cancel, cancel timeout but exchange canceled, cancel rejected, market sell timeout/unknown, partial sell, and database outage after cancel. No scenario may leave unsold bot inventory while local code believes there is a valid stop when none is open. Verify Binance Testnet stop behavior once authenticated Testnet access is available.

## P1 — high-priority safety, correctness, and validation

### F-05. Break-even and trailing thresholds update only local state, not the Binance stop

**Evidence:** `risk_manager.py:508-569` updates `stop_loss_price` and persists it; `bot_engine.py:227-248` places a native stop once; no amend/replace is invoked when the local stop moves.

**Impact:** The exchange-side stop remains at its original trigger while the dashboard/local state may show a tighter break-even or trailing stop. If the process is unavailable, the exchange only has the original stop. This is consistent with some README caveats, but the names and UI can still make the protection look more synchronized than it is.

**Required remediation:** Either synchronize every local stop adjustment with a safe exchange-side stop amend/replace and reconcile old/new order IDs, or make the distinction explicit everywhere and label break-even/trailing exits as process-dependent. The agent must not claim a “trailing exchange stop” unless Binance confirms the updated order.

**Acceptance criteria:** Tests prove local and exchange stop state remain aligned after each move, including replace failure and restart, or UI/API/docs clearly show “exchange initial stop” versus “bot-monitored trailing exit.” Test trigger/replace semantics on Spot Testnet with a valid account.

### F-06. One synchronous CCXT instance is shared across concurrent threads without a request lock

**Evidence:** `dashboard.py:134-158` runs `bot.run_cycle()` in an executor; `dashboard.py:170-200` independently calls balance and ticker methods in executors; `dashboard.py:305-329` requests candles in an executor. All share `self.bot.exchange.exchange`. CCXT's Python `throttle()` implementation inspected locally calculates elapsed time from `lastRestRequestTimestamp`, sleeps, and updates shared timing without a mutex.

**Impact:** Dashboard refreshes, chart fetches, and the trading worker can issue requests concurrently through mutable exchange/rate-limiter state. This can defeat rate pacing, create races in response metadata used by F-01, increase `429/418` risk, or interleave market/order/account operations.

**Required remediation:** Serialize all CCXT operations through a single exchange-I/O worker/queue or a lock covering the full CCXT request and response-metadata read. Prefer a single request scheduling policy that gives emergency/reconciliation operations priority. Do not solve this by creating multiple CCXT instances with the same keys/IP; CCXT documents one limiter per instance.

**Acceptance criteria:** Concurrency tests issue simulated status/candle/trade calls while a bot cycle runs and assert one-at-a-time access (or a properly coordinated async client), bounded request rates, and correct response metadata association. Include 429, timeout, and cancellation behavior.

### F-07. Ticker failure becomes numeric zero and the dashboard treats it as a real price

**Evidence:** `exchange.py:290-301` returns `{}` on ticker error, then `get_current_price()` returns `0.0`; `dashboard.py:199-221` stores that value and calculates position P&L/net worth from it. The overview displays returned values without a stale-price state.

**Impact:** A temporary public-market-data outage can display a position at `$0.00`, fabricate a near-total loss, and distort tracked value/P&L. The risk manager correctly halts on an invalid open-position price, but the user-facing dashboard can still misrepresent the account.

**Required remediation:** Represent unavailable/stale prices as `None` plus a source timestamp/error, never as zero. Show P&L/net worth as unavailable when a required mark is missing or stale. Ensure a price-fetch failure is visible in status and never participates in drawdown/accounting calculations.

**Acceptance criteria:** Dashboard and API tests cover missing ticker, `last=None`, zero/negative/infinite prices, stale response, and partial pair failures. No unavailable price is rendered as `$0.00` or counted as a loss.

### F-08. Reconciliation and equity valuation cover configured assets, not the whole account

**Evidence:** `bot_engine.py:288-365` creates a baseline for configured base assets and validates those balances; `bot_engine.py:978-997` values USDT plus configured base assets. The audit documents this gap; the dashboard calls its metric “Tracked value.”

**Impact:** Non-configured holdings, third-asset commissions/rebates, and other account inventory/liabilities do not enter equity or drawdown calculations. A configured symbol's seed balance is baselined, but changes outside tracked bot activity halt reconciliation without an adoption/accounting workflow. A safe halt is better than silently trading, but risk thresholds and displayed equity are not complete account equity.

**Required remediation:** Specify an account ownership model for Testnet: dedicated empty bot account or explicit baseline snapshot per asset; ingest all asset balances/trades/fees; reconcile external changes. Until this exists, call the metric bot-tracked value and do not represent it as total account equity. Risk breakers must use the defined, complete valuation universe or be clearly scoped/disabled as incomplete.

**Acceptance criteria:** Tests cover pre-seeded assets, unconfigured assets, base-asset fee, quote-asset fee, third-asset fee, deposit/withdrawal/manual order, locked funds, and missing mark prices. Reconciliation has an explicit safe operator path for every mismatch.

### F-09. Testnet reset can invalidate durable local state, with no operator reset/recovery runbook

**Evidence:** Core positions/risk/order state survives on the Railway volume (`state_store.py`); Binance Spot Testnet periodically resets virtual balances, orders, and history. The bot's persisted stop IDs/positions will then be stale and reconciliation will halt.

**Impact:** A Testnet reset or lost Testnet orders can leave the persistent bot state inconsistent with the exchange. Blindly clearing SQLite would erase unresolved order intent and loss/halt history; continuing without reconciliation could misstate exposure.

**Required remediation:** Document and implement a Testnet-only reset/recovery process that first snapshots the database, queries/exports exchange state, records operator acknowledgement, reconciles each position/order, and only then resets the appropriate state with an audit event. Do not automatically wipe state or auto-adopt balances.

**Acceptance criteria:** Simulated account reset produces a persistent, understandable halted state and a documented recovery procedure. Preserve an export of pre-reset positions/orders/events. Verify the procedure using a controlled Testnet account after valid API access is established.

### F-10. Backtest/replay can materially misstate strategy performance

**Evidence:** `main.py:130-238`.

Specific limitations:

- It evaluates a signal using a candle window including that candle's close, then enters/exits at the same close; this assumes an execution price that was not available before the signal decision.
- Stop-loss/take-profit checks use closing prices, not candle high/low paths, so intrabar triggers/gaps are not modeled.
- No fees, slippage, partial fills, spread, market depth, latency, or fee asset effects.
- Symbols are simulated sequentially with one shared `balance`, rather than in timestamp-synchronized portfolio time; it is not a multi-asset portfolio simulation.
- Only a short, recent sample (up to 500 rows per pair) is requested; no dated history, walk-forward split, regime coverage, or parameter sensitivity is implemented.
- It can use Spot Testnet data. Binance documents Testnet virtual funds and periodic resets; it is not a production-market historical dataset suitable for proving edge.

**Impact:** Printed win rate/return can be mistaken for evidence of profitability despite explicit warning text. The current code should remain labeled exploratory and must not be used as a launch gate.

**Required remediation:** Replace the replay with timestamp-aligned portfolio simulation using next-bar/open or explicit execution model, intrabar ambiguity policy, fees/slippage, partial fills, delisted/missing data handling, benchmark, out-of-sample/walk-forward evaluation, parameter sensitivity, and reproducible datasets. If not implemented, hide headline “results” from ordinary users and retain a strong exploratory disclaimer.

**Acceptance criteria:** Deterministic fixtures prove no same-close lookahead; multi-symbol cash/positions are synchronized; transaction costs reduce results; stop/target intrabar ordering is conservative; train/test windows do not overlap; output includes dataset/date/config hash and performance with costs. No result is called validated without statistical review.

### F-11. Realized P&L, accuracy, and risk loss omit full exchange fees and execution costs

**Evidence:** `risk_manager.py:453-505` computes gross P&L as `(exit-entry)*amount`; `bot_engine.py:421-444` only removes base-asset fee from bought quantity; sell fees/quote fee/third-asset commissions are not fully netted; `dashboard.py:230-247` summarizes the JSONL trade log.

**Impact:** Win rate, daily loss, consecutive losses, and P&L can be too optimistic. A fee can turn a small apparent winner into a net loser. The console/dashboard/trade log can diverge because they are separate stores.

**Required remediation:** Build a canonical fills/fees ledger from exchange trade fills. Record fee cost, currency, fill IDs, quote amount, commissions/rebates, and all partial fills on both sides. Compute realized net P&L and risk limits from reconciled net results. Clearly separate estimated from exchange-reconciled values.

**Acceptance criteria:** Exact fixture tests cover base/quote/third-asset fees, multiple partial fills, rebates, zero fee, missing fee history, and rounding. Dashboard totals match the canonical ledger and reconciled exchange trades.

### F-12. Bot starts with entries active and immediately runs its worker after deployment

**Evidence:** `risk_manager.py:347` defaults `entry_mode()` to `ACTIVE`; `dashboard.py:117-120` starts the worker; `dashboard.py:134-158` enters its loop immediately, and `bot_engine.py:965-1017` can submit signals after reconciliation.

**Impact:** Once Testnet credentials become valid and safety checks pass, a redeploy/restart can resume new entries without the operator explicitly pressing Resume/Arm. This may be intended for an autonomous paper bot, but should be an explicit operational policy rather than an accidental default. For a future live-capable system it is unacceptable without an explicit arm/kill control.

**Required remediation:** Introduce a durable startup policy such as `START_PAUSED=true` or require explicit operator arming after a deploy/restart, while keeping existing positions monitored and protected. If autonomous Testnet start is retained, make that behavior conspicuous and separately configurable.

**Acceptance criteria:** Tests verify fresh install, restart, restart with an open position, and deploy after pause. No new entry occurs until the configured startup policy allows it; exits and protective monitoring remain active.

### F-13. Emergency stop can cancel manual/user orders in addition to bot orders

**Evidence:** `bot_engine.py:797-836` fetches the account-wide open-order list and cancels every returned order, then closes only local bot-tracked positions. It does not filter cancellations to stored bot client IDs.

**Impact:** On a shared Spot Testnet account, invoking emergency stop may cancel manually placed Testnet orders unrelated to the bot. Conversely, inventory/positions not represented by bot state are not automatically closed. The current dashboard copy says it cancels open exchange orders, but the operation's account-wide scope deserves explicit review.

**Required remediation:** Prefer a dedicated exchange account/API identity for the bot. Otherwise define emergency stop scope precisely: cancel only identified bot-owned orders by client-ID namespace and separately report unrelated orders; provide an explicit deliberate “cancel all account orders” action if that behavior is still needed. Never market-sell untracked assets automatically.

**Acceptance criteria:** Tests seed bot and manual open orders and prove only the documented set is affected. The UI confirmation enumerates scope and reports partial failures without claiming a full account close.

### F-14. Protection-order behavior and parameters are not proven against Binance Spot Testnet

**Evidence:** `exchange.py:231-243` calls CCXT `create_order(..., 'market', 'sell', ..., {'stopLossPrice': ...})`; `bot_engine.py:227-248` assumes an `open` order and persists it. The last observed Testnet auth failure prevents this path from being exercised.

**Impact:** Source-level mocks only prove a function call shape, not that the selected CCXT version maps the parameters into a valid Spot Testnet order, that the order remains open, or how triggers/fills/cancel races behave.

**Required remediation:** After account auth is fixed, perform a controlled Testnet-only protocol rehearsal with minimum permitted quantities. Capture redacted request/response metadata and verify accepted order type, stop trigger, exchange state, cancel/replace, stop fill, partial fill, and restart reconciliation. Do not use production funds or treat Testnet as evidence of price quality.

**Acceptance criteria:** A reproducible end-to-end runbook and redacted exchange evidence for each order lifecycle case; tests remain deterministic with mocks in CI. Failure leaves the bot halted and records specific operator steps.

## P2 — operational resilience, security, and test quality

### F-15. Dashboard polling competes with the trader and performs repeated/unbounded work

**Evidence:** `templates/overview.html:13,28` polls status every 10 seconds and candles every 30 seconds; positions/history pages also poll status. `dashboard.py:170-247` performs a private balance call, ticker calls per configured symbol, and reads/parses the entire `trades.jsonl` for every status response; candle endpoint calls CCXT separately.

**Impact:** Polling increases API usage, shares the concurrent CCXT instance (F-06), and reads an ever-growing trade file into memory and sends the complete history with status. Multiple tabs/users amplify this. This can harm rate budget and dashboard responsiveness.

**Required remediation:** Keep status responses bounded and derived from a background snapshot/cache with timestamps; paginate trades/history; avoid a private balance request on every UI refresh. Coordinate all exchange reads with the exchange I/O queue. Add explicit stale-data indicators.

**Acceptance criteria:** Load tests with multiple dashboard tabs/users show bounded memory, request count, response size, and no rate-limit violations in a mocked exchange.

### F-16. Health/readiness and dead-man monitoring are not explicit

**Evidence:** Routes are `/`, `/positions`, etc., all behind Basic Auth in `dashboard.py:30-44`; `railway.json` has no healthcheck path, and there is no independent heartbeat/alert if the container exits, stalls, or cannot reach Binance.

**Impact:** “Railway Online” only proves a process is running, not that account reconciliation or market data works. The last incident illustrates this: app process healthy while private account access failed. No external monitor is present to alert if the worker dies or hangs.

**Required remediation:** Add safe liveness/readiness semantics: liveness independent of exchange, readiness reports bounded internal health without exposing secrets/account data; configure Railway health check appropriately. Add an external heartbeat/dead-man alert independent of the worker (Telegram alone from the same process is insufficient).

**Acceptance criteria:** Simulate process down, event loop responsive but CCXT unavailable, worker exception, stale cycle, and auth rejection; monitoring distinguishes them and alerts without secret leakage.

### F-17. SQLite storage and other ledgers lack migration/retention/backup controls

**Evidence:** `state_store.py:30-67` creates tables with `CREATE TABLE IF NOT EXISTS` but no `schema_version`/migration system; audit events/order intents are retained indefinitely; trades use a separate append-only JSONL file (`bot_engine.py:84-103`). No application-level backup/export verification is provided.

**Impact:** Schema changes can be difficult to deploy safely; unbounded state/log growth can consume the Railway volume; positions and risk state are in SQLite while human-visible trade records live elsewhere and can diverge. Railway volume snapshots are not a tested database recovery process.

**Required remediation:** Add versioned migrations, bounded/archived logs, a canonical trade/fill ledger, automated integrity/backup verification and restore procedure. Keep one replica unless state moves to a server database plus distributed coordination.

**Acceptance criteria:** Migration tests from every supported prior schema; restore test retains positions, halts, intents, fills, and event history; disk-full and corrupt database tests halt safely and raise operator alerts.

### F-18. Automated tests are unit-heavy and do not cover the live application boundary

**Evidence:** `tests.py:1-405` has a single unittest module with mocked Binance/exchange behavior and strategy/risk unit tests. No integration/browser/API-auth test suite, CI workflow, static type/lint configuration, or verified Testnet run record is present in the repository file list reviewed.

**Impact:** Mocks do not validate CCXT parameter translation, actual Binance Testnet order states, aiohttp middleware/routes, dashboard rendering, same-origin checks behind Railway proxy headers, deployment settings, or the full exchange-to-DB lifecycle. High-impact gaps in F-01–F-14 can remain green.

**Required remediation:** Organize tests into unit, component, and deterministic integration layers; add API auth/CSRF, state recovery, concurrency, account snapshot, order lifecycle, and dashboard tests. Add CI for syntax, tests, dependency audit, and build. Keep external Testnet smoke tests opt-in and separately documented.

**Acceptance criteria:** CI runs on every PR/push; failure-injection matrix covers all P0/P1 cases; test output records exact Python/CCXT/dependency versions. No CI path can submit orders to production.

### F-19. Dependency versions are open-ended and deployment is not reproducible

**Evidence:** `requirements.txt` uses lower bounds only (`ccxt>=4.5.6`, etc.); no lock/constraints file or hashes. Docker base is `python:3.11-slim` without digest pin; unused or apparently unused runtime packages include APScheduler and websockets (`rg` found no use in current application code).

**Impact:** Rebuilds can install different CCXT/API behavior without a source change, directly affecting authentication diagnostics, stop-order parameter mapping, and rate limiting. Broader dependency surface also increases supply-chain exposure.

**Required remediation:** Pin and lock tested dependency versions (with hashes where practical), update deliberately with CI, minimize unused packages, and pin base image by controlled release/digest policy. Document how Railway builds (NIXPACKS vs Dockerfile) so the deployed environment is reproducible.

**Acceptance criteria:** Two clean builds from the lock produce the same dependency versions; CI validates lock consistency; tested CCXT response/stop-order behavior maps to the pinned version.

### F-20. Authentication/security controls need defense-in-depth

**Evidence:** `dashboard.py:30-44` uses a single static Basic Auth username (`operator`) and `DASHBOARD_TOKEN`; no login rate limiter, IP access policy, session expiry/MFA, or lockout is implemented. Security response headers are added only after the handler returns normally, so unauthorized/error responses bypass them. No CSP is present; current overview uses inline script.

**Impact:** The dashboard is a public web control plane (including emergency-stop and Testnet smoke-trade routes). A high-entropy secret and HTTPS are useful but do not provide rate limiting or independent access controls. Lack of CSP reduces browser-side defense in depth.

**Required remediation:** Consider Railway/network-level access control (e.g., trusted identity proxy/IP restriction), rate-limit authentication failures, add consistent headers on all responses, and implement a CSP compatible with the pages (move inline JS to static assets or use a per-response nonce). Keep same-origin validation on every mutating route. Never weaken these controls to make Testnet troubleshooting easier.

**Acceptance criteria:** Automated tests verify unauthenticated requests cannot read data or mutate bot state; origin-mismatched/missing Origin requests are rejected; response headers are applied to 2xx/4xx/5xx; brute-force protections and CSP are verified in deployment.

### F-21. Strategy “confidence” is a heuristic score, not a probability, and votes are correlated

**Evidence:** `strategies.py:60-99`, `strategies.py:124-180`, `strategies.py:215-256`, `strategies.py:287-493`.

**Impact:** Confidence calculations use EMA gap, RSI depth, and raw MACD histogram scaling, then the combined strategy averages the scores and uses a `0.5` threshold. These are not calibrated probabilities. EMA, RSI, and MACD all derive from the same OHLCV price series, so 2-of-3 agreement is not independent evidence. The comments call the combined approach “Institutional-grade” and “LOWEST / CONSERVATIVE” without validation.

**Required remediation:** Rename confidence to a heuristic score or remove it until calibrated; document all strategy rules and thresholds. Evaluate signal quality only in a valid cost-aware out-of-sample framework (F-10). Remove claims implying safety/profitability from code comments/UI/docs.

**Acceptance criteria:** Signal score semantics are explicit; no score is presented as a win probability absent calibration; parameter/regime sensitivity is reported and reproducible.

### F-22. Operational configuration can fail unclearly and has ambiguous startup semantics

**Evidence:** `config.py:17-64` parses many environment variables at import time with direct `float()`/`int()`; malformed values can abort import before aggregated validation. The dashboard worker uses `INTERVAL_SECONDS`, while CLI interval is separate. `.env.example` says to change `ALLOW_LIVE_TRADING` only after checklist, although the code still refuses live trading even when true. Some config limits and defaults are documented in different places.

**Impact:** Railway mistakes can crash startup with low-actionability errors or lead the operator to believe a setting changes behavior when it does not. Tests that mutate environment after import may not reflect a fresh deployment.

**Required remediation:** Centralize typed config parsing with named errors, distinguish unsupported settings from active controls, add validated worker interval and startup mode, and keep Railway/README/.env example/audit values aligned. State clearly that live stays unavailable in this build.

**Acceptance criteria:** Table-driven config tests cover missing, malformed, nonfinite, out-of-range, and contradictory env values; app returns a clear startup diagnostic without logging secrets.

## P3 — maintainability and operator experience

### F-23. Core orchestration is too large and boundaries are blurred

**Evidence:** `bot_engine.py` is 1,075 lines; `risk_manager.py` 633; `strategies.py` 497; `dashboard.py` 422. `TradingBot` coordinates signal evaluation, exchange orchestration, reconciliation, test trades, alerting, and control semantics. Risk manager combines position, P&L, persistence, and halt logic.

**Impact:** Changes to order recovery, risk, and dashboard behavior are difficult to isolate/review and regressions are harder to reason about.

**Required remediation:** Refactor only after stabilizing P0/P1 semantics. Suggested boundaries: exchange gateway/serialized request worker; order lifecycle/reconciler; portfolio/fill ledger; risk policy; strategy interface; dashboard API; presentation. Preserve behavior with characterization tests before splitting modules.

**Acceptance criteria:** Modules have narrow interfaces, no circular dependency, and each critical state transition has one owner and tests.

### F-24. Duplicate/unused templates and UI strings can drift

**Evidence:** Dashboard routes serve `overview.html`, `positions.html`, `history.html`, `strategy.html`, and `glossary.html`; `templates/index.html` and `templates/terms.html` are present but not referenced by the current route handlers. Several dashboard pages embed large single-line JavaScript blocks.

**Impact:** Unused files can be mistaken for active pages and create divergent fixes/copy. Inline scripts are hard to lint/review and complicate CSP.

**Required remediation:** Confirm no external deployment path needs the legacy templates, then remove or label them as archived. Extract page scripts into versioned static assets and format HTML/JS for review.

**Acceptance criteria:** Route/template mapping is documented and each served page has lint/syntax coverage; no obsolete duplicate page is mistaken for production UI.

### F-25. User-facing performance metrics can overstate completeness

**Evidence:** `dashboard.py:209-247` combines local trade-log SELL P&L and current position estimates, uses configured `INITIAL_BALANCE` default `$10,000`, and counts all SELL records for win-rate/accuracy. It does not include complete exchange fees, account-wide inventory, or a verified external trade history.

**Impact:** “Recorded P&L,” accuracy, and tracked value can be mistaken for audited account performance. The UI includes some caveats, but the values are not a reconciled statement.

**Required remediation:** Rename metrics to clearly indicate estimates and source/coverage, show last reconciliation and mark timestamps, and derive official results from the canonical fill ledger (F-11). Do not imply tax/accounting-grade performance.

**Acceptance criteria:** API field definitions and UI labels state inclusions/exclusions; unavailable or incomplete source data produces “Unavailable / incomplete,” not a number that appears exact.

### F-26. Testnet itself is a protocol sandbox, not a market-quality or strategy simulator

**Evidence:** `.env.example` labels Testnet as having independent prices/order book; Binance says Spot Testnet assets are virtual, can only be used there, and the environment can reset periodically. Order filters/rate limits are generally similar, but market data/liquidity is not production execution.

**Impact:** A successful Testnet order verifies a subset of API wiring and state transitions. It does not prove slippage, realistic fill quality, strategy performance, recovery under production volatility, or live account safety.

**Required remediation:** Keep Testnet rehearsal evidence separate from strategy research. Use historical production-market data for backtesting and a controlled shadow/monitoring stage for operational behavior. Do not change to Demo or live merely to clear this backlog; this task is scoped to the current Testnet configuration.

**Acceptance criteria:** Operator docs clearly state what a Testnet smoke trade proves and does not prove; results from Testnet are never presented as profitability evidence.

## Existing strengths to preserve

1. Live connection initialization is explicitly refused (`exchange.py`); do not remove this guard.
2. API/dashboard routes require configured Basic Auth and mutating routes enforce a same-origin check (`dashboard.py`). Preserve and strengthen it.
3. Account/API failures are fail-closed; missing balance is not intentionally converted to `$0.00` for account availability.
4. Order intent is journaled with deterministic client IDs before submission (`bot_engine.py`, `state_store.py`). Do not add blind retries after ambiguous create-order results.
5. Cycle-level locking, durable EXIT_ONLY mode, reconciliation halts, and explicit emergency-stop review exist. Preserve safety latch semantics.
6. Market entry preflight checks precision, exchange limits, spread, and visible depth (`bot_engine.py`). Expand it to complete filters and executable-cost checks rather than deleting it.
7. Closed-candle filtering and malformed OHLCV rejection exist (`exchange.py`). Keep signal inputs closed-bar only.
8. Spot only is configured; margin, futures, leverage, and withdrawals are not part of this code path.
9. Project documentation openly says live trading is not ready. Keep that message even after code improvements.

## Recommended implementation sequence for the receiving agent

### Phase A — restore diagnosable Testnet account access (no orders)

1. Fix F-01 and add redaction tests.
2. Add a read-only authenticated account diagnostic that reports balance/open-order access separately, returns a sanitized error code, and never submits or cancels an order.
3. Verify Railway variable names/mode without printing values; require the operator to enter/rotate secrets directly in Railway if the diagnosis supports it. Never request secrets in chat or logs.
4. Continue to fail closed until both balance and order reconciliation succeed.

### Phase B — make order lifecycle failures safe under injection

1. Resolve F-02 through F-05 and F-13 with explicit state transitions and post-condition checks.
2. Add deterministic mock/fault-injection coverage for accepted/unknown/partial/fee-missing/cancel-race/persistence-failure scenarios.
3. Ensure no automatic recovery makes new entries while an order, position, or protective stop is ambiguous.

### Phase C — isolate exchange I/O and truthful portfolio status

1. Resolve F-06 through F-09 and F-15 through F-17.
2. Distinguish process liveness from exchange readiness; add heartbeats, bounded status snapshots, and operator runbooks.
3. Keep a single replica while SQLite remains the state store.

### Phase D — validate paper behavior and only then improve strategy research

1. Perform Testnet lifecycle rehearsal (F-14), explicitly noting Testnet limitations (F-26).
2. Replace exploratory replay or keep it clearly non-evidentiary (F-10).
3. Reconcile fees/P&L before evaluating win rate or risk breakers (F-11, F-25).
4. Measure extended uptime and failure recovery on the exact deployed Testnet commit. There is no approval in this handover to enable live trading.
