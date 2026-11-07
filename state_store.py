"""Small transactional state store for a single Railway worker.

SQLite must live on Railway's persistent volume. This is not a multi-replica
database; run one worker until a server database and distributed locking exist.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path


class StateStore:
    SCHEMA_VERSION = 1
    ORDER_TRANSITIONS = {
        "PENDING": {"SUBMITTED", "UNKNOWN", "REJECTED"},
        "SUBMITTED": {"UNKNOWN", "OPEN", "PARTIALLY_FILLED", "PARTIAL_CLOSED", "FILLED", "CANCELED", "REJECTED", "EXPIRED"},
        "UNKNOWN": {"UNKNOWN", "OPEN", "PARTIALLY_FILLED", "PARTIAL_CLOSED", "FILLED", "CANCELED", "REJECTED", "EXPIRED"},
        "OPEN": {"UNKNOWN", "OPEN", "PARTIALLY_FILLED", "PARTIAL_CLOSED", "FILLED", "CANCELED", "REJECTED", "EXPIRED"},
        "PARTIALLY_FILLED": {"UNKNOWN", "PARTIALLY_FILLED", "PARTIAL_CLOSED", "FILLED", "CANCELED", "REJECTED", "EXPIRED"},
    }
    TERMINAL = {"FILLED", "PARTIAL_CLOSED", "CANCELED", "REJECTED", "EXPIRED"}

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _initialize(self):
        with self._lock, closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    description TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, value_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS order_intents (
                    client_order_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
                    amount TEXT NOT NULL, state TEXT NOT NULL, exchange_order_id TEXT,
                    filled TEXT NOT NULL DEFAULT '0', average TEXT, request_json TEXT NOT NULL,
                    response_json TEXT, applied INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                INSERT OR IGNORE INTO schema_migrations (version, description)
                VALUES (1, 'Base schema: state, positions, order_intents, audit_events');
            """)

    def get_schema_version(self) -> int:
        """Return the current schema migration version (F-17)."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    def create_backup(self, backup_path: str | Path | None = None) -> Path:
        """Create a consistent online SQLite backup (F-17)."""
        if backup_path is None:
            ts = int(time.time() * 1000)
            backup_path = self.path.with_name(f"{self.path.stem}_backup_{ts}.db")
        else:
            backup_path = Path(backup_path)
            backup_path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock, closing(self._connect()) as src, closing(sqlite3.connect(backup_path)) as dst:
            src.backup(dst)

        return backup_path

    def snapshot_and_reset_for_testnet_wipe(self, operator_reason: str, export_path: str | Path | None = None) -> dict:
        """Safely snapshot state and reset positions/orders after a Binance Spot Testnet reset (F-09)."""
        if not operator_reason or not operator_reason.strip():
            raise ValueError("Operator reason is required for Testnet reset recovery")

        backup_file = self.create_backup()
        positions = self.read_positions()
        unresolved = self.unresolved_orders()

        export_data = {
            "timestamp": int(time.time() * 1000),
            "operator_reason": operator_reason.strip(),
            "backup_file": str(backup_file),
            "positions": positions,
            "unresolved_orders": unresolved,
        }

        if export_path:
            out_p = Path(export_path)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            with open(out_p, "w", encoding="utf-8") as f:
                json.dump(export_data, f, indent=2)

        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Clear positions
            conn.execute("DELETE FROM positions")
            # Mark all non-terminal orders as canceled with testnet reset reason
            conn.execute("""
                UPDATE order_intents
                SET state='CANCELED', applied=1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE state NOT IN ('FILLED','PARTIAL_CLOSED','CANCELED','REJECTED','EXPIRED') OR applied=0
            """)
            # Reset baseline balances and smoke trade marker
            conn.execute("DELETE FROM state WHERE key IN ('baseline_asset_balances', 'testnet_smoke_trade')")
            # Record audit event
            conn.execute(
                "INSERT INTO audit_events(event_type, details_json) VALUES(?,?)",
                ("testnet_reset_recovery", json.dumps(export_data, allow_nan=False))
            )
            conn.commit()

        return export_data

    def quick_check(self):
        with closing(self._connect()) as conn:
            return conn.execute("PRAGMA quick_check").fetchone()[0]

    def read_state(self, key):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value_json FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def write_state(self, key, value):
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO state(key,value_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json", (key, encoded))
            conn.commit()

    def read_positions(self):
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT symbol,value_json FROM positions ORDER BY symbol").fetchall()
            return {row["symbol"]: json.loads(row["value_json"]) for row in rows}

    def write_positions(self, positions):
        encoded = [(symbol, json.dumps(value, allow_nan=False, separators=(",", ":"))) for symbol, value in positions.items()]
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM positions")
            conn.executemany("INSERT INTO positions(symbol,value_json) VALUES(?,?)", encoded)
            conn.commit()

    def create_order_intent(self, client_order_id, symbol, side, amount, request):
        side_value = side.upper()
        amount_value = str(amount)
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO order_intents(client_order_id,symbol,side,amount,state,request_json) VALUES(?,?,?,?,?,?)",
                (client_order_id, symbol, side_value, amount_value, "PENDING", json.dumps(request, allow_nan=False)),
            )
            conn.commit()
        try:
            self.append_event("order_status", {
                "symbol": symbol, "side": side_value.lower(), "amount": amount_value, "state": "PENDING",
            })
        except Exception:
            # Audit visibility is best-effort; it must not block a safety-managed order flow.
            pass

    def update_order(self, client_order_id, state, *, exchange_order_id=None, filled=None, average=None, response=None):
        row = self.get_order(client_order_id)
        if not row or state not in self.ORDER_TRANSITIONS.get(row["state"], set()):
            raise ValueError("Invalid or missing order-state transition")
        filled_value = str(filled) if filled is not None else None
        average_value = str(average) if average is not None else None
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE order_intents SET state=?,exchange_order_id=COALESCE(?,exchange_order_id),filled=COALESCE(?,filled),average=COALESCE(?,average),response_json=COALESCE(?,response_json),updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE client_order_id=?",
                (state, exchange_order_id, filled_value, average_value, json.dumps(response, allow_nan=False) if response is not None else None, client_order_id),
            )
            conn.commit()
        details = {
            "symbol": row["symbol"],
            "side": row["side"].lower(),
            "amount": row["amount"],
            "state": state,
        }
        if filled_value is not None:
            details["filled"] = filled_value
        if average_value is not None:
            details["average"] = average_value
        try:
            self.append_event("order_status", details)
        except Exception:
            # Audit visibility is best-effort; the durable order state is authoritative.
            pass

    def get_order(self, client_order_id):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM order_intents WHERE client_order_id=?", (client_order_id,)).fetchone()
            return dict(row) if row else None

    def is_known_client_order(self, client_order_id: str) -> bool:
        if not client_order_id:
            return False
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT 1 FROM order_intents WHERE client_order_id=?", (client_order_id,)).fetchone()
            return row is not None

    def mark_order_applied(self, client_order_id):
        with self._lock, closing(self._connect()) as conn:
            conn.execute("UPDATE order_intents SET applied=1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE client_order_id=? AND state IN ('FILLED','PARTIAL_CLOSED','CANCELED','REJECTED','EXPIRED')", (client_order_id,))
            if conn.total_changes != 1:
                raise ValueError("Only terminal orders can be marked applied")

    def unresolved_orders(self):
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM order_intents WHERE state NOT IN ('FILLED','PARTIAL_CLOSED','CANCELED','REJECTED','EXPIRED') OR applied=0 ORDER BY created_at").fetchall()
            return [dict(row) for row in rows]

    def append_event(self, event_type, details):
        encoded = json.dumps(details, allow_nan=False, separators=(",", ":"))
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO audit_events(event_type,details_json) VALUES(?,?)",
                (event_type, encoded),
            )
            if event_type == "market_check":
                # Keep the market-decision history bounded without pruning order/risk audit records.
                conn.execute("""
                    DELETE FROM audit_events
                    WHERE event_type='market_check'
                      AND id NOT IN (
                          SELECT id FROM audit_events
                          WHERE event_type='market_check'
                          ORDER BY id DESC LIMIT 20000
                      )
                """)
            conn.commit()

    def list_events(self, limit=100, *, event_type=None, exclude_event_type=None):
        """Return recent audit events; callers must sanitize details before exposing them."""
        limit = max(1, min(int(limit), 200))
        where = ""
        params = []
        if event_type is not None:
            where = " WHERE event_type=?"
            params.append(event_type)
        elif exclude_event_type is not None:
            where = " WHERE event_type<>?"
            params.append(exclude_event_type)
        params.append(limit)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT id,event_type,details_json,created_at FROM audit_events{where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "event_type": row["event_type"],
                    "details": json.loads(row["details_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
