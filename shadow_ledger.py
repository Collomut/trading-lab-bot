"""Halt-free, order-free shadow ledger for forward candidate expectancy.

The shadow ledger evaluates a candidate strategy on every completed candle using
the same entry/exit/stop/sizing rules as live trading, but it never places orders
and never trips a risk halt. Its only purpose is to accumulate forward expectancy
per candidate so the active strategy is chosen on evidence rather than on reused
in-sample backtests.
"""

from __future__ import annotations

import math

FEE_RATE = 0.001       # taker fee per side
RISK_PCT = 1.0         # % of equity risked per trade
NOTIONAL_PCT = 10.0    # % of equity notional cap per position


class ShadowLedger:
    """Persisted paper-trading ledger with next-bar-open execution semantics."""

    def __init__(self, store, initial_equity: float = 10000.0):
        self.store = store
        data = store.read_state("shadow_ledger") or {}
        try:
            initial = float(data.get("initial_equity", initial_equity))
        except (TypeError, ValueError):
            initial = initial_equity
        self.initial_equity = initial if math.isfinite(initial) and initial > 0 else initial_equity
        self.positions = data.get("positions", {})
        self.pending_entries = data.get("pending_entries", {})
        self.pending_exits = {tuple(k) for k in data.get("pending_exits", [])}
        self.trades = data.get("trades", [])
        self.realized_pnl = float(data.get("realized_pnl", 0.0))

    # -- persistence -----------------------------------------------------
    def _persist(self):
        self.store.write_state("shadow_ledger", {
            "initial_equity": self.initial_equity,
            "positions": self.positions,
            "pending_entries": self.pending_entries,
            "pending_exits": [list(k) for k in self.pending_exits],
            "trades": self.trades,
            "realized_pnl": self.realized_pnl,
        })

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _key(candidate_id: str, symbol: str) -> str:
        return f"{candidate_id}|{symbol}"

    def equity(self) -> float:
        return max(0.0, self.initial_equity + self.realized_pnl)

    def has_position(self, candidate_id: str, symbol: str) -> bool:
        return self._key(candidate_id, symbol) in self.positions

    def _open(self, key: str, candidate_id: str, symbol: str, ts: int, open_price: float,
              stop: float, target: float | None, atr: float) -> None:
        equity = self.equity()
        if equity <= 0 or not (0 < stop < open_price):
            return
        risk_amount = equity * RISK_PCT / 100.0
        risk_per_unit = open_price - stop
        risk_qty = risk_amount / risk_per_unit if risk_per_unit > 0 else 0.0
        notional_qty = (equity * NOTIONAL_PCT / 100.0) / open_price
        qty = min(risk_qty, notional_qty)
        if qty <= 0 or not math.isfinite(qty):
            return
        self.positions[key] = {
            "candidate_id": candidate_id,
            "symbol": symbol,
            "entry_price": open_price,
            "amount": qty,
            "stop": stop,
            "target": target,
            "atr": atr,
            "entry_time": ts,
        }

    def _close(self, key: str, symbol: str, price: float, ts: int, reason: str) -> None:
        pos = self.positions.pop(key)
        entry = pos["entry_price"]
        qty = pos["amount"]
        gross = (price - entry) * qty
        entry_fee = entry * qty * FEE_RATE
        exit_fee = price * qty * FEE_RATE
        pnl = gross - entry_fee - exit_fee
        notional = entry * qty
        pnl_pct = (pnl / notional * 100.0) if notional > 0 else 0.0
        risk_distance = entry - pos["stop"]
        r_multiple = ((price - entry) / risk_distance) if risk_distance > 0 else None
        self.realized_pnl += pnl
        self.trades.append({
            "candidate_id": pos["candidate_id"],
            "symbol": symbol,
            "side": "long",
            "entry_price": entry,
            "exit_price": price,
            "amount": qty,
            "entry_time": pos["entry_time"],
            "exit_time": ts,
            "pnl": round(pnl, 6),
            "pnl_pct": round(pnl_pct, 4),
            "r_multiple": round(r_multiple, 4) if r_multiple is not None else None,
            "reason": reason,
        })

    # -- event loop ------------------------------------------------------
    def advance(self, candidate_id: str, symbol: str, candle: list, signal: str, *,
                stop_ref: float | None = None, atr: float = 0.0) -> None:
        """Advance one candidate/symbol on one completed candle.

        candle: [ts_ms, open, high, low, close, volume]
        signal: "BUY" | "SELL" | "HOLD"
        stop_ref: prior-five-bar swing low (entry stop reference), required to open
        atr: ATR14 used for the distant backstop target
        """
        if not isinstance(candle, (list, tuple)) or len(candle) < 5:
            return
        try:
            ts = int(candle[0])
            open_price, high, low, close = (float(v) for v in candle[1:5])
        except (TypeError, ValueError, OverflowError):
            return
        if not all(math.isfinite(v) for v in (open_price, high, low, close)) or min(open_price, high, low, close) <= 0:
            return
        key = self._key(candidate_id, symbol)

        # 1. Execute a queued signal exit at this candle's open.
        if key in self.pending_exits and key in self.positions:
            self._close(key, symbol, open_price, ts, "signal_exit")
            self.pending_exits.discard(key)

        # 2. Execute a queued signal entry at this candle's open.
        pending = self.pending_entries.get(key)
        if pending is not None and key not in self.positions:
            self._open(key, candidate_id, symbol, ts, open_price,
                       pending["stop"], pending["target"], pending["atr"])
            self.pending_entries.pop(key, None)

        # 3. Intrabar stop/target on the open position (stop-first).
        pos = self.positions.get(key)
        if pos is not None:
            if low <= pos["stop"]:
                fill = min(open_price, pos["stop"])
                self._close(key, symbol, fill, ts, "stop_loss")
                pos = None
            elif pos["target"] is not None and high >= pos["target"]:
                self._close(key, symbol, pos["target"], ts, "take_profit")
                pos = None

        # 4. Queue a signal exit for the next candle's open.
        if pos is not None and signal == "SELL":
            self.pending_exits.add(key)

        # 5. Queue a signal entry for the next candle's open.
        if pos is None and signal == "BUY":
            if stop_ref is not None and math.isfinite(float(stop_ref)) and float(stop_ref) > 0 and open_price > 0:
                stop = max(float(stop_ref), open_price * 0.96)
                if 0 < stop < open_price:
                    target = open_price + 100.0 * float(atr) if math.isfinite(float(atr)) and float(atr) > 0 else None
                    self.pending_entries[key] = {"stop": stop, "target": target, "atr": float(atr)}

        self._persist()

    # -- reporting -------------------------------------------------------
    def summary(self, candidate_id: str | None = None) -> dict:
        trades = [t for t in self.trades if candidate_id is None or t["candidate_id"] == candidate_id]
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] < 0]
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = -sum(t["pnl"] for t in losses)
        if gross_loss > 0:
            pf = gross_win / gross_loss
        elif gross_win > 0:
            pf = float("inf")
        else:
            pf = None
        r_values = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
        open_count = sum(
            1 for p in self.positions.values()
            if candidate_id is None or p["candidate_id"] == candidate_id
        )
        return {
            "candidate_id": candidate_id,
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(trades) * 100.0, 1) if trades else 0.0,
            "profit_factor": round(pf, 3) if isinstance(pf, float) and math.isfinite(pf) else pf,
            "total_pnl": round(sum(t["pnl"] for t in trades), 2),
            "realized_pnl": round(self.realized_pnl if candidate_id is None else sum(t["pnl"] for t in trades), 2),
            "avg_r_multiple": round(sum(r_values) / len(r_values), 3) if r_values else None,
            "open_positions": open_count,
        }

