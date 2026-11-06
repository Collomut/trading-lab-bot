"""
Web-Based Surveillance & Control Dashboard for the Automated Trading Bot.

Built on asynchronous aiohttp.web.
Serves a sleek dark-mode trading interface at http://localhost:5000:
  - Real-time balances and ticker prices
  - Live open positions tracking with dynamic unrealized P&L
  - Break-even protection and trailing stop visual indicators
  - Interactive "Run Analysis" control
  - Complete trade history and audit log
"""

import os
import json
import asyncio
from pathlib import Path
from aiohttp import web

from config import BotConfig
from exchange import ExchangeConnector
from strategies import get_strategy
from risk_manager import RiskManager
from bot_engine import TradingBot


class WebDashboard:
    def __init__(self, port: int = 5000):
        self.port = port
        self.config = BotConfig()
        self.bot = TradingBot(self.config)
        self.app = web.Application()
        self._setup_routes()

    def _setup_routes(self):
        self.app.router.add_get('/', self.handle_index)
        self.app.router.add_get('/api/status', self.handle_status)
        self.app.router.add_post('/api/run-cycle', self.handle_run_cycle)

    async def handle_index(self, request):
        """Serve the HTML dashboard."""
        html_path = Path(__file__).parent / "templates" / "index.html"
        if not html_path.exists():
            return web.Response(text="<h1>Dashboard template not found</h1>", content_type="text/html")
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()
        return web.Response(text=content, content_type="text/html")

    async def _get_current_status(self) -> dict:
        """Fetch real-time bot and market status."""
        loop = asyncio.get_event_loop()
        exchange = self.bot.exchange
        risk = self.bot.risk_manager

        # Balance
        usdt_balance = await loop.run_in_executor(None, exchange.get_usdt_balance)

        # Current prices of monitored pairs
        prices = {}
        for sym in self.config.strategy.trading_pairs:
            sym = sym.strip()
            p = await loop.run_in_executor(None, exchange.get_current_price, sym)
            prices[sym] = round(p, 2)

        # Open positions
        positions = {}
        for sym, pos in risk.open_positions.items():
            positions[sym] = pos.to_dict()

        # Trade history from trades.jsonl
        trades = []
        if self.bot.trade_log_path.exists():
            try:
                with open(self.bot.trade_log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            trades.append(json.loads(line))
            except Exception:
                pass

        return {
            "paper_trading": self.config.exchange.paper_trading,
            "exchange": self.config.exchange.name,
            "balance": round(usdt_balance, 2),
            "daily_pnl": round(risk._daily_loss, 2),
            "positions": positions,
            "positions_count": len(positions),
            "max_positions": self.config.risk.max_open_positions,
            "prices": prices,
            "strategy_name": self.config.strategy.name.upper(),
            "timeframe": self.config.strategy.timeframe,
            "stats": risk.get_performance_summary(),
            "trades": trades,
        }

    async def handle_status(self, request):
        """API endpoint returning live JSON status."""
        try:
            status = await self._get_current_status()
            return web.json_response(status)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_run_cycle(self, request):
        """Trigger an instant analysis cycle via web button."""
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self.bot.run_once)
            status = await self._get_current_status()
            return web.json_response(status)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    def run(self):
        """Start the dashboard server."""
        print(f"\n🚀 Trading Bot Web Dashboard launching at:")
        print(f"👉 http://localhost:{self.port}\n")
        web.run_app(self.app, host="0.0.0.0", port=self.port)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Trading Bot Web Dashboard")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    args = parser.parse_args()

    dashboard = WebDashboard(port=args.port)
    dashboard.run()
