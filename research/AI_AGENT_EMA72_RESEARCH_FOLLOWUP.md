# Follow-up handover: verify candidate, then finish strategy search

Use this task together with `research/AI_AGENT_EDGE_RESEARCH_HANDOVER.md`. The goal is to find a robust, executable, cost-adjusted edge—or conclude that none has been demonstrated. Do not force a winner and do not modify or deploy the active strategy.

## New independent evidence to reconcile

The current local worktree contains:

- `research/backtest_ema72_donchian.py`
- `research/results/ema72_donchian_results.json`
- `research/EMA72_DONCHIAN_REVIEW.md`

Important: the replay depends on uncommitted research-simulator changes in `research/backtest_strategy.py`. Do not treat these results as reproducible from the current branch commit until those simulator changes are independently reviewed and included in the research checkout.

The runtime-aligned replay of `D168_M480_E72` found 116 / 171 / 42 trades (train / validation / 2026 review), rather than the 311 / 213 / 47 in `AI_AGENT_STRATEGY_HANDOVER7.md`. A separate 480-bar warmup replay found 209 / 171 / 42, so warmup choice explains part, but not all, of the discrepancy. Reproduce both versions and identify the remaining assumptions causing it. Do not dismiss the discrepancy as a stop-model effect unless controlled runs isolate and quantify it.

Resolve these specific inconsistencies:

1. Handover 7 reports 480-bar EMA warmup but also says 149 contiguous bars are sufficient. This work now requests 2,501 raw candles to yield 2,500 closed, continuous bars and materially reduce recursive EMA seed error. Re-evaluate this warmup against the exact backtest calculation, API limits, and data availability.
2. Specify whether the tested strategy has no take-profit, a 100×ATR target, or another target. Run controlled variants so the effect is clear; “uncapped” must not be used for a finite target.
3. Reconcile stop definition, EMA calculation/initialization, signal timing, split-boundary warmup, next-open fills, and position re-entry semantics against the result files.
4. Re-run the candidate with current daily, peak-drawdown, and consecutive-loss halts, including open-position management after entry halts. Report exact stop time/reason and distinguish these short samples from full-period strategy-only results.
5. Candidate is not an automatic pass: the runtime-aligned replay has 6.44% train base DD / 7.53% train stress DD, 3.32 trades/month in training, and 4.21 trades/month in the 2026 review. Explain the 5% drawdown-control breach and the frequency shortfalls. Do not tune against the already-used 2026 history and call it holdout.

## Complete the comparative study

Audit the actual current runtime strategy and simulator, then follow the broad family and validation requirements in `research/AI_AGENT_EDGE_RESEARCH_HANDOVER.md`. Include current strategy, buy-and-hold/cash baselines, trend/momentum, breakout, pullback, range mean reversion, and cross-sectional rotation where data permits. Freeze candidate rules before validation/test runs. Use identical fees, slippage, fills, position limits, stops, account accounting, and risk controls, and report strategy-only and operational-halt results separately.

At a minimum report per window and per symbol: trades, monthly frequency, PF, net expectancy, net return, exposure, average win/loss, max drawdown and recovery, losing streaks, costs, and stress-cost results. Show parameter-neighbourhood sensitivity and uncertainty using resampling that respects serial dependence. Verify all calculations against an auditable sample trade ledger. Account resets must be explicit; never sum independent split P&Ls into a continuous return.

## Deliverable and change boundary

Provide reproducible scripts and machine-readable outputs, a concise ranked report, all failing gates, remaining data/model uncertainty, and the next safe shadow/Testnet experiment if justified. If no candidate passes predeclared criteria, say **no demonstrated edge**. Make research changes only; no live strategy, risk setting, order-path, commit, push, or deployment changes. Report exactly what files changed and whether they remain local.
