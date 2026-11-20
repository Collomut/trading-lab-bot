"""
Trading Bot Engine — the main loop that ties everything together.

This is the orchestrator that:
  1. Fetches real-time market data
  2. Runs strategy analysis with a fixed 50 EMA trend & volume filter
  3. Checks risk management rules (with ATR & break-even protection)
  4. Executes trades when conditions are met
  5. Monitors open positions for trailing stop-loss / take-profit
  6. Dispatches notifications (Console + File + optional Telegram)
"""

import logging
import logging.handlers
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
from strategies import (
    DowntrendRsiRecoveryTestnetStrategy, MacroTrendPullbackStrategy, get_strategy, ohlcv_to_dataframe,
    Signal, TradeSignal,
)
from risk_manager import RiskManager
from shadow_strategy import CANDIDATE_ID, evaluate_shadow_candidate
from regime_router import evaluate_regime_router
from shadow_ledger import ShadowLedger

logger = logging.getLogger("bot.engine")
ESTIMATED_TAKER_FEE_RATE = 0.001


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
            ema72_max_atr_pct=config.strategy.ema72_max_atr_pct,
        )
        self.downtrend_recovery_strategy = DowntrendRsiRecoveryTestnetStrategy(
            rsi_recovery_level=getattr(
                config.strategy, "downtrend_rsi_recovery_level", 25.0
            )
        )
        self.risk_manager = RiskManager(config.risk, persistence_dir=config.data_dir)
        self.shadow_ledger = ShadowLedger(self.risk_manager.store)
        if (config.strategy.name in {"macro_donchian_ema72", "dynamic_regime", "macro_trend_pullback"} and any(
                position.exit_policy not in {"ema72_signal", "downtrend_rsi_recovery", "ema36_signal"}
                for position in self.risk_manager.open_positions.values()
        )):
            self.risk_manager.halt(
                "Existing position uses a different exit policy; reconcile it before starting the paper profile"
            )
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

            # File handler with size-based rotation so a long-running worker
            # cannot fill the persistent volume.
            file_handler = logging.handlers.RotatingFileHandler(
                self.config.log_file, encoding="utf-8",
                maxBytes=10 * 1024 * 1024, backupCount=3,
            )
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

    def _log_closed_trade(self, record, reason: str, exit_price: float, order_id: str = ""):
        """Persist every confirmed position close with full attribution."""
        if not record:
            return
        self._log_trade({
            "action": "SELL",
            "symbol": record.symbol,
            "price": exit_price,
            "amount": record.amount,
            "entry_price": record.entry_price,
            "entry_time": record.entry_time,
            "pnl": record.pnl,
            "pnl_pct": record.pnl_pct,
            "fee": record.fee,
            "accounting_complete": record.accounting_complete,
            "order_id": str(order_id or record.order_id or ""),
            "reason": reason,
            "strategy": record.strategy,
            "route": record.route,
            "regime": record.regime,
            "r_multiple": record.r_multiple,
            "simulated": record.simulated,
        })

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
            # SMTP exceptions can include server addresses and authentication
            # details. Keep diagnostics useful without logging exception text.
            logger.warning("Failed to send email notification (%s)", type(e).__name__)

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
            # urllib errors can echo the full webhook URL, which contains its
            # bearer credential. Never write that URL to application logs.
            logger.warning("Failed to send Discord webhook notification (%s)", type(e).__name__)

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
            gap_issues = getattr(self.exchange, "_candle_sequence_issues", {})
            gap_details = gap_issues.get(symbol, {}) if isinstance(gap_issues, dict) else {}
            issue = "candle_sequence_gap" if gap_details else "insufficient_candles"
            issue_state = f"{issue}:{gap_details.get('expected_timestamp')}:{gap_details.get('actual_timestamp')}"
            if self._analysis_error_states.get(symbol) != issue_state:
                try:
                    self.risk_manager.store.append_event("market_data_issue", {
                        "symbol": symbol, "issue": issue, "available_candles": available,
                        **gap_details,
                    })
                except Exception:
                    logger.exception("Could not record insufficient market data for %s", symbol)
            self._analysis_error_states[symbol] = issue_state
            logger.warning(f"Insufficient data for {symbol} ({available} candles)")
            return
        self._analysis_error_states.pop(symbol, None)

        candle_timestamp = int(ohlcv[-1][0])
        if self._last_analyzed_candle.get(symbol) == candle_timestamp:
            logger.debug("Skipping duplicate closed candle for %s at %s", symbol, candle_timestamp)
            return

        # Capture observable execution conditions for each completed-candle
        # decision, including HOLDs. This is diagnostic only and never gates an
        # entry; the actual pre-trade spread/depth check still runs at order time.
        top_bid = top_ask = measured_spread_bps = None
        spread_sample_status = "unavailable"
        try:
            ticker = self.exchange.get_ticker(symbol)
            if isinstance(ticker, dict):
                bid, ask = ticker.get("bid"), ticker.get("ask")
                if bid is not None and ask is not None:
                    bid, ask = float(bid), float(ask)
                    if math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask:
                        mid = (bid + ask) / 2.0
                        top_bid, top_ask = bid, ask
                        measured_spread_bps = round((ask - bid) / mid * 10000.0, 2)
                        spread_sample_status = "ok"
                    else:
                        spread_sample_status = "invalid_book"
        except (TypeError, ValueError, OverflowError):
            spread_sample_status = "invalid_book"
        except Exception as ticker_exc:
            # Do not let optional telemetry interfere with strategy analysis.
            logger.debug("Could not sample order-book spread for %s (%s)", symbol, type(ticker_exc).__name__)

        # Classify before selecting a strategy. Dynamic mode enables the EMA72
        # candidate in stable uptrends and the separately risk-capped recovery
        # experiment in stable downtrends. Both routes are Testnet-only.
        strategy_name = str(getattr(self.config.strategy, "name", "combined")).strip().lower()
        dynamic_mode = strategy_name == "dynamic_regime"
        router_observation = {"status": "unavailable", "regime": "uncertain",
                              "route": "no_validated_strategy", "entry_route_enabled": False,
                              "reason": "The hourly regime router is unavailable; new entries are disabled in dynamic mode."}
        if self.config.strategy.timeframe == "1h":
            try:
                router_observation = evaluate_regime_router(
                    ohlcv,
                    strategy_name,
                    enable_downtrend_recovery=getattr(
                        self.config.strategy, "enable_downtrend_recovery_testnet", False
                    ),
                )
            except Exception as exc:
                logger.warning("Regime routing unavailable for %s (%s)", symbol, type(exc).__name__)
                router_observation["reason"] = f"The market-regime check failed ({type(exc).__name__}); new entries are disabled."
                router_observation["reason_code"] = f"router_error_{type(exc).__name__}"[:64]

        # Convert to DataFrame and run the selected strategy once per candle.
        df = ohlcv_to_dataframe(ohlcv)
        has_position = symbol in getattr(self.risk_manager, "open_positions", {})
        ema72_profile = strategy_name in {"macro_donchian_ema72", "dynamic_regime"}
        pullback_profile = strategy_name == "macro_trend_pullback"
        open_position = getattr(self.risk_manager, "open_positions", {}).get(symbol)
        if dynamic_mode and open_position and open_position.exit_policy == "downtrend_rsi_recovery":
            signal = self.downtrend_recovery_strategy.analyze(df, symbol, in_position=True)
        elif dynamic_mode and not has_position and not router_observation.get("entry_route_enabled", False):
            reason = str(router_observation.get("reason") or "No validated strategy for this regime.")
            signal = TradeSignal(
                Signal.HOLD, symbol, 0.0, f"No entry strategy selected · {reason}",
                {
                    "strategy": "dynamic_regime",
                    "selected_strategy": None,
                    "regime": router_observation.get("regime", "uncertain"),
                },
            )
        elif dynamic_mode and not has_position and router_observation.get("selected_strategy") == "downtrend_rsi_recovery_testnet":
            signal = self.downtrend_recovery_strategy.analyze(df, symbol, in_position=False)
        elif ema72_profile or pullback_profile:
            # Existing positions always use the exit policy selected at entry,
            # even after a regime change. The router only governs flat-symbol entries.
            signal = self.strategy.analyze(df, symbol, in_position=has_position)
        else:
            signal = self.strategy.analyze(df, symbol)
        if isinstance(signal.indicators, dict):
            signal.indicators.setdefault("regime", router_observation.get("regime", ""))
            signal.indicators.setdefault("route", router_observation.get("route", ""))
        self._last_analyzed_candle[symbol] = candle_timestamp
        self.last_signals[symbol] = {
            "signal": signal.signal.value,
            "reason": signal.reason,
            "confidence": signal.confidence,
            "indicators": signal.indicators,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        self.last_signals[symbol]["regime_router"] = router_observation

        # Research-only observer. It reads the closed raw candles separately
        # and is never routed to the active strategy or the order handlers.
        shadow = {"candidate_id": CANDIDATE_ID, "signal": "HOLD", "status": "unavailable"}
        try:
            shadow = evaluate_shadow_candidate(ohlcv)
        except Exception as exc:
            # A shadow-data failure must not affect active analysis or execution.
            shadow = {
                "candidate_id": CANDIDATE_ID,
                "signal": "HOLD",
                "status": f"error_{type(exc).__name__}"[:48],
            }

        # Advance the halt-free shadow ledger for the EMA72 candidate so its
        # forward expectancy is measured independently of risk halts and the
        # regime router. This is the evidence source for choosing the active
        # strategy; it never places an order.
        try:
            if ema72_profile:
                shadow_has_position = self.shadow_ledger.has_position("macro_donchian_ema72", symbol)
                raw = self.strategy.analyze(df, symbol, in_position=shadow_has_position)
                raw_indicators = raw.indicators if isinstance(raw.indicators, dict) else {}
                swing = raw_indicators.get("swing_low_5")
                atr_v = raw_indicators.get("atr", 0.0)
                self.shadow_ledger.advance(
                    "macro_donchian_ema72", symbol, ohlcv[-1], raw.signal.value,
                    stop_ref=float(swing) if swing is not None else None,
                    atr=float(atr_v) if atr_v is not None else 0.0,
                )
            elif pullback_profile:
                shadow_has_position = self.shadow_ledger.has_position("macro_trend_pullback", symbol)
                raw = self.strategy.analyze(df, symbol, in_position=shadow_has_position)
                raw_indicators = raw.indicators if isinstance(raw.indicators, dict) else {}
                swing = raw_indicators.get("swing_low_5")
                atr_v = raw_indicators.get("atr", 0.0)
                self.shadow_ledger.advance(
                    "macro_trend_pullback", symbol, ohlcv[-1], raw.signal.value,
                    stop_ref=float(swing) if swing is not None else None,
                    atr=float(atr_v) if atr_v is not None else 0.0,
                )
        except Exception as exc:
            logger.debug("Shadow ledger advance failed for %s (%s)", symbol, type(exc).__name__)

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
            for key in (
                "market_regime", "adx", "atr_pct", "volume", "vol_avg20", "ema_trend",
                "consensus_required", "entry_gates", "close", "ema480", "ema72",
                "ema20", "rsi", "donchian_high168", "swing_low_5", "entry_ready", "exit_ready",
                "breakout_ready", "max_atr_pct", "rsi_recovery_level", "strategy",
            ):
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
                "measured_spread_bps": measured_spread_bps,
                "top_bid": top_bid,
                "top_ask": top_ask,
                "spread_sample_status": spread_sample_status,
                "votes": vote_details,
                "indicators": indicator_details,
                "shadow": shadow,
                "regime_router": router_observation,
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
        state = {
            "closed": "FILLED", "open": "OPEN", "new": "OPEN",
            "partially_filled": "PARTIALLY_FILLED", "canceled": "CANCELED",
            "rejected": "REJECTED", "expired": "EXPIRED",
        }.get(raw_status, "UNKNOWN")
        if state in {"OPEN", "PARTIALLY_FILLED"}:
            # Market orders should finish quickly. Cancel any remaining
            # quantity and re-read the exchange state before a caller treats
            # the order as terminal or places another sell/stop order.
            order_id = str(order.get("id") or "")
            if not order_id:
                state = "UNKNOWN"
            else:
                self.exchange.cancel_order(order_id, symbol)
                try:
                    order = self.exchange.fetch_order(order_id, symbol)
                    raw_status = str(order.get("status", "")).lower()
                    filled = order.get("filled") or 0
                    filled_decimal = Decimal(str(filled))
                    state = {
                        "closed": "FILLED", "open": "OPEN", "new": "OPEN",
                        "partially_filled": "PARTIALLY_FILLED", "canceled": "CANCELED",
                        "rejected": "REJECTED", "expired": "EXPIRED",
                    }.get(raw_status, "UNKNOWN")
                except Exception:
                    state = "UNKNOWN"
            if state in {"OPEN", "PARTIALLY_FILLED"} and filled_decimal > 0:
                state = "PARTIALLY_FILLED"
        if state in {"FILLED", "CANCELED", "EXPIRED"} and Decimal(str(filled)) > 0 and Decimal(str(filled)) < amount_decimal * Decimal("0.99999999"):
            state = "PARTIAL_CLOSED"
        average = order.get("average") or order.get("price")
        self.risk_manager.store.update_order(
            client_id, state, exchange_order_id=order.get("id"),
            filled=filled, average=average, response=order,
        )
        return order, client_id

    def _post_only_limit_chase_buy(self, symbol: str, amount, decision_key: str, *, max_requotes: int = 5):
        """Maker-first BUY entry using a post-only limit chase (zero taker fee).

        Posts a post-only limit at the current best bid and, while unfilled,
        cancels and re-quotes one touch higher, up to `max_requotes` times or
        until the book moves beyond the configured `max_slippage_bps` cap, at
        which point the entry is aborted with no fill. Never crosses the spread,
        so the taker fee and spread-crossing slippage are eliminated.

        Mirrors `_submit_market_order`'s intent journal and reconciliation so an
        ambiguous exchange outcome still fails closed to operator review. Returns
        `(order, client_id)` like `_submit_market_order`, where `order` is None
        when the entry was aborted.
        """
        amount_decimal = Decimal(str(amount))
        if not amount_decimal.is_finite() or amount_decimal <= 0:
            raise ValueError("Order amount must be a finite positive decimal")
        client_id = self._client_order_id(symbol, "buy", str(decision_key))
        self.risk_manager.store.create_order_intent(
            client_id, symbol, "buy", format(amount_decimal, "f"),
            {"symbol": symbol, "side": "buy", "amount": format(amount_decimal, "f"), "post_only": True},
        )
        self.risk_manager.store.update_order(client_id, "SUBMITTED")
        last_attempt_client_id = client_id
        slippage_cap_bps = float(getattr(self.config, "max_slippage_bps", 30) or 30)
        try:
            book = self.exchange.get_order_book(symbol)
            if not book or not book.get("bids"):
                raise RuntimeError("order book is unavailable for post-only entry")
            anchor_bid = Decimal(str(book["bids"][0][0]))
            if not anchor_bid.is_finite() or anchor_bid <= 0:
                raise RuntimeError("invalid top of book for post-only entry")
            max_price = anchor_bid * (Decimal(1) + Decimal(str(slippage_cap_bps)) / Decimal(10000))
            order = None
            for attempt in range(max(1, max_requotes)):
                book = self.exchange.get_order_book(symbol)
                bids = book.get("bids") if book else None
                if not bids:
                    raise RuntimeError("order book unavailable during post-only chase")
                best_bid = Decimal(str(bids[0][0]))
                if not best_bid.is_finite() or best_bid <= 0:
                    raise RuntimeError("invalid top of book during post-only chase")
                if best_bid > max_price:
                    self.risk_manager.store.update_order(client_id, "CANCELED", response={"result": "slippage_cap_exceeded"})
                    self.risk_manager.store.mark_order_applied(client_id)
                    logger.info("Post-only entry aborted for %s: best bid %.8f exceeds slippage cap %.8f", symbol, best_bid, max_price)
                    return None, client_id
                # Binance requires a unique clientOrderId per submitted order; a
                # canceled then re-quoted order must not reuse the parent id or
                # the exchange rejects the duplicate. Keep the stable parent id
                # for the intent journal, but send a distinct id each re-quote.
                attempt_client_id = client_id if attempt == 0 else f"{client_id}{attempt}"
                last_attempt_client_id = attempt_client_id
                order = self.exchange.place_post_only_limit_buy(symbol, format(amount_decimal, "f"), format(best_bid, "f"), attempt_client_id)
                if not order:
                    self.risk_manager.store.update_order(client_id, "REJECTED", response={"result": "post_only_rejected_without_order"})
                    self.risk_manager.store.mark_order_applied(client_id)
                    self.risk_manager.halt(f"Buy order was rejected for {symbol}; operator review required")
                    return None, client_id
                raw_status = str(order.get("status", "")).lower()
                if raw_status in {"closed", "canceled", "cancelled", "rejected", "expired"}:
                    break
                # Still open / partially filled: cancel and confirm the exchange
                # state before sending a replacement. A successful cancel request
                # alone does not prove that the order stopped working.
                order_id = str(order.get("id") or "")
                if not order_id:
                    raise RuntimeError("open post-only order has no exchange order id")
                try:
                    self.exchange.cancel_order(order_id, symbol)
                except Exception:
                    # The cancel response can be lost after the exchange acted.
                    # Always read the order state before deciding whether to retry.
                    logger.warning("Cancel response was uncertain for post-only order %s; verifying exchange state", order_id)
                try:
                    confirmed = self.exchange.fetch_order(order_id, symbol)
                except Exception as exc:
                    raise RuntimeError("post-only cancellation outcome could not be verified") from exc
                if not isinstance(confirmed, dict):
                    raise RuntimeError("post-only cancellation returned no order state")
                confirmed_status = str(confirmed.get("status", "")).lower()
                confirmed_filled = Decimal(str(confirmed.get("filled") or 0))
                if (not confirmed_filled.is_finite() or confirmed_filled < 0
                        or confirmed_filled > amount_decimal):
                    raise RuntimeError("exchange returned an invalid fill amount after cancellation")
                if confirmed_status not in {"closed", "canceled", "cancelled", "rejected", "expired"}:
                    # Do not place another quote while this one might still fill.
                    self.risk_manager.store.update_order(
                        client_id, "UNKNOWN", exchange_order_id=order_id,
                        filled=confirmed_filled,
                        average=confirmed.get("average") or confirmed.get("price"),
                        response={"attempt_client_order_id": attempt_client_id,
                                  "exchange_status": confirmed_status or "unknown",
                                  "cancel_unconfirmed": True},
                    )
                    raise RuntimeError("post-only order remains open or its exchange state is unknown")
                order = confirmed
                raw_status = confirmed_status
                if confirmed_filled > 0:
                    # A partial fill is already real inventory. Stop the chase and
                    # let the normal position/risk path manage only that quantity.
                    break
            if not order:
                self.risk_manager.store.update_order(client_id, "REJECTED", response={"result": "chase_ended_without_order"})
                self.risk_manager.store.mark_order_applied(client_id)
                return None, client_id
            raw_status = str(order.get("status", "")).lower()
            filled = order.get("filled") or 0
            filled_decimal = Decimal(str(filled))
            if not filled_decimal.is_finite() or filled_decimal < 0 or filled_decimal > amount_decimal:
                raise RuntimeError("exchange returned an invalid fill amount")
            state = {
                "closed": "FILLED", "open": "OPEN", "new": "OPEN",
                "partially_filled": "PARTIALLY_FILLED", "canceled": "CANCELED",
                "cancelled": "CANCELED", "rejected": "REJECTED", "expired": "EXPIRED",
            }.get(raw_status, "UNKNOWN")
            if state in {"CANCELED", "EXPIRED"} and filled_decimal >= amount_decimal * Decimal("0.99999999"):
                # The final quantity may have filled immediately before cancel.
                state = "FILLED"
                order = {**order, "status": "closed"}
            elif state in {"CANCELED", "EXPIRED"} and filled_decimal > 0:
                state = "PARTIAL_CLOSED"
            elif state == "FILLED" and filled_decimal < amount_decimal * Decimal("0.99999999"):
                state = "PARTIAL_CLOSED"
            average = order.get("average") or order.get("price")
            self.risk_manager.store.update_order(
                client_id, state, exchange_order_id=order.get("id"),
                filled=filled, average=average, response=order,
            )
            if state in {"CANCELED", "REJECTED", "EXPIRED"}:
                # A verified zero-fill maker entry is aborted cleanly.
                self.risk_manager.store.mark_order_applied(client_id)
                logger.info("Post-only entry for %s not filled (state %s); no position opened", symbol, state)
                return None, client_id
            if state in {"PARTIAL_CLOSED", "PARTIALLY_FILLED", "OPEN", "UNKNOWN"}:
                if state != "PARTIAL_CLOSED":
                    raise RuntimeError("post-only order did not reach a confirmed terminal fill state")
                # Leave partial inventory unapplied so reconciliation blocks any
                # further entry until the position ledger records the actual fill.
            return order, client_id
        except Exception as exc:
            existing = self.risk_manager.store.get_order(client_id)
            if existing and existing.get("state") not in {"UNKNOWN", "FILLED", "PARTIAL_CLOSED", "CANCELED", "REJECTED", "EXPIRED"}:
                self.risk_manager.store.update_order(client_id, "UNKNOWN")
            try:
                order = self.exchange.fetch_order_by_client_id(last_attempt_client_id, symbol)
                if not order:
                    raise RuntimeError("order was not returned by reconciliation")
                status = str(order.get("status", "")).lower()
                filled = Decimal(str(order.get("filled") or 0))
                if (status not in {"closed", "canceled", "cancelled", "rejected", "expired"}
                        or not filled.is_finite() or filled < 0 or filled > amount_decimal):
                    raise RuntimeError("reconciled post-only order is not terminal")
                if filled >= amount_decimal * Decimal("0.99999999"):
                    state = "FILLED"
                    order = {**order, "status": "closed"}
                elif filled > 0:
                    state = "PARTIAL_CLOSED"
                    order = {**order, "status": "canceled"}
                else:
                    state = {"rejected": "REJECTED", "expired": "EXPIRED"}.get(status, "CANCELED")
                existing = self.risk_manager.store.get_order(client_id)
                if existing and existing.get("state") in {"UNKNOWN", "SUBMITTED"}:
                    self.risk_manager.store.update_order(
                        client_id, state, exchange_order_id=order.get("id"),
                        filled=filled, average=order.get("average") or order.get("price"),
                        response={**order, "attempt_client_order_id": last_attempt_client_id},
                    )
                if state in {"CANCELED", "REJECTED", "EXPIRED"}:
                    self.risk_manager.store.mark_order_applied(client_id)
                return order, client_id
            except Exception:
                raise RuntimeError("Post-only order outcome remains unknown; operator reconciliation required") from exc

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
        client_id = ""
        try:
            if not self.exchange.cancel_order(old_stop_id, symbol):
                self.risk_manager.halt(f"Exchange stop replacement state is uncertain for {symbol}; reconcile before continuing")
                logger.critical("Could not confirm cancellation of old stop %s for %s", old_stop_id, symbol)
                return False
            cleared_persistently = self.risk_manager.clear_exchange_stop(symbol)
            if not cleared_persistently:
                # Keep going to restore exchange protection even if the durable
                # state write failed; attach_exchange_stop will retry persistence.
                self.risk_manager.halt(f"Canceled exchange stop reference could not be persisted for {symbol}")
            client_id = self._client_order_id(symbol, "stop_trail", f"{int(time.time()*1000)}")
            new_stop = self.exchange.place_native_stop_loss(
                symbol, position.amount, position.stop_loss_price, client_id
            )
            if isinstance(new_stop, dict) and new_stop.get("id") and str(new_stop.get("status", "")).lower() == "open":
                if not self.risk_manager.attach_exchange_stop(symbol, new_stop["id"], client_id):
                    raise RuntimeError("Replacement stop reference could not be persisted")
                self.risk_manager.store.append_event("exchange_stop_tightened", {
                    "symbol": symbol, "old_order_id": old_stop_id, "new_order_id": str(new_stop["id"]),
                    "new_stop_price": position.stop_loss_price
                })
                logger.info("Synchronized tightened stop to Binance for %s at $%s", symbol, position.stop_loss_price)
                return True
            else:
                raise RuntimeError("Replacement exchange stop not confirmed open")
        except Exception as exc:
            if client_id:
                try:
                    recovered = self.exchange.fetch_order_by_client_id(client_id, symbol)
                    if (isinstance(recovered, dict) and recovered.get("id") and
                            str(recovered.get("status", "")).lower() == "open" and
                            self.risk_manager.attach_exchange_stop(symbol, recovered["id"], client_id)):
                        self.risk_manager.store.append_event("exchange_stop_recovered_after_sync_error", {
                            "symbol": symbol,
                            "old_order_id": old_stop_id,
                            "new_order_id": str(recovered["id"]),
                        })
                        logger.warning("Recovered confirmed replacement stop for %s after an ambiguous sync response", symbol)
                        return True
                except Exception as recovery_exc:
                    logger.critical("Replacement stop state could not be reconciled for %s (%s)", symbol, type(recovery_exc).__name__)
            self.risk_manager.halt(f"Replacement exchange stop failed for {symbol}; protective order must be reconciled immediately")
            logger.critical("Failed to synchronize exchange stop for %s (%s)", symbol, type(exc).__name__)
            return False

    def _recover_terminal_partial_sell(self, symbol: str, order: dict, client_order_id: str, requested_amount) -> bool:
        """Reconcile a terminal partial exit, protect its remainder, then halt for review."""
        if not isinstance(order, dict) or str(order.get("status", "")).lower() not in {"closed", "canceled", "rejected", "expired"}:
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
                if not self.risk_manager.mark_position_accounting_incomplete(symbol):
                    raise ValueError("incomplete accounting status could not be persisted")
                fee_cost = self._resolve_fee_quote_cost(order, symbol, fill_price)
                record = self.risk_manager.close_position(
                    symbol, fill_price, "Partial sell reconciled to zero remaining balance", fee_cost=fee_cost
                )
                if not record:
                    raise ValueError("closed position could not be persisted")
                self._log_closed_trade(record, record.reason, fill_price, str(order.get("id", "")))
            else:
                if not self.risk_manager.update_position_amount(symbol, float(remaining)):
                    raise ValueError("remaining position quantity could not be persisted")
                if not self._place_and_persist_native_stop(symbol, str(order.get("id", ""))):
                    raise ValueError("remaining position could not be protected by a new exchange stop")
            self.risk_manager.store.mark_order_applied(client_order_id)
            self.risk_manager.store.append_event("partial_exit_reconciled", {
                "symbol": symbol, "order_id": str(order.get("id", "")),
                "filled": str(filled), "remaining": str(remaining),
                "accounting_complete": False,
            })
            self.risk_manager.halt(f"Partial sell for {symbol} was reconciled; manual review required")
            return True
        except Exception as exc:
            self.risk_manager.halt(f"Partial sell for {symbol} could not be safely reconciled; operator action required")
            logger.critical("Partial sell recovery failed for %s (%s)", symbol, type(exc).__name__)
            return False

    def _pending_unbaselined_stop_review(self) -> dict | None:
        """Return a protective-stop event whose missing asset baseline is unreviewed."""
        try:
            events = self.risk_manager.store.list_events(200)
        except Exception:
            return None
        latest_ack_id = max(
            (int(event.get("id", 0)) for event in events
             if event.get("event_type") == "protective_stop_review_acknowledged"),
            default=0,
        )
        for event in events:
            if (event.get("event_type") == "protective_stop_filled"
                    and int(event.get("id", 0)) > latest_ack_id
                    and isinstance(event.get("details"), dict)
                    and event["details"].get("baseline_was_missing") is True):
                return event
        return None

    def _below_minimum_notional(self, symbol: str, amount: Decimal) -> bool:
        """Whether ``amount`` of ``symbol`` is below the exchange minimum notional.

        A partial fill can leave a remnant too small to sell. Treat that remnant
        as dust so reconciliation can record it into the balance baseline instead
        of failing forever. Returns False on any uncertainty (fail closed).
        """
        try:
            limits = self.exchange.get_market_limits(symbol)
            min_cost = limits.get("min_cost")
            if min_cost is None:
                return False
            price = self.exchange.get_current_price(symbol)
            if price is None or price <= 0 or not math.isfinite(float(price)):
                return False
            notional = amount * Decimal(str(price))
            return notional.is_finite() and notional < Decimal(str(min_cost))
        except Exception:
            return False

    def _reconcile_terminal_no_fill_order_intents(self) -> None:
        """Resolve journaled orders only when Binance confirms every attempt ended unfilled."""
        for intent in self.risk_manager.store.unresolved_orders():
            if not isinstance(intent, dict):
                raise ValueError("unresolved local order intent exists")
            if intent.get("state") not in {"PENDING", "SUBMITTED", "UNKNOWN", "OPEN", "PARTIALLY_FILLED"}:
                # A terminal but unapplied fill may represent real inventory or
                # proceeds; it must be repaired by the position/trade ledger.
                raise ValueError("unresolved local order intent exists")
            symbol = str(intent.get("symbol") or "")
            client_id = str(intent.get("client_order_id") or "")
            if not symbol or not client_id:
                raise ValueError("unresolved local order intent exists")
            try:
                request = json.loads(intent.get("request_json") or "{}")
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("unresolved local order intent exists") from exc

            # The maker chase used the stable parent id plus suffixes 1-4. Check
            # every possible order so an old still-working requote can never be
            # hidden by a canceled final attempt.
            candidate_ids = [client_id]
            if str(intent.get("side", "")).upper() == "BUY" and request.get("post_only") is True:
                candidate_ids.extend(f"{client_id}{attempt}" for attempt in range(1, 5))
            found_orders = []
            seen_exchange_ids = set()
            for candidate_id in candidate_ids:
                try:
                    order = self.exchange.fetch_order_by_client_id(candidate_id, symbol)
                except Exception as exc:
                    raise ValueError("unresolved local order intent exists") from exc
                if not order:
                    continue
                if not isinstance(order, dict):
                    raise ValueError("unresolved local order intent exists")
                exchange_id = str(order.get("id") or "")
                if exchange_id and exchange_id in seen_exchange_ids:
                    continue
                if exchange_id:
                    seen_exchange_ids.add(exchange_id)
                status = str(order.get("status", "")).lower()
                try:
                    filled = Decimal(str(order.get("filled") or 0))
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise ValueError("unresolved local order intent exists") from exc
                if (status not in {"canceled", "cancelled", "rejected", "expired"}
                        or not filled.is_finite() or filled != 0):
                    raise ValueError("unresolved local order intent exists")
                found_orders.append(order)

            if not found_orders and intent.get("exchange_order_id"):
                # Fallback for exchanges that cannot resolve by client id while
                # retaining a persisted exchange order id.
                try:
                    order = self.exchange.fetch_order(str(intent["exchange_order_id"]), symbol)
                    status = str(order.get("status", "")).lower() if isinstance(order, dict) else ""
                    filled = Decimal(str(order.get("filled") or 0)) if isinstance(order, dict) else Decimal("-1")
                except Exception as exc:
                    raise ValueError("unresolved local order intent exists") from exc
                if (status not in {"canceled", "cancelled", "rejected", "expired"}
                        or not filled.is_finite() or filled != 0):
                    raise ValueError("unresolved local order intent exists")
                found_orders.append(order)

            # No matching client order is also safe only because the private
            # exchange lookup returned a confirmed not-found result for every
            # id. Account balances and open orders are checked separately in
            # this same reconciliation pass before it is allowed to complete.
            final_state = "CANCELED" if intent.get("state") != "PENDING" else "REJECTED"
            response = {
                "result": "exchange_confirmed_no_fill",
                "orders_checked": len(candidate_ids),
                "orders_found": len(found_orders),
            }
            self.risk_manager.store.update_order(
                client_id, final_state,
                exchange_order_id=(found_orders[-1].get("id") if found_orders else intent.get("exchange_order_id")),
                filled="0", response=response,
            )
            self.risk_manager.store.mark_order_applied(client_id)
            self.risk_manager.store.append_event("order_reconciled_no_fill", {
                "symbol": symbol,
                "side": str(intent.get("side", "")).lower(),
                "orders_checked": len(candidate_ids),
                "orders_found": len(found_orders),
            })

    def _reconcile_exchange_state(self, *, allow_manual_review_rebaseline: bool = False) -> bool:
        """Block all order activity until authenticated account state matches local state."""
        try:
            balance, open_orders = self.exchange.fetch_account_snapshot()
            self.exchange.exchange.load_markets()
            totals = balance.get("total", {}) or {}
            configured = {pair.strip(): pair.strip().split("/")[0] for pair in self.config.strategy.trading_pairs}
            baseline = self.risk_manager.store.read_state("baseline_asset_balances")
            current_environment = (
                self.config.exchange.paper_environment
                if self.config.exchange.paper_trading else "live"
            )
            persisted_environment = self.risk_manager.store.read_state("paper_environment")
            if persisted_environment is None:
                # Establish an origin marker on the current paper profile before any switch.
                self.risk_manager.store.write_state("paper_environment", current_environment)
                self.risk_manager.store.append_event("paper_environment_baselined", {
                    "environment": current_environment,
                    "existing_baseline": baseline is not None,
                })
            elif persisted_environment != current_environment:
                unresolved = self.risk_manager.store.unresolved_orders()
                if (not self.config.exchange.paper_trading or self.risk_manager.open_positions
                        or unresolved or open_orders):
                    raise ValueError(
                        "Paper environment changed while positions or orders exist; reconcile before switching"
                    )
                demo_equity = Decimal(str(totals.get("USDT") or 0))
                if not demo_equity.is_finite() or demo_equity <= 0:
                    raise ValueError("New paper account has no valid USDT equity for risk initialization")
                next_baseline = {base: str(totals.get(base) or 0) for base in set(configured.values())}
                # Save the account-specific baseline first; marker is written last so a crash retries safely.
                self.risk_manager.store.write_state("baseline_asset_balances", next_baseline)
                self.risk_manager.rebase_for_paper_environment(float(demo_equity))
                self.risk_manager.set_exit_only()
                self.risk_manager.store.append_event("paper_environment_changed", {
                    "from": str(persisted_environment), "to": current_environment,
                    "positions": 0, "unresolved_orders": 0, "exchange_open_orders": 0,
                    "risk_equity_rebased": True, "entries_paused_for_review": True,
                })
                self.risk_manager.store.write_state("paper_environment", current_environment)
                baseline = next_baseline
            if baseline is None:
                if self.risk_manager.open_positions:
                    raise ValueError("baseline asset balances are missing while local positions exist")
                baseline = {base: str(totals.get(base) or 0) for base in set(configured.values())}
                self.risk_manager.store.write_state("baseline_asset_balances", baseline)
            tolerances = {}
            amount_steps = {}
            for symbol, base in configured.items():
                market = self.exchange.exchange.market(symbol)
                precision = market.get("precision", {}).get("amount")
                precision_mode = getattr(self.exchange.exchange, "precisionMode", None)
                tick_size_mode = getattr(self.exchange.exchange, "TICK_SIZE", 4)
                decimal_places_mode = getattr(self.exchange.exchange, "DECIMAL_PLACES", 2)
                significant_digits_mode = getattr(self.exchange.exchange, "SIGNIFICANT_DIGITS", 3)
                if not isinstance(tick_size_mode, int):
                    tick_size_mode = 4
                if not isinstance(decimal_places_mode, int):
                    decimal_places_mode = 2
                if not isinstance(significant_digits_mode, int):
                    significant_digits_mode = 3
                if precision_mode == tick_size_mode and precision is not None:
                    amount_steps[base] = Decimal(str(precision))
                elif precision_mode == decimal_places_mode and precision is not None:
                    amount_steps[base] = Decimal(1).scaleb(-int(precision))
                elif precision_mode == significant_digits_mode and precision is not None:
                    reference = Decimal(str(
                        self.risk_manager.open_positions[symbol].amount
                        if symbol in self.risk_manager.open_positions
                        else totals.get(base) or 1
                    ))
                    if not reference.is_finite() or reference <= 0:
                        reference = Decimal("1")
                    normalized = Decimal(str(self.exchange.amount_to_precision(symbol, reference)))
                    if not normalized.is_finite() or normalized <= 0 or int(precision) <= 0:
                        raise ValueError(f"invalid significant-digit amount precision for {base}")
                    amount_steps[base] = Decimal(1).scaleb(normalized.adjusted() - int(precision) + 1)
                else:
                    raise ValueError(f"unsupported amount precision mode for configured asset {base}")
                if not amount_steps[base].is_finite() or amount_steps[base] <= 0:
                    raise ValueError(f"invalid amount precision for configured asset {base}")
                tolerances[base] = amount_steps[base] / 2
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
                # Derive the base asset from the current position's symbol. The
                # precision loop above leaves `base` bound to the last configured
                # pair, which would otherwise mis-attribute this position's
                # balance and tolerance checks to the wrong asset.
                base = configured.get(symbol, symbol.split("/")[0] if "/" in symbol else symbol)
                if not position.stop_order_id or not position.stop_client_order_id:
                    raise ValueError(f"tracked position {symbol} has no persisted exchange stop")
                if symbol in seen_stops:
                    continue
                stop = self.exchange.fetch_order(position.stop_order_id, symbol)
                if str(stop.get("status", "")).lower() == "closed":
                    filled = Decimal(str(stop.get("filled") or 0))
                    ordered = Decimal(str(stop.get("amount") or 0))
                    stop_side = str(stop.get("side", "")).lower()
                    local_amount = Decimal(str(position.amount))
                    expected_order_amount = Decimal(str(self.exchange.amount_to_precision(symbol, position.amount)))
                    if (not filled.is_finite() or not ordered.is_finite()
                            or filled <= 0 or ordered <= 0
                            or stop_side != "sell"
                            or ordered != expected_order_amount
                            or ordered > local_amount
                            or filled > ordered + tolerances[base]):
                        raise ValueError(f"filled stop order quantity is invalid for {symbol}")
                    if filled >= ordered * Decimal("0.99999999"):
                        price = float(stop.get("average") or stop.get("price") or 0)
                        if not math.isfinite(price) or price <= 0:
                            raise ValueError(f"triggered stop for {symbol} lacks a valid fill price")
                        base_total = Decimal(str(totals.get(base) or 0))
                        baseline_total = Decimal(str(baseline.get(base, "0")))
                        residual = base_total - baseline_total
                        expected_residual = local_amount - filled
                        baseline_known = base in baseline
                        if (not base_total.is_finite() or not residual.is_finite()
                                or not expected_residual.is_finite()
                                or (baseline_known and base_total < baseline_total)
                                or expected_residual < 0
                                or (baseline_known and expected_residual > amount_steps[base] + tolerances[base])
                                or (baseline_known and residual > expected_residual + amount_steps[base] + tolerances[base])):
                            raise ValueError(f"filled stop for {symbol} left an unexpected remaining balance")
                        # If this asset was absent from the account baseline, its
                        # starting inventory is unknown (for example, the pair
                        # may have been added after initial setup). Preserve the
                        # exchange's actual remainder, mark this trade's quantity
                        # accounting incomplete, and require explicit review.
                        # Otherwise, only accept the sub-step residue explained by
                        # Binance's amount precision rounding.
                        if not baseline_known:
                            self.risk_manager.mark_position_accounting_incomplete(symbol)
                        updated_baseline = dict(baseline)
                        updated_baseline[base] = str(base_total)
                        self.risk_manager.store.write_state("baseline_asset_balances", updated_baseline)
                        fee_cost = self._resolve_fee_quote_cost(stop, symbol, price)
                        record = self.risk_manager.close_position(
                            symbol, price, "Exchange-native stop-loss filled", fee_cost=fee_cost,
                            closed_amount=float(filled),
                        )
                        if not record:
                            raise ValueError(f"triggered stop for {symbol} could not be persisted")
                        self._log_closed_trade(record, record.reason, price, str(stop.get("id", position.stop_order_id)))
                        self.risk_manager.store.append_event("protective_stop_filled", {
                            "symbol": symbol, "order_id": str(stop.get("id", position.stop_order_id)),
                            "filled": str(filled), "average": str(price),
                            "remaining": str(residual if baseline_known else base_total),
                            "baseline_updated": True,
                            "baseline_was_missing": not baseline_known,
                            "unattributed_balance": str(base_total) if not baseline_known else None,
                            "accounting_complete": record.accounting_complete,
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
                    pending_stop = self._pending_unbaselined_stop_review()
                    pending_details = pending_stop.get("details", {}) if pending_stop else {}
                    pending_symbol = str(pending_details.get("symbol", ""))
                    may_rebaseline = (
                        allow_manual_review_rebaseline
                        and pending_stop is not None
                        and pending_symbol == symbol
                        and not self.risk_manager.open_positions
                        and not self.risk_manager.store.unresolved_orders()
                        and not open_orders
                        and reported.is_finite()
                        and reported >= 0
                    )
                    # Auto-record untradable dust: an excess below the exchange's
                    # minimum notional can never be sold, so re-baseline it instead
                    # of failing reconciliation forever. Only when flat with no
                    # unresolved or open exchange orders.
                    excess = reported - expected
                    dust_auto_sweep = (
                        not may_rebaseline
                        and excess.is_finite()
                        and excess > 0
                        and not self.risk_manager.open_positions
                        and not self.risk_manager.store.unresolved_orders()
                        and not open_orders
                        and self._below_minimum_notional(symbol, excess)
                    )
                    if not may_rebaseline and not dust_auto_sweep:
                        raise ValueError(f"balance mismatch for configured asset {base}")
                    previous = str(baseline.get(base, "0"))
                    baseline = dict(baseline)
                    baseline[base] = str(reported)
                    self.risk_manager.store.write_state("baseline_asset_balances", baseline)
                    if may_rebaseline:
                        self.risk_manager.store.append_event("protective_stop_balance_rebased_after_review", {
                            "symbol": symbol,
                            "previous_baseline": previous,
                            "account_balance": str(reported),
                            "difference": str(reported - Decimal(previous)),
                            "stop_order_id": str(pending_details.get("order_id", "")),
                            "operator_review": True,
                        })
                    else:
                        self.risk_manager.store.append_event("dust_balance_rebased", {
                            "symbol": symbol,
                            "previous_baseline": previous,
                            "account_balance": str(reported),
                            "difference": str(excess),
                            "below_minimum_notional": True,
                        })
            self._reconcile_terminal_no_fill_order_intents()
            if self.risk_manager.store.unresolved_orders():
                raise ValueError("unresolved local order intent exists")
            self._last_account_balance = balance
            self._last_account_error = ""
            self._last_reconciliation_ok = True
            self.risk_manager.resume_after_reconciliation()
            return True
        except Exception as exc:
            self._last_reconciliation_ok = False
            safe_value_error_prefixes = (
                "Paper environment changed while positions or orders exist",
                "New paper account has no valid USDT equity",
                "baseline asset balances are missing while local positions exist",
                "invalid amount precision for configured asset ",
                "unreconciled open order exists on the account",
                "tracked position ",
                "filled stop order quantity is invalid for ",
                "triggered stop for ",
                "filled stop for ",
                "persisted exchange stop for ",
                "balance mismatch for configured asset ",
                "unresolved local order intent exists",
            )
            error_message = str(exc)
            safe_detail = (
                error_message[:240]
                if isinstance(exc, ValueError) and error_message.startswith(safe_value_error_prefixes)
                else type(exc).__name__
            )
            if isinstance(exc, AccountAccessError):
                self._last_account_error = str(exc)
                # Keep the durable halt in the reconciliation class so a
                # later clean private-account check can clear this transient
                # connectivity/authentication halt safely.
                reason = f"Exchange reconciliation failed: {safe_detail}"
            else:
                self._last_account_error = "Account reconciliation failed. Check exchange permissions and service logs."
                reason = f"Exchange reconciliation failed: {safe_detail}"
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

    def _bot_managed_equity(self) -> Decimal:
        """Value only bot capital: quote cash plus bot-tracked open positions.

        Pre-existing exchange balances are recorded in baseline_asset_balances
        and are not bot positions. They must not drive this bot's drawdown
        circuit breaker or make Testnet seed assets appear to be deployed
        strategy capital.
        """
        if not isinstance(self._last_account_balance, dict):
            raise ValueError("account balance snapshot is unavailable")
        totals = self._last_account_balance.get("total", {}) or {}
        cash = Decimal(str(totals.get("USDT") or 0))
        if not cash.is_finite() or cash < 0:
            raise ValueError("USDT account balance is invalid")
        equity = cash
        for symbol, position in self.risk_manager.open_positions.items():
            amount = Decimal(str(position.amount))
            if not amount.is_finite() or amount <= 0:
                raise ValueError(f"tracked position amount is invalid for {symbol}")
            raw_price = self.exchange.get_current_price(symbol)
            if raw_price is None:
                raise ValueError(f"market valuation is unavailable for tracked position {symbol}")
            mark = Decimal(str(raw_price))
            if not mark.is_finite() or mark <= 0:
                raise ValueError(f"market valuation is invalid for tracked position {symbol}")
            equity += amount * mark
        if not equity.is_finite() or equity <= 0:
            raise ValueError("bot-managed equity is invalid")
        return equity

    def _simulated_wallet_active(self) -> bool:
        """True only when the exchange connector is operating a virtual wallet."""
        if getattr(self.exchange, "is_fallback_simulated", False) is True:
            return True
        is_sim = getattr(self.exchange, "_is_simulated", None)
        return bool(callable(is_sim) and is_sim() is True)

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

    def _sell_was_not_submitted(self, symbol: str, decision_key: str) -> bool:
        """Allow stop restoration only when durable state proves no sell POST occurred."""
        client_id = self._client_order_id(symbol, "sell", str(decision_key))
        try:
            order = self.risk_manager.store.get_order(client_id)
        except Exception:
            return False
        return order is None or order.get("state") == "PENDING"

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
    def _terminal_zero_fill(order: dict) -> bool:
        """Whether an exit is conclusively terminal with no fill to protect against."""
        if not isinstance(order, dict) or str(order.get("status", "")).lower() not in {
            "closed", "canceled", "rejected", "expired",
        }:
            return False
        try:
            filled = Decimal(str(order.get("filled") or 0))
            return filled.is_finite() and filled == 0
        except (InvalidOperation, TypeError, ValueError):
            return False

    @staticmethod
    def _post_fill_stop_risk(amount: float, entry_price: float, stop_price: float, entry_fee_quote: float, risk_cap: float) -> tuple[Decimal | None, bool]:
        """Return post-fill quote risk and whether it respects the configured cap."""
        try:
            values = [Decimal(str(value)) for value in (amount, entry_price, stop_price, entry_fee_quote, risk_cap)]
            amount_d, entry_d, stop_d, fee_d, cap_d = values
            if any(not value.is_finite() for value in values) or amount_d <= 0 or entry_d <= stop_d or stop_d <= 0 or fee_d < 0 or cap_d <= 0:
                return None, False
            risk = amount_d * (entry_d - stop_d) + fee_d
            return risk, risk <= cap_d
        except (InvalidOperation, TypeError, ValueError):
            return None, False

    @staticmethod
    def _order_fee_entries(order: dict) -> list[dict] | None:
        """Normalize an order response into ``{currency, cost}`` fee entries.

        Handles the three shapes CCXT may return (a ``fees`` list, a single
        ``fee`` dict, or Binance ``info.fills[].commission*``) and returns None
        when fee data is absent or malformed so callers fail closed instead of
        assuming a zero commission.
        """
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
        return fees

    @staticmethod
    def _net_base_filled(order: dict, symbol: str) -> Decimal | None:
        """Account for commission charged in the received base asset.

        Return None when the order response lacks fee data; silently assuming
        zero base commission would make local quantity disagree with the wallet.
        """
        base = symbol.split("/")[0]
        fees = TradingBot._order_fee_entries(order)
        if not fees:
            return None
        try:
            filled = Decimal(str(order.get("filled")))
            base_fee = sum((Decimal(str(fee.get("cost") or 0)) for fee in fees if fee.get("currency") == base), Decimal("0"))
            net = filled - base_fee
            return net if net.is_finite() and net > 0 else None
        except (InvalidOperation, TypeError, ValueError, AttributeError):
            return None

    @staticmethod
    def _fee_quote_cost(order: dict, symbol: str, fill_price: float, third_asset_prices: dict[str, float] | None = None) -> float | None:
        """Convert reported fees into quote currency.

        Base fees use the fill price; third-asset fees (e.g. BNB) use the
        provided contemporaneous ``third_asset_prices`` mapping (currency ->
        price in quote). Any fee whose currency cannot be priced is ambiguous
        and returns None so the caller fails closed rather than miscounting.
        """
        base, quote = symbol.split("/", 1)
        fees = TradingBot._order_fee_entries(order)
        if not fees:
            return None
        third_asset_prices = third_asset_prices or {}
        total = Decimal("0")
        try:
            price = Decimal(str(fill_price))
            if not price.is_finite() or price <= 0:
                return None
            for fee in fees:
                if fee.get("cost") is None:
                    return None
                cost = Decimal(str(fee["cost"] or 0))
                currency = fee.get("currency")
                if not cost.is_finite() or cost < 0:
                    return None
                if currency == quote:
                    total += cost
                elif currency == base:
                    total += cost * price
                elif currency in third_asset_prices:
                    third_price = third_asset_prices[currency]
                    if third_price is None or not math.isfinite(float(third_price)) or float(third_price) <= 0:
                        return None
                    total += cost * Decimal(str(third_price))
                else:
                    # Fee conversion requires a contemporaneous third-asset quote.
                    return None
            result = float(total)
            return result if math.isfinite(result) else None
        except (InvalidOperation, TypeError, ValueError):
            return None

    def _third_asset_fee_prices(self, order: dict, symbol: str) -> dict[str, float]:
        """Resolve quote prices for any fee currency that is neither base nor quote."""
        base, quote = symbol.split("/", 1)
        fees = self._order_fee_entries(order)
        if not fees:
            return {}
        third = {fee.get("currency") for fee in fees if fee.get("currency") not in (base, quote)}
        prices: dict[str, float] = {}
        for currency in third:
            if not currency:
                continue
            try:
                price = self.exchange.get_current_price(f"{currency}/{quote}")
            except Exception:
                price = None
            if price is not None:
                try:
                    value = float(price)
                    if math.isfinite(value) and value > 0:
                        prices[currency] = value
                except (TypeError, ValueError):
                    continue
        return prices

    def _resolve_fee_quote_cost(self, order: dict, symbol: str, fill_price: float) -> float | None:
        """Use order fees, then verified per-fill fees; never assume missing fees are zero."""
        third_asset_prices = self._third_asset_fee_prices(order, symbol)
        fee_cost = self._fee_quote_cost(order, symbol, fill_price, third_asset_prices)
        if fee_cost is not None:
            return fee_cost
        if not isinstance(order, dict):
            return None
        order_id = order.get("id") or (order.get("info") or {}).get("orderId")
        filled_value = order.get("filled")
        if not order_id or filled_value is None:
            return None
        try:
            expected = Decimal(str(filled_value))
            if not expected.is_finite() or expected <= 0:
                return None
            fills = self.exchange.fetch_order_trades(str(order_id), symbol)
        except Exception as exc:
            logger.warning("Could not retrieve fills for fee reconciliation on %s (%s)", symbol, type(exc).__name__)
            return None
        if not isinstance(fills, list) or not fills:
            return None

        total_filled = Decimal("0")
        aggregated: dict[str, Decimal] = {}
        try:
            for fill in fills:
                if not isinstance(fill, dict):
                    return None
                amount = Decimal(str(fill.get("amount")))
                if not amount.is_finite() or amount <= 0:
                    return None
                total_filled += amount
                fees = fill.get("fees")
                if fees is None:
                    fee = fill.get("fee")
                    fees = [fee] if isinstance(fee, dict) else None
                if not isinstance(fees, list) or not fees:
                    return None
                for fee in fees:
                    if not isinstance(fee, dict) or fee.get("currency") is None or fee.get("cost") is None:
                        return None
                    currency = str(fee["currency"])
                    cost = Decimal(str(fee["cost"]))
                    if not cost.is_finite() or cost < 0:
                        return None
                    aggregated[currency] = aggregated.get(currency, Decimal("0")) + cost
            tolerance = max(Decimal("0.0000000001"), expected * Decimal("0.00000001"))
            if abs(total_filled - expected) > tolerance or not aggregated:
                return None
        except (InvalidOperation, TypeError, ValueError):
            return None

        verified_order = {
            "fees": [{"currency": currency, "cost": str(cost)} for currency, cost in aggregated.items()]
        }
        third_asset_prices = self._third_asset_fee_prices(verified_order, symbol)
        return self._fee_quote_cost(verified_order, symbol, fill_price, third_asset_prices)

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
        indicators = signal.indicators if isinstance(signal.indicators, dict) else {}

        # Get current portfolio value
        usdt_balance = self.exchange.get_usdt_balance()
        if usdt_balance <= 0:
            logger.warning(f"No USDT balance available for buying {symbol}")
            return
        # Use managed equity (cash + tracked positions) as the single risk basis
        # so entry sizing and the daily-loss gate agree with the equity halt.
        # Fall back to free cash when an equity snapshot is unavailable (e.g. in
        # isolated tests or before the first reconciliation).
        try:
            entry_equity = float(self._bot_managed_equity())
        except Exception:
            entry_equity = usdt_balance

        # Keep the downtrend experiment to one position across the portfolio.
        recovery_candidate = (
            getattr(self.config.strategy, "name", "").strip().lower() == "dynamic_regime"
            and isinstance(signal.indicators, dict)
            and signal.indicators.get("strategy") == "downtrend_rsi_recovery_testnet"
        )
        if recovery_candidate and any(
            pos.exit_policy == "downtrend_rsi_recovery"
            for pos in self.risk_manager.open_positions.values()
        ):
            logger.info("Trade blocked for %s: one downtrend-recovery Testnet position is already open", symbol)
            return

        # Risk check: can we open a new position? The loss-budget route is
        # derived from the candidate strategy so a route-scoped consecutive-loss
        # halt (e.g. from the experimental downtrend route) never blocks the
        # independent uptrend EMA72 route.
        candidate_strategy_id = signal.indicators.get("strategy") if isinstance(signal.indicators, dict) else None
        entry_route = {
            "macro_donchian_ema72": "uptrend",
            "macro_trend_pullback": "uptrend",
            "downtrend_rsi_recovery_testnet": "downtrend",
        }.get(candidate_strategy_id, "legacy")
        can_trade, reason = self.risk_manager.can_open_position(
            symbol, entry_equity, route=entry_route
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
        profile_name = getattr(self.config.strategy, "name", "combined").strip().lower()
        candidate_strategy = signal.indicators.get("strategy") if isinstance(signal.indicators, dict) else None
        ema72_candidate = profile_name == "macro_donchian_ema72" or (
            profile_name == "dynamic_regime" and candidate_strategy == "macro_donchian_ema72"
        )
        pullback_candidate = profile_name == "macro_trend_pullback" or (
            profile_name == "dynamic_regime" and candidate_strategy == "macro_trend_pullback"
        )
        recovery_candidate = profile_name == "dynamic_regime" and candidate_strategy == "downtrend_rsi_recovery_testnet"
        managed_candidate = ema72_candidate or pullback_candidate or recovery_candidate
        # Reserve headroom under the 0.20% strategy cap for entry fees and
        # rounded exchange quantities before the post-fill risk validation.
        risk_multiplier = 0.20 if recovery_candidate else 1.0
        if managed_candidate:
            public_price = self.exchange.get_public_market_data_price(symbol)
            if public_price is None or public_price <= 0:
                logger.warning("Entry blocked for %s: public market price is unavailable for paper price-parity check", symbol)
                return
            divergence_pct = abs(current_price - public_price) / public_price * 100.0
            if divergence_pct > self.config.max_testnet_price_divergence_pct:
                logger.info(
                    "Entry blocked for %s: paper and public Spot prices differ by %.3f%% (limit %.3f%%)",
                    symbol, divergence_pct, self.config.max_testnet_price_divergence_pct,
                )
                return
        swing_low = None
        if managed_candidate:
            try:
                swing_low = float(signal.indicators["swing_low_5"])
                if not math.isfinite(swing_low) or swing_low <= 0:
                    raise ValueError("invalid prior swing low")
                stop_loss_price = max(swing_low, current_price * 0.96)
                if not 0 < stop_loss_price < current_price:
                    raise ValueError("strategy stop is not below the current price")
            except (KeyError, TypeError, ValueError, OverflowError):
                logger.warning("Entry blocked for %s because the strategy stop is unavailable or invalid", symbol)
                return
        else:
            stop_loss_price = self.risk_manager.calculate_stop_loss(current_price, atr=atr)
        amount = self.risk_manager.calculate_position_size(
            entry_equity, current_price, stop_loss_price,
            risk_multiplier=risk_multiplier,
            entry_fee_rate=0.001 if recovery_candidate else 0.0,
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
            # The experimental recovery route must enforce its reduced cap
            # before submission as well as after the exchange fill. Include
            # estimated entry commission so quantity rounding cannot consume
            # the entire stop-risk budget before the order is placed.
            fee_reserve = amount * Decimal(str(current_price)) * (
                Decimal("0.001") if recovery_candidate else Decimal("0")
            )
            risk_cap = (
                Decimal(str(entry_equity)) * Decimal(str(self.config.risk.max_risk_per_trade))
                / Decimal(100) * Decimal(str(risk_multiplier))
            )
            exposure_cap = Decimal(str(entry_equity)) * Decimal(str(self.config.risk.position_size_pct)) / Decimal(100)
            if rounded_risk + fee_reserve > risk_cap or needed_quote > exposure_cap:
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
            if getattr(self.config, "post_only_entries", False):
                order, client_order_id = self._post_only_limit_chase_buy(symbol, amount, decision_key)
            else:
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
                raw_fill_status in {"closed", "canceled", "rejected", "expired"} and
                filled_quantity > 0 and filled_quantity < requested_quantity * Decimal("0.99999999")
            )
            if not self._confirmed_full_fill(order, amount) and not partial_terminal_fill:
                if self._terminal_zero_fill(order):
                    self.risk_manager.store.mark_order_applied(client_order_id)
                    self.risk_manager.store.append_event("entry_order_no_fill", {
                        "symbol": symbol, "order_id": str(order.get("id", "")),
                        "status": raw_fill_status,
                    })
                    logger.info("Entry order for %s ended without a fill; no position was opened", symbol)
                    return
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

            entry_fee_quote = self._resolve_fee_quote_cost(order, symbol, fill_price)
            if entry_fee_quote is None:
                fee_ambiguous = True
                entry_fee_quote = 0.0

            strategy_stop_adjusted_after_fill = False
            actual_stop_loss = stop_loss_price
            actual_take_profit = None
            if managed_candidate:
                proposed_stop = max(float(swing_low), fill_price * 0.96)
                if not math.isfinite(proposed_stop) or proposed_stop <= 0:
                    self.risk_manager.halt(f"Candidate stop became invalid after fill for {symbol}; manual reconciliation required")
                    return
                if proposed_stop >= fill_price:
                    # A fast gap can invalidate the swing reference after a
                    # market buy. Protect the acquired testnet inventory with
                    # a 4% fallback, then halt for explicit review.
                    actual_stop_loss = fill_price * 0.96
                    strategy_stop_adjusted_after_fill = True
                else:
                    actual_stop_loss = proposed_stop
                target_atr_multiple = 2.0 if recovery_candidate else float(
                    getattr(self.config.strategy, "ema72_take_profit_atr_multiple", 100.0)
                )
                actual_take_profit = fill_price + target_atr_multiple * float(atr)
                if not math.isfinite(actual_take_profit) or actual_take_profit <= fill_price:
                    self.risk_manager.halt(f"Candidate target became invalid after fill for {symbol}; manual reconciliation required")
                    return

            try:
                self.risk_manager.open_position(
                    symbol=symbol,
                    entry_price=fill_price,
                    amount=fill_amount,
                    order_id=order.get("id", ""),
                    atr=atr,
                    entry_fee_quote=entry_fee_quote,
                    stop_loss_override=actual_stop_loss if managed_candidate else None,
                    take_profit_override=actual_take_profit if managed_candidate else None,
                    exit_policy=("downtrend_rsi_recovery" if recovery_candidate
                                 else "ema72_signal" if ema72_candidate
                                 else "ema36_signal" if pullback_candidate
                                 else "risk_managed"),
                    route=str(indicators.get("route", "") or ""),
                    regime=str(indicators.get("regime", "") or ""),
                    simulated=self._simulated_wallet_active(),
                )
            except Exception as exc:
                logger.error("Failed to open local position: %s", exc)

            position = self.risk_manager.open_positions.get(symbol)
            if position and fee_ambiguous:
                self.risk_manager.mark_position_accounting_incomplete(symbol)
                position = self.risk_manager.open_positions.get(symbol)
            actual_stop_risk = None
            actual_risk_cap = (
                Decimal(str(usdt_balance)) * Decimal(str(self.config.risk.max_risk_per_trade))
                / Decimal(100) * Decimal(str(risk_multiplier))
            )
            risk_within_cap = False
            if position:
                actual_stop_risk, risk_within_cap = self._post_fill_stop_risk(
                    fill_amount, fill_price, position.stop_loss_price, entry_fee_quote, float(actual_risk_cap)
                )
            if not position or actual_stop_risk is None:
                self.risk_manager.halt(f"Post-fill risk could not be validated for {symbol}; reconcile position and stop")

            # F-02: Even if local persistence failed or risk_manager is halted,
            # we MUST still place the native stop loss on the exchange for the acquired coins!
            stop_placed = self._place_and_persist_native_stop(
                symbol, order.get("id", ""), fallback_sl=actual_stop_loss, fallback_amount=fill_amount
            )

            if self.risk_manager._is_halted or not stop_placed:
                logger.error("Position or stop persistence issue after buy for %s; stop_placed: %s; trading halted", symbol, stop_placed)
                self.risk_manager.store.append_event("unpersisted_entry_incident", {
                    "symbol": symbol, "fill_amount": fill_amount, "fill_price": fill_price,
                    "stop_placed": stop_placed, "order_id": str(order.get("id", ""))
                })
                return

            # If the exchange filled far from the closed-candle price that
            # generated this signal, the signal was likely stale or the market
            # gapped. The position is already protected by its stop; record the
            # divergence and halt for review so a non-representative fill is
            # never silently counted as forward evidence.
            signal_close = None
            try:
                candidate_close = float(indicators.get("close"))
                if math.isfinite(candidate_close) and candidate_close > 0:
                    signal_close = candidate_close
            except (TypeError, ValueError):
                signal_close = None
            if managed_candidate and signal_close is not None:
                divergence_bps = abs(fill_price - signal_close) / signal_close * 10000.0
                if divergence_bps > self.config.max_signal_fill_divergence_bps:
                    self.risk_manager.store.append_event("signal_fill_divergence", {
                        "symbol": symbol,
                        "signal_close": str(signal_close),
                        "fill_price": str(fill_price),
                        "divergence_bps": round(divergence_bps, 2),
                        "protective_stop_placed": True,
                    })
                    self.risk_manager.halt(
                        f"Entry for {symbol} filled {divergence_bps:.1f} bps from its signal close; "
                        f"position protected, review required"
                    )
                    return

            if not risk_within_cap:
                self.risk_manager.store.append_event("post_fill_risk_cap_breach", {
                    "symbol": symbol,
                    "estimated_stop_risk_quote": str(actual_stop_risk) if actual_stop_risk is not None else None,
                    "risk_cap_quote": str(actual_risk_cap),
                    "protective_stop_placed": True,
                })
                self.risk_manager.halt(f"Post-fill stop risk exceeded its cap for {symbol}; position protected, operator review required")
                logger.critical("Post-fill stop risk for %s exceeded the configured cap; entries halted", symbol)
                return

            if fee_ambiguous:
                self.risk_manager.halt(f"Buy fee details were incomplete for {symbol}; position protected with stop, operator reconciliation required")
                return

            if strategy_stop_adjusted_after_fill:
                self.risk_manager.store.append_event("strategy_stop_adjusted_after_fill", {
                    "symbol": symbol,
                    "strategy": candidate_strategy,
                    "fill_price": fill_price,
                    "fallback_stop_price": actual_stop_loss,
                    "swing_low_reference": swing_low,
                    "protective_stop_placed": True,
                })
                self.risk_manager.halt(
                    f"Market fill invalidated the candidate swing stop for {symbol}; 4% fallback protected position, operator review required"
                )
                return

            self.risk_manager.store.mark_order_applied(client_order_id)
            if partial_terminal_fill:
                self.risk_manager.halt(f"Partial entry fill for {symbol} was protected; manual review required")
                self.risk_manager.store.append_event("partial_entry_fill", {
                    "symbol": symbol, "order_id": str(order.get("id", "")),
                    "requested": str(requested_quantity), "filled": str(filled_quantity),
                    "protected_amount": str(net_filled),
                })

            pos = self.risk_manager.open_positions.get(symbol)
            sl_text = f"${pos.stop_loss_price:,.2f}" if pos else "N/A"
            tp_text = (
                f"${pos.take_profit_price:,.2f} (100×ATR backstop)" if pos and pos.exit_policy in {"ema72_signal", "ema36_signal"} and pos.take_profit_price is not None
                else "EMA72 completed-candle exit" if pos and pos.exit_policy == "ema72_signal"
                else "EMA36 completed-candle exit" if pos and pos.exit_policy == "ema36_signal"
                else f"${pos.take_profit_price:,.2f} (2×ATR target)" if pos and pos.exit_policy == "downtrend_rsi_recovery" and pos.take_profit_price is not None
                else f"${pos.take_profit_price:,.2f}" if pos and pos.take_profit_price is not None
                else "N/A"
            )

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
            safe_to_reprotect = self._sell_was_not_submitted(symbol, decision_key)
            restored = safe_to_reprotect and self._place_and_persist_native_stop(
                symbol, f"reprotect_{int(time.time()*1000)}",
                fallback_sl=old_stop_price, fallback_amount=position_amount,
            )
            self.risk_manager.halt(
                f"Sell order state requires reconciliation for {symbol}; "
                + ("protective stop restored" if restored else "do not duplicate an uncertain sell; operator reconciliation required")
            )
            logger.error("Sell order state requires reconciliation for %s (%s)", symbol, type(exc).__name__)
            return
        if not order:
            restored = self._place_and_persist_native_stop(
                symbol, f"reprotect_{int(time.time()*1000)}",
                fallback_sl=old_stop_price, fallback_amount=position_amount,
            )
            self.risk_manager.halt(
                f"Sell order was rejected for {symbol}; "
                f"protective stop {'restored' if restored else 'could not be restored; operator action required'}"
            )
            return
        if order:
            if not self._confirmed_full_fill(order, position_amount):
                if self._recover_terminal_partial_sell(symbol, order, client_order_id, position_amount):
                    return
                if str(order.get("status", "")).lower() not in {"closed", "canceled", "rejected", "expired"}:
                    self.risk_manager.halt(
                        f"Sell order for {symbol} remains open or uncertain; trading halted until the order is reconciled"
                    )
                    return
                if not self._terminal_zero_fill(order):
                    self.risk_manager.halt(
                        f"Partial sell for {symbol} could not be reconciled; protective order was not duplicated"
                    )
                    return
                self.risk_manager.store.mark_order_applied(client_order_id)
                self._place_and_persist_native_stop(symbol, f"reprotect_{int(time.time()*1000)}", fallback_sl=old_stop_price, fallback_amount=position_amount)
                self.risk_manager.halt(f"Unconfirmed or partial sell fill for {symbol}; protective stop restored, reconcile exchange orders and balances")
                logger.error("Sell order for %s was not confirmed fully filled; position remains protected", symbol)
                return
            fill_price = float(order.get("average") or order.get("price") or 0)
            if fill_price <= 0:
                self._place_and_persist_native_stop(symbol, f"reprotect_{int(time.time()*1000)}", fallback_sl=old_stop_price, fallback_amount=position_amount)
                self.risk_manager.halt(f"Invalid sell fill details for {symbol}; protective stop restored, reconcile exchange orders and balances")
                return

            fee_cost = self._resolve_fee_quote_cost(order, symbol, fill_price)

            record = self.risk_manager.close_position(
                symbol=symbol,
                exit_price=fill_price,
                reason=f"Strategy signal: {signal.reason}",
                fee_cost=fee_cost
            )

            if record:
                self.risk_manager.store.mark_order_applied(client_order_id)
                pnl_text = (
                    f"${record.pnl:+,.2f} ({record.pnl_pct:+.2f}%)"
                    if record.accounting_complete else "pending fee/partial-fill reconciliation"
                )
                msg = (
                    f"🔴 *BOT SELL EXECUTED*\n"
                    f"Pair: `{symbol}`\n"
                    f"Exit Price: `${fill_price:,.2f}`\n"
                    f"P&L: {pnl_text}\n"
                    f"Reason: {record.reason}"
                )
                subject_pnl = f"${record.pnl:+,.2f}" if record.accounting_complete else "pending reconciliation"
                self.send_notification(msg, subject=f"🔴 Bot SELL: {symbol} (P&L: {subject_pnl})")
                self._log_closed_trade(record, record.reason, fill_price, str(order.get("id", "")))

    def check_stop_loss_take_profit(self):
        """Check all open positions for Trailing SL, Breakeven, and TP triggers."""
        # 1. Sync any tightened break-even or trailing stops to Binance exchange (F-05)
        for sym, pos in list(self.risk_manager.open_positions.items()):
            if pos.exit_policy in {"ema72_signal", "downtrend_rsi_recovery", "ema36_signal"}:
                continue
            cur_p = self.exchange.get_current_price(sym)
            if cur_p is not None and cur_p > 0:
                gain_pct = ((cur_p - pos.entry_price) / pos.entry_price) * 100
                if gain_pct >= 1.5 and not pos.break_even_activated:
                    pos.stop_loss_price = max(pos.stop_loss_price, pos.entry_price)
                    pos.break_even_activated = True
                    if not self.risk_manager._save_positions() or not self._sync_exchange_stop(sym):
                        self.risk_manager.halt(f"Break-even stop update failed for {sym}; manual reconciliation required")
                        return
                elif pos.break_even_activated:
                    trailing_sl = pos.highest_price * (1 - 0.015)
                    if trailing_sl > pos.stop_loss_price:
                        pos.stop_loss_price = trailing_sl
                        if not self.risk_manager._save_positions() or not self._sync_exchange_stop(sym):
                            self.risk_manager.halt(f"Trailing stop update failed for {sym}; manual reconciliation required")
                            return

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
                safe_to_reprotect = self._sell_was_not_submitted(symbol, key)
                restored = safe_to_reprotect and self._place_and_persist_native_stop(
                    symbol, f"reprotect_{int(time.time()*1000)}",
                    fallback_sl=old_stop_price, fallback_amount=position_amount,
                )
                self.risk_manager.halt(
                    f"Stop-exit order state requires reconciliation for {symbol}; "
                    + ("protective stop restored" if restored else "do not duplicate an uncertain sell; operator reconciliation required")
                )
                logger.error("Stop-exit order state requires reconciliation for %s (%s)", symbol, type(exc).__name__)
                return
            if not order:
                restored = self._place_and_persist_native_stop(
                    symbol, f"reprotect_{int(time.time()*1000)}",
                    fallback_sl=old_stop_price, fallback_amount=position_amount,
                )
                self.risk_manager.halt(
                    f"Stop-exit order was rejected for {symbol}; "
                    f"protective stop {'restored' if restored else 'could not be restored; operator action required'}"
                )
                return
            if order:
                if not self._confirmed_full_fill(order, position_amount):
                    if self._recover_terminal_partial_sell(symbol, order, client_order_id, position_amount):
                        return
                    if str(order.get("status", "")).lower() not in {"closed", "canceled", "rejected", "expired"}:
                        self.risk_manager.halt(
                            f"Stop-exit order for {symbol} remains open or uncertain; trading halted until the order is reconciled"
                        )
                        return
                    if not self._terminal_zero_fill(order):
                        self.risk_manager.halt(
                            f"Partial stop-exit for {symbol} could not be reconciled; protective order was not duplicated"
                        )
                        return
                    self.risk_manager.store.mark_order_applied(client_order_id)
                    self._place_and_persist_native_stop(symbol, f"reprotect_{int(time.time()*1000)}", fallback_sl=old_stop_price, fallback_amount=position_amount)
                    self.risk_manager.halt(f"Unconfirmed or partial stop-exit for {symbol}; protective stop restored, reconcile exchange orders and balances")
                    logger.error("Stop-exit for %s was not confirmed fully filled; position remains protected", symbol)
                    return
                fill_price = float(order.get("average") or order.get("price") or 0)
                if fill_price <= 0:
                    self._place_and_persist_native_stop(symbol, f"reprotect_{int(time.time()*1000)}", fallback_sl=old_stop_price, fallback_amount=position_amount)
                    self.risk_manager.halt(f"Invalid stop-exit fill details for {symbol}; protective stop restored, reconcile exchange orders and balances")
                    return
                fee_cost = self._resolve_fee_quote_cost(order, symbol, fill_price)

                record = self.risk_manager.close_position(symbol, fill_price, reason, fee_cost=fee_cost)

                if record:
                    self.risk_manager.store.mark_order_applied(client_order_id)
                    pnl_text = (
                        f"${record.pnl:+,.2f} ({record.pnl_pct:+.2f}%)"
                        if record.accounting_complete else "pending fee/partial-fill reconciliation"
                    )
                    msg = (
                        f"🛡️ *POSITION CLOSED ({reason})*\n"
                        f"Pair: `{symbol}`\n"
                        f"Exit Price: `${fill_price:,.2f}`\n"
                        f"P&L: {pnl_text}"
                    )
                    self.send_notification(msg, subject=f"🛡️ Position Closed: {symbol} ({reason})")
                    self._log_closed_trade(record, record.reason, fill_price, str(order.get("id", "")))

    def print_status(self):
        """Print current bot status including open positions and P&L."""
        balance = self.exchange.get_usdt_balance()
        positions = self.risk_manager.open_positions
        mode = self._trading_mode_label()

        print(f"\n{'─' * 60}")
        print(f"  Cycle #{self._cycle_count} | "
              f"Mode: {mode} | "
              f"USDT Balance: ${balance:,.2f} | "
              f"Positions: {len(positions)}/{self.config.risk.max_open_positions}")

        if positions:
            for symbol, pos in positions.items():
                current = self.exchange.get_current_price(symbol)
                if current is None or current <= 0:
                    target = ("EMA72 close exit" if pos.exit_policy == "ema72_signal"
                              else "EMA36 close exit" if pos.exit_policy == "ema36_signal"
                              else f"${pos.take_profit_price:,.2f} (2×ATR target)" if pos.exit_policy == "downtrend_rsi_recovery"
                              else f"${pos.take_profit_price:,.2f}")
                    print(f"    ⚠️ {symbol}: price unavailable | SL: ${pos.stop_loss_price:,.2f} | TP/exit: {target}")
                    continue
                pnl = pos.unrealized_pnl(current)
                pnl_pct = pos.unrealized_pnl_pct(current)
                emoji = "📈" if pnl >= 0 else "📉"
                be_tag = " [BREAK-EVEN]" if pos.break_even_activated else ""
                target = ("EMA72 close exit" if pos.exit_policy == "ema72_signal"
                          else f"${pos.take_profit_price:,.2f} (2×ATR target)" if pos.exit_policy == "downtrend_rsi_recovery"
                          else f"${pos.take_profit_price:,.2f}")
                print(
                    f"    {emoji} {symbol}: ${pos.entry_price:,.2f} → "
                    f"${current:,.2f} | P&L: ${pnl:+,.2f} ({pnl_pct:+.2f}%){be_tag} | "
                    f"SL: ${pos.stop_loss_price:,.2f} | TP/exit: {target}"
                )

        print(f"{'─' * 60}")

    def _trading_mode_label(self) -> str:
        """Describe the exchange mode actually configured for this worker."""
        if not self.config.exchange.paper_trading:
            return "Live trading"
        if getattr(self.exchange, "is_fallback_simulated", False):
            return "Simulated paper fallback"
        environment = str(self.config.exchange.paper_environment).strip().lower()
        return {
            "demo": "Binance Demo",
            "testnet": "Spot Testnet",
            "simulated": "Simulated paper",
        }.get(environment, f"Paper ({environment or 'unknown'})")

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
                safe_to_reprotect = False
                key = f"emergency-stop:{position.order_id}:{datetime.now(timezone.utc).date().isoformat()}"
                try:
                    order, client_id = self._submit_market_order(symbol, "sell", position.amount, key)
                    if not order:
                        safe_to_reprotect = True
                        raise RuntimeError("the exchange explicitly rejected the close order")
                    if not self._confirmed_full_fill(order, position.amount):
                        if self._recover_terminal_partial_sell(symbol, order, client_id, position.amount):
                            return False, f"Emergency stop reconciled a partial close for {symbol}; remaining amount was re-protected and trading is halted."
                        if str(order.get("status", "")).lower() not in {"closed", "canceled", "rejected", "expired"}:
                            raise RuntimeError("the sell order remains open or uncertain; reconciliation is required before another sell order")
                        if not self._terminal_zero_fill(order):
                            raise RuntimeError("the terminal partial fill could not be reconciled; manual account review is required")
                        safe_to_reprotect = True
                        self.risk_manager.store.mark_order_applied(client_id)
                        raise RuntimeError("the close order was not fully confirmed")
                    fill_price = float(order.get("average") or order.get("price") or 0)
                    if not math.isfinite(fill_price) or fill_price <= 0:
                        raise RuntimeError("the exchange returned invalid fill details")
                    fee_cost = self._resolve_fee_quote_cost(order, symbol, fill_price)
                    record = self.risk_manager.close_position(
                        symbol, fill_price, "Emergency stop", fee_cost=fee_cost
                    )
                    if not record:
                        raise RuntimeError("closed-position state could not be persisted")
                    self._log_closed_trade(record, record.reason, fill_price, str(order.get("id", "")))
                    self.risk_manager.store.mark_order_applied(client_id)
                except Exception as exc:
                    safe_to_reprotect = safe_to_reprotect or self._sell_was_not_submitted(symbol, key)
                    if safe_to_reprotect:
                        self._place_and_persist_native_stop(
                            symbol, f"reprotect_{int(time.time()*1000)}",
                            fallback_sl=position.stop_loss_price, fallback_amount=position.amount,
                        )
                    self.risk_manager.halt(f"Emergency stop failed to close tracked position {symbol}; operator action required")
                    return False, f"Entries paused. Could not safely confirm the close for {symbol} ({type(exc).__name__})."

            self.risk_manager.halt("Emergency stop complete; manual review required before resuming")
            msg = f"Bot-owned exchange orders ({bot_cancelled_count}) were cancelled and tracked positions were closed. Trading is halted."
            if non_bot_untouched_count > 0:
                msg += f" Note: {non_bot_untouched_count} external/manual order(s) were left untouched."
            return True, msg
        finally:
            self._cycle_lock.release()

    def close_position_manually(self, symbol: str, allow_loss_close: bool = False) -> dict:
        """Market-close one tracked paper position after exchange reconciliation."""
        if not isinstance(allow_loss_close, bool):
            return {"success": False, "message": "Invalid loss-close confirmation."}
        if not self.config.exchange.paper_trading:
            return {"success": False, "message": "Manual dashboard closes are disabled outside paper trading."}
        if getattr(self.exchange, "is_fallback_simulated", False):
            return {"success": False, "message": "The authenticated Demo account is unavailable; no order was sent."}
        if symbol not in {pair.strip() for pair in self.config.strategy.trading_pairs}:
            return {"success": False, "message": "That market is not configured for this bot."}
        if not self._cycle_lock.acquire(timeout=10):
            return {"success": False, "message": "The bot is checking the account. Wait briefly and try again."}
        try:
            if symbol not in self.risk_manager.open_positions:
                return {"success": False, "message": "This position is no longer open."}
            if not self._reconcile_exchange_state():
                return {
                    "success": False,
                    "message": "Account check failed, so the close was not submitted. Review the account status first.",
                }

            position = self.risk_manager.open_positions[symbol]
            try:
                current_price = self.exchange.get_current_price(symbol)
            except Exception:
                logger.warning("Could not fetch a fresh price before manual close for %s", symbol, exc_info=True)
                current_price = None
            estimate_available = (
                isinstance(current_price, (int, float))
                and math.isfinite(float(current_price)) and current_price > 0
                and position.accounting_complete
            )
            estimated_net_pnl = None
            estimated_net_pnl_pct = None
            if estimate_available:
                current_price = float(current_price)
                gross_pnl = (current_price - position.entry_price) * position.amount
                estimated_exit_fee = current_price * position.amount * ESTIMATED_TAKER_FEE_RATE
                estimated_net_pnl = gross_pnl - position.entry_fee_quote - estimated_exit_fee
                cost_basis = position.entry_price * position.amount
                estimated_net_pnl_pct = estimated_net_pnl / cost_basis * 100 if cost_basis > 0 else None
            if (not estimate_available or estimated_net_pnl <= 0) and not allow_loss_close:
                message = (
                    f"Current estimated net result is ${estimated_net_pnl:+,.2f} ({estimated_net_pnl_pct:+.2f}%) after known entry fees and estimated sell fees."
                    if estimate_available and estimated_net_pnl_pct is not None else
                    "A reliable net estimate is unavailable because price or fee accounting is incomplete."
                )
                return {
                    "success": False,
                    "requires_confirmation": True,
                    "estimated_net_pnl": estimated_net_pnl,
                    "estimated_net_pnl_pct": estimated_net_pnl_pct,
                    "message": f"Close not submitted. {message} Review the estimate and explicitly confirm if you still want to close.",
                }

            previous_trade_count = len(self.risk_manager.trade_history)
            decision_key = f"dashboard-manual-close:{int(time.time() * 1000)}"
            signal = TradeSignal(
                Signal.SELL, symbol, 1.0,
                "Manually closed from dashboard", {},
            )
            self._handle_sell_signal(signal, decision_key=decision_key)

            if symbol in self.risk_manager.open_positions:
                return {
                    "success": False,
                    "message": self.risk_manager._halt_reason or "The position remains open; check its protective stop and exchange order state.",
                    "halted": self.risk_manager._is_halted,
                }
            if len(self.risk_manager.trade_history) <= previous_trade_count:
                self.risk_manager.halt("Manual close state could not be verified; operator reconciliation required")
                return {
                    "success": False,
                    "message": "The position state changed, but its close record could not be verified. Review exchange orders before retrying.",
                    "halted": True,
                }

            record = self.risk_manager.trade_history[-1]
            return {
                "success": True,
                "accounting_complete": record.accounting_complete,
                "symbol": record.symbol,
                "exit_price": record.exit_price,
                "pnl": record.pnl,
                "pnl_pct": record.pnl_pct,
                "loss_close_override": allow_loss_close and (not estimate_available or estimated_net_pnl <= 0),
                "message": (
                    f"Closed {symbol} at market. Net result: ${record.pnl:+,.2f} ({record.pnl_pct:+.2f}%)."
                    if record.accounting_complete and record.pnl is not None
                    else f"Closed {symbol}, but final fees or fills need reconciliation. New entries are halted."
                ),
            }
        except Exception as exc:
            logger.exception("Dashboard manual close failed for %s", symbol)
            position = self.risk_manager.open_positions.get(symbol)
            if position and not position.stop_order_id:
                self._place_and_persist_native_stop(
                    symbol, f"manual_close_reprotect_{int(time.time() * 1000)}",
                    fallback_sl=position.stop_loss_price, fallback_amount=position.amount,
                )
            self.risk_manager.halt(
                f"Dashboard close state requires review for {symbol}; check exchange orders before retrying"
            )
            return {
                "success": False,
                "message": f"Close could not be safely confirmed ({type(exc).__name__}). Check the exchange order before retrying.",
                "halted": self.risk_manager._is_halted,
            }
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
            halt_reason = self.risk_manager._halt_reason
            reviewable_halt = (
                self.risk_manager._is_halted
                and self.risk_manager.is_reviewed_clear_halt(halt_reason)
            )
            pending_unbaselined_stop = self._pending_unbaselined_stop_review()
            if self.risk_manager._is_halted and pending_unbaselined_stop:
                reviewable_halt = True
            if self.risk_manager.entry_mode() != "EXIT_ONLY" and not reviewable_halt:
                return False, "New entries are not paused."
            if acknowledge_manual_halt and reviewable_halt and (
                    self.risk_manager.open_positions or self.risk_manager.store.unresolved_orders()):
                return False, "Manual recovery is blocked while positions or unresolved orders remain. Inspect the configured Demo account."
            if not self._reconcile_exchange_state(
                    allow_manual_review_rebaseline=bool(acknowledge_manual_halt and reviewable_halt)):
                detail = self._last_account_error or self.risk_manager._halt_reason
                return False, f"Cannot resume: account reconciliation failed. {detail}"
            rolling_rebase_equity = None
            if (acknowledge_manual_halt and reviewable_halt
                    and self.risk_manager._is_halted
                    and self.risk_manager.is_rolling_drawdown_halt(self.risk_manager._halt_reason)):
                try:
                    rolling_rebase_equity = float(self._bot_managed_equity())
                except Exception as exc:
                    return False, (
                        "Cannot rebase the rolling drawdown peak: account equity is "
                        f"unavailable ({type(exc).__name__})."
                    )
            if self.risk_manager._is_halted:
                if not (acknowledge_manual_halt and reviewable_halt):
                    return False, f"Cannot resume while a safety halt is active: {self.risk_manager._halt_reason}"
                if self.risk_manager.open_positions or self.risk_manager.store.unresolved_orders():
                    return False, "Manual recovery is blocked while positions or unresolved orders remain. Inspect the Testnet account."
                if not self.risk_manager.acknowledge_manual_halt_review(
                        rolling_drawdown_rebase_equity=rolling_rebase_equity):
                    return False, "The safety-halt review could not be saved; trading remains halted."
            self.risk_manager.store.write_state("entry_mode", "ACTIVE")
            self.risk_manager.store.append_event("mode_changed", {"mode": "ACTIVE", "source": "operator_resume"})
            return True, "Entries resumed after successful account reconciliation."
        except Exception as exc:
            self.risk_manager.halt("Resume request could not be saved; operator review required")
            logger.exception("Could not safely resume entries (%s)", type(exc).__name__)
            return False, "Resume could not be saved. Trading remains paused; inspect service logs."
        finally:
            self._cycle_lock.release()

    def clear_route_loss_halts(self) -> tuple[bool, str]:
        """Clear route-scoped consecutive-loss halts after operator review.

        Route halts block only new entries for one strategy route (uptrend or
        downtrend); the portfolio-wide halt, daily-loss, and rolling-drawdown
        controls are unaffected. Requires a clean reconciliation with no
        unresolved orders.
        """
        if not self._cycle_lock.acquire(timeout=10):
            return False, "The trading worker is busy. Wait for the current account check and try again."
        try:
            if self.risk_manager.store.unresolved_orders():
                return False, "Cannot clear a route halt while unresolved orders remain; reconcile them first."
            if not self._reconcile_exchange_state():
                detail = self._last_account_error or "account reconciliation failed"
                return False, f"Cannot clear a route halt: {detail}"
            if not self.risk_manager.clear_route_halts():
                return False, "No route-scoped loss halt is active."
            return True, "Route-scoped loss halts cleared after successful account reconciliation."
        except Exception as exc:
            logger.exception("Could not clear route loss halts (%s)", type(exc).__name__)
            return False, "Route halt review could not be saved."
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
            entry_fee_quote = self._resolve_fee_quote_cost(buy, symbol, fill_price)
            self.risk_manager.open_position(
                symbol, fill_price, float(net_amount), buy.get("id", ""),
                entry_fee_quote=(entry_fee_quote if entry_fee_quote is not None else 0.0),
            )
            if entry_fee_quote is None:
                # An unpriced entry commission must not be recorded as a clean
                # zero-fee round trip; mark the smoke accounting incomplete.
                self.risk_manager.mark_position_accounting_incomplete(symbol)
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
            try:
                sell, sell_client_id = self._submit_market_order(symbol, "sell", net_amount, "testnet-smoke-sell")
            except Exception:
                safe_to_reprotect = self._sell_was_not_submitted(symbol, "testnet-smoke-sell")
                if safe_to_reprotect:
                    restored = self._place_and_persist_native_stop(
                        symbol, f"reprotect_{int(time.time()*1000)}",
                        fallback_sl=self.risk_manager.open_positions[symbol].stop_loss_price,
                        fallback_amount=net_amount,
                    )
                    self.risk_manager.halt(
                        "Testnet smoke sell was not submitted; "
                        f"protective stop {'restored' if restored else 'could not be restored; operator action required'}"
                    )
                else:
                    self.risk_manager.halt("Testnet smoke sell outcome is uncertain; do not duplicate the sell, reconcile immediately")
                raise
            if not self._confirmed_full_fill(sell, net_amount):
                if self._recover_terminal_partial_sell(symbol, sell, sell_client_id, net_amount):
                    raise RuntimeError("Partial Testnet sell was reconciled, re-protected, and halted for review")
                if sell is None or self._terminal_zero_fill(sell):
                    if sell is not None:
                        self.risk_manager.store.mark_order_applied(sell_client_id)
                    restored = self._place_and_persist_native_stop(
                        symbol, f"reprotect_{int(time.time()*1000)}",
                        fallback_sl=self.risk_manager.open_positions[symbol].stop_loss_price,
                        fallback_amount=net_amount,
                    )
                    self.risk_manager.halt(
                        "Testnet smoke sell failed; "
                        f"protective stop {'restored' if restored else 'could not be restored; operator action required'}"
                    )
                elif isinstance(sell, dict) and str(sell.get("status", "")).lower() in {"closed", "canceled", "rejected", "expired"}:
                    self.risk_manager.halt("Testnet smoke sell partial fill could not be reconciled; protective order was not duplicated")
                else:
                    self.risk_manager.halt("Testnet smoke sell outcome remains open or uncertain; manual reconciliation required")
                self.risk_manager.halt("Testnet smoke sell was not confirmed fully filled; position remains tracked")
                raise RuntimeError("Sell was not fully confirmed; use the emergency stop and reconcile")
            sell_price = float(sell.get("average") or sell.get("price") or 0)
            if not math.isfinite(sell_price) or sell_price <= 0:
                self.risk_manager.halt("Testnet smoke sell returned invalid fill details")
                raise RuntimeError("Sell fill details were invalid")
            exit_fee_quote = self._resolve_fee_quote_cost(sell, symbol, sell_price)
            record = self.risk_manager.close_position(
                symbol, sell_price, "Testnet order-path smoke check", fee_cost=exit_fee_quote,
            )
            if not record:
                raise RuntimeError("Closed-position state could not be persisted")
            self.risk_manager.store.mark_order_applied(sell_client_id)
            result = {
                "state": "complete", "symbol": symbol,
                "buy_order_id": str(buy.get("id", "")),
                "sell_order_id": str(sell.get("id", "")),
                "buy_price": fill_price, "sell_price": sell_price,
                "amount": str(net_amount),
                "entry_fee_quote": entry_fee_quote,
                "exit_fee_quote": exit_fee_quote,
                "accounting_complete": bool(record.accounting_complete),
                "pnl_net_fees": record.pnl,
            }
            self.risk_manager.store.write_state("testnet_smoke_trade", result)
            # Smoke orders validate exchange order handling, not the trading
            # strategy. Keep them out of strategy P&L/history, but make the
            # completed round-trip visible in the dashboard activity log.
            try:
                self.risk_manager.store.append_event("testnet_smoke_trade_completed", {
                    "symbol": symbol,
                    "amount": str(net_amount),
                    "buy_price": fill_price,
                    "sell_price": sell_price,
                    "entry_fee_quote": entry_fee_quote,
                    "exit_fee_quote": exit_fee_quote,
                    "accounting_complete": bool(record.accounting_complete),
                    "pnl_net_fees": record.pnl,
                    "recovered_from_saved_result": False,
                })
            except Exception:
                logger.exception("Testnet smoke trade completed but its activity event could not be saved")
            return result
        finally:
            self._cycle_lock.release()

    def _enforce_equity_circuit_breakers(self) -> None:
        """Enforce local daily-loss / rolling-drawdown halt checks.

        These value bot-managed equity from cached cash plus positions marked at
        current ticker prices. They never place an order and may only HALT (never
        clear a halt, since a legacy-equity halt clear requires a reconciled
        account). Safe to run even when exchange reconciliation has failed, so
        loss limits keep firing during an outage instead of being skipped.
        """
        try:
            equity = self._bot_managed_equity()
            if not self.risk_manager.monitor_equity(
                float(equity), account_reconciled=self._last_reconciliation_ok
            ):
                logger.error("Equity risk circuit breaker is active: %s", self.risk_manager._halt_reason)
        except Exception:
            self.risk_manager.halt("Account equity valuation failed; operator review required")
            logger.exception("Equity valuation failed; new orders blocked")

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
                # Reconciliation blocks all order activity, but the local loss-limit
                # circuit breakers must still run so drawdown limits are enforced
                # during an outage rather than skipped until the next clean cycle.
                self._enforce_equity_circuit_breakers()
                return False
            self._enforce_equity_circuit_breakers()
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
