"""
Exchange connector — handles all communication with the crypto exchange.
Supports both paper trading (testnet) and live trading.
"""

import ccxt
import os
import logging
import json
import math
import re
import time
import threading
from datetime import datetime, timezone
from config import ExchangeConfig

logger = logging.getLogger("bot.exchange")


class AccountAccessError(RuntimeError):
    """Safe, actionable account-access failure without echoing credentials."""


def extract_ccxt_response_metadata(exchange_obj, exc: Exception | None = None) -> tuple[dict | str | None, int | None]:
    """Safely extract response body and numeric HTTP status code from a CCXT exchange object and exception."""
    body = None
    http_status = None

    if exchange_obj is not None:
        # Prefer parsed JSON dict if available, otherwise raw HTTP body.
        # Check snake_case (CCXT Python standard) and camelCase as fallback.
        body = (
            getattr(exchange_obj, "last_json_response", None)
            or getattr(exchange_obj, "lastJsonResponse", None)
            or getattr(exchange_obj, "last_http_response", None)
            or getattr(exchange_obj, "lastHttpResponse", None)
        )
        raw_status = getattr(exchange_obj, "last_http_status", None)
        if isinstance(raw_status, int):
            http_status = raw_status

    if http_status is None and exc is not None:
        status_match = re.search(r'\b(400|401|403|404|418|429|500|502|503|504)\b', str(exc))
        if status_match:
            try:
                http_status = int(status_match.group(1))
            except ValueError:
                pass

    return body, http_status


def account_access_message(exc: Exception, response_body=None, http_status=None) -> str:
    """Safe, actionable account-access failure diagnosis without echoing credentials.

    Surfaces only allowlisted Binance error codes, HTTP status, and sanitized
    guidance. Never reflects raw URLs, request signatures, headers, or full bodies.
    """
    error_text = str(exc).lower() if exc else ""
    response_data = {}
    if isinstance(response_body, dict):
        response_data = response_body
    elif isinstance(response_body, str) and response_body.strip():
        try:
            parsed = json.loads(response_body)
            if isinstance(parsed, dict):
                response_data = parsed
        except (TypeError, ValueError):
            pass

    response_code = response_data.get("code") if isinstance(response_data, dict) else None
    match = re.search(r'"?code"?\s*[:=]\s*(-?\d+)', error_text, re.IGNORECASE)
    code = str(response_code) if isinstance(response_code, (int, str)) else (match.group(1) if match else "")
    response_message = str(response_data.get("msg", "")).lower() if isinstance(response_data, dict) else ""
    combined_text = f"{error_text} {response_message}".strip()

    valid_http_status = http_status if isinstance(http_status, int) else None
    if valid_http_status is None and exc:
        status_match = re.search(r'\b(400|401|403|404|418|429|500|502|503|504)\b', error_text)
        if status_match:
            try:
                valid_http_status = int(status_match.group(1))
            except ValueError:
                pass

    if code == "-2015" or "invalid api-key, ip, or permissions" in combined_text:
        return "Binance rejected the API key, IP, or permissions (-2015). Use an active Spot Testnet key pair and check its IP restrictions and Spot permission."
    if code == "-2014" or "api-key format invalid" in combined_text:
        return "Binance says the API key format is invalid (-2014). Re-enter the complete Spot Testnet API key without spaces."
    if code == "-1022" or "signature for this request is not valid" in combined_text:
        return "Binance rejected the request signature (-1022). API_SECRET does not match this API_KEY; re-enter the matching Spot Testnet pair."
    if code == "-1021" or "timestamp for this request is outside" in combined_text or "timestamp for this request was" in combined_text:
        return "Binance rejected the request timestamp (-1021). Check server clock synchronization and retry."
    if code == "-1003" or valid_http_status in (418, 429) or isinstance(exc, ccxt.RateLimitExceeded):
        return "Binance Testnet rate-limited the account request. The bot is waiting and will retry."
    if code == "-2010" or "account has insufficient balance" in combined_text:
        return "Binance reported insufficient balance or rejected the trade rules (-2010)."

    if valid_http_status == 403 or isinstance(exc, ccxt.PermissionDenied):
        status_label = f" (HTTP {valid_http_status})" if valid_http_status else ""
        return f"Binance denied account access{status_label}. Check the Testnet key permissions, WAF rules, and any IP restrictions."
    if valid_http_status == 401 or isinstance(exc, ccxt.AuthenticationError):
        status_label = f" (HTTP {valid_http_status})" if valid_http_status else ""
        return (
            f"Binance rejected Testnet authentication{status_label}. Check that API_KEY and API_SECRET are the matching pair "
            "created for Spot Testnet, are active, and have Spot trading permission. Do not use Demo or live keys."
        )
    if isinstance(exc, ccxt.NetworkError):
        return "Binance Testnet could not be reached. Check service networking and retry after it recovers."

    suffix = f" (Binance error {code})" if code else ""
    if not code and isinstance(valid_http_status, int):
        suffix = f" (HTTP {valid_http_status})"
    return (
        f"Binance Testnet rejected the authenticated account request{suffix}. In Railway, verify that API_KEY and "
        "API_SECRET are the matching, active Spot Testnet pair, with Spot permission and any required IP access. "
        "Demo or live credentials will not work on this endpoint."
    )


