"""One-time, fail-closed import from Railway's SQLite volume to Supabase Postgres.

Railway runs this before starting the dashboard/worker. No DATABASE_URL means
the existing SQLite mode is unchanged. A configured URL requires a successful
import before the app can initialize its Postgres state store.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is not configured; keeping SQLite storage.")
        return 0

    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    sqlite_path = data_dir / "bot_state.sqlite3"
    trade_log_path = data_dir / "trades.jsonl"

    try:
        from postgres_state_store import PostgresStateStore
        result = PostgresStateStore.migrate_from_sqlite(
            database_url, sqlite_path, trade_log_path
        )
    except Exception as exc:
        # Never log the connection string or SQL parameters.
        print(
            "Supabase migration failed; startup is blocked and SQLite data was retained "
            f"({type(exc).__name__}). Check the Railway and Supabase configuration."
        )
        return 1

    print(
        "Supabase migration "
        f"{result['status']}: imported counts={result['counts']}; "
        f"SQLite backup={result['backup']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
