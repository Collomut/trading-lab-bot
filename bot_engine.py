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
import math
import threading
import hashlib
from decimal import Decimal, InvalidOperation
import numpy as np
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from config import BotConfig
from exchange import AccountAccessError, ExchangeConnector
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
        self._analysis_error_states = {}
        self._last_reconciliation_ok = False
        self._last_account_balance = None
        self._last_account_error = ""
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
        """Persist trade history to PostgreSQL when enabled and keep a volume copy."""
        trade_data["timestamp"] = datetime.now(timezone.utc).isoformat()
        append_trade = getattr(self.risk_manager.store, "append_trade", None)
        if callable(append_trade):
            append_trade(trade_data)
        with open(self.trade_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(trade_data, allow_nan=False) + "\n")

    def _send_telegram_notification(self, message: str):
        """Send notification to Telegram bot."""
        if not (self.config.telegram_bot_token and self.config.telegram_chat_id):
            return
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
            logger.info("Telegram notification dispatched successfully")
        except Exception:
            logger.debug("Telegram notification delivery failed (details suppressed)")

    def _send_email_notification(self, subject: str, message: str):
        """Send an email or carrier SMS notification via SMTP (multipart plain text + HTML)."""
        if not (self.config.smtp_host and self.config.alert_email):
            return
        try:
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText

            clean_message = (
                message.replace("*", "")
                .replace("`", "")
            )

            # Build a styled HTML card for email readers
            lines = [l.strip() for l in clean_message.strip().split("\n") if l.strip()]
            title = lines[0] if lines else "Trading Alert"
            details = lines[1:]

            is_buy = "BUY" in title.upper()
            is_profit = "+" in clean_message and ("P&L" in clean_message or "PROFIT" in clean_message)
            badge_color = "#2e7d32" if (is_buy or is_profit) else "#c62828"

            rows_html = "".join(
                f"<tr><td style='padding:8px 12px;border-bottom:1px solid #eee;color:#555;'>{l.split(':', 1)[0]}</td>"
                f"<td style='padding:8px 12px;border-bottom:1px solid #eee;font-weight:600;color:#111;text-align:right;'>{l.split(':', 1)[1].strip() if ':' in l else ''}</td></tr>"
                for l in details
            )

            html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background-color:#F7F4EE;margin:0;padding:24px 12px;color:#222;">
  <div style="max-width:520px;margin:0 auto;background:#ffffff;border:1px solid #dcd7cc;border-radius:10px;padding:24px;box-shadow:0 2px 10px rgba(0,0,0,0.04);">
    <div style="display:inline-block;padding:5px 12px;border-radius:4px;background:{badge_color};color:#ffffff;font-size:12px;font-weight:bold;letter-spacing:0.5px;margin-bottom:16px;">
      {title}
    </div>
    <table style="width:100%;border-collapse:collapse;margin:12px 0 20px;font-size:14px;">
      {rows_html}
    </table>
    <div style="padding-top:16px;border-top:1px solid #eee;font-size:12px;color:#777;display:flex;justify-content:space-between;align-items:center;">
      <span>Automated Trading Bot</span>
      <a href="https://trial-production-7ce1.up.railway.app" style="color:#6d4c41;text-decoration:none;font-weight:600;">Open Dashboard &rarr;</a>
    </div>
  </div>
