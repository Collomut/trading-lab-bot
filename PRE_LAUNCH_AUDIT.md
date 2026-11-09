# Pre-launch readiness review

**Decision: live trading is blocked.** The build rejects live exchange initialization in `ExchangeConnector._create_exchange`; setting `PAPER_TRADING=false` does not enable live orders. This is intentional. “PASS” below means a safeguard exists in this build, not that the strategy is profitable or production-ready.

## Software reliability and security

| Criterion | Status | Evidence / remaining work |
|---|---|---|
| Stable client order ID and pre-submit journal | PARTIAL | `TradingBot._client_order_id` and `_submit_market_order` persist intent before submit and query by client ID after an ambiguous response. Unresolved outcomes halt; safe automatic retry is deliberately absent. |
| Crash recovery / cold-start reconciliation | PARTIAL | `StateStore` persists order intents, state and positions; `TradingBot._reconcile_exchange_state` captures a configured-asset baseline, checks subsequent balances/open orders, and blocks unresolved intents. Unresolved cases need operator reconciliation; unconfigured assets are not reconciled. |
| Order lifecycle and partial fills | PARTIAL | `StateStore.ORDER_TRANSITIONS` records lifecycle. A terminal partial buy is tracked and protected by a native stop, then halted for review. Partial sells halt for manual reconciliation; fees and realized partial P&L are not fully accounted. |
| Orphan orders / self-trade prevention | PARTIAL | Any unrecognized open order blocks trading. Market orders use the exchange's symbol/account STP policy instead of forcing a mode unsupported by some symbols. Orphan orders are not automatically cancelled by routine reconciliation. |
| Concurrent state changes | PARTIAL | `TradingBot.run_cycle` serializes cycles in-process and SQLite serializes writes. No WebSocket consumer exists; this is not cross-replica coordination. |
| Decimal arithmetic / market filters | PARTIAL | Entry amount precision, limits, spread and visible depth are checked before orders. Strategy/risk/PnL still use floats; maximum cost, exact fee and all exchange filters are not modeled end-to-end. |
| Rate limits and network recovery | PARTIAL | CCXT `enableRateLimit` is enabled; there is no emergency-priority budget or explicit 429/418 circuit/backoff policy. |
| API-key permissions / secret handling | PARTIAL | Secrets come from environment variables and `.env` is ignored. Withdrawal/transfer restrictions and IP allowlisting must be configured and verified at the exchange. |
| Database failure while a position is open | CRITICAL | A persisted Binance stop can protect an already accepted order if the process or database fails, but stop placement and position persistence are not atomic. Failure in that gap or loss of exchange connectivity remains unprotected. |
| Immutable forensic event logging | PARTIAL | SQLite records order intents and selected audit events; logs are not immutable/rotated and do not retain every raw request, response, latency, and decision snapshot. |

## Trading validity and execution

| Criterion | Status | Evidence / remaining work |
|---|---|---|
| Closed-candle signal inputs | PASS | `filter_closed_ohlcv` drops incomplete/malformed candles; `TradingBot.analyze_market` analyzes a completed candle once. |
| Out-of-sample edge and regime robustness | CRITICAL | The replay is exploratory; no untouched multi-regime/out-of-sample study or parameter sensitivity evidence exists. |
| Fees, funding and realistic slippage in backtest | CRITICAL | No validated net-of-cost performance results. |
| Spread and book-depth entry checks | PARTIAL | `TradingBot._handle_buy_signal` rejects wide spreads and insufficient visible asks within a slippage bound. No executable VWAP model or adverse-selection study. |
| Exchange maintenance, rejected exit and wick fallback | PARTIAL | A Binance Spot `STOP_LOSS` market order is placed for each tracked position after entry; software trailing/take-profit exits still depend on the running process, and no independent fallback loop or exchange-side OCO exists. Testnet acceptance and fill behavior remain unverified. |
| Exchange-native protective stop | PARTIAL | Native stop placement, durable stop-ID storage, reconciliation and stop-fill recognition are implemented. The stop is not atomic with the buy, its testnet behavior has not yet been verified, and there is no exchange-side take-profit/OCO. A stop-market order can fill materially beyond its trigger in a fast market. |
| Per-trade hard risk ceiling | PASS | Risk config caps `MAX_RISK_PER_TRADE` at 2%; sizing is rechecked after exchange rounding and position exposure is capped. This is a modeled threshold, not a guaranteed loss limit. |
| Daily/rolling equity drawdown | PARTIAL | `RiskManager.monitor_equity` persists daily starting equity and a rolling peak using bot-managed USDT cash plus tracked positions. Pre-existing baseline coin inventory is excluded so unmanaged Testnet seed assets cannot trigger the bot's drawdown breaker. Reconciliation gates legacy-baseline migration; fees/rebates and assets outside bot-managed positions still need complete accounting before this safeguard can be treated as a full portfolio loss limit. |
| Consecutive-loss pause | PARTIAL | `RiskManager.close_position` persists loss streaks and halts after `MAX_CONSECUTIVE_LOSSES`. Fees and partial exits are not included in realized loss reliably, so this gate needs complete accounting before it can be treated as a pass. |
| Cross-asset correlation cap | PARTIAL | `TradingBot._has_correlated_open_position` estimates aligned closed-candle return correlation and blocks highly correlated entries. Correlation is noisy; there is no portfolio covariance model. |
| Volatility sizing | PARTIAL | ATR influences stop distance and therefore size; no separate volatility shock or portfolio covariance controller. |
| Leverage / liquidation | PASS (not applicable) | The connector is Binance spot only; margin, futures and leverage are not enabled. |

