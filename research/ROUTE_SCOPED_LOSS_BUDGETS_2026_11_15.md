# Route-scoped loss budgets — 2026-11-15

Change to the consecutive-loss circuit breaker. Before this change, one shared
`consecutive_losses` counter stopped **all** new entries after `MAX_CONSECUTIVE_LOSSES`
(3) losing trades regardless of which strategy produced them. That meant a run of
losing experimental downtrend-recovery trades could halt the independent uptrend
EMA72 route (flagged in `research/REGIME_ROUTER_REVIEW.md`).

## What changed

`RiskManager` now keeps a loss streak **per route**:

| `exit_policy` | route key | halt scope |
|---|---|---|
| `ema72_signal` | `uptrend` | blocks only uptrend entries |
| `downtrend_rsi_recovery` | `downtrend` | blocks only downtrend entries |
| `risk_managed` (legacy) | `legacy` | original portfolio-wide behavior |

- A route's loss streak resets on a win (unchanged semantics, now per route).
- Reaching the cap halts **only that route**; the portfolio-wide `_is_halted`
  latch and daily/rolling-equity halt are untouched.
- Route state persists in `daily_risk` (survives restarts, with the same
  crash-survival guarantee as the legacy counter).
- `can_open_position(symbol, equity, route=...)` blocks only the named route;
  `bot_engine` passes the route derived from the candidate strategy id.
- Route halts are exposed in `/api/status` (`route_halts`) and cleared via
  `POST /api/clear-route-halts` (requires a clean reconciliation, no unresolved
  orders).

## Profit-factor lever

The 2026-11-15 halt-cap sweep (`research/backtest_halt_cap_sweep.py`) showed the EMA72
route is positive in validation at a **4-loss budget** (PF 1.68, 34 trades) but
flat at the deployed 3 (PF 0, 4 trades). The config ceiling is therefore relaxed
from 3 to 5 for the `macro_donchian_ema72` / `dynamic_regime` profiles while the
**default stays 3** (conservative). The 2026 review slice is still negative at
every cap, so this is a dial, not a claim of profit.

## Tests

`tests.py` additions cover: downtrend losses not halting the uptrend route, the
uptrend route having its own budget, per-route streak reset on a win, route-halt
persistence across restart, route-halt clearing, and the config ceiling. Full
suite: 196 tests pass.
