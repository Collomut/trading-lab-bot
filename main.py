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
    Historical replay simulation incorporating next-bar execution and fees.
    This replay is an exploratory research tool and does not prove live profitability.
    """
    from exchange import ExchangeConnector
    from strategies import get_strategy, ohlcv_to_dataframe, Signal
    from risk_manager import RiskManager
    import pandas as pd

    print("\n" + "=" * 70)
    print("⚠️ EXPLORATORY HISTORICAL REPLAY ONLY (F-10)")
    print("This replay models next-bar open execution and 0.1% trading fees, but omits")
    print("live order-book depth, spread, latency, and Testnet liquidity dynamics.")
    print("Do NOT use these simulated results as evidence of live edge or profitability.")
    print("=" * 70 + "\n")

    exchange = ExchangeConnector(config.exchange)
    strategy = get_strategy(
        config.strategy.name,
        regime_filter_enabled=config.strategy.regime_filter_enabled,
        min_trend_adx=config.strategy.min_trend_adx,
        max_atr_pct=config.strategy.max_atr_pct,
    )

    # Simulated portfolio
    initial_balance = 10000.0  # $10,000 starting balance
    balance = initial_balance
    trades = []
    fee_rate = 0.001  # 0.1% Spot fee per trade

    for symbol in config.strategy.trading_pairs:
        symbol = symbol.strip()
        print(f"\n🔍 Replaying {symbol}...")

        # Fetch historical data
        ohlcv = exchange.fetch_ohlcv(symbol, config.strategy.timeframe, limit=500)
        if not ohlcv or len(ohlcv) < 50:
            print(f"  ⚠️ Insufficient data for {symbol}")
            continue

        df = ohlcv_to_dataframe(ohlcv)
        position = None  # (amount, entry_price) for this specific symbol

        # Walk through history simulating next-bar execution without lookahead
        for i in range(50, len(df) - 1):
            window = df.iloc[:i+1].copy()
            signal = strategy.analyze(window, symbol)
            next_bar = df.iloc[i+1]
            exec_price = float(next_bar["open"])
            bar_high = float(next_bar["high"])
            bar_low = float(next_bar["low"])

            if position is not None:
                pos_amount, entry_price = position
                stop_price = entry_price * (1 - config.risk.stop_loss_pct / 100)
                tp_price = entry_price * (1 + config.risk.take_profit_pct / 100)

                should_sell = False
                sell_price = exec_price
                reason = ""

                # Conservative intrabar check: if low breached stop_price, stop triggered
                if bar_low <= stop_price:
                    should_sell = True
                    sell_price = min(exec_price, stop_price)
                    reason = "STOP-LOSS"
                elif bar_high >= tp_price:
                    should_sell = True
                    sell_price = max(exec_price, tp_price)
                    reason = "TAKE-PROFIT"
                elif signal.signal == Signal.SELL and signal.confidence >= 0.5:
                    should_sell = True
                    sell_price = exec_price
                    reason = "STRATEGY SIGNAL"

                if should_sell:
                    gross_proceeds = pos_amount * sell_price
                    exit_fee = gross_proceeds * fee_rate
                    net_proceeds = gross_proceeds - exit_fee
                    cost_basis = pos_amount * entry_price
                    pnl = net_proceeds - cost_basis
                    balance += net_proceeds
                    trades.append({
                        "action": "SELL",
                        "symbol": symbol,
                        "price": sell_price,
                        "pnl": pnl,
                        "fee": exit_fee,
                        "reason": reason,
                        "time": str(next_bar.name) if hasattr(next_bar, "name") else str(window.index[-1]),
                    })
                    position = None

            elif signal.signal == Signal.BUY and position is None and signal.confidence >= 0.5:
                # Buy at next bar open with fee deduction
                amount_usd = balance * (config.risk.position_size_pct / 100)
                buy_fee = amount_usd * fee_rate
                net_usd = amount_usd - buy_fee
                amount = net_usd / exec_price
                position = (amount, exec_price)
                balance -= amount_usd
                trades.append({
                    "action": "BUY",
                    "symbol": symbol,
                    "price": exec_price,
                    "fee": buy_fee,
                    "time": str(next_bar.name) if hasattr(next_bar, "name") else str(window.index[-1]),
                })

        # Close any remaining open position for this symbol at last price
        if position:
            pos_amount, entry_price = position
            last_price = float(df["close"].iloc[-1])
            gross_proceeds = pos_amount * last_price
            exit_fee = gross_proceeds * fee_rate
            net_proceeds = gross_proceeds - exit_fee
            pnl = net_proceeds - (pos_amount * entry_price)
            balance += net_proceeds
            trades.append({
                "action": "SELL",
                "symbol": symbol,
                "price": last_price,
                "pnl": pnl,
                "fee": exit_fee,
                "reason": "END OF BACKTEST",
                "time": str(df.index[-1]),
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

    print("  ℹ️  Exploratory replay only; the execution assumptions are inadequate for estimating live performance.")
    print("  ℹ️  A positive result is not evidence of a profitable strategy.\n")


if __name__ == "__main__":
    main()
