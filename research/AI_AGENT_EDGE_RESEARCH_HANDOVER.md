# Handover: find the strongest defensible strategy edge

**For:** independent AI research agent  
**Project:** Trading Lab (`Collomut/trial`)  
**Purpose:** determine whether any strategy has a robust, cost-adjusted edge for this bot and identify the best-supported candidate. This is a research assignment, not authorization to trade live or promise returns.

## Assignment

Independently evaluate a broad, predeclared set of strategy families against the bot's present strategy. Find the candidate with the strongest evidence of a persistent, executable edge **or conclude that none has one**. Do not optimize for trade count, force a winner, or select the strategy with the highest single backtest return.

The project owner wants a careful answer to: “What strategy, if any, has the best chance of producing positive net expectancy after realistic costs and risk controls, and what evidence supports that conclusion?”

## Starting point in this repository

- The active strategy is the combined EMA/RSI/MACD strategy with an ADX 20 gate. Inspect the current code and configuration to confirm the actual runtime rules before testing; do not rely on this description alone.
- Live order placement is disabled. Keep the system on paper/Testnet during this work.
- Prior research did not establish a profitable edge. Read these before designing the study:
  - `research/STRATEGY_REEVALUATION.md`
  - `research/STRATEGY_FAMILY_SCREEN.md`
  - `research/MACRO_DONCHIAN_REVIEW.md`
  - `research/BLUEPRINT_ASSESSMENT.md`
  - `research/FREQUENCY_RESEARCH.md`
  - `research/LOSS_REDUCTION_REVIEW.md`
  - `research/SQUEEZE_CANDIDATE_REVIEW.md`
  - `research/HANDOVER_PULLBACK_BACKTEST.md`
  - `research/README.md`
- Existing research scripts and local historical Binance data are useful starting points, but independently audit their signal timing, fills, accounting, and risk simulation before treating outputs as evidence. The data directory may be git-ignored and absent in a fresh checkout.
- Prior macro-Donchian research was positive in a strategy-only replay but did not reproduce the handover's claimed figures; its results weakened under current loss/drawdown halts. Treat it as one hypothesis among several, not the presumed winner.

## Required strategy families

Pre-register the rules and parameter ranges before evaluating validation/test periods. At minimum compare:

1. The exact current production strategy, with its actual entries, exits, filters, sizing, and safety controls.
2. Simple passive and naïve baselines: buy-and-hold and a no-trade/cash baseline. Where appropriate, include a simple time-series momentum benchmark.
3. Trend following / time-series momentum with volatility-scaled risk and explicit trend exits.
4. Breakout / channel systems, including the existing Donchian candidate, with macro or higher-timeframe filters only if specified before testing.
5. Trend pullback / continuation entries with a defined invalidation and exit.
6. Mean reversion in a clearly defined range regime, with a regime filter and capped risk.
7. Cross-sectional or relative-strength rotation among the supported spot pairs, including realistic rebalance frequency and turnover costs.
8. A small, predeclared regime-aware combination only if individual components show independent, stable value; do not create a complex ensemble merely to fit history.

Use a modest, justified parameter grid. Report how many variants were tried. Do not keep searching a test period after seeing its results. If additional ideas are explored after looking at held-out data, label those results exploratory and obtain a new untouched test period.

## Data and split design

- Confirm source, timestamp convention, candle completeness, missing bars, duplicates, outliers, pair listings, quote volume, and whether symbols were actually tradable throughout each period. Document exclusions and data cleaning.
- Use point-in-time universe membership where possible. The current BTC/ETH/SOL-only archive has survivorship and universe-selection limitations; do not present it as unbiased market-wide evidence.
- Separate chronological development, validation, and final test periods. Prefer rolling walk-forward evaluation across multiple market regimes. Purge overlapping positions/labels at boundaries and apply a suitable embargo so information from adjacent periods cannot leak.
- Fit thresholds, normalization, regime labels, pair selection, and any learned components only on the training window. Freeze them before the next window.
- Report each window separately and aggregate only with a stated method. Do not reset capital at each split and then add the split P&Ls as if they came from one continuous account.
- If the available history has already been inspected by this project or prior agents, state plainly that it is not an untouched holdout. Do not call a reused period “out of sample.”

## Backtest integrity and execution audit

Audit the existing simulator before reuse. Add or repair an independent test if necessary. At minimum verify:

- Signals use only information available at the decision timestamp; indicators are computed with correct warm-up and completed candles. Check for lookahead, future indexing, survivorship, and accidental use of the signal candle's close as a fill.
- Specify order type, decision time, next-bar or delayed fill, spread, fees, slippage, partial fills, minimum notional/quantity, precision, exchange filters, and rejected-order behavior. Use the actual supported spot venue and fee assumptions where available.
- Model gaps and stop execution conservatively. If one OHLC bar touches both stop and target, use a conservative ordering or finer data; explain the choice. Do not call an ATR multiple “uncapped.”
- Apply the same portfolio constraints to every candidate: equity-based sizing, notional caps, maximum positions, pair correlation limits, exposure, cash availability, and exchange constraints.
- Include the actual daily/rolling drawdown controls, consecutive-loss halt, stop/target/trailing behavior, and operator recovery logic in a separate operational replay. Report both strategy-only results and results with the bot's real safeguards; never conflate them.
- Reconcile every simulated trade against raw candles and produce a small auditable trade ledger. Check P&L arithmetic, fee application on both sides, realized/unrealized equity, and drawdown calculations.

