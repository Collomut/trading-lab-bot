"""
Risk Manager — the safety net of the bot.

Upgraded features:
  - Position persistence to disk (data/open_positions.json): survives restarts!
  - Trailing stop-loss: locks in profits as price climbs.
  - Break-even protection: moves stop-loss to entry price once up 1.5%.
  - Volatility-adjusted stop-loss (ATR-aware).
  - Strict daily drawdown circuit breakers.
"""

import json
import logging
import math
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from pathlib import Path
from config import RiskConfig
from state_store import StateStore

logger = logging.getLogger("bot.risk")


@dataclass
class Position:
    """Represents an open trading position."""
    symbol: str
    side: str  # "long"
    entry_price: float
    amount: float
    entry_time: str  # ISO 8601 string for easy JSON serialization
    stop_loss_price: float
    take_profit_price: float
    order_id: str = ""
    stop_order_id: str = ""
    stop_client_order_id: str = ""
    highest_price: float = 0.0  # For trailing stop
    break_even_activated: bool = False

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
        return current_price >= self.take_profit_price

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
    pnl: float
    pnl_pct: float
    reason: str  # Why the trade was closed
    fee: float = 0.0

    @property
    def is_winner(self) -> bool:
        return self.pnl > 0


class RiskManager:
    """
    Production-grade Risk Manager.
    """

    def __init__(self, config: RiskConfig, persistence_dir: str = "data"):
        self.config = config
        self.persistence_path = Path(persistence_dir) / "open_positions.json"
        self.persistence_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = StateStore(self.persistence_path.parent / "bot_state.sqlite3")

        self.open_positions: dict[str, Position] = {}
        self.trade_history: list[TradeRecord] = []
        self._daily_loss = 0.0
        self._daily_starting_equity = 0.0
        self._rolling_peak_equity = 0.0
        self._consecutive_losses = 0
        self._daily_loss_reset_date = datetime.now(timezone.utc).date()
        self._is_halted = False
        self._halt_reason = ""

        # Restore both risk state stores before allowing any entries. Position
        # state is also a risk control: unreadable state must halt new entries.
        self._load_daily_state()
        self._load_positions()

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
            if (not math.isfinite(daily_loss) or not isinstance(is_halted, bool) or
                    not math.isfinite(daily_start) or daily_start < 0 or
                    not math.isfinite(rolling_peak) or rolling_peak < 0 or consecutive < 0):
                raise ValueError("daily equity risk state contains invalid values")
            self._rolling_peak_equity = rolling_peak
            self._consecutive_losses = consecutive
            if self._consecutive_losses >= self.config.max_consecutive_losses:
                self._is_halted = True
                self._halt_reason = f"Consecutive loss limit persists ({self._consecutive_losses}); entries paused"
            if state_date == today:
                self._daily_loss = daily_loss
                self._is_halted = is_halted
                self._halt_reason = str(state.get("halt_reason", ""))
                self._daily_starting_equity = daily_start
                self._daily_loss_reset_date = today
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
            "consecutive_losses": self._consecutive_losses,
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
                        position.take_profit_price, position.highest_price,
                    )
                    if (position.symbol != sym or position.side != "long" or
                            not all(math.isfinite(float(value)) for value in numeric_values) or
                            position.entry_price <= 0 or position.amount <= 0 or
                            position.stop_loss_price <= 0 or
                            position.take_profit_price <= position.entry_price):
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
            if self._is_halted and ("daily loss" in self._halt_reason.lower() or "daily equity drawdown" in self._halt_reason.lower()):
                self._is_halted = False
                self._halt_reason = ""
                logger.info("✅ Trading resumed — new day, daily loss limit reset")
            self._save_daily_state()

    def monitor_equity(self, equity: float) -> bool:
        """Persist daily and rolling equity peaks and halt on threshold breaches."""
        if not isinstance(equity, (int, float)) or not math.isfinite(equity) or equity <= 0:
            self.halt("Account equity unavailable or invalid; operator review required")
            return False
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

    def can_open_position(self, symbol: str, portfolio_value: float) -> tuple[bool, str]:
        """Check if opening a new position complies with risk rules."""
        self._reset_daily_if_needed()

        mode = self.entry_mode()
        if mode != "ACTIVE":
            return False, "Entry-only pause is active; existing positions continue to be monitored and exited"

        if self._is_halted:
            return False, f"Trading halted: {self._halt_reason}"

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

    def set_exit_only(self):
        """Durably stop new entries while retaining existing position monitoring."""
        self.store.write_state("entry_mode", "EXIT_ONLY")
        self.store.append_event("mode_changed", {"mode": "EXIT_ONLY"})

    def acknowledge_emergency_stop_review(self) -> bool:
        """Clear only a latched emergency-stop halt after the operator review flow."""
        if not self._is_halted or not self._halt_reason.startswith("Emergency stop"):
            return False
        previous_reason = self._halt_reason
        self._is_halted, self._halt_reason = False, ""
        try:
            self.store.append_event("emergency_stop_review_acknowledged", {"previous_reason": previous_reason})
            self._save_daily_state()
            return True
        except Exception:
            self._is_halted = True
            self._halt_reason = "Emergency-stop review could not be saved; operator action required"
            try:
                self._save_daily_state()
            except Exception:
                pass
            return False

    def entry_mode(self):
        persisted = self.store.read_state("entry_mode")
        if persisted:
            return persisted
        return "EXIT_ONLY" if getattr(self.config, "start_paused", False) else "ACTIVE"

    def calculate_position_size(
        self,
        portfolio_value: float,
        entry_price: float,
        stop_loss_price: float
    ) -> float:
        """
        Conservative position sizing:
        Ensures max capital at risk on stop-loss is <= max_risk_per_trade%.
        """
        if not all(math.isfinite(value) for value in (portfolio_value, entry_price, stop_loss_price)):
            return 0.0
        if portfolio_value <= 0 or entry_price <= 0 or stop_loss_price <= 0:
            return 0.0

        risk_amount = portfolio_value * (self.config.max_risk_per_trade / 100)
        risk_per_unit = abs(entry_price - stop_loss_price)

        if risk_per_unit == 0:
            return 0.0

        risk_based_size = risk_amount / risk_per_unit
        max_position_value = portfolio_value * (self.config.position_size_pct / 100)
        pct_based_size = max_position_value / entry_price

        return min(risk_based_size, pct_based_size)

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
        atr: float = 0.0
    ) -> Position:
        """Record and persist a new open position."""
        position = Position(
            symbol=symbol,
            side="long",
            entry_price=entry_price,
            amount=amount,
            entry_time=datetime.now(timezone.utc).isoformat(),
            stop_loss_price=self.calculate_stop_loss(entry_price, atr),
            take_profit_price=self.calculate_take_profit(entry_price),
            order_id=order_id,
            highest_price=entry_price,
            break_even_activated=False,
        )
        self.open_positions[symbol] = position
        if not self._save_positions():
            return position

        logger.info(
            f"📊 Opened position: {symbol} | "
            f"Entry: ${entry_price:,.2f} | "
            f"Amount: {amount:.6f} | "
            f"SL: ${position.stop_loss_price:,.2f} | "
            f"TP: ${position.take_profit_price:,.2f}"
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
        return self._save_positions()

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        reason: str,
        fee_cost: float = 0.0
    ) -> TradeRecord | None:
        """Close an open position, record the trade, and update persistence."""
        if symbol not in self.open_positions:
            logger.warning(f"No open position for {symbol} to close")
            return None

        position = self.open_positions.pop(symbol)
        if not self._save_positions():
            # Keep the in-memory view conservative and aligned with the last
            # durable state, which still contains this potentially open position.
            self.open_positions[symbol] = position
            return None

        gross_pnl = position.unrealized_pnl(exit_price)
        total_fee = max(0.0, float(fee_cost))
        pnl = gross_pnl - total_fee
        total_cost = position.entry_price * position.amount
        pnl_pct = (pnl / total_cost * 100) if total_cost > 0 else 0.0

        record = TradeRecord(
            symbol=symbol,
            side=position.side,
            entry_price=position.entry_price,
            exit_price=exit_price,
            amount=position.amount,
            entry_time=position.entry_time,
            exit_time=datetime.now(timezone.utc).isoformat(),
            pnl=pnl,
            pnl_pct=pnl_pct,
            reason=reason,
            fee=total_fee,
        )
        self.trade_history.append(record)
        self._daily_loss += pnl
        self._consecutive_losses = self._consecutive_losses + 1 if pnl < 0 else 0
        try:
            self._save_daily_state()
        except Exception:
            # The exit is already durable, but daily drawdown state is not.
            # Preserve the completed close and prevent any further entries.
            return record
        if self._consecutive_losses >= self.config.max_consecutive_losses:
            self.halt(f"Consecutive loss limit reached ({self._consecutive_losses}); entries paused")

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
        if not self.trade_history:
            return {
                "total_trades": 0,
                "message": "No completed trades yet",
                "open_positions": len(self.open_positions),
                "daily_pnl": round(self._daily_loss, 2),
                "is_halted": self._is_halted,
            }

        total_pnl = sum(t.pnl for t in self.trade_history)
        winners = [t for t in self.trade_history if t.is_winner]
        losers = [t for t in self.trade_history if not t.is_winner]
        win_rate = (len(winners) / len(self.trade_history)) * 100

        avg_win = sum(t.pnl for t in winners) / len(winners) if winners else 0
        avg_loss = sum(t.pnl for t in losers) / len(losers) if losers else 0
        profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')

        return {
            "total_trades": len(self.trade_history),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "best_trade": round(max(t.pnl for t in self.trade_history), 2),
            "worst_trade": round(min(t.pnl for t in self.trade_history), 2),
            "open_positions": len(self.open_positions),
            "daily_pnl": round(self._daily_loss, 2),
            "is_halted": self._is_halted,
        }

    def display_performance(self):
        """Print a formatted performance summary."""
        stats = self.get_performance_summary()

        if stats["total_trades"] == 0:
            print(f"\n📊 Open Positions: {stats['open_positions']} | Today's P&L: ${stats['daily_pnl']:+,.2f} | Status: {'🛑 HALTED' if stats['is_halted'] else '✅ ACTIVE'}\n")
            return

        print(f"""
╔══════════════════════════════════════════════════════════╗
║                 PERFORMANCE SUMMARY                      ║
╠══════════════════════════════════════════════════════════╣
║  Total Trades:    {stats['total_trades']:<38}║
║  Win Rate:        {stats['win_rate']}% ({stats['winners']}W / {stats['losers']}L){'':<20}║
║  Total P&L:       ${stats['total_pnl']:+,.2f}{'':<30}║
║  Avg Win:         ${stats['avg_win']:+,.2f}{'':<30}║
║  Avg Loss:        ${stats['avg_loss']:+,.2f}{'':<30}║
║  Profit Factor:   {stats['profit_factor']:<38}║
║  Best Trade:      ${stats['best_trade']:+,.2f}{'':<30}║
║  Worst Trade:     ${stats['worst_trade']:+,.2f}{'':<30}║
╠══════════════════════════════════════════════════════════╣
║  Today's P&L:     ${stats['daily_pnl']:+,.2f}{'':<30}║
║  Open Positions:  {stats['open_positions']:<38}║
║  Status:          {'🛑 HALTED' if stats['is_halted'] else '✅ ACTIVE':<38}║
╚══════════════════════════════════════════════════════════╝
""")
