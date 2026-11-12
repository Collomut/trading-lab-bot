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

`--once` runs one strategy cycle and may place Testnet orders. The generic `--backtest` replay is for legacy strategies only; the active EMA72/Donchian candidate is rejected by that replay to prevent misleading output. Continuous paper trading with `--interval 60` may place Testnet orders.

## Railway

- Keep one replica and mount a Volume at `/app/data` with `DATA_DIR=/app/data`.
- Set a long random `DASHBOARD_TOKEN`; keep exchange keys and `DATABASE_URL` in Railway variables.
- Use matching Testnet keys and keep `PAPER_TRADING=true`. Live orders are blocked in this build.
- Startup runs the guarded SQLite-to-Postgres migration check before starting the dashboard or trading worker; a migration error prevents startup.
- Account or order reconciliation failures halt trading. Process-monitored exits also depend on the worker and network staying online.
- Railway uses the repository's Railpack configuration. The Procfile, Railway config, and Dockerfile share the same startup command.
- The GitHub Actions workflow compiles the Python sources and runs the deterministic test suite on every push and pull request.

## Operating boundaries

- The current `macro_donchian_ema72` profile is a Testnet research candidate, not a validated profitable strategy.
- `python main.py --once` can place Testnet orders when configured with Testnet keys. Do not use it as a read-only health check.
- `python main.py --backtest` is an exploratory replay for the legacy strategies; it does not simulate the active EMA72/Donchian candidate. Use the dedicated research scripts and documented historical data for candidate evaluation.
- SQLite remains the default local store. Setting `DATABASE_URL` enables the guarded Postgres/Supabase store and import check.

## Project notes

- [Current pre-launch review](docs/audits/pre-launch-readiness.md)
- [Strategy research](research/README.md)
- [Testnet limits](docs/testnet_limits.md) and [reset recovery](docs/testnet_reset_recovery.md)
- [Historical audits](docs/audits/archive/)
- [Configuration example](.env.example)