## Operations and accounting

| Criterion | Status | Evidence / remaining work |
|---|---|---|
| Emergency stop that cancels/closes | PARTIAL | The authenticated same-origin control cancels exchange orders, attempts to close bot-tracked positions, then latches a halt. It depends on the same service/API/network, does not terminate Railway, and does not sweep untracked inventory. |
| Exit-only mode | PASS | `/api/exit-only` durably disables new entries; existing tracked positions continue through stop/strategy exit monitoring. `/api/resume` takes the same trading lock and requires fresh successful account reconciliation. A latched emergency-stop halt additionally requires explicit operator review, no open bot positions, and no unresolved orders; other risk halts cannot be cleared through this control. |
| Manual exchange intervention | PARTIAL | Balance/open-order mismatch blocks new orders. Automatic position import/adoption and safe external-close accounting are not implemented. |
| Safe hot reload | CRITICAL | Configuration changes require a restart; in-flight risk limits are not dynamically reconciled. |
| External heartbeat / alert routing | PARTIAL | Optional Telegram notifications exist; no independent dead-man monitor or SMS/phone paging is configured by code. |
| Trade explanation and drift | PARTIAL | Trade logs record signal indicators and fills; no captured order-book snapshot, complete risk calculation, or benchmark-vs-VWAP drift ledger. |
| Fees, tax export and realized net PnL | CRITICAL | Entry tracking halts if base-asset fee data is absent. The ledger does not fully account for all fee currencies/rebates or provide a reconciled tax export. |
| 14-day shadow soak | CRITICAL / NOT EVIDENCED | Requires running the exact deployed build for 14 uninterrupted days and reviewing uptime, memory, orders and reconciliation evidence. It cannot be proven by source changes. |
| Minimum-size burn-in | CRITICAL / NOT STARTED | Requires a separately approved live rollout after all preceding blockers are resolved; this build cannot place live orders. |

## Deployment gates

1. Keep `PAPER_TRADING=true` and `PAPER_ENVIRONMENT=testnet`; use only dedicated Spot Testnet keys. Railway config is set to Testnet, but key validity and authenticated account reconciliation are unverified until the deployed build reports a successful private account snapshot.
2. Mount one Railway Volume at `/app/data`, set `DATA_DIR=/app/data`, and keep exactly one bot replica. SQLite here is a single-worker store, not shared multi-replica storage.
3. Treat the exchange-side stop, full fee reconciliation, complete account-equity valuation, independent kill/monitoring, statistical validation, and 14-day soak as launch blockers. Do not enable real-money trading based on this audit or a successful test suite.

## Evidence index

Line numbers refer to this reviewed build:

- Live refusal: `exchange.py:57` (`ExchangeConnector._create_exchange`). Closed-candle filter: `exchange.py:17` (`filter_closed_ohlcv`).
- Exchange balance/open-order snapshot and market rules: `exchange.py:119` (`fetch_account_snapshot`), `exchange.py:129` (`amount_to_precision`), `exchange.py:133` (`get_market_limits`), `exchange.py:145` (`get_order_book`).
- Durable schema and order transitions: `state_store.py:17` (`ORDER_TRANSITIONS`), `state_store.py:39` (`_initialize`), `state_store.py:81` (`write_positions`), `state_store.py:89` (`create_order_intent`), `state_store.py:109` (`mark_order_applied`).
- Stable IDs, pre-submit journal, reconciliation, fill and entry preflight: `bot_engine.py:166` (`_client_order_id`), `bot_engine.py:171` (`_submit_market_order`), `bot_engine.py:205` (`_reconcile_exchange_state`), `bot_engine.py:259` (`_net_base_filled`), `bot_engine.py:286` (`_handle_buy_signal`), `bot_engine.py:567` (`run_cycle`).
- Risk persistence and entry gate: `risk_manager.py:118` (`_load_daily_state`), `risk_manager.py:145` (`_save_daily_state`), `risk_manager.py:160` (`_save_positions`), `risk_manager.py:182` (`_load_positions`), `risk_manager.py:246` (`can_open_position`), `risk_manager.py:287` (`calculate_position_size`), `risk_manager.py:415` (`check_positions`).
- Entry pause/resume endpoints: `dashboard.py:80` (routes), `dashboard.py:340` (`handle_exit_only`), `dashboard.py:351` (`handle_resume_entries`); serialized resume/reconciliation: `bot_engine.py:824` (`resume_entries`). Risk/config caps: `config.py:68` (`validate`), `config.py:130` (spread bound), `config.py:137` (per-trade risk bound).