def filter_closed_ohlcv(candles: list, timeframe_seconds: float, now_ms: int | None = None) -> list:
    """Keep only complete, structurally valid OHLCV candles."""
    if not isinstance(candles, list) or not math.isfinite(timeframe_seconds) or timeframe_seconds <= 0:
        return []
    cutoff_ms = (int(time.time() * 1000) if now_ms is None else int(now_ms)) - 2000
    duration_ms = timeframe_seconds * 1000
    result = []
    for candle in candles:
        if not isinstance(candle, (list, tuple)) or len(candle) < 6:
            continue
        try:
            timestamp = int(candle[0])
            open_price, high, low, close, volume = (float(value) for value in candle[1:6])
        except (TypeError, ValueError, OverflowError):
            continue
        values = (open_price, high, low, close, volume)
        if not all(math.isfinite(value) for value in values):
            continue
        if timestamp <= 0 or min(open_price, high, low, close) <= 0 or volume < 0:
            continue
        if high < max(open_price, close, low) or low > min(open_price, close):
            continue
        if timestamp + duration_ms > cutoff_ms:
            continue
        result.append([timestamp, open_price, high, low, close, volume])
    result.sort(key=lambda row: row[0])
    deduplicated = {}
    for candle in result:
        deduplicated[candle[0]] = candle
    return list(deduplicated.values())


def validate_candle_sequence(candles: list, timeframe_seconds: float, now_ms: int | None = None,
                             symbol: str | None = None) -> list:
    """Reject gaps or stale latest bars rather than calculating signals on incomplete history."""
    if not candles or not math.isfinite(timeframe_seconds) or timeframe_seconds <= 0:
        return []
    ordered = sorted(candles, key=lambda row: row[0])
    duration_ms = int(timeframe_seconds * 1000)
    if duration_ms <= 0:
        return []
    for index in range(1, len(ordered)):
        expected_ts = ordered[index - 1][0] + duration_ms
        actual_ts = ordered[index][0]
        if actual_ts != expected_ts:
            delta_ms = actual_ts - ordered[index - 1][0]
            logger.warning(
                "Candle sequence gap for %s: expected timestamp %s, found %s "
                "(step %s ms; %.2f intervals)",
                symbol or "unknown symbol", expected_ts, actual_ts, delta_ms,
                delta_ms / duration_ms,
            )
            return []
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    latest_close = ordered[-1][0] + duration_ms
    if now - latest_close > duration_ms * 2 or latest_close > now + 2000:
        return []
    return ordered


