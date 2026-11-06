#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║             AUTOMATED CRYPTO TRADING BOT                     ║
║                                                              ║
║  A disciplined, risk-managed cryptocurrency trading bot      ║
║  that uses technical analysis to find trade opportunities     ║
║  while keeping strict risk controls.                         ║
║                                                              ║
║  ⚠️  DISCLAIMER: Trading involves significant risk.          ║
║  Never trade with money you can't afford to lose.            ║
║  Past performance does not guarantee future results.         ║
║  This bot is for educational purposes.                       ║
╚══════════════════════════════════════════════════════════════╝

Usage:
    python main.py              # Run the bot
    python main.py --once       # Run a single cycle (test)
    python main.py --status     # Show current config and exit
    python main.py --backtest   # Run backtesting simulation

"""

import sys
import argparse
import logging

from config import BotConfig
from bot_engine import TradingBot


def print_banner():
    """Print the startup banner."""
    print(r"""
    ╔═══════════════════════════════════════════════════╗
    ║                                                   ║
    ║    🤖  AUTOMATED CRYPTO TRADING BOT  🤖           ║
    ║                                                   ║
    ║    ▸ Strategy-driven decision making              ║
    ║    ▸ Strict risk management                       ║
    ║    ▸ Real-time market analysis                    ║
    ║    ▸ Automated stop-loss & take-profit            ║
    ║                                                   ║
    ╚═══════════════════════════════════════════════════╝
    """)


def main():
    parser = argparse.ArgumentParser(
        description="Automated Crypto Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                    Start the bot (paper trading by default)
  python main.py --once             Run a single analysis cycle
  python main.py --status           Show configuration and exit
  python main.py --interval 120     Analyze every 2 minutes
  python main.py --backtest         Run backtest simulation
        """
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single analysis cycle and exit"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show current configuration and exit"
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Seconds between analysis cycles (default: 60)"
    )
    parser.add_argument(
        "--backtest", action="store_true",
        help="Run backtesting simulation instead of live trading"
    )
    parser.add_argument(
        "--timeframe", type=str, default="",
        help="Override timeframe (e.g. 15m, 1h, 4h)"
    )
    parser.add_argument(
        "--dashboard", action="store_true",
        help="Start the web surveillance dashboard"
    )
    parser.add_argument(
        "--port", type=int, default=5000,
        help="Web dashboard port (default: 5000)"
    )
    args = parser.parse_args()

    print_banner()

    if args.dashboard:
        from dashboard import WebDashboard
        dashboard = WebDashboard(port=args.port)
        dashboard.run()
        return

    # Load configuration
    config = BotConfig()
    if args.timeframe:
        config.strategy.timeframe = args.timeframe

    if args.status:
        config.display()
        errors = config.validate()
        if errors:
            print("\n⚠️  Configuration issues:")
            for err in errors:
                print(f"  ❌ {err}")
        else:
            print("  ✅ Configuration looks good!")
        return

    if args.backtest:
        run_backtest(config)
        return

    # Create and run the bot
    bot = TradingBot(config)

    if args.once:
        bot.run_once()
    else:
        print(f"  Starting continuous trading (interval: {args.interval}s)")
        print(f"  Press Ctrl+C to stop\n")
        bot.run(interval_seconds=args.interval)


