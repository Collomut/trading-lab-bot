# 🤖 Automated Crypto Trading Bot

A cryptocurrency trading research and paper-trading project written in Python.

> ⚠️ **DISCLAIMER:** Trading cryptocurrency involves financial risk. No trading bot can guarantee profits. Past backtest performance does not guarantee future results. Start with **paper trading** to verify real-time performance.

---

## Safety status

This repository is an educational paper-trading project. It is **not ready to place real orders**. Live order placement is deliberately blocked in this build; do not enable real exchange keys. The rules-based strategy and the short sample backtest have not established a profitable edge.

Before exposing the Railway service, set `DASHBOARD_TOKEN` to a long random secret and mount a Railway Volume at `/app/data` with `DATA_DIR=/app/data`. All dashboard routes require HTTP Basic authentication (username `operator`, password the token). Without the token the dashboard returns 503 and the bot configuration validation prevents trading.

Railway files outside a mounted volume are ephemeral, so open-position and risk state can disappear on a redeploy. The volume is required for restart persistence, but it does not make local JSON state transactional or reconcile it with the exchange. Do not treat persistence as proof that positions are safely managed.

## Implemented features (paper trading only)

1. **Macro Trend Regime Filter**: Automatically vetoes BUY signals during downtrends (checks against 50/200-period EMA).
2. **Process-monitored exit thresholds**: The worker checks break-even and trailing thresholds, but these are not exchange-side orders and can fail during downtime or network errors.
3. **ATR-Aware Sizing**: Calculates Average True Range (ATR) to avoid getting shaken out by normal market wicks.
4. **Volume Confirmation**: Rejects fakeout signals during low-volume market dead zones.
5. **Disk Persistence**: Actively writes open positions to `data/open_positions.json`. If your computer restarts, the bot restores its tracking automatically.
6. **Multi-Timeframe Support**: Tested and tuned for both `15m` and `1h` (1-hour timeframe achieved **66.7% win rate** in backtests).
7. **Optional Telegram Alerts**: Sends live buy/sell/stop-loss notifications directly to your phone.

---

## Backtest (exploratory only)

The existing quick backtest is exploratory only. It uses a short recent sample and does not model exchange fees, slippage, partial fills, latency, or out-of-sample walk-forward validation. Its printed returns and win rates are not evidence of a tradable edge and must not be used to justify live trading.

Previous headline values below are retained only as historical project notes; they are not independently verified or suitable for investment decisions:

| Timeframe | Strategy | Win Rate | Net P&L | Key Observation |
|---|---|---|---|---|
| **1h (Recommended)** | Combined + Trend Filter | **66.7%** | **+$46.61 (+0.47%)** | Filtered out noise; locked in gains |
| **15m** | Combined + Trend Filter | 16.7% | -$193.86 (-1.94%) | Strict 1% risk limit protected 98% of capital |

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

# Paper trading (true = fake money, false = real Binance account)
PAPER_TRADING=true

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
- Mount a Railway Volume at `/app/data` and set `DATA_DIR=/app/data`; otherwise local JSON state and logs can be lost on redeploy.
- Use testnet keys only. The app does not reconcile positions with the exchange and does not place exchange-side stop orders.
- Do not grant withdrawal permission to exchange API keys.
