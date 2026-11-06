"""
Trading Bot Engine — the main loop that ties everything together.

This is the orchestrator that:
  1. Fetches real-time market data
  2. Runs upgraded strategy analysis (with 200 EMA trend & volume filters)
  3. Checks risk management rules (with ATR & break-even protection)
  4. Executes trades when conditions are met
  5. Monitors open positions for trailing stop-loss / take-profit
  6. Dispatches notifications (Console + File + optional Telegram)
"""

import logging
import time
import json
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from config import BotConfig
from exchange import ExchangeConnector
from strategies import get_strategy, ohlcv_to_dataframe, Signal
from risk_manager import RiskManager

logger = logging.getLogger("bot.engine")


class TradingBot:
    """
    Main trading bot engine.

    Runs in a continuous loop, analyzing markets and executing trades
    according to the configured strategy and risk management rules.
    """

    def __init__(self, config: BotConfig):
        self.config = config
        self.exchange = ExchangeConnector(config.exchange)
        self.strategy = get_strategy(config.strategy.name)
        self.risk_manager = RiskManager(config.risk, persistence_dir=config.data_dir)
        self.is_running = False
        self._cycle_count = 0
        self._setup_logging()
        self._setup_trade_log()

    def _setup_logging(self):
        """Configure logging to file and console."""
        log_dir = Path(self.config.data_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        self.config.log_file = str(log_dir / Path(self.config.log_file).name)

        bot_logger = logging.getLogger("bot")
        bot_logger.setLevel(getattr(logging, self.config.log_level))

        # Console handler
        if not bot_logger.handlers:
            console = logging.StreamHandler()
            console.setLevel(logging.INFO)
            console_fmt = logging.Formatter(
                "%(asctime)s │ %(levelname)-7s │ %(message)s",
                datefmt="%H:%M:%S"
            )
            console.setFormatter(console_fmt)
            bot_logger.addHandler(console)

            # File handler
            file_handler = logging.FileHandler(self.config.log_file, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_fmt = logging.Formatter(
                "%(asctime)s │ %(name)-15s │ %(levelname)-7s │ %(message)s"
            )
            file_handler.setFormatter(file_fmt)
            bot_logger.addHandler(file_handler)

    def _setup_trade_log(self):
        """Setup a separate JSON trade log for record keeping."""
        log_dir = Path(self.config.data_dir)
        self.trade_log_path = log_dir / "trades.jsonl"

    def _log_trade(self, trade_data: dict):
        """Append a trade record to the JSON trade log."""
        trade_data["timestamp"] = datetime.now(timezone.utc).isoformat()
        with open(self.trade_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(trade_data) + "\n")

    def send_notification(self, message: str):
        """Send notifications to console and optional Telegram bot."""
        if not self.config.enable_notifications:
            return

        # Optional Telegram notification
        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            try:
                url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
                payload = {
                    "chat_id": self.config.telegram_chat_id,
                    "text": message,
                    "parse_mode": "Markdown",
                }
                data = urllib.parse.urlencode(payload).encode("utf-8")
                req = urllib.request.Request(url, data=data)
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                logger.debug(f"Telegram notification delivery note: {e}")

    def analyze_market(self, symbol: str):
        """
        Analyze a single trading pair and potentially execute a trade.
        """
        # Fetch market data
        ohlcv = self.exchange.fetch_ohlcv(
            symbol,
            self.config.strategy.timeframe,
            self.config.strategy.lookback_candles
        )

        if not ohlcv or len(ohlcv) < 30:
            logger.warning(f"Insufficient data for {symbol} ({len(ohlcv)} candles)")
            return

        # Convert to DataFrame and run strategy
        df = ohlcv_to_dataframe(ohlcv)
        signal = self.strategy.analyze(df, symbol)

        if signal.signal != Signal.HOLD:
            logger.info(str(signal))

        # Execute based on signal
        if signal.signal == Signal.BUY:
            self._handle_buy_signal(signal)
        elif signal.signal == Signal.SELL:
            self._handle_sell_signal(signal)

    def _handle_buy_signal(self, signal):
        """Process a BUY signal through risk checks and potentially buy."""
        symbol = signal.symbol

        # Get current portfolio value
        usdt_balance = self.exchange.get_usdt_balance()
        if usdt_balance <= 0:
            logger.warning(f"No USDT balance available for buying {symbol}")
            return

        # Risk check: can we open a new position?
        can_trade, reason = self.risk_manager.can_open_position(
            symbol, usdt_balance
        )
        if not can_trade:
            logger.info(f"⏸ Trade blocked for {symbol}: {reason}")
            return

        # Confidence check
        if signal.confidence < 0.5:
            logger.info(
                f"⏸ Signal too weak for {symbol}: "
                f"{signal.confidence:.0%} confidence (need ≥50%)"
            )
            return

        current_price = self.exchange.get_current_price(symbol)
        if current_price <= 0:
            return

        atr = signal.indicators.get("atr", 0.0)
        stop_loss_price = self.risk_manager.calculate_stop_loss(current_price, atr=atr)
        amount = self.risk_manager.calculate_position_size(
            usdt_balance, current_price, stop_loss_price
        )

        # Check minimum order
        min_amount = self.exchange.get_min_order_amount(symbol)
        if amount < min_amount:
            logger.warning(
                f"Position size {amount:.8f} below minimum {min_amount} for {symbol}"
            )
            return

        cost = amount * current_price
        if cost < 10:  # Min $10 order on most exchanges
            logger.warning(f"Order value ${cost:.2f} too small for {symbol}")
            return

        logger.info(
            f"🚀 Executing BUY: {amount:.6f} {symbol} @ ~${current_price:,.2f} "
            f"(${cost:,.2f}) | Confidence: {signal.confidence:.0%}"
        )

        order = self.exchange.place_market_buy(symbol, amount)
        if order:
            fill_price = order.get("average", order.get("price", current_price))
            fill_amount = order.get("filled", amount)

            self.risk_manager.open_position(
                symbol=symbol,
                entry_price=fill_price,
                amount=fill_amount,
                order_id=order.get("id", ""),
                atr=atr
            )

            msg = (
                f"🟢 *BOT BUY EXECUTED*\n"
                f"Pair: `{symbol}`\n"
                f"Amount: `{fill_amount:.6f}`\n"
                f"Price: `${fill_price:,.2f}`\n"
                f"Reason: {signal.reason}"
            )
            self.send_notification(msg)

            self._log_trade({
                "action": "BUY",
                "symbol": symbol,
                "price": fill_price,
                "amount": fill_amount,
                "cost": fill_price * fill_amount,
                "confidence": signal.confidence,
                "reason": signal.reason,
                "indicators": signal.indicators,
            })

    def _handle_sell_signal(self, signal):
        """Process a SELL signal — sell if we have an open position."""
        symbol = signal.symbol

        if symbol not in self.risk_manager.open_positions:
            logger.debug(f"SELL signal for {symbol} but no open position — ignoring")
            return

        current_price = self.exchange.get_current_price(symbol)
        if current_price <= 0:
            return

        position = self.risk_manager.open_positions[symbol]

        logger.info(
            f"🔻 Executing SELL: {position.amount:.6f} {symbol} @ ~${current_price:,.2f} "
            f"| Confidence: {signal.confidence:.0%}"
        )

        order = self.exchange.place_market_sell(symbol, position.amount)
        if order:
            fill_price = order.get("average", order.get("price", current_price))

            record = self.risk_manager.close_position(
                symbol=symbol,
                exit_price=fill_price,
                reason=f"Strategy signal: {signal.reason}"
            )

            if record:
                msg = (
                    f"🔴 *BOT SELL EXECUTED*\n"
                    f"Pair: `{symbol}`\n"
                    f"Exit Price: `${fill_price:,.2f}`\n"
                    f"P&L: `${record.pnl:+,.2f}` ({record.pnl_pct:+.2f}%)\n"
                    f"Reason: {record.reason}"
                )
                self.send_notification(msg)

                self._log_trade({
                    "action": "SELL",
                    "symbol": symbol,
                    "price": fill_price,
                    "amount": record.amount,
                    "pnl": record.pnl,
                    "pnl_pct": record.pnl_pct,
                    "reason": record.reason,
                })

    def check_stop_loss_take_profit(self):
        """Check all open positions for Trailing SL, Breakeven, and TP triggers."""
        to_close = self.risk_manager.check_positions(
            self.exchange.get_current_price
        )

        for symbol, reason in to_close:
            current_price = self.exchange.get_current_price(symbol)
            position = self.risk_manager.open_positions.get(symbol)
            if not position:
                continue

            logger.info(f"🔻 Auto-closing {symbol}: {reason}")

            order = self.exchange.place_market_sell(symbol, position.amount)
            if order:
                fill_price = order.get("average", order.get("price", current_price))
                record = self.risk_manager.close_position(symbol, fill_price, reason)

                if record:
                    msg = (
                        f"🛡️ *POSITION CLOSED ({reason})*\n"
                        f"Pair: `{symbol}`\n"
                        f"Exit Price: `${fill_price:,.2f}`\n"
                        f"P&L: `${record.pnl:+,.2f}` ({record.pnl_pct:+.2f}%)"
                    )
                    self.send_notification(msg)

                    self._log_trade({
                        "action": "SELL",
                        "symbol": symbol,
                        "price": fill_price,
                        "amount": record.amount,
                        "pnl": record.pnl,
                        "pnl_pct": record.pnl_pct,
                        "reason": record.reason,
                    })

    def print_status(self):
        """Print current bot status including open positions and P&L."""
        balance = self.exchange.get_usdt_balance()
        positions = self.risk_manager.open_positions

        print(f"\n{'─' * 60}")
        print(f"  Cycle #{self._cycle_count} | "
              f"USDT Balance: ${balance:,.2f} | "
              f"Positions: {len(positions)}/{self.config.risk.max_open_positions}")

        if positions:
            for symbol, pos in positions.items():
                current = self.exchange.get_current_price(symbol)
                pnl = pos.unrealized_pnl(current)
                pnl_pct = pos.unrealized_pnl_pct(current)
                emoji = "📈" if pnl >= 0 else "📉"
                be_tag = " [BREAK-EVEN]" if pos.break_even_activated else ""
                print(
                    f"    {emoji} {symbol}: ${pos.entry_price:,.2f} → "
                    f"${current:,.2f} | P&L: ${pnl:+,.2f} ({pnl_pct:+.2f}%){be_tag} | "
                    f"SL: ${pos.stop_loss_price:,.2f} | TP: ${pos.take_profit_price:,.2f}"
                )

        print(f"{'─' * 60}")

    def run(self, interval_seconds: int = 60):
        """
        Main continuous bot loop.
        """
        self.is_running = True
        self.config.display()

        errors = self.config.validate()
        if errors:
            logger.error("Configuration errors:")
            for err in errors:
                logger.error(f"  ❌ {err}")
            return

        logger.info("🤖 Bot running...")
        logger.info(f"Strategy: {self.config.strategy.name}")
        logger.info(f"Pairs: {', '.join(self.config.strategy.trading_pairs)}")
        logger.info(f"Timeframe: {self.config.strategy.timeframe}")
        logger.info(f"Loop interval: {interval_seconds}s")
        if self.risk_manager.open_positions:
            logger.info(f"Active positions tracked: {list(self.risk_manager.open_positions.keys())}")
        logger.info("")

        try:
            while self.is_running:
                self._cycle_count += 1
                cycle_start = time.time()

                try:
                    # 1. Check stop-loss, take-profit & trailing stop
                    self.check_stop_loss_take_profit()

                    # 2. Analyze each pair
                    for symbol in self.config.strategy.trading_pairs:
                        symbol = symbol.strip()
                        try:
                            self.analyze_market(symbol)
                        except Exception as e:
                            logger.error(f"Error analyzing {symbol}: {e}")

                    # 3. Print status every cycle if position exists, or every 5 cycles
                    if self.risk_manager.open_positions or self._cycle_count % 5 == 0:
                        self.print_status()

                except Exception as e:
                    logger.error(f"Error in trading cycle: {e}")

                elapsed = time.time() - cycle_start
                sleep_time = max(0, interval_seconds - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("\n🛑 Bot stopped by user (Ctrl+C)")
        finally:
            self.is_running = False
            self._shutdown()

    def _shutdown(self):
        """Clean shutdown — display final stats."""
        logger.info("\n📊 Final Performance Report:")
        self.risk_manager.display_performance()

        if self.risk_manager.open_positions:
            logger.warning(
                f"⚠️  You have {len(self.risk_manager.open_positions)} "
                f"persisted open positions in data/open_positions.json."
            )
            for symbol, pos in self.risk_manager.open_positions.items():
                logger.warning(
                    f"    {symbol}: {pos.amount:.6f} entered at ${pos.entry_price:,.2f}"
                )

    def run_once(self):
        """Run a single analysis cycle."""
        self.config.display()
        errors = self.config.validate()
        if errors:
            for err in errors:
                logger.error(f"  ❌ {err}")
            return

        self._cycle_count += 1
        logger.info(f"🔍 Running single analysis cycle...")

        self.check_stop_loss_take_profit()

        for symbol in self.config.strategy.trading_pairs:
            symbol = symbol.strip()
            self.analyze_market(symbol)

        self.print_status()
        self.risk_manager.display_performance()