def run_backtest(config: BotConfig):
    """
    Simple backtesting simulation using historical data.
    Fetches historical candles and simulates what the bot
    would have done.
    """
    from exchange import ExchangeConnector
    from strategies import get_strategy, ohlcv_to_dataframe, Signal
    from risk_manager import RiskManager
    import pandas as pd

    print("📊 Running Backtest Simulation")
    print("=" * 50)

    exchange = ExchangeConnector(config.exchange)
    strategy = get_strategy(config.strategy.name)

    # Simulated portfolio
    initial_balance = 10000.0  # $10,000 starting balance
    balance = initial_balance
    trades = []

    for symbol in config.strategy.trading_pairs:
        symbol = symbol.strip()
        print(f"\n🔍 Backtesting {symbol}...")

        # Fetch historical data
        ohlcv = exchange.fetch_ohlcv(symbol, config.strategy.timeframe, limit=500)
        if not ohlcv or len(ohlcv) < 50:
            print(f"  ⚠️ Insufficient data for {symbol}")
            continue

        df = ohlcv_to_dataframe(ohlcv)
        position = None  # (amount, entry_price) for this specific symbol

        # Walk through history simulating trades
        for i in range(50, len(df)):
            window = df.iloc[:i+1].copy()
            signal = strategy.analyze(window, symbol)
            current_price = window["close"].iloc[-1]

            if signal.signal == Signal.BUY and position is None and signal.confidence >= 0.5:
                # Buy
                amount_usd = balance * (config.risk.position_size_pct / 100)
                amount = amount_usd / current_price
                position = (amount, current_price)
                balance -= amount_usd
                trades.append({
                    "action": "BUY",
                    "symbol": symbol,
                    "price": current_price,
                    "time": window.index[-1],
                })

            elif position is not None:
                pos_amount, entry_price = position

                # Check stop-loss
                stop_price = entry_price * (1 - config.risk.stop_loss_pct / 100)
                tp_price = entry_price * (1 + config.risk.take_profit_pct / 100)

                should_sell = False
                reason = ""

                if current_price <= stop_price:
                    should_sell = True
                    reason = "STOP-LOSS"
                elif current_price >= tp_price:
                    should_sell = True
                    reason = "TAKE-PROFIT"
                elif signal.signal == Signal.SELL and signal.confidence >= 0.5:
                    should_sell = True
                    reason = "STRATEGY SIGNAL"

                if should_sell:
                    proceeds = pos_amount * current_price
                    pnl = proceeds - (pos_amount * entry_price)
                    balance += proceeds
                    trades.append({
                        "action": "SELL",
                        "symbol": symbol,
                        "price": current_price,
                        "pnl": pnl,
                        "reason": reason,
                        "time": window.index[-1],
                    })
                    position = None

        # Close any remaining open position for this symbol at last price
        if position:
            pos_amount, entry_price = position
            last_price = df["close"].iloc[-1]
            proceeds = pos_amount * last_price
            pnl = proceeds - (pos_amount * entry_price)
            balance += proceeds
            trades.append({
                "action": "SELL",
                "symbol": symbol,
                "price": last_price,
                "pnl": pnl,
                "reason": "END OF BACKTEST",
                "time": df.index[-1],
            })

    # Results
    total_pnl = balance - initial_balance
    total_return_pct = (total_pnl / initial_balance) * 100
    sell_trades = [t for t in trades if t["action"] == "SELL"]
    winners = [t for t in sell_trades if t.get("pnl", 0) > 0]
    losers = [t for t in sell_trades if t.get("pnl", 0) <= 0]

    print(f"""
╔══════════════════════════════════════════════════════════╗
║                BACKTEST RESULTS                          ║
╠══════════════════════════════════════════════════════════╣
║  Initial Balance:  ${initial_balance:>10,.2f}                        ║
║  Final Balance:    ${balance:>10,.2f}                        ║
║  Total P&L:        ${total_pnl:>+10,.2f} ({total_return_pct:+.2f}%)             ║
╠══════════════════════════════════════════════════════════╣
║  Total Trades:     {len(sell_trades):<38}║
║  Winners:          {len(winners):<38}║
║  Losers:           {len(losers):<38}║
║  Win Rate:         {len(winners)/max(len(sell_trades),1)*100:.1f}%{'':<34}║
╚══════════════════════════════════════════════════════════╝
""")

    if total_pnl > 0:
        print("  ✅ Strategy was PROFITABLE in this period")
    else:
        print("  ⚠️  Strategy LOST money in this period")
    print("  ℹ️  Past performance does NOT guarantee future results!\n")


if __name__ == "__main__":
    main()
