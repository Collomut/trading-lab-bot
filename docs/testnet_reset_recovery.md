# Binance Spot Testnet Periodic Reset Recovery Runbook (F-09)

## Overview
Binance Spot Testnet periodically resets account balances, orders, and trading history back to initial test defaults.
When a reset occurs:
1. The remote exchange orders and open positions disappear on Binance Spot Testnet.
2. The bot's persistent SQLite database on Railway holds durable records of previous positions and protective stop order IDs.
3. The fail-closed reconciliation engine (`_reconcile_exchange_state()`) immediately halts all trading cycles with `Exchange reconciliation failed` because the exchange stops and balances no longer match the local database.

## Safe Recovery Procedure

**NEVER blindly delete the SQLite database `state.db` on Railway.**
Deleting the database erases unresolved order intent journals, risk circuit breaker history, and audit trails.

### Step 1: Verify the Testnet Reset
Check the bot dashboard or API diagnostic:
- Run `GET /api/account-diagnostic`.
- Review the Railway deployment logs.
- Log into Binance Spot Testnet web UI to verify that balances have been restored to default virtual funds (e.g. 10,000 USDT) and open orders are empty.

### Step 2: Perform Snapshot & Reset via StateStore
The system provides `snapshot_and_reset_for_testnet_wipe()` in `state_store.py`:
1. It creates an immutable, timestamped SQLite backup file (`state_backup_<timestamp>.db`).
2. It exports all tracked pre-reset positions and unresolved orders to JSON with operator metadata.
3. It appends an official audit event (`testnet_reset_recovery`) to the persistent audit log.
4. It safely transitions open orders to `CANCELED` and clears stale open positions and old baseline markers.

You can execute this in a one-liner via Railway CLI or python script:
```python
from state_store import StateStore
store = StateStore("/app/data/state.db") # or local persistence path
summary = store.snapshot_and_reset_for_testnet_wipe("Binance Spot Testnet monthly reset confirmed")
print(f"Backup created at: {summary['backup_file']}")
```

### Step 3: Re-baseline and Resume
Once the snapshot and reset completes:
1. Trigger `POST /api/resume` or restart the Railway service.
2. The bot performs a fresh baseline balance snapshot of the new virtual Testnet funds.
3. Reconciliation succeeds, and paper trading resumes safely.
