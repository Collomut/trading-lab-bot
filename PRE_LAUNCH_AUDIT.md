# Pre-launch readiness review

**Decision: live trading is blocked.** The build rejects live exchange initialization in `ExchangeConnector._create_exchange`; setting `PAPER_TRADING=false` does not enable live orders. This is intentional. “PASS” below means a safeguard exists in this build, not that the strategy is profitable or production-ready.

## Software reliability and security

| Criterion | Status | Evidence / remaining work |
|---|---|---|
| Stable client order ID and pre-submit journal | PARTIAL | `TradingBot._client_order_id` and `_submit_market_order`; SQLite uniqueness prevents resubmission. An ambiguous outcome is halted, not automatically queried/resolved at the exchange. |
| Crash recovery / cold-start reconciliation | PARTIAL | `StateStore` persists order intents, state and positions; `TradingBot._reconcile_exchange_state` checks authenticated balances, open orders and unresolved intents before each cycle. Unresolved cases need operator reconciliation. |
| Order lifecycle and partial fills | PARTIAL | `StateStore.ORDER_TRANSITIONS` records lifecycle. Incomplete fills halt. Protective quantities are not automatically resized and recovered. |
| Orphan orders / self-trade prevention | PARTIAL | Any open order blocks trading; automatic cancellation and exchange STP settings are not implemented. |
| Concurrent state changes | PARTIAL | `TradingBot.run_cycle` serializes cycles in-process and SQLite serializes writes. No WebSocket consumer exists; this is not cross-replica coordination. |
| Decimal arithmetic / market filters | PARTIAL | Entry amount precision, limits, spread and visible depth are checked before orders. Strategy/risk/PnL still use floats; maximum cost, exact fee and all exchange filters are not modeled end-to-end. |
| Rate limits and network recovery | PARTIAL | CCXT `enableRateLimit` is enabled; there is no emergency-priority budget or explicit 429/418 circuit/backoff policy. |
| API-key permissions / secret handling | PARTIAL | Secrets come from environment variables and `.env` is ignored. Withdrawal/transfer restrictions and IP allowlisting must be configured and verified at the exchange. |
| Database failure while a position is open | CRITICAL | A failed state write blocks further orders, but local stop monitoring cannot protect an open position during process, network, or database failure. |
| Immutable forensic event logging | PARTIAL | SQLite records order intents and selected audit events; logs are not immutable/rotated and do not retain every raw request, response, latency, and decision snapshot. |

## Trading validity and execution

| Criterion | Status | Evidence / remaining work |
|---|---|---|
| Closed-candle signal inputs | PASS | `filter_closed_ohlcv` drops incomplete/malformed candles; `TradingBot.analyze_market` analyzes a completed candle once. |
| Out-of-sample edge and regime robustness | CRITICAL | The replay is exploratory; no untouched multi-regime/out-of-sample study or parameter sensitivity evidence exists. |
| Fees, funding and realistic slippage in backtest | CRITICAL | No validated net-of-cost performance results. |
| Spread and book-depth entry checks | PARTIAL | `TradingBot._handle_buy_signal` rejects wide spreads and insufficient visible asks within a slippage bound. No executable VWAP model or adverse-selection study. |
| Exchange maintenance, rejected exit and wick fallback | CRITICAL | Exits are process-monitored market orders using last price. No exchange-side stop, mark-price trigger, or verified fallback loop. |
| Exchange-native protective stop | CRITICAL | No stop is placed at the exchange. The process-managed threshold is not protection against an outage. Live mode stays disabled. |
| Per-trade hard risk ceiling | PASS | Risk config caps `MAX_RISK_PER_TRADE` at 2%; sizing is rechecked after exchange rounding and position exposure is capped. This is a modeled threshold, not a guaranteed loss limit. |
| Daily/rolling equity drawdown | PARTIAL | Daily realized-loss halt exists; unrealized equity and rolling peak-to-trough drawdown are not covered. |
| Consecutive-loss pause / correlation cap | CRITICAL | Neither control exists. |
| Volatility sizing | PARTIAL | ATR influences stop distance and therefore size; no independent volatility shock or portfolio correlation controller. |
| Leverage / liquidation | PASS (not applicable) | The connector is Binance spot only; margin, futures and leverage are not enabled. |

