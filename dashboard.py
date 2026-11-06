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
import base64
import hmac
from pathlib import Path
from aiohttp import web

from config import BotConfig
from exchange import AccountAccessError, ExchangeConnector
from strategies import get_strategy
from risk_manager import RiskManager
from bot_engine import TradingBot

logger = logging.getLogger("bot.dashboard")


LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Operator Login · Trading Lab</title>
  <link rel="stylesheet" href="/assets/app.css">
  <script src="/assets/app.js" defer></script>
</head>
<body>
<nav>
  <div class="nav-inner">
    <a class="brand" href="/">TRADING LAB</a>
    <div class="nav-links">
      <a class="nav-link" href="/terms">Glossary</a>
    </div>
    <button class="theme-toggle" type="button" data-theme-toggle aria-label="Switch theme">
      <span aria-hidden="true">◐</span><span data-theme-label>Dark mode</span>
    </button>
  </div>
</nav>
<main class="container" style="max-width: 480px; margin-top: 40px;">
  <div class="card" style="padding: 28px;">
    <div class="eyebrow" style="margin-bottom: 6px;">Secure Access</div>
    <h1 style="font-size: 24px; margin-bottom: 12px;">Sign in to Trading Lab</h1>
    <p class="intro" style="font-size: 13px; margin-bottom: 20px;">
      Enter your secret operator access token (<code>DASHBOARD_TOKEN</code>) to view the surveillance dashboard and manage trading controls.
    </p>
    <form method="POST" action="/api/login" id="login-form">
      <div style="margin-bottom: 16px;">
        <label for="token-input" style="display: block; font-size: 12px; color: var(--muted); margin-bottom: 6px; font-weight: 600;">ACCESS TOKEN</label>
        <input type="password" id="token-input" name="token" required autofocus autocomplete="current-password" placeholder="Enter DASHBOARD_TOKEN" style="width: 100%; box-sizing: border-box; padding: 10px 12px; font-size: 14px; background: var(--soft); border: 1px solid var(--line); border-radius: 6px; color: var(--ink);">
      </div>
      <button type="submit" class="button" style="width: 100%; justify-content: center; padding: 11px;">Enter Dashboard</button>
      <div id="login-error" class="help error-text" style="margin-top: 10px; text-align: center;"></div>
    </form>
    <div style="margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--line); font-size: 12px; color: var(--muted); text-align: center;">
      New to trading? <a href="/terms" style="text-decoration: underline;">Read the beginner's glossary</a> without logging in.
    </div>
  </div>
