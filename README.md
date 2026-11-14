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

`--once` runs one strategy cycle and may place Testnet orders. The `dynamic_regime` profile selects the EMA72/Donchian candidate after three completed hourly bars confirm an uptrend. In stable downtrends it can evaluate a separate RSI recovery experiment, sized at no more than 0.20% of equity risk per trade with one such position at a time. That experiment has not passed historical cost checks and is for Binance Testnet forward evaluation only; live orders remain disabled. Range, high-volatility, transition, and invalid-data states stay flat. Open positions retain their original exit policy. The generic `--backtest` replay does not model this profile. Continuous paper trading with `--interval 60` may place Testnet orders.

## Railway

- Keep one replica and mount a Volume at `/app/data` with `DATA_DIR=/app/data`.
- Set a long random `DASHBOARD_TOKEN`; keep exchange keys and `DATABASE_URL` in Railway variables.
- Use matching Testnet keys and keep `PAPER_TRADING=true`. Live orders are blocked in this build.
- Startup runs `start.py`, which completes the guarded SQLite-to-Postgres migration check before replacing itself with the dashboard/worker; a migration error prevents startup.
- Account or order reconciliation failures halt trading. Process-monitored exits also depend on the worker and network staying online.
- Railway uses the repository's Railpack configuration. The Procfile, Railway config, and Dockerfile share the same Python startup entrypoint.
- The GitHub Actions workflow compiles the Python sources and runs the deterministic test suite on every push and pull request.

## Operating boundaries

- The configured `dynamic_regime` profile is a guarded Testnet research router, not a validated profitable strategy. It routes stable uptrends to EMA72/Donchian and stable downtrends to a separate low-risk RSI recovery experiment. The recovery experiment failed historical cost checks and is enabled only to collect low-risk Testnet execution evidence; it is not eligible for live orders.
- The EMA72/Donchian candidate was positive in strategy-only historical slices but breached the training drawdown screen and quickly reached existing loss halts. No range or high-volatility strategy passed the cost-adjusted screen, so those regimes are deliberately not traded.
- `python main.py --once` can place Testnet orders when configured with Testnet keys. Do not use it as a read-only health check.
- `python main.py --backtest` is an exploratory replay for legacy strategies; it does not simulate the EMA72 candidate or dynamic routing. Use dedicated research scripts and documented historical data for candidate evaluation.
- SQLite remains the default local store. Setting `DATABASE_URL` enables the guarded Postgres/Supabase store and import check.

## Project notes

- [Current pre-launch review](docs/audits/pre-launch-readiness.md)
- [Strategy research](research/README.md)
- [Testnet limits](docs/testnet_limits.md) and [reset recovery](docs/testnet_reset_recovery.md)
- [Historical audits](docs/audits/archive/)
- [Configuration example](.env.example)
