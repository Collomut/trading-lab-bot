# Production-readiness fixes — 2026-11-15

**Baseline verified:** `testnet-prelaunch-safeguards` @ `780a72d` (the deployed
commit). Full suite `python tests.py` = **214 tests, OK** (exit 0).

**Commit / push:** committed as `351185d19051f7c3d310b3af87511605484cada1` and
pushed to `origin/testnet-prelaunch-safeguards` (the Railway deploy branch). The
advisory `.github/workflows/ci.yml` change (mypy/bandit) is **not** in this commit
— it remains an uncommitted local change because the available GitHub credential
is an OAuth App token without the `workflow` scope that GitHub requires to modify
workflow files. Pushing it needs a credential with `workflow` scope (or SSH).

## Implemented and tested (this change set)

- **Finding F — smoke-trade fee accounting.** `run_testnet_smoke_trade` now
  resolves the actual entry fee (`_resolve_fee_quote_cost` on the buy) and exit
  fee (on the sell) and passes them into `open_position` / `close_position`.
  The recorded result no longer labels net P&L as `pnl_before_fees`; it reports
  `entry_fee_quote`, `exit_fee_quote`, `accounting_complete`, and `pnl_net_fees`.
  An unpriced entry/exit commission marks the round trip `accounting_complete=False`
  (P&L `None`) instead of being counted as zero. Dashboard and log/overview
  templates now show "fees incomplete" instead of a before-fee figure.
  Files: `bot_engine.py`, `dashboard.py`, `templates/logs.html`,
  `templates/overview.html`.
- **Maker-first post-only entry path (disabled by default).** New
  `POST_ONLY_ENTRIES` toggle (default false) and `_post_only_limit_chase_buy`
  post a post-only limit at the near touch and re-quote instead of crossing the
  spread, eliminating taker fee + spread-crossing slippage. Each re-quote now
  sends a **unique clientOrderId** (Binance rejects a duplicate id after a
  cancel), while the stable parent id journals the intent. Aborts cleanly on a
  slippage-cap breach. Files: `config.py`, `exchange.py`, `bot_engine.py`.
- **Volatility-targeted sizing.** `calculate_parkinson_volatility` and a
  `[0.50, 1.00]` volatility multiplier scale position size inversely with
  realized volatility so dollar-at-risk stays roughly constant across regimes.
  Missing/absent volatility inputs leave prior sizing unchanged (backward
  compatible). File: `risk_manager.py`.
- **Statistical edge verification tool.** `research/verify_statistical_edge.py`
  computes a one-sample t-test vs. a zero-return baseline plus profit factor,
  with an `is_significant_p05` flag that requires p<0.05 AND positive t.
  Research-only; never places orders.
- **CI hardening (advisory, PENDING — not pushed).** A local
  `.github/workflows/ci.yml` change adds `continue-on-error` mypy (via
  `pyproject.toml`) and bandit AST scans. It is not in commit `351185d` (see push
  status above). `requirements.txt` adds scipy (pushed; required by the
  statistical edge tool).
- **Finding L — docs/config alignment.** `.env.example` third-asset (BNB) fee
  note updated to reflect that third-asset commissions are now converted and
  reconciled fail-closed. README watchlist corrected to the default 3-symbol
  universe (BTC/ETH/SOL) with the other five opt-in.

## New tests

- `test_post_only_chase_sends_unique_client_ids_per_requote` (unique clientOrderId
  per re-quote; cancel-on-open; maker fill accepted).
- `test_parkinson_volatility_flat_and_short_series`, `test_position_size_volatility_scaling`.
- `test_verify_statistical_edge_significance`.

## Not completed — requires external access (not code)

- **P0-A GitHub/Railway release controls** (branch protection, required CI,
  Railway source/gate alignment, stale upstream URL): requires GitHub admin and
  Railway project access not available in this environment.
- **P0-B credential rotation** (dashboard token): requires Railway access; no
  secrets are present in the repo or these changes.
- **Finding J reconciliation + 30-day Demo soak**, and any Binance Demo/Testnet
  live checks: require Binance Demo/Testnet API keys and a running deployment.
- **Findings G/H** re-run of unbiased cost-aware strategy evaluation: requires
  the frozen holdout data and a full research pass; the existing reports already
  flag the small sample and reused review slice.

## Conclusion

Engineering corrections in this change set are implemented and covered by the
passing deterministic suite. The remaining gates are **blocked on external
access** (GitHub admin, Railway, Binance Demo credentials) rather than on code.
Strategy profitability remains **inconclusive** pending an unbiased frozen
holdout with adequate trade count; a month of Demo monitoring alone cannot prove
an edge.
