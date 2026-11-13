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
import time
import asyncio
import logging
import base64
import hmac
from datetime import datetime, timedelta, timezone
from pathlib import Path
from aiohttp import web

from config import BotConfig
from exchange import AccountAccessError, ExchangeConnector
from strategies import get_strategy
from risk_manager import RiskManager
from bot_engine import TradingBot

logger = logging.getLogger("bot.dashboard")
_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_WINDOW_SECONDS = 900
_LOGIN_MAX_FAILURES = 5


LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Operator Login · Trading Lab</title>
  <link rel="stylesheet" href="/assets/app.css?v=m8">
  <script src="/assets/app.js?v=m8" defer></script>
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
  <div class="card login-card" style="padding: 28px;">
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
        return False
    cookie_token = request.cookies.get("dashboard_token", "")
    if cookie_token and hmac.compare_digest(cookie_token, token):
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


def _is_same_origin(request) -> bool:
    origin = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
    request_origin = f"{forwarded_proto or request.scheme}://{request.host}".rstrip("/")
    if origin:
        return hmac.compare_digest(origin.rstrip("/"), request_origin)
    if referer:
        return referer.rstrip("/").startswith(request_origin + "/") or hmac.compare_digest(referer.rstrip("/"), request_origin)
    return False


@web.middleware
async def dashboard_auth(request, handler):
    """Protect all dashboard data and controls with flexible token authentication."""
    token = os.getenv("DASHBOARD_TOKEN", "")
    path = request.path
    if (path.startswith("/assets/") or path == "/terms" or
            path == "/login" or path == "/api/login" or path == "/logout" or
            path.startswith("/health/")):
        return await handler(request)

    if not token:
        return web.json_response({"error": "Dashboard access is disabled: DASHBOARD_TOKEN is not configured"}, status=503)

    authenticated = verify_token(request, token)

    if not authenticated:
        if path.startswith("/api/"):
            return web.json_response({"error": "Unauthorized: valid DASHBOARD_TOKEN required"}, status=401)
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="Trading Bot", charset="UTF-8"'})
        return web.Response(
            text=LOGIN_HTML,
            content_type="text/html",
            status=200,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            }
        )

    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.cookies.get("dashboard_token") and not _is_same_origin(request):
        return web.json_response({"error": "Same-origin request required"}, status=403)
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:; connect-src 'self';"
    return response


