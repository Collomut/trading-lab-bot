# Trading Lab

Python crypto market research and paper-trading bot.

> **Paper/Testnet only. Live order placement is disabled.** The strategy has not demonstrated a profitable edge; backtests are research, not a forecast.

## Run locally

1. Install dependencies: `python -m pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and add Binance **Spot Testnet** keys. Keep secrets out of GitHub and chat.
3. Start with a paper/Testnet check:

```bash
python tests.py
python main.py --once
```

`--once` runs one strategy cycle and may place Testnet orders. For research, run `python main.py --backtest --timeframe 1h`. For continuous paper trading, run `python main.py --interval 60`.

## Railway

- Keep one replica and mount a Volume at `/app/data` with `DATA_DIR=/app/data`.
- Set a long random `DASHBOARD_TOKEN`; keep exchange keys and `DATABASE_URL` in Railway variables.
- Use matching Testnet keys and keep `PAPER_TRADING=true`. Live orders are blocked in this build.
- Account or order reconciliation failures halt trading. Process-monitored exits also depend on the worker and network staying online.
- SQLite is the default. `DATABASE_URL` enables the guarded Postgres/Supabase migration.

## Project notes

- [Current pre-launch review](docs/audits/pre-launch-readiness.md)
- [Strategy research](research/README.md)
- [Testnet limits](docs/testnet_limits.md) and [reset recovery](docs/testnet_reset_recovery.md)
- [Historical audits](docs/audits/archive/)
- [Configuration example](.env.example)
