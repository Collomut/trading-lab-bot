# Strategy research

This folder contains the reproducible strategy comparison used to decide whether a higher-frequency strategy was justified. The result was negative: no evaluated variant had sufficiently positive evidence to change production entry rules.

`REGIME_ROUTER_REVIEW.md` documents the guarded `dynamic_regime` Testnet router. Its executable allowlist contains `macro_donchian_ema72` for stable uptrends and `downtrend_rsi_recovery_testnet` for stable downtrends. The recovery route uses one fifth of the configured risk per trade and one open recovery position at most; it remains an unvalidated Testnet experiment because its historical stress-cost checks lost money. Range, high-volatility, transition, and invalid-data conditions still route to no new entry. No route has been established as profitable or ready for live orders.

`backtest_downtrend_rsi_recovery.py` compares the recovery candidate on the three-pair and eight-pair universes. `backtest_rsi_recovery_thresholds.py` screens RSI recovery levels 30, 35, 40, 45, and 50 with the same costs and risk rules; the results are in `results/rsi_recovery_thresholds.json`. The eight-pair universe created more historical opportunities in some periods, but did not produce a robust profitable result. The broader list is enabled on Binance Testnet only to gather prospective observations under the same strict risk controls.

## Reproduction

1. Use Python with the application's pinned requirements installed.
2. Run `python research/download_binance_klines.py` to fetch official Binance Spot monthly 1-hour archives for BTCUSDT, ETHUSDT, and SOLUSDT through October 2026. The fetched CSVs are stored under `research/data/`, which is intentionally git-ignored because it is large.
3. Run `python research/backtest_strategy.py` for the current-strategy and gate variants.
4. Run `python research/backtest_pullback.py` for the additional research-only pullback candidate.

The scripts write compact JSON results under `research/results/`. The methodology, assumptions, findings, and limits are summarized in `STRATEGY_REEVALUATION.md`.

Historical simulations do not establish future profitability. They use hourly OHLCV approximations and cannot fully model order-book execution, exchange outages, or intrabar stop paths. The 2025–2026 periods have already been reviewed in multiple candidate screens and must not be described as untouched holdout data. New candidates need a frozen specification, reproducible same-simulator comparison, and prospective Testnet observation before they can be added to the executable route allowlist.

## Supplied state-plus-trigger blueprint

`BLUEPRINT_ASSESSMENT.md` evaluates the 2026-11-10 blueprint against a research-only replay of the current 1-hour Binance Spot history. The candidate is not wired into the production strategy factory and did not pass the historical robustness screen. Reproduce it with `python research/backtest_blueprint.py`.

`FREQUENCY_RESEARCH.md` compares the current rules on 1-hour and 15-minute data and tests a higher-timeframe-filtered 15-minute candidate. None passed the frequency-versus-risk screen. Reproduce the study with `python research/backtest_frequency.py` after downloading the ignored research data using `python research/download_interval_data.py --interval 15m`.

`LOSS_REDUCTION_REVIEW.md` records the follow-up Testnet-only change to the default minimum ADX entry gate (18 → 20), its historical results, remaining limitations, and a November trade-count estimate. Reproduce the comparison with `python research/backtest_loss_reduction.py` and `python research/estimate_month_end.py`.

`SQUEEZE_CANDIDATE_REVIEW.md` records an independent replay of the supplied Keltner squeeze breakout specification. It failed cost-adjusted validation and remains research-only. Reproduce it with `python research/backtest_squeeze.py`; it uses the local, git-ignored Binance hourly archive.

`MACRO_DONCHIAN_REVIEW.md` independently replays the macro-filtered Donchian candidate from the 2026-11-12 handover. Results were positive without halts, but did not match the supplied report and failed the minimum trade-frequency gate in validation and 2026. The candidate remains research-only. Reproduce with `python research/backtest_macro_donchian.py`.

`HANDOVER_REVIEW_2026-11-14.md` records the independent full-history and chronological replay of the latest pullback/ATR-stop handover. All evaluated variants lost after modeled fees and slippage; none were added to the order path. Reproduce with `python research/backtest_handover_pullback.py`.

`EMA72_DONCHIAN_REVIEW.md` independently replays the follow-up EMA72 exit candidate. It outperformed the current strategy in no-halt historical slices but exceeded the 5% drawdown limit in training, traded below the proposed frequency floor in the 2026 review, and was halt-truncated in validation and 2026. Its counts do not match the supplied handover and require reconciliation. Reproduce with `python research/backtest_ema72_donchian.py`. `AI_AGENT_EMA72_RESEARCH_FOLLOWUP.md` gives the other research agent the exact follow-up checks.