## Operations and accounting

| Criterion | Status | Evidence / remaining work |
|---|---|---|
| Independent kill switch that cancels/closes | CRITICAL | The dashboard has an authenticated, same-origin exit-only pause. It does not cancel exchange orders, market-close positions, or terminate the service. |
| Exit-only mode | PASS | `/api/exit-only` durably disables new entries; existing tracked positions continue through stop/strategy exit monitoring. No UI resume control is provided. |
| Manual exchange intervention | PARTIAL | Balance/open-order mismatch blocks new orders. Automatic position import/adoption and safe external-close accounting are not implemented. |
| Safe hot reload | CRITICAL | Configuration changes require a restart; in-flight risk limits are not dynamically reconciled. |
| External heartbeat / alert routing | PARTIAL | Optional Telegram notifications exist; no independent dead-man monitor or SMS/phone paging is configured by code. |
| Trade explanation and drift | PARTIAL | Trade logs record signal indicators and fills; no captured order-book snapshot, complete risk calculation, or benchmark-vs-VWAP drift ledger. |
| Fees, tax export and realized net PnL | CRITICAL | Entry tracking halts if base-asset fee data is absent. The ledger does not fully account for all fee currencies/rebates or provide a reconciled tax export. |
| 14-day shadow soak | CRITICAL / NOT EVIDENCED | Requires running the exact deployed build for 14 uninterrupted days and reviewing uptime, memory, orders and reconciliation evidence. It cannot be proven by source changes. |
| Minimum-size burn-in | CRITICAL / NOT STARTED | Requires a separately approved live rollout after all preceding blockers are resolved; this build cannot place live orders. |

## Deployment gates

1. Keep `PAPER_TRADING=true`; use only dedicated Demo or Spot Testnet keys. The current service has had Demo authentication failures, so no account is verified until strict balance/open-order reconciliation succeeds.
2. Mount one Railway Volume at `/app/data`, set `DATA_DIR=/app/data`, and keep exactly one bot replica. SQLite here is a single-worker store, not shared multi-replica storage.
3. Treat the exchange-side stop, equity drawdown guard, full fee reconciliation, kill switch, statistical validation, and 14-day soak as launch blockers. Do not enable real-money trading based on this audit or a successful test suite.

## Evidence index

Line numbers refer to this reviewed build:

- Live refusal: `exchange.py:57` (`ExchangeConnector._create_exchange`). Closed-candle filter: `exchange.py:17` (`filter_closed_ohlcv`).
- Exchange balance/open-order snapshot and market rules: `exchange.py:119` (`fetch_account_snapshot`), `exchange.py:129` (`amount_to_precision`), `exchange.py:133` (`get_market_limits`), `exchange.py:145` (`get_order_book`).
- Durable schema and order transitions: `state_store.py:17` (`ORDER_TRANSITIONS`), `state_store.py:39` (`_initialize`), `state_store.py:81` (`write_positions`), `state_store.py:89` (`create_order_intent`), `state_store.py:109` (`mark_order_applied`).
- Stable IDs, pre-submit journal, reconciliation, fill and entry preflight: `bot_engine.py:166` (`_client_order_id`), `bot_engine.py:171` (`_submit_market_order`), `bot_engine.py:205` (`_reconcile_exchange_state`), `bot_engine.py:259` (`_net_base_filled`), `bot_engine.py:286` (`_handle_buy_signal`), `bot_engine.py:567` (`run_cycle`).
- Risk persistence and entry gate: `risk_manager.py:118` (`_load_daily_state`), `risk_manager.py:145` (`_save_daily_state`), `risk_manager.py:160` (`_save_positions`), `risk_manager.py:182` (`_load_positions`), `risk_manager.py:246` (`can_open_position`), `risk_manager.py:287` (`calculate_position_size`), `risk_manager.py:415` (`check_positions`).
- Exit-only operator endpoint: `dashboard.py:80` (route), `dashboard.py:330` (`handle_exit_only`). Risk/config caps: `config.py:68` (`validate`), `config.py:130` (spread bound), `config.py:137` (per-trade risk bound).
