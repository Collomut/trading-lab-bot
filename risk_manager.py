"""
Risk Manager — the safety net of the bot.

Upgraded features:
  - Position persistence to disk (data/open_positions.json): survives restarts!
  - Trailing stop-loss: locks in profits as price climbs.
  - Break-even protection: moves stop-loss to entry price once up 1.5%.
  - Volatility-adjusted stop-loss (ATR-aware).
  - Strict daily drawdown circuit breakers.
"""

import csv
import io
import json
import logging
import os
import math
import numpy as np
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from pathlib import Path
from config import RiskConfig
from state_store import StateStore
from postgres_state_store import PostgresStateStore

logger = logging.getLogger("bot.risk")


def calculate_parkinson_volatility(highs, lows, period: int = 14) -> float:
    """Annualized Parkinson high-low realized volatility.

    Uses the last `period` high/low pairs. Returns the 40% benchmark when fewer
    than `period` valid bars are available, matching the sizing default so a
    missing series never inflates or collapses a position.
    """
    if highs is None or lows is None:
        return 0.40
    try:
        h = np.asarray(highs, dtype=float)[-period:]
        l = np.asarray(lows, dtype=float)[-period:]
    except (TypeError, ValueError):
        return 0.40
    valid = np.isfinite(h) & np.isfinite(l) & (h > 0) & (l > 0) & (h >= l)
    h = h[valid]
    l = l[valid]
    if h.shape[0] < period or l.shape[0] < period:
        return 0.40
    log_hl = np.log(h / l)
    parkinson_var = (1.0 / (4.0 * np.log(2.0) * period)) * float(np.sum(log_hl ** 2))
    daily_vol = float(np.sqrt(parkinson_var))
    return float(daily_vol * np.sqrt(365.25))


@dataclass
class Position:
    """Represents an open trading position."""
    symbol: str
    side: str  # "long"
    entry_price: float
    amount: float
    entry_time: str  # ISO 8601 string for easy JSON serialization
    stop_loss_price: float
    take_profit_price: float | None
    order_id: str = ""
    stop_order_id: str = ""
    stop_client_order_id: str = ""
    entry_fee_quote: float = 0.0
    highest_price: float = 0.0  # For trailing stop
    break_even_activated: bool = False
    exit_policy: str = "risk_managed"
    accounting_complete: bool = True
    route: str = ""        # Router-selected strategy id at entry
    regime: str = ""       # Market regime observed at entry
    simulated: bool = False  # True when opened on a virtual wallet

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.amount

    def unrealized_pnl(self, current_price: float) -> float:
        """Calculate unrealized profit/loss."""
        if self.side == "long":
            return (current_price - self.entry_price) * self.amount
        return 0.0

    def unrealized_pnl_pct(self, current_price: float) -> float:
        """Calculate unrealized P&L as percentage."""
        if self.entry_price == 0:
            return 0.0
        if self.side == "long":
            return ((current_price - self.entry_price) / self.entry_price) * 100
        return 0.0

    def should_stop_loss(self, current_price: float) -> bool:
        """Check if stop-loss should trigger."""
        return current_price <= self.stop_loss_price

    def should_take_profit(self, current_price: float) -> bool:
        """Check if take-profit should trigger."""
        return self.take_profit_price is not None and current_price >= self.take_profit_price

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Position":
        return cls(**data)


@dataclass
class TradeRecord:
    """Record of a completed trade for performance tracking."""
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    amount: float
    entry_time: str
    exit_time: str
    pnl: float | None
    pnl_pct: float | None
    reason: str  # Why the trade was closed
    fee: float | None = 0.0
    accounting_complete: bool = True
    strategy: str = ""       # Which candidate strategy produced the trade
    route: str = ""          # Router-selected strategy id at entry ("" when direct)
    regime: str = ""         # Market regime observed at entry ("" when unavailable)
    r_multiple: float | None = None  # Gross price R multiple vs initial stop distance
    order_id: str = ""
    simulated: bool = False  # True when the trade was executed on a virtual wallet

    @property
    def is_winner(self) -> bool:
        return self.accounting_complete and self.pnl is not None and self.pnl > 0


