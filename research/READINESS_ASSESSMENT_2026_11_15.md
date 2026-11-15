# Readiness assessment — 2026-11-15

## Verdict: NOT ready for live trading.

The build is safe (live execution is refused in `ExchangeConnector._create_exchange`,
and `PAPER_TRADING=false` cannot place orders), but the strategy does not meet the
project's own launch gates. The correct next step is exactly what is planned: one
month of uninterrupted demo/Testnet forward-running, then a decision.

## Why not ready (evidence)

1. **Out-of-sample edge not established.** `research/MACRO_DONCHIAN_REVIEW.md`:
   the no-halt replay is positive (PF 1.9/1.7/1.97) but fails the 6–18
   trades/month frequency gate, has higher drawdown than the incumbent, and the
   2026 period is not a pristine holdout. "The strategy-only edge does not
   currently survive the project's deployed halt behavior in these slices."
2. **Fees/net-of-cost not validated** and **no untouched multi-regime study**
   (`docs/audits/pre-launch-readiness.md`, CRITICAL rows).
3. **Operational blockers remain:** stop placement and position persistence are
   not atomic; no safe hot reload; fee/tax accounting is incomplete; no 14-day
   soak evidence; no minimum-size live burn-in. These are process items a demo
   month directly serves.
4. **Win rate (accuracy) is structurally low** — ~31–35% for a 7-day breakout
   trend-follower. Accuracy here comes from payoff asymmetry, not win frequency.

## New research (research/backtest_readiness_analysis.py)

- **Per-symbol, 2026 holdout, no halts:** BTC PF 0.83, ETH 0.75, BNB 0.48 (losers);
  SOL 1.30, XRP 2.08, ADA 1.07, DOGE 1.03, LINK 1.00 (neutral/positive).
- **Universe:** 8-symbol no-halt 2026 = −$98 (PF 0.93); 3-symbol core = +$245
  (PF 1.50). The wider universe is net-negative in the holdout and worsens
  drawdown, consistent with the readiness audit's coin-universe note.
- **Take-profit:** a finite target raises win rate (2.0×ATR → ~60%) but lowers
  profit factor (cuts the trend payoff); the distant 100×ATR backstop is
  PF-optimal for a trend-follower.

## Changes shipped (this pass)

1. **Default universe narrowed to BTC/ETH/SOL** (config + `.env.example` +
   test). The five altcoins remain opt-in via `TRADING_PAIRS`.
2. **`EMA72_TAKE_PROFIT_ATR_MULTIPLE` config** (default 100.0 = uncapped
   backstop) so the operator can dial accuracy (2.0×ATR) vs profit factor.
3. Regression tests updated; full suite passes 197 tests.

## What a demo month must verify (not source-changeable)

- 14 uninterrupted days of the exact deployed build (uptime, memory, cycles).
- A real Testnet round-trip fill with exchange-native stop behavior.
- Fee reconciliation and realized PnL matching the exchange record.
- That the narrower 3-symbol universe actually produces the modeled PF/accuracy.
