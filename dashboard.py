"""
Web-Based Surveillance & Control Dashboard for the Automated Trading Bot.

Enhanced with:
  - Beginner-Friendly metrics (Total Net Worth, All-Time Profit/Loss in $, Accuracy %)
  - Plain-English status and safety descriptions
  - 24/7 background worker
  - Real-time updates with live market tickers
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
        self.port = port or int(os.getenv("PORT", 5000))
        self.config = BotConfig()
        self.bot = TradingBot(self.config)
        self.initial_balance = float(os.getenv("INITIAL_BALANCE", "10000.0"))
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
                await loop.run_in_executor(None, self.bot.check_stop_loss_take_profit)
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
        """Fetch real-time bot and market status tailored for beginners."""
        loop = asyncio.get_event_loop()
        exchange = self.bot.exchange
        risk = self.bot.risk_manager

        # 1. Fetch available cash
        usdt_balance = await loop.run_in_executor(None, exchange.get_usdt_balance)
        if usdt_balance <= 0 and self.config.exchange.paper_trading:
            usdt_balance = 10000.0

        # 2. Fetch live prices
        prices = {}
        for sym in self.config.strategy.trading_pairs:
            sym = sym.strip()
            p = await loop.run_in_executor(None, exchange.get_current_price, sym)
            prices[sym] = round(p, 2)

        # 3. Read open positions & calculate live unrealized profit/loss
        positions = {}
        unrealized_total = 0.0
        for sym, pos in risk.open_positions.items():
            cur_p = prices.get(sym, pos.entry_price)
            pos_pnl = (cur_p - pos.entry_price) * pos.amount
            pos_pnl_pct = ((cur_p - pos.entry_price) / pos.entry_price) * 100 if pos.entry_price > 0 else 0
            unrealized_total += pos_pnl

            pos_dict = pos.to_dict()
            pos_dict["current_price"] = cur_p
            pos_dict["unrealized_pnl"] = round(pos_pnl, 2)
            pos_dict["unrealized_pnl_pct"] = round(pos_pnl_pct, 2)
            pos_dict["max_loss_dollars"] = round(abs(pos.entry_price - pos.stop_loss_price) * pos.amount, 2)
            pos_dict["target_gain_dollars"] = round((pos.take_profit_price - pos.entry_price) * pos.amount, 2)
            positions[sym] = pos_dict

        # 4. Total portfolio net worth = Cash + Open Position Values
        total_net_worth = usdt_balance + sum(p["amount"] * p["current_price"] for p in positions.values())
        if not positions:
            total_net_worth = usdt_balance

        # 5. Read completed trade history
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

        # 6. Calculate accuracy and performance stats
        realized_pnl = sum(t.get("pnl", 0.0) for t in trades if t.get("action") == "SELL")
        total_gain_loss = realized_pnl + unrealized_total
        gain_loss_pct = (total_gain_loss / self.initial_balance) * 100 if self.initial_balance > 0 else 0.0

        completed_sells = [t for t in trades if t.get("action") == "SELL"]
        winners = [t for t in completed_sells if t.get("pnl", 0) > 0]
        losers = [t for t in completed_sells if t.get("pnl", 0) <= 0]
        accuracy_pct = (len(winners) / len(completed_sells) * 100) if completed_sells else 0.0

        # Beginner explanation summary
        if len(positions) == 0:
            market_summary = "All funds safe in cash. The bot is actively watching Bitcoin, Ethereum, and Solana for a safe, low-risk entry."
        else:
            market_summary = f"The bot has {len(positions)} active safe trade(s) with protective stop-loss shields enabled."

        return {
            "paper_trading": self.config.exchange.paper_trading,
            "exchange": self.config.exchange.name,
            "net_worth": round(total_net_worth, 2),
            "cash_balance": round(usdt_balance, 2),
            "initial_balance": self.initial_balance,
            "total_gain_loss": round(total_gain_loss, 2),
            "total_gain_loss_pct": round(gain_loss_pct, 2),
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_total, 2),
            "daily_pnl": round(risk._daily_loss, 2),
            "accuracy_pct": round(accuracy_pct, 1),
            "total_trades": len(completed_sells),
            "winners_count": len(winners),
            "losers_count": len(losers),
            "positions": positions,
            "positions_count": len(positions),
            "max_positions": self.config.risk.max_open_positions,
            "prices": prices,
            "market_summary": market_summary,
            "strategy_name": self.config.strategy.name.upper(),
            "timeframe": self.config.strategy.timeframe,
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
