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
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from pathlib import Path
from config import RiskConfig

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

        self.open_positions: dict[str, Position] = {}
        self.trade_history: list[TradeRecord] = []
        self._daily_loss = 0.0
        self._daily_loss_reset_date = datetime.now(timezone.utc).date()
        self._is_halted = False
        self._halt_reason = ""

        # Load persisted positions if available
        self._load_positions()

    def _save_positions(self):
        """Persist open positions to disk so restarts don't lose them."""
        try:
            data = {sym: pos.to_dict() for sym, pos in self.open_positions.items()}
            with open(self.persistence_path, "w") as f:
                json.dump(data, f, indent=2)
            logger.debug(f"Saved {len(self.open_positions)} positions to {self.persistence_path}")
        except Exception as e:
            logger.error(f"Failed to persist positions: {e}")

    def _load_positions(self):
        """Load positions from disk upon bot startup."""
        if self.persistence_path.exists():
            try:
                with open(self.persistence_path, "r") as f:
                    data = json.load(f)
                for sym, pos_data in data.items():
                    self.open_positions[sym] = Position.from_dict(pos_data)
                if self.open_positions:
                    logger.info(f"🔄 Restored {len(self.open_positions)} active position(s) from disk: {list(self.open_positions.keys())}")
            except Exception as e:
                logger.error(f"Failed to load persisted positions: {e}")

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
            if self._is_halted and "daily loss" in self._halt_reason.lower():
                self._is_halted = False
                self._halt_reason = ""
                logger.info("✅ Trading resumed — new day, daily loss limit reset")

    def can_open_position(self, symbol: str, portfolio_value: float) -> tuple[bool, str]:
        """Check if opening a new position complies with risk rules."""
        self._reset_daily_if_needed()

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
            return False, self._halt_reason

        return True, "OK"

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
        if entry_price <= 0 or stop_loss_price <= 0:
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
        self._save_positions()

        logger.info(
            f"📊 Opened position: {symbol} | "
            f"Entry: ${entry_price:,.2f} | "
            f"Amount: {amount:.6f} | "
            f"SL: ${position.stop_loss_price:,.2f} | "
            f"TP: ${position.take_profit_price:,.2f}"
        )
        return position

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        reason: str
    ) -> TradeRecord | None:
        """Close an open position, record the trade, and update persistence."""
        if symbol not in self.open_positions:
            logger.warning(f"No open position for {symbol} to close")
            return None

        position = self.open_positions.pop(symbol)
        self._save_positions()

        pnl = position.unrealized_pnl(exit_price)
        pnl_pct = position.unrealized_pnl_pct(exit_price)

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
        )
        self.trade_history.append(record)
        self._daily_loss += pnl

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
            if current_price <= 0:
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
