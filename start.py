"""Run the guarded storage preflight, then replace this process with the dashboard."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from migrate_sqlite_to_postgres import main as migrate_storage


def main() -> int:
    migration_status = migrate_storage()
    if migration_status != 0:
        return migration_status

    dashboard = Path(__file__).with_name("dashboard.py")
    os.execv(sys.executable, [sys.executable, str(dashboard)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
