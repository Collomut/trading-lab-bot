# Strategy research

This folder contains reproducible strategy comparisons. None establish a live-trading edge. A weak downtrend candidate is enabled on the current Binance Demo service only as a bounded forward-observation experiment; it does not change live-order eligibility.

`REGIME_ROUTER_REVIEW.md` documents the guarded `dynamic_regime` paper router. Its executable allowlist contains `macro_donchian_ema72` for stable uptrends and the opt-in `downtrend_rsi_recovery_testnet` candidate for stable downtrends. The recovery route uses one fifth of normal risk per trade and at most one open recovery position; it remains an unvalidated Demo/Testnet experiment because its historical training and validation replays lost money and stressed validation costs were negative. Range, high-volatility, transition, and invalid-data conditions still route to no new entry. No route has been established as profitable or ready for live orders. The current Railway Demo opt-in and its reason are recorded in `ACTIVE_STRATEGY_REVIEW_2026-11-20.md`.

`backtest_downtrend_rsi_recovery.py` compares the recovery candidate on the three-pair and eight-pair universes. `backtest_rsi_recovery_thresholds.py` screens RSI recovery levels 30, 35, 40, 45, and 50 with the same costs and risk rules; the results are in `results/rsi_recovery_thresholds.json`. The eight-pair universe created more historical opportunities in some periods, but did not produce a robust profitable result. The broader list is used for paper observation only; it is not evidence that the strategy is profitable.

`backtest_range_rsi_mean_reversion.py` (2026-11-15) replays a pre-registered range-regime RSI mean-reversion candidate for the previously empty "range" regime (stable ADX≤18/ATR≤3% range, RSI14 cross up through 30, RSI≥50/EMA20 exits, 0.50× risk). It replayed net-negative on the eight-coin universe and in the 2026 review, so `range` still routes to no entry; results are in `results/range_rsi_mean_reversion_results.json`.

## Reproduction

1. Use Python with the application's pinned requirements installed.
2. Run `python research/download_binance_klines.py` to fetch official Binance Spot monthly 1-hour archives for the three-pair and eight-pair universes through October 2026. The fetched CSVs are stored under `research/data/`, which is intentionally git-ignored because it is large.
3. Run `python research/backtest_strategy.py` for the current-strategy and gate variants.
4. Run `python research/backtest_pullback.py` for the additional research-only pullback candidate.
5. Run `python research/backtest_macro_trend_pullback.py` for the active-pullback chronology, three/eight-pair universe, and loss-halt sensitivity.
6. Run `python research/backtest_downtrend_rsi_recovery.py` for the exact RSI-25 recovery route configured for the limited Demo observation.

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
