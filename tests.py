Warning: truncated output (original token count: 75650)
Total output lines: 5669

"""
Comprehensive unit and integration test suite for the Automated Trading Bot.

Covers all core components, strategies, safety mechanisms, persistence, dashboard,
UX wording, and execution safeguards:
  1. TestConfigurationAndValidation: Parameter validation, bounds, non-finite values, pairs, live trading guards.
  2. TestTechnicalIndicatorsAndStrategies: Indicators, crossover signals, consensus, trend & volume filters.
  3. TestRiskManagementAndExecutionLimits: Sizing, break-even & trailing stops, circuit breakers, loss limits, fees.
  4. TestStateStoreAndPersistence: SQLite schema, migrations, order state machine, audit log, backups, resets.
  5. TestExchangeConnectorAndSecurity: CCXT safety, credential redaction, tickers, locks, simulated demo modes.
  6. TestBotEngineOrderLifecycle: Order placement, fills, stop sync, recovery, notifications, emergency stop.
  7. TestDashboardApiAndSecurity: Multi-format auth, CSRF same-origin enforcement, health probes, asset caching.
  8. TestTemplatesWordingAndMobileUx: Plain-English safety, ban on misleading claims, glossary, responsive CSS/HTML.
  9. TestBacktestingAndRealism: Replay warnings, next-bar execution logic, simulation disclaimers.

Run with: python tests.py
"""

import asyncio
import base64
import io
import json
import math
import os
import sqlite3
import sys

# Isolate unit/integration tests from ambient developer .env or shell environment settings
os.environ["BOT_TEST_SUITE"] = "1"
for _k in (
    "STRATEGY", "TIMEFRAME", "LOOKBACK_CANDLES", "REGIME_FILTER_ENABLED", "MIN_TREND_ADX",
    "MAX_ATR_PCT", "EMA72_MAX_ATR_PCT", "EMA72_TAKE_PROFIT_ATR_MULTIPLE",
    "ENABLE_DOWNTREND_RECOVERY_TESTNET", "ENABLE_DOWNTEND_RECOVERY_TESTNET",
    "DOWNTREND_RSI_RECOVERY_LEVEL", "TRADING_PAIRS",
    "MAX_RISK_PER_TRADE", "MAX_OPEN_POSITIONS", "STOP_LOSS_PCT", "TAKE_PROFIT_PCT",
    "MAX_DAILY_LOSS_PCT", "POSITION_SIZE_PCT", "MAX_ROLLING_DRAWDOWN_PCT",
    "MAX_CONSECUTIVE_LOSSES", "STAR…74650 tokens truncated…ls entry and queues a SELL exit (close below EMA72).
            ledger.advance("macro_donchian_ema72", "BTC/USDT",
                           [2, 101.0, 102.0, 100.0, 100.5, 1000.0], "SELL", stop_ref=95.0, atr=1.0)
            self.assertTrue(ledger.has_position("macro_donchian_ema72", "BTC/USDT"))
            # Candle 3 executes the queued exit at its open.
            ledger.advance("macro_donchian_ema72", "BTC/USDT",
                           [3, 105.0, 106.0, 104.0, 105.5, 1000.0], "HOLD", stop_ref=95.0, atr=1.0)
            self.assertEqual(len(ledger.trades), 1)
            self.assertEqual(ledger.trades[0]["reason"], "signal_exit")
            self.assertGreater(ledger.trades[0]["pnl"], 0)

            # Persisted across reconstruction.
            restored = ShadowLedger(store)
            self.assertEqual(len(restored.trades), 1)
            summary = restored.summary("macro_donchian_ema72")
            self.assertEqual(summary["trades"], 1)
            self.assertEqual(summary["wins"], 1)

    def test_summary_profit_factor_and_no_stop_ref_holds(self):
        with TemporaryDirectory() as tmpdir:
            ledger = ShadowLedger(self._store(tmpdir))
            # No stop_ref means no entry can be queued.
            ledger.advance("c", "BTC/USDT", [1, 100.0, 105.0, 99.0, 104.0, 1000.0], "BUY")
            self.assertEqual(len(ledger.pending_entries), 0)
            # Directly seed two trades to exercise the PF computation.
            ledger.trades = [
                {"candidate_id": "c", "pnl": 10.0, "r_multiple": 1.0},
                {"candidate_id": "c", "pnl": -5.0, "r_multiple": -0.5},
            ]
            summary = ledger.summary("c")
            self.assertEqual(summary["profit_factor"], 2.0)
            self.assertEqual(summary["wins"], 1)
            self.assertEqual(summary["losses"], 1)
            self.assertEqual(summary["avg_r_multiple"], 0.25)


if __name__ == "__main__":
    unittest.main()