class ExchangeConnector:
    """Manages the connection to a cryptocurrency exchange via ccxt."""

    @property
    def _exchange_lock(self):
        if not hasattr(self, "_lock_obj"):
            self._lock_obj = threading.RLock()
        return self._lock_obj

    def __init__(self, config: ExchangeConfig):
        self.config = config
        self._lock_obj = threading.RLock()
        self.exchange = self._create_exchange()
        self.market_data_exchange = self._create_public_market_data_exchange()
        self._daily_pnl = 0.0
        self._trades_today = 0
        try:
            self._sim_cash = float(os.getenv("INITIAL_BALANCE", "10000.0"))
        except ValueError:
            self._sim_cash = 10000.0
        self._sim_positions = {}
        self._sim_orders = {}
        self._sim_stops = {}
        self._candle_sequence_issues = {}
        self.is_fallback_simulated = False
        self.fallback_reason = ""

    def _create_public_market_data_exchange(self):
        """Use Binance's unauthenticated public data API for candles in Testnet mode.

        Spot Testnet may not retain enough history to warm up long indicators.
        This client has no credentials and is never used for balances or orders.
        """
        if not (self.config.paper_trading and self.config.paper_environment == "testnet"):
            return None
        if self.config.name.strip().lower() != "binance":
            return None
        public_exchange = getattr(ccxt, self.config.name)({
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })
        public_urls = public_exchange.urls.get("api", {})
        if isinstance(public_urls, dict) and "public" in public_urls:
            public_urls["public"] = "https://data-api.binance.vision/api/v3"
        logger.info(
            "Testnet mode: candles use Binance's unauthenticated public Spot data API; "
            "account and order requests remain on Spot Testnet"
        )
        return public_exchange

    def _is_simulated(self) -> bool:
        cfg = getattr(self, "config", None)
        if not cfg or not getattr(cfg, "paper_trading", False):
            return False
        return getattr(cfg, "paper_environment", "") == "simulated" or getattr(self, "is_fallback_simulated", False)

    def _get_simulated_balance_dict(self) -> dict:
        cash = getattr(self, "_sim_cash", 10000.0)
        total_holdings = {"USDT": cash}
        for sym, amt in getattr(self, "_sim_positions", {}).items():
            base = sym.split("/")[0]
            total_holdings[base] = amt
        return {
            "total": total_holdings,
            "free": {"USDT": cash},
            "used": {"USDT": 0.0},
        }

    def _get_simulated_holdings(self) -> dict:
        cash = getattr(self, "_sim_cash", 10000.0)
        holdings = {"USDT": {"total": cash, "free": cash, "used": 0.0}}
        for sym, amt in getattr(self, "_sim_positions", {}).items():
            if amt > 0:
                base = sym.split("/")[0]
                holdings[base] = {"total": amt, "free": amt, "used": 0.0}
        return holdings

    def _fetch_simulated_snapshot(self) -> tuple[dict, list]:
        return self._get_simulated_balance_dict(), list(getattr(self, "_sim_stops", {}).values())

    def _simulated_market_buy(self, symbol: str, amount, client_order_id: str | None = None) -> dict:
        price = self.get_current_price(symbol)
        amt = float(amount)
        cost = amt * price
        self._sim_cash = max(0.0, getattr(self, "_sim_cash", 10000.0) - cost)
        if not hasattr(self, "_sim_positions"):
            self._sim_positions = {}
        self._sim_positions[symbol] = self._sim_positions.get(symbol, 0.0) + amt
        order_id = f"sim_buy_{int(time.time()*1000)}"
        cid = client_order_id or f"client_{order_id}"
        base = symbol.split("/")[0]
        order = {
            "id": order_id,
            "clientOrderId": cid,
            "symbol": symbol,
            "side": "buy",
            "type": "market",
            "status": "closed",
            "price": price,
            "average": price,
            "amount": amt,
            "filled": amt,
            "remaining": 0.0,
            "cost": cost,
            "fee": {"currency": base, "cost": amt * 0.001},
            "timestamp": int(time.time() * 1000),
        }
        if not hasattr(self, "_sim_orders"):
            self._sim_orders = {}
        self._sim_orders[order_id] = order
        self._sim_orders[cid] = order
        return order

    def _simulated_market_sell(self, symbol: str, amount, client_order_id: str | None = None) -> dict:
        price = self.get_current_price(symbol)
        amt = float(amount)
        cost = amt * price
        self._sim_cash = getattr(self, "_sim_cash", 10000.0) + cost
        if not hasattr(self, "_sim_positions"):
            self._sim_positions = {}
        self._sim_positions[symbol] = max(0.0, self._sim_positions.get(symbol, 0.0) - amt)
        order_id = f"sim_sell_{int(time.time()*1000)}"
        cid = client_order_id or f"client_{order_id}"
        order = {
            "id": order_id,
            "clientOrderId": cid,
            "symbol": symbol,
            "side": "sell",
            "type": "market",
            "status": "closed",
            "price": price,
            "average": price,
            "amount": amt,
            "filled": amt,
            "remaining": 0.0,
            "cost": cost,
            "fee": {"currency": "USDT", "cost": cost * 0.001},
            "timestamp": int(time.time() * 1000),
        }
        if not hasattr(self, "_sim_orders"):
            self._sim_orders = {}
        self._sim_orders[order_id] = order
        self._sim_orders[cid] = order
        return order

    def _create_exchange(self):
        """Create a paper/demo exchange connection; live connections are disabled."""
        if not self.config.paper_trading:
            raise RuntimeError(
                "Live trading is disabled in this build; refusing to initialize a live exchange connection"
            )
        exchange_class = getattr(ccxt, self.config.name)
        api_key = self.config.api_key
        api_secret = self.config.api_secret
        if self.config.paper_trading and self.config.paper_environment == "demo":
            api_key = self.config.demo_api_key
            api_secret = self.config.demo_api_secret
            if not api_key or not api_secret:
                raise RuntimeError("DEMO_API_KEY and DEMO_API_SECRET are required for Binance Demo Mode")

        exchange = exchange_class({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "enableLastJsonResponse": True,
            "enableLastHttpResponse": True,
            "enableLastResponseHeaders": True,
            "options": {
                "defaultType": "spot",
                "adjustForTimeDifference": True,
                "warnOnFetchOpenOrdersWithoutSymbol": False,
                "fetchOpenOrders": {
                    "warnWithoutSymbol": False,
                },
            },
        })
        exchange.enableLastJsonResponse = True
        exchange.enableLastHttpResponse = True
        exchange.enableLastResponseHeaders = True
        exchange.options["warnOnFetchOpenOrdersWithoutSymbol"] = False
        if "fetchOpenOrders" not in exchange.options:
            exchange.options["fetchOpenOrders"] = {}
        if isinstance(exchange.options["fetchOpenOrders"], dict):
            exchange.options["fetchOpenOrders"]["warnWithoutSymbol"] = False

        if self.config.paper_trading:
            if self.config.paper_environment == "demo":
                enable_demo_trading = getattr(exchange, "enable_demo_trading", None)
                if not callable(enable_demo_trading):
                    raise RuntimeError(
                        "Installed CCXT does not support Binance Demo Mode; upgrade CCXT to >=4.5.6"
                    )
                enable_demo_trading(True)
                logger.info("🧪 Running in Binance SPOT DEMO mode (no real funds)")
            elif self.config.paper_environment == "testnet":
                exchange.set_sandbox_mode(True)
                logger.info("🧪 Running in PAPER TRADING mode (testnet)")
            elif self.config.paper_environment == "simulated":
                logger.info("🧪 Running in SIMULATED PAPER TRADING mode (real Binance market data + virtual wallet)")
            else:
                raise ValueError("PAPER_ENVIRONMENT must be either 'testnet', 'demo', or 'simulated'")
        else:
            logger.warning("💰 Running in LIVE TRADING mode — real money at risk!")

        return exchange

    def get_balance(self) -> dict:
        """Fetch current account balances; private API errors must not look like zero."""
        if self._is_simulated():
            return self._get_simulated_holdings()
        with self._exchange_lock:
            try:
                balance = self.exchange.fetch_balance()
                # Extract non-zero balances
                holdings = {}
                for currency, amount in balance.get("total", {}).items():
                    if amount and amount > 0:
                        holdings[currency] = {
                            "total": amount,
                            "free": balance["free"].get(currency, 0),
                            "used": balance["used"].get(currency, 0),
                        }
                return holdings
            except ccxt.BaseError as e:
                body, status_code = extract_ccxt_response_metadata(self.exchange, e)
                safe_message = account_access_message(e, body, status_code)
                cfg = getattr(self, "config", None)
                if (cfg and getattr(cfg, "paper_trading", False) and
                        getattr(cfg, "fallback_to_simulation", False) and
                        "-2015" in str(safe_message)):
                    self.is_fallback_simulated = True
                    self.fallback_reason = safe_message
                    return self._get_simulated_holdings()
                logger.error("Failed to fetch balance (%s): %s", type(e).__name__, safe_message)
                raise AccountAccessError(safe_message) from e

    def fetch_account_snapshot(self) -> tuple[dict, list]:
        """Strict authenticated snapshot; private API failures must not look empty."""
        if self._is_simulated():
            return self._fetch_simulated_snapshot()
        with self._exchange_lock:
            try:
                balance = self.exchange.fetch_balance()
                orders = []
                try:
                    orders = self.exchange.fetch_open_orders()
                except ccxt.ExchangeError:
                    # Reconciliation cannot assume an empty account when the
                    # exchange did not return its open orders. Fail closed so
                    # a permission or endpoint error cannot hide a live stop
                    # or another order that could race the bot's next action.
                    raise
                return balance, orders
            except ccxt.BaseError as exc:
                body, status_code = extract_ccxt_response_metadata(self.exchange, exc)
                safe_message = account_access_message(exc, body, status_code)
                cfg = getattr(self, "config", None)
                if (cfg and getattr(cfg, "paper_trading", False) and
                        getattr(cfg, "fallback_to_simulation", False) and
                        "-2015" in str(safe_message)):
                    self.is_fallback_simulated = True
                    self.fallback_reason = safe_message
                    logger.warning("Binance Spot Testnet authentication rejected credentials (-2015). Activating safe simulated paper trading mode with real-time market data.")
                    return self._fetch_simulated_snapshot()
                logger.error("Private account reconciliation failed (%s): %s", type(exc).__name__, safe_message)
                raise AccountAccessError(safe_message) from exc

    def fetch_balance_snapshot(self) -> dict:
        """Read account balances independently from the open-order permission."""
        if self._is_simulated():
            return self._get_simulated_balance_dict()
        with self._exchange_lock:
            try:
                return self.exchange.fetch_balance()
            except ccxt.BaseError as exc:
                body, status_code = extract_ccxt_response_metadata(self.exchange, exc)
                safe_message = account_access_message(exc, body, status_code)
                cfg = getattr(self, "config", None)
                if (cfg and getattr(cfg, "paper_trading", False) and
                        getattr(cfg, "fallback_to_simulation", False) and
                        "-2015" in str(safe_message)):
                    self.is_fallback_simulated = True
                    self.fallback_reason = safe_message
                    logger.warning("Binance Spot Testnet balance request rejected credentials (-2015). Activating safe simulated paper trading mode with real-time market data.")
                    return self._get_simulated_balance_dict()
                logger.error("Private balance request failed (%s): %s", type(exc).__name__, safe_message)
                raise AccountAccessError(safe_message) from exc

    def diagnose_account_access(self) -> dict:
        """Read-only account diagnostic testing balance and open-order access separately.

        Returns sanitized status, error code, and non-sensitive diagnostic messages.
        Never submits, cancels, or modifies orders.
        """
        result = {
            "environment": self.config.paper_environment if self.config.paper_trading else "live",
            "paper_trading": self.config.paper_trading,
            "balance_ok": False,
            "balance_error": None,
            "orders_ok": False,
            "orders_error": None,
            "overall_status": "FAILED",
        }
        if self._is_simulated():
            result["balance_ok"] = True
            result["orders_ok"] = True
            result["overall_status"] = "SIMULATED_WALLET"
            return result

        with self._exchange_lock:
            # 1. Test balance access independently
            try:
                self.exchange.fetch_balance()
                result["balance_ok"] = True
            except ccxt.BaseError as exc:
                body, status_code = extract_ccxt_response_metadata(self.exchange, exc)
                result["balance_error"] = account_access_message(exc, body, status_code)

            # 2. Test open-order access independently
            try:
                self.exchange.fetch_open_orders()
                result["orders_ok"] = True
            except ccxt.ExchangeError as ee:
                body, status_code = extract_ccxt_response_metadata(self.exchange, ee)
                result["orders_error"] = account_access_message(ee, body, status_code)
            except ccxt.BaseError as exc:
                body, status_code = extract_ccxt_response_metadata(self.exchange, exc)
                result["orders_error"] = account_access_message(exc, body, status_code)

        if result["balance_ok"] and result["orders_ok"]:
            result["overall_status"] = "AUTHENTICATED_AND_RECONCILED"
        elif result["balance_ok"]:
            result["overall_status"] = "BALANCE_ONLY_ORDER_PERMISSION_MISSING"
        else:
            result["overall_status"] = "AUTHENTICATION_FAILED"

        return result

    def amount_to_precision(self, symbol: str, amount) -> str:
        with self._exchange_lock:
            self.exchange.load_markets()
            return self.exchange.amount_to_precision(symbol, str(amount))

    def price_to_precision(self, symbol: str, price) -> str:
        with self._exchange_lock:
            self.exchange.load_markets()
            return self.exchange.price_to_precision(symbol, str(price))

    def get_market_limits(self, symbol: str) -> dict:
        with self._exchange_lock:
            self.exchange.load_markets()
            market = self.exchange.market(symbol)
            limits = market.get("limits", {})
            amount = limits.get("amount", {}) or {}
            cost = limits.get("cost", {}) or {}
            return {
                "min_amount": amount.get("min"), "max_amount": amount.get("max"),
                "min_cost": cost.get("min"), "max_cost": cost.get("max"),
                "active": market.get("active", True),
            }

    def get_order_book(self, symbol: str, limit: int = 20) -> dict:
        """Fetch L2 liquidity for entry preflight; unavailable means no entry."""
        with self._exchange_lock:
            try:
                return self.exchange.fetch_order_book(symbol, limit=limit)
            except ccxt.BaseError as exc:
                logger.error("Order book unavailable for %s (%s)", symbol, type(exc).__name__)
                raise RuntimeError("Order book preflight is unavailable") from exc

    def place_native_stop_loss(self, symbol: str, amount, stop_price, client_order_id: str) -> dict:
        """Place a Binance Spot STOP_LOSS market order on the exchange."""
        if self._is_simulated():
            order_id = f"sim_stop_{int(time.time()*1000)}"
            order = {
                "id": order_id,
                "clientOrderId": client_order_id,
                "symbol": symbol,
                "side": "sell",
                "type": "stop_loss_limit",
                "status": "open",
                "price": float(stop_price),
                "stopPrice": float(stop_price),
                "amount": float(amount),
                "filled": 0.0,
                "timestamp": int(time.time() * 1000),
            }
            if not hasattr(self, "_sim_stops"):
                self._sim_stops = {}
            if not hasattr(self, "_sim_orders"):
                self._sim_orders = {}
            self._sim_stops[symbol] = order
            self._sim_orders[order_id] = order
            self._sim_orders[client_order_id] = order
            return order
        with self._exchange_lock:
            try:
                prec_stop = self.price_to_precision(symbol, stop_price)
                return self.exchange.create_order(
                    symbol, "market", "sell", float(amount), None,
                    {
                        "stopLossPrice": prec_stop,
                        "clientOrderId": client_order_id,
                    },
                )
            except ccxt.BaseError as exc:
                body, status_code = extract_ccxt_response_metadata(self.exchange, exc)
                safe_msg = account_access_message(exc, body, status_code)
                logger.error("Native stop placement failed for %s (%s): %s", symbol, type(exc).__name__, safe_msg)
                raise RuntimeError(f"Exchange-native stop was not confirmed: {safe_msg}") from exc

    def fetch_order(self, order_id: str, symbol: str) -> dict:
        if self._is_simulated():
            oid = str(order_id)
            if hasattr(self, "_sim_orders") and oid in self._sim_orders:
                return self._sim_orders[oid]
            return {"id": order_id, "status": "closed", "price": 0.0, "average": 0.0, "filled": 0.0}
        with self._exchange_lock:
            try:
                return self.exchange.fetch_order(order_id, symbol)
            except ccxt.BaseError as exc:
                logger.error("Order status lookup failed for %s (%s)", symbol, type(exc).__name__)
                raise RuntimeError("Exchange order status is unavailable") from exc

    def fetch_order_by_client_id(self, client_order_id: str, symbol: str) -> dict:
        if self._is_simulated():
            cid = str(client_order_id)
            if hasattr(self, "_sim_orders") and cid in self._sim_orders:
                return self._sim_orders[cid]
            return {"id": client_order_id, "clientOrderId": client_order_id, "status": "closed"}
        with self._exchange_lock:
            try:
                return self.exchange.fetch_order(None, symbol, {"origClientOrderId": client_order_id})
            except ccxt.BaseError as exc:
                logger.error("Client-order reconciliation failed for %s (%s)", symbol, type(exc).__name__)
                raise RuntimeError("Client order status is unavailable") from exc

    def get_usdt_balance(self) -> float:
        """Get available USDT balance."""
        balance = self.get_balance()
        return balance.get("USDT", {}).get("free", 0.0)

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> list:
        """
        Fetch OHLCV data, paging backward when more than Binance's 1,000-row
        per-request limit is needed for a long indicator warmup.

        Returns list of [timestamp, open, high, low, close, volume]
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0 or limit > 5000:
            logger.error("Rejected invalid OHLCV limit for %s", symbol)
            return []
        with self._exchange_lock:
            try:
                data_exchange = getattr(self, "market_data_exchange", None) or self.exchange
                if limit <= 1000:
                    ohlcv = data_exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
                else:
                    step_ms = int(float(data_exchange.parse_timeframe(timeframe)) * 1000)
                    if step_ms <= 0:
                        return []
                    page_size = 1000
                    latest = data_exchange.fetch_ohlcv(symbol, timeframe, limit=page_size) or []
                    candles_by_time = {int(row[0]): row for row in latest if isinstance(row, (list, tuple)) and len(row) >= 6}
                    if not candles_by_time:
                        return []
                    first_timestamp = min(candles_by_time)
                    pages_needed = (limit + page_size - 1) // page_size + 1
                    for _ in range(pages_needed - 1):
                        if len(candles_by_time) >= limit:
                            break
                        since = first_timestamp - page_size * step_ms
                        older = data_exchange.fetch_ohlcv(
                            symbol, timeframe, since=since, limit=page_size
                        ) or []
                        new_rows = [
                            row for row in older
                            if isinstance(row, (list, tuple)) and len(row) >= 6
                            and int(row[0]) < first_timestamp
                        ]
                        if not new_rows:
                            break
                        for row in new_rows:
                            candles_by_time[int(row[0])] = row
                        new_first = min(int(row[0]) for row in new_rows)
                        if new_first >= first_timestamp:
                            break
                        first_timestamp = new_first
                    ohlcv = [candles_by_time[timestamp] for timestamp in sorted(candles_by_time)[-limit:]]
                    if len(ohlcv) < limit:
                        logger.warning(
                            "Only %s of %s requested candles are available for %s (%s)",
                            len(ohlcv), limit, symbol, timeframe,
                        )
                logger.debug("Fetched %s candles for %s (%s)", len(ohlcv), symbol, timeframe)
                return ohlcv
            except ccxt.BaseError as e:
                logger.error("Failed to fetch OHLCV for %s (%s)", symbol, type(e).__name__)
                return []
            except (TypeError, ValueError, IndexError, OverflowError) as e:
                logger.error("Invalid OHLCV pagination response for %s (%s)", symbol, type(e).__name__)
                return []

    def get_public_market_data_price(self, symbol: str) -> float | None:
        """Read a fresh public Binance Spot price for Testnet price-parity checks."""
        market_data_exchange = getattr(self, "market_data_exchange", None)
        if market_data_exchange is None:
            return None
        try:
            ticker = market_data_exchange.fetch_ticker(symbol)
            if not isinstance(ticker, dict) or ticker.get("last") is None:
                return None
            price = float(ticker["last"])
            timestamp = ticker.get("timestamp")
            if timestamp is None and ticker.get("datetime"):
                timestamp = market_data_exchange.parse8601(ticker["datetime"])
            if timestamp is None:
                return None
            age_ms = int(time.time() * 1000) - int(timestamp)
            if age_ms < -5000 or age_ms > 120_000 or not math.isfinite(price) or price <= 0:
                return None
            return price
        except (ccxt.BaseError, AttributeError, TypeError, ValueError, OverflowError) as exc:
            logger.warning("Public Binance price unavailable for %s (%s)", symbol, type(exc).__name__)
            return None

    def fetch_closed_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> list:
        """Fetch and filter candles so strategy signals use closed bars only."""
        self._candle_sequence_issues.pop(symbol, None)
        candles = self.fetch_ohlcv(symbol, timeframe, limit)
        if not candles:
            return []
        try:
            with self._exchange_lock:
                seconds = float(self.exchange.parse_timeframe(timeframe))
        except (AttributeError, TypeError, ValueError) as exc:
            logger.error("Cannot determine candle duration for %s: %s", timeframe, exc)
            return []
        closed = filter_closed_ohlcv(candles, seconds)
        duration_ms = int(seconds * 1000)
        ordered = sorted(closed, key=lambda row: row[0])
        for index in range(1, len(ordered)):
            expected_ts = int(ordered[index - 1][0]) + duration_ms
            actual_ts = int(ordered[index][0])
            if actual_ts != expected_ts:
                delta_ms = actual_ts - int(ordered[index - 1][0])
                self._candle_sequence_issues[symbol] = {
                    "expected_timestamp": expected_ts,
                    "actual_timestamp": actual_ts,
                    "step_ms": delta_ms,
                    "missing_intervals": max(0, (delta_ms // duration_ms) - 1) if duration_ms > 0 else None,
                }
                break
        sequence = validate_candle_sequence(closed, seconds, symbol=symbol)
        if not sequence and symbol not in self._candle_sequence_issues:
            logger.warning("Closed candle data for %s is stale, discontinuous, or invalid", symbol)
        return sequence

    def get_ticker(self, symbol: str) -> dict | None:
        """Get current ticker data (price, volume, etc.). Returns None on failure."""
        with self._exchange_lock:
            try:
                return self.exchange.fetch_ticker(symbol)
            except ccxt.BaseError as e:
                logger.error("Failed to fetch ticker for %s (%s)", symbol, type(e).__name__)
                return None

    def get_current_price(self, symbol: str) -> float | None:
        """Get a fresh finite positive ticker price, rejecting missing/stale exchange timestamps."""
        ticker = self.get_ticker(symbol)
        if not ticker or not isinstance(ticker, dict):
            return None
        last = ticker.get("last")
        if last is None:
            return None
        try:
            val = float(last)
            timestamp = ticker.get("timestamp")
            if timestamp is None and ticker.get("datetime"):
                timestamp = self.exchange.parse8601(ticker["datetime"])
            if timestamp is None:
                return None
            timestamp = int(timestamp)
            age_ms = int(time.time() * 1000) - timestamp
            if age_ms < -5000 or age_ms > 120_000:
                logger.warning("Ticker for %s is outside the freshness window", symbol)
                return None
            return val if math.isfinite(val) and val > 0 else None
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None

    def place_market_buy(self, symbol: str, amount, client_order_id: str | None = None) -> dict | None:
        """
        Place a market buy order.

        Args:
            symbol: Trading pair (e.g., 'BTC/USDT')
            amount: Amount of base currency to buy

        Returns:
            Order details dict or None on failure
        """
        if self._is_simulated():
            return self._simulated_market_buy(symbol, amount, client_order_id)
        with self._exchange_lock:
            try:
                # Let the symbol/account's Binance STP policy apply. Supported
                # modes vary by symbol; hard-coding one can reject every order.
                params = {"clientOrderId": client_order_id} if client_order_id else {}
                order = self.exchange.create_market_buy_order(symbol, amount, params)
                cost = order.get("cost", 0)
                price = order.get("average", order.get("price", 0))
                logger.info(
                    f"✅ BUY {amount} {symbol} @ ${price:,.2f} "
                    f"(Total: ${cost:,.2f})"
                )
                self._trades_today += 1
                return order
            except ccxt.InsufficientFunds as e:
                logger.error("Insufficient funds for buy order on %s (%s)", symbol, type(e).__name__)
                return None
            except ccxt.BaseError as e:
                # A timeout or exchange error can occur after acceptance. Treat order
                # outcome as unknown; callers must halt and reconcile before retrying.
                logger.error("Buy order outcome is unknown for %s; manual reconciliation required", symbol)
                raise RuntimeError("Exchange did not confirm buy order outcome") from e

    def place_market_sell(self, symbol: str, amount, client_order_id: str | None = None) -> dict | None:
        """
        Place a market sell order.

        Args:
            symbol: Trading pair (e.g., 'BTC/USDT')
            amount: Amount of base currency to sell

        Returns:
            Order details dict or None on failure
        """
        if self._is_simulated():
            return self._simulated_market_sell(symbol, amount, client_order_id)
        with self._exchange_lock:
            try:
                params = {"clientOrderId": client_order_id} if client_order_id else {}
                order = self.exchange.create_market_sell_order(symbol, amount, params)
                cost = order.get("cost", 0)
                price = order.get("average", order.get("price", 0))
                logger.info(
                    f"✅ SELL {amount} {symbol} @ ${price:,.2f} "
                    f"(Total: ${cost:,.2f})"
                )
                self._trades_today += 1
                return order
            except ccxt.BaseError as e:
                logger.error("Sell order outcome is unknown for %s; manual reconciliation required", symbol)
                raise RuntimeError("Exchange did not confirm sell order outcome") from e

    def get_open_orders(self, symbol: str = None) -> list:
        """Get open orders; propagate failures instead of implying none exist."""
        if self._is_simulated():
            return list(getattr(self, "_sim_stops", {}).values())
        with self._exchange_lock:
            try:
                return self.exchange.fetch_open_orders(symbol)
            except ccxt.BaseError as e:
                logger.error("Failed to fetch open orders (%s)", type(e).__name__)
                raise RuntimeError("Exchange open-order state is unavailable") from e

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel an open order."""
        if self._is_simulated():
            oid = str(order_id)
            if hasattr(self, "_sim_stops"):
                for s, o in list(self._sim_stops.items()):
                    if str(o.get("id")) == oid or str(o.get("clientOrderId")) == oid:
                        del self._sim_stops[s]
            if hasattr(self, "_sim_orders") and oid in self._sim_orders:
                self._sim_orders[oid]["status"] = "canceled"
            return True
        with self._exchange_lock:
            try:
                self.exchange.cancel_order(order_id, symbol)
                logger.info(f"Cancelled order {order_id} for {symbol}")
                return True
            except ccxt.BaseError as e:
                logger.error("Failed to cancel order %s (%s)", order_id, type(e).__name__)
                return False

    def get_min_order_amount(self, symbol: str) -> float:
        """Get the minimum order amount for a trading pair."""
        with self._exchange_lock:
            try:
                self.exchange.load_markets()
                market = self.exchange.market(symbol)
                return market.get("limits", {}).get("amount", {}).get("min", 0.0)
            except ccxt.BaseError as e:
                logger.error("Failed to get minimum amount for %s (%s)", symbol, type(e).__name__)
                return 0.0

    def get_server_time(self) -> datetime:
        """Get the exchange server time."""
        with self._exchange_lock:
            try:
                timestamp = self.exchange.fetch_time()
                return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
            except ccxt.BaseError:
                return datetime.now(timezone.utc)
