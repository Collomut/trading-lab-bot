# 🤖 Automated Crypto Trading Bot

A cryptocurrency trading research and paper-trading project written in Python.

> ⚠️ **DISCLAIMER:** Trading cryptocurrency involves financial risk. No trading bot can guarantee profits. Past backtest performance does not guarantee future results. Start with **paper trading** to verify real-time performance.

---

## Safety status

This repository is an educational paper-trading project. It is **not ready to place real orders**. Live order placement is deliberately blocked in this build; do not enable real exchange keys. The rules-based strategy and the short sample backtest have not established a profitable edge. Passing software checks is not evidence of a trading edge or safe live execution.

Before exposing the Railway service, set `DASHBOARD_TOKEN` to a long random secret and mount a Railway Volume at `/app/data` with `DATA_DIR=/app/data`. Run exactly one worker/replica: local SQLite state is transactional for one process and is not a distributed database. All dashboard routes require HTTP Basic authentication (username `operator`, password the token). Without the token the dashboard returns 503 and the bot configuration validation prevents trading.

Railway files outside a mounted volume are ephemeral. The bot stores positions, daily risk state, order intents, and audit events in `bot_state.sqlite3` under `DATA_DIR`; mount the volume before starting it. Startup and each trading cycle require authenticated balance/open-order reconciliation. A mismatch or API outage halts all orders. This local state does not create exchange-side protection, and a persistent local stop still cannot execute while the process or network is down.

## Implemented features (paper trading only)

1. **Macro Trend Regime Filter**: Automatically vetoes BUY signals during downtrends (checks against 50/200-period EMA).
2. **Process-monitored exit thresholds**: The worker checks break-even and trailing thresholds, but these are not exchange-side orders and can fail during downtime or network errors.
3. **ATR-Aware Sizing**: Calculates Average True Range (ATR) to avoid getting shaken out by normal market wicks.
4. **Fixed Market-Regime Gate**: The combined strategy pauses new buys when ADX is below `MIN_TREND_ADX` or ATR exceeds `MAX_ATR_PCT`. It does not alter thresholds automatically; exits and SELL signals remain available.
5. **Volume Confirmation**: Rejects fakeout signals during low-volume market dead zones.
6. **Transactional State & Order Journal**: Persists positions/risk state in SQLite and records a deterministic client ID before submitting an order. An ambiguous or unapplied order intent halts trading for reconciliation rather than being retried.
7. **Entry Preflight**: Uses exchange amount precision/limits and checks top-of-book spread and visible depth before entries. Values are configurable with `MAX_SPREAD_BPS` and `MAX_SLIPPAGE_BPS`.
8. **Exit-only control**: The overview page can durably pause new entries while the worker continues monitoring tracked positions.
9. **Multi-Timeframe Support**: Supports common exchange candle intervals; performance is not established across them.
10. **Optional Telegram Alerts**: Sends buy/sell/stop-loss notifications directly to your phone.

---

## Backtest (exploratory only)

The existing quick backtest is exploratory only. It uses a short recent sample and does not model exchange fees, slippage, partial fills, latency, or out-of-sample walk-forward validation. Its printed returns and win rates are not evidence of a tradable edge and must not be used to justify live trading.

No historical headline return or win-rate figure in this repository has been independently reproduced. The command prints an exploratory replay only; do not interpret it as evidence of profitability.
---

## 🚀 Quick Start

### 1. Run Unit Tests
Verify all strategy and risk calculations in under 1 second:
```bash
python tests.py
```

### 2. Check Live Status & Testnet Balance
```bash
python main.py --once
```

### 3. Run Historical Backtest
```bash
# Recommended 1-hour timeframe:
python main.py --backtest --timeframe 1h

# 15-minute timeframe:
python main.py --backtest --timeframe 15m
```

### 4. Start 24/7 Automated Paper Trading
```bash
python main.py --interval 60
```

---

## ⚙️ Configuration (`.env`)

```ini
# Exchange
EXCHANGE=binance
API_KEY=your_key
API_SECRET=your_secret

# Keep paper trading enabled. Select "demo" only after adding DEMO_API_KEY/SECRET.
PAPER_TRADING=true
PAPER_ENVIRONMENT=testnet  # choose demo for Binance Demo Mode
DEMO_API_KEY=your_demo_api_key
DEMO_API_SECRET=your_demo_api_secret

# Trading pairs to monitor
TRADING_PAIRS=BTC/USDT,ETH/USDT,SOL/USDT

# Risk Controls
MAX_RISK_PER_TRADE=1.0    # Max 1% portfolio risk per trade
MAX_OPEN_POSITIONS=3      # Max 3 simultaneous trades
STOP_LOSS_PCT=2.0         # 2% initial stop loss
TAKE_PROFIT_PCT=3.0       # 3% take profit
MAX_DAILY_LOSS_PCT=3.0    # Circuit breaker: halts trading if down 3% in a day
POSITION_SIZE_PCT=10.0    # Max 10% portfolio per trade

# Strategy
STRATEGY=combined         # Consensus of EMA + RSI + MACD + Trend Filter
TIMEFRAME=1h              # 1h recommended for higher signal reliability
LOOKBACK_CANDLES=150

# Optional Instant Alerts
ENABLE_NOTIFICATIONS=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

---

## Live trading

This build intentionally blocks live trading even if `PAPER_TRADING=false`. Do not bypass that guard. A future live-capable version would first need exchange-side protective orders, order/fill reconciliation, durable transactional storage, reliable kill switches, robust out-of-sample evaluation, and a separately reviewed operational runbook. Testnet and paper trading do not guarantee live execution behavior.

- The dashboard requires a high-entropy `DASHBOARD_TOKEN` in Railway; without it, requests fail closed.
- Mount a Railway Volume at `/app/data` and set `DATA_DIR=/app/data`; otherwise SQLite state and logs can be lost on redeploy.
- Keep a single Railway replica. SQLite on a mounted volume is not safe as shared state for multiple bot workers.
- Use dedicated Binance Demo Mode keys when `PAPER_ENVIRONMENT=demo`; use Spot Testnet keys when `PAPER_ENVIRONMENT=testnet`. Never use live account keys for paper mode.
- Demo Mode is a better execution rehearsal than Spot Testnet, but it does not prove profitability or guarantee live fills. The app reconciles configured asset balances and open orders, but it does not place exchange-side stop orders; local stop management is unavailable during an outage.
- Do not grant withdrawal permission to exchange API keys.
