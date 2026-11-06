"""
Exchange connector — handles all communication with the crypto exchange.
Supports both paper trading (testnet) and live trading.
"""

import ccxt
import logging
from datetime import datetime, timezone
from config import ExchangeConfig

logger = logging.getLogger("bot.exchange")


class ExchangeConnector:
    """Manages the connection to a cryptocurrency exchange via ccxt."""

    def __init__(self, config: ExchangeConfig):
        self.config = config
        self.exchange = self._create_exchange()
        self._daily_pnl = 0.0
        self._trades_today = 0

    def _create_exchange(self):
        """Create and configure the ccxt exchange instance."""
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
            logger.error(f"Failed to fetch balance: {e}")
            return {}

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
            logger.error(f"Failed to fetch OHLCV for {symbol}: {e}")
            return []

    def get_ticker(self, symbol: str) -> dict:
        """Get current ticker data (price, volume, etc.)."""
        try:
            return self.exchange.fetch_ticker(symbol)
        except ccxt.BaseError as e:
            logger.error(f"Failed to fetch ticker for {symbol}: {e}")
            return {}

    def get_current_price(self, symbol: str) -> float:
        """Get the current price of a trading pair."""
        ticker = self.get_ticker(symbol)
        return ticker.get("last", 0.0)

    def place_market_buy(self, symbol: str, amount: float) -> dict | None:
        """
        Place a market buy order.

        Args:
            symbol: Trading pair (e.g., 'BTC/USDT')
            amount: Amount of base currency to buy

        Returns:
            Order details dict or None on failure
        """
        try:
            order = self.exchange.create_market_buy_order(symbol, amount)
            cost = order.get("cost", 0)
            price = order.get("average", order.get("price", 0))
            logger.info(
                f"✅ BUY {amount} {symbol} @ ${price:,.2f} "
                f"(Total: ${cost:,.2f})"
            )
            self._trades_today += 1
            return order
        except ccxt.InsufficientFunds as e:
            logger.error(f"Insufficient funds to buy {amount} {symbol}: {e}")
            return None
        except ccxt.BaseError as e:
            logger.error(f"Failed to place buy order for {symbol}: {e}")
            return None

    def place_market_sell(self, symbol: str, amount: float) -> dict | None:
        """
        Place a market sell order.

        Args:
            symbol: Trading pair (e.g., 'BTC/USDT')
            amount: Amount of base currency to sell

        Returns:
            Order details dict or None on failure
        """
        try:
            order = self.exchange.create_market_sell_order(symbol, amount)
            cost = order.get("cost", 0)
            price = order.get("average", order.get("price", 0))
            logger.info(
                f"✅ SELL {amount} {symbol} @ ${price:,.2f} "
                f"(Total: ${cost:,.2f})"
            )
            self._trades_today += 1
            return order
        except ccxt.BaseError as e:
            logger.error(f"Failed to place sell order for {symbol}: {e}")
            return None

    def get_open_orders(self, symbol: str = None) -> list:
        """Get all open orders, optionally filtered by symbol."""
        try:
            return self.exchange.fetch_open_orders(symbol)
        except ccxt.BaseError as e:
            logger.error(f"Failed to fetch open orders: {e}")
            return []

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel an open order."""
        try:
            self.exchange.cancel_order(order_id, symbol)
            logger.info(f"Cancelled order {order_id} for {symbol}")
            return True
        except ccxt.BaseError as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    def get_min_order_amount(self, symbol: str) -> float:
        """Get the minimum order amount for a trading pair."""
        try:
            self.exchange.load_markets()
            market = self.exchange.market(symbol)
            return market.get("limits", {}).get("amount", {}).get("min", 0.0)
        except ccxt.BaseError as e:
            logger.error(f"Failed to get min order amount for {symbol}: {e}")
            return 0.0

    def get_server_time(self) -> datetime:
        """Get the exchange server time."""
        try:
            timestamp = self.exchange.fetch_time()
            return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
        except ccxt.BaseError:
            return datetime.now(timezone.utc)
