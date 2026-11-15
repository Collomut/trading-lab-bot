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

`--once` runs one strategy cycle and may place Testnet orders. The default `macro_donchian_ema72` profile is the EMA72/Donchian candidate (7-day breakout above EMA480 with an entry-only ATR14 ceiling of 2.5% of price, exit on a completed 1-hour close below EMA72) with a prior-5-bar swing stop floored at 4% and a distant 100×ATR backstop target. It is the highest profit-factor option in the independent cost-adjusted replay, but it remains a Testnet research candidate, not a validated edge, and live orders stay disabled. The alternative `dynamic_regime` profile adds a three-closed-bar regime gate that routes stable uptrends to EMA72/Donchian and stable downtrends to a separate RSI recovery experiment (≤0.20% of equity risk per trade, one position); that router was worse on profit factor and drawdown in train and validation, and the recovery experiment has not passed historical cost checks. The default Testnet watchlist is BTC, ETH, and SOL against USDT (the three symbols with a validated positive edge in the cost-adjusted replay). BNB, XRP, ADA, DOGE, and LINK remain opt-in via `TRADING_PAIRS`; the wider 8-symbol universe was net-negative in the 2026 holdout and materially worsened drawdown. Open positions retain their original exit policy. The generic `--backtest` replay does not model this profile. Continuous paper trading with `--interval 60` may place Testnet orders.

## Railway

- Deploy from the `testnet-prelaunch-safeguards` branch. To auto-deploy on push, connect the GitHub repository `Collomut/trading-lab-bot` (project → Settings → Source → GitHub) and set that branch as the deploy branch. Pushing to it then triggers a Railpack rebuild and deploy automatically.
- Keep one replica and mount a Volume at `/app/data` with `DATA_DIR=/app/data`.
- Set a long random `DASHBOARD_TOKEN`; keep exchange keys and `DATABASE_URL` in Railway variables (never in the repo). Required variables mirror `.env.example`: `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `DASHBOARD_TOKEN`, and `PAPER_ENVIRONMENT`/`PAPER_TRADING` for Testnet.
- Use matching Testnet keys and keep `PAPER_TRADING=true`. Live orders are blocked in this build.
- Startup runs `start.py`, which completes the guarded SQLite-to-Postgres migration check before replacing itself with the dashboard/worker; a migration error prevents startup.
- Account or order reconciliation failures halt trading. Process-monitored exits also depend on the worker and network staying online.
- Railway uses the repository's Railpack configuration with Infrastructure as
  Code in `.railway/railway.ts` (the deprecated `railway.json` was removed).
  The Procfile and Dockerfile share the same Python startup entrypoint.
- The GitHub Actions workflow compiles the Python sources and runs the deterministic test suite on every push and pull request.

## Operating boundaries

- The default `macro_donchian_ema72` profile is a guarded Testnet research candidate, not a validated profitable strategy. The optional `dynamic_regime` profile routes stable uptrends to EMA72/Donchian and otherwise stands aside; its stable-downtrend RSI recovery experiment replayed net-negative (PF < 1) in every historical slice and is therefore entry-disabled by default (opt-in via `ENABLE_DOWNTEND_RECOVERY_TESTNET=true`).
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