class WebDashboard:
    def __init__(self, port: int = None):
        self.port = port or int(os.getenv("PORT", 5000))
        self.config = BotConfig()
        self.bot = TradingBot(self.config)
        self._status_cache = None
        self._status_cache_time = 0.0
        self._status_cache_ttl = 5.0
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
        self.app.router.add_get('/logs', self.handle_logs)
        self.app.router.add_get('/strategy', self.handle_strategy)
        self.app.router.add_get('/terms', self.handle_terms)
        self.app.router.add_get('/assets/{name}', self.handle_asset)
        self.app.router.add_get('/health/liveness', self.handle_liveness)
        self.app.router.add_get('/health/readiness', self.handle_readiness)
        self.app.router.add_post('/api/login', self.handle_login)
        self.app.router.add_get('/logout', self.handle_logout)
        self.app.router.add_get('/api/status', self.handle_status)
        self.app.router.add_get('/api/logs', self.handle_logs_api)
        self.app.router.add_get('/api/candles', self.handle_candles)
        self.app.router.add_post('/api/run-cycle', self.handle_run_cycle)
        self.app.router.add_post('/api/exit-only', self.handle_exit_only)
        self.app.router.add_post('/api/resume', self.handle_resume_entries)
        self.app.router.add_post('/api/emergency-stop', self.handle_emergency_stop)
        self.app.router.add_post('/api/testnet-smoke-trade', self.handle_testnet_smoke_trade)
        self.app.router.add_post('/api/test-notification', self.handle_test_notification)
        self.app.router.add_get('/api/account-diagnostic', self.handle_account_diagnostic)

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

    async def handle_logs(self, request):
        return await self._serve_page("logs.html")

    async def handle_strategy(self, request):
        return await self._serve_page("strategy.html")

    async def _serve_page(self, filename):
        html_path = Path(__file__).parent / "templates" / filename
        resp = web.FileResponse(html_path)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    async def handle_asset(self, request):
        # Only serve explicitly supported shared assets; never accept arbitrary paths.
        assets = {"app.css": "app.css", "app.js": "app.js"}
        asset_path = assets.get(request.match_info.get("name"))
        if asset_path is None:
            raise web.HTTPNotFound()
        resp = web.FileResponse(Path(__file__).parent / "templates" / asset_path)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    async def handle_liveness(self, request):
        """Unauthenticated liveness probe (F-16). Reports if web event loop is alive."""
        return web.json_response({
            "status": "alive",
            "service": "trading-bot",
            "timestamp": int(time.time() * 1000)
        })

    async def handle_readiness(self, request):
        """Readiness probe reporting operational bot status without exposing secrets (F-16)."""
        risk = self.bot.risk_manager
        exchange = self.bot.exchange
        is_reconciled = self.bot._last_reconciliation_ok
        is_halted = risk._is_halted
        is_fallback_sim = getattr(exchange, "is_fallback_simulated", False)
        mode = risk.entry_mode()

        ready = (is_reconciled or is_fallback_sim) and not is_halted
        status_code = 200 if ready else 503
        return web.json_response({
            "status": "ready" if ready else "not_ready",
            "halted": is_halted,
            "entry_mode": mode,
            "reconciled": is_reconciled,
            "fallback_simulated": is_fallback_sim,
            "timestamp": int(time.time() * 1000)
        }, status=status_code)

    def _setup_lifecycle(self):
        self.app.on_startup.append(self.start_background_tasks)
        self.app.on_cleanup.append(self.cleanup_background_tasks)

    async def start_background_tasks(self, app):
        """Start 24/7 continuous trading loop in the background."""
        try:
            self.bot.risk_manager.store.append_event("worker_started", {})
        except Exception as exc:
            logger.warning("Could not record worker start event: %s", type(exc).__name__)
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
                # Exchange/network exceptions may contain signed URLs or API
                # metadata. Log the class only; endpoint handlers expose safe
                # actionable messages where appropriate.
                logger.error("Error in continuous background loop (%s)", type(e).__name__)

            await asyncio.sleep(interval)

    @staticmethod
    def _is_valid_origin(request) -> bool:
        return _is_same_origin(request)

    @staticmethod
    def _secure_cookie(request) -> bool:
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
        return forwarded_proto == "https" or request.scheme == "https"

    async def handle_login(self, request):
        token = os.getenv("DASHBOARD_TOKEN", "")
        if not token:
            raise web.HTTPServiceUnavailable(text="Dashboard access is disabled: DASHBOARD_TOKEN is not configured")
        now = time.monotonic()
        ip = request.remote or "unknown"
        failures = [stamp for stamp in _LOGIN_FAILURES.get(ip, []) if now - stamp < _LOGIN_WINDOW_SECONDS]
        _LOGIN_FAILURES[ip] = failures
        if len(failures) >= _LOGIN_MAX_FAILURES:
            raise web.HTTPTooManyRequests(text="Too many login attempts; try again later")
        data = await request.post()
        submitted = data.get("token", "").strip()
        if token and hmac.compare_digest(submitted, token):
            _LOGIN_FAILURES.pop(ip, None)
            resp = web.HTTPFound("/")
            resp.set_cookie("dashboard_token", token, max_age=12 * 3600, path="/", httponly=True, secure=self._secure_cookie(request), samesite="Strict")
            return resp
        failures.append(now)
        _LOGIN_FAILURES[ip] = failures
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
        return web.Response(
            text=content,
            content_type="text/html",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            }
        )

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
            if not self.bot._last_reconciliation_ok:
                account_issue = (
                    f"{self.config.exchange.paper_environment.title()} keys were rejected by Binance. "
                    "The bot is using a simulated wallet with public Binance prices."
                )
        elif not self.bot._last_reconciliation_ok and self.bot._last_account_error:
            account_issue = self.bot._last_account_error

        # 2. Fetch live prices
        prices = {}
        price_failures = []
        for sym in self.config.strategy.trading_pairs:
            sym = sym.strip()
            p = await loop.run_in_executor(None, exchange.get_current_price, sym)
            if p is not None and p > 0:
                prices[sym] = round(p, 2)
            else:
                prices[sym] = None
                price_failures.append(sym)

        # 3. Read open positions & calculate live unrealized profit/loss
        positions = {}
        unrealized_total = 0.0
        has_unrealized_error = False
        for sym, pos in risk.open_positions.items():
            cur_p = prices.get(sym)
            if cur_p is None:
                has_unrealized_error = True
                pos_dict = pos.to_dict()
                pos_dict["current_price"] = None
                pos_dict["unrealized_pnl"] = None
                pos_dict["unrealized_pnl_pct"] = None
                pos_dict["max_loss_dollars"] = round(abs(pos.entry_price - pos.stop_loss_price) * pos.amount, 2)
                pos_dict["target_gain_dollars"] = (
                    round((pos.take_profit_price - pos.entry_price) * pos.amount, 2)
                    if pos.take_profit_price is not None else None
                )
                positions[sym] = pos_dict
            else:
                pos_pnl = (cur_p - pos.entry_price) * pos.amount
                pos_pnl_pct = ((cur_p - pos.entry_price) / pos.entry_price) * 100 if pos.entry_price > 0 else 0
                unrealized_total += pos_pnl

                pos_dict = pos.to_dict()
                pos_dict["current_price"] = cur_p
                pos_dict["unrealized_pnl"] = round(pos_pnl, 2)
                pos_dict["unrealized_pnl_pct"] = round(pos_pnl_pct, 2)
                pos_dict["max_loss_dollars"] = round(abs(pos.entry_price - pos.stop_loss_price) * pos.amount, 2)
                pos_dict["target_gain_dollars"] = (
                    round((pos.take_profit_price - pos.entry_price) * pos.amount, 2)
                    if pos.take_profit_price is not None else None
                )
                positions[sym] = pos_dict

        # 4. Total portfolio net worth = Cash + Open Position Values
        # If the exchange balance call failed or mark is missing, report unavailable (F-07, F-08).
        if not account_available or usdt_balance is None or has_unrealized_error:
            total_net_worth = None
        else:
            total_net_worth = usdt_balance + sum(
                p["amount"] * p["current_price"] for p in positions.values() if p.get("current_price") is not None
            )

        # Non-configured holdings on account (F-08)
        configured_bases = {p.split("/")[0].strip() for p in self.config.strategy.trading_pairs}
        unconfigured_holdings = {}
        if isinstance(account, dict):
            for asset, amt in (account.get("total") or {}).items():
                if asset != "USDT" and asset not in configured_bases:
                    try:
                        f_amt = float(amt)
                        if f_amt > 0:
                            unconfigured_holdings[asset] = f_amt
                    except (TypeError, ValueError):
                        pass

        # 5. Read completed trade history from the active durable store.
        trades = []
        list_trades = getattr(risk.store, "list_trades", None)
        if callable(list_trades):
            trades = list_trades(5000)
        elif self.bot.trade_log_path.exists():
            with open(self.bot.trade_log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        trades.append(json.loads(line))

        # 6. Calculate accuracy and performance stats
        realized_pnl = sum(t.get("pnl", 0.0) for t in trades if t.get("action") == "SELL")
        total_gain_loss = None if total_net_worth is None else realized_pnl + unrealized_total
        gain_loss_pct = (
            ((total_gain_loss / self.initial_balance) * 100)
            if (total_gain_loss is not None and self.initial_balance > 0)
            else 0.0
        )

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
            "bot_tracked_value": round(total_net_worth, 2) if total_net_worth is not None else None,
            "unconfigured_holdings": unconfigured_holdings,
            "price_failures": price_failures,
            "cash_balance": round(usdt_balance, 2) if usdt_balance is not None else None,
            "account_available": account_available,
            "account_issue": account_issue,
            "initial_balance": self.initial_balance,
            "total_gain_loss": round(total_gain_loss, 2) if total_gain_loss is not None else None,
            "total_gain_loss_pct": round(gain_loss_pct, 2) if gain_loss_pct is not None else None,
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_total, 2) if not has_unrealized_error else None,
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
            "take_profit_managed_by_strategy": self.config.strategy.name in {"macro_donchian_ema72", "dynamic_regime"},
            "strategy_exit_description": (
                "Dynamic paper route: EMA72 candidate exits on a completed 1-hour close below EMA72; 100×ATR target is a distant backstop"
                if self.config.strategy.name == "dynamic_regime" else
                "Completed 1-hour close below EMA72; 100×ATR target is a distant backstop"
                if self.config.strategy.name == "macro_donchian_ema72" else "Fixed take-profit"
            ),
            "regime_filter_enabled": self.config.strategy.regime_filter_enabled,
            "min_trend_adx": self.config.strategy.min_trend_adx,
            "max_atr_pct": self.config.strategy.max_atr_pct,
            "halted": risk._is_halted,
            "halt_reason": risk._halt_reason,
            "entry_mode": risk.entry_mode(),
            "exchange_reconciled": self.bot._last_reconciliation_ok,
            "resume_available": risk.entry_mode() == "EXIT_ONLY" and not risk._is_halted and account_available and self.bot._last_reconciliation_ok,
            "manual_resume_available": (
                risk._is_halted and risk._halt_reason.startswith((
                    "Emergency stop",
                    "Consecutive loss limit reached",
                    "Consecutive loss limit persists",
                )) and not positions and
                not risk.store.unresolved_orders() and account_available and self.bot._last_reconciliation_ok
            ),
            "testnet_smoke_attempted": risk.store.read_state("testnet_smoke_trade") is not None,
            "prices": prices,
            "market_summary": market_summary,
            "strategy_name": self.config.strategy.name.upper(),
            "timeframe": self.config.strategy.timeframe,
            "trades": trades,
            "notifications": {
                "enabled": self.config.enable_notifications,
                "telegram": bool(self.config.telegram_bot_token and self.config.telegram_chat_id),
                "email": bool(self.config.smtp_host and self.config.alert_email),
                "discord": bool(self.config.discord_webhook_url),
            },
        }

    async def handle_status(self, request):
        now = time.time()
        if self._status_cache is not None and (now - self._status_cache_time) < self._status_cache_ttl:
            return web.json_response(
                self._status_cache,
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                    "Pragma": "no-cache",
                    "Expires": "0",
                }
            )
        try:
            status = await self._get_current_status()
            self._status_cache = status
            self._status_cache_time = now
            return web.json_response(
                status,
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                    "Pragma": "no-cache",
                    "Expires": "0",
                }
            )
        except Exception:
            logger.exception("Dashboard status request failed")
            return web.json_response({"error": "Status is temporarily unavailable."}, status=500)

    @staticmethod
    def _safe_log_details(event_type, details):
        """Allowlist only display-safe fields; never expose raw order responses or reset snapshots."""
        details = details if isinstance(details, dict) else {}
        if event_type == "market_check":
            indicators = details.get("indicators", {})
            indicators = indicators if isinstance(indicators, dict) else {}
            votes = details.get("votes", {})
            safe_votes = {}
            if isinstance(votes, dict):
                for name, vote in votes.items():
                    if isinstance(vote, dict):
                        safe_votes[str(name)[:30]] = {
                            "signal": str(vote.get("signal", "HOLD"))[:12],
                            "reason": str(vote.get("reason", ""))[:240],
                        }
            safe_indicators = {}
            for key in (
                "market_regime", "adx", "atr_pct", "volume", "vol_avg20", "ema_trend",
                "consensus_required", "entry_gates", "close", "ema480", "ema72",
                "donchian_high168", "entry_ready", "exit_ready",
            ):
                value = indicators.get(key)
                if value is None or isinstance(value, (str, int, float, bool, dict)):
                    safe_indicators[key] = value
            if indicators.get("strategy") is not None:
                safe_indicators["strategy"] = str(indicators["strategy"])[:48]
            return {
                "symbol": str(details.get("symbol", ""))[:32],
                "timeframe": str(details.get("timeframe", ""))[:12],
                "candle_timestamp": details.get("candle_timestamp"),
                "signal": str(details.get("signal", "HOLD"))[:12],
                "reason": str(details.get("reason", ""))[:500],
                "confidence": details.get("confidence"),
                "measured_spread_bps": details.get("measured_spread_bps"),
                "top_bid": details.get("top_bid"),
                "top_ask": details.get("top_ask"),
                "spread_sample_status": str(details.get("spread_sample_status", "unknown"))[:32],
                "votes": safe_votes,
                "indicators": safe_indicators,
            }
        if event_type == "order_status":
            return {
                key: str(details[key])[:80]
                for key in ("symbol", "side", "amount", "state", "filled", "average")
                if details.get(key) is not None
            }
        if event_type in {"analysis_error", "market_data_issue"}:
            safe = {
                "symbol": str(details.get("symbol", ""))[:32],
                "issue": str(details.get("issue", ""))[:80],
                "error_type": str(details.get("error_type", ""))[:80],
                "available_candles": details.get("available_candles"),
            }
            for key in ("expected_timestamp", "actual_timestamp", "step_ms", "missing_intervals"):
                value = details.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    safe[key] = value
            return safe
        if event_type == "risk_halt":
            return {"reason": str(details.get("reason", "Trading halted"))[:500]}
        if event_type == "risk_equity_basis_migrated":
            cleared = details.get("legacy_equity_halt_cleared") is True
            return {
                "managed_equity": details.get("managed_equity"),
                "legacy_equity_halt_cleared": cleared,
                "message": (
                    "Risk baseline now measures USDT cash and bot-tracked positions; the obsolete seed-balance drawdown halt was cleared after clean account reconciliation."
                    if cleared else
                    "Risk baseline now measures USDT cash and bot-tracked positions; an existing safety halt remains active for review."
                ),
            }
        if event_type == "worker_started":
            return {"status": "Background worker started"}
        if event_type == "mode_changed":
            return {
                "mode": str(details.get("mode", ""))[:24],
                "source": str(details.get("source", ""))[:40],
            }
        if event_type in {"protective_stop_placed", "protective_stop_filled", "protective_stop_cancelled", "partial_entry_fill", "partial_exit_reconciled"}:
            allowed = ("symbol", "amount", "filled", "remaining", "stop_price", "average")
            return {key: str(details[key])[:80] for key in allowed if details.get(key) is not None}
        if event_type == "reconciliation_failed":
            return {"message": "Account reconciliation failed; new entries remained blocked."}
        if event_type == "emergency_stop_review_acknowledged":
            return {"message": "Emergency-stop review acknowledged."}
        if event_type == "testnet_smoke_trade_completed":
            recovered = details.get("recovered_from_saved_result") is True
            return {
                "symbol": str(details.get("symbol", ""))[:32],
                "amount": str(details.get("amount", ""))[:80],
                "buy_price": str(details.get("buy_price", ""))[:80],
                "sell_price": str(details.get("sell_price", ""))[:80],
                "pnl_before_fees": str(details.get("pnl_before_fees", ""))[:80],
                "message": (
                    "Recovered from the saved completed Testnet check; excluded from strategy performance."
                    if recovered else
                    "Testnet order-path check completed; excluded from strategy performance."
                ),
            }
        if event_type == "testnet_reset_recovery":
            return {"message": "Testnet state reset was recorded; review the operator recovery notes."}
        return {}

    @staticmethod
    def _summarize_market_activity(market_events, audit_events, *, now=None):
        """Summarize the last 24 hours of strategy checks without implying orders."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        cutoff = now - timedelta(hours=24)

        def event_time(item):
            try:
                value = str(item.get("created_at", "")).replace("Z", "+00:00")
                occurred_at = datetime.fromisoformat(value)
                if occurred_at.tzinfo is None:
                    occurred_at = occurred_at.replace(tzinfo=timezone.utc)
                return occurred_at.astimezone(timezone.utc)
            except (OverflowError, TypeError, ValueError):
                return None

        recent_market = [
            event for event in market_events
            if (occurred_at := event_time(event)) is not None and occurred_at >= cutoff
        ]
        recent_audit = [
            event for event in audit_events
            if (occurred_at := event_time(event)) is not None and occurred_at >= cutoff
        ]
        signal_counts = {name: 0 for name in ("BUY", "SELL", "HOLD", "OTHER")}
        vote_counts = {name: {vote: 0 for vote in ("BUY", "SELL", "HOLD")} for name in ("ema", "rsi", "macd")}
        blocked_by_gate = {}
        blocked_checks = 0
        no_consensus = 0
        all_votes_hold = 0

        for event in recent_market:
            details = event.get("details") or {}
            signal = str(details.get("signal", "HOLD")).upper()
            signal_counts[signal if signal in signal_counts else "OTHER"] += 1
            reason = str(details.get("reason", "")).lower()
            no_consensus += reason.startswith("no consensus")
            votes = details.get("votes") if isinstance(details.get("votes"), dict) else {}
            normalized_votes = []
            for indicator in vote_counts:
                vote = votes.get(indicator, {})
                vote = str(vote.get("signal", "HOLD")).upper() if isinstance(vote, dict) else "HOLD"
                if vote not in vote_counts[indicator]:
                    vote = "HOLD"
                vote_counts[indicator][vote] += 1
                normalized_votes.append(vote)
            all_votes_hold += bool(normalized_votes) and all(vote == "HOLD" for vote in normalized_votes)

            gates = (details.get("indicators") or {}).get("entry_gates", {})
            failed_gates = []
            if isinstance(gates, dict):
                failed_gates = [
                    name for name, gate in gates.items()
                    if isinstance(gate, dict)
                    and gate.get("enabled") is True
                    and gate.get("relevant_to_current_buy") is True
                    and gate.get("passed") is False
                ]
            if failed_gates:
                blocked_checks += 1
                for name in failed_gates:
                    blocked_by_gate[name] = blocked_by_gate.get(name, 0) + 1

        return {
            "window_hours": 24,
            "checks": len(recent_market),
            "signals": signal_counts,
            "votes": vote_counts,
            "no_consensus": no_consensus,
            "all_votes_hold": all_votes_hold,
            "buy_setups_blocked": blocked_checks,
            "blocked_by_gate": blocked_by_gate,
            "analysis_issues": sum(event.get("event_type") in {"analysis_error", "market_data_issue"} for event in recent_audit),
            "reconciliation_failures": sum(event.get("event_type") == "reconciliation_failed" for event in recent_audit),
        }

    async def handle_logs_api(self, request):
        try:
            try:
                limit = int(request.query.get("limit", "100"))
            except (TypeError, ValueError):
                limit = 100
            limit = max(1, min(limit, 100))
            store = self.bot.risk_manager.store
            summary_market_events = store.list_events(200, event_type="market_check")
            summary_audit_events = store.list_events(200, exclude_event_type="market_check")
            market_events = summary_market_events[:limit]
            audit_events = summary_audit_events[:limit]
            market_summary = self._summarize_market_activity(summary_market_events, summary_audit_events)

            def event_order(item):
                try:
                    value = str(item.get("created_at", "")).replace("Z", "+00:00")
                    occurred_at = datetime.fromisoformat(value)
                    if occurred_at.tzinfo is None:
                        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
                    return occurred_at.timestamp()
                except (OverflowError, TypeError, ValueError):
                    return 0.0

            events = sorted(market_events + audit_events, key=event_order, reverse=True)
            payload = [{
                "id": item["id"],
                "type": item["event_type"],
                "created_at": item["created_at"],
                "details": self._safe_log_details(item["event_type"], item["details"]),
            } for item in events[:limit * 2]]
            return web.json_response({"events": payload, "limit": limit, "market_summary": market_summary})
        except Exception:
            logger.exception("Dashboard activity log request failed")
            return web.json_response({"error": "Activity log is temporarily unavailable."}, status=500)

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
        self._status_cache = None
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
        self._status_cache = None
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
        self._status_cache = None
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
        self._status_cache = None
        loop = asyncio.get_event_loop()
        success, message = await loop.run_in_executor(None, self.bot.emergency_stop)
        status = 200 if success else 503
        return web.json_response({"success": success, "message": message}, status=status)

    async def handle_testnet_smoke_trade(self, request):
        if not self._is_valid_origin(request):
            return web.json_response({"error": "Same-origin request required."}, status=403)
        if not self.config.exchange.paper_trading or self.config.exchange.paper_environment != "testnet":
            return web.json_response({"error": "This one-time smoke trade is available only on Binance Spot Testnet."}, status=403)
        self._status_cache = None
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, self.bot.run_testnet_smoke_trade)
            return web.json_response(result)
        except Exception as exc:
            logger.error("Testnet smoke trade did not complete (%s)", type(exc).__name__)
            return web.json_response({"error": f"Smoke trade did not complete ({type(exc).__name__}); inspect the service logs."}, status=409)

    async def handle_test_notification(self, request):
        if not self._is_valid_origin(request):
            return web.json_response({"error": "Same-origin request required."}, status=403)

        channels = []
        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            channels.append("Telegram")
        if self.config.smtp_host and self.config.alert_email:
            channels.append("Email/SMS")
        if self.config.discord_webhook_url:
            channels.append("Discord")

        if not channels:
            return web.json_response({
                "success": False,
                "message": "No notification channels configured. Add Telegram, SMTP/Email, or Discord settings in Railway variables."
            }, status=400)

        loop = asyncio.get_event_loop()
        test_msg = (
            "🔔 *Trading Bot Alert Test*\n"
            "This is a test notification from your Trading Lab surveillance dashboard.\n"
            f"Active channels: {', '.join(channels)}"
        )
        try:
            await loop.run_in_executor(
                None,
                lambda: self.bot.send_notification(test_msg, subject="🔔 Trading Bot Alert Test")
            )
            return web.json_response({
                "success": True,
                "message": f"Test alert dispatched to: {', '.join(channels)}",
                "channels": channels
            })
        except Exception as exc:
            return web.json_response({"success": False, "message": str(exc)}, status=500)

    async def handle_account_diagnostic(self, request):
        loop = asyncio.get_event_loop()
        diag = await loop.run_in_executor(None, self.bot.exchange.diagnose_account_access)
        return web.json_response(diag)

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