class RiskManager:
    """
    Production-grade Risk Manager.
    """

    EQUITY_BASIS = "usdt_cash_plus_tracked_positions_v1"

    # Route-scoped consecutive-loss budgets. Each strategy route keeps its own
    # loss streak; reaching the cap halts only that route's new entries, never
    # the whole portfolio. The legacy "risk_managed" route keeps the original
    # portfolio-wide behavior for backward compatibility. Research:
    # research/REGIME_ROUTER_REVIEW.md (a downtrend loss streak must not halt the
    # better uptrend EMA72 route) and the 2026-11-15 halt-cap sweep.
    ROUTE_BY_EXIT_POLICY = {
        "ema72_signal": "uptrend",
        "downtrend_rsi_recovery": "downtrend",
        "risk_managed": "legacy",
    }
    MANAGED_ROUTES = ("uptrend", "downtrend")

    @classmethod
    def route_key_for(cls, exit_policy: str) -> str:
        """Map a position exit policy to a loss-budget route key."""
        return cls.ROUTE_BY_EXIT_POLICY.get(exit_policy, "legacy")

    # Single source of truth for halt-recovery semantics. Halt reason strings are
    # matched by prefix so the recovery path for each halt lives in exactly one
    # place instead of being duplicated across the risk manager, engine, and
    # dashboard.
    # - DAILY_RESET_HALT_PREFIXES reset automatically at the next UTC day.
    # - REVIEWED_CLEAR_HALT_PREFIXES can be cleared by an operator acknowledgement
    #   once the account is flat, reconciled, and free of unresolved orders.
    # Any other halt remains a hard latch that requires explicit operator
    # investigation (and, if necessary, direct state repair).
    DAILY_RESET_HALT_PREFIXES = (
        "Daily equity drawdown limit reached:",
        "Daily loss limit reached:",
    )
    ROLLING_DRAWDOWN_HALT_PREFIX = "Rolling peak-to-trough drawdown limit reached:"
    REVIEWED_CLEAR_HALT_PREFIXES = (
        "Emergency stop",
        "Exchange stop filled",
        "Consecutive loss limit reached",
        "Consecutive loss limit persists",
        ROLLING_DRAWDOWN_HALT_PREFIX,
        "Buy order was rejected",
        "Sell order was rejected",
        "Stop-exit order was rejected",
        "Buy order state requires reconciliation",
        "Sell order state requires reconciliation",
        "Stop-exit order state requires reconciliation",
        "Unresolved order intent exists",
        "Sell order for",
        "Stop-exit order for",
        "Partial sell for",
        "Partial stop-exit for",
        "Partial entry fill for",
        "Trade closed for",
        "Unconfirmed or partial buy fill",
        "Unconfirmed or partial sell fill",
        "Unconfirmed or partial stop-exit",
        "Invalid buy fill details",
        "Invalid sell fill details",
        "Invalid stop-exit fill details",
        "Buy fee details were incomplete",
        "Post-fill stop risk exceeded",
        "Candidate stop became invalid after fill",
        "Candidate target became invalid after fill",
        "Post-fill risk could not be validated",
        "Signal execution failed",
        "Position monitoring failed",
        "Account equity valuation failed",
        "Account equity unavailable or invalid",
        "Native stop placement/persistence failed",
        "Exchange stop replacement state is uncertain",
        "Replacement exchange stop failed",
        "Canceled exchange stop reference could not be persisted",
        "Could not cancel exchange stop",
        "Break-even stop update failed",
        "Trailing stop update failed",
        "Unable to obtain a valid market price",
        "Cannot protect missing or invalid position parameters",
        "Exchange stop could not be attached",
        "Equity risk baseline audit record failed",
        "Manual close state could not be verified",
        "Dashboard close state requires review",
        "Entry pause could not be saved",
        "Resume request could not be saved",
        "Testnet smoke buy was not confirmed fully filled",
        "Testnet smoke buy fee/fill data was incomplete",
        "Testnet smoke sell",
    )

    @classmethod
    def is_daily_reset_halt(cls, reason: str) -> bool:
        """Whether a halt reason is a daily-equity latch that resets each UTC day."""
        return isinstance(reason, str) and reason.startswith(cls.DAILY_RESET_HALT_PREFIXES)

    @classmethod
    def is_reviewed_clear_halt(cls, reason: str) -> bool:
        """Whether a halt reason can be cleared by an operator acknowledgement."""
        return isinstance(reason, str) and reason.startswith(cls.REVIEWED_CLEAR_HALT_PREFIXES)

    @classmethod
    def is_rolling_drawdown_halt(cls, reason: str) -> bool:
        """Whether a halt reason is the rolling peak-to-trough drawdown latch."""
        return isinstance(reason, str) and reason.startswith(cls.ROLLING_DRAWDOWN_HALT_PREFIX)

    def __init__(self, config: RiskConfig, persistence_dir: str = "data"):
        self.config = config
        self.persistence_path = Path(persistence_dir) / "open_positions.json"
        self.persistence_path.parent.mkdir(parents=True, exist_ok=True)
        database_url = os.environ.get("DATABASE_URL", "").strip()
        sqlite_db_path = self.persistence_path.parent / "bot_state.sqlite3"
        if database_url:
            # Import before constructing the Postgres store so all state is present
            # even when the platform's configured start command bypasses our CLI wrapper.
            PostgresStateStore.migrate_from_sqlite(
                database_url,
                sqlite_db_path,
                self.persistence_path.parent / "trades.jsonl",
            )
            self.store = PostgresStateStore(database_url)
        else:
            self.store = StateStore(sqlite_db_path)

        self.open_positions: dict[str, Position] = {}
        self.trade_history: list[TradeRecord] = []
        self._daily_loss = 0.0
        self._daily_starting_equity = 0.0
        self._rolling_peak_equity = 0.0
        self._consecutive_losses = 0
        self._route_losses: dict[str, int] = {}
        self._route_halted: dict[str, str] = {}
        self._daily_loss_reset_date = datetime.now(timezone.utc).date()
        self._is_halted = False
        self._halt_reason = ""
        self.on_halt = None
        self._equity_basis: str | None = None
        self._equity_basis_migration_pending = False

        # Restore both risk state stores before allowing any entries. Position
        # state is also a risk control: unreadable state must halt new entries.
        self._load_daily_state()
        self._load_positions()
        self._load_trade_history()

    @property
    def daily_state_path(self) -> Path:
        return self.persistence_path.parent / "daily_risk_state.json"

    def _load_daily_state(self):
        """Restore realized daily drawdown and halt state across restarts."""
        try:
            state = self.store.read_state("daily_risk")
            # One-time migration from the legacy JSON state file.
            if state is None and self.daily_state_path.exists():
                state = json.loads(self.daily_state_path.read_text(encoding="utf-8"))
                self.store.write_state("daily_risk", state)
            if state is None:
                return
            state_date = datetime.fromisoformat(state["date"]).date()
            today = datetime.now(timezone.utc).date()
            daily_loss = float(state.get("daily_loss", 0.0))
            daily_start = float(state.get("daily_starting_equity", 0.0))
            rolling_peak = float(state.get("rolling_peak_equity", 0.0))
            is_halted = state.get("is_halted", False)
            consecutive = int(state.get("consecutive_losses", 0))
            route_losses = state.get("route_losses", {})
            route_halted = state.get("route_halted", {})
            if not isinstance(route_losses, dict) or not isinstance(route_halted, dict):
                raise ValueError("daily equity risk state contains invalid route data")
            normalized_losses: dict[str, int] = {}
            for key, value in route_losses.items():
                if not isinstance(key, str) or not isinstance(value, (int, float)) or int(value) < 0:
                    raise ValueError("daily equity risk state contains invalid route data")
                normalized_losses[key] = int(value)
            normalized_halted: dict[str, str] = {}
            for key, value in route_halted.items():
                if not isinstance(key, str) or not isinstance(value, str) or not value:
                    raise ValueError("daily equity risk state contains invalid route data")
                normalized_halted[key] = value[:500]
            if (not math.isfinite(daily_loss) or not isinstance(is_halted, bool) or
                    not math.isfinite(daily_start) or daily_start < 0 or
                    not math.isfinite(rolling_peak) or rolling_peak < 0 or consecutive < 0):
                raise ValueError("daily equity risk state contains invalid values")
            self._route_losses = normalized_losses
            self._route_halted = normalized_halted
            self._equity_basis = state.get("equity_basis")
            basis_matches = self._equity_basis == self.EQUITY_BASIS
            prior_equity_baseline_exists = daily_start > 0 or rolling_peak > 0
            self._equity_basis_migration_pending = not basis_matches and prior_equity_baseline_exists
            self._rolling_peak_equity = rolling_peak if basis_matches else 0.0
            self._consecutive_losses = consecutive
            halt_reason = str(state.get("halt_reason", ""))
            if state_date == today:
                self._daily_loss = daily_loss
                self._daily_starting_equity = daily_start if basis_matches else 0.0
                self._is_halted = is_halted
                self._halt_reason = halt_reason
                self._daily_loss_reset_date = today
            else:
                # Daily equity anchors belong to the prior day and reset here.
                # A non-daily halt is a deliberate safety latch and must survive
                # a redeploy that happens after UTC midnight; silently clearing
                # it would re-enable entries without operator review.
                self._daily_loss = 0.0
                self._daily_starting_equity = 0.0
                self._daily_loss_reset_date = today
                if is_halted and not self.is_daily_reset_halt(halt_reason):
                    self._is_halted = True
                    self._halt_reason = halt_reason
            # A crash can happen after the loss counter is persisted but before
            # the corresponding halt write. Never let a stale false flag erase
            # a loss limit that the durable counter itself proves was reached.
            if self._consecutive_losses >= self.config.max_consecutive_losses:
                self._is_halted = True
                if not self._halt_reason:
                    self._halt_reason = f"Consecutive loss limit persists ({self._consecutive_losses}); entries paused"
            # Route-scoped crash survival: a route's durable loss count proves the
            # limit was reached even if the corresponding route-halt write was lost.
            for route in self.MANAGED_ROUTES:
                if (self._route_losses.get(route, 0) >= self.config.max_consecutive_losses
                        and route not in self._route_halted):
                    self._route_halted[route] = (
                        f"Consecutive loss limit persists ({self._route_losses[route]}); "
                        f"{route} route entries paused"
                    )
        except Exception as exc:
            # Fail closed: corrupt state should prevent new orders until reconciled.
            self._is_halted = True
            self._halt_reason = "Daily risk state is unreadable; operator reconciliation required"
            logger.error("Could not restore daily risk state: %s", exc)

    def _save_daily_state(self):
        state = {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "daily_loss": self._daily_loss,
            "daily_starting_equity": self._daily_starting_equity,
            "rolling_peak_equity": self._rolling_peak_equity,
            "equity_basis": self._equity_basis,
            "consecutive_losses": self._consecutive_losses,
            "route_losses": self._route_losses,
            "route_halted": self._route_halted,
            "is_halted": self._is_halted,
            "halt_reason": self._halt_reason,
        }
        try:
            self.store.write_state("daily_risk", state)
        except Exception as exc:
            self._is_halted = True
            self._halt_reason = "Daily risk state could not be saved; operator reconciliation required"
            logger.critical("Failed to persist daily risk state: %s", exc)
            raise

    def _save_positions(self) -> bool:
        """Atomically persist positions; state failure halts new trading."""
        try:
            data = {sym: pos.to_dict() for sym, pos in self.open_positions.items()}
            self.store.write_positions(data)
            logger.debug("Saved %s positions to %s", len(self.open_positions), self.persistence_path)
            return True
        except Exception as exc:
            self._is_halted = True
            self._halt_reason = "Open-position state could not be saved; operator reconciliation required"
            logger.error("Failed to persist open positions: %s", exc)
            return False

    def halt(self, reason: str):
        """Stop all new orders and persist the circuit-breaker state."""
        was_halted = self._is_halted
        previous_reason = self._halt_reason
        self._is_halted = True
        self._halt_reason = str(reason)[:500]
        if not was_halted or previous_reason != self._halt_reason:
            try:
                self.store.append_event("risk_halt", {"reason": self._halt_reason})
            except Exception as exc:
                logger.error("Could not record trading halt event: %s", type(exc).__name__)
        try:
            self._save_daily_state()
        except Exception as exc:
            logger.critical("Could not persist trading halt state: %s", exc)
        if not was_halted or previous_reason != self._halt_reason:
            callback = self.on_halt
            if callable(callback):
                try:
                    callback(self._halt_reason)
                except Exception as exc:
                    logger.warning("Safety-halt notification callback failed (%s)", type(exc).__name__)

    def _load_positions(self):
        """Load positions from disk upon bot startup."""
        try:
            data = self.store.read_positions()
            if not data and self.persistence_path.exists():
                data = json.loads(self.persistence_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("open-position state must be an object")
                self.store.write_positions(data)
            if data:
                if not isinstance(data, dict):
                    raise ValueError("open-position state must be an object")
                for sym, pos_data in data.items():
                    position = Position.from_dict(pos_data)
                    numeric_values = (
                        position.entry_price, position.amount, position.stop_loss_price,
                        position.highest_price,
                    )
                    valid_target = (
                        position.take_profit_price is None and position.exit_policy == "ema72_signal"
                    ) or (
                        position.take_profit_price is not None
                        and math.isfinite(float(position.take_profit_price))
                        and position.take_profit_price > position.entry_price
                    )
                    if (position.symbol != sym or position.side != "long" or
                            not all(math.isfinite(float(value)) for value in numeric_values) or
                            position.entry_price <= 0 or position.amount <= 0 or
                            position.stop_loss_price <= 0 or
                            not valid_target or position.exit_policy not in {"risk_managed", "ema72_signal", "downtrend_rsi_recovery"}):
                        raise ValueError(f"invalid persisted position for {sym}")
                    self.open_positions[sym] = position
                if self.open_positions:
                    logger.info(f"🔄 Restored {len(self.open_positions)} active position(s) from disk: {list(self.open_positions.keys())}")
            unresolved = self.store.unresolved_orders()
            if unresolved:
                self._is_halted = True
                self._halt_reason = "Unresolved order intent exists; exchange reconciliation required"
                logger.critical("Startup halted with %s unresolved or unapplied order intent(s)", len(unresolved))
        except Exception as e:
            self._is_halted = True
            self._halt_reason = "Open-position state is unreadable; operator reconciliation required"
            logger.error("Failed to load persisted positions; new entries halted: %s", type(e).__name__)

    def _load_trade_history(self):
        """Hydrate in-memory TradeRecords from the durable store so performance
        reporting matches the dashboard across restarts on both backends."""
        try:
            list_trades = getattr(self.store, "list_trades", None)
            if callable(list_trades):
                rows = list_trades(20000)
            else:
                rows = []
                trade_log = self.persistence_path.parent / "trades.jsonl"
                if trade_log.exists():
                    with open(trade_log, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rows.append(json.loads(line))
                            except (ValueError, TypeError):
                                continue
            for trade in rows:
                if not isinstance(trade, dict) or trade.get("action") != "SELL":
                    continue
                try:
                    record = TradeRecord(
                        symbol=str(trade.get("symbol", "")),
                        side="long",
                        entry_price=float(trade.get("entry_price", 0.0)),
                        exit_price=float(trade.get("price", 0.0)),
                        amount=float(trade.get("amount", 0.0)),
                        entry_time=str(trade.get("entry_time", "")),
                        exit_time=str(trade.get("timestamp", "")),
                        pnl=float(trade["pnl"]) if trade.get("pnl") is not None else None,
                        pnl_pct=float(trade["pnl_pct"]) if trade.get("pnl_pct") is not None else None,
                        reason=str(trade.get("reason", "")),
                        fee=float(trade["fee"]) if trade.get("fee") is not None else None,
                        accounting_complete=trade.get("accounting_complete") is not False,
                        strategy=str(trade.get("strategy", "")),
                        route=str(trade.get("route", "")),
                        regime=str(trade.get("regime", "")),
                        r_multiple=float(trade["r_multiple"]) if trade.get("r_multiple") is not None else None,
                        order_id=str(trade.get("order_id", "")),
                        simulated=bool(trade.get("simulated", False)),
                    )
                except (TypeError, ValueError, KeyError):
                    continue
                if record.symbol and record.entry_price > 0 and record.exit_price > 0 and record.amount > 0:
                    self.trade_history.append(record)
        except Exception as exc:
            logger.warning("Could not hydrate trade history from durable store: %s", type(exc).__name__)

    def resume_after_reconciliation(self):
        """Only clear transient exchange-reconciliation halts after clean state."""
        if not self._is_halted or not self._halt_reason.startswith("Exchange reconciliation failed:"):
            return False
        if self.store.unresolved_orders():
            return False
        self._is_halted, self._halt_reason = False, ""
        self._save_daily_state()
        return True

    def _reset_daily_if_needed(self):
        """Reset daily loss counter at the start of each UTC day."""
        today = datetime.now(timezone.utc).date()
        if today != self._daily_loss_reset_date:
            if self._daily_loss < 0:
                logger.info(
                    f"📅 New trading day — resetting daily loss "
                    f"(yesterday: ${self._daily_loss:+,.2f})"
                )
            self._daily_loss = 0.0
            self._daily_loss_reset_date = today
            self._daily_starting_equity = 0.0
            if self._is_halted and self.is_daily_reset_halt(self._halt_reason):
                self._is_halted = False
                self._halt_reason = ""
                logger.info("✅ Trading resumed — new day, daily loss limit reset")
            self._save_daily_state()

    def rebase_for_paper_environment(self, equity: float):
        """Start fresh equity anchors for a new paper exchange, keeping safety latches."""
        if not isinstance(equity, (int, float)) or not math.isfinite(equity) or equity <= 0:
            raise ValueError("Paper account equity must be finite and positive")
        today = datetime.now(timezone.utc).date()
        self._daily_loss = 0.0
        self._daily_loss_reset_date = today
        self._daily_starting_equity = float(equity)
        self._rolling_peak_equity = float(equity)
        self._equity_basis = self.EQUITY_BASIS
        self._equity_basis_migration_pending = False
        # Existing halt and consecutive-loss state are deliberately preserved.
        self._save_daily_state()

    def acknowledge_demo_account_reset(self, equity: float) -> bool:
        """Start a reviewed Binance Demo risk cycle after an exchange reset.

        This clears only daily/rolling equity drawdown halts. It preserves trade
        history, the consecutive-loss streak, and all configured risk limits.
        The caller must first prove the Demo account is flat and reconciled.
        """
        if not isinstance(equity, (int, float)) or not math.isfinite(equity) or equity <= 0:
            return False
        if not self._is_halted or not (
            self._halt_reason.startswith("Daily equity drawdown limit reached:")
            or self.is_rolling_drawdown_halt(self._halt_reason)
        ):
            return False
        today = datetime.now(timezone.utc).date()
        previous = {
            "daily_loss": self._daily_loss,
            "daily_loss_reset_date": self._daily_loss_reset_date,
            "daily_starting_equity": self._daily_starting_equity,
            "rolling_peak_equity": self._rolling_peak_equity,
            "equity_basis": self._equity_basis,
            "equity_basis_migration_pending": self._equity_basis_migration_pending,
            "is_halted": self._is_halted,
            "halt_reason": self._halt_reason,
        }
        self._daily_loss = 0.0
        self._daily_loss_reset_date = today
        self._daily_starting_equity = float(equity)
        self._rolling_peak_equity = float(equity)
        self._equity_basis = self.EQUITY_BASIS
        self._equity_basis_migration_pending = False
        self._is_halted = False
        self._halt_reason = ""
        try:
            self._save_daily_state()
            self.store.append_event("demo_account_reset_reviewed", {
                "managed_equity": round(float(equity), 8),
                "previous_halt": previous["halt_reason"],
                "daily_loss_reset": True,
                "rolling_peak_rebased": True,
                "consecutive_losses_preserved": self._consecutive_losses,
                "entries_paused": True,
            })
            return True
        except Exception:
            self._daily_loss = previous["daily_loss"]
            self._daily_loss_reset_date = previous["daily_loss_reset_date"]
            self._daily_starting_equity = previous["daily_starting_equity"]
            self._rolling_peak_equity = previous["rolling_peak_equity"]
            self._equity_basis = previous["equity_basis"]
            self._equity_basis_migration_pending = previous["equity_basis_migration_pending"]
            self._is_halted = True
            self._halt_reason = "Demo reset review could not be saved; operator review required"
            try:
                self._save_daily_state()
            except Exception:
                pass
            return False

    def _has_persisted_trade_history(self) -> bool:
        """Check durable strategy history before clearing a legacy equity halt."""
        if self.trade_history:
            return True
        list_trades = getattr(self.store, "list_trades", None)
        if callable(list_trades):
            if list_trades(1):
                return True
        # SQLite deployments keep completed strategy records in this JSONL
        # file; its contents survive a process restart even though the
        # in-memory TradeRecord list does not.
        trade_log = self.persistence_path.parent / "trades.jsonl"
        return trade_log.exists() and trade_log.stat().st_size > 0

    def monitor_equity(self, equity: float, *, account_reconciled: bool = False) -> bool:
        """Persist daily and rolling equity peaks and halt on threshold breaches."""
        if not isinstance(equity, (int, float)) or not math.isfinite(equity) or equity <= 0:
            self.halt("Account equity unavailable or invalid; operator review required")
            return False
        # Reset the daily anchor before comparing today's equity. This also
        # runs during quiet markets when no BUY signal reaches can_open_position.
        self._reset_daily_if_needed()
        if self._equity_basis_migration_pending:
            previous_basis = self._equity_basis or "legacy whole-account equity"
            previous_halt = self._halt_reason
            self._daily_starting_equity = float(equity)
            self._rolling_peak_equity = float(equity)
            self._equity_basis = self.EQUITY_BASIS
            self._equity_basis_migration_pending = False

            equity_halt = previous_halt.startswith((
                "Daily equity drawdown limit reached:",
                "Rolling peak-to-trough drawdown limit reached:",
            ))
            safe_to_clear_legacy_halt = (
                equity_halt
                and account_reconciled
                and not self.open_positions
                and not self._has_persisted_trade_history()
                and not self.store.unresolved_orders()
                and self._consecutive_losses < self.config.max_consecutive_losses
                and self._daily_loss > -(equity * self.config.max_daily_loss_pct / 100.0)
            )
            cleared_halt = bool(safe_to_clear_legacy_halt)
            if cleared_halt:
                self._is_halted = False
                self._halt_reason = ""
            try:
                self._save_daily_state()
            except Exception:
                logger.exception("Could not persist migrated equity risk baseline")
                return False
            try:
                self.store.append_event("risk_equity_basis_migrated", {
                    "previous_basis": previous_basis,
                    "equity_basis": self.EQUITY_BASIS,
                    "managed_equity": round(float(equity), 2),
                    "legacy_equity_halt_cleared": cleared_halt,
                })
            except Exception:
                self.halt("Equity risk baseline audit record failed; operator review required")
                logger.exception("Could not audit equity risk baseline migration")
                return False
            logger.warning(
                "Migrated equity risk basis from %s to %s at %.2f USDT%s",
                previous_basis, self.EQUITY_BASIS, equity,
                "; cleared obsolete equity halt after clean reconciliation" if cleared_halt else "",
            )
        elif self._equity_basis is None:
            # First-ever equity observation: bind the starting values to the
            # current bot-managed equity basis before applying risk limits.
            self._equity_basis = self.EQUITY_BASIS
        if self._daily_starting_equity <= 0:
            self._daily_starting_equity = float(equity)
        if self._rolling_peak_equity <= 0:
            self._rolling_peak_equity = float(equity)
        self._rolling_peak_equity = max(self._rolling_peak_equity, float(equity))
        daily_drawdown = max(0.0, (self._daily_starting_equity - equity) / self._daily_starting_equity * 100)
        rolling_drawdown = max(0.0, (self._rolling_peak_equity - equity) / self._rolling_peak_equity * 100)
        try:
            self._save_daily_state()
        except Exception:
            return False
        if daily_drawdown >= self.config.max_daily_loss_pct:
            self.halt(f"Daily equity drawdown limit reached: {daily_drawdown:.2f}%")
            return False
        if rolling_drawdown >= self.config.max_rolling_drawdown_pct:
            self.halt(f"Rolling peak-to-trough drawdown limit reached: {rolling_drawdown:.2f}%")
            return False
        return True

    def can_open_position(self, symbol: str, portfolio_value: float, route: str | None = None) -> tuple[bool, str]:
        """Check if opening a new position complies with risk rules.

        ``route`` is the loss-budget route key (``uptrend``, ``downtrend``, or
        ``legacy``). When provided, a route-scoped consecutive-loss halt blocks
        only that route; omitted routes use the legacy portfolio-wide behavior.
        """
        self._reset_daily_if_needed()

        mode = self.entry_mode()
        if mode != "ACTIVE":
            return False, "Entry-only pause is active; existing positions continue to be monitored and exited"

        if self._is_halted:
            return False, f"Trading halted: {self._halt_reason}"

        if route is not None and route in self._route_halted:
            return False, self._route_halted[route]

        if len(self.open_positions) >= self.config.max_open_positions:
            return False, (
                f"Max positions reached ({self.config.max_open_positions}). "
                f"Open: {list(self.open_positions.keys())}"
            )

        if symbol in self.open_positions:
            return False, f"Already have an open position in {symbol}"

        max_daily_loss = portfolio_value * (self.config.max_daily_loss_pct / 100)
        if abs(self._daily_loss) >= max_daily_loss and self._daily_loss < 0:
            self._is_halted = True
            self._halt_reason = (
                f"Daily loss limit reached: ${self._daily_loss:,.2f} "
                f"(limit: -${max_daily_loss:,.2f})"
            )
            logger.warning(f"🛑 {self._halt_reason}")
            self._save_daily_state()
            return False, self._halt_reason

        return True, "OK"

    def route_halts(self) -> dict[str, str]:
        """Snapshot of route-scoped consecutive-loss halts (route -> reason)."""
        return dict(self._route_halted)

    def clear_route_halts(self) -> bool:
        """Clear route-scoped consecutive-loss halts after operator review.

        Resets only the halted routes' streaks. The portfolio-wide halt, daily
        loss, and rolling drawdown controls are untouched.
        """
        if not self._route_halted:
            return False
        previous_halted = dict(self._route_halted)
        previous_losses = dict(self._route_losses)
        self._route_halted = {}
        for route in previous_halted:
            self._route_losses[route] = 0
        try:
            self._save_daily_state()
            self.store.append_event("route_loss_halt_cleared", {"routes": list(previous_halted)})
            return True
        except Exception:
            self._route_halted = previous_halted
            self._route_losses = previous_losses
            return False

    def set_exit_only(self):
        """Durably stop new entries while retaining existing position monitoring."""
        self.store.write_state("entry_mode", "EXIT_ONLY")
        self.store.append_event("mode_changed", {"mode": "EXIT_ONLY"})

    def acknowledge_manual_halt_review(self, *, rolling_drawdown_rebase_equity: float | None = None) -> bool:
        """Clear a reviewed halt after the operator confirms the account is safe.

        Only halt reasons in REVIEWED_CLEAR_HALT_PREFIXES may be cleared here;
        state-integrity and other halts remain hard latches. Callers
        (resume_entries) must already have verified a clean, reconciled, flat
        account with no unresolved orders before invoking this method.
        """
        if not self._is_halted:
            return False
        reason = self._halt_reason
        if not self.is_reviewed_clear_halt(reason):
            return False
        loss_limit_halt = reason.startswith(("Consecutive loss limit reached", "Consecutive loss limit persists"))
        rolling_drawdown_halt = self.is_rolling_drawdown_halt(reason)
        protective_stop_halt = reason.startswith("Exchange stop filled")
        emergency_halt = reason.startswith("Emergency stop")
        previous_reason = reason
        previous_losses = self._consecutive_losses
        previous_rolling_peak = self._rolling_peak_equity
        if loss_limit_halt:
            # A reviewed acknowledgement starts a new loss streak. Daily and
            # rolling drawdown counters/limits remain untouched.
            self._consecutive_losses = 0
        if rolling_drawdown_halt:
            # A rolling drawdown latch needs a fresh peak to measure the next
            # streak from; the reconciled account equity is the natural anchor.
            if (not isinstance(rolling_drawdown_rebase_equity, (int, float))
                    or not math.isfinite(rolling_drawdown_rebase_equity)
                    or rolling_drawdown_rebase_equity <= 0):
                return False
            self._rolling_peak_equity = float(rolling_drawdown_rebase_equity)
        self._is_halted, self._halt_reason = False, ""
        try:
            if loss_limit_halt:
                event_type = "consecutive_loss_halt_review_acknowledged"
            elif rolling_drawdown_halt:
                event_type = "rolling_drawdown_halt_review_acknowledged"
            elif protective_stop_halt:
                event_type = "protective_stop_review_acknowledged"
            elif emergency_halt:
                event_type = "emergency_stop_review_acknowledged"
            else:
                event_type = "manual_halt_review_acknowledged"
            self.store.append_event(event_type, {
                "previous_reason": previous_reason,
                "previous_consecutive_losses": previous_losses,
                "consecutive_losses_reset": loss_limit_halt,
                "rolling_drawdown_peak_rebased": rolling_drawdown_halt,
                "previous_rolling_peak_equity": previous_rolling_peak,
                "rolling_peak_equity": self._rolling_peak_equity,
            })
            self._save_daily_state()
            return True
        except Exception:
            self._consecutive_losses = previous_losses
            self._rolling_peak_equity = previous_rolling_peak
            self._is_halted = True
            self._halt_reason = "Manual halt review could not be saved; operator action required"
            try:
                self._save_daily_state()
            except Exception:
                pass
            return False

    def acknowledge_emergency_stop_review(self) -> bool:
        """Compatibility wrapper for callers that explicitly review emergency stops."""
        if not self._halt_reason.startswith("Emergency stop"):
            return False
        return self.acknowledge_manual_halt_review()

    def entry_mode(self):
        persisted = self.store.read_state("entry_mode")
        if persisted:
            return persisted
        return "EXIT_ONLY" if getattr(self.config, "start_paused", False) else "ACTIVE"

    @staticmethod
    def _volatility_multiplier(target_volatility, current_parkinson_volatility) -> float:
        """Volatility-targeting scale bounded to [0.50, 1.00].

        Scales position size inversely with realized volatility so dollar-at-risk
        stays roughly constant across calm and explosive regimes. Returns 1.0
        (no scaling) when either input is missing or invalid, preserving prior
        sizing behavior for callers that do not supply volatility data.
        """
        if target_volatility is None or current_parkinson_volatility is None:
            return 1.0
        try:
            target = float(target_volatility)
            current = float(current_parkinson_volatility)
        except (TypeError, ValueError):
            return 1.0
        if not math.isfinite(target) or not math.isfinite(current) or target <= 0 or current <= 0:
            return 1.0
        return min(1.0, max(0.50, target / current))

    def calculate_position_size(
        self,
        portfolio_value: float,
        entry_price: float,
        stop_loss_price: float,
        *,
        risk_multiplier: float = 1.0,
        entry_fee_rate: float = 0.0,
        target_volatility: float | None = None,
        current_parkinson_volatility: float | None = None,
    ) -> float:
        """
        Conservative position sizing:
        Ensures max capital at risk on stop-loss is <= max_risk_per_trade%.
        Optionally scales the result by a volatility-targeting multiplier so
        dollar-at-risk stays constant across regimes; omitted volatility inputs
        leave sizing unchanged.
        """
        if not all(math.isfinite(value) for value in (portfolio_value, entry_price, stop_loss_price, risk_multiplier, entry_fee_rate)):
            return 0.0
        if (portfolio_value <= 0 or entry_price <= 0 or stop_loss_price <= 0
                or not 0 < risk_multiplier <= 1 or not 0 <= entry_fee_rate <= 0.05):
            return 0.0

        risk_amount = portfolio_value * (self.config.max_risk_per_trade / 100) * risk_multiplier
        risk_per_unit = abs(entry_price - stop_loss_price) + entry_price * entry_fee_rate

        if risk_per_unit == 0:
            return 0.0

        risk_based_size = risk_amount / risk_per_unit
        max_position_value = portfolio_value * (self.config.position_size_pct / 100)
        pct_based_size = max_position_value / entry_price

        volatility_multiplier = self._volatility_multiplier(target_volatility, current_parkinson_volatility)
        return min(risk_based_size, pct_based_size) * volatility_multiplier

    def calculate_stop_loss(self, entry_price: float, atr: float = 0.0) -> float:
        """Calculate initial stop-loss price, optionally adapting to ATR."""
        if atr > 0:
            # 1.5 * ATR stop loss, clamped between 1.0% and 3.0%
            atr_sl_distance = 1.5 * atr
            min_sl_dist = entry_price * 0.010
            max_sl_dist = entry_price * 0.030
            clamped_dist = max(min_sl_dist, min(max_sl_dist, atr_sl_distance))
            return entry_price - clamped_dist
        return entry_price * (1 - self.config.stop_loss_pct / 100)

    def calculate_take_profit(self, entry_price: float) -> float:
        """Calculate take-profit price based on configured percentage."""
        return entry_price * (1 + self.config.take_profit_pct / 100)

    def open_position(
        self,
        symbol: str,
        entry_price: float,
        amount: float,
        order_id: str = "",
        atr: float = 0.0,
        entry_fee_quote: float = 0.0,
        stop_loss_override: float | None = None,
        take_profit_override: float | None = None,
        exit_policy: str = "risk_managed",
        route: str = "",
        regime: str = "",
        simulated: bool = False,
    ) -> Position:
        """Record and persist a new open position."""
        if exit_policy not in {"risk_managed", "ema72_signal", "downtrend_rsi_recovery"}:
            raise ValueError("Unsupported position exit policy")
        selected_stop = self.calculate_stop_loss(entry_price, atr) if stop_loss_override is None else float(stop_loss_override)
        selected_target = (
            float(take_profit_override)
            if take_profit_override is not None
            else None if exit_policy == "ema72_signal"
            else self.calculate_take_profit(entry_price)
        )
        if (not math.isfinite(float(entry_price)) or entry_price <= 0 or
                not math.isfinite(float(amount)) or amount <= 0 or
                not math.isfinite(selected_stop) or selected_stop <= 0 or selected_stop >= entry_price or
                (selected_target is not None and
                 (not math.isfinite(selected_target) or selected_target <= entry_price))):
            raise ValueError("Invalid entry position prices or quantity")
        position = Position(
            symbol=symbol,
            side="long",
            entry_price=entry_price,
            amount=amount,
            entry_time=datetime.now(timezone.utc).isoformat(),
            stop_loss_price=selected_stop,
            take_profit_price=selected_target,
            order_id=order_id,
            entry_fee_quote=max(0.0, float(entry_fee_quote)),
            highest_price=entry_price,
            break_even_activated=False,
            exit_policy=exit_policy,
            route=route,
            regime=regime,
            simulated=simulated,
        )
        self.open_positions[symbol] = position
        if not self._save_positions():
            return position

        logger.info(
            f"📊 Opened position: {symbol} | "
            f"Entry: ${entry_price:,.2f} | "
            f"Amount: {amount:.6f} | "
            f"SL: ${position.stop_loss_price:,.2f} | "
            f"TP/exit: {'EMA72 signal' if position.take_profit_price is None else f'${position.take_profit_price:,.2f}'}"
        )
        return position

    def attach_exchange_stop(self, symbol: str, order_id: str, client_order_id: str) -> bool:
        """Persist the exchange-native protective stop reference on the position."""
        position = self.open_positions.get(symbol)
        if not position or not order_id or not client_order_id:
            self.halt(f"Exchange stop could not be attached to tracked position {symbol}")
            return False
        position.stop_order_id = str(order_id)
        position.stop_client_order_id = str(client_order_id)
        if not self._save_positions():
            return False
        return True

    def clear_exchange_stop(self, symbol: str) -> bool:
        position = self.open_positions.get(symbol)
        if not position:
            return False
        position.stop_order_id = ""
        position.stop_client_order_id = ""
        return self._save_positions()

    def update_position_amount(self, symbol: str, amount: float) -> bool:
        position = self.open_positions.get(symbol)
        if not position or not math.isfinite(float(amount)) or amount <= 0:
            return False
        position.amount = float(amount)
        # A partial sell changes the position's cost basis and fee allocation.
        # Until that partial execution is fully reconstructed, exclude the
        # eventual round trip from profit-factor statistics.
        position.accounting_complete = False
        return self._save_positions()

    def mark_position_accounting_incomplete(self, symbol: str) -> bool:
        position = self.open_positions.get(symbol)
        if not position:
            return False
        position.accounting_complete = False
        return self._save_positions()

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        reason: str,
        fee_cost: float | None = 0.0,
        closed_amount: float | None = None,
    ) -> TradeRecord | None:
        """Close an open position, record the trade, and update persistence."""
        if symbol not in self.open_positions:
            logger.warning(f"No open position for {symbol} to close")
            return None

        if not isinstance(exit_price, (int, float)) or not math.isfinite(float(exit_price)) or exit_price <= 0:
            return None
        if fee_cost is not None and (
            not isinstance(fee_cost, (int, float)) or not math.isfinite(float(fee_cost)) or fee_cost < 0
        ):
            return None

        position = self.open_positions[symbol]
        amount = position.amount if closed_amount is None else closed_amount
        if (not isinstance(amount, (int, float)) or not math.isfinite(float(amount))
                or amount <= 0 or amount > position.amount):
            return None
        amount = float(amount)
        allocated_entry_fee = position.entry_fee_quote * (amount / position.amount)
        self.open_positions.pop(symbol)
        if not self._save_positions():
            # Keep the in-memory view conservative and aligned with the last
            # durable state, which still contains this potentially open position.
            self.open_positions[symbol] = position
            return None

        accounting_complete = position.accounting_complete and fee_cost is not None
        if accounting_complete:
            gross_pnl = (exit_price - position.entry_price) * amount
            total_fee = allocated_entry_fee + float(fee_cost)
            pnl = gross_pnl - total_fee
            total_cost = position.entry_price * amount
            pnl_pct = (pnl / total_cost * 100) if total_cost > 0 else 0.0
        else:
            # Do not represent an unpriced commission or partial-fill basis as
            # a zero-fee completed result. The account is halted for review.
            total_fee = None
            pnl = None
            pnl_pct = None

        strategy = {
            "ema72_signal": "macro_donchian_ema72",
            "downtrend_rsi_recovery": "downtrend_rsi_recovery_testnet",
            "risk_managed": "risk_managed",
        }.get(position.exit_policy, position.exit_policy)
        risk_distance = position.entry_price - position.stop_loss_price
        r_multiple = (
            ((exit_price - position.entry_price) / risk_distance)
            if risk_distance > 0 and math.isfinite(risk_distance)
            else None
        )

        record = TradeRecord(
            symbol=symbol,
            side=position.side,
            entry_price=position.entry_price,
            exit_price=exit_price,
            amount=amount,
            entry_time=position.entry_time,
            exit_time=datetime.now(timezone.utc).isoformat(),
            pnl=pnl,
            pnl_pct=pnl_pct,
            reason=reason,
            fee=total_fee,
            accounting_complete=accounting_complete,
            strategy=strategy,
            route=getattr(position, "route", "") or "",
            regime=getattr(position, "regime", "") or "",
            r_multiple=r_multiple,
            order_id=getattr(position, "order_id", "") or "",
            simulated=bool(getattr(position, "simulated", False)),
        )
        self.trade_history.append(record)
        if not accounting_complete:
            self.halt(f"Trade closed for {symbol}, but fee or partial-fill accounting is incomplete; operator reconciliation required")
            return record

        self._daily_loss += pnl
        route_key = self.route_key_for(position.exit_policy)
        is_loss = pnl < 0
        self._route_losses[route_key] = self._route_losses.get(route_key, 0) + 1 if is_loss else 0
        if route_key == "legacy":
            self._consecutive_losses = self._consecutive_losses + 1 if is_loss else 0
        try:
            self._save_daily_state()
        except Exception:
            # The exit is already durable, but daily drawdown state is not.
            # Preserve the completed close and prevent any further entries.
            return record
        if route_key == "legacy" and self._consecutive_losses >= self.config.max_consecutive_losses:
            self.halt(f"Consecutive loss limit reached ({self._consecutive_losses}); entries paused")
        elif route_key in self.MANAGED_ROUTES and self._route_losses[route_key] >= self.config.max_consecutive_losses:
            reason = f"Consecutive loss limit reached ({self._route_losses[route_key]}); {route_key} route entries paused"
            self._route_halted[route_key] = reason
            try:
                self._save_daily_state()
            except Exception:
                return record
            self.store.append_event("route_loss_halt", {"route": route_key, "reason": reason})
            logger.warning("🛑 Route-scoped loss halt: %s", reason)

        emoji = "💰" if pnl >= 0 else "💸"
        logger.info(
            f"{emoji} Closed {symbol} | "
            f"Entry: ${position.entry_price:,.2f} → Exit: ${exit_price:,.2f} | "
            f"P&L: ${pnl:+,.2f} ({pnl_pct:+.2f}%) | "
            f"Reason: {reason}"
        )

        return record

    def check_positions(self, price_getter) -> list[tuple[str, str]]:
        """
        Check all open positions against:
          1. Stop-Loss
          2. Take-Profit
          3. Break-Even Trigger (> +1.5% profit)
          4. Trailing Stop (trailing behind highest reached price)

        Returns list of (symbol, reason) to close immediately.
        """
        self._reset_daily_if_needed()
        to_close = []

        for symbol, position in list(self.open_positions.items()):
            current_price = price_getter(symbol)
            if not isinstance(current_price, (int, float)) or not math.isfinite(current_price) or current_price <= 0:
                self.halt(f"Unable to obtain a valid market price for open position {symbol}; operator review required")
                continue

            # Update highest price reached
            if current_price > position.highest_price:
                position.highest_price = current_price

            gain_pct = ((current_price - position.entry_price) / position.entry_price) * 100

            # Signal-managed trend positions keep their strategy stop and use
            # completed-candle exits; do not add an untested break-even/trail.
            if position.exit_policy in {"ema72_signal", "downtrend_rsi_recovery"}:
                if position.should_stop_loss(current_price):
                    to_close.append((symbol, "STOP-LOSS triggered"))
                elif position.should_take_profit(current_price):
                    to_close.append((symbol, "TAKE-PROFIT triggered"))
                continue

            # 1. Break-Even Protection: Once up 1.5%, move stop to entry price
            if gain_pct >= 1.5 and not position.break_even_activated:
                old_sl = position.stop_loss_price
                position.stop_loss_price = max(position.stop_loss_price, position.entry_price)
                position.break_even_activated = True
                self._save_positions()
                logger.info(
                    f"🛡️ BREAK-EVEN ACTIVATED for {symbol}: "
                    f"Price reached ${current_price:,.2f} (+{gain_pct:.2f}%). "
                    f"Stop-loss moved from ${old_sl:,.2f} to Entry ${position.entry_price:,.2f}"
                )

            # 2. Trailing Stop: If above break-even, trail stop 1.5% below highest price
            if position.break_even_activated:
                trailing_sl = position.highest_price * (1 - 0.015)
                if trailing_sl > position.stop_loss_price:
                    position.stop_loss_price = trailing_sl
                    self._save_positions()
                    logger.debug(f"📈 Trailing stop updated for {symbol}: ${position.stop_loss_price:,.2f}")

            # 3. Check Stop-Loss Trigger
            if position.should_stop_loss(current_price):
                reason = "TRAILING STOP triggered" if position.break_even_activated else "STOP-LOSS triggered"
                to_close.append((symbol, reason))
                logger.warning(
                    f"⚠️ {reason}: {symbol} at ${current_price:,.2f} "
                    f"(SL: ${position.stop_loss_price:,.2f})"
                )

            # 4. Check Take-Profit Trigger
            elif position.should_take_profit(current_price):
                to_close.append((symbol, "TAKE-PROFIT triggered"))
                logger.info(
                    f"🎯 TAKE-PROFIT: {symbol} reached ${current_price:,.2f} "
                    f"(TP: ${position.take_profit_price:,.2f})"
                )

        return to_close

    def get_performance_summary(self) -> dict:
        """Calculate overall trading performance statistics."""
        incomplete_trades = [t for t in self.trade_history if not t.accounting_complete or t.pnl is None]
        completed_trades = [t for t in self.trade_history if t.accounting_complete and t.pnl is not None]
        if not completed_trades:
            return {
                "total_trades": 0,
                "incomplete_trades": len(incomplete_trades),
                "message": "No fully reconciled completed trades yet",
                "open_positions": len(self.open_positions),
                "daily_pnl": round(self._daily_loss, 2),
                "is_halted": self._is_halted,
                "total_fees": 0.0,
                "profit_factor": None,
            }

        total_pnl = sum(t.pnl for t in completed_trades)
        winners = [t for t in completed_trades if t.is_winner]
        losers = [t for t in completed_trades if t.pnl <= 0]
        win_rate = (len(winners) / len(completed_trades)) * 100

        avg_win = sum(t.pnl for t in winners) / len(winners) if winners else 0
        avg_loss = sum(t.pnl for t in losers) / len(losers) if losers else 0
        gross_profit = sum(t.pnl for t in winners)
        gross_loss = -sum(t.pnl for t in losers)
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else None

        return {
            "total_trades": len(completed_trades),
            "incomplete_trades": len(incomplete_trades),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "total_fees": round(sum(t.fee for t in completed_trades if t.fee is not None), 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
            "best_trade": round(max(t.pnl for t in completed_trades), 2),
            "worst_trade": round(min(t.pnl for t in completed_trades), 2),
            "open_positions": len(self.open_positions),
            "daily_pnl": round(self._daily_loss, 2),
            "is_halted": self._is_halted,
        }

    def trade_ledger(self) -> list[dict]:
        """Normalize closed trades into an auditable, export-ready ledger.

        Each row reports the reconciled figures already captured on the
        :class:`TradeRecord`. Proceeds, cost basis, and gross PnL are derived
        from the stored entry/exit prices and amount so a reviewer can see the
        full tax-lot shape. Entry and exit fees are combined in ``fee`` (the
        reconciled total), matching how they are captured at close; incomplete
        closes are included with ``None`` figures rather than silently dropped
        or misrepresented as zero-cost.
        """
        rows: list[dict] = []
        for trade in self.trade_history:
            try:
                entry = float(trade.entry_price)
                exit_price = float(trade.exit_price)
                amount = float(trade.amount)
            except (TypeError, ValueError):
                continue
            rows.append({
                "symbol": trade.symbol,
                "side": trade.side,
                "strategy": trade.strategy,
                "route": trade.route,
                "regime": trade.regime,
                "entry_time": trade.entry_time,
                "exit_time": trade.exit_time,
                "entry_price": entry,
                "exit_price": exit_price,
                "amount": amount,
                "proceeds": round(exit_price * amount, 8),
                "cost_basis": round(entry * amount, 8),
                "gross_pnl": round((exit_price - entry) * amount, 8),
                "fee": trade.fee,
                "net_pnl": trade.pnl,
                "pnl_pct": trade.pnl_pct,
                "r_multiple": trade.r_multiple,
                "order_id": trade.order_id,
                "accounting_complete": trade.accounting_complete,
                "simulated": trade.simulated,
            })
        return rows

    def trade_ledger_csv(self) -> str:
        """Serialize the trade ledger to CSV (header row plus one row per trade)."""
        columns = [
            "symbol", "side", "strategy", "route", "regime",
            "entry_time", "exit_time", "entry_price", "exit_price", "amount",
            "proceeds", "cost_basis", "gross_pnl", "fee", "net_pnl", "pnl_pct",
            "r_multiple", "order_id", "accounting_complete", "simulated",
        ]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        for row in self.trade_ledger():
            writer.writerow({column: row.get(column, "") for column in columns})
        return buffer.getvalue()

    def display_performance(self):
        """Print a formatted performance summary."""
        stats = self.get_performance_summary()

        if stats["total_trades"] == 0:
            print(
                f"\n📊 Reconciled Trades: 0 | Incomplete exits: {stats['incomplete_trades']} | "
                f"Open Positions: {stats['open_positions']} | Today's P&L: ${stats['daily_pnl']:+,.2f} | "
                f"Status: {'🛑 HALTED' if stats['is_halted'] else '✅ ACTIVE'}\n"
            )
            return

        pf_display = f"{stats['profit_factor']:.2f}" if stats["profit_factor"] is not None else "N/A (no losses)"

        print(f"""
╔══════════════════════════════════════════════════════════╗
║                 PERFORMANCE SUMMARY                      ║
╠══════════════════════════════════════════════════════════╣
║  Total Trades:    {stats['total_trades']:<38}║
║  Win Rate:        {stats['win_rate']}% ({stats['winners']}W / {stats['losers']}L){'':<20}║
║  Total P&L:       ${stats['total_pnl']:+,.2f}{'':<30}║
║  Avg Win:         ${stats['avg_win']:+,.2f}{'':<30}║
║  Avg Loss:        ${stats['avg_loss']:+,.2f}{'':<30}║
║  Profit Factor:   {pf_display:<38}║
║  Incomplete Exits:{stats['incomplete_trades']:<38}║
║  Best Trade:      ${stats['best_trade']:+,.2f}{'':<30}║
║  Worst Trade:     ${stats['worst_trade']:+,.2f}{'':<30}║
╠══════════════════════════════════════════════════════════╣
║  Today's P&L:     ${stats['daily_pnl']:+,.2f}{'':<30}║
║  Open Positions:  {stats['open_positions']:<38}║
║  Status:          {'🛑 HALTED' if stats['is_halted'] else '✅ ACTIVE':<38}║
╚══════════════════════════════════════════════════════════╝
""")