## Robustness and risk checks

For every serious candidate, report at least:

- Trade count, exposure/time in market, turnover, average holding time, win rate, average win/loss, expectancy per trade, profit factor, net return, volatility, maximum drawdown, recovery time, and downside tail measure such as expected shortfall.
- Results per chronological window, per pair, per direction if applicable, and by broad market regime. A profitable aggregate must not conceal dependence on one symbol, one year, or a few outlier trades.
- Base, stressed, and severe cost scenarios. Include materially higher fees/slippage, spread widening, delayed entries, missed fills, and conservative stop gaps. Use actual exchange/account fees if known; otherwise show a range and mark it as assumed.
- Parameter-neighbourhood stability: nearby values should not cause the edge to collapse. Report the full neighborhood, not just the selected optimum.
- Trade/order resampling or block-bootstrap / Monte Carlo analysis appropriate for serial dependence. Estimate the distribution of drawdown and losing streaks, not only the best historical path.
- Sensitivity to start dates, bar intervals, and a delayed execution assumption. Avoid treating many correlated parameter variations as independent confirmation.
- Comparison against buy-and-hold and cash after the same costs and exposure assumptions. Explain whether the strategy's return compensates for its drawdown, turnover, and time at risk.

## Selection rules

Define numerical pass/fail criteria before running validation and final-test results. At minimum, a candidate cannot be called viable if it:

- has non-positive net expectancy or profit factor at/below 1 after base costs in the final untouched window;
- turns negative under a plausible stressed-cost scenario or fails in most walk-forward windows;
- relies on a single pair, narrow parameter optimum, or a handful of exceptional trades;
- exceeds the owner's predeclared drawdown/capital-loss limits, or only appears profitable when the real safety halts are disabled;
- cannot be executed with the repository's actual order and risk-management architecture.

Do not relax risk limits or lower thresholds just to pass a trade-frequency target. Frequency is a secondary measure: report it honestly, but optimize risk-adjusted expectancy and robustness first. If no candidate passes, say **no demonstrated edge** and recommend the next evidence-gathering step rather than naming a false winner.

## Deliverables

Provide a concise executive decision plus reproducible artifacts:

1. A ranked comparison table with identical definitions, periods, costs, and portfolio constraints for every strategy, including the current strategy and simple baselines.
2. A clear statement of the winning candidate **or** “none passed,” with why alternatives failed and the uncertainty that remains.
3. A detailed methodology: data provenance, splits, rules fixed before test, parameter-search size, fill/cost assumptions, and risk controls.
4. Per-window and stressed-cost metrics, drawdown/losing-streak distributions, robustness charts/tables, and an auditable sample trade ledger.
5. Reproducible scripts, pinned dependencies if new ones are needed, machine-readable results, and tests for signal timing and accounting. Make scripts fail loudly when required data or columns are missing.
6. A short integration assessment identifying the exact runtime modules/configuration that would need changes, conflicts with the risk manager, migration/rollback needs, and monitoring requirements. Do not modify active strategy or order-routing code as part of the research result.
7. An explicit change record: what was changed, in which files, and whether anything was committed, pushed, or deployed. For this assignment, research artifacts only; no push/deploy or live trading.

## Shadow and Testnet evidence after research

If a candidate passes all offline criteria, recommend—not silently enable—a read-only shadow phase. Log timestamped signals, inputs, intended entry/exit, spread, fees estimate, rejected setups, and hypothetical fills without creating exchange orders. Compare realized market conditions to simulation over multiple regimes and enough independent trades to make uncertainty meaningful. Only then propose a separately reviewed Testnet order-path experiment with strict caps and reconciliation checks. Historical success is not authorization or evidence for live-money use.

## Required final answer format

1. **Decision:** best-supported candidate or no demonstrated edge.
2. **Comparison:** current strategy versus all candidates, including base and stress costs, each time split, and actual-halt behavior.
3. **Why the result is credible:** key leakage, execution, multiple-testing, and robustness checks completed.
4. **What could invalidate it:** limitations, uncertainty, and missing data.
5. **Next step:** concrete shadow/Testnet research step, or what evidence/data is still needed.
6. **Change status:** local only / committed / pushed / deployed, with commit or deployment identifier only if applicable.

Never state that a strategy is “profitable,” “safe,” or “ready for live” based only on a backtest. Distinguish observed historical performance from an estimate of future performance, and provide uncertainty rather than a guaranteed trade count or return.
