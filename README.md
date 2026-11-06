# 🤖 Automated Crypto Trading Bot

A disciplined, low-risk cryptocurrency trading bot built in Python. Designed for capital preservation, multi-indicator consensus, and automated trade management.

> ⚠️ **DISCLAIMER:** Trading cryptocurrency involves financial risk. No trading bot can guarantee profits. Past backtest performance does not guarantee future results. Start with **paper trading** to verify real-time performance.

---

## 🌟 Upgraded Production Features

1. **Macro Trend Regime Filter**: Automatically vetoes BUY signals during downtrends (checks against 50/200-period EMA).
2. **Break-Even & Trailing Stop-Loss**:
   - Once a position gains **+1.5%**, the stop-loss automatically moves up to the entry price (Zero downside risk).
   - As price continues upwards, a trailing stop locks in accrued profits.
3. **ATR-Aware Sizing**: Calculates Average True Range (ATR) to avoid getting shaken out by normal market wicks.
4. **Volume Confirmation**: Rejects fakeout signals during low-volume market dead zones.
5. **Disk Persistence**: Actively writes open positions to `data/open_positions.json`. If your computer restarts, the bot restores its tracking automatically.
6. **Multi-Timeframe Support**: Tested and tuned for both `15m` and `1h` (1-hour timeframe achieved **66.7% win rate** in backtests).
7. **Optional Telegram Alerts**: Sends live buy/sell/stop-loss notifications directly to your phone.

---

## 📊 Backtest Verification

In backtests across the 500 most recent candles for `BTC/USDT`, `ETH/USDT`, and `SOL/USDT`:

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

## 🔒 Pre-Flight Checklist for Going Live

When you decide to switch from paper trading to live trading:

- [ ] Run paper trading for at least **1 to 2 weeks** to see real fill behavior.
- [ ] Confirm your Binance account has **2FA enabled** and API keys with **Spot Trading ONLY** (disable Withdrawal permissions!).
- [ ] Deposit only what you are willing to risk.
- [ ] In `.env`, set `PAPER_TRADING=false` and insert your live API keys.
- [ ] Run `python main.py --once` first to check live balance and connectivity before starting continuous loop.
