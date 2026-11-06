"""
Exchange connector — handles all communication with the crypto exchange.
Supports both paper trading (testnet) and live trading.
"""

import ccxt
import logging
import math
import time
from datetime import datetime, timezone
from config import ExchangeConfig

logger = logging.getLogger("bot.exchange")


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


class ExchangeConnector:
    """Manages the connection to a cryptocurrency exchange via ccxt."""

    def __init__(self, config: ExchangeConfig):
        self.config = config
        self.exchange = self._create_exchange()
        self._daily_pnl = 0.0
        self._trades_today = 0

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
            "options": {
                "defaultType": "spot",
                "adjustForTimeDifference": True,
            },
        })

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
            else:
                raise ValueError("PAPER_ENVIRONMENT must be either 'testnet' or 'demo'")
        else:
            logger.warning("💰 Running in LIVE TRADING mode — real money at risk!")

        return exchange

    def get_balance(self) -> dict:
        """Fetch current account balances."""
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
            logger.error("Failed to fetch balance (%s)", type(e).__name__)
            return {}

    def fetch_account_snapshot(self) -> tuple[dict, list]:
        """Strict authenticated snapshot; private API failures must not look empty."""
        try:
            balance = self.exchange.fetch_balance()
            orders = self.exchange.fetch_open_orders()
            return balance, orders
        except ccxt.BaseError as exc:
            logger.error("Private account reconciliation failed (%s)", type(exc).__name__)
            raise RuntimeError("Account reconciliation is unavailable") from exc

    def amount_to_precision(self, symbol: str, amount) -> str:
        self.exchange.load_markets()
        return self.exchange.amount_to_precision(symbol, str(amount))

    def get_market_limits(self, symbol: str) -> dict:
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
        try:
            return self.exchange.fetch_order_book(symbol, limit=limit)
        except ccxt.BaseError as exc:
            logger.error("Order book unavailable for %s (%s)", symbol, type(exc).__name__)
            raise RuntimeError("Order book preflight is unavailable") from exc

    def get_usdt_balance(self) -> float:
        """Get available USDT balance."""
        balance = self.get_balance()
        return balance.get("USDT", {}).get("free", 0.0)

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> list:
        """
        Fetch OHLCV (candlestick) data for a trading pair.

        Returns list of [timestamp, open, high, low, close, volume]
        """
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            logger.debug(f"Fetched {len(ohlcv)} candles for {symbol} ({timeframe})")
            return ohlcv
        except ccxt.BaseError as e:
            logger.error("Failed to fetch OHLCV for %s (%s)", symbol, type(e).__name__)
            return []

    def fetch_closed_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> list:
        """Fetch and filter candles so strategy signals use closed bars only."""
        candles = self.fetch_ohlcv(symbol, timeframe, limit)
        if not candles:
            return []
        try:
            seconds = float(self.exchange.parse_timeframe(timeframe))
        except (AttributeError, TypeError, ValueError) as exc:
            logger.error("Cannot determine candle duration for %s: %s", timeframe, exc)
            return []
        return filter_closed_ohlcv(candles, seconds)

    def get_ticker(self, symbol: str) -> dict:
        """Get current ticker data (price, volume, etc.)."""
        try:
            return self.exchange.fetch_ticker(symbol)
        except ccxt.BaseError as e:
            logger.error("Failed to fetch ticker for %s (%s)", symbol, type(e).__name__)
            return {}

    def get_current_price(self, symbol: str) -> float:
        """Get the current price of a trading pair."""
        ticker = self.get_ticker(symbol)
        return ticker.get("last", 0.0)

    def place_market_buy(self, symbol: str, amount, client_order_id: str | None = None) -> dict | None:
        """
        Place a market buy order.

        Args:
            symbol: Trading pair (e.g., 'BTC/USDT')
            amount: Amount of base currency to buy

        Returns:
            Order details dict or None on failure
        """
        try:
            params = {"newClientOrderId": client_order_id} if client_order_id else {}
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
        try:
            params = {"newClientOrderId": client_order_id} if client_order_id else {}
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
        """Get all open orders, optionally filtered by symbol."""
        try:
            return self.exchange.fetch_open_orders(symbol)
        except ccxt.BaseError as e:
            logger.error("Failed to fetch open orders (%s)", type(e).__name__)
            return []

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel an open order."""
        try:
            self.exchange.cancel_order(order_id, symbol)
            logger.info(f"Cancelled order {order_id} for {symbol}")
            return True
        except ccxt.BaseError as e:
            logger.error("Failed to cancel order %s (%s)", order_id, type(e).__name__)
            return False

    def get_min_order_amount(self, symbol: str) -> float:
        """Get the minimum order amount for a trading pair."""
        try:
            self.exchange.load_markets()
            market = self.exchange.market(symbol)
            return market.get("limits", {}).get("amount", {}).get("min", 0.0)
        except ccxt.BaseError as e:
            logger.error("Failed to get minimum amount for %s (%s)", symbol, type(e).__name__)
            return 0.0

    def get_server_time(self) -> datetime:
        """Get the exchange server time."""
        try:
            timestamp = self.exchange.fetch_time()
            return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
        except ccxt.BaseError:
            return datetime.now(timezone.utc)
