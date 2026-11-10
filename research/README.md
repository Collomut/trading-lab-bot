# Strategy research

This folder contains the reproducible strategy comparison used to decide whether a higher-frequency strategy was justified. The result was negative: no evaluated variant had sufficiently positive evidence to change production entry rules.

## Reproduction

1. Use Python with the application's pinned requirements installed.
2. Run `python research/download_binance_klines.py` to fetch official Binance Spot monthly 1-hour archives for BTCUSDT, ETHUSDT, and SOLUSDT through October 2026. The fetched CSVs are stored under `research/data/`, which is intentionally git-ignored because it is large.
3. Run `python research/backtest_strategy.py` for the current-strategy and gate variants.
4. Run `python research/backtest_pullback.py` for the additional research-only pullback candidate.

The scripts write compact JSON results under `research/results/`. The methodology, assumptions, findings, and limits are summarized in `STRATEGY_REEVALUATION.md`.

Historical simulations do not establish future profitability. They use hourly OHLCV approximations and cannot fully model order-book execution, exchange outages, or intrabar stop paths.

## Supplied state-plus-trigger blueprint

`BLUEPRINT_ASSESSMENT.md` evaluates the 2026-11-10 blueprint against a research-only replay of the current 1-hour Binance Spot history. The candidate is not wired into the production strategy factory and did not pass the historical robustness screen. Reproduce it with `python research/backtest_blueprint.py`.

`FREQUENCY_RESEARCH.md` compares the current rules on 1-hour and 15-minute data and tests a higher-timeframe-filtered 15-minute candidate. None passed the frequency-versus-risk screen; the active application strategy was left unchanged. Reproduce the study with `python research/backtest_frequency.py` after downloading the ignored research data using `python research/download_interval_data.py --interval 15m`.
