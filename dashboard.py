"""
Web-Based Surveillance & Control Dashboard for the Automated Trading Bot.

Built on asynchronous aiohttp.web.
Runs BOTH:
  1. The 24/7 automated strategy & risk engine in the background
  2. The real-time web surveillance interface on $PORT (defaults to 5000)
"""

import os
import json
import asyncio
import logging
from pathlib import Path
from aiohttp import web

from config import BotConfig
from exchange import ExchangeConnector
from strategies import get_strategy
from risk_manager import RiskManager
from bot_engine import TradingBot

logger = logging.getLogger("bot.dashboard")


class WebDashboard:
    def __init__(self, port: int = None):
        # Read from PORT env var for Railway / Cloud deployment
        self.port = port or int(os.getenv("PORT", 5000))
        self.config = BotConfig()
        self.bot = TradingBot(self.config)
        self.app = web.Application()
        self.app["bot"] = self.bot
        self.app["config"] = self.config
        self.bg_task = None
        self._setup_routes()
        self._setup_lifecycle()

    def _setup_routes(self):
        self.app.router.add_get('/', self.handle_index)
        self.app.router.add_get('/api/status', self.handle_status)
        self.app.router.add_post('/api/run-cycle', self.handle_run_cycle)

    def _setup_lifecycle(self):
        self.app.on_startup.append(self.start_background_tasks)
        self.app.on_cleanup.append(self.cleanup_background_tasks)

    async def start_background_tasks(self, app):
        """Start 24/7 continuous trading loop in the background."""
        self.bg_task = asyncio.create_task(self.background_trading_loop(app))
        logger.info("🚀 24/7 background trading worker activated.")

    async def cleanup_background_tasks(self, app):
        """Gracefully stop background loop on shutdown."""
        if self.bg_task:
            self.bg_task.cancel()
            try:
                await self.bg_task
            except asyncio.CancelledError:
                pass
        logger.info("Background trading worker stopped.")

    async def background_trading_loop(self, app):
        """Continuous market monitoring and trade execution loop."""
        interval = int(os.getenv("INTERVAL_SECONDS", "60"))
        logger.info(f"Background trading loop active (interval: {interval}s)")

        while True:
            try:
                loop = asyncio.get_event_loop()

                # 1. Check stop-loss, trailing stops and take-profits
                await loop.run_in_executor(None, self.bot.check_stop_loss_take_profit)

                # 2. Analyze each configured pair
                for symbol in self.config.strategy.trading_pairs:
                    sym = symbol.strip()
                    try:
                        await loop.run_in_executor(None, self.bot.analyze_market, sym)
                    except Exception as e:
                        logger.error(f"Error evaluating {sym} in background: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in continuous background loop: {e}")

            await asyncio.sleep(interval)

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

        usdt_balance = await loop.run_in_executor(None, exchange.get_usdt_balance)

        prices = {}
        for sym in self.config.strategy.trading_pairs:
            sym = sym.strip()
            p = await loop.run_in_executor(None, exchange.get_current_price, sym)
            prices[sym] = round(p, 2)

        positions = {sym: pos.to_dict() for sym, pos in risk.open_positions.items()}

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
        try:
            status = await self._get_current_status()
            return web.json_response(status)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_run_cycle(self, request):
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self.bot.run_once)
            status = await self._get_current_status()
            return web.json_response(status)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    def run(self):
        print(f"\n🚀 Trading Bot Web Dashboard launching on port {self.port}...")
        web.run_app(self.app, host="0.0.0.0", port=self.port)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Trading Bot Web Dashboard")
    parser.add_argument("--port", type=int, default=None, help="Port to listen on (default: $PORT or 5000)")
    args = parser.parse_args()

    dashboard = WebDashboard(port=args.port)
    dashboard.run()