</main>
<script>
const params = new URLSearchParams(window.location.search);
if (params.get('error')) {
  document.getElementById('login-error').textContent = 'Invalid access token. Please check your Railway environment variables.';
}
</script>
</body>
</html>
"""


def verify_token(request, token: str) -> bool:
    if not token:
        return True
    cookie_token = request.cookies.get("dashboard_token", "")
    if cookie_token and hmac.compare_digest(cookie_token, token):
        return True
    query_token = request.query.get("token", "")
    if query_token and hmac.compare_digest(query_token, token):
        return True
    header_token = request.headers.get("X-Dashboard-Token", "")
    if header_token and hmac.compare_digest(header_token, token):
        return True
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        bearer = auth_header[7:].strip()
        if bearer and hmac.compare_digest(bearer, token):
            return True
    if auth_header.startswith("Basic "):
        try:
            value = auth_header[6:].strip()
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
            user, password = decoded.split(":", 1) if ":" in decoded else ("", decoded)
            if hmac.compare_digest(password, token):
                return True
        except Exception:
            pass
    return False


@web.middleware
async def dashboard_auth(request, handler):
    """Protect all dashboard data and controls with flexible token authentication."""
    token = os.getenv("DASHBOARD_TOKEN", "")
    path = request.path
    if (path.startswith("/assets/") or path == "/terms" or
            path == "/login" or path == "/api/login" or path == "/logout"):
        return await handler(request)

    authenticated = verify_token(request, token)
    query_token = request.query.get("token", "")
    used_query_token = query_token and token and hmac.compare_digest(query_token, token)

    if not authenticated:
        if path.startswith("/api/"):
            return web.json_response({"error": "Unauthorized: valid DASHBOARD_TOKEN required"}, status=401)
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="Trading Bot", charset="UTF-8"'})
        return web.Response(text=LOGIN_HTML, content_type="text/html", status=200)

    response = await handler(request)
    if used_query_token:
        response.set_cookie("dashboard_token", token, max_age=30 * 86400, path="/", httponly=True, samesite="Lax")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


class WebDashboard:
    def __init__(self, port: int = None):
        self.port = port or int(os.getenv("PORT", 5000))
        self.config = BotConfig()
        self.bot = TradingBot(self.config)
        try:
            self.initial_balance = float(os.getenv("INITIAL_BALANCE", "10000.0"))
        except ValueError:
            self.initial_balance = 0.0
        if not (self.initial_balance > 0 and self.initial_balance < float("inf")):
            raise ValueError("INITIAL_BALANCE must be a finite positive number")
        self.app = web.Application(middlewares=[dashboard_auth])
        self.app["bot"] = self.bot
        self.app["config"] = self.config
        self.bg_task = None
        self._setup_routes()
        self._setup_lifecycle()

    def _setup_routes(self):
        self.app.router.add_get('/', self.handle_index)
        self.app.router.add_get('/positions', self.handle_positions)
        self.app.router.add_get('/history', self.handle_history)
        self.app.router.add_get('/strategy', self.handle_strategy)
        self.app.router.add_get('/terms', self.handle_terms)
        self.app.router.add_get('/assets/{name}', self.handle_asset)
        self.app.router.add_post('/api/login', self.handle_login)
        self.app.router.add_get('/logout', self.handle_logout)
        self.app.router.add_get('/api/status', self.handle_status)
        self.app.router.add_get('/api/candles', self.handle_candles)
        self.app.router.add_post('/api/run-cycle', self.handle_run_cycle)
        self.app.router.add_post('/api/exit-only', self.handle_exit_only)
        self.app.router.add_post('/api/resume', self.handle_resume_entries)
        self.app.router.add_post('/api/emergency-stop', self.handle_emergency_stop)
        self.app.router.add_post('/api/testnet-smoke-trade', self.handle_testnet_smoke_trade)

    async def handle_terms(self, request):
        """Serve the terms and glossary page."""
        html_path = Path(__file__).parent / "templates" / "glossary.html"
        if not html_path.exists():
            return web.Response(text="<h1>Terms template not found</h1>", content_type="text/html")
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()
        return web.Response(text=content, content_type="text/html")

    async def handle_positions(self, request):
        return await self._serve_page("positions.html")

    async def handle_history(self, request):
        return await self._serve_page("history.html")

    async def handle_strategy(self, request):
        return await self._serve_page("strategy.html")

    async def _serve_page(self, filename):
        html_path = Path(__file__).parent / "templates" / filename
        return web.FileResponse(html_path)

    async def handle_asset(self, request):
        # Only serve explicitly supported shared assets; never accept arbitrary paths.
        assets = {"app.css": "app.css", "app.js": "app.js"}
        asset_path = assets.get(request.match_info.get("name"))
        if asset_path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(Path(__file__).parent / "templates" / asset_path)

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
        try:
            interval = int(os.getenv("INTERVAL_SECONDS", "60"))
        except ValueError:
            interval = 60
        interval = min(max(interval, 1), 3600)
        logger.info("Background trading loop active (interval: %ss)", interval)

        while True:
            try:
                config_errors = self.config.validate()
                if config_errors:
                    logger.error("Trading cycle skipped because configuration is invalid: %s", "; ".join(config_errors))
                    await asyncio.sleep(interval)
                    continue

                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self.bot.run_cycle)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in continuous background loop: {e}")

            await asyncio.sleep(interval)

    @staticmethod
    def _is_valid_origin(request) -> bool:
        origin = request.headers.get("Origin", "")
        referer = request.headers.get("Referer", "")
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
        request_origin = f"{forwarded_proto or request.scheme}://{request.host}".rstrip("/")
        if origin and hmac.compare_digest(origin.rstrip("/"), request_origin):
            return True
        if referer and referer.rstrip("/").startswith(request_origin):
            return True
        token = os.getenv("DASHBOARD_TOKEN", "")
        if verify_token(request, token):
            return True
        return False

    async def handle_login(self, request):
        token = os.getenv("DASHBOARD_TOKEN", "")
        data = await request.post()
        submitted = data.get("token", "").strip()
        if token and hmac.compare_digest(submitted, token):
            resp = web.HTTPFound("/")
            resp.set_cookie("dashboard_token", token, max_age=30 * 86400, path="/", httponly=True, samesite="Lax")
            return resp
        return web.HTTPFound("/?error=1")

    async def handle_logout(self, request):
        resp = web.HTTPFound("/")
        resp.del_cookie("dashboard_token", path="/")
        return resp

    async def handle_index(self, request):
        """Serve the HTML dashboard."""
        html_path = Path(__file__).parent / "templates" / "overview.html"
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

        # 1. Fetch cash independently from open-order permission. This lets
        # the dashboard show a valid balance while still blocking trading if
        # full account reconciliation is unavailable.
        account_issue = ""
        try:
            account = await loop.run_in_executor(None, exchange.fetch_balance_snapshot)
            usdt_balance = float((account.get("free") or {}).get("USDT") or 0.0)
            account_available = True
        except Exception as exc:
            account_issue = str(exc) if isinstance(exc, AccountAccessError) else "Balance unavailable: the private account request failed."
            usdt_balance = None
            account_available = False
        is_fallback_sim = getattr(exchange, "is_fallback_simulated", False)
        if is_fallback_sim:
            account_available = True
            account_issue = (
                "Spot Testnet keys rejected by Binance (-2015). Active in simulated mode with live Binance prices."
            )
        if not self.bot._last_reconciliation_ok and self.bot._last_account_error and not is_fallback_sim:
            account_issue = self.bot._last_account_error

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
        # If the exchange balance call failed, report unavailable instead of inventing equity.
        total_net_worth = None if not account_available else usdt_balance + sum(
            p["amount"] * p["current_price"] for p in positions.values()
        )

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
            market_summary = "No positions are currently tracked. Strategy signals do not establish that an entry is safe or profitable."
        else:
            market_summary = f"The bot is tracking {len(positions)} open position(s). Stops are monitored by this process and are not guaranteed exchange-side protection."

        return {
            "paper_trading": self.config.exchange.paper_trading,
            "paper_environment": "simulated" if is_fallback_sim else self.config.exchange.paper_environment,
            "fallback_simulated": is_fallback_sim,
            "exchange": self.config.exchange.name,
            "net_worth": round(total_net_worth, 2) if total_net_worth is not None else None,
            "cash_balance": round(usdt_balance, 2) if usdt_balance is not None else None,
            "account_available": account_available,
            "account_issue": account_issue,
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
            "latest_signals": self.bot.last_signals,
            "max_positions": self.config.risk.max_open_positions,
            "risk_per_trade_pct": self.config.risk.max_risk_per_trade,
            "position_size_pct": self.config.risk.position_size_pct,
            "daily_loss_limit_pct": self.config.risk.max_daily_loss_pct,
            "stop_loss_pct": self.config.risk.stop_loss_pct,
            "take_profit_pct": self.config.risk.take_profit_pct,
            "regime_filter_enabled": self.config.strategy.regime_filter_enabled,
            "min_trend_adx": self.config.strategy.min_trend_adx,
            "max_atr_pct": self.config.strategy.max_atr_pct,
            "halted": risk._is_halted,
            "halt_reason": risk._halt_reason,
            "entry_mode": risk.entry_mode(),
            "exchange_reconciled": self.bot._last_reconciliation_ok,
            "resume_available": risk.entry_mode() == "EXIT_ONLY" and not risk._is_halted and account_available and self.bot._last_reconciliation_ok,
            "manual_resume_available": (
                risk.entry_mode() == "EXIT_ONLY" and risk._is_halted and
                risk._halt_reason.startswith("Emergency stop") and not positions and
                not risk.store.unresolved_orders() and account_available and self.bot._last_reconciliation_ok
            ),
            "testnet_smoke_attempted": risk.store.read_state("testnet_smoke_trade") is not None,
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
        except Exception:
            logger.exception("Dashboard status request failed")
            return web.json_response({"error": "Status is temporarily unavailable."}, status=500)

    async def handle_candles(self, request):
        """Return recent OHLCV data for the dashboard chart."""
        symbol = request.query.get("symbol", "").strip()
        timeframe = request.query.get("timeframe", self.config.strategy.timeframe)
        allowed_timeframes = {"15m", "1h", "4h", "1d"}
        allowed_symbols = {s.strip() for s in self.config.strategy.trading_pairs}
        if symbol not in allowed_symbols:
            return web.json_response({"error": "Choose a configured trading pair."}, status=400)
        if timeframe not in allowed_timeframes:
            return web.json_response({"error": "Unsupported chart timeframe."}, status=400)
        loop = asyncio.get_event_loop()
        candles = await loop.run_in_executor(
            None, self.bot.exchange.fetch_ohlcv, symbol, timeframe, 100
        )
        if not candles:
            return web.json_response({"error": "Market candles are temporarily unavailable."}, status=502)
        return web.json_response({
            "symbol": symbol,
            "timeframe": timeframe,
            "candles": [
                {"time": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                 "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])}
                for c in candles if len(c) >= 6
            ],
        })

    async def handle_run_cycle(self, request):
        if not self._is_valid_origin(request):
            return web.json_response({"error": "Same-origin request required."}, status=403)
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self.bot.run_once)
            status = await self._get_current_status()
            return web.json_response(status)
        except Exception:
            logger.exception("Manual dashboard cycle failed")
            return web.json_response({"error": "Cycle failed; inspect service logs."}, status=500)

    async def handle_exit_only(self, request):
        if not self._is_valid_origin(request):
            return web.json_response({"error": "Same-origin request required."}, status=403)
        loop = asyncio.get_event_loop()
        success, message = await loop.run_in_executor(None, self.bot.pause_entries)
        return web.json_response({
            "success": success,
            "entry_mode": self.bot.risk_manager.entry_mode(),
            "message": message,
        }, status=200 if success else 503)

    async def handle_resume_entries(self, request):
        if not self._is_valid_origin(request):
            return web.json_response({"error": "Same-origin request required."}, status=403)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        loop = asyncio.get_event_loop()
        manual_review_confirmed = isinstance(payload, dict) and payload.get("manual_review_confirmed") is True
        success, message = await loop.run_in_executor(
            None, self.bot.resume_entries, manual_review_confirmed
        )
        return web.json_response({
            "success": success,
            "entry_mode": self.bot.risk_manager.entry_mode(),
            "message": message,
        }, status=200 if success else 409)

    async def handle_emergency_stop(self, request):
        if not self._is_valid_origin(request):
            return web.json_response({"error": "Same-origin request required."}, status=403)
        loop = asyncio.get_event_loop()
        success, message = await loop.run_in_executor(None, self.bot.emergency_stop)
        status = 200 if success else 503
        return web.json_response({"success": success, "message": message}, status=status)

    async def handle_testnet_smoke_trade(self, request):
        if not self._is_valid_origin(request):
            return web.json_response({"error": "Same-origin request required."}, status=403)
        if not self.config.exchange.paper_trading or self.config.exchange.paper_environment != "testnet":
            return web.json_response({"error": "This one-time smoke trade is available only on Binance Spot Testnet."}, status=403)
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, self.bot.run_testnet_smoke_trade)
            return web.json_response(result)
        except Exception as exc:
            logger.error("Testnet smoke trade did not complete (%s)", type(exc).__name__)
            return web.json_response({"error": f"Smoke trade did not complete ({type(exc).__name__}); inspect the service logs."}, status=409)

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
