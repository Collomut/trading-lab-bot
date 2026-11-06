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
import threading
import hashlib
from decimal import Decimal, InvalidOperation
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
        self.strategy = get_strategy(
            config.strategy.name,
            regime_filter_enabled=config.strategy.regime_filter_enabled,
            min_trend_adx=config.strategy.min_trend_adx,
            max_atr_pct=config.strategy.max_atr_pct,
        )
        self.risk_manager = RiskManager(config.risk, persistence_dir=config.data_dir)
        self.is_running = False
        self._cycle_count = 0
        self._cycle_lock = threading.Lock()
        self.last_signals = {}
        self._last_analyzed_candle = {}
        self._last_reconciliation_ok = False
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
                logger.debug("Telegram notification delivery failed (details suppressed)")

    def analyze_market(self, symbol: str):
        """
        Analyze a single trading pair and potentially execute a trade.
        """
        # Fetch market data
        ohlcv = self.exchange.fetch_closed_ohlcv(
            symbol,
            self.config.strategy.timeframe,
            self.config.strategy.lookback_candles
        )

        if not ohlcv or len(ohlcv) < 30:
            logger.warning(f"Insufficient data for {symbol} ({len(ohlcv)} candles)")
            return

        candle_timestamp = int(ohlcv[-1][0])
        if self._last_analyzed_candle.get(symbol) == candle_timestamp:
            logger.debug("Skipping duplicate closed candle for %s at %s", symbol, candle_timestamp)
            return

        # Convert to DataFrame and run strategy once per completed candle.
        df = ohlcv_to_dataframe(ohlcv)
        signal = self.strategy.analyze(df, symbol)
        self._last_analyzed_candle[symbol] = candle_timestamp
        self.last_signals[symbol] = {
            "signal": signal.signal.value,
            "reason": signal.reason,
            "confidence": signal.confidence,
            "indicators": signal.indicators,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if signal.signal != Signal.HOLD:
            logger.info(str(signal))

        # Execute based on signal
        try:
            if signal.signal == Signal.BUY:
                self._handle_buy_signal(signal, candle_timestamp)
            elif signal.signal == Signal.SELL:
                self._handle_sell_signal(signal, candle_timestamp)
        except Exception:
            self.risk_manager.halt("Signal execution failed; operator reconciliation required")
            logger.exception("Signal execution failed for %s; trading halted", symbol)

    @staticmethod
    def _client_order_id(symbol: str, side: str, decision_key: str) -> str:
        """Stable retry key within Binance's client-order-id length limit."""
        digest = hashlib.sha256(f"{symbol}|{side}|{decision_key}".encode()).hexdigest()[:28]
        return f"cb{digest}"

    def _submit_market_order(self, symbol: str, side: str, amount, decision_key: str):
        """Journal before POST and persist every known/ambiguous exchange outcome."""
        amount_decimal = Decimal(str(amount))
        if not amount_decimal.is_finite() or amount_decimal <= 0:
            raise ValueError("Order amount must be a finite positive decimal")
        client_id = self._client_order_id(symbol, side, str(decision_key))
        self.risk_manager.store.create_order_intent(
            client_id, symbol, side, format(amount_decimal, "f"),
            {"symbol": symbol, "side": side, "amount": format(amount_decimal, "f")},
        )
        self.risk_manager.store.update_order(client_id, "SUBMITTED")
        try:
            submit = self.exchange.place_market_buy if side == "buy" else self.exchange.place_market_sell
            order = submit(symbol, format(amount_decimal, "f"), client_id)
        except Exception:
            self.risk_manager.store.update_order(client_id, "UNKNOWN")
            raise
        if not order:
            self.risk_manager.store.update_order(client_id, "REJECTED", response={"result": "rejected_without_order"})
            self.risk_manager.store.mark_order_applied(client_id)
            self.risk_manager.halt(f"{side.title()} order was rejected for {symbol}; operator review required")
            return None, client_id
        raw_status = str(order.get("status", "")).lower()
        state = {"closed": "FILLED", "open": "OPEN", "canceled": "CANCELED", "rejected": "REJECTED", "expired": "EXPIRED"}.get(raw_status, "UNKNOWN")
        filled = order.get("filled") or 0
        average = order.get("average") or order.get("price")
        if state == "FILLED" and Decimal(str(filled)) < amount_decimal * Decimal("0.99999999"):
            state = "PARTIALLY_FILLED"
        self.risk_manager.store.update_order(
            client_id, state, exchange_order_id=order.get("id"),
            filled=filled, average=average, response=order,
        )
        return order, client_id

    def _reconcile_exchange_state(self) -> bool:
        """Block all order activity until authenticated account state matches local state."""
        try:
            balance, open_orders = self.exchange.fetch_account_snapshot()
            self.exchange.exchange.load_markets()
            totals = balance.get("total", {}) or {}
            configured = {pair.strip(): pair.strip().split("/")[0] for pair in self.config.strategy.trading_pairs}
            tolerances = {}
            for symbol, base in configured.items():
                market = self.exchange.exchange.market(symbol)
                precision = market.get("precision", {}).get("amount")
                precision_mode = getattr(self.exchange.exchange, "precisionMode", None)
                tick_size_mode = getattr(self.exchange.exchange, "TICK_SIZE", 4)
                if precision_mode == tick_size_mode and precision is not None:
                    tolerances[base] = Decimal(str(precision)) / 2
                else:
                    precision_sample = self.exchange.amount_to_precision(symbol, "1")
                    precision_exponent = Decimal(str(precision_sample)).as_tuple().exponent
                    tolerances[base] = Decimal(1).scaleb(precision_exponent) / 2
                reported = Decimal(str(totals.get(base) or 0))
                local = Decimal(str(self.risk_manager.open_positions[symbol].amount if symbol in self.risk_manager.open_positions else 0))
                if abs(reported - local) > tolerances[base]:
                    raise ValueError(f"balance mismatch for configured asset {base}")
            if open_orders:
                raise ValueError("unreconciled open order exists on the account")
            if self.risk_manager.store.unresolved_orders():
                raise ValueError("unresolved local order intent exists")
            self._last_reconciliation_ok = True
            self.risk_manager.resume_after_reconciliation()
            return True
        except Exception as exc:
            self._last_reconciliation_ok = False
            reason = f"Exchange reconciliation failed: {type(exc).__name__}"
            self.risk_manager.halt(reason)
            try:
                self.risk_manager.store.append_event("reconciliation_failed", {"reason": reason})
            except Exception:
                logger.critical("Could not append reconciliation failure to durable audit log")
            logger.error("Account state failed reconciliation; all new orders blocked (%s)", type(exc).__name__)
            return False

    @staticmethod
    def _confirmed_full_fill(order: dict, requested_amount: float) -> bool:
        """Accept only an explicit, complete market-order fill."""
        if not isinstance(order, dict) or order.get("status") != "closed":
            return False
        try:
            filled = Decimal(str(order.get("filled")))
            requested = Decimal(str(requested_amount))
            return filled.is_finite() and requested.is_finite() and filled > 0 and requested > 0 and filled >= requested * Decimal("0.99999999")
        except (InvalidOperation, TypeError, ValueError):
            return False

    @staticmethod
    def _net_base_filled(order: dict, symbol: str) -> Decimal | None:
        """Account for commission charged in the received base asset.

        Return None when the order response lacks fee data; silently assuming
        zero base commission would make local quantity disagree with the wallet.
        """
        base = symbol.split("/")[0]
        fees = order.get("fees")
        if fees is None:
            fee = order.get("fee")
            fees = [fee] if isinstance(fee, dict) else None
        if not fees:
            fills = (order.get("info") or {}).get("fills")
            if isinstance(fills, list) and fills:
                fees = [{"currency": fill.get("commissionAsset"), "cost": fill.get("commission")} for fill in fills]
        if not fees:
            return None
        if not isinstance(fees, list) or any(not isinstance(fee, dict) for fee in fees):
            return None
        try:
            filled = Decimal(str(order.get("filled")))
            base_fee = sum((Decimal(str(fee.get("cost") or 0)) for fee in fees if fee.get("currency") == base), Decimal("0"))
            net = filled - base_fee
            return net if net.is_finite() and net > 0 else None
        except (InvalidOperation, TypeError, ValueError, AttributeError):
            return None

    def _handle_buy_signal(self, signal, decision_key="manual"):
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

        # Exchange rules and market quality are checked before any entry.
        try:
            amount = Decimal(self.exchange.amount_to_precision(symbol, amount))
            limits = self.exchange.get_market_limits(symbol)
            book = self.exchange.get_order_book(symbol)
            best_bid = Decimal(str(book["bids"][0][0]))
            best_ask = Decimal(str(book["asks"][0][0]))
            if not best_bid.is_finite() or not best_ask.is_finite() or best_bid <= 0 or best_ask < best_bid:
                raise ValueError("Invalid top of book")
            spread_bps = (best_ask - best_bid) / ((best_ask + best_bid) / 2) * Decimal(10000)
            if spread_bps > Decimal(str(self.config.max_spread_bps)):
                logger.info("Entry blocked for %s: spread %.2f bps exceeds configured limit", symbol, spread_bps)
                return
            needed_quote = amount * best_ask
            asks = book["asks"]
            available = Decimal("0")
            for price, quantity in asks:
                level_price, level_quantity = Decimal(str(price)), Decimal(str(quantity))
                if not level_price.is_finite() or not level_quantity.is_finite() or level_price <= 0 or level_quantity < 0:
                    raise ValueError("Invalid order-book depth")
                if level_price <= best_ask * (1 + Decimal(str(self.config.max_slippage_bps)) / Decimal(10000)):
                    available += level_quantity
            if available < amount:
                logger.info("Entry blocked for %s: insufficient visible depth within slippage limit", symbol)
                return
            minimum = Decimal(str(limits.get("min_amount") or 0))
            max_amount = limits.get("max_amount")
            min_cost = Decimal(str(limits.get("min_cost") or 0))
            max_cost = limits.get("max_cost")
            if not limits.get("active", True) or amount <= 0 or amount < minimum or (max_amount and amount > Decimal(str(max_amount))) or needed_quote < min_cost or (max_cost and needed_quote > Decimal(str(max_cost))):
                logger.info("Entry blocked for %s by exchange market limits", symbol)
                return
            rounded_risk = amount * abs(Decimal(str(current_price)) - Decimal(str(stop_loss_price)))
            risk_cap = Decimal(str(usdt_balance)) * Decimal(str(self.config.risk.max_risk_per_trade)) / Decimal(100)
            exposure_cap = Decimal(str(usdt_balance)) * Decimal(str(self.config.risk.position_size_pct)) / Decimal(100)
            if rounded_risk > risk_cap or needed_quote > exposure_cap:
                logger.warning("Rounded amount exceeds configured risk or exposure cap for %s", symbol)
                return
        except Exception as exc:
            logger.warning("Entry blocked for %s because preflight failed (%s)", symbol, type(exc).__name__)
            return

        # Check minimum order
        cost = amount * Decimal(str(current_price))

        logger.info(
            f"🚀 Executing BUY: {amount:.6f} {symbol} @ ~${current_price:,.2f} "
            f"(${cost:,.2f}) | Confidence: {signal.confidence:.0%}"
        )

        try:
            order, client_order_id = self._submit_market_order(symbol, "buy", amount, decision_key)
        except Exception as exc:
            self.risk_manager.halt(f"Buy order state requires reconciliation for {symbol}")
            logger.error("Buy order state requires reconciliation for %s: %s", symbol, exc)
            return
        if not order:
            self.risk_manager.store.mark_order_applied(client_order_id)
            return
        if order:
            if not self._confirmed_full_fill(order, amount):
                self.risk_manager.halt(f"Unconfirmed or partial buy fill for {symbol}; reconcile exchange orders and balances")
                logger.error("Buy order for %s was not confirmed fully filled; new orders halted", symbol)
                return
            fill_price = float(order.get("average") or order.get("price") or 0)
            net_filled = self._net_base_filled(order, symbol)
            if net_filled is None:
                self.risk_manager.halt(f"Buy fee/quantity details are incomplete for {symbol}; operator reconciliation required")
                return
            fill_amount = float(net_filled)
            if fill_price <= 0 or fill_amount <= 0:
                self.risk_manager.halt(f"Invalid buy fill details for {symbol}; reconcile exchange orders and balances")
                return

            self.risk_manager.open_position(
                symbol=symbol,
                entry_price=fill_price,
                amount=fill_amount,
                order_id=order.get("id", ""),
                atr=atr
            )
            if self.risk_manager._is_halted:
                logger.error("Position persistence failed after buy for %s; trading halted", symbol)
                return
            self.risk_manager.store.mark_order_applied(client_order_id)

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

    def _handle_sell_signal(self, signal, decision_key="manual"):
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

        try:
            order, client_order_id = self._submit_market_order(symbol, "sell", position.amount, decision_key)
        except Exception as exc:
            self.risk_manager.halt(f"Sell order state requires reconciliation for {symbol}")
            logger.error("Sell order state requires reconciliation for %s: %s", symbol, exc)
            return
        if order:
            if not self._confirmed_full_fill(order, position.amount):
                self.risk_manager.halt(f"Unconfirmed or partial sell fill for {symbol}; reconcile exchange orders and balances")
                logger.error("Sell order for %s was not confirmed fully filled; position remains tracked", symbol)
                return
            fill_price = float(order.get("average") or order.get("price") or 0)
            if fill_price <= 0:
                self.risk_manager.halt(f"Invalid sell fill details for {symbol}; reconcile exchange orders and balances")
                return

            record = self.risk_manager.close_position(
                symbol=symbol,
                exit_price=fill_price,
                reason=f"Strategy signal: {signal.reason}"
            )

            if record:
                self.risk_manager.store.mark_order_applied(client_order_id)
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

            try:
                key = f"stop:{position.order_id}:{reason}:{int(time.time() // 60)}"
                order, client_order_id = self._submit_market_order(symbol, "sell", position.amount, key)
            except Exception as exc:
                self.risk_manager.halt(f"Stop-exit order state requires reconciliation for {symbol}")
                logger.error("Stop-exit order state requires reconciliation for %s: %s", symbol, exc)
                return
            if order:
                if not self._confirmed_full_fill(order, position.amount):
                    self.risk_manager.halt(f"Unconfirmed or partial stop-exit for {symbol}; reconcile exchange orders and balances")
                    logger.error("Stop-exit for %s was not confirmed fully filled", symbol)
                    return
                fill_price = float(order.get("average") or order.get("price") or 0)
                if fill_price <= 0:
                    self.risk_manager.halt(f"Invalid stop-exit fill details for {symbol}; reconcile exchange orders and balances")
                    return
                record = self.risk_manager.close_position(symbol, fill_price, reason)

                if record:
                    self.risk_manager.store.mark_order_applied(client_order_id)
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

    def run_cycle(self) -> bool:
        """Run one serialized, validated risk-monitoring and strategy cycle."""
        if not self._cycle_lock.acquire(blocking=False):
            logger.warning("Trading cycle skipped because another cycle is still running")
            return False
        try:
            errors = self.config.validate()
            if errors:
                logger.error("Trading cycle blocked by configuration errors: %s", "; ".join(errors))
                return False

            self._cycle_count += 1
            if not self._reconcile_exchange_state():
                return False
            try:
                self.check_stop_loss_take_profit()
            except Exception:
                self.risk_manager.halt("Position monitoring failed; operator reconciliation required")
                logger.exception("Position monitoring failed; skipping new entries this cycle")
                return False
            if self.risk_manager._is_halted:
                logger.error("Trading halted during position monitoring: %s", self.risk_manager._halt_reason)
                return False

            for configured_symbol in self.config.strategy.trading_pairs:
                if self.risk_manager._is_halted:
                    logger.error("Remaining symbols skipped because trading is halted: %s", self.risk_manager._halt_reason)
                    break
                symbol = configured_symbol.strip()
                if not symbol:
                    continue
                try:
                    self.analyze_market(symbol)
                except Exception:
                    logger.exception("Error analyzing %s", symbol)

            if self.risk_manager.open_positions or self._cycle_count % 5 == 0:
                self.print_status()
            return True
        finally:
            self._cycle_lock.release()

    def run(self, interval_seconds: int = 60):
        """Main continuous bot loop."""
        if interval_seconds < 1:
            raise ValueError("interval_seconds must be a positive integer")
        self.config.display()
        errors = self.config.validate()
        if errors:
            logger.error("Configuration errors:")
            for err in errors:
                logger.error("  ❌ %s", err)
            return

        self.is_running = True
        logger.info("🤖 Bot running...")
        logger.info("Strategy: %s", self.config.strategy.name)
        logger.info("Pairs: %s", ", ".join(self.config.strategy.trading_pairs))
        logger.info("Timeframe: %s", self.config.strategy.timeframe)
        logger.info("Loop interval: %ss", interval_seconds)

        try:
            while self.is_running:
                cycle_start = time.time()
                self.run_cycle()
                sleep_time = max(0, interval_seconds - (time.time() - cycle_start))
                if sleep_time:
                    time.sleep(sleep_time)
        except KeyboardInterrupt:
            logger.info("\n🛑 Bot stopped by user (Ctrl+C)")
        finally:
            self.is_running = False
            self._shutdown()

    def run_once(self):
        """Run a single serialized analysis cycle."""
        self.config.display()
        if self.run_cycle():
            self.print_status()
            self.risk_manager.display_performance()

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