</body>
</html>"""

            msg = MIMEMultipart("alternative")
            sender = self.config.smtp_user or "trading-bot@railway.internal"
            msg["Subject"] = subject
            msg["From"] = sender
            msg["To"] = self.config.alert_email

            part_plain = MIMEText(clean_message, "plain", "utf-8")
            part_html = MIMEText(html_body, "html", "utf-8")
            msg.attach(part_plain)
            msg.attach(part_html)

            recipients = [addr.strip() for addr in self.config.alert_email.split(",") if addr.strip()]
            if not recipients:
                return

            if self.config.smtp_port == 465:
                server = smtplib.SMTP_SSL(self.config.smtp_host, self.config.smtp_port, timeout=10)
            else:
                server = smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=10)
                try:
                    server.starttls()
                except Exception:
                    pass

            if self.config.smtp_user and self.config.smtp_password:
                server.login(self.config.smtp_user, self.config.smtp_password)

            server.sendmail(sender, recipients, msg.as_string())
            server.quit()
            logger.info("Email alert dispatched to %s", self.config.alert_email)
        except Exception as e:
            logger.warning("Failed to send email notification: %s", str(e))

    def _send_discord_notification(self, message: str):
        """Send a notification to Discord webhook for instant push notifications."""
        if not self.config.discord_webhook_url:
            return
        try:
            payload = json.dumps({"content": message}).encode("utf-8")
            req = urllib.request.Request(
                self.config.discord_webhook_url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "CryptoTradingBot/1.0"}
            )
            urllib.request.urlopen(req, timeout=5)
            logger.info("Discord webhook notification dispatched")
        except Exception as e:
            logger.warning("Failed to send Discord webhook notification: %s", str(e))

    def send_notification(self, message: str, subject: str = "Trading Bot Alert"):
        """Send notifications to all configured channels (Telegram, Email/SMS, Discord)."""
        if not self.config.enable_notifications:
            return

        self._send_telegram_notification(message)
        self._send_email_notification(subject, message)
        self._send_discord_notification(message)

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
            available = len(ohlcv) if ohlcv else 0
            if self._analysis_error_states.get(symbol) != "insufficient_candles":
                try:
                    self.risk_manager.store.append_event("market_data_issue", {
                        "symbol": symbol, "issue": "insufficient_candles", "available_candles": available,
                    })
                except Exception:
                    logger.exception("Could not record insufficient market data for %s", symbol)
            self._analysis_error_states[symbol] = "insufficient_candles"
            logger.warning(f"Insufficient data for {symbol} ({available} candles)")
            return
        self._analysis_error_states.pop(symbol, None)

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

        # Persist one explainable record per newly completed candle, including HOLDs.
        # Audit failure must not change strategy or order behavior.
        try:
            indicators = signal.indicators if isinstance(signal.indicators, dict) else {}
            vote_details = {}
            raw_votes = indicators.get("strategy_votes", {})
            if isinstance(raw_votes, dict):
                for name, vote in raw_votes.items():
                    if isinstance(vote, dict):
                        vote_details[str(name)[:30]] = {
                            "signal": str(vote.get("signal", "HOLD"))[:12],
                            "reason": str(vote.get("reason", ""))[:240],
                        }
            indicator_details = {}
            for key in ("market_regime", "adx", "atr_pct", "volume", "vol_avg20", "ema_trend", "consensus_required", "entry_gates"):
                value = indicators.get(key)
                if value is None or isinstance(value, (str, int, float, bool, dict)):
                    indicator_details[key] = value
            self.risk_manager.store.append_event("market_check", {
                "symbol": symbol,
                "timeframe": self.config.strategy.timeframe,
                "candle_timestamp": candle_timestamp,
                "signal": signal.signal.value,
                "reason": str(signal.reason)[:500],
                "confidence": round(float(signal.confidence), 4),
                "votes": vote_details,
                "indicators": indicator_details,
            })
        except Exception:
            logger.exception("Could not persist market decision for %s", symbol)

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
        except Exception as submit_error:
            self.risk_manager.store.update_order(client_id, "UNKNOWN")
            try:
                order = self.exchange.fetch_order_by_client_id(client_id, symbol)
                if not order:
                    raise RuntimeError("order was not returned by reconciliation")
            except Exception as reconcile_error:
                raise RuntimeError("Order submission outcome remains unknown; operator reconciliation required") from submit_error
        if not order:
            self.risk_manager.store.update_order(client_id, "REJECTED", response={"result": "rejected_without_order"})
            self.risk_manager.store.mark_order_applied(client_id)
            self.risk_manager.halt(f"{side.title()} order was rejected for {symbol}; operator review required")
            return None, client_id
        raw_status = str(order.get("status", "")).lower()
        filled = order.get("filled") or 0
        filled_decimal = Decimal(str(filled))
        state = {"closed": "FILLED", "open": "OPEN", "canceled": "CANCELED", "rejected": "REJECTED", "expired": "EXPIRED"}.get(raw_status, "UNKNOWN")
        if state == "OPEN" and filled_decimal > 0 and order.get("id"):
            if self.exchange.cancel_order(str(order["id"]), symbol):
                try:
                    order = self.exchange.fetch_order(str(order["id"]), symbol)
                    raw_status = str(order.get("status", "")).lower()
                    filled = order.get("filled") or 0
                    filled_decimal = Decimal(str(filled))
                    state = {"closed": "FILLED", "open": "OPEN", "canceled": "CANCELED", "rejected": "REJECTED", "expired": "EXPIRED"}.get(raw_status, "UNKNOWN")
                except Exception:
                    state = "UNKNOWN"
            else:
                state = "PARTIALLY_FILLED"
        if state in {"FILLED", "CANCELED", "EXPIRED"} and Decimal(str(filled)) > 0 and Decimal(str(filled)) < amount_decimal * Decimal("0.99999999"):
            state = "PARTIAL_CLOSED"
        average = order.get("average") or order.get("price")
        self.risk_manager.store.update_order(
            client_id, state, exchange_order_id=order.get("id"),
            filled=filled, average=average, response=order,
        )
        return order, client_id

    def _place_and_persist_native_stop(self, symbol: str, entry_order_id: str, fallback_sl: float | None = None, fallback_amount: float | None = None) -> bool:
        position = self.risk_manager.open_positions.get(symbol)
        amount = position.amount if position else fallback_amount
        sl_price = position.stop_loss_price if position else fallback_sl
        if not amount or not sl_price or amount <= 0 or sl_price <= 0:
            self.risk_manager.halt(f"Cannot protect missing or invalid position parameters for {symbol}")
            return False
        client_id = self._client_order_id(symbol, "stop", entry_order_id or (position.entry_time if position else int(time.time()*1000)))
        try:
            order = self.exchange.place_native_stop_loss(
                symbol, amount, sl_price, client_id
            )
            if not isinstance(order, dict) or not order.get("id") or str(order.get("status", "")).lower() != "open":
                raise RuntimeError("Exchange did not confirm an open protective stop")
            if position:
                if not self.risk_manager.attach_exchange_stop(symbol, order["id"], client_id):
                    raise RuntimeError("Protective stop reference could not be persisted")
            self.risk_manager.store.append_event("protective_stop_placed", {
                "symbol": symbol, "order_id": str(order["id"]), "client_order_id": client_id,
                "amount": str(amount), "stop_price": str(sl_price),
            })
            return True
        except Exception as exc:
            self.risk_manager.halt(f"Native stop placement/persistence failed for {symbol}; reconcile before continuing")
            logger.critical("Exchange-native stop was not confirmed for %s (%s)", symbol, type(exc).__name__)
            return False

    def _sync_exchange_stop(self, symbol: str) -> bool:
        """Safely synchronize a tightened local break-even or trailing stop to Binance (F-05)."""
        position = self.risk_manager.open_positions.get(symbol)
        if not position or not position.stop_order_id:
            return False
        old_stop_id = position.stop_order_id
        try:
            if not self.exchange.cancel_order(old_stop_id, symbol):
                logger.warning("Could not cancel old stop %s for %s during trailing sync; leaving local exit monitored", old_stop_id, symbol)
                return False
            client_id = self._client_order_id(symbol, "stop_trail", f"{int(time.time()*1000)}")
            new_stop = self.exchange.place_native_stop_loss(
                symbol, position.amount, position.stop_loss_price, client_id
            )
            if isinstance(new_stop, dict) and new_stop.get("id") and str(new_stop.get("status", "")).lower() == "open":
                position.stop_order_id = str(new_stop["id"])
                position.stop_client_order_id = client_id
                self.risk_manager._save_positions()
                self.risk_manager.store.append_event("exchange_stop_tightened", {
                    "symbol": symbol, "old_order_id": old_stop_id, "new_order_id": str(new_stop["id"]),
                    "new_stop_price": position.stop_loss_price
                })
                logger.info("Synchronized tightened stop to Binance for %s at $%s", symbol, position.stop_loss_price)
                return True
            else:
                raise RuntimeError("Replacement exchange stop not confirmed open")
        except Exception as exc:
            logger.warning("Failed to synchronize exchange stop to Binance for %s: %s; local trailing stop remains active", symbol, exc)
            return False

    def _recover_terminal_partial_sell(self, symbol: str, order: dict, client_order_id: str, requested_amount) -> bool:
        """Reconcile a terminal partial exit, protect its remainder, then halt for review."""
        if not isinstance(order, dict) or str(order.get("status", "")).lower() not in {"closed", "canceled", "expired"}:
            return False
        try:
            filled = Decimal(str(order.get("filled") or 0))
            requested = Decimal(str(requested_amount))
            if filled <= 0 or filled >= requested * Decimal("0.99999999"):
                return False
            balance, _ = self.exchange.fetch_account_snapshot()
            base = symbol.split("/")[0]
            baseline = self.risk_manager.store.read_state("baseline_asset_balances") or {}
            remaining = Decimal(str((balance.get("total") or {}).get(base) or 0)) - Decimal(str(baseline.get(base, "0")))
            if not remaining.is_finite() or remaining < 0 or remaining > requested:
                raise ValueError("partial sell balance is outside expected position bounds")
            if remaining == 0:
                fill_price = float(order.get("average") or order.get("price") or 0)
                if not math.isfinite(fill_price) or fill_price <= 0:
                    raise ValueError("partial sell response has no valid average fill price")
                if not self.risk_manager.close_position(symbol, fill_price, "Partial sell reconciled to zero remaining balance"):
                    raise ValueError("closed position could not be persisted")
            else:
                if not self.risk_manager.update_position_amount(symbol, float(remaining)):
                    raise ValueError("remaining position quantity could not be persisted")
                if not self._place_and_persist_native_stop(symbol, str(order.get("id", ""))):
                    raise ValueError("remaining position could not be protected by a new exchange stop")
            self.risk_manager.store.mark_order_applied(client_order_id)
            self.risk_manager.store.append_event("partial_exit_reconciled", {
                "symbol": symbol, "order_id": str(order.get("id", "")),
                "filled": str(filled), "remaining": str(remaining),
            })
            self.risk_manager.halt(f"Partial sell for {symbol} was reconciled; manual review required")
            return True
        except Exception as exc:
            self.risk_manager.halt(f"Partial sell for {symbol} could not be safely reconciled; operator action required")
            logger.critical("Partial sell recovery failed for %s (%s)", symbol, type(exc).__name__)
            return False

    def _reconcile_exchange_state(self) -> bool:
        """Block all order activity until authenticated account state matches local state."""
        try:
            balance, open_orders = self.exchange.fetch_account_snapshot()
            self.exchange.exchange.load_markets()
            totals = balance.get("total", {}) or {}
            configured = {pair.strip(): pair.strip().split("/")[0] for pair in self.config.strategy.trading_pairs}
            baseline = self.risk_manager.store.read_state("baseline_asset_balances")
            if baseline is None:
                if self.risk_manager.open_positions:
                    raise ValueError("baseline asset balances are missing while local positions exist")
                baseline = {base: str(totals.get(base) or 0) for base in set(configured.values())}
                self.risk_manager.store.write_state("baseline_asset_balances", baseline)
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
            expected_stop_ids = {
                str(position.stop_order_id): symbol
                for symbol, position in self.risk_manager.open_positions.items()
                if position.stop_order_id
            }
            expected_stop_client_ids = {
                str(position.stop_client_order_id): symbol
                for symbol, position in self.risk_manager.open_positions.items()
                if position.stop_client_order_id
            }
            seen_stops = set()
            for order in open_orders:
                order_id = str(order.get("id", ""))
                client_id = str(order.get("clientOrderId", ""))
                symbol = expected_stop_ids.get(order_id) or expected_stop_client_ids.get(client_id)
                if not symbol:
                    raise ValueError("unreconciled open order exists on the account")
                seen_stops.add(symbol)
            for symbol, position in list(self.risk_manager.open_positions.items()):
                if not position.stop_order_id or not position.stop_client_order_id:
                    raise ValueError(f"tracked position {symbol} has no persisted exchange stop")
                if symbol in seen_stops:
                    continue
                stop = self.exchange.fetch_order(position.stop_order_id, symbol)
                if str(stop.get("status", "")).lower() == "closed":
                    filled = Decimal(str(stop.get("filled") or 0))
                    if filled >= Decimal(str(position.amount)):
                        price = float(stop.get("average") or stop.get("price") or 0)
                        if not math.isfinite(price) or price <= 0:
                            raise ValueError(f"triggered stop for {symbol} lacks a valid fill price")
                        record = self.risk_manager.close_position(symbol, price, "Exchange-native stop-loss filled")
                        if not record:
                            raise ValueError(f"triggered stop for {symbol} could not be persisted")
                        self.risk_manager.store.append_event("protective_stop_filled", {
                            "symbol": symbol, "order_id": str(stop.get("id", position.stop_order_id)),
                            "filled": str(filled), "average": str(price),
                        })
                        self.risk_manager.halt(f"Exchange stop filled for {symbol}; manual review required before resuming")
                        self._last_account_balance = balance
                        return False
                raise ValueError(f"persisted exchange stop for {symbol} is missing or not fully filled")
            for symbol, base in configured.items():
                reported = Decimal(str(totals.get(base) or 0))
                local = Decimal(str(self.risk_manager.open_positions[symbol].amount if symbol in self.risk_manager.open_positions else 0))
                expected = Decimal(str(baseline.get(base, "0"))) + local
                if abs(reported - expected) > tolerances[base]:
                    raise ValueError(f"balance mismatch for configured asset {base}")
            if self.risk_manager.store.unresolved_orders():
                raise ValueError("unresolved local order intent exists")
            self._last_account_balance = balance
            self._last_account_error = ""
            self._last_reconciliation_ok = True
            self.risk_manager.resume_after_reconciliation()
            return True
        except Exception as exc:
            self._last_reconciliation_ok = False
            if isinstance(exc, AccountAccessError):
                self._last_account_error = str(exc)
                # Keep the durable halt in the reconciliation class so a
                # later clean private-account check can clear this transient
                # connectivity/authentication halt safely.
                reason = f"Exchange reconciliation failed: {type(exc).__name__}"
            else:
                self._last_account_error = "Account reconciliation failed. Check exchange permissions and service logs."
                reason = f"Exchange reconciliation failed: {type(exc).__name__}"
            current_halt = self.risk_manager._halt_reason
            if self.risk_manager._is_halted and not current_halt.startswith("Exchange reconciliation failed:"):
                # A transient API failure must not erase an earlier circuit
                # breaker, emergency-stop latch, or manual-review halt.
                self.risk_manager._save_daily_state()
            else:
                self.risk_manager.halt(reason)
            try:
                self.risk_manager.store.append_event("reconciliation_failed", {"reason": reason})
            except Exception:
                logger.critical("Could not append reconciliation failure to durable audit log")
            logger.error("Account state failed reconciliation; all new orders blocked (%s)", type(exc).__name__)
            return False

    def _cancel_exchange_stop(self, symbol: str) -> bool:
        position = self.risk_manager.open_positions.get(symbol)
        if not position or not position.stop_order_id:
            return True
        stop_id = position.stop_order_id
        if not self.exchange.cancel_order(stop_id, symbol):
            self.risk_manager.halt(f"Could not cancel exchange stop for {symbol}; operator reconciliation required")
            return False
        if not self.risk_manager.clear_exchange_stop(symbol):
            self.risk_manager.halt(f"Cancelled exchange stop reference could not be persisted for {symbol}")
            return False
        self.risk_manager.store.append_event("protective_stop_cancelled", {
            "symbol": symbol, "order_id": stop_id,
        })
        return True

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

    @staticmethod
    def _return_correlation(candles_a: list, candles_b: list) -> float | None:
        """Pearson correlation on aligned returns from closed candles only."""
        try:
            a = {int(row[0]): float(row[4]) for row in candles_a if len(row) >= 6}
            b = {int(row[0]): float(row[4]) for row in candles_b if len(row) >= 6}
            times = sorted(a.keys() & b.keys())
            if len(times) < 31:
                return None
            returns_a = np.diff([a[t] for t in times]) / np.asarray([a[t] for t in times[:-1]])
            returns_b = np.diff([b[t] for t in times]) / np.asarray([b[t] for t in times[:-1]])
            if not np.isfinite(returns_a).all() or not np.isfinite(returns_b).all():
                return None
            correlation = float(np.corrcoef(returns_a, returns_b)[0, 1])
            return correlation if math.isfinite(correlation) else None
        except (TypeError, ValueError, IndexError, ZeroDivisionError):
            return None

    def _has_correlated_open_position(self, symbol: str) -> tuple[bool, str]:
        """Block a new slot when a tracked holding is highly correlated."""
        if not self.risk_manager.open_positions:
            return False, ""
        candidate = self.exchange.fetch_closed_ohlcv(
            symbol, self.config.strategy.timeframe, max(60, self.config.strategy.lookback_candles)
        )
        if len(candidate) < 31:
            return True, "Not enough aligned closed candles to estimate portfolio correlation"
        for existing_symbol in self.risk_manager.open_positions:
            other = self.exchange.fetch_closed_ohlcv(
                existing_symbol, self.config.strategy.timeframe, max(60, self.config.strategy.lookback_candles)
            )
            correlation = self._return_correlation(candidate, other)
            if correlation is None:
                return True, f"Correlation data unavailable for {symbol} and {existing_symbol}"
            if correlation >= self.config.max_pairwise_correlation:
                return True, f"{symbol} is highly correlated with open position {existing_symbol} ({correlation:.2f})"
        return False, ""

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

        correlated, correlation_reason = self._has_correlated_open_position(symbol)
        if correlated:
            logger.info("⏸ Trade blocked for %s: %s", symbol, correlation_reason)
            return

        # Confidence check
        if signal.confidence < 0.5:
            logger.info(
                f"⏸ Signal too weak for {symbol}: "
                f"{signal.confidence:.0%} confidence (need ≥50%)"
            )
            return

        current_price = self.exchange.get_current_price(symbol)
        if current_price is None or current_price <= 0:
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
            raw_fill_status = str(order.get("status", "")).lower()
            filled_quantity = Decimal(str(order.get("filled") or 0))
            requested_quantity = Decimal(str(amount))
            partial_terminal_fill = (
                raw_fill_status in {"closed", "canceled", "expired"} and
                filled_quantity > 0 and filled_quantity < requested_quantity * Decimal("0.99999999")
            )
            if not self._confirmed_full_fill(order, amount) and not partial_terminal_fill:
                self.risk_manager.halt(f"Unconfirmed or partial buy fill for {symbol}; reconcile exchange orders and balances")
                logger.error("Buy order for %s was not confirmed fully filled; new orders halted", symbol)
                return
            fill_price = float(order.get("average") or order.get("price") or 0)
            net_filled = self._net_base_filled(order, symbol)
            fee_ambiguous = False
            if net_filled is None and order.get("id"):
                try:
                    refetched = self.exchange.exchange.fetch_order(order.get("id"), symbol)
                    if isinstance(refetched, dict):
                        order = refetched
                        net_filled = self._net_base_filled(order, symbol)
                except Exception:
                    pass
            if net_filled is None:
                fee_ambiguous = True
                net_filled = filled_quantity
                self.risk_manager.store.append_event("buy_fee_missing_reconciled", {
                    "symbol": symbol, "order_id": str(order.get("id", "")), "filled": str(filled_quantity)
                })

            fill_amount = float(net_filled)
            if fill_price <= 0 or fill_amount <= 0:
                self.risk_manager.halt(f"Invalid buy fill details for {symbol}; reconcile exchange orders and balances")
                return

            try:
                self.risk_manager.open_position(
                    symbol=symbol,
                    entry_price=fill_price,
                    amount=fill_amount,
                    order_id=order.get("id", ""),
                    atr=atr
                )
            except Exception as exc:
                logger.error("Failed to open local position: %s", exc)

            # F-02: Even if local persistence failed or risk_manager is halted,
            # we MUST still place the native stop loss on the exchange for the acquired coins!
            stop_placed = self._place_and_persist_native_stop(
                symbol, order.get("id", ""), fallback_sl=stop_loss_price, fallback_amount=fill_amount
            )

            if self.risk_manager._is_halted or not stop_placed:
                logger.error("Position or stop persistence issue after buy for %s; stop_placed: %s; trading halted", symbol, stop_placed)
                self.risk_manager.store.append_event("unpersisted_entry_incident", {
                    "symbol": symbol, "fill_amount": fill_amount, "fill_price": fill_price,
                    "stop_placed": stop_placed, "order_id": str(order.get("id", ""))
                })
                return

            if fee_ambiguous:
                self.risk_manager.halt(f"Buy fee details were incomplete for {symbol}; position protected with stop, operator reconciliation required")
                return

            self.risk_manager.store.mark_order_applied(client_order_id)
            if partial_terminal_fill:
                self.risk_manager.halt(f"Partial entry fill for {symbol} was protected; manual review required")
                self.risk_manager.store.append_event("partial_entry_fill", {
                    "symbol": symbol, "order_id": str(order.get("id", "")),
                    "requested": str(requested_quantity), "filled": str(filled_quantity),
                    "protected_amount": str(net_filled),
                })

            pos = self.risk_manager.positions.get(symbol)
            sl_text = f"${pos.stop_loss_price:,.2f}" if pos else "N/A"
            tp_text = f"${pos.take_profit_price:,.2f}" if pos else "N/A"

            msg = (
                f"🟢 *BOT BUY EXECUTED*\n"
                f"Pair: `{symbol}`\n"
                f"Amount: `{fill_amount:.6f}`\n"
                f"Entry Price: `${fill_price:,.2f}`\n"
                f"Stop Loss: `{sl_text}`\n"
                f"Take Profit: `{tp_text}`\n"
                f"Reason: {signal.reason}"
            )
            self.send_notification(msg, subject=f"🟢 Bot BUY: {symbol} @ ${fill_price:,.2f}")

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
        if current_price is None or current_price <= 0:
            return

        position = self.risk_manager.open_positions[symbol]
        old_stop_price = position.stop_loss_price
        position_amount = position.amount

        if not self._cancel_exchange_stop(symbol):
            return

        logger.info(
            f"🔻 Executing SELL: {position_amount:.6f} {symbol} @ ~${current_price:,.2f} "
            f"| Confidence: {signal.confidence:.0%}"
        )

        try:
            order, client_order_id = self._submit_market_order(symbol, "sell", position_amount, decision_key)
        except Exception as exc:
            self._place_and_persist_native_stop(symbol, f"reprotect_{int(time.time()*1000)}", fallback_sl=old_stop_price, fallback_amount=position_amount)
            self.risk_manager.halt(f"Sell order state requires reconciliation for {symbol}; protective stop restored")
            logger.error("Sell order state requires reconciliation for %s: %s; protective stop restored", symbol, exc)
            return
        if order:
            if not self._confirmed_full_fill(order, position_amount):
                if self._recover_terminal_partial_sell(symbol, order, client_order_id, position_amount):
                    return
                self._place_and_persist_native_stop(symbol, f"reprotect_{int(time.time()*1000)}", fallback_sl=old_stop_price, fallback_amount=position_amount)
                self.risk_manager.halt(f"Unconfirmed or partial sell fill for {symbol}; protective stop restored, reconcile exchange orders and balances")
                logger.error("Sell order for %s was not confirmed fully filled; position remains protected", symbol)
                return
            fill_price = float(order.get("average") or order.get("price") or 0)
            if fill_price <= 0:
                self._place_and_persist_native_stop(symbol, f"reprotect_{int(time.time()*1000)}", fallback_sl=old_stop_price, fallback_amount=position_amount)
                self.risk_manager.halt(f"Invalid sell fill details for {symbol}; protective stop restored, reconcile exchange orders and balances")
                return

            fee_cost = 0.0
            if isinstance(order, dict):
                if isinstance(order.get("fee"), dict) and "cost" in order["fee"]:
                    try:
                        fee_cost = float(order["fee"]["cost"] or 0.0)
                    except (TypeError, ValueError):
                        pass
                elif isinstance(order.get("fees"), list):
                    try:
                        fee_cost = sum(float(f.get("cost", 0) or 0) for f in order["fees"])
                    except (TypeError, ValueError):
                        pass

            record = self.risk_manager.close_position(
                symbol=symbol,
                exit_price=fill_price,
                reason=f"Strategy signal: {signal.reason}",
                fee_cost=fee_cost
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
                self.send_notification(msg, subject=f"🔴 Bot SELL: {symbol} (P&L: ${record.pnl:+,.2f})")

                self._log_trade({
                    "action": "SELL",
                    "symbol": symbol,
                    "price": fill_price,
                    "amount": record.amount,
                    "pnl": record.pnl,
                    "pnl_pct": record.pnl_pct,
                    "fee": record.fee,
                    "reason": record.reason,
                })

    def check_stop_loss_take_profit(self):
        """Check all open positions for Trailing SL, Breakeven, and TP triggers."""
        # 1. Sync any tightened break-even or trailing stops to Binance exchange (F-05)
        for sym, pos in list(self.risk_manager.open_positions.items()):
            cur_p = self.exchange.get_current_price(sym)
            if cur_p is not None and cur_p > 0:
                gain_pct = ((cur_p - pos.entry_price) / pos.entry_price) * 100
                if gain_pct >= 1.5 and not pos.break_even_activated:
                    pos.stop_loss_price = max(pos.stop_loss_price, pos.entry_price)
                    pos.break_even_activated = True
                    self.risk_manager._save_positions()
                    self._sync_exchange_stop(sym)
                elif pos.break_even_activated:
                    trailing_sl = pos.highest_price * (1 - 0.015)
                    if trailing_sl > pos.stop_loss_price:
                        pos.stop_loss_price = trailing_sl
                        self.risk_manager._save_positions()
                        self._sync_exchange_stop(sym)

        to_close = self.risk_manager.check_positions(
            self.exchange.get_current_price
        )

        for symbol, reason in to_close:
            current_price = self.exchange.get_current_price(symbol)
            if current_price is None or current_price <= 0:
                continue
            position = self.risk_manager.open_positions.get(symbol)
            if not position:
                continue

            old_stop_price = position.stop_loss_price
            position_amount = position.amount

            if not self._cancel_exchange_stop(symbol):
                return

            logger.info(f"🔻 Auto-closing {symbol}: {reason}")

            try:
                key = f"stop:{position.order_id}:{reason}:{int(time.time() // 60)}"
                order, client_order_id = self._submit_market_order(symbol, "sell", position_amount, key)
            except Exception as exc:
                self._place_and_persist_native_stop(symbol, f"reprotect_{int(time.time()*1000)}", fallback_sl=old_stop_price, fallback_amount=position_amount)
                self.risk_manager.halt(f"Stop-exit order state requires reconciliation for {symbol}; protective stop restored")
                logger.error("Stop-exit order state requires reconciliation for %s: %s; protective stop restored", symbol, exc)
                return
            if order:
                if not self._confirmed_full_fill(order, position_amount):
                    if self._recover_terminal_partial_sell(symbol, order, client_order_id, position_amount):
                        return
                    self._place_and_persist_native_stop(symbol, f"reprotect_{int(time.time()*1000)}", fallback_sl=old_stop_price, fallback_amount=position_amount)
                    self.risk_manager.halt(f"Unconfirmed or partial stop-exit for {symbol}; protective stop restored, reconcile exchange orders and balances")
                    logger.error("Stop-exit for %s was not confirmed fully filled; position remains protected", symbol)
                    return
                fill_price = float(order.get("average") or order.get("price") or 0)
                if fill_price <= 0:
                    self._place_and_persist_native_stop(symbol, f"reprotect_{int(time.time()*1000)}", fallback_sl=old_stop_price, fallback_amount=position_amount)
                    self.risk_manager.halt(f"Invalid stop-exit fill details for {symbol}; protective stop restored, reconcile exchange orders and balances")
                    return
                fee_cost = 0.0
                if isinstance(order, dict):
                    if isinstance(order.get("fee"), dict) and "cost" in order["fee"]:
                        try:
                            fee_cost = float(order["fee"]["cost"] or 0.0)
                        except (TypeError, ValueError):
                            pass
                    elif isinstance(order.get("fees"), list):
                        try:
                            fee_cost = sum(float(f.get("cost", 0) or 0) for f in order["fees"])
                        except (TypeError, ValueError):
                            pass

                record = self.risk_manager.close_position(symbol, fill_price, reason, fee_cost=fee_cost)

                if record:
                    self.risk_manager.store.mark_order_applied(client_order_id)
                    msg = (
                        f"🛡️ *POSITION CLOSED ({reason})*\n"
                        f"Pair: `{symbol}`\n"
                        f"Exit Price: `${fill_price:,.2f}`\n"
                        f"P&L: `${record.pnl:+,.2f}` ({record.pnl_pct:+.2f}%)"
                    )
                    self.send_notification(msg, subject=f"🛡️ Position Closed: {symbol} ({reason})")

                    self._log_trade({
                        "action": "SELL",
                        "symbol": symbol,
                        "price": fill_price,
                        "amount": record.amount,
                        "pnl": record.pnl,
                        "pnl_pct": record.pnl_pct,
                        "fee": record.fee,
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
                if current is None or current <= 0:
                    print(f"    ⚠️ {symbol}: price unavailable | SL: ${pos.stop_loss_price:,.2f} | TP: ${pos.take_profit_price:,.2f}")
                    continue
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

    def emergency_stop(self) -> tuple[bool, str]:
        """Pause entries, cancel exchange orders, close tracked spot positions, and latch halt."""
        if not self._cycle_lock.acquire(timeout=30):
            self.risk_manager.set_exit_only()
            self.risk_manager.halt("Emergency stop requested while a trading cycle was still running")
            return False, "Could not acquire the trading lock; entries are paused and the bot is halted."
        try:
            self.risk_manager.set_exit_only()
            try:
                _, open_orders = self.exchange.fetch_account_snapshot()
                bot_cancelled_count = 0
                non_bot_untouched_count = 0
                for order in open_orders:
                    cid = str(order.get("clientOrderId") or "")
                    oid = str(order.get("id") or "")
                    sym = order.get("symbol")
                    is_bot_order = (
                        cid.startswith("cb") or
                        cid.startswith("testnet-smoke") or
                        cid.startswith("sim_") or
                        cid.startswith("client_") or
                        cid.startswith("stop") or
                        self.risk_manager.store.is_known_client_order(cid)
                    )
                    if is_bot_order:
                        if not self.exchange.cancel_order(oid, sym):
                            raise RuntimeError(f"Bot-owned order {oid} ({cid}) could not be cancelled")
                        bot_cancelled_count += 1
                    else:
                        non_bot_untouched_count += 1
                        logger.info("Emergency stop: leaving external/manual order %s (%s) untouched", oid, cid)
            except Exception as exc:
                self.risk_manager.halt("Emergency stop could not verify/cancel exchange orders; operator action required")
                detail = str(exc) if isinstance(exc, AccountAccessError) else f"Exchange-order cancellation failed ({type(exc).__name__})"
                return False, f"Entries paused. {detail} Inspect the Testnet account before attempting recovery."

            for symbol, position in list(self.risk_manager.open_positions.items()):
                try:
                    key = f"emergency-stop:{position.order_id}:{datetime.now(timezone.utc).date().isoformat()}"
                    order, client_id = self._submit_market_order(symbol, "sell", position.amount, key)
                    if not self._confirmed_full_fill(order, position.amount):
                        if self._recover_terminal_partial_sell(symbol, order, client_id, position.amount):
                            return False, f"Emergency stop reconciled a partial close for {symbol}; remaining amount was re-protected and trading is halted."
                        raise RuntimeError("the close order was not fully confirmed")
                    fill_price = float(order.get("average") or order.get("price") or 0)
                    if not math.isfinite(fill_price) or fill_price <= 0:
                        raise RuntimeError("the exchange returned invalid fill details")
                    record = self.risk_manager.close_position(symbol, fill_price, "Emergency stop")
                    if not record:
                        raise RuntimeError("closed-position state could not be persisted")
                    self.risk_manager.store.mark_order_applied(client_id)
                except Exception as exc:
                    self.risk_manager.halt(f"Emergency stop failed to close tracked position {symbol}; operator action required")
                    return False, f"Entries paused. Could not safely confirm the close for {symbol} ({type(exc).__name__})."

            self.risk_manager.halt("Emergency stop complete; manual review required before resuming")
            msg = f"Bot-owned exchange orders ({bot_cancelled_count}) were cancelled and tracked positions were closed. Trading is halted."
            if non_bot_untouched_count > 0:
                msg += f" Note: {non_bot_untouched_count} external/manual order(s) were left untouched."
            return True, msg
        finally:
            self._cycle_lock.release()

    def pause_entries(self) -> tuple[bool, str]:
        """Serialize the entry pause against order cycles and durably record it."""
        if not self._cycle_lock.acquire(timeout=30):
            return False, "The trading cycle is still busy. Pause was not confirmed; retry and verify the status."
        try:
            self.risk_manager.set_exit_only()
            return True, "New entries paused. Existing positions remain monitored."
        except Exception:
            self.risk_manager.halt("Entry pause could not be saved; operator review required")
            logger.exception("Could not persist entry pause")
            return False, "Pause could not be saved. Trading is halted; inspect service logs."
        finally:
            self._cycle_lock.release()

    def resume_entries(self, acknowledge_manual_halt: bool = False) -> tuple[bool, str]:
        """Resume a deliberate entry pause only after a fresh clean account reconciliation."""
        if not self._cycle_lock.acquire(timeout=10):
            return False, "The trading worker is busy. Wait for the current account check and try again."
        try:
            if self.risk_manager.entry_mode() != "EXIT_ONLY":
                return False, "New entries are not paused."
            if not self._reconcile_exchange_state():
                reason = self._last_account_error or self.risk_manager._halt_reason
                return False, f"Cannot resume: account reconciliation failed. {reason}"
            if self.risk_manager._is_halted:
                emergency_stop_needs_review = self.risk_manager._halt_reason.startswith("Emergency stop")
                if not (acknowledge_manual_halt and emergency_stop_needs_review):
                    return False, f"Cannot resume while a safety halt is active: {self.risk_manager._halt_reason}"
                if self.risk_manager.open_positions or self.risk_manager.store.unresolved_orders():
                    return False, "Manual recovery is blocked while positions or unresolved orders remain. Inspect the Testnet account."
                if not self.risk_manager.acknowledge_emergency_stop_review():
                    return False, "The emergency-stop review could not be saved; trading remains halted."
            self.risk_manager.store.write_state("entry_mode", "ACTIVE")
            self.risk_manager.store.append_event("mode_changed", {"mode": "ACTIVE", "source": "operator_resume"})
            return True, "Entries resumed after successful account reconciliation."
        except Exception as exc:
            self.risk_manager.halt("Resume request could not be saved; operator review required")
            logger.exception("Could not safely resume entries (%s)", type(exc).__name__)
            return False, "Resume could not be saved. Trading remains paused; inspect service logs."
        finally:
            self._cycle_lock.release()

    def run_testnet_smoke_trade(self) -> dict:
        """One-time <=10 USDT buy/sell routing check; this is not a strategy signal."""
        if not self.config.exchange.paper_trading or self.config.exchange.paper_environment != "testnet":
            raise RuntimeError("Smoke trades are available only on Binance Spot Testnet")
        if not self._cycle_lock.acquire(timeout=10):
            raise RuntimeError("Trading cycle is busy; retry after it finishes")
        try:
            if self.risk_manager._is_halted or self.risk_manager.entry_mode() != "ACTIVE":
                raise RuntimeError("Trading is halted or exit-only mode is active")
            if self.risk_manager.open_positions:
                raise RuntimeError("Close or reconcile existing bot positions before the smoke trade")
            if self.risk_manager.store.read_state("testnet_smoke_trade") is not None:
                raise RuntimeError("The one-time Testnet smoke trade was already attempted")
            if not self._reconcile_exchange_state():
                raise RuntimeError("Exchange reconciliation failed; no test order was sent")

            symbol = self.config.strategy.trading_pairs[0].strip()
            if not self.risk_manager.can_open_position(symbol, self.exchange.get_usdt_balance())[0]:
                raise RuntimeError("Risk controls currently block a new position")
            book = self.exchange.get_order_book(symbol)
            best_bid, best_ask = Decimal(str(book["bids"][0][0])), Decimal(str(book["asks"][0][0]))
            spread_bps = (best_ask - best_bid) / ((best_ask + best_bid) / 2) * Decimal(10000)
            if spread_bps > Decimal(str(self.config.max_spread_bps)):
                raise RuntimeError("Spread is above the configured entry limit")
            amount = Decimal(self.exchange.amount_to_precision(symbol, Decimal("10") / best_ask))
            limits = self.exchange.get_market_limits(symbol)
            cost = amount * best_ask
            if (amount <= 0 or cost > Decimal("10") or amount < Decimal(str(limits.get("min_amount") or 0)) or
                    cost < Decimal(str(limits.get("min_cost") or 0)) or not limits.get("active", True)):
                raise RuntimeError("A <=10 USDT test order does not satisfy the current exchange filters")
            visible = sum((Decimal(str(qty)) for price, qty in book["asks"]
                           if Decimal(str(price)) <= best_ask * (1 + Decimal(str(self.config.max_slippage_bps)) / 10000)), Decimal("0"))
            if visible < amount:
                raise RuntimeError("Insufficient visible ask depth within the configured slippage limit")

            marker = {"state": "started", "symbol": symbol, "notional_cap_usdt": "10"}
            self.risk_manager.store.write_state("testnet_smoke_trade", marker)
            buy, buy_client_id = self._submit_market_order(symbol, "buy", amount, "testnet-smoke-buy")
            if not self._confirmed_full_fill(buy, amount):
                self.risk_manager.halt("Testnet smoke buy was not confirmed fully filled")
                raise RuntimeError("Buy was not fully confirmed; account reconciliation is required")
            net_amount = self._net_base_filled(buy, symbol)
            fill_price = float(buy.get("average") or buy.get("price") or 0)
            if net_amount is None or not math.isfinite(fill_price) or fill_price <= 0:
                self.risk_manager.halt("Testnet smoke buy fee/fill data was incomplete")
                raise RuntimeError("Buy fill or fee details were incomplete")
            self.risk_manager.open_position(symbol, fill_price, float(net_amount), buy.get("id", ""))
            if self.risk_manager._is_halted:
                raise RuntimeError("Bought position could not be persisted")
            if not self._place_and_persist_native_stop(symbol, buy.get("id", "")):
                raise RuntimeError("Bought position could not be protected by a confirmed Testnet stop")
            self.risk_manager.store.mark_order_applied(buy_client_id)

            # Remove the protective order only after the matching sell is
            # about to be submitted. If cancellation cannot be confirmed, do
            # not race a market sell against the still-open stop.
            if not self._cancel_exchange_stop(symbol):
                raise RuntimeError("Could not safely cancel the temporary stop before the Testnet sell")
            sell, sell_client_id = self._submit_market_order(symbol, "sell", net_amount, "testnet-smoke-sell")
            if not self._confirmed_full_fill(sell, net_amount):
                if self._recover_terminal_partial_sell(symbol, sell, sell_client_id, net_amount):
                    raise RuntimeError("Partial Testnet sell was reconciled, re-protected, and halted for review")
                self.risk_manager.halt("Testnet smoke sell was not confirmed fully filled; position remains tracked")
                raise RuntimeError("Sell was not fully confirmed; use the emergency stop and reconcile")
            sell_price = float(sell.get("average") or sell.get("price") or 0)
            if not math.isfinite(sell_price) or sell_price <= 0:
                self.risk_manager.halt("Testnet smoke sell returned invalid fill details")
                raise RuntimeError("Sell fill details were invalid")
            record = self.risk_manager.close_position(symbol, sell_price, "Testnet order-path smoke check")
            if not record:
                raise RuntimeError("Closed-position state could not be persisted")
            self.risk_manager.store.mark_order_applied(sell_client_id)
            result = {
                "state": "complete", "symbol": symbol,
                "buy_order_id": str(buy.get("id", "")),
                "sell_order_id": str(sell.get("id", "")),
                "buy_price": fill_price, "sell_price": sell_price,
                "amount": str(net_amount), "pnl_before_fees": record.pnl,
            }
            self.risk_manager.store.write_state("testnet_smoke_trade", result)
            return result
        finally:
            self._cycle_lock.release()

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
                totals = self._last_account_balance.get("total", {})
                equity = Decimal(str(totals.get("USDT") or 0))
                for base in sorted({pair.strip().split("/")[0] for pair in self.config.strategy.trading_pairs}):
                    symbol = next(pair.strip() for pair in self.config.strategy.trading_pairs if pair.strip().split("/")[0] == base)
                    quantity = Decimal(str(totals.get(base) or 0))
                    if quantity > 0:
                        raw_price = self.exchange.get_current_price(symbol)
                        if raw_price is None or raw_price <= 0:
                            raise ValueError(f"market valuation is unavailable for {symbol}")
                        mark = Decimal(str(raw_price))
                        if not mark.is_finite() or mark <= 0:
                            raise ValueError("market valuation is unavailable")
                        equity += quantity * mark
                if not self.risk_manager.monitor_equity(float(equity)):
                    logger.error("Equity risk circuit breaker is active: %s", self.risk_manager._halt_reason)
            except Exception as exc:
                self.risk_manager.halt("Account equity valuation failed; operator review required")
                logger.error("Equity valuation failed; new orders blocked (%s)", type(exc).__name__)
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
                except Exception as exc:
                    error_type = type(exc).__name__
                    if self._analysis_error_states.get(symbol) != error_type:
                        try:
                            self.risk_manager.store.append_event("analysis_error", {
                                "symbol": symbol, "error_type": error_type,
                            })
                        except Exception:
                            logger.exception("Could not record analysis error for %s", symbol)
                    self._analysis_error_states[symbol] = error_type
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
