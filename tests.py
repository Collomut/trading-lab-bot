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
    "MAX_CONSECUTIVE_LOSSES", "START_PAUSED", "POST_ONLY_ENTRIES", "ENABLE_NOTIFICATIONS",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SMTP_HOST", "SMTP_PORT", "SMTP_USER",
    "SMTP_PASSWORD", "ALERT_EMAIL", "RESEND_API_KEY", "SENDGRID_API_KEY", "ALERT_EMAIL_FROM",
    "DISCORD_WEBHOOK_URL", "DASHBOARD_TOKEN", "DATA_DIR",
    "MAX_SPREAD_BPS", "MAX_SLIPPAGE_BPS", "MAX_PAIRWISE_CORRELATION",
    "MAX_TESTNET_PRICE_DIVERGENCE_PCT", "MAX_SIGNAL_FILL_DIVERGENCE_BPS",
    "EXCHANGE", "API_KEY", "API_SECRET", "DEMO_API_KEY", "DEMO_API_SECRET",
    "PAPER_TRADING", "PAPER_ENVIRONMENT", "FALLBACK_TO_SIMULATION", "ALLOW_LIVE_TRADING",
    "LOG_LEVEL", "LOG_FILE",
):
    os.environ.pop(_k, None)
import threading
import time
import urllib.error
import unittest
from contextlib import closing, nullcontext, redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import ccxt
import numpy as np
import pandas as pd
from aiohttp.test_utils import make_mocked_request
from aiohttp.web import HTTPNotFound

from bot_engine import TradingBot
from config import BotConfig, ExchangeConfig, RiskConfig, StrategyConfig
from dashboard import LOGIN_HTML, WebDashboard, verify_token, _friendly_account_reconciliation_issue
from exchange import (
    AccountAccessError,
    ExchangeConnector,
    account_access_message,
    extract_ccxt_response_metadata,
    filter_closed_ohlcv,
    validate_candle_sequence,
)
from main import run_backtest
from risk_manager import Position, RiskManager, calculate_parkinson_volatility, normalize_trade_records
from state_store import StateStore
from shadow_ledger import ShadowLedger
from strategies import (
    CombinedStrategy,
    EMA_Crossover_Strategy,
    MACD_Strategy,
    RSI_Mean_Reversion_Strategy,
    DowntrendRsiRecoveryTestnetStrategy,
    Signal,
    TradeSignal,
    get_strategy,
    ohlcv_to_dataframe,
)
from shadow_strategy import evaluate_shadow_candidate
from regime_router import REGIME_ROUTE_POLICY, evaluate_regime_router


class TestShadowLogSafety(unittest.TestCase):
    def test_order_activity_log_exposes_origin_without_sensitive_fields(self):
        safe = WebDashboard._safe_log_details("order_status", {
            "symbol": "BTC/USDT", "side": "buy", "state": "FILLED",
            "source": "manual", "api_key": "must not pass",
        })
        self.assertEqual(safe["source"], "manual")
        self.assertNotIn("api_key", safe)

        safe_manual_entry = WebDashboard._safe_log_details("manual_demo_buy_completed", {
            "symbol": "BTC/USDT", "entry_price": 100, "amount": 1,
            "stop_protected": True, "secret": "must not pass",
        })
        self.assertTrue(safe_manual_entry["stop_protected"])
        self.assertNotIn("secret", safe_manual_entry)

    def test_email_delivery_log_is_allowlisted_and_explains_provider_status(self):
        safe = WebDashboard._safe_log_details("notification_delivery", {
            "channel": "email", "status": "accepted_by_provider", "category": "safety_halt",
            "recipient": "private@example.com", "password": "must not pass",
        })
        self.assertEqual(safe["channel"], "email")
        self.assertEqual(safe["status"], "accepted_by_provider")
        self.assertIn("does not confirm inbox placement", safe["message"])
        self.assertNotIn("recipient", safe)
        self.assertNotIn("password", safe)

    def test_reconciliation_log_shows_safe_reason_and_hides_unrecognized_details(self):
        safe = WebDashboard._safe_log_details("reconciliation_failed", {
            "reason": "Exchange reconciliation failed: persisted exchange stop for XRP/USDT is missing or not fully filled",
        })
        self.assertIn("persisted exchange stop", safe["message"])

        diagnostic = WebDashboard._safe_log_details("reconciliation_failed", {
            "reason": "Exchange reconciliation failed: unresolved local order intent exists (invalid_request_record)",
        })
        self.assertIn("invalid_request_record", diagnostic["message"])

        unsafe = WebDashboard._safe_log_details("reconciliation_failed", {
            "reason": "Exchange reconciliation failed: traceback with credential=private-value",
        })
        self.assertEqual(
            unsafe["message"],
            "Account reconciliation failed; new entries remained blocked.",
        )

    def test_volume_breakout_observer_detects_predeclared_setup(self):
        candles = []
        for index in range(260):
            close = 80 + 0.075 * index + 0.35 * math.sin(index * 0.55)
            candles.append([index * 3_600_000, close - 0.05, close + 0.5, close - 0.5, close, 100])
        candles[-1] = [candles[-1][0], 99.4, 101.1, 99.3, 100.8, 150]
        result = evaluate_shadow_candidate(candles[-125:])
        self.assertEqual(result["signal"], "BUY")
        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(result["indicators"]["adx"], 20)
        self.assertGreaterEqual(result["indicators"]["volume_ratio"], 1.2)

    def test_research_shadow_data_is_omitted_from_user_facing_market_logs(self):
        safe = WebDashboard._safe_log_details("market_check", {
            "symbol": "BTC/USDT", "signal": "HOLD", "shadow": {
                "candidate_id": "volume_breakout_24h_ema100_v1", "signal": "BUY", "status": "ok",
                "reason": "breakout", "private": "must not pass",
                "indicators": {"adx": 25, "close": 100, "secret": "must not pass"},
            },
            "indicators": {
                "strategy": "macro_donchian_ema72", "close": 100.0, "ema480": 110.0,
                "ema72": 103.0, "donchian_high168": 108.0, "entry_ready": False,
                "secret": "must not pass",
            },
            "measured_spread_bps": 12.5, "top_bid": 100.0, "top_ask": 100.25,
            "spread_sample_status": "ok", "private": "must not pass",
        })
        self.assertNotIn("shadow", safe)
        self.assertEqual(safe["measured_spread_bps"], 12.5)
        self.assertEqual(safe["spread_sample_status"], "ok")
        self.assertNotIn("private", safe)
        self.assertEqual(safe["indicators"]["strategy"], "macro_donchian_ema72")
        self.assertEqual(safe["indicators"]["ema480"], 110.0)
        self.assertEqual(safe["indicators"]["donchian_high168"], 108.0)
        self.assertNotIn("secret", safe["indicators"])

        routed = WebDashboard._safe_log_details("market_check", {
            "regime_router": {
                "regime": "downtrend", "route_state": "experimental_testnet_candidate",
                "selected_strategy": "downtrend_rsi_recovery_testnet",
                "entry_route_enabled": True,
                "reason": "Testnet route only", "private": "must not pass",
            },
            "indicators": {"strategy": "downtrend_rsi_recovery_testnet", "rsi": 42, "ema20": 99, "private": "omit"},
        })
        self.assertEqual(routed["regime_router"]["selected_strategy"], "downtrend_rsi_recovery_testnet")
        self.assertTrue(routed["regime_router"]["entry_route_enabled"])
        self.assertNotIn("private", routed["regime_router"])
        self.assertEqual(routed["indicators"]["rsi"], 42)
        self.assertNotIn("private", routed["indicators"])

        gap_safe = WebDashboard._safe_log_details("market_data_issue", {
            "symbol": "BTC/USDT", "issue": "candle_sequence_gap",
            "expected_timestamp": 1_700_000_000_000,
            "actual_timestamp": 1_700_007_200_000,
            "step_ms": 7_200_000, "missing_intervals": 1,
            "api_key": "must not pass",
        })
        self.assertEqual(gap_safe["missing_intervals"], 1)
        self.assertNotIn("api_key", gap_safe)


class TestShadowRegimeRouter(unittest.TestCase):
    @staticmethod
    def candles(kind="trend"):
        candles = []
        for index in range(2501):
            if kind == "trend":
                close = 100 * (1.001 ** index)
                spread = close * 0.002
            elif kind == "volatile":
                close = 100.0
                spread = 5.0
            elif kind == "downtrend":
                close = 100 * (0.999 ** index)
                spread = close * 0.002
            else:
                close = 100.0
                spread = 0.2
            candles.append([
                1_700_000_000_000 + index * 3_600_000,
                close, close + spread, close - spread, close, 100.0,
            ])
        return candles

    def test_static_candidate_profile_does_not_claim_dynamic_entry_authorization(self):
        result = evaluate_regime_router(self.candles(), "macro_donchian_ema72")
        self.assertEqual(result["regime"], "uptrend")
        self.assertEqual(result["route"], "observe_existing_strategy_only")
        self.assertFalse(result["entry_authorized"])
        self.assertEqual(result["raw_regimes"], ["uptrend"] * 3)

    def test_dynamic_router_selects_regime_specific_testnet_candidates(self):
        uptrend = evaluate_regime_router(self.candles(), "dynamic_regime")
        self.assertEqual(uptrend["status"], "dynamic_testnet")
        self.assertEqual(uptrend["selected_strategy"], "macro_donchian_ema72")
        self.assertTrue(uptrend["entry_route_enabled"])

        downtrend = evaluate_regime_router(self.candles("downtrend"), "dynamic_regime")
        self.assertEqual(downtrend["regime"], "downtrend")
        # Entry-disabled by default: the recovery candidate replayed net-negative.
        self.assertIsNone(downtrend["selected_strategy"])
        self.assertFalse(downtrend["entry_route_enabled"])
        self.assertEqual(downtrend["route_state"], "experimental_testnet_candidate_disabled")
        self.assertEqual(downtrend["candidate_strategy"], "downtrend_rsi_recovery_testnet")
        self.assertIn("PF < 1", downtrend["reason"])

        opted_in = evaluate_regime_router(
            self.candles("downtrend"), "dynamic_regime", enable_downtrend_recovery=True
        )
        self.assertEqual(opted_in["selected_strategy"], "downtrend_rsi_recovery_testnet")
        self.assertTrue(opted_in["entry_route_enabled"])
        self.assertEqual(opted_in["route_state"], "experimental_testnet_candidate")
        self.assertIn("not passed live-market validation", opted_in["reason"])

        sideways = evaluate_regime_router(self.candles("range"), "dynamic_regime")
        self.assertEqual(sideways["selected_strategy"], None)
        self.assertFalse(sideways["entry_route_enabled"])
        self.assertEqual(sideways["route"], "no_validated_strategy")

    def test_router_fails_closed_for_range_and_high_volatility(self):
        range_result = evaluate_regime_router(self.candles("range"), "macro_donchian_ema72")
        volatile_result = evaluate_regime_router(self.candles("volatile"), "macro_donchian_ema72")
        self.assertEqual(range_result["regime"], "range")
        self.assertEqual(range_result["route"], "no_validated_strategy")
        self.assertEqual(volatile_result["regime"], "high_volatility")
        self.assertEqual(volatile_result["route"], "no_validated_strategy")
        self.assertFalse(range_result["entry_authorized"])
        self.assertFalse(volatile_result["entry_authorized"])

    def test_router_rejects_short_and_gapped_history(self):
        short = evaluate_regime_router(self.candles()[:100], "macro_donchian_ema72")
        self.assertEqual(short["status"], "unavailable")
        self.assertEqual(short["regime"], "uncertain")
        self.assertEqual(short["reason_code"], "needs_2500_closed_hourly_candles_received_100")
        self.assertEqual(short["reason"], "Need 2,500 completed hourly candles; received 100.")
        self.assertNotIn("_", short["reason"])
        exact_warmup = evaluate_regime_router(self.candles()[:2500], "dynamic_regime")
        self.assertNotIn("reason_code", exact_warmup)
        self.assertEqual(exact_warmup["status"], "dynamic_testnet")
        self.assertTrue(exact_warmup["entry_route_enabled"])
        gapped = self.candles()
        gapped[100][0] += 3_600_000
        result = evaluate_regime_router(gapped, "macro_donchian_ema72")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["route"], "no_validated_strategy")

    def test_dynamic_policy_is_an_explicit_allowlist_with_one_enabled_candidate(self):
        enabled = [
            (regime, route["strategy"])
            for regime, route in REGIME_ROUTE_POLICY.items()
            if route["entry_enabled"]
        ]
        self.assertEqual(enabled, [("uptrend", "macro_donchian_ema72")])
        # The downtrend candidate exists but is entry-disabled by default (opt-in only).
        self.assertEqual(
            REGIME_ROUTE_POLICY["downtrend"]["strategy"], "downtrend_rsi_recovery_testnet"
        )
        self.assertFalse(REGIME_ROUTE_POLICY["downtrend"]["entry_enabled"])
        for regime in ("range", "high_volatility", "transition"):
            self.assertIsNone(REGIME_ROUTE_POLICY[regime]["strategy"])
            self.assertFalse(REGIME_ROUTE_POLICY[regime]["entry_enabled"])

        uptrend = evaluate_regime_router(self.candles(), "dynamic_regime")
        self.assertEqual(uptrend["route_state"], "testnet_candidate")
        self.assertTrue(uptrend["entry_route_enabled"])
        for kind, expected_state in (
            ("range", "no_validated_candidate"),
            ("volatile", "risk_blocked"),
        ):
            routed = evaluate_regime_router(self.candles(kind), "dynamic_regime")
            self.assertEqual(routed["route_state"], expected_state)
            self.assertFalse(routed["entry_route_enabled"])
            self.assertIsNone(routed["selected_strategy"])

    def test_downtrend_recovery_requires_confirmed_price_context_and_rsi_cross(self):
        close = [100.0 * (0.999 ** i) for i in range(2500)]
        idx = pd.date_range("2020-01-01", periods=2500, freq="h")
        df = pd.DataFrame({"close": close, "high": [v * 1.002 for v in close],
                           "low": [v * 0.998 for v in close]}, index=idx)
        strategy = DowntrendRsiRecoveryTestnetStrategy()
        with patch("strategies.RSIIndicator") as rsi_mock:
            rsi_mock.return_value.rsi.return_value = pd.Series([38.0] * 2498 + [24.0, 26.0])
            signal = strategy.analyze(df, "BTC/USDT")
        self.assertEqual(signal.signal, Signal.BUY)
        self.assertEqual(signal.indicators["risk_multiplier"], 0.20)
        self.assertEqual(signal.indicators["strategy"], "downtrend_rsi_recovery_testnet")
        self.assertEqual(signal.indicators["rsi_recovery_level"], 25.0)
        with patch("strategies.RSIIndicator") as rsi_mock:
            rsi_mock.return_value.rsi.return_value = pd.Series([38.0] * 2499 + [71.0])
            signal = strategy.analyze(df, "BTC/USDT", in_position=True)
        self.assertEqual(signal.signal, Signal.SELL)

    def test_downtrend_recovery_honors_configured_rsi_level(self):
        close = [100.0 * (0.999 ** i) for i in range(2500)]
        idx = pd.date_range("2020-01-01", periods=2500, freq="h")
        df = pd.DataFrame({"close": close, "high": [v * 1.002 for v in close],
                           "low": [v * 0.998 for v in close]}, index=idx)
        strategy = DowntrendRsiRecoveryTestnetStrategy(rsi_recovery_level=40.0)
        # A cross above 25 must NOT trigger when the level is 40.
        with patch("strategies.RSIIndicator") as rsi_mock:
            rsi_mock.return_value.rsi.return_value = pd.Series([38.0] * 2498 + [24.0, 26.0])
            signal = strategy.analyze(df, "BTC/USDT")
        self.assertEqual(signal.signal, Signal.HOLD)
        self.assertEqual(signal.indicators["rsi_recovery_level"], 40.0)
        # A cross above 40 does trigger.
        with patch("strategies.RSIIndicator") as rsi_mock:
            rsi_mock.return_value.rsi.return_value = pd.Series([38.0] * 2498 + [39.0, 42.0])
            signal = strategy.analyze(df, "BTC/USDT")
        self.assertEqual(signal.signal, Signal.BUY)

    def test_downtrend_recovery_rejects_invalid_rsi_level(self):
        with self.assertRaises(ValueError):
            DowntrendRsiRecoveryTestnetStrategy(rsi_recovery_level=9.0)
        with self.assertRaises(ValueError):
            DowntrendRsiRecoveryTestnetStrategy(rsi_recovery_level=46.0)


# ==============================================================================
# 1. Configuration & Parameter Validation Suite
# ==============================================================================

class TestConfigurationAndValidation(unittest.TestCase):
    """Verifies strict configuration bounds, type-safety, and refusal of invalid or dangerous configs."""

    def test_default_config_is_valid(self):
        config = BotConfig()
        config.dashboard_token = "test_secret_token"
        errors = config.validate()
        structural_errors = [e for e in errors if "API_KEY" not in e and "API_SECRET" not in e]
        self.assertEqual(structural_errors, [])

    def test_default_strategy_profile_matches_example_configuration(self):
        strategy = StrategyConfig()
        self.assertEqual(strategy.name, "macro_donchian_ema72")
        self.assertEqual(strategy.timeframe, "1h")
        self.assertEqual(strategy.lookback_candles, 2500)
        self.assertEqual(strategy.trading_pairs, ["BTC/USDT", "ETH/USDT", "SOL/USDT"])
        self.assertEqual(strategy.min_trend_adx, 20.0)
        self.assertEqual(strategy.ema72_take_profit_atr_multiple, 100.0)

    def test_ema72_take_profit_atr_multiple_is_validated(self):
        cfg = BotConfig()
        cfg.dashboard_token = "test_secret_token"
        self.assertEqual(cfg.strategy.ema72_take_profit_atr_multiple, 100.0)
        self.assertEqual([e for e in cfg.validate() if "EMA72_TAKE_PROFIT_ATR_MULTIPLE" in e], [])
        for bad in (0.0, -1.0, 101.0, math.nan, float("inf")):
            cfg.strategy.ema72_take_profit_atr_multiple = bad
            self.assertTrue(
                any("EMA72_TAKE_PROFIT_ATR_MULTIPLE must be a finite value greater than 0 and no more than 100" in e
                    for e in cfg.validate()))

    def test_downtrend_recovery_flag_is_off_by_default_and_requires_dynamic_regime(self):
        self.assertFalse(StrategyConfig().enable_downtrend_recovery_testnet)

        with patch.dict(os.environ, {"ENABLE_DOWNTREND_RECOVERY_TESTNET": "true"}, clear=True):
            self.assertTrue(StrategyConfig().enable_downtrend_recovery_testnet)
        with patch.dict(os.environ, {
            "ENABLE_DOWNTREND_RECOVERY_TESTNET": "false",
            "ENABLE_DOWNTEND_RECOVERY_TESTNET": "true",
        }, clear=True):
            self.assertFalse(StrategyConfig().enable_downtrend_recovery_testnet)
        with patch.dict(os.environ, {"ENABLE_DOWNTEND_RECOVERY_TESTNET": "true"}, clear=True):
            self.assertTrue(StrategyConfig().enable_downtrend_recovery_testnet)

        cfg = BotConfig()
        cfg.dashboard_token = "test_secret_token"
        cfg.strategy.name = "macro_donchian_ema72"
        cfg.strategy.enable_downtrend_recovery_testnet = True
        self.assertTrue(
            any("ENABLE_DOWNTREND_RECOVERY_TESTNET requires STRATEGY=dynamic_regime" in e
                for e in cfg.validate())
        )

        cfg2 = BotConfig()
        cfg2.dashboard_token = "test_secret_token"
        cfg2.strategy.name = "dynamic_regime"
        self.assertFalse(cfg2.strategy.enable_downtrend_recovery_testnet)
        self.assertEqual(
            [e for e in cfg2.validate() if "ENABLE_DOWNTREND_RECOVERY_TESTNET" in e], []
        )

    def test_macro_trend_pullback_config_validation(self):
        cfg = BotConfig()
        cfg.dashboard_token = "test_secret_token"
        cfg.strategy.name = "macro_trend_pullback"
        cfg.strategy.lookback_candles = 600
        structural_errors = [e for e in cfg.validate() if "API_KEY" not in e and "API_SECRET" not in e]
        self.assertEqual(structural_errors, [])

        # Lookback less than 600 fails
        cfg.strategy.lookback_candles = 599
        self.assertTrue(any("LOOKBACK_CANDLES >= 600" in e for e in cfg.validate()))

    def test_downtrend_rsi_recovery_level_is_validated(self):
        cfg = BotConfig()
        cfg.dashboard_token = "test_secret_token"
        self.assertEqual(cfg.strategy.downtrend_rsi_recovery_level, 25.0)
        self.assertEqual(
            [e for e in cfg.validate() if "DOWNTREND_RSI_RECOVERY_LEVEL" in e], [])
        for bad in (9.0, 46.0, math.nan, float("inf")):
            cfg.strategy.downtrend_rsi_recovery_level = bad
            self.assertTrue(
                any("DOWNTREND_RSI_RECOVERY_LEVEL must be a finite value between 10 and 45" in e
                    for e in cfg.validate()))

    def test_consecutive_loss_cap_allows_researched_budget_for_managed_profiles(self):
        cfg = BotConfig()
        cfg.dashboard_token = "test_secret_token"
        cfg.strategy.name = "dynamic_regime"
        self.assertEqual(cfg.risk.max_consecutive_losses, 3)
        self.assertEqual([e for e in cfg.validate() if "MAX_CONSECUTIVE_LOSSES" in e], [])
        # Research (halt-cap sweep) showed the EMA72 route is positive at a 4-loss
        # budget in validation, so 4-5 is allowed; the default stays 3.
        cfg.risk.max_consecutive_losses = 4
        self.assertEqual([e for e in cfg.validate() if "MAX_CONSECUTIVE_LOSSES" in e], [])
        cfg.risk.max_consecutive_losses = 5
        self.assertEqual([e for e in cfg.validate() if "MAX_CONSECUTIVE_LOSSES" in e], [])
        cfg.risk.max_consecutive_losses = 6
        self.assertTrue(any("must be between 1 and 5" in e for e in cfg.validate()))

    def test_ema72_volatility_band_default_and_bounds(self):
        self.assertEqual(StrategyConfig().ema72_max_atr_pct, 2.5)
        cfg = BotConfig()
        cfg.dashboard_token = "test_secret_token"
        self.assertEqual([e for e in cfg.validate() if "EMA72_MAX_ATR_PCT" in e], [])
        for bad in (0.0, -1.0, 5.5, math.nan, float("inf")):
            cfg = BotConfig()
            cfg.dashboard_token = "test_secret_token"
            cfg.strategy.ema72_max_atr_pct = bad
            self.assertTrue(
                any("EMA72_MAX_ATR_PCT" in e for e in cfg.validate()),
                f"Validation failed to reject ema72_max_atr_pct={bad}",
            )

    def test_fallback_simulation_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(ExchangeConfig().fallback_to_simulation)

    def test_non_finite_risk_values_are_rejected(self):
        for field_name in [
            "max_risk_per_trade",
            "stop_loss_pct",
            "take_profit_pct",
            "max_daily_loss_pct",
            "position_size_pct",
            "max_rolling_drawdown_pct",
        ]:
            for bad_val in [math.nan, float("inf"), -float("inf")]:
                cfg = BotConfig()
                setattr(cfg.risk, field_name, bad_val)
                errors = cfg.validate()
                self.assertTrue(
                    any("must be finite" in e for e in errors),
                    f"Validation failed to reject {field_name}={bad_val}"
                )

    def test_extreme_risk_values_are_rejected(self):
        cfg = BotConfig()
        cfg.risk.max_risk_per_trade = 6.0
        cfg.risk.stop_loss_pct = 12.0
        cfg.risk.position_size_pct = 60.0
        errors = cfg.validate()
        self.assertTrue(any("MAX_RISK_PER_TRADE > 5%" in e for e in errors))
        self.assertTrue(any("STOP_LOSS_PCT > 10%" in e for e in errors))
        self.assertTrue(any("POSITION_SIZE_PCT > 50%" in e for e in errors))

    def test_strategy_and_timeframe_validation(self):
        cfg = BotConfig()
        cfg.strategy.name = "nonexistent_strategy"
        cfg.strategy.timeframe = "99m"
        errors = cfg.validate()
        self.assertTrue(any("STRATEGY must be one of" in e for e in errors))
        self.assertTrue(any("TIMEFRAME is unsupported" in e for e in errors))

    def test_ema72_donchian_profile_requires_binance_paper_and_full_lookback(self):
        cfg = BotConfig()
        cfg.dashboard_token = "test-token"
        cfg.strategy.name = "macro_donchian_ema72"
        cfg.strategy.timeframe = "1h"
        cfg.strategy.lookback_candles = 2500
        structural_errors = [e for e in cfg.validate() if "API_KEY" not in e and "API_SECRET" not in e]
        self.assertEqual(structural_errors, [])

        cfg.strategy.lookback_candles = 2499
        self.assertTrue(any("LOOKBACK_CANDLES >= 2500" in e for e in cfg.validate()))
        cfg.strategy.lookback_candles = 2500
        cfg.exchange.paper_trading = False
        self.assertTrue(any("requires Binance Spot Testnet or Binance Demo" in e for e in cfg.validate()))
        cfg.exchange.paper_trading = True
        cfg.exchange.paper_environment = "demo"
        self.assertFalse(any("requires Binance Spot Testnet or Binance Demo" in e for e in cfg.validate()))

    def test_dynamic_regime_profile_requires_binance_paper_and_full_lookback(self):
        cfg = BotConfig()
        cfg.dashboard_token = "test-token"
        cfg.strategy.name = "dynamic_regime"
        cfg.strategy.timeframe = "1h"
        cfg.strategy.lookback_candles = 2500
        structural_errors = [e for e in cfg.validate() if "API_KEY" not in e and "API_SECRET" not in e]
        self.assertEqual(structural_errors, [])

        cfg.strategy.lookback_candles = 2499
        self.assertTrue(any("LOOKBACK_CANDLES >= 2500" in e for e in cfg.validate()))
        cfg.strategy.lookback_candles = 2500
        cfg.exchange.paper_trading = False
        self.assertTrue(any("requires Binance Spot Testnet or Binance Demo" in e for e in cfg.validate()))
        cfg.exchange.paper_trading = True
        cfg.exchange.paper_environment = "demo"
        self.assertFalse(any("requires Binance Spot Testnet or Binance Demo" in e for e in cfg.validate()))

    def test_trading_pairs_validation(self):
        cfg = BotConfig()
        cfg.strategy.trading_pairs = ["BTC/USDT", "BTC/USDT"]
        errors = cfg.validate()
        self.assertTrue(any("must not contain duplicates" in e for e in errors))

        cfg.strategy.trading_pairs = ["BTCUSDT"]
        errors = cfg.validate()
        self.assertTrue(any("BASE/QUOTE format" in e for e in errors))

        cfg.strategy.trading_pairs = ["BTC/EUR"]
        errors = cfg.validate()
        self.assertTrue(any("must be USDT-quoted" in e for e in errors))

    def test_live_trading_is_refused_in_config(self):
        cfg = BotConfig()
        cfg.exchange.paper_trading = False
        cfg.exchange.allow_live_trading = False
        self.assertTrue(any("Live trading is disabled" in e for e in cfg.validate()))

        cfg.exchange.allow_live_trading = True
        self.assertTrue(any("operator confirmation and is not enabled" in e for e in cfg.validate()))

    def test_spread_and_slippage_bounds(self):
        cfg = BotConfig()
        cfg.max_spread_bps = 150.0
        cfg.max_slippage_bps = 0.0
        errors = cfg.validate()
        self.assertTrue(any("MAX_SPREAD_BPS" in e for e in errors))
        self.assertTrue(any("MAX_SLIPPAGE_BPS" in e for e in errors))

    def test_non_finite_risk_configuration_is_rejected(self):
        config = BotConfig(
            exchange=ExchangeConfig(api_key="key", api_secret="secret"),
            risk=RiskConfig(max_risk_per_trade=float("nan")),
            strategy=StrategyConfig(),
            dashboard_token="test-token",
        )
        self.assertTrue(any("MAX_RISK_PER_TRADE must be finite" in e for e in config.validate()))


# ==============================================================================
# 2. Technical Indicators & Strategies Suite
# ==============================================================================

class TestTechnicalIndicatorsAndStrategies(unittest.TestCase):
    """Verifies algorithmic correctness, edge cases, consensus, and regime/volume filters."""

    def setUp(self):
        dates = pd.date_range("2026-01-01", periods=150, freq="15min")
        rng = np.random.default_rng(42)
        prices = [100.0]
        for _ in range(149):
            change = rng.normal(0, 0.5)
            prices.append(max(10.0, prices[-1] + change))

        data = {
            "timestamp": [int(d.timestamp() * 1000) for d in dates],
            "open": prices,
            "high": [p + 0.5 for p in prices],
            "low": [p - 0.5 for p in prices],
            "close": prices,
            "volume": [1000.0 for _ in prices],
        }
        self.df = pd.DataFrame(data)
        self.df["timestamp"] = pd.to_datetime(self.df["timestamp"], unit="ms")
        self.df.set_index("timestamp", inplace=True)

    def test_ema_strategy_runs(self):
        strat = EMA_Crossover_Strategy(9, 21)
        sig = strat.analyze(self.df.copy(), "BTC/USDT")
        self.assertIn(sig.signal, [Signal.BUY, Signal.SELL, Signal.HOLD])

    def test_ema_accepts_recent_confirmed_cross_but_not_stale_cross(self):
        df = self.df.iloc[:30].copy()
        short = pd.Series([9.0] * 27 + [9.0, 11.0, 12.0], index=df.index)
        long = pd.Series([10.0] * len(df), index=df.index)
        with patch("strategies.EMAIndicator", side_effect=[
            type("Indicator", (), {"ema_indicator": lambda self: short})(),
            type("Indicator", (), {"ema_indicator": lambda self: long})(),
        ]):
            signal = EMA_Crossover_Strategy(2, 3).analyze(df.copy(), "BTC/USDT")
        self.assertEqual(signal.signal, Signal.BUY)
        self.assertIn("candle(s) ago", signal.reason)

        stale_short = pd.Series([9.0] * 27 + [11.0, 12.0, 13.0], index=df.index)
        with patch("strategies.EMAIndicator", side_effect=[
            type("Indicator", (), {"ema_indicator": lambda self: stale_short})(),
            type("Indicator", (), {"ema_indicator": lambda self: long})(),
        ]):
            stale = EMA_Crossover_Strategy(2, 3).analyze(df.copy(), "BTC/USDT")
        self.assertEqual(stale.signal, Signal.HOLD)

    def test_rsi_votes_only_on_recovery_through_pullback_level(self):
        df = self.df.iloc[:30].copy()
        rsi_values = pd.Series([50.0] * 28 + [39.0, 41.0], index=df.index)
        with patch("strategies.RSIIndicator") as indicator:
            indicator.return_value.rsi.return_value = rsi_values
            recovered = RSI_Mean_Reversion_Strategy().analyze(df.copy(), "BTC/USDT")
        self.assertEqual(recovered.signal, Signal.BUY)
        self.assertIn("recovered above 40", recovered.reason)

        falling_values = pd.Series([50.0] * 28 + [25.0, 20.0], index=df.index)
        with patch("strategies.RSIIndicator") as indicator:
            indicator.return_value.rsi.return_value = falling_values
            falling = RSI_Mean_Reversion_Strategy().analyze(df.copy(), "BTC/USDT")
        self.assertEqual(falling.signal, Signal.HOLD)

    def test_rsi_strategy_runs(self):
        strat = RSI_Mean_Reversion_Strategy(14)
        sig = strat.analyze(self.df.copy(), "BTC/USDT")
        self.assertIn(sig.signal, [Signal.BUY, Signal.SELL, Signal.HOLD])

    def test_macd_strategy_runs(self):
        strat = MACD_Strategy(12, 26, 9)
        sig = strat.analyze(self.df.copy(), "BTC/USDT")
        self.assertIn(sig.signal, [Signal.BUY, Signal.SELL, Signal.HOLD])

    def test_macd_accepts_recent_confirmed_cross_but_not_stale_cross(self):
        df = self.df.copy()
        signal_line = pd.Series([0.0] * len(df), index=df.index)

        def indicator_for(values):
            indicator = type("Indicator", (), {})()
            indicator.macd = lambda: values
            indicator.macd_signal = lambda: signal_line
            indicator.macd_diff = lambda: values - signal_line
            return indicator

        recent_values = pd.Series([-1.0] * 148 + [1.0, 2.0], index=df.index)
        with patch("strategies.MACD", return_value=indicator_for(recent_values)):
            recent = MACD_Strategy().analyze(df.copy(), "BTC/USDT")
        self.assertEqual(recent.signal, Signal.BUY)

        stale_values = pd.Series([-1.0] * 147 + [1.0, 2.0, 3.0], index=df.index)
        with patch("strategies.MACD", return_value=indicator_for(stale_values)):
            stale = MACD_Strategy().analyze(df.copy(), "BTC/USDT")
        self.assertEqual(stale.signal, Signal.HOLD)

    def test_combined_strategy_exposes_each_vote_and_hold_reason(self):
        signal = CombinedStrategy(use_trend_filter=False).analyze(
            self.df.copy(), "BTC/USDT"
        )
        self.assertEqual(signal.indicators["consensus_required"], 2)
        self.assertEqual(set(signal.indicators["strategy_votes"]), {"ema", "rsi", "macd"})
        for vote in signal.indicators["strategy_votes"].values():
            self.assertIn(vote["signal"], {"BUY", "SELL", "HOLD"})
            self.assertTrue(vote["reason"])

    def test_adx_default_uses_stricter_loss_reduction_threshold(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(StrategyConfig().min_trend_adx, 20.0)
            self.assertEqual(CombinedStrategy().min_trend_adx, 20.0)

    def test_combined_strategy_consensus(self):
        strat = CombinedStrategy(use_trend_filter=False)
        sig = strat.analyze(self.df.copy(), "BTC/USDT")
        self.assertIn(sig.signal, [Signal.BUY, Signal.SELL, Signal.HOLD])

    def test_f21_trade_signal_heuristic_score_formatting(self):
        sig = TradeSignal(Signal.BUY, "BTC/USDT", 0.75, "RSI bounce", {})
        self.assertIn("score: 75%", str(sig))

    def test_ema_crossover_bullish_and_bearish(self):
        strat = EMA_Crossover_Strategy(short_period=5, long_period=15)
        # 40 rows flat at 100, then sharp rise
        prices = [100.0] * 40 + [100.0 + i * 2.0 for i in range(20)]
        df = pd.DataFrame({
            "close": prices,
            "open": prices,
            "high": [p + 1 for p in prices],
            "low": [p - 1 for p in prices],
            "volume": [1000.0] * len(prices)
        })
        sig = strat.analyze(df, "BTC/USDT")
        self.assertIn(sig.signal, [Signal.BUY, Signal.HOLD])
        if sig.signal == Signal.BUY:
            self.assertIn("crossed above", sig.reason)
            self.assertGreater(sig.confidence, 0.0)

    def test_ema_insufficient_data(self):
        strat = EMA_Crossover_Strategy(9, 21)
        df = self.df.iloc[:10].copy()
        sig = strat.analyze(df, "BTC/USDT")
        self.assertEqual(sig.signal, Signal.HOLD)
        self.assertEqual(sig.confidence, 0.0)

    def test_rsi_oversold_and_overbought(self):
        strat = RSI_Mean_Reversion_Strategy(14, oversold=30.0, overbought=70.0)
        # Sharp crash -> falling knife is held (not bought until recovery through 40)
        crash_prices = [100.0 - i * 3.0 for i in range(50)]
        df_crash = pd.DataFrame({
            "close": crash_prices,
            "open": crash_prices,
            "high": crash_prices,
            "low": crash_prices,
            "volume": [1000.0] * 50
        })
        sig_crash = strat.analyze(df_crash, "BTC/USDT")
        self.assertEqual(sig_crash.signal, Signal.HOLD)

        # Pullback recovery through 40 -> triggers BUY
        with patch("strategies.RSIIndicator") as indicator:
            indicator.return_value.rsi.return_value = pd.Series([50.0] * 28 + [38.0, 42.0])
            sig_recover = strat.analyze(df_crash.iloc[:30].copy(), "BTC/USDT")
            self.assertEqual(sig_recover.signal, Signal.BUY)
            self.assertIn("recovered above 40", sig_recover.reason)

        # Sharp rally -> RSI overbought
        rally_prices = [100.0 + i * 3.0 for i in range(50)]
        df_rally = pd.DataFrame({
            "close": rally_prices,
            "open": rally_prices,
            "high": rally_prices,
            "low": rally_prices,
            "volume": [1000.0] * 50
        })
        sig_rally = strat.analyze(df_rally, "BTC/USDT")
        self.assertEqual(sig_rally.signal, Signal.SELL)
        self.assertIn("overbought", sig_rally.reason)

    def test_combined_strategy_trend_filter_blocks_buy_in_downtrend(self):
        strat = CombinedStrategy(use_trend_filter=True, trend_period=50, regime_filter_enabled=False)
        prices = [200.0 - i * 1.5 for i in range(60)]
        df = pd.DataFrame({
            "close": prices,
            "open": prices,
            "high": [p + 0.5 for p in prices],
            "low": [p - 0.5 for p in prices],
            "volume": [1000.0] * 60
        })
        strat.ema.analyze = lambda d, s: TradeSignal(Signal.BUY, s, 0.8, "ema buy", {})
        strat.rsi.analyze = lambda d, s: TradeSignal(Signal.BUY, s, 0.8, "rsi buy", {})
        strat.macd.analyze = lambda d, s: TradeSignal(Signal.HOLD, s, 0.0, "hold", {})

        sig = strat.analyze(df, "BTC/USDT")
        self.assertEqual(sig.signal, Signal.HOLD)
        self.assertIn("BUY BLOCKED by Downtrend filter", sig.reason)

    def test_combined_strategy_does_not_shrink_ema_when_history_is_short(self):
        strat = CombinedStrategy(use_trend_filter=True, trend_period=50, regime_filter_enabled=False)
        prices = [100.0 + i for i in range(40)]
        df = pd.DataFrame({"close": prices, "open": prices, "high": [p + 1 for p in prices],
                           "low": [p - 1 for p in prices], "volume": [1000.0] * len(prices)})
        strat.ema.analyze = lambda d, s: TradeSignal(Signal.BUY, s, 0.8, "buy", {})
        strat.rsi.analyze = lambda d, s: TradeSignal(Signal.BUY, s, 0.8, "buy", {})
        strat.macd.analyze = lambda d, s: TradeSignal(Signal.HOLD, s, 0.0, "hold", {})
        signal = strat.analyze(df, "BTC/USDT")
        self.assertEqual(signal.signal, Signal.HOLD)
        self.assertIn("need 50 completed candles", signal.reason)

    def test_combined_strategy_volume_filter_blocks_buy(self):
        strat = CombinedStrategy(use_trend_filter=False, regime_filter_enabled=False)
        prices = [100.0 + i * 0.1 for i in range(60)]
        volumes = [1000.0] * 59 + [50.0]
        df = pd.DataFrame({
            "close": prices,
            "open": prices,
            "high": [p + 0.5 for p in prices],
            "low": [p - 0.5 for p in prices],
            "volume": volumes
        })
        strat.ema.analyze = lambda d, s: TradeSignal(Signal.BUY, s, 0.8, "ema buy", {})
        strat.rsi.analyze = lambda d, s: TradeSignal(Signal.BUY, s, 0.8, "rsi buy", {})
        strat.macd.analyze = lambda d, s: TradeSignal(Signal.HOLD, s, 0.0, "hold", {})

        sig = strat.analyze(df, "BTC/USDT")
        self.assertEqual(sig.signal, Signal.HOLD)
        self.assertIn("BUY BLOCKED by low volume", sig.reason)

    def test_combined_strategy_sell_never_blocked_by_regime_or_trend(self):
        strat = CombinedStrategy(use_trend_filter=True, regime_filter_enabled=True)
        prices = [200.0 - i * 2.0 for i in range(60)]
        df = pd.DataFrame({
            "close": prices,
            "open": prices,
            "high": [p + 0.5 for p in prices],
            "low": [p - 0.5 for p in prices],
            "volume": [1000.0] * 60
        })
        strat.ema.analyze = lambda d, s: TradeSignal(Signal.SELL, s, 0.8, "ema sell", {})
        strat.rsi.analyze = lambda d, s: TradeSignal(Signal.SELL, s, 0.8, "rsi sell", {})
        strat.macd.analyze = lambda d, s: TradeSignal(Signal.HOLD, s, 0.0, "hold", {})

        sig = strat.analyze(df, "BTC/USDT")
        self.assertEqual(sig.signal, Signal.SELL)
        self.assertIn("SELL CONSENSUS", sig.reason)

    def test_get_strategy_factory(self):
        strat = get_strategy("combined")
        self.assertIsInstance(strat, CombinedStrategy)
        candidate = get_strategy("macro_donchian_ema72")
        self.assertEqual(candidate.__class__.__name__, "MacroDonchianEma72Strategy")
        with self.assertRaises(ValueError):
            get_strategy("unknown_strat")

    def test_ema72_donchian_uses_completed_breakout_and_position_aware_exit(self):
        from strategies import MacroDonchianEma72Strategy

        index = pd.date_range("2025-01-01", periods=2500, freq="1h")
        close = np.full(2500, 100.0)
        high = np.full(2500, 101.0)
        low = np.full(2500, 99.0)
        close[-1], high[-1], low[-1] = 102.0, 103.0, 101.0
        frame = pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": 10.0}, index=index)
        strategy = MacroDonchianEma72Strategy()

        entry = strategy.analyze(frame, "BTC/USDT")
        self.assertEqual(entry.signal, Signal.BUY)
        self.assertGreater(entry.indicators["close"], entry.indicators["donchian_high168"])
        self.assertGreater(entry.indicators["close"], entry.indicators["ema480"])
        self.assertIn("swing_low_5", entry.indicators)

        held = strategy.analyze(frame, "BTC/USDT", in_position=True)
        self.assertEqual(held.signal, Signal.HOLD)

        frame.iloc[-1, frame.columns.get_loc("close")] = 99.0
        frame.iloc[-1, frame.columns.get_loc("high")] = 100.0
        frame.iloc[-1, frame.columns.get_loc("low")] = 98.0
        exit_signal = strategy.analyze(frame, "BTC/USDT", in_position=True)
        self.assertEqual(exit_signal.signal, Signal.SELL)
        self.assertLess(exit_signal.indicators["close"], exit_signal.indicators["ema72"])

    def test_get_strategy_wires_ema72_volatility_band(self):
        candidate = get_strategy("macro_donchian_ema72", ema72_max_atr_pct=1.25)
        self.assertEqual(candidate.max_atr_pct, 1.25)
        routed = get_strategy("dynamic_regime", ema72_max_atr_pct=1.25)
        self.assertEqual(routed.max_atr_pct, 1.25)
        legacy = get_strategy("combined", max_atr_pct=4.0, ema72_max_atr_pct=1.25)
        self.assertEqual(legacy.max_atr_pct, 4.0)

    def test_ema72_donchian_rejects_invalid_volatility_band(self):
        from strategies import MacroDonchianEma72Strategy

        for bad in (0.0, -2.5, math.nan, float("inf")):
            with self.assertRaises(ValueError):
                MacroDonchianEma72Strategy(max_atr_pct=bad)

    def test_ema72_donchian_atr_band_blocks_only_high_volatility_entries(self):
        from strategies import MacroDonchianEma72Strategy

        index = pd.date_range("2025-01-01", periods=2500, freq="1h")
        close = np.full(2500, 100.0)
        high = np.full(2500, 105.0)
        low = np.full(2500, 95.0)
        close[-1], high[-1], low[-1] = 110.0, 111.0, 99.0
        frame = pd.DataFrame({"open": close, "high": high, "low": low, "close": close,
                              "volume": 10.0}, index=index)

        blocked = MacroDonchianEma72Strategy().analyze(frame, "BTC/USDT")
        self.assertEqual(blocked.signal, Signal.HOLD)
        self.assertIn("volatility band", blocked.reason)
        self.assertGreater(blocked.indicators["atr_pct"], blocked.indicators["max_atr_pct"])
        self.assertEqual(blocked.indicators["max_atr_pct"], 2.5)
        self.assertTrue(blocked.indicators["breakout_ready"])
        self.assertFalse(blocked.indicators["entry_ready"])

        allowed = MacroDonchianEma72Strategy(max_atr_pct=12.0).analyze(frame, "BTC/USDT")
        self.assertEqual(allowed.signal, Signal.BUY)

    def test_ema72_donchian_atr_band_never_blocks_exit(self):
        from strategies import MacroDonchianEma72Strategy

        index = pd.date_range("2025-01-01", periods=2500, freq="1h")
        close = np.full(2500, 100.0)
        high = np.full(2500, 105.0)
        low = np.full(2500, 95.0)
        close[-1], high[-1], low[-1] = 90.0, 111.0, 89.0
        frame = pd.DataFrame({"open": close, "high": high, "low": low, "close": close,
                              "volume": 10.0}, index=index)

        signal = MacroDonchianEma72Strategy().analyze(frame, "BTC/USDT", in_position=True)
        self.assertEqual(signal.signal, Signal.SELL)
        self.assertGreater(signal.indicators["atr_pct"], signal.indicators["max_atr_pct"])

    def test_ema72_donchian_holds_on_short_or_gapped_warmup(self):
        from strategies import MacroDonchianEma72Strategy

        index = pd.date_range("2025-01-01", periods=2500, freq="1h")
        values = np.full(2500, 100.0)
        frame = pd.DataFrame({"open": values, "high": values + 1, "low": values - 1,
                              "close": values, "volume": 10.0}, index=index)
        strategy = MacroDonchianEma72Strategy()
        self.assertEqual(strategy.analyze(frame.iloc[:-1], "BTC/USDT").signal, Signal.HOLD)
        frame.index = frame.index[:2450].append(frame.index[2450:] + pd.Timedelta(hours=1))
        self.assertEqual(strategy.analyze(frame, "BTC/USDT").signal, Signal.HOLD)

    def test_macro_trend_pullback_uses_pullback_reclaim_and_ema36_exit(self):
        from strategies import MacroTrendPullbackStrategy

        index = pd.date_range("2025-01-01", periods=600, freq="1h")
        base = np.linspace(50.0, 100.0, 600)
        close = base.copy()
        open_ = base - 0.5
        high = base + 1.0
        low = base - 1.0

        frame = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 100.0}, index=index)
        strategy = MacroTrendPullbackStrategy(max_atr_pct=5.0)

        # Craft explicit pullback:
        # bar -2: low touches below EMA20
        # bar -1: bullish close above open and above EMA9
        ema20_val = frame["close"].ewm(span=20, adjust=False).mean().iloc[-1]
        frame.iloc[-2, frame.columns.get_loc("low")] = ema20_val - 2.0
        frame.iloc[-1, frame.columns.get_loc("open")] = 99.0
        frame.iloc[-1, frame.columns.get_loc("close")] = 101.0
        frame.iloc[-1, frame.columns.get_loc("high")] = 102.0
        frame.iloc[-1, frame.columns.get_loc("low")] = 98.5

        entry = strategy.analyze(frame, "BTC/USDT")
        self.assertEqual(entry.signal, Signal.BUY)
        self.assertTrue(entry.indicators["entry_ready"])
        self.assertTrue(entry.indicators["macro_bullish"])
        self.assertTrue(entry.indicators["pulled_back"])
        self.assertTrue(entry.indicators["reclaimed"])
        self.assertIn("swing_low_5", entry.indicators)

        # In-position: while close > EMA36, HOLD
        held = strategy.analyze(frame, "BTC/USDT", in_position=True)
        self.assertEqual(held.signal, Signal.HOLD)

        # When close drops below EMA36, SELL
        frame.iloc[-1, frame.columns.get_loc("open")] = 84.0
        frame.iloc[-1, frame.columns.get_loc("close")] = 80.0
        frame.iloc[-1, frame.columns.get_loc("low")] = 79.0
        frame.iloc[-1, frame.columns.get_loc("high")] = 85.0
        exit_sig = strategy.analyze(frame, "BTC/USDT", in_position=True)
        self.assertEqual(exit_sig.signal, Signal.SELL)
        self.assertIn("below EMA36", exit_sig.reason)

    def test_macro_trend_pullback_warmup_and_invalid_data(self):
        from strategies import MacroTrendPullbackStrategy

        strategy = MacroTrendPullbackStrategy()
        index = pd.date_range("2025-01-01", periods=599, freq="1h")
        frame = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}, index=index)
        self.assertEqual(strategy.analyze(frame, "BTC/USDT").signal, Signal.HOLD)

        for bad in (0.0, -1.0, math.nan, float("inf")):
            with self.assertRaises(ValueError):
                MacroTrendPullbackStrategy(max_atr_pct=bad)

    def test_get_strategy_wires_macro_trend_pullback(self):
        candidate = get_strategy("macro_trend_pullback", max_atr_pct=2.8)
        self.assertEqual(candidate.__class__.__name__, "MacroTrendPullbackStrategy")
        self.assertEqual(candidate.max_atr_pct, 2.8)


# ==============================================================================
# 3. Risk Management & Execution Limits Suite
# ==============================================================================

class TestRiskManagementAndExecutionLimits(unittest.TestCase):
    """Verifies mathematical position sizing, break-even & trailing stops, fee netting, and circuit breakers."""

    def test_risk_manager_position_sizing(self):
        config = RiskConfig(
            max_risk_per_trade=1.0,
            max_open_positions=3,
            stop_loss_pct=2.0,
            take_profit_pct=3.0,
            max_daily_loss_pct=3.0,
            position_size_pct=10.0,
        )
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(config, persistence_dir=persistence_dir)
            portfolio = 10000.0
            entry_price = 100.0
            sl_price = 98.0
            size = rm.calculate_position_size(portfolio, entry_price, sl_price)
            self.assertAlmostEqual(size, 10.0)

    def test_paper_environment_rebase_keeps_safety_latches_and_loss_streak(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            rm._consecutive_losses = 1
            rm._is_halted = True
            rm._halt_reason = "Manual halt review required"
            rm.rebase_for_paper_environment(5000.0)

            self.assertEqual(rm._daily_loss, 0.0)
            self.assertEqual(rm._daily_starting_equity, 5000.0)
            self.assertEqual(rm._rolling_peak_equity, 5000.0)
            self.assertEqual(rm._consecutive_losses, 1)
            self.assertTrue(rm._is_halted)
            self.assertEqual(rm._halt_reason, "Manual halt review required")

    def test_reviewed_demo_reset_rebases_equity_but_preserves_loss_streak(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            rm._consecutive_losses = 2
            rm._daily_loss = -12.0
            rm._daily_starting_equity = 5476.74
            rm._rolling_peak_equity = 5478.08
            rm.halt("Daily equity drawdown limit reached: 8.70%")

            self.assertTrue(rm.acknowledge_demo_account_reset(5000.0))
            self.assertFalse(rm._is_halted)
            self.assertEqual(rm._daily_starting_equity, 5000.0)
            self.assertEqual(rm._rolling_peak_equity, 5000.0)
            self.assertEqual(rm._daily_loss, 0.0)
            self.assertEqual(rm._consecutive_losses, 2)
            events = rm.store.list_events(event_type="demo_account_reset_reviewed")
            self.assertEqual(len(events), 1)

    def test_demo_reset_review_does_not_clear_non_drawdown_halts(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            rm.halt("Emergency stop requires operator review")
            self.assertFalse(rm.acknowledge_demo_account_reset(5000.0))
            self.assertTrue(rm._is_halted)
            self.assertEqual(rm._halt_reason, "Emergency stop requires operator review")
            self.assertFalse(rm.acknowledge_demo_account_reset(float("nan")))

    def test_demo_reset_review_reconciles_and_leaves_entries_paused(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(
                exchange=SimpleNamespace(paper_trading=True, paper_environment="demo"),
                strategy=SimpleNamespace(trading_pairs=["BTC/USDT"]),
            )
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.risk_manager.store.write_state("paper_environment", "demo")
            bot.risk_manager.store.write_state("baseline_asset_balances", {"BTC": "0.00017982"})
            bot.risk_manager._daily_starting_equity = 5476.74
            bot.risk_manager._rolling_peak_equity = 5478.08
            bot.risk_manager._consecutive_losses = 2
            bot.risk_manager.halt("Daily equity drawdown limit reached: 8.70%")
            bot.exchange = Mock()
            bot.exchange.is_fallback_simulated = False
            bot.exchange.fetch_account_snapshot.return_value = (
                {"total": {"BTC": "0", "USDT": "5000"}}, []
            )
            bot.exchange.exchange.load_markets.return_value = None
            bot.exchange.exchange.market.return_value = {"precision": {"amount": 8}}
            bot.exchange.exchange.precisionMode = 2
            bot.exchange.amount_to_precision.return_value = "1.00000000"
            bot._cycle_lock = threading.Lock()
            bot._last_account_error = ""
            bot._last_reconciliation_ok = False
            bot._last_account_balance = None
            bot.initial_balance = 0.0
            bot.persistence_path = Path(persistence_dir) / "open_positions.json"
            bot.risk_manager.store.write_state("entry_mode", "ACTIVE")

            success, message = bot.review_demo_account_reset()

            self.assertTrue(success, message)
            self.assertFalse(bot.risk_manager._is_halted)
            self.assertTrue(bot._last_reconciliation_ok)
            self.assertEqual(bot.risk_manager.entry_mode(), "EXIT_ONLY")
            self.assertEqual(bot.risk_manager.store.read_state("baseline_asset_balances"), {"BTC": "0"})
            self.assertEqual(bot.risk_manager._daily_starting_equity, 5000.0)
            self.assertEqual(bot.risk_manager._consecutive_losses, 2)
            self.assertTrue(any(
                event["event_type"] == "demo_account_reset_balance_baselined"
                for event in bot.risk_manager.store.list_events(20)
            ))

    def test_demo_reset_review_refuses_unresolved_orders_without_rebasing(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(
                exchange=SimpleNamespace(paper_trading=True, paper_environment="demo"),
                strategy=SimpleNamespace(trading_pairs=["BTC/USDT"]),
            )
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.risk_manager.store.write_state("paper_environment", "demo")
            bot.risk_manager.store.write_state("baseline_asset_balances", {"BTC": "0.00017982"})
            bot.risk_manager.store.create_order_intent("pending-demo-reset", "BTC/USDT", "BUY", "0.01", {"type": "market"})
            bot.risk_manager.halt("Daily equity drawdown limit reached: 8.70%")
            bot._cycle_lock = threading.Lock()
            bot._reconcile_exchange_state = Mock(return_value=True)

            success, message = bot.review_demo_account_reset()

            self.assertFalse(success)
            self.assertIn("unresolved orders", message)
            bot._reconcile_exchange_state.assert_not_called()
            self.assertEqual(bot.risk_manager.store.read_state("baseline_asset_balances"), {"BTC": "0.00017982"})
            self.assertTrue(bot.risk_manager._is_halted)

    def test_demo_reset_review_refuses_exchange_open_orders_without_rebasing(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(
                exchange=SimpleNamespace(paper_trading=True, paper_environment="demo"),
                strategy=SimpleNamespace(trading_pairs=["BTC/USDT"]),
            )
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.risk_manager.store.write_state("paper_environment", "demo")
            bot.risk_manager.store.write_state("baseline_asset_balances", {"BTC": "0.00017982"})
            bot.risk_manager.halt("Daily equity drawdown limit reached: 8.70%")
            bot.exchange = Mock()
            bot.exchange.fetch_account_snapshot.return_value = (
                {"total": {"BTC": "0", "USDT": "5000"}}, [{"id": "unrecognized-order"}]
            )
            bot.exchange.exchange.load_markets.return_value = None
            bot._cycle_lock = threading.Lock()
            bot._last_account_error = ""
            bot._last_reconciliation_ok = False

            success, message = bot.review_demo_account_reset()

            self.assertFalse(success)
            self.assertIn("remains paused", message)
            self.assertEqual(bot.risk_manager.store.read_state("baseline_asset_balances"), {"BTC": "0.00017982"})
            self.assertTrue(bot.risk_manager._is_halted)

    def test_position_sizing_calculation_and_hard_risk_cap(self):
        with TemporaryDirectory() as tmpdir:
            config = RiskConfig(
                max_risk_per_trade=1.0,
                position_size_pct=10.0,
                stop_loss_pct=2.0
            )
            rm = RiskManager(config, persistence_dir=tmpdir)
            size = rm.calculate_position_size(10000.0, 100.0, 98.0)
            self.assertAlmostEqual(size, 10.0)

    def test_experimental_position_sizing_multiplier_reduces_risk(self):
        with TemporaryDirectory() as tmpdir:
            rm = RiskManager(RiskConfig(max_risk_per_trade=1.0, position_size_pct=10.0), persistence_dir=tmpdir)
            standard = rm.calculate_position_size(10000.0, 100.0, 90.0)
            experimental = rm.calculate_position_size(10000.0, 100.0, 90.0, risk_multiplier=0.25)
            self.assertAlmostEqual(standard, 10.0)
            self.assertAlmostEqual(experimental, 2.5)
            self.assertEqual(rm.calculate_position_size(10000.0, 100.0, 90.0, risk_multiplier=0), 0.0)
            fee_adjusted = rm.calculate_position_size(
                10000.0, 100.0, 96.0, risk_multiplier=0.20, entry_fee_rate=0.001,
            )
            self.assertAlmostEqual(fee_adjusted, 20.0 / 4.1)

    def test_downtrend_recovery_position_keeps_fixed_stop_and_target(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            pos = rm.open_position("BTC/USDT", 100.0, 1.0,
                                   stop_loss_override=96.0, take_profit_override=104.0,
                                   exit_policy="downtrend_rsi_recovery")
            self.assertEqual(pos.exit_policy, "downtrend_rsi_recovery")
            self.assertEqual(rm.check_positions(lambda _: 101.5), [])
            self.assertFalse(pos.break_even_activated)
            restored = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            self.assertEqual(restored.open_positions["BTC/USDT"].exit_policy, "downtrend_rsi_recovery")

    def test_position_sizing_with_zero_or_negative_balance(self):
        with TemporaryDirectory() as tmpdir:
            rm = RiskManager(RiskConfig(), persistence_dir=tmpdir)
            self.assertEqual(rm.calculate_position_size(0.0, 100.0, 98.0), 0.0)
            self.assertEqual(rm.calculate_position_size(-500.0, 100.0, 98.0), 0.0)

    def test_max_open_positions_enforcement(self):
        with TemporaryDirectory() as tmpdir:
            config = RiskConfig(max_open_positions=2)
            rm = RiskManager(config, persistence_dir=tmpdir)
            can_open, _ = rm.can_open_position("BTC/USDT", 10000.0)
            self.assertTrue(can_open)
            rm.open_position("BTC/USDT", 50000.0, 0.1)

            can_open, _ = rm.can_open_position("ETH/USDT", 10000.0)
            self.assertTrue(can_open)
            rm.open_position("ETH/USDT", 3000.0, 1.0)

            can_open, reason = rm.can_open_position("SOL/USDT", 10000.0)
            self.assertFalse(can_open)
            self.assertIn("Max positions", reason)

    def test_duplicate_position_prevention(self):
        with TemporaryDirectory() as tmpdir:
            rm = RiskManager(RiskConfig(), persistence_dir=tmpdir)
            rm.open_position("BTC/USDT", 50000.0, 0.1)
            can_open, reason = rm.can_open_position("BTC/USDT", 10000.0)
            self.assertFalse(can_open)
            self.assertIn("Already have an open position in BTC/USDT", reason)

    def test_risk_manager_break_even_and_trailing_stop(self):
        with TemporaryDirectory() as persistence_dir:
            config = RiskConfig(stop_loss_pct=2.0, take_profit_pct=3.0)
            rm = RiskManager(config, persistence_dir=persistence_dir)

            pos = rm.open_position("BTC/USDT", 100.0, 1.0)
            self.assertEqual(pos.stop_loss_price, 98.0)

            rm.check_positions(lambda s: 101.8)
            self.assertTrue(rm.open_positions["BTC/USDT"].break_even_activated)
            self.assertGreaterEqual(rm.open_positions["BTC/USDT"].stop_loss_price, 100.0)

    def test_ema72_position_uses_strategy_target_without_legacy_trailing(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(stop_loss_pct=2.0, take_profit_pct=3.0), persistence_dir=persistence_dir)
            position = rm.open_position(
                "BTC/USDT", 100.0, 1.0,
                stop_loss_override=96.0,
                take_profit_override=1010.0,
                exit_policy="ema72_signal",
            )
            self.assertEqual(position.stop_loss_price, 96.0)
            self.assertEqual(position.take_profit_price, 1010.0)
            self.assertFalse(position.should_take_profit(200.0))
            self.assertTrue(position.should_take_profit(1010.0))

            self.assertEqual(rm.check_positions(lambda _: 110.0), [])
            restored = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            persisted = restored.open_positions["BTC/USDT"]
            self.assertEqual(persisted.exit_policy, "ema72_signal")
            self.assertEqual(persisted.take_profit_price, 1010.0)
            self.assertEqual(persisted.stop_loss_price, 96.0)
            self.assertFalse(persisted.break_even_activated)
            self.assertEqual(restored.check_positions(lambda _: 95.0), [("BTC/USDT", "STOP-LOSS triggered")])

    def test_break_even_protection_one_way_ratchet(self):
        with TemporaryDirectory() as tmpdir:
            rm = RiskManager(RiskConfig(stop_loss_pct=2.0, take_profit_pct=20.0), persistence_dir=tmpdir)
            pos = rm.open_position("BTC/USDT", 100.0, 1.0)
            initial_stop = pos.stop_loss_price
            self.assertEqual(initial_stop, 98.0)

            # Price rises +1.5% to 101.5 -> break-even triggers, stop moves to entry
            to_close = rm.check_positions(lambda sym: 101.5)
            self.assertEqual(to_close, [])
            pos = rm.open_positions["BTC/USDT"]
            self.assertTrue(pos.break_even_activated)
            self.assertEqual(pos.stop_loss_price, 100.0)

            # Price falls back to 100.5 -> stop must NOT move down
            to_close = rm.check_positions(lambda sym: 100.5)
            self.assertEqual(to_close, [])
            self.assertEqual(rm.open_positions["BTC/USDT"].stop_loss_price, 100.0)

    def test_trailing_stop_one_way_ratchet(self):
        with TemporaryDirectory() as tmpdir:
            rm = RiskManager(RiskConfig(take_profit_pct=20.0), persistence_dir=tmpdir)
            pos = rm.open_position("BTC/USDT", 100.0, 1.0)
            # Price climbs to 105.0 -> trailing stop activates (105 * 0.985 = 103.425)
            to_close = rm.check_positions(lambda sym: 105.0)
            self.assertEqual(to_close, [])
            stop_105 = rm.open_positions["BTC/USDT"].stop_loss_price
            self.assertAlmostEqual(stop_105, 103.425)

            # Price climbs higher to 110.0 -> stop moves higher (110 * 0.985 = 108.35)
            to_close = rm.check_positions(lambda sym: 110.0)
            self.assertEqual(to_close, [])
            stop_110 = rm.open_positions["BTC/USDT"].stop_loss_price
            self.assertAlmostEqual(stop_110, 108.35)

            # Price pulls back to 109.0 (> 108.35) -> stop MUST NOT drop
            to_close = rm.check_positions(lambda sym: 109.0)
            self.assertEqual(to_close, [])
            self.assertEqual(rm.open_positions["BTC/USDT"].stop_loss_price, stop_110)

            # Price drops to 107.0 (< 108.35) -> trailing stop triggers exit!
            to_close = rm.check_positions(lambda sym: 107.0)
            self.assertEqual(len(to_close), 1)
            self.assertEqual(to_close[0][0], "BTC/USDT")
            self.assertIn("TRAILING STOP", to_close[0][1])

    def test_daily_loss_circuit_breaker(self):
        with TemporaryDirectory() as tmpdir:
            config = RiskConfig(max_daily_loss_pct=3.0)
            rm = RiskManager(config, persistence_dir=tmpdir)
            rm.monitor_equity(10000.0)
            rm.open_position("BTC/USDT", 100.0, 10.0)

            # Close with $400 loss (4% loss exceeds 3% limit)
            rm.close_position("BTC/USDT", 60.0, "Manual exit")
            can_open, reason = rm.can_open_position("ETH/USDT", 9600.0)
            self.assertFalse(can_open)
            self.assertTrue(rm._is_halted)
            self.assertIn("Daily loss limit reached", rm._halt_reason)

    def test_consecutive_losses_reset_on_win(self):
        with TemporaryDirectory() as tmpdir:
            config = RiskConfig(max_consecutive_losses=3)
            rm = RiskManager(config, persistence_dir=tmpdir)
            # 2 losses
            rm.open_position("BTC/USDT", 100.0, 1.0)
            rm.close_position("BTC/USDT", 90.0, "Stop loss")
            self.assertEqual(rm._consecutive_losses, 1)

            rm.open_position("BTC/USDT", 100.0, 1.0)
            rm.close_position("BTC/USDT", 90.0, "Stop loss")
            self.assertEqual(rm._consecutive_losses, 2)
            self.assertFalse(rm._is_halted)

            # 1 win -> must reset counter to 0
            rm.open_position("BTC/USDT", 100.0, 1.0)
            rm.close_position("BTC/USDT", 110.0, "Take profit")
            self.assertEqual(rm._consecutive_losses, 0)
            self.assertFalse(rm._is_halted)

            # 3 straight losses -> halts!
            for _ in range(3):
                rm.open_position("BTC/USDT", 100.0, 1.0)
                rm.close_position("BTC/USDT", 90.0, "Stop loss")
            self.assertTrue(rm._is_halted)
            self.assertIn("Consecutive loss limit reached", rm._halt_reason)

    def test_consecutive_losses_latch_the_risk_manager(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(max_daily_loss_pct=5.0, max_consecutive_losses=2), persistence_dir=persistence_dir)
            rm.open_position("BTC/USDT", 100.0, 1.0)
            rm.close_position("BTC/USDT", 99.0, "test loss")
            rm.open_position("ETH/USDT", 100.0, 1.0)
            rm.close_position("ETH/USDT", 99.0, "test loss")
            self.assertTrue(rm._is_halted)
            self.assertEqual(rm._consecutive_losses, 2)

    def test_downtrend_losses_do_not_halt_the_uptrend_route(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(max_consecutive_losses=3), persistence_dir=persistence_dir)
            for _ in range(3):
                rm.open_position("ADA/USDT", 100.0, 1.0, exit_policy="downtrend_rsi_recovery")
                rm.close_position("ADA/USDT", 90.0, "stop loss")
            # The portfolio is NOT halted; only the downtrend route is paused.
            self.assertFalse(rm._is_halted)
            self.assertIn("downtrend", rm.route_halts())
            can_downtrend, reason = rm.can_open_position("ADA/USDT", 10000.0, route="downtrend")
            self.assertFalse(can_downtrend)
            self.assertIn("downtrend route entries paused", reason)
            # The uptrend route is untouched and can still enter.
            can_uptrend, _ = rm.can_open_position("BTC/USDT", 10000.0, route="uptrend")
            self.assertTrue(can_uptrend)

    def test_uptrend_route_has_its_own_loss_budget(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(max_consecutive_losses=3), persistence_dir=persistence_dir)
            for _ in range(3):
                rm.open_position("BTC/USDT", 100.0, 1.0, exit_policy="ema72_signal")
                rm.close_position("BTC/USDT", 90.0, "stop loss")
            self.assertFalse(rm._is_halted)
            self.assertIn("uptrend", rm.route_halts())
            self.assertFalse(rm.can_open_position("BTC/USDT", 10000.0, route="uptrend")[0])
            self.assertTrue(rm.can_open_position("ADA/USDT", 10000.0, route="downtrend")[0])

    def test_route_win_resets_that_routes_streak(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(max_consecutive_losses=3), persistence_dir=persistence_dir)
            rm.open_position("ADA/USDT", 100.0, 1.0, exit_policy="downtrend_rsi_recovery")
            rm.close_position("ADA/USDT", 90.0, "loss")
            rm.open_position("ADA/USDT", 100.0, 1.0, exit_policy="downtrend_rsi_recovery")
            rm.close_position("ADA/USDT", 90.0, "loss")
            self.assertEqual(rm._route_losses.get("downtrend"), 2)
            rm.open_position("ADA/USDT", 100.0, 1.0, exit_policy="downtrend_rsi_recovery")
            rm.close_position("ADA/USDT", 110.0, "win")
            self.assertEqual(rm._route_losses.get("downtrend"), 0)
            self.assertEqual(rm.route_halts(), {})
            self.assertFalse(rm._is_halted)

    def test_route_halts_persist_across_restart(self):
        with TemporaryDirectory() as persistence_dir:
            first = RiskManager(RiskConfig(max_consecutive_losses=2), persistence_dir=persistence_dir)
            for _ in range(2):
                first.open_position("ADA/USDT", 100.0, 1.0, exit_policy="downtrend_rsi_recovery")
                first.close_position("ADA/USDT", 90.0, "loss")
            self.assertIn("downtrend", first.route_halts())

            restored = RiskManager(RiskConfig(max_consecutive_losses=2), persistence_dir=persistence_dir)
            self.assertFalse(restored._is_halted)
            self.assertIn("downtrend", restored.route_halts())
            self.assertFalse(restored.can_open_position("ADA/USDT", 10000.0, route="downtrend")[0])
            self.assertTrue(restored.can_open_position("BTC/USDT", 10000.0, route="uptrend")[0])

    def test_clear_route_halts_resets_streak(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(max_consecutive_losses=3), persistence_dir=persistence_dir)
            for _ in range(3):
                rm.open_position("ADA/USDT", 100.0, 1.0, exit_policy="downtrend_rsi_recovery")
                rm.close_position("ADA/USDT", 90.0, "loss")
            self.assertIn("downtrend", rm.route_halts())
            self.assertTrue(rm.clear_route_halts())
            self.assertEqual(rm.route_halts(), {})
            self.assertEqual(rm._route_losses.get("downtrend"), 0)
            self.assertTrue(rm.can_open_position("ADA/USDT", 10000.0, route="downtrend")[0])
            # Clearing with no active route halt is a no-op returning False.
            self.assertFalse(rm.clear_route_halts())

    def test_rolling_equity_drawdown_halts_entries(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(max_daily_loss_pct=10.0, max_rolling_drawdown_pct=5.0), persistence_dir=persistence_dir)
            self.assertTrue(rm.monitor_equity(1000.0))
            self.assertFalse(rm.monitor_equity(940.0))
            self.assertIn("rolling", rm._halt_reason.lower())

    def test_halt_taxonomy_classifies_and_recovers_manual_halts(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            self.assertTrue(RiskManager.is_daily_reset_halt("Daily equity drawdown limit reached: 5.1%"))
            self.assertTrue(RiskManager.is_daily_reset_halt("Daily loss limit reached: -$50.00 (limit: -$100.00)"))
            self.assertFalse(RiskManager.is_daily_reset_halt("Emergency stop failed; operator action required"))
            self.assertTrue(RiskManager.is_rolling_drawdown_halt("Rolling peak-to-trough drawdown limit reached: 5.2%"))
            self.assertTrue(RiskManager.is_reviewed_clear_halt("Buy fee details were incomplete for BTC/USDT; position protected with stop, operator reconciliation required"))
            self.assertFalse(RiskManager.is_reviewed_clear_halt("Open-position state is unreadable; operator reconciliation required"))
            self.assertTrue(RiskManager.is_reviewed_clear_halt("Unresolved order intent exists; exchange reconciliation required"))

            # A previously-sticky execution halt is now recoverable via review.
            rm.halt("Buy fee details were incomplete for BTC/USDT; position protected with stop, operator reconciliation required")
            self.assertTrue(rm._is_halted)
            self.assertTrue(rm.acknowledge_manual_halt_review())
            self.assertFalse(rm._is_halted)

            # State-integrity halts remain hard latches.
            rm.halt("Open-position state is unreadable; operator reconciliation required")
            self.assertFalse(rm.acknowledge_manual_halt_review())
            self.assertTrue(rm._is_halted)

    def test_halt_notifies_only_when_reason_changes(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            rm.on_halt = Mock()
            rm.halt("Unresolved order intent exists; exchange reconciliation required")
            rm.halt("Unresolved order intent exists; exchange reconciliation required")
            rm.halt("Exchange reconciliation failed; operator review required")
            self.assertEqual(rm.on_halt.call_count, 2)
            self.assertEqual(rm.on_halt.call_args.args[0], "Exchange reconciliation failed; operator review required")

    def test_manual_halt_survives_restart_across_utc_midnight(self):
        with TemporaryDirectory() as persistence_dir:
            first = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            first.store.write_state("daily_risk", {
                "date": (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat(),
                "daily_loss": -5.0,
                "daily_starting_equity": 1000.0,
                "rolling_peak_equity": 1000.0,
                "consecutive_losses": 0,
                "is_halted": True,
                "halt_reason": "Buy fee details were incomplete for BTC/USDT; position protected with stop, operator reconciliation required",
            })
            restored = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            self.assertTrue(restored._is_halted)
            self.assertIn("Buy fee details were incomplete", restored._halt_reason)
            # The daily equity anchors reset, but the manual latch must survive.
            self.assertEqual(restored._daily_loss, 0.0)
            self.assertEqual(restored._daily_starting_equity, 0.0)

    def test_daily_equity_halt_resets_across_utc_midnight(self):
        with TemporaryDirectory() as persistence_dir:
            first = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            first.store.write_state("daily_risk", {
                "date": (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat(),
                "daily_loss": -50.0,
                "daily_starting_equity": 1000.0,
                "rolling_peak_equity": 1000.0,
                "consecutive_losses": 0,
                "is_halted": True,
                "halt_reason": "Daily equity drawdown limit reached: 4.60%",
            })
            restored = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            self.assertFalse(restored._is_halted)
            self.assertEqual(restored._halt_reason, "")
            self.assertEqual(restored._daily_loss, 0.0)


    def test_equity_monitor_resets_daily_anchor_before_new_day_drawdown(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(max_daily_loss_pct=3.0, max_rolling_drawdown_pct=20.0), persistence_dir=persistence_dir)
            rm._equity_basis = RiskManager.EQUITY_BASIS
            rm._daily_loss_reset_date = datetime.now(timezone.utc).date() - timedelta(days=1)
            rm._daily_starting_equity = 1000.0
            rm._rolling_peak_equity = 1000.0
            self.assertTrue(rm.monitor_equity(900.0))
            self.assertEqual(rm._daily_starting_equity, 900.0)
            self.assertFalse(rm._is_halted)

    def test_close_position_records_strategy_route_regime_and_r_multiple(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            rm.open_position(
                "BTC/USDT", 100.0, 1.0, order_id="buy-1",
                stop_loss_override=90.0, exit_policy="ema72_signal",
                route="macro_donchian_ema72", regime="uptrend",
            )
            record = rm.close_position("BTC/USDT", 110.0, "Test exit")
            self.assertEqual(record.strategy, "macro_donchian_ema72")
            self.assertEqual(record.route, "macro_donchian_ema72")
            self.assertEqual(record.regime, "uptrend")
            self.assertAlmostEqual(record.r_multiple, 1.0)
            self.assertEqual(record.order_id, "buy-1")
            self.assertFalse(record.simulated)

    def test_consecutive_loss_halt_survives_crash_between_counter_and_halt_writes(self):
        with TemporaryDirectory() as persistence_dir:
            first = RiskManager(RiskConfig(max_consecutive_losses=2), persistence_dir=persistence_dir)
            first.store.write_state("daily_risk", {
                "date": datetime.now(timezone.utc).date().isoformat(),
                "daily_loss": -2.0,
                "daily_starting_equity": 1000.0,
                "rolling_peak_equity": 1000.0,
                "consecutive_losses": 2,
                "is_halted": False,
                "halt_reason": "",
            })
            restored = RiskManager(RiskConfig(max_consecutive_losses=2), persistence_dir=persistence_dir)
            self.assertTrue(restored._is_halted)
            self.assertIn("Consecutive loss limit", restored._halt_reason)

    def test_legacy_equity_halt_is_not_cleared_when_durable_trade_log_exists(self):
        with TemporaryDirectory() as persistence_dir:
            first = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            first.store.write_state("daily_risk", {
                "date": datetime.now(timezone.utc).date().isoformat(),
                "daily_loss": 0.0,
                "daily_starting_equity": 87186.49,
                "rolling_peak_equity": 88817.88,
                "consecutive_losses": 1,
                "is_halted": True,
                "halt_reason": "Daily equity drawdown limit reached: 4.60%",
            })
            Path(persistence_dir, "trades.jsonl").write_text('{"action":"SELL"}\n', encoding="utf-8")
            restored = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            self.assertTrue(restored.monitor_equity(10000.0, account_reconciled=True))
            self.assertTrue(restored._is_halted)
            self.assertTrue(restored.store.read_state("daily_risk")["is_halted"])

    def test_legacy_whole_account_equity_halt_migrates_after_clean_reconciliation(self):
        with TemporaryDirectory() as persistence_dir:
            first = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            first.store.write_state("daily_risk", {
                "date": datetime.now(timezone.utc).date().isoformat(),
                "daily_loss": 0.0,
                "daily_starting_equity": 87186.49,
                "rolling_peak_equity": 88817.88,
                "consecutive_losses": 1,
                "is_halted": True,
                "halt_reason": "Daily equity drawdown limit reached: 4.60%",
            })

            migrated = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            self.assertTrue(migrated._is_halted)
            self.assertTrue(migrated.monitor_equity(10000.0, account_reconciled=True))
            self.assertFalse(migrated._is_halted)
            self.assertEqual(migrated._daily_starting_equity, 10000.0)
            self.assertEqual(migrated._rolling_peak_equity, 10000.0)
            self.assertEqual(migrated._consecutive_losses, 1)
            saved = migrated.store.read_state("daily_risk")
            self.assertEqual(saved["equity_basis"], RiskManager.EQUITY_BASIS)
            audit = migrated.store.list_events(event_type="risk_equity_basis_migrated")
            self.assertEqual(len(audit), 1)
            self.assertTrue(audit[0]["details"]["legacy_equity_halt_cleared"])

    def test_equity_basis_migration_does_not_clear_halt_with_open_position(self):
        with TemporaryDirectory() as persistence_dir:
            first = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            first.store.write_state("daily_risk", {
                "date": datetime.now(timezone.utc).date().isoformat(),
                "daily_loss": 0.0,
                "daily_starting_equity": 87186.49,
                "rolling_peak_equity": 88817.88,
                "consecutive_losses": 1,
                "is_halted": True,
                "halt_reason": "Daily equity drawdown limit reached: 4.60%",
            })

            migrated = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            migrated.open_positions["BTC/USDT"] = Position(
                "BTC/USDT", "long", 50000.0, 0.01,
                datetime.now(timezone.utc).isoformat(), 49000.0, 51500.0,
            )
            self.assertTrue(migrated.monitor_equity(10000.0, account_reconciled=True))
            self.assertTrue(migrated._is_halted)
            self.assertIn("Daily equity drawdown", migrated._halt_reason)

    def test_legacy_equity_halt_requires_reconciled_account_to_clear(self):
        with TemporaryDirectory() as persistence_dir:
            first = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            first.store.write_state("daily_risk", {
                "date": datetime.now(timezone.utc).date().isoformat(),
                "daily_loss": 0.0,
                "daily_starting_equity": 87186.49,
                "rolling_peak_equity": 88817.88,
                "consecutive_losses": 1,
                "is_halted": True,
                "halt_reason": "Daily equity drawdown limit reached: 4.60%",
            })

            migrated = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            self.assertTrue(migrated.monitor_equity(10000.0))
            self.assertTrue(migrated._is_halted)

    def test_invalid_persisted_daily_loss_halts_startup(self):
        with TemporaryDirectory() as persistence_dir:
            today = datetime.now(timezone.utc).date().isoformat()
            Path(persistence_dir, "daily_risk_state.json").write_text(
                '{"date": "' + today + '", "daily_loss": NaN, "is_halted": false}',
                encoding="utf-8",
            )
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            self.assertTrue(rm._is_halted)

    def test_f11_canonical_fee_netting_in_close_position(self):
        """F-11: Close position must net fees against gross PnL."""
        with TemporaryDirectory() as persistence_dir:
            risk = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            risk.open_position("BTC/USDT", 50000.0, 0.1, order_id="buy_1")
            record = risk.close_position("BTC/USDT", 50500.0, "Take profit", fee_cost=10.0)
            self.assertIsNotNone(record)
            self.assertAlmostEqual(record.pnl, 40.0)
            self.assertEqual(record.fee, 10.0)
            self.assertAlmostEqual(record.pnl_pct, 0.8)

    def test_profit_factor_uses_gross_profit_divided_by_gross_loss(self):
        with TemporaryDirectory() as persistence_dir:
            risk = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            for price in (200.0, 90.0, 90.0):
                risk.open_position("BTC/USDT", 100.0, 1.0)
                risk.close_position("BTC/USDT", price, "fixture", fee_cost=0.0)
            stats = risk.get_performance_summary()
            self.assertEqual(stats["gross_profit"], 100.0)
            self.assertEqual(stats["gross_loss"], 20.0)
            self.assertEqual(stats["profit_factor"], 5.0)

    def test_unknown_exit_fee_is_incomplete_and_halts_entries(self):
        with TemporaryDirectory() as persistence_dir:
            risk = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            risk.open_position("BTC/USDT", 100.0, 1.0)
            record = risk.close_position("BTC/USDT", 110.0, "fee missing", fee_cost=None)
            self.assertIsNotNone(record)
            self.assertIsNone(record.pnl)
            self.assertIsNone(record.pnl_pct)
            self.assertIsNone(record.fee)
            self.assertFalse(record.accounting_complete)
            self.assertTrue(risk._is_halted)
            self.assertEqual(risk.get_performance_summary()["total_trades"], 0)
            self.assertEqual(risk.get_performance_summary()["incomplete_trades"], 1)

    def test_f12_start_paused_policy(self):
        """F-12: START_PAUSED=true policy defaults to EXIT_ONLY mode."""
        with TemporaryDirectory() as persistence_dir:
            config = RiskConfig(start_paused=True)
            risk = RiskManager(config, persistence_dir=persistence_dir)
            self.assertEqual(risk.entry_mode(), "EXIT_ONLY")
            can_open, reason = risk.can_open_position("BTC/USDT", 10000.0)
            self.assertFalse(can_open)
            self.assertIn("Entry-only pause is active", reason)

    def test_parkinson_volatility_flat_and_short_series(self):
        """Parkinson volatility: flat bars -> ~0, short series -> 40% benchmark."""
        highs = [100.0] * 20
        lows = [100.0] * 20
        self.assertAlmostEqual(calculate_parkinson_volatility(highs, lows, period=14), 0.0, places=6)
        self.assertEqual(calculate_parkinson_volatility([100.0, 101.0], [99.0, 100.0], period=14), 0.40)
        rising_highs = [100.0 + i for i in range(20)]
        rising_lows = [98.0 + i for i in range(20)]
        self.assertGreater(calculate_parkinson_volatility(rising_highs, rising_lows, period=14), 0.0)

    def test_position_size_volatility_scaling(self):
        """Volatility targeting scales size down in high-vol regimes; defaults unchanged."""
        with TemporaryDirectory() as tmpdir:
            rm = RiskManager(RiskConfig(max_risk_per_trade=1.0, position_size_pct=10.0), persistence_dir=tmpdir)
            base = rm.calculate_position_size(10000.0, 100.0, 90.0)
            self.assertAlmostEqual(base, 10.0)
            # Missing volatility inputs leave sizing unchanged (backward compatible).
            self.assertAlmostEqual(rm.calculate_position_size(10000.0, 100.0, 90.0, target_volatility=None), base)
            # Volatility above target -> multiplier bounded at 0.5.
            scaled = rm.calculate_position_size(
                10000.0, 100.0, 90.0, target_volatility=0.40, current_parkinson_volatility=0.80,
            )
            self.assertAlmostEqual(scaled, base * 0.5)
            # Volatility below target -> multiplier capped at 1.0.
            self.assertAlmostEqual(
                rm.calculate_position_size(
                    10000.0, 100.0, 90.0, target_volatility=0.40, current_parkinson_volatility=0.20,
                ),
                base,
            )

    def test_verify_statistical_edge_significance(self):
        """The research t-test harness flags a clearly positive series as significant."""
        import importlib.util
        path = Path(__file__).resolve().parent / "research" / "verify_statistical_edge.py"
        spec = importlib.util.spec_from_file_location("verify_statistical_edge", str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.verify_strategy_significance([5.0, -1.0] * 10)
        self.assertTrue(result["is_significant_p05"])
        self.assertGreater(result["profit_factor"], 1.0)
        self.assertFalse(module.verify_strategy_significance([])["is_significant_p05"])


# ==============================================================================
# 4. StateStore & Persistence Suite
# ==============================================================================

class TestStateStoreAndPersistence(unittest.TestCase):
    """Verifies SQLite durability, schema migrations, order intent state machine, and backup recovery."""

    def test_schema_version_and_tables(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            store = StateStore(db_path)
            self.assertEqual(store.get_schema_version(), 1)
            with closing(store._connect()) as conn:
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                self.assertIn("order_intents", tables)
                self.assertIn("positions", tables)
                self.assertIn("state", tables)
                self.assertIn("audit_events", tables)
                self.assertIn("schema_migrations", tables)

    def test_sqlite_store_trades_parity(self):
        with TemporaryDirectory() as tmpdir:
            store = StateStore(Path(tmpdir) / "state.db")
            payload = {
                "action": "SELL", "symbol": "BTC/USDT", "price": 100.0, "amount": 1.0,
                "timestamp": "2026-11-15T00:00:00+00:00",
            }
            store.append_trade(payload)
            store.append_trade(payload)  # duplicate payload must be deduplicated
            trades = store.list_trades(10)
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0]["symbol"], "BTC/USDT")

    def test_reconciled_trade_suppresses_matching_stale_pending_copy_without_deleting_audit_row(self):
        with TemporaryDirectory() as tmpdir:
            store = StateStore(Path(tmpdir) / "bot_state.sqlite3")
            common = {
                "action": "SELL", "symbol": "BNB/USDT", "price": 527.71,
                "amount": 0.921, "timestamp": "2026-11-21T08:42:25.860151+00:00",
                "order_id": "9504747443", "reason": "Closed manually on Binance Demo; reconciliation reviewed",
                "entry_price": 539.13, "entry_time": "2026-11-21T00:00:12+00:00",
            }
            pending = {**common, "pnl": None, "fee": None, "accounting_complete": False}
            reconciled = {**common, "pnl": -11.376524470170128, "fee": 0.8587044701701276, "accounting_complete": True}
            store.append_trade(pending)
            store.append_trade(reconciled)

            risk = RiskManager(RiskConfig(), persistence_dir=tmpdir)

            self.assertEqual(len(risk.trade_history), 1)
            self.assertTrue(risk.trade_history[0].accounting_complete)
            self.assertAlmostEqual(risk.trade_history[0].pnl, -11.376524470170128)
            summary = risk.get_performance_summary()
            self.assertEqual(summary["total_trades"], 1)
            self.assertEqual(summary["incomplete_trades"], 0)
            self.assertEqual(len(risk.trade_ledger()), 1)
            # Keep the original incomplete row in the durable ledger for auditability.
            self.assertEqual(len(store.list_trades()), 2)
            visible = normalize_trade_records(store.list_trades())
            self.assertEqual(visible, [reconciled])

    def test_trade_normalization_keeps_ambiguous_or_unidentified_rows(self):
        complete = {
            "action": "SELL", "symbol": "BNB/USDT", "order_id": "1",
            "timestamp": "2026-11-21T08:42:25Z", "pnl": 1.0, "fee": 0.1,
            "accounting_complete": True,
        }
        incomplete_same_order = {**complete, "pnl": None, "fee": None, "accounting_complete": False}
        conflicting_complete = {**complete, "pnl": 2.0}
        no_exchange_id = {**incomplete_same_order, "order_id": ""}
        separate_timestamp = {**incomplete_same_order, "timestamp": "2026-11-21T08:43:25Z"}

        # Multiple reconciled versions make the grouping ambiguous: do not hide any.
        ambiguous = normalize_trade_records([complete, conflicting_complete, incomplete_same_order])
        self.assertEqual(len(ambiguous), 3)
        # Without a proven exact identity, preserve the incomplete record for review.
        preserved = normalize_trade_records([complete, no_exchange_id, separate_timestamp])
        self.assertEqual(len(preserved), 3)

    def test_order_transitions_enforcement(self):
        with TemporaryDirectory() as tmpdir:
            store = StateStore(Path(tmpdir) / "state.db")
            store.create_order_intent("client_1", "BTC/USDT", "BUY", 0.1, {"type": "market"})
            store.update_order("client_1", "SUBMITTED")
            store.update_order("client_1", "OPEN")
            store.update_order("client_1", "FILLED")
            
            with self.assertRaises(ValueError):
                store.update_order("client_1", "SUBMITTED")

    def test_order_activity_records_manual_and_bot_origin(self):
        with TemporaryDirectory() as tmpdir:
            store = StateStore(Path(tmpdir) / "state.db")
            store.create_order_intent("manual_1", "BTC/USDT", "BUY", "0.1", {"source": "manual"})
            store.update_order("manual_1", "SUBMITTED")
            store.create_order_intent("bot_1", "ETH/USDT", "BUY", "0.2", {"source": "bot"})
            events = store.list_events(10, event_type="order_status")
            sources = {event["details"]["source"] for event in events}
            self.assertEqual(sources, {"manual", "bot"})

    def test_positions_read_write_roundtrip(self):
        with TemporaryDirectory() as tmpdir:
            store = StateStore(Path(tmpdir) / "state.db")
            test_positions = {
                "BTC/USDT": {
                    "symbol": "BTC/USDT",
                    "amount": 0.54321,
                    "entry_price": 65432.10,
                    "entry_time": "2026-11-07T08:00:00Z",
                    "stop_loss_price": 64000.0,
                    "take_profit_price": 70000.0,
                }
            }
            store.write_positions(test_positions)
            loaded = store.read_positions()
            self.assertEqual(len(loaded), 1)
            self.assertAlmostEqual(loaded["BTC/USDT"]["amount"], 0.54321)
            self.assertAlmostEqual(loaded["BTC/USDT"]["entry_price"], 65432.10)

    def test_arbitrary_state_read_write(self):
        with TemporaryDirectory() as tmpdir:
            store = StateStore(Path(tmpdir) / "state.db")
            store.write_state("daily_risk", {"date": "2026-11-07", "starting_equity": 10000.0})
            val = store.read_state("daily_risk")
            self.assertEqual(val["starting_equity"], 10000.0)
            self.assertIsNone(store.read_state("non_existent_key"))

    def test_audit_event_logging(self):
        with TemporaryDirectory() as tmpdir:
            store = StateStore(Path(tmpdir) / "state.db")
            store.append_event("EMERGENCY_STOP", {"operator": "manual", "reason": "test"})
            with closing(store._connect()) as conn:
                rows = conn.execute("SELECT event_type, details_json FROM audit_events").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][0], "EMERGENCY_STOP")

    def test_sqlite_positions_and_risk_state_survive_restart(self):
        with TemporaryDirectory() as persistence_dir:
            first = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            first.open_position("BTC/USDT", 100.0, 0.1)
            first.halt("test durable halt")
            second = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            self.assertIn("BTC/USDT", second.open_positions)
            self.assertTrue(second._is_halted)
            self.assertEqual(second._halt_reason, "test durable halt")
            self.assertEqual(second.store.quick_check(), "ok")

    def test_trade_history_hydrates_from_store_on_restart(self):
        with TemporaryDirectory() as persistence_dir:
            first = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            first.store.append_trade({
                "action": "SELL", "symbol": "BTC/USDT", "price": 110.0, "amount": 1.0,
                "entry_price": 100.0, "entry_time": "2026-11-15T00:00:00+00:00",
                "pnl": 10.0, "pnl_pct": 10.0, "fee": 0.1, "accounting_complete": True,
                "reason": "Test", "strategy": "macro_donchian_ema72",
                "route": "macro_donchian_ema72", "regime": "uptrend",
                "r_multiple": 1.0, "order_id": "buy-1", "simulated": False,
                "timestamp": "2026-11-15T01:00:00+00:00",
            })
            restored = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            self.assertEqual(len(restored.trade_history), 1)
            record = restored.trade_history[0]
            self.assertEqual(record.strategy, "macro_donchian_ema72")
            self.assertEqual(record.route, "macro_donchian_ema72")
            self.assertEqual(record.regime, "uptrend")
            self.assertEqual(record.r_multiple, 1.0)
            self.assertEqual(record.order_id, "buy-1")

    def test_order_intent_is_unique_and_unresolved_until_applied(self):
        with TemporaryDirectory() as persistence_dir:
            store = StateStore(Path(persistence_dir) / "state.sqlite3")
            store.create_order_intent("stable-id", "BTC/USDT", "buy", "0.1", {"side": "buy"})
            store.update_order("stable-id", "SUBMITTED")
            store.update_order("stable-id", "UNKNOWN")
            self.assertEqual(len(store.unresolved_orders()), 1)
            with self.assertRaises(sqlite3.IntegrityError):
                store.create_order_intent("stable-id", "BTC/USDT", "buy", "0.1", {"side": "buy"})
            store.update_order("stable-id", "FILLED", filled="0.1", average="100")
            store.mark_order_applied("stable-id")
            self.assertEqual(store.unresolved_orders(), [])

    def test_f17_sqlite_schema_version_and_online_backup(self):
        """F-17 & F-09: SQLite schema versioning, online backup, and Testnet reset recovery."""
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            store = StateStore(db_path)
            self.assertEqual(store.get_schema_version(), 1)

            store.write_state("baseline_asset_balances", {"BTC": "0.1"})
            store.write_positions({"BTC/USDT": {"amount": 0.05, "entry_price": 50000.0}})
            store.create_order_intent("client_123", "BTC/USDT", "BUY", 0.05, {"type": "market"})

            backup_file = store.create_backup()
            self.assertTrue(backup_file.exists())
            backup_store = StateStore(backup_file)
            self.assertIn("BTC/USDT", backup_store.read_positions())

            reset_data = store.snapshot_and_reset_for_testnet_wipe("Bi-weekly Spot Testnet reset")
            self.assertIn("backup_file", reset_data)
            self.assertEqual(reset_data["operator_reason"], "Bi-weekly Spot Testnet reset")
            self.assertTrue(reset_data["state_preserved"])
            self.assertIn("BTC/USDT", store.read_positions())
            self.assertEqual(len(store.unresolved_orders()), 1)
            self.assertEqual(store.read_state("baseline_asset_balances"), {"BTC": "0.1"})
            self.assertEqual(len(store.list_events(event_type="testnet_reset_review_required")), 1)

    def test_postgres_trade_recovery_merges_sqlite_and_jsonl_without_duplicates(self):
        import postgres_state_store as postgres_store

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy_db = root / "state.db"
            store = StateStore(legacy_db)
            sqlite_trade = {"symbol": "XRP/USDT", "timestamp": "2026-11-14T14:25:34Z", "pnl": None}
            jsonl_only_trade = {"symbol": "BTC/USDT", "timestamp": "2026-11-15T00:00:00Z", "pnl": 0.25}
            store.append_trade(sqlite_trade)
            (root / "trades.jsonl").write_text(
                json.dumps(sqlite_trade) + "\n" + json.dumps(jsonl_only_trade) + "\n",
                encoding="utf-8",
            )

            recovered = postgres_store._trade_payload_rows(legacy_db, root / "trades.jsonl")
            self.assertEqual(len(recovered), 2)
            self.assertEqual({row[1]["symbol"] for row in recovered}, {"XRP/USDT", "BTC/USDT"})
            self.assertTrue(all(row[0].startswith("runtime:") for row in recovered))

            class Result:
                def __init__(self, row):
                    self.row = row

                def fetchone(self):
                    return self.row

            class Connection:
                def __init__(self):
                    self.payloads = set()

                def execute(self, query, params):
                    payload = json.loads(params[0] if query.lstrip().startswith("SELECT") else params[1])
                    if query.lstrip().startswith("SELECT"):
                        return Result((1,) if json.dumps(payload, sort_keys=True) in self.payloads else None)
                    self.payloads.add(json.dumps(payload, sort_keys=True))
                    return Result((1,))

            connection = Connection()
            first = postgres_store._backfill_trade_rows(connection, recovered)
            second = postgres_store._backfill_trade_rows(connection, recovered)
            self.assertEqual(first, 2)
            self.assertEqual(second, 0)

    def test_postgres_testnet_reset_snapshot_never_mutates_trade_state(self):
        import postgres_state_store as postgres_store

        store = object.__new__(postgres_store.PostgresStateStore)
        conn = Mock()
        conn.transaction.return_value = nullcontext()
        store._pool = Mock()
        store._pool.connection.return_value = nullcontext(conn)
        store._now = Mock(return_value="2026-11-21T00:00:00Z")
        store.create_backup = Mock(return_value=Path("state-backup.json"))
        store.read_positions = Mock(return_value={"BTC/USDT": {"amount": 0.01}})
        store.unresolved_orders = Mock(return_value=[{"client_order_id": "pending"}])

        with patch.object(postgres_store, "Jsonb", side_effect=lambda value: value):
            result = store.snapshot_and_reset_for_testnet_wipe("confirmed Testnet reset")

        self.assertTrue(result["state_preserved"])
        statements = [str(call.args[0]).strip().upper() for call in conn.execute.call_args_list]
        self.assertEqual(len(statements), 1)
        self.assertTrue(statements[0].startswith("INSERT INTO BOT_PRIVATE.AUDIT_EVENTS"))

    def test_postgres_trade_recovery_supports_legacy_sqlite_without_trade_table(self):
        import postgres_state_store as postgres_store

        with TemporaryDirectory() as temp_dir:
            legacy_db = Path(temp_dir) / "legacy.db"
            with closing(sqlite3.connect(legacy_db)) as conn:
                conn.execute("CREATE TABLE state (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)")
            trades_path = legacy_db.with_name("trades.jsonl")
            trades_path.write_text(json.dumps({"symbol": "ETH/USDT"}) + "\n", encoding="utf-8")
            recovered = postgres_store._trade_payload_rows(legacy_db, trades_path)
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0][1]["symbol"], "ETH/USDT")

    def test_completed_postgres_import_does_not_require_legacy_sqlite_file(self):
        import postgres_state_store as postgres_store

        imported_counts = {"state": 3, "positions": 0, "trades": 2}

        class Cursor:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def transaction(self):
                return nullcontext()

            def execute(self, query):
                if "to_regclass" in query:
                    return Cursor({"table_name": "bot_private.storage_migrations"})
                return Cursor({"counts_json": imported_counts})

        class Pool:
            def connection(self):
                return Connection()

            def close(self):
                pass

        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            missing_source = Path(temp_dir) / "removed_legacy.sqlite3"
            with patch.object(postgres_store, "_conninfo_from_url", return_value={"port": "5432"}), \
                    patch.object(postgres_store, "make_conninfo", return_value="test-dsn"), \
                    patch.object(postgres_store, "ConnectionPool", return_value=Pool()):
                result = postgres_store.PostgresStateStore.migrate_from_sqlite(
                    "postgresql://test", missing_source, Path(temp_dir) / "trades.jsonl"
                )

        self.assertEqual(result["status"], "already_imported")
        self.assertEqual(result["counts"], imported_counts)
        self.assertIn("not required", result["backup"])

    def test_startup_does_not_launch_dashboard_when_migration_fails(self):
        import start

        with patch("start.migrate_storage", return_value=1), patch("start.os.execv") as execv:
            self.assertEqual(start.main(), 1)
        execv.assert_not_called()

    def test_startup_replaces_itself_with_dashboard_after_migration(self):
        import start

        with patch("start.migrate_storage", return_value=0), patch("start.os.execv") as execv:
            self.assertEqual(start.main(), 0)
        executable, arguments = execv.call_args.args
        self.assertEqual(executable, sys.executable)
        self.assertEqual(Path(arguments[1]).name, "dashboard.py")


# ==============================================================================
# 5. Exchange Connector & Security Suite
# ==============================================================================

class TestExchangeConnectorAndSecurity(unittest.TestCase):
    """Verifies exchange connector guardrails, live refusal, credential safety, and rate-limiting."""

    def test_client_order_lookup_distinguishes_not_found_from_exchange_failure(self):
        connector = object.__new__(ExchangeConnector)
        connector.exchange = Mock()
        connector.exchange.fetch_order.side_effect = ccxt.OrderNotFound("order does not exist")
        self.assertIsNone(connector.fetch_order_by_client_id("cb-missing", "SOL/USDT"))

        connector.exchange.fetch_order.side_effect = ccxt.NetworkError("service unavailable")
        with self.assertRaisesRegex(RuntimeError, "Client order status is unavailable"):
            connector.fetch_order_by_client_id("cb-unknown", "SOL/USDT")

    @patch("exchange.ccxt.binance")
    def test_live_exchange_initialization_is_refused(self, binance_factory):
        config = ExchangeConfig(
            name="binance",
            api_key="live-key",
            api_secret="live-secret",
            paper_trading=False,
            allow_live_trading=True,
        )
        with self.assertRaisesRegex(RuntimeError, "Live trading is disabled"):
            ExchangeConnector(config)
        binance_factory.assert_not_called()

    @patch("exchange.ccxt.binance")
    def test_binance_demo_mode_uses_demo_endpoints_not_testnet(self, binance_factory):
        config = ExchangeConfig(
            name="binance",
            api_key="demo-key",
            api_secret="testnet-secret",
            demo_api_key="demo-key",
            demo_api_secret="demo-secret",
            paper_trading=True,
            paper_environment="demo",
        )
        ExchangeConnector(config)
        private_options = binance_factory.call_args_list[0].args[0]
        self.assertEqual(private_options["apiKey"], "demo-key")
        self.assertEqual(private_options["secret"], "demo-secret")
        exchange = binance_factory.return_value
        exchange.enable_demo_trading.assert_called_once_with(True)
        exchange.set_sandbox_mode.assert_not_called()

    def test_closed_candle_filter_drops_incomplete_and_malformed_rows(self):
        now = 1_800_000_000_000
        candles = [
            [now - 60_000, 100, 102, 99, 101, 10],
            [now, 101, 103, 100, 102, 11],
            [now - 120_000, 100, 99, 98, 100, 5],
            [now - 180_000, 100, float("nan"), 99, 101, 4],
        ]
        filtered = filter_closed_ohlcv(candles, 60, now_ms=now + 3000)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0][0], now - 60_000)

    def test_candle_sequence_rejects_gaps_and_stale_history(self):
        now = 1_800_000_000_000
        continuous = [[now - 180_000, 1, 2, 1, 2, 3], [now - 120_000, 2, 3, 2, 3, 4]]
        gap = [continuous[0], [now - 60_000, 3, 4, 3, 4, 5]]
        self.assertEqual(len(validate_candle_sequence(continuous, 60, now_ms=now)), 2)
        with self.assertLogs("bot.exchange", level="WARNING") as captured:
            self.assertEqual(validate_candle_sequence(gap, 60, now_ms=now), [])
        self.assertIn("expected timestamp", captured.output[0])
        self.assertIn("found", captured.output[0])
        self.assertIn("intervals", captured.output[0])
        self.assertEqual(validate_candle_sequence(continuous, 60, now_ms=now + 600_000), [])

    def test_exchange_preserves_gap_metadata_for_audit_event(self):
        connector = object.__new__(ExchangeConnector)
        connector._lock_obj = threading.RLock()
        connector._candle_sequence_issues = {}
        connector.exchange = Mock()
        connector.exchange.parse_timeframe.return_value = 60
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start = (now_ms // 60_000) * 60_000 - 600_000
        connector.exchange.fetch_ohlcv.return_value = [
            [start, 100, 101, 99, 100, 10],
            [start + 120_000, 100, 101, 99, 100, 10],
        ]

        self.assertEqual(connector.fetch_closed_ohlcv("BTC/USDT", "1m", 10), [])
        self.assertEqual(connector._candle_sequence_issues["BTC/USDT"]["missing_intervals"], 1)

    def test_long_ohlcv_history_is_paged_and_returned_in_timestamp_order(self):
        connector = object.__new__(ExchangeConnector)
        connector._lock_obj = threading.RLock()
        connector.exchange = Mock()
        connector.exchange.parse_timeframe.return_value = 3600

        def page(_symbol, _timeframe, since=None, limit=None):
            self.assertEqual(limit, 1000)
            timestamps = range(1000, 2000) if since is None else range(0, 1000)
            return [[ts * 3_600_000, 1, 2, 1, 2, 3] for ts in timestamps]

        connector.exchange.fetch_ohlcv.side_effect = page
        candles = connector.fetch_ohlcv("BTC/USDT", "1h", 1500)
        self.assertEqual(len(candles), 1500)
        self.assertEqual(candles[0][0], 500 * 3_600_000)
        self.assertEqual(candles[-1][0], 1999 * 3_600_000)
        self.assertEqual(connector.exchange.fetch_ohlcv.call_count, 2)

    def test_testnet_candles_use_public_data_client_not_authenticated_testnet_client(self):
        connector = object.__new__(ExchangeConnector)
        connector._lock_obj = threading.RLock()
        connector.exchange = Mock()
        connector.market_data_exchange = Mock()
        connector.market_data_exchange.fetch_ohlcv.return_value = [[1, 100, 101, 99, 100, 10]]

        candles = connector.fetch_ohlcv("BTC/USDT", "1h", 1)

        self.assertEqual(candles, [[1, 100, 101, 99, 100, 10]])
        connector.market_data_exchange.fetch_ohlcv.assert_called_once_with("BTC/USDT", "1h", limit=1)
        connector.exchange.fetch_ohlcv.assert_not_called()

    def test_paper_public_market_client_has_no_private_credentials(self):
        config = ExchangeConfig(
            name="binance", api_key="testnet-key", api_secret="testnet-secret",
            paper_trading=True, paper_environment="testnet",
        )
        connector = object.__new__(ExchangeConnector)
        connector.config = config
        public_client = Mock()
        public_client.urls = {"api": {"public": "https://api.binance.com/api/v3"}}
        with patch("exchange.ccxt.binance", return_value=public_client) as constructor:
            result = connector._create_public_market_data_exchange()

        self.assertIs(result, public_client)
        constructor.assert_called_once()
        options = constructor.call_args.args[0]
        self.assertNotIn("apiKey", options)
        self.assertNotIn("secret", options)
        self.assertEqual(
            public_client.urls["api"]["public"],
            "https://data-api.binance.vision/api/v3",
        )

        config.paper_environment = "demo"
        with patch("exchange.ccxt.binance", return_value=public_client) as constructor:
            result = connector._create_public_market_data_exchange()
        self.assertIs(result, public_client)
        constructor.assert_called_once()

    def test_long_strategy_request_returns_required_completed_candle_count(self):
        connector = object.__new__(ExchangeConnector)
        connector._lock_obj = threading.RLock()
        connector._candle_sequence_issues = {}
        connector.exchange = Mock()
        connector.exchange.parse_timeframe.return_value = 3600
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        latest_open = now // 3_600_000 * 3_600_000
        start = latest_open - 2501 * 3_600_000
        connector.fetch_ohlcv = Mock(return_value=[
            [start + i * 3_600_000, 100, 101, 99, 100, 10]
            for i in range(2502)
        ])
        closed = connector.fetch_closed_ohlcv("BTC/USDT", "1h", 2501)
        self.assertEqual(len(closed), 2501)
        self.assertLess(closed[-1][0], latest_open)
        connector.fetch_ohlcv.assert_called_once_with("BTC/USDT", "1h", 2502)

    def test_ema72_warmup_gets_2500_closed_bars_with_open_bar_filtered(self):
        connector = object.__new__(ExchangeConnector)
        connector._lock_obj = threading.RLock()
        connector._candle_sequence_issues = {}
        connector.exchange = Mock()
        connector.exchange.parse_timeframe.return_value = 3600
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        latest_open = now // 3_600_000 * 3_600_000
        start = latest_open - 2500 * 3_600_000
        connector.fetch_ohlcv = Mock(return_value=[
            [start + i * 3_600_000, 100, 101, 99, 100, 10]
            for i in range(2501)
        ])
        closed = connector.fetch_closed_ohlcv("BTC/USDT", "1h", 2500)
        self.assertEqual(len(closed), 2500)
        self.assertLess(closed[-1][0], latest_open)
        connector.fetch_ohlcv.assert_called_once_with("BTC/USDT", "1h", 2501)

    def test_closed_candle_return_correlation_is_aligned_by_timestamp(self):
        candles_a, candles_b = [], []
        for i in range(40):
            price_a = 100 + i * 2 + (i % 3)
            price_b = 50 + i * 1 + ((i % 3) / 2)
            candles_a.append([i + 1, price_a, price_a, price_a, price_a, 1])
            candles_b.append([i + 1, price_b, price_b, price_b, price_b, 1])
        self.assertGreater(TradingBot._return_correlation(candles_a, candles_b), 0.9)

    def test_exchange_reconciliation_error_is_not_treated_as_empty_balance(self):
        connector = object.__new__(ExchangeConnector)
        connector.exchange = Mock()
        connector.exchange.fetch_balance.side_effect = ccxt.AuthenticationError("secret omitted")
        with self.assertRaisesRegex(RuntimeError, "Testnet authentication"):
            connector.fetch_account_snapshot()

    def test_open_order_lookup_failure_blocks_account_reconciliation(self):
        connector = object.__new__(ExchangeConnector)
        connector.config = ExchangeConfig(paper_trading=True, paper_environment="testnet")
        connector.exchange = Mock()
        connector.exchange.fetch_balance.return_value = {"total": {"USDT": 1000.0}}
        connector.exchange.fetch_open_orders.side_effect = ccxt.ExchangeError(
            "fetchOpenOrders is not supported"
        )
        connector._is_simulated = lambda: False

        with self.assertRaises(AccountAccessError):
            connector.fetch_account_snapshot()

        diagnostic = connector.diagnose_account_access()
        self.assertEqual(diagnostic["overall_status"], "BALANCE_ONLY_ORDER_PERMISSION_MISSING")
        self.assertFalse(diagnostic["orders_ok"])

    def test_account_authentication_failure_is_actionable_without_echoing_secrets(self):
        mock_ex = Mock()
        mock_ex.last_json_response = {"code": -2014, "msg": "API-key format invalid."}
        mock_ex.last_http_response = '{"code": -2014, "msg": "API-key format invalid."}'
        mock_ex.last_http_status = 400
        body, status = extract_ccxt_response_metadata(mock_ex)
        self.assertEqual(body, {"code": -2014, "msg": "API-key format invalid."})
        self.assertEqual(status, 400)

        connector = object.__new__(ExchangeConnector)
        connector.exchange = Mock()
        connector.exchange.last_json_response = None
        connector.exchange.last_http_response = None
        connector.exchange.last_http_status = None
        connector.exchange.fetch_balance.side_effect = ccxt.AuthenticationError("private-value-must-not-appear")

        with self.assertRaises(AccountAccessError) as raised:
            connector.fetch_balance_snapshot()

        self.assertIn("Spot Testnet", str(raised.exception))
        self.assertNotIn("private-value-must-not-appear", str(raised.exception))

        msg_2014 = account_access_message(ccxt.ExchangeError('{"code":-2014,"msg":"API-key format invalid."}'))
        self.assertIn("-2014", msg_2014)
        self.assertIn("API key format is invalid", msg_2014)

        msg_2015_dict = account_access_message(ccxt.ExchangeError("Authentication error"), response_body={"code": -2015, "msg": "key rejected"})
        self.assertIn("-2015", msg_2015_dict)
        self.assertIn("API key, IP, or permissions", msg_2015_dict)

        msg_2015_text = account_access_message(ccxt.ExchangeError("Invalid API-key, IP, or permissions for action."))
        self.assertIn("-2015", msg_2015_text)

        msg_1022 = account_access_message(ccxt.ExchangeError('{"code":-1022,"msg":"signature for this request is not valid"}'))
        self.assertIn("-1022", msg_1022)
        self.assertIn("API_SECRET does not match", msg_1022)

        msg_1021 = account_access_message(ccxt.ExchangeError('{"code":-1021,"msg":"Timestamp for this request is outside of the recvWindow."}'))
        self.assertIn("-1021", msg_1021)
        self.assertIn("server clock synchronization", msg_1021)

        msg_401 = account_access_message(ccxt.AuthenticationError("HTTP 401 Unauthorized"), http_status=401)
        self.assertIn("HTTP 401", msg_401)
        self.assertIn("Spot Testnet", msg_401)
        demo_401 = account_access_message(
            ccxt.AuthenticationError("HTTP 401 Unauthorized"), http_status=401, environment="demo"
        )
        self.assertIn("HTTP 401", demo_401)
        self.assertIn("DEMO_API_KEY", demo_401)
        self.assertIn("Binance Demo", demo_401)

        msg_403 = account_access_message(ccxt.PermissionDenied("HTTP 403 Forbidden"), http_status=403)
        self.assertIn("HTTP 403", msg_403)
        self.assertIn("denied account access", msg_403)

        msg_html = account_access_message(ccxt.ExchangeError("502 Bad Gateway"), response_body="<html><title>502 Bad Gateway</title></html>", http_status=502)
        self.assertIn("HTTP 502", msg_html)
        self.assertNotIn("<html>", msg_html)
        self.assertNotIn("<title>", msg_html)

        msg_net = account_access_message(ccxt.NetworkError("Failed to connect to host"))
        self.assertIn("could not be reached", msg_net)

        leak_probe = (
            "GET https://testnet.binance.vision/api/v3/account?timestamp=1690000000"
            "&signature=supersecret_sig_1234567890abcdef&apiKey=supersecret_key_9876543210 "
            'failed with {"code":-2015,"msg":"Invalid API-key"}'
        )
        msg_probe = account_access_message(ccxt.ExchangeError(leak_probe))
        self.assertNotIn("supersecret_sig_1234567890abcdef", msg_probe)
        self.assertNotIn("supersecret_key_9876543210", msg_probe)
        self.assertNotIn("https://testnet.binance.vision", msg_probe)
        self.assertNotIn("1690000000", msg_probe)
        self.assertIn("-2015", msg_probe)

    def test_diagnose_account_access(self):
        config_sim = ExchangeConfig(paper_trading=True, paper_environment="simulated")
        conn_sim = ExchangeConnector(config_sim)
        diag_sim = conn_sim.diagnose_account_access()
        self.assertEqual(diag_sim["overall_status"], "SIMULATED_WALLET")
        self.assertTrue(diag_sim["balance_ok"])
        self.assertTrue(diag_sim["orders_ok"])

        conn = object.__new__(ExchangeConnector)
        conn.config = ExchangeConfig(paper_trading=True, paper_environment="testnet")
        conn.exchange = Mock()
        conn.exchange.last_json_response = {"code": -2015, "msg": "Invalid API-key"}
        conn.exchange.last_http_response = '{"code": -2015, "msg": "Invalid API-key"}'
        conn.exchange.last_http_status = 401
        conn.exchange.fetch_balance.side_effect = ccxt.AuthenticationError("auth failed")
        conn._is_simulated = lambda: False

        diag_auth_fail = conn.diagnose_account_access()
        self.assertEqual(diag_auth_fail["overall_status"], "AUTHENTICATION_FAILED")
        self.assertFalse(diag_auth_fail["balance_ok"])
        self.assertIn("-2015", diag_auth_fail["balance_error"])

        conn.exchange.fetch_balance.side_effect = None
        conn.exchange.fetch_balance.return_value = {"total": {"USDT": 1000.0}}
        conn.exchange.fetch_open_orders.side_effect = ccxt.PermissionDenied("HTTP 403 Forbidden")
        conn.exchange.last_json_response = None
        conn.exchange.last_http_response = None
        conn.exchange.last_http_status = 403

        diag_perm = conn.diagnose_account_access()
        self.assertEqual(diag_perm["overall_status"], "BALANCE_ONLY_ORDER_PERMISSION_MISSING")
        self.assertTrue(diag_perm["balance_ok"])
        self.assertFalse(diag_perm["orders_ok"])
        self.assertIn("HTTP 403", diag_perm["orders_error"])

        conn.exchange.fetch_open_orders.side_effect = None
        conn.exchange.fetch_open_orders.return_value = []
        diag_ok = conn.diagnose_account_access()
        self.assertEqual(diag_ok["overall_status"], "AUTHENTICATED_AND_RECONCILED")
        self.assertTrue(diag_ok["balance_ok"])
        self.assertTrue(diag_ok["orders_ok"])

    def test_simulated_paper_trading_mode(self):
        config = ExchangeConfig(
            api_key="", api_secret="",
            paper_trading=True, paper_environment="simulated",
        )
        connector = ExchangeConnector(config)
        connector.get_current_price = lambda symbol: 50000.0

        snapshot, orders = connector.fetch_account_snapshot()
        self.assertIn("USDT", snapshot["total"])
        self.assertEqual(len(orders), 0)

        buy = connector.place_market_buy("BTC/USDT", 0.1, "sim-buy-1")
        self.assertEqual(buy["status"], "closed")
        self.assertEqual(buy["filled"], 0.1)

        stop = connector.place_native_stop_loss("BTC/USDT", 0.1, 49000.0, "sim-stop-1")
        self.assertEqual(stop["status"], "open")

        snap2, orders2 = connector.fetch_account_snapshot()
        self.assertEqual(len(orders2), 1)

        sell = connector.place_market_sell("BTC/USDT", 0.1, "sim-sell-1")
        self.assertEqual(sell["status"], "closed")

    def test_auto_fallback_to_simulation_on_2015(self):
        config = ExchangeConfig(
            api_key="key", api_secret="secret",
            paper_trading=True, paper_environment="testnet",
            fallback_to_simulation=True,
        )
        connector = ExchangeConnector(config)
        connector.exchange.fetch_balance = Mock(side_effect=ccxt.AuthenticationError('{"code":-2015,"msg":"Invalid API-key"}'))
        connector.get_current_price = lambda symbol: 50000.0

        snapshot, orders = connector.fetch_account_snapshot()
        self.assertTrue(connector.is_fallback_simulated)
        self.assertIn("USDT", snapshot["total"])

    def test_f06_exchange_locking_is_present(self):
        """F-06: ExchangeConnector must serialize CCXT calls through an internal lock."""
        cfg = ExchangeConfig(name="binance", paper_trading=True, paper_environment="simulated")
        conn = ExchangeConnector(cfg)
        self.assertTrue(hasattr(conn, "_exchange_lock"))
        with conn._exchange_lock:
            with conn._exchange_lock:
                pass

    def test_f07_ticker_failure_returns_none_not_zero(self):
        """F-07: Ticker failure or invalid price must return None, never numeric $0.00."""
        cfg = ExchangeConfig(name="binance", paper_trading=True, paper_environment="simulated")
        conn = ExchangeConnector(cfg)
        conn.exchange = Mock()

        conn.exchange.fetch_ticker.side_effect = ccxt.NetworkError("Exchange timeout")
        self.assertIsNone(conn.get_ticker("BTC/USDT"))
        self.assertIsNone(conn.get_current_price("BTC/USDT"))

        conn.exchange.fetch_ticker.side_effect = None
        conn.exchange.fetch_ticker.return_value = {"last": None}
        self.assertIsNone(conn.get_current_price("BTC/USDT"))

        conn.exchange.fetch_ticker.return_value = {"last": 0.0}
        self.assertIsNone(conn.get_current_price("BTC/USDT"))

        conn.exchange.fetch_ticker.return_value = {"last": -50.0}
        self.assertIsNone(conn.get_current_price("BTC/USDT"))

        conn.exchange.fetch_ticker.return_value = {"last": float("nan")}
        self.assertIsNone(conn.get_current_price("BTC/USDT"))

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        conn.exchange.fetch_ticker.return_value = {"last": 52100.50, "timestamp": now_ms}
        self.assertEqual(conn.get_current_price("BTC/USDT"), 52100.50)
        conn.exchange.fetch_ticker.return_value = {"last": 52100.50}
        self.assertIsNone(conn.get_current_price("BTC/USDT"))
        conn.exchange.fetch_ticker.return_value = {"last": 52100.50, "timestamp": now_ms - 180_000}
        self.assertIsNone(conn.get_current_price("BTC/USDT"))


# ==============================================================================
# 6. Bot Engine Order Lifecycle Suite
# ==============================================================================

class FakeExchange:
    """Deterministic in-memory exchange used for end-to-end round-trip tests.

    Models a single quote-balance wallet with realistic taker fees: buy fees are
    charged in the received base asset, sell fees in the quote asset, both at a
    configurable rate. This lets a test drive the real order-submission and
    close-accounting paths and then assert that the bot's realized PnL exactly
    equals the wallet's quote-balance delta (no accounting drift).
    """

    def __init__(self, quote_balance: float, price: float, fee_rate: float = 0.001):
        self.quote = Decimal(str(quote_balance))
        self.base = Decimal("0")
        self.price = Decimal(str(price))
        self.fee_rate = Decimal(str(fee_rate))
        self._order_seq = 0
        self.prices: dict[str, Decimal] = {}

    def set_price(self, symbol: str, price: float) -> None:
        self.prices[symbol] = Decimal(str(price))

    def get_current_price(self, symbol: str) -> float:
        return float(self.prices.get(symbol, self.price))

    def _next_id(self, side: str) -> str:
        self._order_seq += 1
        return f"{side}-{self._order_seq}"

    def place_market_buy(self, symbol: str, amount: str, client_id: str) -> dict:
        base = symbol.split("/")[0]
        price = self.prices.get(symbol, self.price)
        amount_d = Decimal(amount)
        cost = amount_d * price
        fee_base = amount_d * self.fee_rate
        self.quote -= cost
        self.base += (amount_d - fee_base)
        return {
            "id": self._next_id("buy"),
            "status": "closed",
            "filled": float(amount_d),
            "average": float(price),
            "fees": [{"currency": base, "cost": str(fee_base)}],
        }

    def place_market_sell(self, symbol: str, amount: str, client_id: str) -> dict:
        quote = symbol.split("/")[1]
        price = self.prices.get(symbol, self.price)
        amount_d = Decimal(amount)
        proceeds = amount_d * price
        fee_quote = proceeds * self.fee_rate
        self.quote += (proceeds - fee_quote)
        self.base -= amount_d
        return {
            "id": self._next_id("sell"),
            "status": "closed",
            "filled": float(amount_d),
            "average": float(price),
            "fees": [{"currency": quote, "cost": str(fee_quote)}],
        }


class TestBotEngineOrderLifecycle(unittest.TestCase):
    """Verifies complete order execution, fee deduction, stop loss synchronization, recovery, and alerts."""

    def test_reconcile_orders_only_checks_account_without_running_strategy_and_stays_paused(self):
        bot = object.__new__(TradingBot)
        bot.risk_manager = Mock()
        bot.risk_manager.store.unresolved_orders.return_value = []
        bot._cycle_lock = threading.Lock()
        bot._reconcile_terminal_no_fill_order_intents = Mock()
        bot._reconcile_exchange_state = Mock(return_value=True)
        bot.run_once = Mock()

        success, message = bot.reconcile_orders_only()

        self.assertTrue(success)
        self.assertIn("Entries remain paused", message)
        bot.risk_manager.set_exit_only.assert_called()
        self.assertEqual(bot.risk_manager.set_exit_only.call_count, 2)
        bot._reconcile_terminal_no_fill_order_intents.assert_called_once_with()
        bot._reconcile_exchange_state.assert_called_once_with()
        bot.run_once.assert_not_called()
        self.assertFalse(bot._cycle_lock.locked())

    def test_reconcile_orders_only_failure_keeps_entry_mode_paused(self):
        bot = object.__new__(TradingBot)
        bot.risk_manager = Mock()
        bot.risk_manager.store.unresolved_orders.return_value = []
        bot.risk_manager._halt_reason = "Exchange reconciliation failed: account mismatch"
        bot._last_account_error = "Account reconciliation failed."
        bot._cycle_lock = threading.Lock()
        bot._reconcile_terminal_no_fill_order_intents = Mock()
        bot._reconcile_exchange_state = Mock(return_value=False)

        success, message = bot.reconcile_orders_only()

        self.assertFalse(success)
        self.assertIn("Entries remain paused", message)
        self.assertIn("no order was sent", message)
        self.assertEqual(bot.risk_manager.set_exit_only.call_count, 2)
        self.assertFalse(bot._cycle_lock.locked())

    def test_manual_demo_buy_reuses_risk_checks_and_marks_operator_managed_position(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(
                exchange=SimpleNamespace(paper_trading=True, paper_environment="demo"),
                strategy=SimpleNamespace(trading_pairs=["BTC/USDT"]),
            )
            bot.exchange = SimpleNamespace(is_fallback_simulated=False)
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot._cycle_lock = threading.Lock()
            bot._last_reconciliation_ok = False
            bot._reconcile_exchange_state = Mock(side_effect=lambda: setattr(bot, "_last_reconciliation_ok", True) or True)

            def simulate_entry(signal, decision_key):
                self.assertEqual(signal.signal, Signal.BUY)
                self.assertTrue(signal.indicators["manual_operator"])
                self.assertEqual(signal.indicators["route"], "manual_operator")
                self.assertIn("dashboard-manual-buy:", decision_key)
                position = bot.risk_manager.open_position(
                    signal.symbol, 100.0, 1.0, order_id="demo-buy-1", route="manual_operator"
                )
                position.stop_order_id = "demo-stop-1"

            bot._handle_buy_signal = Mock(side_effect=simulate_entry)
            result = bot.manual_demo_buy("BTC/USDT", "0123456789abcdef0123456789abcdef")

            self.assertTrue(result["success"])
            self.assertTrue(result["stop_protected"])
            self.assertEqual(bot.risk_manager.open_positions["BTC/USDT"].route, "manual_operator")
            bot._reconcile_exchange_state.assert_called_once_with()
            bot._handle_buy_signal.assert_called_once()

    def test_manual_demo_buy_fails_closed_outside_demo_or_with_safety_halt(self):
        bot = object.__new__(TradingBot)
        bot.config = SimpleNamespace(
            exchange=SimpleNamespace(paper_trading=True, paper_environment="testnet"),
            strategy=SimpleNamespace(trading_pairs=["BTC/USDT"]),
        )
        bot.exchange = SimpleNamespace(is_fallback_simulated=False)
        self.assertFalse(bot.manual_demo_buy("BTC/USDT", "0123456789abcdef0123456789abcdef")["success"])

        with TemporaryDirectory() as persistence_dir:
            bot.config.exchange.paper_environment = "demo"
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.risk_manager.halt("Test safety halt")
            bot._cycle_lock = threading.Lock()
            bot._reconcile_exchange_state = Mock()
            result = bot.manual_demo_buy("BTC/USDT", "0123456789abcdef0123456789abcdef")
            self.assertFalse(result["success"])
            self.assertIn("safety control", result["message"])
            bot._reconcile_exchange_state.assert_not_called()

    def test_manual_demo_buy_obeys_open_position_limit_without_reconciling_or_ordering(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(
                exchange=SimpleNamespace(paper_trading=True, paper_environment="demo"),
                strategy=SimpleNamespace(trading_pairs=["BTC/USDT", "ETH/USDT"]),
            )
            bot.exchange = SimpleNamespace(is_fallback_simulated=False)
            bot.risk_manager = RiskManager(RiskConfig(max_open_positions=1), persistence_dir=persistence_dir)
            bot.risk_manager.open_position("ETH/USDT", 100.0, 1.0)
            bot._cycle_lock = threading.Lock()
            bot._reconcile_exchange_state = Mock()

            result = bot.manual_demo_buy("BTC/USDT", "0123456789abcdef0123456789abcdef")

            self.assertFalse(result["success"])
            self.assertIn("Position limit reached", result["message"])
            bot._reconcile_exchange_state.assert_not_called()

    def test_strategy_sell_does_not_override_operator_managed_demo_position(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.risk_manager.open_position("BTC/USDT", 100.0, 1.0, route="manual_operator")
            bot.exchange = Mock()
            bot.exchange.get_current_price.return_value = 100.0
            bot._submit_market_order = Mock()
            bot._cancel_exchange_stop = Mock()

            bot._handle_sell_signal(TradeSignal(Signal.SELL, "BTC/USDT", 1.0, "strategy exit", {}))

            self.assertIn("BTC/USDT", bot.risk_manager.open_positions)
            bot._submit_market_order.assert_not_called()
            bot._cancel_exchange_stop.assert_not_called()

    def test_status_log_reports_selected_environment_and_neutral_atr_target(self):
        bot = object.__new__(TradingBot)
        bot.config = SimpleNamespace(
            exchange=SimpleNamespace(paper_trading=True, paper_environment="demo"),
            risk=SimpleNamespace(max_open_positions=3),
        )
        bot.exchange = Mock()
        bot.exchange.get_usdt_balance.return_value = 4487.76
        bot.exchange.get_current_price.return_value = 0.99
        bot.exchange.is_fallback_simulated = False
        position = SimpleNamespace(
            entry_price=0.9943,
            stop_loss_price=0.98,
            take_profit_price=1.02,
            exit_policy="downtrend_rsi_recovery",
            break_even_activated=False,
            unrealized_pnl=Mock(return_value=-2.66),
            unrealized_pnl_pct=Mock(return_value=-0.53),
        )
        bot.risk_manager = SimpleNamespace(open_positions={"XRP/USDT": position})
        bot._cycle_count = 9

        output = io.StringIO()
        with redirect_stdout(output):
            bot.print_status()

        rendered = output.getvalue()
        self.assertIn("Mode: Binance Demo", rendered)
        self.assertIn("2×ATR target", rendered)
        self.assertNotIn("Testnet target", rendered)

    def test_dynamic_profile_executes_candidate_only_after_router_selects_it(self):
        bot = object.__new__(TradingBot)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 3_600_000
        candles = []
        for index in range(2501):
            close = 100 * (1.001 ** index)
            candles.append([
                now_ms - (2500 - index) * 3_600_000,
                close, close * 1.002, close * 0.998, close, 100.0,
            ])
        bot.config = SimpleNamespace(strategy=SimpleNamespace(
            name="dynamic_regime", timeframe="1h", lookback_candles=2501,
        ), enable_notifications=False)
        bot.exchange = Mock()
        bot.exchange.fetch_closed_ohlcv.return_value = candles
        bot.exchange.get_ticker.return_value = {"bid": 100.0, "ask": 100.1}
        bot.strategy = Mock()
        bot.strategy.analyze.return_value = TradeSignal(
            Signal.BUY, "BTC/USDT", 0.85, "candidate setup", {"strategy": "macro_donchian_ema72"}
        )
        bot.risk_manager = SimpleNamespace(open_positions={}, store=Mock())
        bot._last_analyzed_candle = {}
        bot._analysis_error_states = {}
        bot.last_signals = {}
        bot._handle_buy_signal = Mock()
        bot._handle_sell_signal = Mock()

        with patch("bot_engine.evaluate_shadow_candidate", return_value={"signal": "HOLD", "status": "test"}):
            bot.analyze_market("BTC/USDT")

        bot.strategy.analyze.assert_called_once()
        self.assertEqual(bot.strategy.analyze.call_args.args[1], "BTC/USDT")
        self.assertFalse(bot.strategy.analyze.call_args.kwargs["in_position"])
        bot._handle_buy_signal.assert_called_once()
        self.assertEqual(
            bot.last_signals["BTC/USDT"]["regime_router"]["selected_strategy"],
            "macro_donchian_ema72",
        )

    def test_dynamic_profile_routes_downtrend_to_experimental_recovery_strategy(self):
        bot = object.__new__(TradingBot)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 3_600_000
        candles = []
        for index in range(2501):
            close = 100 * (0.999 ** index)
            candles.append([
                now_ms - (2500 - index) * 3_600_000,
                close, close * 1.002, close * 0.998, close, 100.0,
            ])
        bot.config = SimpleNamespace(strategy=SimpleNamespace(
            name="dynamic_regime", timeframe="1h", lookback_candles=2501,
            enable_downtrend_recovery_testnet=True,
        ), enable_notifications=False)
        bot.exchange = Mock()
        bot.exchange.fetch_closed_ohlcv.return_value = candles
        bot.exchange.get_ticker.return_value = {"bid": 70.0, "ask": 70.1}
        bot.strategy = Mock()
        bot.downtrend_recovery_strategy = Mock()
        bot.downtrend_recovery_strategy.analyze.return_value = TradeSignal(
            Signal.HOLD, "BTC/USDT", 0.0, "No RSI recovery entry", {"strategy": "downtrend_rsi_recovery_testnet"}
        )
        bot.risk_manager = SimpleNamespace(open_positions={}, store=Mock())
        bot._last_analyzed_candle = {}
        bot._analysis_error_states = {}
        bot.last_signals = {}
        bot._handle_buy_signal = Mock()
        bot._handle_sell_signal = Mock()
        with patch("bot_engine.evaluate_shadow_candidate", return_value={"signal": "HOLD", "status": "test"}):
            bot.analyze_market("BTC/USDT")
        bot.downtrend_recovery_strategy.analyze.assert_called_once()
        bot.strategy.analyze.assert_not_called()
        self.assertEqual(bot.last_signals["BTC/USDT"]["regime_router"]["selected_strategy"], "downtrend_rsi_recovery_testnet")

    def test_dynamic_profile_downtrend_recovery_disabled_by_default(self):
        bot = object.__new__(TradingBot)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 3_600_000
        candles = []
        for index in range(2501):
            close = 100 * (0.999 ** index)
            candles.append([
                now_ms - (2500 - index) * 3_600_000,
                close, close * 1.002, close * 0.998, close, 100.0,
            ])
        bot.config = SimpleNamespace(strategy=SimpleNamespace(
            name="dynamic_regime", timeframe="1h", lookback_candles=2501,
        ), enable_notifications=False)
        bot.exchange = Mock()
        bot.exchange.fetch_closed_ohlcv.return_value = candles
        bot.exchange.get_ticker.return_value = {"bid": 70.0, "ask": 70.1}
        bot.strategy = Mock()
        bot.downtrend_recovery_strategy = Mock()
        bot.risk_manager = SimpleNamespace(open_positions={}, store=Mock())
        bot._last_analyzed_candle = {}
        bot._analysis_error_states = {}
        bot.last_signals = {}
        bot._handle_buy_signal = Mock()
        bot._handle_sell_signal = Mock()
        with patch("bot_engine.evaluate_shadow_candidate", return_value={"signal": "HOLD", "status": "test"}):
            bot.analyze_market("BTC/USDT")
        # The proven-net-negative recovery route must not be evaluated for entry.
        bot.downtrend_recovery_strategy.analyze.assert_not_called()
        bot.strategy.analyze.assert_not_called()
        self.assertEqual(bot.last_signals["BTC/USDT"]["signal"], "HOLD")
        router = bot.last_signals["BTC/USDT"]["regime_router"]
        self.assertEqual(router["regime"], "downtrend")
        self.assertIsNone(router["selected_strategy"])
        self.assertFalse(router["entry_route_enabled"])
        self.assertIn("PF < 1", router["reason"])

    def test_equity_circuit_breakers_still_run_when_reconciliation_fails(self):
        import threading
        from decimal import Decimal

        bot = object.__new__(TradingBot)
        bot._cycle_lock = threading.Lock()
        bot.config = SimpleNamespace(validate=lambda: [], strategy=SimpleNamespace(trading_pairs=["BTC/USDT"]))
        bot._cycle_count = 0
        bot._reconcile_exchange_state = Mock(return_value=False)
        bot._last_reconciliation_ok = False
        bot._bot_managed_equity = Mock(return_value=Decimal("10000"))
        bot.risk_manager = SimpleNamespace(
            monitor_equity=Mock(return_value=True),
            _halt_reason="", _is_halted=False, open_positions={},
        )
        bot.check_stop_loss_take_profit = Mock()

        result = bot.run_cycle()

        self.assertFalse(result)
        # Loss limits still fire on stale-but-local equity during an outage.
        bot.risk_manager.monitor_equity.assert_called_once_with(10000.0, account_reconciled=False)
        # Order activity (soft stop/target monitoring) stays blocked until reconciliation.
        bot.check_stop_loss_take_profit.assert_not_called()

    def test_cycle_marks_worker_degraded_but_continues_other_symbols_after_analysis_error(self):
        import threading

        bot = object.__new__(TradingBot)
        bot._cycle_lock = threading.Lock()
        bot.config = SimpleNamespace(
            validate=lambda: [],
            strategy=SimpleNamespace(trading_pairs=["BTC/USDT", "ETH/USDT"]),
        )
        bot._cycle_count = 0
        bot._analysis_error_states = {}
        bot._reconcile_exchange_state = Mock(return_value=True)
        bot._enforce_equity_circuit_breakers = Mock()
        bot.check_stop_loss_take_profit = Mock()
        bot.risk_manager = SimpleNamespace(
            _is_halted=False,
            open_positions={},
            store=Mock(),
        )
        bot.analyze_market = Mock(side_effect=[RuntimeError("ticker unavailable"), None])
        bot.print_status = Mock()

        result = bot.run_cycle()

        self.assertFalse(result)
        self.assertEqual(bot.analyze_market.call_args_list[1].args, ("ETH/USDT",))
        bot.risk_manager.store.append_event.assert_called_once_with("analysis_error", {
            "symbol": "BTC/USDT", "error_type": "RuntimeError",
        })

    def test_reconciliation_failure_alerts_once_even_when_drawdown_halt_already_exists(self):
        bot = object.__new__(TradingBot)
        bot.config = SimpleNamespace(
            exchange=SimpleNamespace(paper_trading=True, paper_environment="demo"),
        )
        bot.exchange = Mock()
        bot.exchange.fetch_account_snapshot.side_effect = ValueError("private detail must not be emailed")
        bot.risk_manager = SimpleNamespace(
            _is_halted=True,
            _halt_reason="Daily equity drawdown limit reached: 8.70%",
            _save_daily_state=Mock(),
            store=Mock(),
        )
        bot._last_reconciliation_ok = False
        bot._last_account_error = ""
        bot._last_account_balance = None
        bot._last_reconciliation_alert_signature = None
        bot._notify_safety_halt = Mock()

        self.assertFalse(bot._reconcile_exchange_state())
        self.assertFalse(bot._reconcile_exchange_state())

        bot._notify_safety_halt.assert_called_once()
        alert = bot._notify_safety_halt.call_args.args[0]
        self.assertIn("New entries remain blocked", alert)
        self.assertNotIn("private detail must not be emailed", alert)

    def test_dashboard_explains_exchange_balance_mismatch_in_plain_language(self):
        message = _friendly_account_reconciliation_issue(
            "Exchange reconciliation failed: balance mismatch for configured asset BTC",
            "Account reconciliation failed. Check exchange permissions and service logs.",
        )
        self.assertIn("different BTC balance", message)
        self.assertIn("New entries are paused", message)

    def test_candle_gap_is_saved_as_an_auditable_market_data_issue(self):
        bot = object.__new__(TradingBot)
        bot.config = SimpleNamespace(strategy=SimpleNamespace(timeframe="1h", lookback_candles=150))
        bot.exchange = Mock()
        bot.exchange.fetch_closed_ohlcv.return_value = []
        bot.exchange._candle_sequence_issues = {"BTC/USDT": {
            "expected_timestamp": 1_700_000_000_000,
            "actual_timestamp": 1_700_007_200_000,
            "step_ms": 7_200_000,
            "missing_intervals": 1,
        }}
        bot.risk_manager = SimpleNamespace(store=Mock())
        bot._analysis_error_states = {}

        bot.analyze_market("BTC/USDT")

        event = bot.risk_manager.store.append_event.call_args.args[1]
        self.assertEqual(event["issue"], "candle_sequence_gap")
        self.assertEqual(event["missing_intervals"], 1)

    def test_shadow_candidate_signal_is_logged_but_never_routes_to_order_handlers(self):
        bot = object.__new__(TradingBot)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 3_600_000
        candles = [[now_ms - (149 - i) * 3_600_000, 100, 101, 99, 100, 10] for i in range(150)]
        bot.config = SimpleNamespace(strategy=SimpleNamespace(timeframe="1h", lookback_candles=150))
        bot.exchange = Mock()
        bot.exchange.fetch_closed_ohlcv.return_value = candles
        bot.exchange.get_ticker.return_value = {"bid": 100.0, "ask": 100.2}
        bot.strategy = Mock()
        bot.strategy.analyze.return_value = TradeSignal(Signal.HOLD, "BTC/USDT", 0.0, "Active strategy holds", {})
        bot.risk_manager = SimpleNamespace(store=Mock())
        bot._last_analyzed_candle = {}
        bot._analysis_error_states = {}
        bot.last_signals = {}
        bot._handle_buy_signal = Mock()
        bot._handle_sell_signal = Mock()

        with patch("bot_engine.evaluate_shadow_candidate", return_value={
            "candidate_id": "volume_breakout_24h_ema100_v1", "signal": "BUY", "status": "ok",
        }):
            bot.analyze_market("BTC/USDT")

        bot._handle_buy_signal.assert_not_called()
        bot._handle_sell_signal.assert_not_called()
        bot.exchange.fetch_closed_ohlcv.assert_called_once_with("BTC/USDT", "1h", 150)
        event = bot.risk_manager.store.append_event.call_args.args[1]
        self.assertEqual(event["signal"], "HOLD")
        self.assertEqual(event["shadow"]["signal"], "BUY")
        self.assertEqual(event["spread_sample_status"], "ok")
        self.assertEqual(event["measured_spread_bps"], round((0.2 / 100.1) * 10000, 2))

    def test_ema72_signal_analysis_routes_exit_only_when_position_is_tracked(self):
        bot = object.__new__(TradingBot)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 3_600_000
        candles = [[now_ms - (2499 - i) * 3_600_000, 100, 101, 99, 100, 10] for i in range(2500)]
        bot.config = SimpleNamespace(strategy=SimpleNamespace(
            name="macro_donchian_ema72", timeframe="1h", lookback_candles=2501,
        ))
        bot.exchange = Mock()
        bot.exchange.fetch_closed_ohlcv.return_value = candles
        bot.exchange.get_ticker.return_value = {"bid": 99.9, "ask": 100.1}
        bot.strategy = Mock()
        bot.strategy.analyze.return_value = TradeSignal(
            Signal.SELL, "BTC/USDT", 0.85, "EMA72 exit", {"strategy": "macro_donchian_ema72"}
        )
        bot.risk_manager = SimpleNamespace(open_positions={"BTC/USDT": object()}, store=Mock())
        bot._last_analyzed_candle = {}
        bot._analysis_error_states = {}
        bot.last_signals = {}
        bot._handle_buy_signal = Mock()
        bot._handle_sell_signal = Mock()

        with patch("bot_engine.evaluate_shadow_candidate", return_value={"signal": "HOLD", "status": "test"}):
            bot.analyze_market("BTC/USDT")

        bot.strategy.analyze.assert_called_once()
        self.assertTrue(bot.strategy.analyze.call_args.kwargs["in_position"])
        bot._handle_sell_signal.assert_called_once()
        bot._handle_buy_signal.assert_not_called()

    def test_shadow_candidate_requires_history_and_uses_completed_candles(self):
        self.assertEqual(evaluate_shadow_candidate([])["status"], "insufficient_data")

    def test_risk_equity_excludes_preexisting_exchange_seed_balances(self):
        bot = object.__new__(TradingBot)
        bot._last_account_balance = {"total": {
            "USDT": "10000", "BTC": "1.1", "ETH": "1", "SOL": "4",
        }}
        bot.risk_manager = SimpleNamespace(open_positions={})
        bot.exchange = Mock()

        self.assertEqual(bot._bot_managed_equity(), Decimal("10000"))
        bot.exchange.get_current_price.assert_not_called()

        bot.risk_manager.open_positions["BTC/USDT"] = Position(
            "BTC/USDT", "long", 50000.0, 0.1,
            datetime.now(timezone.utc).isoformat(), 49000.0, 51500.0,
        )
        bot.exchange.get_current_price.return_value = 51000.0
        self.assertEqual(bot._bot_managed_equity(), Decimal("15100.00"))
        bot.exchange.get_current_price.assert_called_once_with("BTC/USDT")

    def test_client_order_id_is_deterministic_and_exchange_sized(self):
        first = TradingBot._client_order_id("BTC/USDT", "buy", "candle-123")
        second = TradingBot._client_order_id("BTC/USDT", "buy", "candle-123")
        different = TradingBot._client_order_id("BTC/USDT", "buy", "candle-124")
        self.assertEqual(first, second)
        self.assertNotEqual(first, different)
        self.assertLessEqual(len(first), 36)

    def test_post_only_chase_sends_unique_client_ids_per_requote(self):
        bot = object.__new__(TradingBot)
        bot.config = SimpleNamespace(max_slippage_bps=30)
        bot.exchange = Mock()
        bot.exchange.get_order_book.return_value = {"bids": [[100.0, 10.0]]}
        # First attempt stays open (must be canceled), second fills as a maker.
        bot.exchange.place_post_only_limit_buy.side_effect = [
            {"id": "a", "status": "open", "filled": 0.0},
            {"id": "b", "status": "closed", "filled": 0.01, "average": 100.0},
        ]
        bot.exchange.fetch_order.return_value = {"id": "a", "status": "canceled", "filled": 0.0}
        bot.risk_manager = Mock()
        bot.risk_manager.store = Mock()

        order, client_id = bot._post_only_limit_chase_buy("BTC/USDT", 0.01, "decision-1")

        self.assertIsNotNone(order)
        self.assertEqual(order["id"], "b")
        sent_ids = [call.args[3] for call in bot.exchange.place_post_only_limit_buy.call_args_list]
        self.assertEqual(len(sent_ids), 2)
        self.assertEqual(sent_ids[0], client_id)
        self.assertNotEqual(sent_ids[0], sent_ids[1])
        bot.exchange.cancel_order.assert_called_once_with("a", "BTC/USDT")
        bot.exchange.fetch_order.assert_called_once_with("a", "BTC/USDT")

    def test_post_only_chase_requotes_only_after_confirmed_zero_fill_cancel(self):
        bot = object.__new__(TradingBot)
        bot.config = SimpleNamespace(max_slippage_bps=30)
        bot.exchange = Mock()
        bot.exchange.get_order_book.return_value = {"bids": [[100.0, 10.0]]}
        bot.exchange.place_post_only_limit_buy.side_effect = [
            {"id": "a", "status": "open", "filled": 0.0},
            {"id": "b", "status": "open", "filled": 0.0},
        ]
        bot.exchange.fetch_order.side_effect = [
            {"id": "a", "status": "canceled", "filled": 0.0},
            {"id": "b", "status": "canceled", "filled": 0.0},
        ]
        bot.risk_manager = Mock()
        bot.risk_manager.store = Mock()

        order, client_id = bot._post_only_limit_chase_buy(
            "BTC/USDT", 0.01, "decision-no-fill", max_requotes=2
        )

        self.assertIsNone(order)
        self.assertEqual(bot.exchange.place_post_only_limit_buy.call_count, 2)
        self.assertEqual(bot.exchange.cancel_order.call_count, 2)
        self.assertEqual(bot.exchange.fetch_order.call_count, 2)
        bot.risk_manager.store.update_order.assert_called_with(
            client_id, "CANCELED", exchange_order_id="b", filled=Decimal("0.0"),
            average=None, response={"id": "b", "status": "canceled", "filled": 0.0},
        )
        bot.risk_manager.store.mark_order_applied.assert_called_once_with(client_id)

    def test_post_only_chase_does_not_requote_when_cancel_remains_open(self):
        bot = object.__new__(TradingBot)
        bot.config = SimpleNamespace(max_slippage_bps=30)
        bot.exchange = Mock()
        bot.exchange.get_order_book.return_value = {"bids": [[100.0, 10.0]]}
        bot.exchange.place_post_only_limit_buy.return_value = {
            "id": "a", "status": "open", "filled": 0.0,
        }
        bot.exchange.fetch_order.return_value = {
            "id": "a", "status": "open", "filled": 0.0,
        }
        bot.exchange.fetch_order_by_client_id.side_effect = RuntimeError("not terminal")
        bot.risk_manager = Mock()
        bot.risk_manager.store = Mock()
        bot.risk_manager.store.get_order.return_value = {"state": "UNKNOWN"}

        with self.assertRaisesRegex(RuntimeError, "outcome remains unknown"):
            bot._post_only_limit_chase_buy(
                "BTC/USDT", 0.01, "decision-cancel-uncertain", max_requotes=5
            )

        bot.exchange.place_post_only_limit_buy.assert_called_once()
        self.assertEqual(bot.risk_manager.store.update_order.call_count, 2)
        self.assertEqual(bot.risk_manager.store.update_order.call_args.args[1], "UNKNOWN")

    def test_post_only_partial_fill_stops_chase_and_preserves_unapplied_inventory(self):
        bot = object.__new__(TradingBot)
        bot.config = SimpleNamespace(max_slippage_bps=30)
        bot.exchange = Mock()
        bot.exchange.get_order_book.return_value = {"bids": [[100.0, 10.0]]}
        bot.exchange.place_post_only_limit_buy.return_value = {
            "id": "a", "status": "open", "filled": 0.002,
        }
        bot.exchange.fetch_order.return_value = {
            "id": "a", "status": "canceled", "filled": 0.002, "average": 100.0,
        }
        bot.risk_manager = Mock()
        bot.risk_manager.store = Mock()

        order, client_id = bot._post_only_limit_chase_buy(
            "BTC/USDT", 0.01, "decision-partial", max_requotes=5
        )

        self.assertEqual(order["status"], "canceled")
        bot.exchange.place_post_only_limit_buy.assert_called_once()
        self.assertEqual(bot.risk_manager.store.update_order.call_count, 2)
        self.assertEqual(bot.risk_manager.store.update_order.call_args.args[1], "PARTIAL_CLOSED")
        bot.risk_manager.store.mark_order_applied.assert_not_called()

    def test_reconciliation_clears_maker_intent_only_after_all_quotes_are_terminal_unfilled(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            store = bot.risk_manager.store
            client_id = TradingBot._client_order_id("SOL/USDT", "buy", "candle-1")
            store.create_order_intent(
                client_id, "SOL/USDT", "buy", "6.917",
                {"symbol": "SOL/USDT", "side": "buy", "amount": "6.917", "post_only": True},
            )
            store.update_order(client_id, "SUBMITTED")
            bot.exchange = Mock()
            bot.exchange.fetch_order_by_client_id.side_effect = [
                {"id": f"order-{i}", "status": "canceled", "filled": 0}
                for i in range(5)
            ]

            bot._reconcile_terminal_no_fill_order_intents()

            self.assertEqual(store.unresolved_orders(), [])
            reconciled = store.get_order(client_id)
            self.assertEqual(reconciled["state"], "CANCELED")
            self.assertEqual(reconciled["applied"], 1)
            self.assertEqual(bot.exchange.fetch_order_by_client_id.call_count, 5)
            self.assertTrue(any(
                event["event_type"] == "order_reconciled_no_fill"
                for event in store.list_events(10)
            ))

    def test_reconciliation_accepts_postgres_jsonb_request_mapping(self):
        bot = object.__new__(TradingBot)
        bot.risk_manager = Mock()
        client_id = TradingBot._client_order_id("SOL/USDT", "buy", "supabase-jsonb")
        bot.risk_manager.store.unresolved_orders.return_value = [{
            "client_order_id": client_id,
            "symbol": "SOL/USDT",
            "side": "BUY",
            "state": "UNKNOWN",
            "exchange_order_id": None,
            "request_json": {
                "symbol": "SOL/USDT", "side": "buy", "amount": "6.917", "post_only": True,
            },
        }]
        bot.exchange = Mock()
        bot.exchange.fetch_order_by_client_id.side_effect = [
            {"id": f"order-{i}", "status": "canceled", "filled": 0}
            for i in range(5)
        ]

        bot._reconcile_terminal_no_fill_order_intents()

        self.assertEqual(bot.exchange.fetch_order_by_client_id.call_count, 5)
        bot.risk_manager.store.update_order.assert_called_once()
        bot.risk_manager.store.mark_order_applied.assert_called_once_with(client_id)
        bot.risk_manager.store.append_event.assert_called_once()

    def test_reconciliation_keeps_maker_intent_unresolved_if_any_quote_is_open(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            store = bot.risk_manager.store
            client_id = TradingBot._client_order_id("SOL/USDT", "buy", "candle-2")
            store.create_order_intent(
                client_id, "SOL/USDT", "buy", "6.917",
                {"symbol": "SOL/USDT", "side": "buy", "amount": "6.917", "post_only": True},
            )
            store.update_order(client_id, "SUBMITTED")
            bot.exchange = Mock()
            bot.exchange.fetch_order_by_client_id.side_effect = [
                {"id": "order-0", "status": "canceled", "filled": 0},
                {"id": "order-1", "status": "open", "filled": 0},
            ]

            with self.assertRaisesRegex(
                    ValueError,
                    r"unresolved local order intent exists \(attempt_not_confirmed_zero_fill:open\)"):
                bot._reconcile_terminal_no_fill_order_intents()

            self.assertEqual(store.get_order(client_id)["state"], "SUBMITTED")
            self.assertEqual(len(store.unresolved_orders()), 1)

    def test_only_confirmed_complete_fills_are_accepted(self):
        self.assertTrue(TradingBot._confirmed_full_fill({"status": "closed", "filled": 1.0}, 1.0))
        self.assertFalse(TradingBot._confirmed_full_fill({"status": "open", "filled": 1.0}, 1.0))
        self.assertFalse(TradingBot._confirmed_full_fill({"status": "closed", "filled": 0.5}, 1.0))
        self.assertFalse(TradingBot._confirmed_full_fill({"status": "closed", "filled": None}, 1.0))

    def test_post_fill_stop_risk_includes_fee_and_enforces_cap(self):
        risk, within_cap = TradingBot._post_fill_stop_risk(1, 102, 100, 1, 3)
        self.assertEqual(risk, Decimal("3"))
        self.assertTrue(within_cap)
        risk, within_cap = TradingBot._post_fill_stop_risk(1, 103, 100, 1, 3)
        self.assertEqual(risk, Decimal("4"))
        self.assertFalse(within_cap)
        self.assertEqual(TradingBot._post_fill_stop_risk(1, 99, 100, 0, 3), (None, False))

    def test_base_asset_fee_is_removed_from_tracked_fill_quantity(self):
        net = TradingBot._net_base_filled({
            "filled": "1.000", "fees": [{"currency": "BTC", "cost": "0.001"}],
        }, "BTC/USDT")
        self.assertEqual(str(net), "0.999")
        self.assertIsNone(TradingBot._net_base_filled({"filled": "1.0"}, "BTC/USDT"))

    def test_fee_accounting_converts_base_and_quote_but_rejects_unknown_asset(self):
        self.assertAlmostEqual(TradingBot._fee_quote_cost(
            {"fees": [{"currency": "BTC", "cost": "0.001"}]}, "BTC/USDT", 50000
        ), 50.0)
        self.assertAlmostEqual(TradingBot._fee_quote_cost(
            {"fee": {"currency": "USDT", "cost": "2.5"}}, "BTC/USDT", 50000
        ), 2.5)
        self.assertIsNone(TradingBot._fee_quote_cost(
            {"fee": {"currency": "BNB", "cost": "0.01"}}, "BTC/USDT", 50000
        ))

    def test_fee_accounting_converts_third_asset_fee_when_priced(self):
        # BNB fee on a BTC/USDT trade, priced via a contemporaneous BNB/USDT quote.
        self.assertAlmostEqual(TradingBot._fee_quote_cost(
            {"fees": [{"currency": "BNB", "cost": "0.01"}]}, "BTC/USDT", 50000,
            third_asset_prices={"BNB": 300.0},
        ), 3.0)
        # A missing third-asset price still fails closed rather than miscounting.
        self.assertIsNone(TradingBot._fee_quote_cost(
            {"fees": [{"currency": "BNB", "cost": "0.01"}]}, "BTC/USDT", 50000,
            third_asset_prices={},
        ))

    def test_resolve_fee_quote_cost_prices_third_asset_via_exchange(self):
        bot = object.__new__(TradingBot)
        bot.exchange = Mock()
        bot.exchange.get_current_price.return_value = 300.0
        order = {"id": "order-1", "filled": "1.0", "fees": [{"currency": "BNB", "cost": "0.02"}]}
        self.assertAlmostEqual(bot._resolve_fee_quote_cost(order, "BTC/USDT", 100.0), 6.0)
        # An unavailable third-asset price must fail closed.
        bot.exchange.get_current_price.return_value = None
        self.assertIsNone(bot._resolve_fee_quote_cost(order, "BTC/USDT", 100.0))

    def test_full_round_trip_reconciles_wallet_delta_to_realized_pnl(self):
        # End-to-end: drive the real order-submission and close paths against a
        # deterministic wallet, then assert realized PnL equals the wallet delta.
        with TemporaryDirectory() as persistence_dir:
            fake = FakeExchange(quote_balance=10000.0, price=100.0, fee_rate=0.001)
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(enable_notifications=False)
            bot.exchange = fake
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.trade_log_path = Path(persistence_dir) / "trades.jsonl"

            symbol = "BTC/USDT"
            entry_price = 100.0
            exit_price = 110.0
            fake.set_price(symbol, entry_price)

            # BUY through the real submission/journaling path.
            buy_order, buy_client_id = bot._submit_market_order(symbol, "buy", 1.0, "e2e-buy")
            self.assertEqual(buy_order["status"], "closed")

            # Compute net base and entry fee exactly as the entry path does.
            net_amount = float(bot._net_base_filled(buy_order, symbol))
            entry_fee = bot._resolve_fee_quote_cost(buy_order, symbol, entry_price)
            self.assertIsNotNone(net_amount)
            self.assertIsNotNone(entry_fee)
            position = bot.risk_manager.open_position(
                symbol, entry_price, net_amount, entry_fee_quote=entry_fee,
                order_id=buy_order["id"],
            )
            self.assertIsNotNone(position)

            # SELL through the real signal handler.
            fake.set_price(symbol, exit_price)
            signal = SimpleNamespace(symbol=symbol, reason="e2e exit", confidence=1.0)
            bot._handle_sell_signal(signal, "e2e-sell")

            self.assertNotIn(symbol, bot.risk_manager.open_positions)
            ledger = bot.risk_manager.trade_ledger()
            self.assertEqual(len(ledger), 1)
            row = ledger[0]
            self.assertTrue(row["accounting_complete"])
            self.assertIsNotNone(row["net_pnl"])

            # Money invariant: realized net PnL must equal the wallet's quote delta.
            wallet_delta = float(fake.quote) - 10000.0
            self.assertAlmostEqual(row["net_pnl"], wallet_delta, places=6)
            # 1.0 base * 0.001 = 0.001 base fee (entry) = $0.10; sell fee is quote.
            self.assertAlmostEqual(row["fee"], 0.20989, places=6)

            summary = bot.risk_manager.get_performance_summary()
            self.assertAlmostEqual(summary["total_fees"], 0.21, places=6)
            self.assertAlmostEqual(summary["total_pnl"], 9.78, places=6)

    def test_fee_accounting_recovers_verified_order_fill_fees(self):
        bot = object.__new__(TradingBot)
        bot.exchange = Mock()
        bot.exchange.fetch_order_trades.return_value = [
            {"amount": "0.4", "fee": {"currency": "USDT", "cost": "0.04"}},
            {"amount": "0.6", "fee": {"currency": "USDT", "cost": "0.06"}},
        ]
        self.assertAlmostEqual(
            bot._resolve_fee_quote_cost({"id": "order-1", "filled": "1.0"}, "BTC/USDT", 100.0),
            0.1,
        )
        bot.exchange.fetch_order_trades.return_value[1]["amount"] = "0.5"
        self.assertIsNone(bot._resolve_fee_quote_cost(
            {"id": "order-1", "filled": "1.0"}, "BTC/USDT", 100.0
        ))

    def test_entry_and_exit_fees_are_both_counted_in_realized_pnl(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            rm.open_position("BTC/USDT", 100.0, 1.0, entry_fee_quote=1.0)
            record = rm.close_position("BTC/USDT", 110.0, "test", fee_cost=2.0)
            self.assertEqual(record.fee, 3.0)
            self.assertEqual(record.pnl, 7.0)

    def test_trade_ledger_reports_reconciled_tax_lot_shape(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            rm.open_position("BTC/USDT", 100.0, 1.0, entry_fee_quote=1.0)
            rm.close_position("BTC/USDT", 110.0, "test", fee_cost=2.0)
            ledger = rm.trade_ledger()
            self.assertEqual(len(ledger), 1)
            row = ledger[0]
            self.assertEqual(row["symbol"], "BTC/USDT")
            self.assertEqual(row["entry_price"], 100.0)
            self.assertEqual(row["exit_price"], 110.0)
            self.assertEqual(row["amount"], 1.0)
            self.assertEqual(row["proceeds"], 110.0)
            self.assertEqual(row["cost_basis"], 100.0)
            self.assertEqual(row["gross_pnl"], 10.0)
            self.assertEqual(row["fee"], 3.0)
            self.assertEqual(row["net_pnl"], 7.0)
            self.assertTrue(row["accounting_complete"])

    def test_trade_ledger_csv_has_header_and_row(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            rm.open_position("BTC/USDT", 100.0, 1.0, entry_fee_quote=1.0)
            rm.close_position("BTC/USDT", 110.0, "test", fee_cost=2.0)
            csv_text = rm.trade_ledger_csv()
            lines = csv_text.strip().splitlines()
            self.assertGreaterEqual(len(lines), 2)
            self.assertIn("net_pnl", lines[0])
            self.assertIn("BTC/USDT", csv_text)

    def test_trade_ledger_csv_emits_header_when_empty(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            csv_text = rm.trade_ledger_csv()
            self.assertTrue(csv_text.strip().startswith("symbol,side,strategy"))

    def test_performance_summary_includes_total_fees(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            rm.open_position("BTC/USDT", 100.0, 1.0, entry_fee_quote=1.0)
            rm.close_position("BTC/USDT", 110.0, "test", fee_cost=2.0)
            summary = rm.get_performance_summary()
            self.assertEqual(summary["total_fees"], 3.0)
            self.assertEqual(summary["total_pnl"], 7.0)

    def test_performance_summary_empty_reports_zero_fees(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            summary = rm.get_performance_summary()
            self.assertEqual(summary["total_fees"], 0.0)

    def test_corrupt_position_state_halts_new_entries(self):
        with TemporaryDirectory() as persistence_dir:
            state_path = Path(persistence_dir) / "open_positions.json"
            state_path.write_text("{broken", encoding="utf-8")
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            can_open, reason = rm.can_open_position("BTC/USDT", 10_000.0)
            self.assertFalse(can_open)
            self.assertTrue(rm._is_halted)
            self.assertIn("unreadable", reason.lower())

    def test_position_persistence_failure_halts_new_entries(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            with patch.object(rm.store, "write_positions", side_effect=OSError("disk unavailable")):
                rm.open_position("BTC/USDT", 100.0, 0.1)
            self.assertTrue(rm._is_halted)
            self.assertIn("could not be saved", rm._halt_reason)

    def test_missing_open_position_price_halts_new_trading(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            rm.open_position("BTC/USDT", 100.0, 0.1)
            rm.check_positions(lambda symbol: 0.0)
            can_open, reason = rm.can_open_position("ETH/USDT", 10_000.0)
            self.assertFalse(can_open)
            self.assertTrue(rm._is_halted)
            self.assertIn("market price", reason.lower())

    def test_seeded_testnet_inventory_is_baselined_not_mistaken_for_a_position(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(
                exchange=SimpleNamespace(paper_trading=True, paper_environment="testnet"),
                strategy=SimpleNamespace(trading_pairs=["BTC/USDT"]),
            )
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.fetch_account_snapshot.return_value = ({"total": {"BTC": "1.25", "USDT": "10000"}}, [])
            bot.exchange.exchange.load_markets.return_value = None
            bot.exchange.exchange.market.return_value = {"precision": {"amount": 8}}
            bot.exchange.exchange.precisionMode = 2
            bot.exchange.amount_to_precision.return_value = "1.00000000"
            bot._last_reconciliation_ok = False
            self.assertTrue(bot._reconcile_exchange_state())
            self.assertEqual(bot.risk_manager.store.read_state("baseline_asset_balances"), {"BTC": "1.25"})

    def test_reconciliation_auto_records_untradable_dust_balance(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(
                exchange=SimpleNamespace(paper_trading=True, paper_environment="testnet"),
                strategy=SimpleNamespace(trading_pairs=["XRP/USDT"]),
            )
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.risk_manager.store.write_state("baseline_asset_balances", {"XRP": "0"})
            bot.exchange = Mock()
            bot.exchange.fetch_account_snapshot.return_value = ({"total": {"XRP": "0.0985", "USDT": "10000"}}, [])
            bot.exchange.exchange.load_markets.return_value = None
            bot.exchange.exchange.market.return_value = {"precision": {"amount": 8}}
            bot.exchange.exchange.precisionMode = 2
            bot.exchange.get_market_limits.return_value = {"min_cost": 10}
            bot.exchange.get_current_price.return_value = 0.95
            bot._last_reconciliation_ok = False

            self.assertTrue(bot._reconcile_exchange_state())
            self.assertEqual(bot.risk_manager.store.read_state("baseline_asset_balances"), {"XRP": "0.0985"})
            events = bot.risk_manager.store.list_events(10)
            self.assertTrue(any(event["event_type"] == "dust_balance_rebased" for event in events))

    def test_reconciliation_does_not_auto_record_tradeable_balance(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(
                exchange=SimpleNamespace(paper_trading=True, paper_environment="testnet"),
                strategy=SimpleNamespace(trading_pairs=["XRP/USDT"]),
            )
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.risk_manager.store.write_state("baseline_asset_balances", {"XRP": "0"})
            bot.exchange = Mock()
            bot.exchange.fetch_account_snapshot.return_value = ({"total": {"XRP": "0.0985", "USDT": "10000"}}, [])
            bot.exchange.exchange.load_markets.return_value = None
            bot.exchange.exchange.market.return_value = {"precision": {"amount": 8}}
            bot.exchange.exchange.precisionMode = 2
            bot.exchange.get_market_limits.return_value = {"min_cost": 10}
            bot.exchange.get_current_price.return_value = 500.0  # 0.0985 * 500 = 49.25, tradeable
            bot._last_reconciliation_ok = False

            self.assertFalse(bot._reconcile_exchange_state())
            self.assertEqual(bot.risk_manager.store.read_state("baseline_asset_balances"), {"XRP": "0"})

    def test_below_minimum_notional_uses_exchange_limits(self):
        bot = object.__new__(TradingBot)
        bot.exchange = Mock()
        bot.exchange.get_market_limits.return_value = {"min_cost": 10}
        bot.exchange.get_current_price.return_value = 0.95
        self.assertTrue(bot._below_minimum_notional("XRP/USDT", Decimal("0.0985")))
        bot.exchange.get_current_price.return_value = 500.0
        self.assertFalse(bot._below_minimum_notional("XRP/USDT", Decimal("0.0985")))
        bot.exchange.get_market_limits.return_value = {}
        self.assertFalse(bot._below_minimum_notional("XRP/USDT", Decimal("0.0985")))
        bot.exchange.get_market_limits.return_value = {"min_cost": 10}
        bot.exchange.get_current_price.return_value = None
        self.assertFalse(bot._below_minimum_notional("XRP/USDT", Decimal("0.0985")))

    def test_paper_environment_switch_rebaselines_and_pauses_entries(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(
                exchange=SimpleNamespace(paper_trading=True, paper_environment="demo"),
                strategy=SimpleNamespace(trading_pairs=["BTC/USDT"]),
            )
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.risk_manager.store.write_state("paper_environment", "testnet")
            bot.risk_manager.store.write_state("baseline_asset_balances", {"BTC": "1.25"})
            bot.exchange = Mock()
            bot.exchange.fetch_account_snapshot.return_value = (
                {"total": {"BTC": "0.5", "USDT": "5000"}}, []
            )
            bot.exchange.exchange.load_markets.return_value = None
            bot.exchange.exchange.market.return_value = {"precision": {"amount": 8}}
            bot.exchange.exchange.precisionMode = 2
            bot.exchange.amount_to_precision.return_value = "1.00000000"
            bot._last_reconciliation_ok = False

            self.assertTrue(bot._reconcile_exchange_state())
            self.assertEqual(bot.risk_manager.store.read_state("paper_environment"), "demo")
            self.assertEqual(bot.risk_manager.store.read_state("baseline_asset_balances"), {"BTC": "0.5"})
            self.assertEqual(bot.risk_manager.entry_mode(), "EXIT_ONLY")
            self.assertEqual(bot.risk_manager._daily_starting_equity, 5000.0)
            events = bot.risk_manager.store.list_events(10)
            self.assertTrue(any(event["event_type"] == "paper_environment_changed" for event in events))

    def test_paper_environment_switch_refuses_existing_position(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(
                exchange=SimpleNamespace(paper_trading=True, paper_environment="demo"),
                strategy=SimpleNamespace(trading_pairs=["BTC/USDT"]),
            )
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.risk_manager.store.write_state("paper_environment", "testnet")
            bot.risk_manager.store.write_state("baseline_asset_balances", {"BTC": "1.25"})
            bot.risk_manager.open_position("BTC/USDT", 100.0, 0.1)
            bot.exchange = Mock()
            bot.exchange.fetch_account_snapshot.return_value = ({"total": {"BTC": "1.35", "USDT": "5000"}}, [])
            bot._last_reconciliation_ok = False

            self.assertFalse(bot._reconcile_exchange_state())
            self.assertEqual(bot.risk_manager.store.read_state("paper_environment"), "testnet")
            self.assertEqual(bot.risk_manager.store.read_state("baseline_asset_balances"), {"BTC": "1.25"})
            self.assertTrue(bot.risk_manager._is_halted)

    def test_native_stop_is_submitted_as_market_stop_with_persistable_client_id(self):
        connector = object.__new__(ExchangeConnector)
        connector.exchange = Mock()
        connector.price_to_precision = Mock(return_value="98.00")
        connector.exchange.create_order.return_value = {"id": "stop-1", "status": "open"}

        result = connector.place_native_stop_loss("BTC/USDT", "0.1", 98.0, "stop-client-1")
        self.assertEqual(result["id"], "stop-1")
        connector.exchange.create_order.assert_called_once_with(
            "BTC/USDT", "market", "sell", 0.1, None,
            {"stopLossPrice": "98.00", "clientOrderId": "stop-client-1"},
        )

    def test_reconciliation_closes_stop_filled_at_exchange_amount_precision_and_allows_reviewed_resume(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(
                exchange=SimpleNamespace(paper_trading=True, paper_environment="demo"),
                strategy=SimpleNamespace(trading_pairs=["XRP/USDT"]),
            )
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.trade_log_path = Path(persistence_dir) / "trades.jsonl"
            bot.risk_manager.store.write_state("paper_environment", "demo")
            bot.risk_manager.store.write_state("baseline_asset_balances", {"XRP": "1"})
            position = bot.risk_manager.open_position(
                "XRP/USDT", 0.9943, 500.9985, order_id="buy-1",
                entry_fee_quote=0.49864145, stop_loss_override=0.98,
                exit_policy="downtrend_rsi_recovery",
            )
            position.stop_order_id = "stop-1"
            position.stop_client_order_id = "stop-client-1"
            bot.risk_manager._save_positions()

            bot.exchange = Mock()
            bot.exchange.fetch_account_snapshot.return_value = (
                {"total": {"XRP": "0.0985", "USDT": "4487.76"}}, []
            )
            bot.exchange.exchange.load_markets.return_value = None
            bot.exchange.exchange.market.return_value = {"precision": {"amount": 1}}
            bot.exchange.exchange.precisionMode = 2
            bot.exchange.exchange.TICK_SIZE = 4
            bot.exchange.amount_to_precision.side_effect = lambda symbol, amount: (
                "1.0" if Decimal(str(amount)) == Decimal("1") else "500.9"
            )
            bot.exchange.fetch_order.return_value = {
                "id": "stop-1", "side": "sell", "status": "closed", "amount": "500.9",
                "filled": "500.9", "average": "0.98",
                "fees": [{"cost": "0.49", "currency": "USDT"}],
            }
            bot._last_reconciliation_ok = False
            bot._cycle_lock = threading.Lock()

            # Refuse to rebaseline if XRP fell below its recorded starting
            # balance; a confirmed stop fill cannot explain that loss.
            bot.exchange.fetch_account_snapshot.return_value = (
                {"total": {"XRP": "0.5", "USDT": "4487.76"}}, []
            )
            self.assertFalse(bot._reconcile_exchange_state())
            self.assertIn("XRP/USDT", bot.risk_manager.open_positions)
            self.assertEqual(
                bot.risk_manager.store.read_state("baseline_asset_balances"),
                {"XRP": "1"},
            )
            self.assertEqual(bot.risk_manager.trade_history, [])

            # Binance filled the full precision-rounded stop amount; only
            # 0.0985 XRP remained from the 0.1 XRP amount step, in addition
            # to the prior 1 XRP account baseline.
            bot.exchange.fetch_account_snapshot.return_value = (
                {"total": {"XRP": "1.0985", "USDT": "4487.76"}}, []
            )
            self.assertFalse(bot._reconcile_exchange_state())
            self.assertNotIn("XRP/USDT", bot.risk_manager.open_positions)
            self.assertEqual(
                bot.risk_manager.store.read_state("baseline_asset_balances"),
                {"XRP": "1.0985"},
            )
            record = bot.risk_manager.trade_history[-1]
            self.assertEqual(record.reason, "Exchange-native stop-loss filled")
            self.assertAlmostEqual(record.amount, 500.9)
            self.assertAlmostEqual(record.exit_price, 0.98)
            self.assertTrue(record.accounting_complete)
            self.assertTrue(bot.risk_manager._is_halted)
            events = bot.risk_manager.store.list_events(10)
            self.assertTrue(any(event["event_type"] == "protective_stop_filled" for event in events))

            # The dashboard's explicit review-and-resume action performs a
            # fresh clean reconciliation before acknowledging this halt.
            resumed, message = bot.resume_entries(acknowledge_manual_halt=True)
            self.assertTrue(resumed, message)
            self.assertEqual(bot.risk_manager.entry_mode(), "ACTIVE")
            self.assertFalse(bot.risk_manager._is_halted)

    def test_filled_stop_with_asset_missing_from_baseline_is_recorded_for_manual_review(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(
                exchange=SimpleNamespace(paper_trading=True, paper_environment="demo"),
                strategy=SimpleNamespace(trading_pairs=["XRP/USDT"]),
            )
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.trade_log_path = Path(persistence_dir) / "trades.jsonl"
            bot.risk_manager.store.write_state("paper_environment", "demo")
            bot.risk_manager.store.write_state("baseline_asset_balances", {"BTC": "0"})
            position = bot.risk_manager.open_position(
                "XRP/USDT", 0.9943, 500.9985, order_id="buy-1",
                entry_fee_quote=0.49864145, stop_loss_override=0.98,
                exit_policy="downtrend_rsi_recovery",
            )
            position.stop_order_id = "stop-1"
            position.stop_client_order_id = "stop-client-1"
            bot.risk_manager._save_positions()

            bot.exchange = Mock()
            bot.exchange.fetch_account_snapshot.return_value = (
                {"total": {"XRP": "0", "USDT": "4487.76"}}, []
            )
            bot.exchange.exchange.load_markets.return_value = None
            bot.exchange.exchange.market.return_value = {"precision": {"amount": 1}}
            bot.exchange.exchange.precisionMode = 2
            bot.exchange.exchange.TICK_SIZE = 4
            bot.exchange.amount_to_precision.side_effect = lambda symbol, amount: (
                "1.0" if Decimal(str(amount)) == Decimal("1") else "500.9"
            )
            bot.exchange.fetch_order.return_value = {
                "id": "stop-1", "side": "sell", "status": "closed", "amount": "500.9",
                "filled": "500.9", "average": "0.98",
                "fees": [{"cost": "0.49", "currency": "USDT"}],
            }
            bot._last_reconciliation_ok = False
            bot._cycle_lock = threading.Lock()

            # XRP was never in the original asset baseline. The first snapshot
            # reports zero, so the confirmed exit is recorded without claiming
            # complete accounting and a protective-stop review remains pending.
            self.assertFalse(bot._reconcile_exchange_state())
            self.assertNotIn("XRP/USDT", bot.risk_manager.open_positions)
            self.assertEqual(
                bot.risk_manager.store.read_state("baseline_asset_balances")["XRP"],
                "0",
            )
            record = bot.risk_manager.trade_history[-1]
            self.assertAlmostEqual(record.amount, 500.9)
            self.assertFalse(record.accounting_complete)
            self.assertIsNone(record.pnl)
            self.assertTrue(bot.risk_manager._is_halted)
            event = next(
                event for event in bot.risk_manager.store.list_events(10)
                if event["event_type"] == "protective_stop_filled"
            )
            self.assertTrue(event["details"]["baseline_was_missing"])
            self.assertEqual(event["details"]["unattributed_balance"], "0")

            # A later wallet snapshot reveals a residual XRP balance. Periodic
            # reconciliation must not silently accept it; only the explicit
            # dashboard review action may record the new baseline and resume.
            bot.exchange.fetch_account_snapshot.return_value = (
                {"total": {"XRP": "0.6", "USDT": "4487.76"}}, []
            )
            self.assertFalse(bot._reconcile_exchange_state())
            self.assertEqual(bot.risk_manager._halt_reason, "Exchange stop filled for XRP/USDT; manual review required before resuming")
            self.assertEqual(
                bot.risk_manager.store.read_state("baseline_asset_balances")["XRP"],
                "0",
            )

            resumed, message = bot.resume_entries(acknowledge_manual_halt=True)
            self.assertTrue(resumed, message)
            self.assertEqual(
                bot.risk_manager.store.read_state("baseline_asset_balances")["XRP"],
                "0.6",
            )
            self.assertEqual(bot.risk_manager.entry_mode(), "ACTIVE")
            self.assertFalse(bot.risk_manager._is_halted)
            rebase_event = next(
                event for event in bot.risk_manager.store.list_events(20)
                if event["event_type"] == "protective_stop_balance_rebased_after_review"
            )
            self.assertEqual(rebase_event["details"]["account_balance"], "0.6")
            self.assertTrue(rebase_event["details"]["operator_review"])

    def test_terminal_partial_sell_reconciles_and_reprotects_remaining_position(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.risk_manager.store.write_state("baseline_asset_balances", {"BTC": "0.1"})
            bot.risk_manager.store.create_order_intent("sell-client", "BTC/USDT", "sell", "0.2", {})
            bot.risk_manager.store.update_order("sell-client", "SUBMITTED")
            bot.risk_manager.store.update_order("sell-client", "PARTIAL_CLOSED", filled="0.05")
            position = bot.risk_manager.open_position("BTC/USDT", 100.0, 0.2, order_id="buy-1")
            position.stop_order_id = "old-stop"
            position.stop_client_order_id = "old-stop-client"
            bot.risk_manager._save_positions()
            bot.exchange = Mock()
            bot.exchange.fetch_account_snapshot.return_value = ({"total": {"BTC": "0.25"}}, [])
            bot.exchange.place_native_stop_loss.return_value = {"id": "new-stop", "status": "open"}

            recovered = bot._recover_terminal_partial_sell(
                "BTC/USDT", {"id": "sell-1", "status": "canceled", "filled": "0.05"},
                "sell-client", "0.2",
            )
            self.assertTrue(recovered)
            self.assertAlmostEqual(bot.risk_manager.open_positions["BTC/USDT"].amount, 0.15)
            self.assertEqual(bot.risk_manager.open_positions["BTC/USDT"].stop_order_id, "new-stop")
            self.assertTrue(bot.risk_manager._is_halted)
            self.assertEqual(bot.risk_manager.store.unresolved_orders(), [])

    def test_terminal_sell_with_only_untradeable_dust_closes_and_rebaselines(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.trade_log_path = Path(persistence_dir) / "trades.jsonl"
            bot.risk_manager.store.write_state("baseline_asset_balances", {"BTC": "0.1"})
            bot.risk_manager.store.create_order_intent("sell-dust-client", "BTC/USDT", "sell", "0.2", {})
            bot.risk_manager.store.update_order("sell-dust-client", "SUBMITTED")
            bot.risk_manager.store.update_order("sell-dust-client", "PARTIAL_CLOSED", filled="0.199998")
            bot.risk_manager.open_position("BTC/USDT", 100.0, 0.2, order_id="buy-1")
            bot.exchange = Mock()
            bot.exchange.fetch_account_snapshot.return_value = ({"total": {"BTC": "0.100002"}}, [])
            bot.exchange.get_market_limits.return_value = {"min_cost": "5"}
            bot.exchange.get_current_price.return_value = 60000.0

            recovered = bot._recover_terminal_partial_sell(
                "BTC/USDT",
                {"id": "sell-1", "status": "closed", "amount": "0.199998", "filled": "0.199998",
                 "average": "101", "fees": [{"currency": "USDT", "cost": "0.1"}]},
                "sell-dust-client", "0.2",
            )

            self.assertTrue(recovered)
            self.assertNotIn("BTC/USDT", bot.risk_manager.open_positions)
            self.assertEqual(bot.risk_manager.store.read_state("baseline_asset_balances")["BTC"], "0.100002")
            self.assertEqual(bot.risk_manager.store.unresolved_orders(), [])
            self.assertAlmostEqual(bot.risk_manager.trade_history[-1].amount, 0.199998)
            self.assertTrue(bot.risk_manager.trade_history[-1].accounting_complete)
            bot.exchange.place_native_stop_loss.assert_not_called()

    def test_saved_demo_sell_can_be_reconciled_without_sending_an_order(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(
                exchange=SimpleNamespace(paper_trading=True, paper_environment="demo"),
                strategy=SimpleNamespace(trading_pairs=["BTC/USDT"]),
            )
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.trade_log_path = Path(persistence_dir) / "trades.jsonl"
            bot.risk_manager.store.write_state("baseline_asset_balances", {"BTC": "0.1"})
            bot.risk_manager.open_position("BTC/USDT", 100.0, 0.2, order_id="buy-1")
            bot.risk_manager.store.create_order_intent("sell-client", "BTC/USDT", "sell", "0.2", {})
            bot.risk_manager.store.update_order("sell-client", "SUBMITTED")
            bot.risk_manager.store.update_order(
                "sell-client", "PARTIAL_CLOSED", exchange_order_id="sell-1", filled="0.199998",
                average="101", response={"status": "closed"},
            )
            bot.risk_manager.halt("Unresolved terminal sell order")
            bot.exchange = Mock()
            bot.exchange.is_fallback_simulated = False
            bot.exchange.fetch_order.return_value = {
                "id": "sell-1", "symbol": "BTC/USDT", "side": "sell", "status": "closed",
                "amount": "0.199998", "filled": "0.199998", "average": "101",
                "fees": [{"currency": "USDT", "cost": "0.1"}],
            }
            bot.exchange.fetch_account_snapshot.return_value = ({"total": {"BTC": "0.100002"}}, [])
            bot.exchange.get_market_limits.return_value = {"min_cost": "5"}
            bot.exchange.get_current_price.return_value = 60000.0
            bot._cycle_lock = threading.Lock()
            bot._reconcile_exchange_state = Mock(return_value=True)

            result = bot.reconcile_terminal_demo_sell("BTC/USDT")

            self.assertTrue(result["success"], result["message"])
            self.assertTrue(result["trading_halted"])
            self.assertTrue(bot.risk_manager._is_halted)
            self.assertNotIn("BTC/USDT", bot.risk_manager.open_positions)
            self.assertEqual(bot.risk_manager.store.unresolved_orders(), [])
            bot.exchange.fetch_order.assert_called_once_with("sell-1", "BTC/USDT")
            bot.exchange.place_market_sell.assert_not_called()
            bot._reconcile_exchange_state.assert_called_once_with()

    def test_stop_fill_reconciliation_uses_position_asset_with_multiple_pairs(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(
                exchange=SimpleNamespace(paper_trading=True, paper_environment="demo"),
                strategy=SimpleNamespace(trading_pairs=["BTC/USDT", "XRP/USDT"]),
            )
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.trade_log_path = Path(persistence_dir) / "trades.jsonl"
            bot.risk_manager.store.write_state("paper_environment", "demo")
            bot.risk_manager.store.write_state("baseline_asset_balances", {"BTC": "0", "XRP": "10"})
            position = bot.risk_manager.open_position(
                "BTC/USDT", 60000.0, 1.5, order_id="buy-btc",
                entry_fee_quote=0.5, stop_loss_override=59000.0,
                exit_policy="ema72_signal",
            )
            position.stop_order_id = "stop-btc"
            position.stop_client_order_id = "stop-btc-client"
            bot.risk_manager._save_positions()

            bot.exchange = Mock()
            # XRP reports a balance below its recorded baseline. The BTC stop-fill
            # reconciliation must not mis-attribute BTC's balance check to XRP
            # (the last configured pair) and wrongly refuse the confirmed exit.
            bot.exchange.fetch_account_snapshot.return_value = (
                {"total": {"BTC": "0.0", "XRP": "9", "USDT": "4487.76"}}, []
            )
            bot.exchange.exchange.load_markets.return_value = None
            bot.exchange.exchange.precisionMode = 4  # TICK_SIZE
            bot.exchange.exchange.TICK_SIZE = 4
            bot.exchange.exchange.market.side_effect = lambda symbol: (
                {"precision": {"amount": 0.00001}} if symbol == "BTC/USDT" else {"precision": {"amount": 1}}
            )
            bot.exchange.amount_to_precision.side_effect = lambda symbol, amount: (
                "1.5" if symbol == "BTC/USDT" else str(amount)
            )
            bot.exchange.fetch_order.return_value = {
                "id": "stop-btc", "side": "sell", "status": "closed", "amount": "1.5",
                "filled": "1.5", "average": "59000.0",
                "fees": [{"cost": "0.5", "currency": "USDT"}],
            }
            bot._last_reconciliation_ok = False
            bot._cycle_lock = threading.Lock()

            self.assertFalse(bot._reconcile_exchange_state())
            # The BTC position must be closed by its own stop fill; leaking the
            # last configured pair's base would leave the position open.
            self.assertNotIn("BTC/USDT", bot.risk_manager.open_positions)
            self.assertEqual(len(bot.risk_manager.trade_history), 1)
            self.assertEqual(
                bot.risk_manager.store.read_state("baseline_asset_balances")["BTC"],
                "0.0",
            )

    def test_resume_entries_requires_clean_reconciliation(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.risk_manager.set_exit_only()
            bot._cycle_lock = threading.Lock()
            bot._last_account_error = "Testnet credentials rejected"
            bot._reconcile_exchange_state = Mock(return_value=False)

            success, message = bot.resume_entries()
            self.assertFalse(success)
            self.assertIn("credentials rejected", message)
            self.assertEqual(bot.risk_manager.entry_mode(), "EXIT_ONLY")

            bot._reconcile_exchange_state.return_value = True
            success, message = bot.resume_entries()
            self.assertTrue(success)
            self.assertEqual(bot.risk_manager.entry_mode(), "ACTIVE")

            bot.risk_manager.set_exit_only()
            bot.risk_manager.halt("Emergency stop could not verify/cancel exchange orders; operator action required")
            success, _ = bot.resume_entries()
            self.assertFalse(success)
            success, message = bot.resume_entries(acknowledge_manual_halt=True)
            self.assertTrue(success)
            self.assertEqual(bot.risk_manager.entry_mode(), "ACTIVE")
            self.assertFalse(bot.risk_manager._is_halted)

            # A consecutive-loss halt can only be cleared by explicit review
            # after a fresh reconciliation, and the review resets only its streak.
            bot.risk_manager = RiskManager(
                RiskConfig(max_daily_loss_pct=10.0, max_consecutive_losses=1),
                persistence_dir=persistence_dir,
            )
            bot.risk_manager.open_position("BTC/USDT", 100.0, 1.0)
            bot.risk_manager.close_position("BTC/USDT", 99.0, "Test loss")
            self.assertTrue(bot.risk_manager._is_halted)
            prior_daily_loss = bot.risk_manager._daily_loss
            success, _ = bot.resume_entries()
            self.assertFalse(success)
            self.assertTrue(bot.risk_manager._is_halted)
            success, _ = bot.resume_entries(acknowledge_manual_halt=True)
            self.assertTrue(success)
            self.assertFalse(bot.risk_manager._is_halted)
            self.assertEqual(bot.risk_manager._consecutive_losses, 0)
            self.assertEqual(bot.risk_manager._daily_loss, prior_daily_loss)
            audit = bot.risk_manager.store.list_events(event_type="consecutive_loss_halt_review_acknowledged")
            self.assertEqual(len(audit), 1)

            # A review cannot bypass unresolved order intent or an open position.
            bot.risk_manager.halt("Consecutive loss limit reached (1); entries paused")
            bot.risk_manager.store.create_order_intent("review_pending_1", "BTC/USDT", "BUY", 0.1, {"type": "market"})
            success, message = bot.resume_entries(acknowledge_manual_halt=True)
            self.assertFalse(success)
            self.assertIn("unresolved orders", message)
            self.assertTrue(bot.risk_manager._is_halted)
            bot.risk_manager.store.update_order("review_pending_1", "REJECTED")
            bot.risk_manager.store.mark_order_applied("review_pending_1")
            bot.risk_manager.open_position("ETH/USDT", 100.0, 1.0)
            success, message = bot.resume_entries(acknowledge_manual_halt=True)
            self.assertFalse(success)
            self.assertIn("positions", message)
            self.assertTrue(bot.risk_manager._is_halted)

    def test_resume_entries_rebases_rolling_drawdown_peak_after_reconciliation(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.risk_manager = RiskManager(
                RiskConfig(max_daily_loss_pct=10.0, max_rolling_drawdown_pct=5.0),
                persistence_dir=persistence_dir,
            )
            bot.risk_manager.set_exit_only()
            bot._cycle_lock = threading.Lock()
            bot._last_account_error = ""
            bot._last_account_balance = {"total": {"USDT": "940.0"}}
            bot._reconcile_exchange_state = Mock(return_value=True)
            bot._bot_managed_equity = Mock(return_value=940.0)

            self.assertTrue(bot.risk_manager.monitor_equity(1000.0))
            self.assertFalse(bot.risk_manager.monitor_equity(940.0))
            self.assertTrue(bot.risk_manager._is_halted)
            self.assertIn("rolling", bot.risk_manager._halt_reason.lower())

            success, message = bot.resume_entries(acknowledge_manual_halt=True)
            self.assertTrue(success, message)
            self.assertFalse(bot.risk_manager._is_halted)
            self.assertEqual(bot.risk_manager._rolling_peak_equity, 940.0)
            self.assertEqual(bot.risk_manager.entry_mode(), "ACTIVE")

    def test_manual_review_cannot_clear_other_risk_halts(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            # A daily equity latch resets on the UTC day rollover, not via review.
            rm.halt("Daily equity drawdown limit reached: 5.1%")
            self.assertFalse(rm.acknowledge_manual_halt_review())
            self.assertTrue(rm._is_halted)

            # A rolling drawdown latch needs a fresh peak; it cannot clear
            # without the operator supplying the reconciled account equity.
            rm._is_halted, rm._halt_reason = False, ""
            rm._rolling_peak_equity = 1000.0
            rm.halt("Rolling peak-to-trough drawdown limit reached: 5.2%")
            self.assertFalse(rm.acknowledge_manual_halt_review())
            self.assertTrue(rm._is_halted)
            self.assertTrue(rm.acknowledge_manual_halt_review(rolling_drawdown_rebase_equity=940.0))
            self.assertFalse(rm._is_halted)
            self.assertEqual(rm._rolling_peak_equity, 940.0)
            events = rm.store.list_events(event_type="rolling_drawdown_halt_review_acknowledged")
            self.assertEqual(len(events), 1)

    def test_buy_signal_prefers_managed_equity_risk_basis(self):
        with TemporaryDirectory() as persistence_dir:
            cfg = BotConfig(dashboard_token="test_token")
            cfg.strategy.name = "macro_donchian_ema72"
            cfg.strategy.timeframe = "1h"
            cfg.strategy.lookback_candles = 2501
            bot = object.__new__(TradingBot)
            bot.config = cfg
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.get_usdt_balance.return_value = 10000.0
            bot._bot_managed_equity = Mock(return_value=20000.0)
            with patch.object(bot.risk_manager, "can_open_position", return_value=(False, "blocked")) as can_open:
                signal = TradeSignal(Signal.BUY, "BTC/USDT", 0.8, "Test", indicators={
                    "close": 100.0, "strategy": "macro_donchian_ema72", "atr": 2.0, "swing_low_5": 96.0,
                })
                bot._handle_buy_signal(signal)
            can_open.assert_called_once()
            # Managed equity (cash + positions), not free cash, is the risk basis.
            self.assertAlmostEqual(can_open.call_args[0][1], 20000.0)

    def test_signal_fill_divergence_halts_and_protects_position(self):
        with TemporaryDirectory() as persistence_dir:
            cfg = BotConfig(dashboard_token="test_token")
            cfg.strategy.name = "macro_donchian_ema72"
            cfg.strategy.timeframe = "1h"
            cfg.strategy.lookback_candles = 2501
            bot = object.__new__(TradingBot)
            bot.config = cfg
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.get_usdt_balance.return_value = 10000.0
            bot.exchange.get_current_price.return_value = 100.0
            bot.exchange.get_public_market_data_price.return_value = 99.9
            bot.exchange.amount_to_precision.return_value = "10"
            bot.exchange.get_market_limits.return_value = {"min_amount": 0.1, "min_cost": 10.0, "active": True}
            bot.exchange.get_order_book.return_value = {"bids": [[99.9, 100.0]], "asks": [[100.0, 100.0]]}
            bot._has_correlated_open_position = lambda _symbol: (False, "")
            buy_order = {
                "id": "buy-1", "status": "closed", "filled": "10", "average": "105.0",
                "fees": [{"currency": "USDT", "cost": "1"}],
            }
            bot._submit_market_order = Mock(return_value=(buy_order, "cb-1"))
            bot._place_and_persist_native_stop = Mock(return_value=True)
            bot._fee_quote_cost = Mock(return_value=1.0)
            bot._simulated_wallet_active = Mock(return_value=False)
            bot.send_notification = Mock()
            bot._log_trade = Mock()

            signal = TradeSignal(Signal.BUY, "BTC/USDT", 0.85, "Test", indicators={
                "strategy": "macro_donchian_ema72", "close": 100.0, "swing_low_5": 96.0, "atr": 2.0,
            })
            bot._handle_buy_signal(signal)

            self.assertTrue(bot.risk_manager._is_halted)
            self.assertIn("signal close", bot.risk_manager._halt_reason)
            self.assertIn("BTC/USDT", bot.risk_manager.open_positions)
            events = bot.risk_manager.store.list_events(10)
            self.assertTrue(any(event["event_type"] == "signal_fill_divergence" for event in events))

    def test_f02_confirmed_buy_places_stop_even_if_position_persistence_fails(self):
        """F-02: Confirm native stop loss is placed on exchange even if local DB write fails."""
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = BotConfig(dashboard_token="test_token")
            bot.config.strategy.name = "combined"  # exercise the legacy risk-managed path
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.get_usdt_balance.return_value = 10000.0
            bot.exchange.get_current_price.return_value = 50000.0
            bot.exchange.amount_to_precision.return_value = "0.01"
            bot.exchange.get_market_limits.return_value = {"min_amount": 0.001, "min_cost": 10.0, "active": True}
            bot.exchange.get_order_book.return_value = {"bids": [[49990.0, 1.0]], "asks": [[50000.0, 1.0]]}
            bot._has_correlated_open_position = lambda s: (False, "")

            buy_order = {
                "id": "buy_f02_123",
                "status": "closed",
                "filled": "0.01",
                "average": "50000.0",
                "fees": [{"currency": "USDT", "cost": "0.5"}],
            }
            bot._submit_market_order = Mock(return_value=(buy_order, "cb_buy_client_1"))
            with patch.object(bot.risk_manager, "_save_positions", side_effect=RuntimeError("disk full")):
                signal = TradeSignal(Signal.BUY, "BTC/USDT", 0.8, "Test buy", indicators={"atr": 500.0})
                bot._handle_buy_signal(signal)

            self.assertTrue(bot.risk_manager._is_halted)
            self.assertTrue(bot.exchange.place_native_stop_loss.called)
            call_args = bot.exchange.place_native_stop_loss.call_args[0]
            self.assertEqual(call_args[0], "BTC/USDT")
            self.assertEqual(call_args[1], 0.01)

    def test_initial_stop_recovers_if_exchange_accepted_before_timeout(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.risk_manager.open_position(
                "BTC/USDT", entry_price=100.0, amount=0.1, order_id="buy-1"
            )
            bot.exchange.place_native_stop_loss.side_effect = TimeoutError("response timed out")
            bot.exchange.fetch_order_by_client_id.return_value = {
                "id": "stop-accepted", "status": "open",
            }

            result = bot._place_and_persist_native_stop("BTC/USDT", "buy-1")

            self.assertTrue(result)
            bot.exchange.fetch_order_by_client_id.assert_called_once_with(
                TradingBot._client_order_id("BTC/USDT", "stop", "buy-1"), "BTC/USDT"
            )
            position = bot.risk_manager.open_positions["BTC/USDT"]
            self.assertEqual(position.stop_order_id, "stop-accepted")
            self.assertFalse(bot.risk_manager._is_halted)

    def test_ema72_testnet_buy_uses_swing_stop_and_strategy_exit_target(self):
        with TemporaryDirectory() as persistence_dir:
            cfg = BotConfig(dashboard_token="test_token")
            cfg.strategy.name = "macro_donchian_ema72"
            cfg.strategy.timeframe = "1h"
            cfg.strategy.lookback_candles = 2501
            bot = object.__new__(TradingBot)
            bot.config = cfg
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.get_usdt_balance.return_value = 10000.0
            bot.exchange.get_current_price.return_value = 100.0
            bot.exchange.get_public_market_data_price.return_value = 99.0
            bot.exchange.amount_to_precision.return_value = "10"
            bot.exchange.get_market_limits.return_value = {
                "min_amount": 0.1, "min_cost": 10.0, "active": True,
            }
            bot.exchange.get_order_book.return_value = {
                "bids": [[99.9, 100.0]], "asks": [[100.0, 100.0]],
            }
            bot._has_correlated_open_position = lambda _symbol: (False, "")
            buy_order = {
                "id": "ema72-buy-1", "status": "closed", "filled": "10",
                "average": "100", "fees": [{"currency": "USDT", "cost": "1"}],
            }
            bot._submit_market_order = Mock(return_value=(buy_order, "ema72-client-1"))
            bot._fee_quote_cost = Mock(return_value=1.0)
            bot._place_and_persist_native_stop = Mock(return_value=True)
            bot.send_notification = Mock()
            bot._log_trade = Mock()
            bot.risk_manager.store.create_order_intent(
                "ema72-client-1", "BTC/USDT", "buy", "10", {"test": True}
            )
            bot.risk_manager.store.update_order("ema72-client-1", "SUBMITTED")
            bot.risk_manager.store.update_order(
                "ema72-client-1", "FILLED", exchange_order_id="ema72-buy-1",
                filled="10", average="100",
            )

            signal = TradeSignal(
                Signal.BUY, "BTC/USDT", 0.85, "Test EMA72 entry",
                {"atr": 2.0, "swing_low_5": 97.0},
            )
            bot._handle_buy_signal(signal, decision_key="divergent-candle")
            bot._submit_market_order.assert_not_called()
            bot.exchange.get_public_market_data_price.return_value = 100.2
            bot._handle_buy_signal(signal, decision_key="test-candle")

            position = bot.risk_manager.open_positions["BTC/USDT"]
            self.assertEqual(position.stop_loss_price, 97.0)
            self.assertEqual(position.take_profit_price, 300.0)
            self.assertEqual(position.exit_policy, "ema72_signal")
            self.assertTrue(bot._place_and_persist_native_stop.called)
            self.assertEqual(bot._place_and_persist_native_stop.call_args.kwargs["fallback_sl"], 97.0)
            self.assertEqual(bot.risk_manager.check_positions(lambda _symbol: 105.0), [])
            self.assertEqual(position.stop_loss_price, 97.0)
            self.assertFalse(position.break_even_activated)

    def test_downtrend_recovery_testnet_buy_applies_extra_risk_cap(self):
        with TemporaryDirectory() as persistence_dir:
            cfg = BotConfig(dashboard_token="test_token")
            cfg.strategy.name = "dynamic_regime"
            cfg.strategy.timeframe = "1h"
            cfg.strategy.lookback_candles = 2500
            bot = object.__new__(TradingBot)
            bot.config = cfg
            bot.risk_manager = RiskManager(RiskConfig(max_risk_per_trade=1.0), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.get_usdt_balance.return_value = 10000.0
            bot.exchange.get_current_price.return_value = 100.0
            bot.exchange.get_public_market_data_price.return_value = 100.2
            bot.exchange.amount_to_precision.return_value = "4.75"
            bot.exchange.get_market_limits.return_value = {"min_amount": 0.1, "min_cost": 10.0, "active": True}
            bot.exchange.get_order_book.return_value = {"bids": [[99.9, 100.0]], "asks": [[100.0, 100.0]]}
            bot._has_correlated_open_position = lambda _symbol: (False, "")
            buy_order = {
                "id": "recovery-buy-1", "status": "closed", "filled": "4.75",
                "average": "100", "fees": [{"currency": "USDT", "cost": "1"}],
            }
            bot._submit_market_order = Mock(return_value=(buy_order, "recovery-client-1"))
            bot._resolve_fee_quote_cost = Mock(return_value=1.0)
            bot._place_and_persist_native_stop = Mock(return_value=True)
            bot.send_notification = Mock()
            bot._log_trade = Mock()
            bot.risk_manager.store.create_order_intent(
                "recovery-client-1", "BTC/USDT", "buy", "4.75", {"test": True}
            )
            bot.risk_manager.store.update_order("recovery-client-1", "SUBMITTED")
            bot.risk_manager.store.update_order(
                "recovery-client-1", "FILLED", exchange_order_id="recovery-buy-1",
                filled="4.75", average="100",
            )

            signal = TradeSignal(
                Signal.BUY, "BTC/USDT", 0.65, "RSI recovery test",
                {"strategy": "downtrend_rsi_recovery_testnet", "atr": 1.0, "swing_low_5": 95.0},
            )
            bot._handle_buy_signal(signal, decision_key="recovery-candle")
            position = bot.risk_manager.open_positions["BTC/USDT"]
            self.assertEqual(position.exit_policy, "downtrend_rsi_recovery")
            self.assertEqual(position.stop_loss_price, 96.0)
            self.assertEqual(position.take_profit_price, 102.0)
            risk, within_cap = bot._post_fill_stop_risk(
                position.amount, position.entry_price, position.stop_loss_price,
                position.entry_fee_quote, 20.0,
            )
            self.assertEqual(risk, Decimal("20.00"))
            self.assertTrue(within_cap)
            self.assertFalse(bot.risk_manager._is_halted)
            self.assertEqual(bot._place_and_persist_native_stop.call_args.kwargs["fallback_sl"], 96.0)

    def test_missing_order_fee_uses_verified_fill_fee_for_stop_quantity(self):
        """A missing order fee must be recovered from fills before stop sizing."""
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = BotConfig(dashboard_token="test_token")
            bot.config.strategy.name = "combined"  # exercise the legacy risk-managed path
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.get_usdt_balance.return_value = 10000.0
            bot.exchange.get_current_price.return_value = 50000.0
            bot.exchange.amount_to_precision.return_value = "0.01"
            bot.exchange.get_market_limits.return_value = {"min_amount": 0.001, "min_cost": 10.0, "active": True}
            bot.exchange.get_order_book.return_value = {"bids": [[49990.0, 1.0]], "asks": [[50000.0, 1.0]]}
            bot._has_correlated_open_position = lambda s: (False, "")

            buy_order = {
                "id": "buy_f03_456",
                "status": "closed",
                "filled": "0.01",
                "average": "50000.0",
            }
            bot.exchange.fetch_order_trades.return_value = [{
                "amount": "0.01",
                "fee": {"currency": "BTC", "cost": "0.00001"},
            }]
            bot.exchange.place_native_stop_loss.return_value = {
                "id": "stop_f03_456", "status": "open",
            }
            bot._submit_market_order = Mock(return_value=(buy_order, "cb_buy_client_2"))
            bot.exchange.exchange.fetch_order = Mock(return_value=buy_order)
            bot.send_notification = Mock()
            bot._log_trade = Mock()
            bot.risk_manager.store.create_order_intent(
                "cb_buy_client_2", "BTC/USDT", "buy", "0.01", {"test": True}
            )
            bot.risk_manager.store.update_order("cb_buy_client_2", "SUBMITTED")
            bot.risk_manager.store.update_order(
                "cb_buy_client_2", "FILLED", exchange_order_id="buy_f03_456",
                filled="0.01", average="50000.0",
            )

            signal = TradeSignal(Signal.BUY, "BTC/USDT", 0.8, "Test fee-less buy", indicators={"atr": 500.0})
            bot._handle_buy_signal(signal)

            self.assertIn("BTC/USDT", bot.risk_manager.open_positions)
            self.assertTrue(bot.exchange.place_native_stop_loss.called)
            self.assertAlmostEqual(
                bot.exchange.place_native_stop_loss.call_args.args[1], 0.00999
            )
            self.assertFalse(bot.risk_manager._is_halted)

    def test_net_fill_quantity_uses_account_delta_when_fill_fees_are_missing(self):
        bot = object.__new__(TradingBot)
        bot.exchange = Mock()
        bot.exchange.fetch_order_trades.return_value = []
        bot.exchange.fetch_account_snapshot.return_value = (
            {"total": {"BNB": "0.921078"}}, []
        )
        bot.risk_manager = SimpleNamespace(
            open_positions={}, store=Mock()
        )
        bot.risk_manager.store.read_state.return_value = {"BNB": "0"}

        net, fee_verified = bot._resolve_net_base_filled(
            {"id": "buy_bnb", "filled": "0.922"}, "BNB/USDT"
        )

        self.assertEqual(net, Decimal("0.921078"))
        self.assertFalse(fee_verified)

    def test_net_fill_quantity_rejects_ambiguous_account_delta(self):
        bot = object.__new__(TradingBot)
        bot.exchange = Mock()
        bot.exchange.fetch_order_trades.return_value = []
        bot.exchange.fetch_account_snapshot.return_value = (
            {"total": {"BNB": "1.0"}}, []
        )
        bot.risk_manager = SimpleNamespace(open_positions={}, store=Mock())
        bot.risk_manager.store.read_state.return_value = {"BNB": "0"}

        net, fee_verified = bot._resolve_net_base_filled(
            {"id": "buy_bnb", "filled": "0.922"}, "BNB/USDT"
        )

        self.assertIsNone(net)
        self.assertFalse(fee_verified)

    def test_external_demo_close_reconciles_complete_accounting_from_verified_fills(self):
        with TemporaryDirectory() as persistence_dir:
            cfg = BotConfig(dashboard_token="test_token")
            cfg.exchange.paper_trading = True
            cfg.exchange.paper_environment = "demo"
            cfg.exchange.demo_api_key = "demo-key"
            cfg.exchange.demo_api_secret = "demo-secret"
            cfg.strategy.trading_pairs = ["BNB/USDT"]
            cfg.data_dir = persistence_dir
            bot = object.__new__(TradingBot)
            bot.config = cfg
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.is_fallback_simulated = False
            bot.exchange.fetch_account_snapshot.return_value = (
                {"total": {"BNB": "0.00030850", "USDT": "4966.61"}}, []
            )
            buy = {
                "id": "9493199775", "symbol": "BNB/USDT", "side": "buy",
                "status": "closed", "amount": "0.922", "filled": "0.922",
                "average": "539.13", "fees": [],
                "timestamp": 1795220012000,
            }
            sell = {
                "id": "9504747443", "symbol": "BNB/USDT", "side": "sell",
                "status": "closed", "amount": "0.921", "filled": "0.921",
                "average": "527.71", "fees": [{"currency": "USDT", "cost": "0.48602091"}],
                "timestamp": 1795226741000,
            }
            bot.exchange.fetch_order.side_effect = [buy, sell]
            bot.exchange.fetch_order_trades.return_value = [
                {"amount": "0.922", "fees": [{"currency": "BNB", "cost": "0.0006915"}]}
            ]
            bot.exchange.fetch_order_by_client_id.return_value = None
            bot.risk_manager.store.write_state("baseline_asset_balances", {"BTC": "0.00017982"})
            bot.risk_manager.open_position(
                "BNB/USDT", 539.13, 0.9213085, order_id="9493199775", entry_fee_quote=0.3727
            )
            bot.risk_manager.halt("Native stop placement/persistence failed for BNB/USDT; reconcile before continuing")
            bot.risk_manager.store.create_order_intent(
                "buy-intent", "BNB/USDT", "buy", "0.922", {"test": True}
            )
            bot.risk_manager.store.update_order("buy-intent", "SUBMITTED")
            bot.risk_manager.store.update_order(
                "buy-intent", "FILLED", exchange_order_id="9493199775",
                filled="0.922", average="539.13",
            )
            bot.risk_manager.store.create_order_intent(
                "sell-intent", "BNB/USDT", "sell", "0.922", {"test": True}
            )
            bot.risk_manager.store.update_order("sell-intent", "SUBMITTED")
            bot.risk_manager.store.update_order("sell-intent", "UNKNOWN")
            bot._cycle_lock = threading.Lock()
            bot.trade_log_path = Path(persistence_dir) / "trades.jsonl"
            bot._log_trade = Mock()
            bot._below_minimum_notional = Mock(return_value=True)
            bot._reconcile_exchange_state = Mock(return_value=True)

            result = bot.reconcile_external_close("BNB/USDT", "9493199775", "9504747443")

            self.assertTrue(result["success"], result["message"])
            self.assertNotIn("BNB/USDT", bot.risk_manager.open_positions)
            self.assertEqual(bot.risk_manager.store.unresolved_orders(), [])
            self.assertEqual(bot.risk_manager.store.get_order("sell-intent")["state"], "REJECTED")
            self.assertEqual(bot.risk_manager.store.read_state("baseline_asset_balances")["BNB"], "0.00030850")
            self.assertTrue(bot.risk_manager.trade_history[-1].accounting_complete)
            self.assertAlmostEqual(
                bot.risk_manager.trade_history[-1].fee,
                (0.0006915 * 539.13) * 0.921 / 0.9213085 + 0.48602091,
                places=8,
            )
            self.assertAlmostEqual(
                bot.risk_manager.trade_history[-1].pnl,
                (527.71 - 539.13) * 0.921 - bot.risk_manager.trade_history[-1].fee,
                places=8,
            )
            self.assertTrue(bot.risk_manager._is_halted)
            self.assertEqual(bot.exchange.place_market_sell.call_count, 0)

    def test_external_demo_close_rejects_balance_mismatch_without_mutation(self):
        with TemporaryDirectory() as persistence_dir:
            cfg = BotConfig(dashboard_token="test_token")
            cfg.exchange.paper_trading = True
            cfg.exchange.paper_environment = "demo"
            bot = object.__new__(TradingBot)
            bot.config = cfg
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.is_fallback_simulated = False
            bot.exchange.fetch_account_snapshot.return_value = ({"total": {"BNB": "1"}}, [])
            bot.risk_manager.store.write_state("baseline_asset_balances", {"BNB": "0"})
            bot.risk_manager.open_position("BNB/USDT", 100.0, 1.0, order_id="100")
            bot._cycle_lock = threading.Lock()
            result = bot.reconcile_external_close("BNB/USDT", "100", "200")
            self.assertFalse(result["success"])
            self.assertIn("BNB/USDT", bot.risk_manager.open_positions)
            bot.exchange.fetch_order.assert_not_called()

    def test_closed_demo_trade_review_reconciles_verified_bnb_fills_and_fees(self):
        with TemporaryDirectory() as persistence_dir:
            cfg = BotConfig(dashboard_token="test_token")
            cfg.exchange.paper_trading = True
            cfg.exchange.paper_environment = "demo"
            cfg.strategy.trading_pairs = ["BNB/USDT"]
            bot = object.__new__(TradingBot)
            bot.config = cfg
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.is_fallback_simulated = False
            bot.exchange.fetch_account_snapshot.return_value = ({"total": {"BNB": "0.00030850"}}, [])
            bot.exchange.fetch_order.side_effect = [
                {"id": "9493199775", "symbol": "BNB/USDT", "side": "buy", "status": "closed",
                 "amount": "0.922", "filled": "0.922", "average": "539.13", "timestamp": 1795220012000},
                {"id": "9504747443", "symbol": "BNB/USDT", "side": "sell", "status": "closed",
                 "amount": "0.921", "filled": "0.921", "average": "527.71", "timestamp": 1795226741000},
            ]
            bot.exchange.fetch_order_trades.side_effect = [
                [{"order": "9493199775", "symbol": "BNB/USDT", "amount": "0.922",
                  "fees": [{"currency": "BNB", "cost": "0.0006915"}]}],
                [{"order": "9504747443", "symbol": "BNB/USDT", "amount": "0.921",
                  "fees": [{"currency": "USDT", "cost": "0.48602091"}]}],
            ]
            bot.risk_manager.store.append_trade({
                "action": "SELL", "symbol": "BNB/USDT", "price": 527.71,
                "amount": 0.921, "entry_price": 539.13,
                "entry_time": "2026-11-20T22:00:13.665576+00:00",
                "timestamp": "2026-11-21T08:42:25.860151+00:00", "pnl": None,
                "pnl_pct": None, "fee": None, "accounting_complete": False,
                "order_id": "9504747443", "reason": "Closed manually on Binance Demo; reconciliation reviewed",
                "strategy": "downtrend_rsi_recovery_testnet", "route": "downtrend_rsi_recovery_testnet",
            })
            bot.risk_manager.store.append_event("external_close_reconciled", {
                "symbol": "BNB/USDT", "buy_order_id": "9493199775", "sell_order_id": "9504747443",
                "remaining_dust": "0.00030850", "accounting_complete": False,
            })
            bot.risk_manager.store.write_state("baseline_asset_balances", {"BNB": "0.00030850"})
            bot.risk_manager._load_trade_history()
            bot._cycle_lock = threading.Lock()

            result = bot.reconcile_closed_external_trade()

            self.assertTrue(result["success"], result["message"])
            saved = bot.risk_manager.store.list_trades()[0]
            self.assertTrue(saved["accounting_complete"])
            self.assertAlmostEqual(saved["fee"], 0.48602091 + 0.0006915 * 539.13 * 0.921 / 0.9213085, places=8)
            self.assertAlmostEqual(saved["pnl"], (527.71 - 539.13) * 0.921 - saved["fee"], places=8)
            self.assertTrue(bot.risk_manager.trade_history[0].accounting_complete)
            self.assertEqual(bot.exchange.place_market_sell.call_count, 0)

    def test_closed_demo_trade_review_keeps_record_incomplete_when_fills_are_missing(self):
        with TemporaryDirectory() as persistence_dir:
            cfg = BotConfig(dashboard_token="test_token")
            cfg.exchange.paper_trading = True
            cfg.exchange.paper_environment = "demo"
            cfg.strategy.trading_pairs = ["BNB/USDT"]
            bot = object.__new__(TradingBot)
            bot.config = cfg
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.is_fallback_simulated = False
            bot.exchange.fetch_account_snapshot.return_value = ({"total": {"BNB": "0.00030850"}}, [])
            bot.exchange.fetch_order.side_effect = [
                {"id": "9493199775", "symbol": "BNB/USDT", "side": "buy", "status": "closed",
                 "filled": "0.922", "average": "539.13", "timestamp": 1795220012000},
                {"id": "9504747443", "symbol": "BNB/USDT", "side": "sell", "status": "closed",
                 "filled": "0.921", "average": "527.71", "timestamp": 1795226741000},
            ]
            bot.exchange.fetch_order_trades.return_value = []
            original = {"action": "SELL", "symbol": "BNB/USDT", "price": 527.71,
                        "amount": 0.921, "entry_price": 539.13, "pnl": None, "fee": None,
                        "accounting_complete": False, "order_id": "9504747443"}
            bot.risk_manager.store.append_trade(original)
            bot.risk_manager.store.append_event("external_close_reconciled", {
                "symbol": "BNB/USDT", "buy_order_id": "9493199775", "sell_order_id": "9504747443",
                "remaining_dust": "0.00030850", "accounting_complete": False,
            })
            bot.risk_manager.store.write_state("baseline_asset_balances", {"BNB": "0.00030850"})
            bot._cycle_lock = threading.Lock()

            result = bot.reconcile_closed_external_trade()

            self.assertFalse(result["success"])
            self.assertIsNone(bot.risk_manager.store.list_trades()[0]["pnl"])
            self.assertFalse(bot.risk_manager.store.list_trades()[0]["accounting_complete"])

    def test_f04_exit_failure_restores_protective_stop(self):
        """F-04: If market sell fails after stop cancellation, protective stop must be restored."""
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = BotConfig(dashboard_token="test_token")
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.get_current_price.return_value = 52000.0

            pos = bot.risk_manager.open_position("BTC/USDT", 50000.0, 0.01, order_id="buy_1")
            pos.stop_order_id = "initial_stop_id"
            bot.risk_manager._save_positions()

            bot.exchange.cancel_order.return_value = True
            bot._submit_market_order = Mock(side_effect=RuntimeError("Network failure on sell"))

            signal = TradeSignal(Signal.SELL, "BTC/USDT", 0.9, "Take profit signal", indicators={})
            bot._handle_sell_signal(signal)

            self.assertTrue(bot.risk_manager._is_halted)
            self.assertTrue(bot.exchange.place_native_stop_loss.called)
            call_args = bot.exchange.place_native_stop_loss.call_args[0]
            self.assertEqual(call_args[0], "BTC/USDT")
            self.assertEqual(call_args[1], 0.01)

    def test_dashboard_manual_close_records_actual_net_result(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(
                exchange=SimpleNamespace(paper_trading=True),
                strategy=SimpleNamespace(trading_pairs=["BTC/USDT"]),
            )
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.is_fallback_simulated = False
            bot.exchange.get_current_price.return_value = 110.0
            bot._cycle_lock = threading.Lock()
            bot._reconcile_exchange_state = Mock(return_value=True)
            position = bot.risk_manager.open_position(
                "BTC/USDT", 100.0, 1.0, entry_fee_quote=1.0, order_id="buy-1"
            )

            def close_from_signal(signal, decision_key):
                self.assertEqual(signal.signal, Signal.SELL)
                self.assertTrue(signal.indicators["manual_operator"])
                self.assertIn("dashboard-manual-close:", decision_key)
                bot.risk_manager.close_position(
                    signal.symbol, 110.0, signal.reason, fee_cost=0.5
                )

            bot._handle_sell_signal = Mock(side_effect=close_from_signal)
            result = bot.close_position_manually("BTC/USDT")

            self.assertTrue(result["success"])
            self.assertTrue(result["accounting_complete"])
            self.assertAlmostEqual(result["pnl"], 8.5)
            self.assertNotIn("BTC/USDT", bot.risk_manager.open_positions)
            bot._reconcile_exchange_state.assert_called_once_with()
            bot._handle_sell_signal.assert_called_once()

    def test_dashboard_manual_close_requires_explicit_confirmation_for_estimated_loss(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(
                exchange=SimpleNamespace(paper_trading=True),
                strategy=SimpleNamespace(trading_pairs=["BTC/USDT"]),
            )
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.is_fallback_simulated = False
            bot.exchange.get_current_price.return_value = 99.0
            bot._cycle_lock = threading.Lock()
            bot._reconcile_exchange_state = Mock(return_value=True)
            position = bot.risk_manager.open_position(
                "BTC/USDT", 100.0, 1.0, entry_fee_quote=1.0, order_id="buy-1"
            )
            bot._handle_sell_signal = Mock()

            result = bot.close_position_manually("BTC/USDT")

            self.assertFalse(result["success"])
            self.assertTrue(result["requires_confirmation"])
            self.assertLess(result["estimated_net_pnl"], 0)
            bot._handle_sell_signal.assert_not_called()
            self.assertIn("BTC/USDT", bot.risk_manager.open_positions)

            def close_from_signal(signal, decision_key):
                bot.risk_manager.close_position(
                    signal.symbol, 99.0, signal.reason, fee_cost=0.5
                )

            bot._handle_sell_signal.side_effect = close_from_signal
            result = bot.close_position_manually("BTC/USDT", allow_loss_close=True)
            self.assertTrue(result["success"])
            self.assertTrue(result["loss_close_override"])
            self.assertNotIn("BTC/USDT", bot.risk_manager.open_positions)

    def test_dashboard_manual_close_requires_confirmation_when_net_estimate_unavailable(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(
                exchange=SimpleNamespace(paper_trading=True),
                strategy=SimpleNamespace(trading_pairs=["BTC/USDT"]),
            )
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.is_fallback_simulated = False
            bot.exchange.get_current_price.side_effect = RuntimeError("price unavailable")
            bot._cycle_lock = threading.Lock()
            bot._reconcile_exchange_state = Mock(return_value=True)
            bot.risk_manager.open_position("BTC/USDT", 100.0, 1.0, order_id="buy-1")
            bot._handle_sell_signal = Mock()

            result = bot.close_position_manually("BTC/USDT")

            self.assertFalse(result["success"])
            self.assertTrue(result["requires_confirmation"])
            self.assertIsNone(result["estimated_net_pnl"])
            bot._handle_sell_signal.assert_not_called()

    def test_dashboard_manual_close_rejects_fallback_or_untracked_positions(self):
        bot = object.__new__(TradingBot)
        bot.config = SimpleNamespace(
            exchange=SimpleNamespace(paper_trading=True),
            strategy=SimpleNamespace(trading_pairs=["BTC/USDT"]),
        )
        bot.exchange = SimpleNamespace(is_fallback_simulated=True)
        self.assertFalse(bot.close_position_manually("BTC/USDT")["success"])

        bot.exchange.is_fallback_simulated = False
        bot.risk_manager = SimpleNamespace(open_positions={})
        bot._cycle_lock = threading.Lock()
        self.assertFalse(bot.close_position_manually("BTC/USDT")["success"])

    def test_rejected_strategy_sell_restores_stop_after_confirmed_zero_fill(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = BotConfig(dashboard_token="test_token")
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.get_current_price.return_value = 49000.0
            bot.exchange.cancel_order.return_value = True
            bot.exchange.place_native_stop_loss.return_value = {"id": "replacement-stop", "status": "open"}
            position = bot.risk_manager.open_position("BTC/USDT", 50000.0, 0.01, order_id="buy_1")
            position.stop_order_id = "initial-stop"
            position.stop_client_order_id = "initial-stop-client"
            bot.risk_manager._save_positions()
            bot._submit_market_order = Mock(return_value=(None, "rejected-sell"))

            bot._handle_sell_signal(TradeSignal(Signal.SELL, "BTC/USDT", 0.9, "test rejection", {}))

            self.assertTrue(bot.risk_manager._is_halted)
            self.assertEqual(position.stop_order_id, "replacement-stop")
            self.assertTrue(bot.exchange.place_native_stop_loss.called)

    def test_open_sell_order_does_not_create_duplicate_protective_sell(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = BotConfig(dashboard_token="test_token")
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.get_current_price.return_value = 49000.0
            bot.exchange.cancel_order.return_value = True
            position = bot.risk_manager.open_position("BTC/USDT", 50000.0, 0.01, order_id="buy_1")
            position.stop_order_id = "initial-stop"
            position.stop_client_order_id = "initial-stop-client"
            bot.risk_manager._save_positions()
            bot._submit_market_order = Mock(return_value=({"id": "still-open", "status": "open", "filled": "0"}, "open-sell"))

            bot._handle_sell_signal(TradeSignal(Signal.SELL, "BTC/USDT", 0.9, "test open order", {}))

            self.assertTrue(bot.risk_manager._is_halted)
            bot.exchange.place_native_stop_loss.assert_not_called()

    def test_f05_sync_exchange_stop_on_breakeven_or_trailing(self):
        """F-05: Tighter break-even or trailing stops synchronize to exchange."""
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = BotConfig(dashboard_token="test_token")
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.cancel_order.return_value = True
            bot.exchange.place_native_stop_loss.return_value = {"id": "new_tighter_stop_id", "status": "open"}

            pos = bot.risk_manager.open_position("BTC/USDT", 50000.0, 0.01, order_id="buy_1")
            pos.stop_order_id = "old_stop_id"
            pos.stop_loss_price = 50000.0
            bot.risk_manager._save_positions()

            synced = bot._sync_exchange_stop("BTC/USDT")
            self.assertTrue(synced)
            bot.exchange.cancel_order.assert_called_with("old_stop_id", "BTC/USDT")
            bot.exchange.place_native_stop_loss.assert_called_with("BTC/USDT", 0.01, 50000.0, pos.stop_client_order_id)
            self.assertEqual(pos.stop_order_id, "new_tighter_stop_id")

    def test_trailing_stop_sync_recovers_ambiguous_create_response(self):
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = BotConfig(dashboard_token="test_token")
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.cancel_order.return_value = True
            bot.exchange.place_native_stop_loss.side_effect = RuntimeError("request timed out after exchange accepted order")
            bot.exchange.fetch_order_by_client_id.return_value = {"id": "recovered-stop", "status": "open"}
            pos = bot.risk_manager.open_position("BTC/USDT", 100.0, 1.0, order_id="buy_1")
            pos.stop_order_id = "old-stop"
            bot.risk_manager._save_positions()
            self.assertTrue(bot._sync_exchange_stop("BTC/USDT"))
            self.assertEqual(pos.stop_order_id, "recovered-stop")

    def test_f13_emergency_stop_cancels_only_bot_orders(self):
        """F-13: Emergency stop cancels bot-owned orders and leaves user orders untouched."""
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = BotConfig(dashboard_token="test_token")
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot._cycle_lock = threading.Lock()
            bot.exchange = Mock()

            bot_order = {"id": "111", "symbol": "BTC/USDT", "clientOrderId": "cb_bot_stop_123"}
            manual_order = {"id": "222", "symbol": "BTC/USDT", "clientOrderId": "web_manual_limit_order"}
            bot.exchange.fetch_account_snapshot.return_value = ({"total": {}}, [bot_order, manual_order])
            bot.exchange.cancel_order.return_value = True

            success, msg = bot.emergency_stop()
            self.assertTrue(success)
            bot.exchange.cancel_order.assert_called_once_with("111", "BTC/USDT")
            self.assertIn("1 external/manual order(s) were left untouched", msg)

    def test_telegram_notification_dispatch(self):
        config = BotConfig(
            telegram_bot_token="fake_bot_token",
            telegram_chat_id="fake_chat_id",
            dashboard_token="test_token",
        )
        with patch.object(TradingBot, "_setup_logging"), \
             patch.object(TradingBot, "_setup_trade_log"), \
             patch("exchange.ExchangeConnector"), \
             patch("bot_engine.urllib.request.urlopen") as mock_urlopen:
            bot = TradingBot(config)
            bot.send_notification("Test telegram alert")
            self.assertTrue(mock_urlopen.called)
            req = mock_urlopen.call_args[0][0]
            self.assertIn("api.telegram.org", req.full_url)
            self.assertIn("fake_bot_token", req.full_url)

    def test_discord_notification_dispatch(self):
        config = BotConfig(
            discord_webhook_url="https://discord.com/api/webhooks/123/xyz",
            dashboard_token="test_token",
        )
        with patch.object(TradingBot, "_setup_logging"), \
             patch.object(TradingBot, "_setup_trade_log"), \
             patch("exchange.ExchangeConnector"), \
             patch("bot_engine.urllib.request.urlopen") as mock_urlopen:
            bot = TradingBot(config)
            bot.send_notification("Test discord alert")
            self.assertTrue(mock_urlopen.called)
            req = mock_urlopen.call_args[0][0]
            self.assertEqual(req.full_url, "https://discord.com/api/webhooks/123/xyz")
            body = json.loads(req.data.decode("utf-8"))
            self.assertIn("Test discord alert", body["content"])

    def test_discord_webhook_failure_does_not_log_credential_url(self):
        webhook_url = "https://discord.com/api/webhooks/123/secret-token"
        bot = object.__new__(TradingBot)
        bot.config = SimpleNamespace(discord_webhook_url=webhook_url)
        with patch("bot_engine.urllib.request.urlopen", side_effect=urllib.error.URLError(webhook_url)):
            with self.assertLogs("bot.engine", level="WARNING") as captured:
                bot._send_discord_notification("test")
        self.assertNotIn(webhook_url, "\n".join(captured.output))

    def test_email_notification_dispatch(self):
        config = BotConfig(
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="alerts@example.com",
            smtp_password="secretpassword",
            alert_email="trader@example.com",
            dashboard_token="test_token",
        )
        with patch.object(TradingBot, "_setup_logging"), \
             patch.object(TradingBot, "_setup_trade_log"), \
             patch("exchange.ExchangeConnector"), \
             patch("smtplib.SMTP") as mock_smtp:
            server_instance = MagicMock()
            server_instance.sendmail.return_value = {}
            mock_smtp.return_value = server_instance

            bot = TradingBot(config)
            result = bot.send_notification("Test email body", subject="Test Subject")
            self.assertEqual(result["channels"]["email"], "accepted_by_provider")

            mock_smtp.assert_called_with("smtp.example.com", 587, timeout=10)
            server_instance.starttls.assert_called_once()
            server_instance.login.assert_called_with("alerts@example.com", "secretpassword")
            server_instance.sendmail.assert_called_once()
            args = server_instance.sendmail.call_args[0]
            self.assertEqual(args[0], "alerts@example.com")
            self.assertEqual(args[1], ["trader@example.com"])
            self.assertIn("Test Subject", args[2])

    def test_resend_email_notification_uses_https_provider_without_logging_key(self):
        config = BotConfig(
            resend_api_key="re_test_secret",
            alert_email_from="Trading Lab <alerts@example.com>",
            alert_email="trader@example.com",
            dashboard_token="test_token",
        )
        with patch.object(TradingBot, "_setup_logging"), \
             patch.object(TradingBot, "_setup_trade_log"), \
             patch("exchange.ExchangeConnector"), \
             patch("bot_engine.urllib.request.urlopen") as mock_urlopen:
            response = MagicMock()
            response.status = 200
            response.__enter__.return_value = response
            mock_urlopen.return_value = response
            bot = TradingBot(config)
            result = bot.send_notification("Test email body", subject="Test Subject")
            self.assertEqual(result["channels"]["email"], "accepted_by_provider")
            request = mock_urlopen.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.resend.com/emails")
            self.assertEqual(request.get_header("Authorization"), "Bearer re_test_secret")
            payload = json.loads(request.data.decode("utf-8"))
            self.assertEqual(payload["to"], ["trader@example.com"])
            self.assertEqual(payload["from"], "Trading Lab <alerts@example.com>")
            self.assertEqual(payload["subject"], "Test Subject")
            self.assertIn("Test email body", payload["text"])
            self.assertFalse(mock_urlopen.call_args.kwargs.get("headers"))

    def test_resend_email_notification_rejects_http_error_without_leaking_key(self):
        config = BotConfig(
            resend_api_key="re_test_secret",
            alert_email_from="alerts@example.com",
            alert_email="trader@example.com",
            dashboard_token="test_token",
        )
        with patch.object(TradingBot, "_setup_logging"), \
             patch.object(TradingBot, "_setup_trade_log"), \
             patch("exchange.ExchangeConnector"), \
             patch("bot_engine.urllib.request.urlopen", side_effect=urllib.error.HTTPError(
                 "https://api.resend.com/emails", 401, "Unauthorized", {}, io.BytesIO(b"secret response")
             )), \
             self.assertLogs("bot.engine", level="WARNING") as captured:
            bot = TradingBot(config)
            result = bot.send_notification("Test email body")
        self.assertEqual(result["channels"]["email"], "failed")
        self.assertNotIn("re_test_secret", "\n".join(captured.output))
        self.assertNotIn("secret response", "\n".join(captured.output))

    def test_sendgrid_email_notification_supports_verified_single_sender_over_https(self):
        config = BotConfig(
            sendgrid_api_key="SG.test_secret",
            alert_email_from="myaddress@gmail.com",
            alert_email="trader@example.com",
            dashboard_token="test_token",
        )
        with patch.object(TradingBot, "_setup_logging"), \
             patch.object(TradingBot, "_setup_trade_log"), \
             patch("exchange.ExchangeConnector"), \
             patch("bot_engine.urllib.request.urlopen") as mock_urlopen:
            response = MagicMock()
            response.status = 202
            response.__enter__.return_value = response
            mock_urlopen.return_value = response
            bot = TradingBot(config)
            result = bot.send_notification("Test email body", subject="Test Subject")
        self.assertEqual(result["channels"]["email"], "accepted_by_provider")
        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.sendgrid.com/v3/mail/send")
        self.assertEqual(request.get_header("Authorization"), "Bearer SG.test_secret")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["from"]["email"], "myaddress@gmail.com")
        self.assertEqual(payload["personalizations"][0]["to"][0]["email"], "trader@example.com")
        self.assertEqual(payload["subject"], "Test Subject")
        self.assertEqual(payload["content"][0]["value"], "Test email body")

    def test_safety_halt_alert_contains_reason_and_remains_off_order_thread(self):
        bot = object.__new__(TradingBot)
        bot.config = SimpleNamespace(
            enable_notifications=True,
            exchange=SimpleNamespace(paper_trading=True, paper_environment="demo"),
        )
        bot._send_notification_background = Mock()

        bot._notify_safety_halt("Exchange reconciliation failed: unresolved order intent")

        args, _kwargs = bot._send_notification_background.call_args
        self.assertIn("Exchange reconciliation failed: unresolved order intent", args[0])
        self.assertIn("NEW ENTRIES BLOCKED", args[0])
        self.assertEqual(args[1], "Trading Bot Safety Halt")

    def test_buy_setup_alert_is_explicitly_not_an_order(self):
        bot = object.__new__(TradingBot)
        bot.config = SimpleNamespace(
            enable_notifications=True,
            strategy=SimpleNamespace(name="dynamic_regime", timeframe="1h"),
        )
        bot._send_notification_background = Mock()
        signal = TradeSignal(
            Signal.BUY, "BTC/USDT", 0.75, "EMA breakout confirmed",
            {"strategy": "macro_donchian_ema72", "route": "uptrend", "close": 60000},
        )

        bot._notify_buy_setup(signal)

        args, _kwargs = bot._send_notification_background.call_args
        self.assertEqual(args[1], "BUY Setup: BTC/USDT")
        self.assertIn("BUY SETUP DETECTED — NO ORDER CONFIRMED", args[0])
        self.assertIn("Risk, account, and order checks may still block the entry", args[0])

    def test_notification_disabled(self):
        config = BotConfig(
            enable_notifications=False,
            telegram_bot_token="fake_bot_token",
            telegram_chat_id="fake_chat_id",
            smtp_host="smtp.example.com",
            alert_email="trader@example.com",
            discord_webhook_url="https://discord.com/api/webhooks/123/xyz",
            dashboard_token="test_token",
        )
        with patch.object(TradingBot, "_setup_logging"), \
             patch.object(TradingBot, "_setup_trade_log"), \
             patch("exchange.ExchangeConnector"), \
             patch("bot_engine.urllib.request.urlopen") as mock_urlopen, \
             patch("smtplib.SMTP") as mock_smtp:
            bot = TradingBot(config)
            bot.send_notification("Should not be sent")
            self.assertFalse(mock_urlopen.called)
            self.assertFalse(mock_smtp.called)


# ==============================================================================
# 7. Dashboard API & Security Suite
# ==============================================================================

class TestReconcileOrdersDashboardHandler(unittest.TestCase):
    def test_saved_demo_sell_recovery_endpoint_is_same_origin_and_sends_no_order(self):
        dashboard = object.__new__(WebDashboard)
        dashboard._status_cache = "stale"
        dashboard.bot = Mock()
        dashboard.bot.reconcile_terminal_demo_sell.return_value = {
            "success": True, "message": "Verified sale reconciled.", "trading_halted": True,
        }

        class Request:
            headers = {"Host": "localhost:8080", "Origin": "http://localhost:8080"}
            scheme = "http"
            host = "localhost:8080"

            async def json(self):
                return {"symbol": "BTC/USDT", "terminal_sell": True}

        class InlineLoop:
            async def run_in_executor(self, _executor, function, *args):
                return function(*args)

        request = Request()
        with patch("dashboard.asyncio.get_event_loop", return_value=InlineLoop()):
            coroutine = dashboard.handle_reconcile_external_close(request)
            try:
                coroutine.send(None)
            except StopIteration as completed:
                response = completed.value
            else:
                self.fail("The terminal-sale recovery endpoint unexpectedly yielded")

        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(response.text)["trading_halted"])
        self.assertIsNone(dashboard._status_cache)
        dashboard.bot.reconcile_terminal_demo_sell.assert_called_once_with("BTC/USDT")
        dashboard.bot.reconcile_external_close.assert_not_called()

    def test_endpoint_is_same_origin_and_never_runs_a_cycle(self):
        dashboard = object.__new__(WebDashboard)
        dashboard._status_cache = "stale"
        dashboard.bot = Mock()
        dashboard.bot.reconcile_orders_only.return_value = (
            True, "Binance order and account checks passed. Entries remain paused."
        )
        dashboard.bot.risk_manager.entry_mode.return_value = "EXIT_ONLY"

        req = make_mocked_request(
            "POST", "/api/reconcile-orders-only",
            headers={"Host": "localhost:8080", "Origin": "http://localhost:8080"},
        )
        class InlineLoop:
            async def run_in_executor(self, _executor, function):
                return function()

        def finish(coroutine):
            try:
                coroutine.send(None)
            except StopIteration as completed:
                return completed.value
            self.fail("The reconciliation handler unexpectedly yielded")

        with patch("dashboard.asyncio.get_running_loop", return_value=InlineLoop()):
            response = finish(dashboard.handle_reconcile_orders_only(req))

        self.assertEqual(response.status, 200)
        body = json.loads(response.text)
        self.assertTrue(body["success"])
        self.assertEqual(body["entry_mode"], "EXIT_ONLY")
        self.assertIsNone(dashboard._status_cache)
        dashboard.bot.reconcile_orders_only.assert_called_once_with()
        dashboard.bot.run_once.assert_not_called()

        evil = make_mocked_request(
            "POST", "/api/reconcile-orders-only",
            headers={"Host": "localhost:8080", "Origin": "http://attacker.invalid"},
        )
        with patch("dashboard.asyncio.get_running_loop", return_value=InlineLoop()):
            rejected = finish(dashboard.handle_reconcile_orders_only(evil))
        self.assertEqual(rejected.status, 403)
        dashboard.bot.reconcile_orders_only.assert_called_once_with()


class TestDashboardApiAndSecurity(unittest.TestCase):
    """Verifies authentication, CSRF origin checking, health probes, and cache-control headers."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        with patch("exchange.ExchangeConnector._create_exchange"):
            self.dashboard = WebDashboard(port=8080)
            self.dashboard.config.dashboard_token = "secret123"

    def tearDown(self):
        self.loop.close()

    def test_verify_token_mechanisms(self):
        # 1. Bearer Token
        req_bearer = make_mocked_request("GET", "/api/status", headers={"Authorization": "Bearer secret123"})
        self.assertTrue(verify_token(req_bearer, "secret123"))

        # 2. Basic Auth
        basic_val = base64.b64encode(b":secret123").decode()
        req_basic = make_mocked_request("GET", "/api/status", headers={"Authorization": f"Basic {basic_val}"})
        self.assertTrue(verify_token(req_basic, "secret123"))

        # 3. Header Token
        req_header = make_mocked_request("GET", "/api/status", headers={"X-Dashboard-Token": "secret123"})
        self.assertTrue(verify_token(req_header, "secret123"))

        # 4. Cookie Token
        req_cookie = make_mocked_request("GET", "/api/status", headers={"Cookie": "dashboard_token=secret123"})
        self.assertTrue(verify_token(req_cookie, "secret123"))

        # 5. Invalid Token
        req_bad = make_mocked_request("GET", "/api/status", headers={"Authorization": "Bearer wrong"})
        self.assertFalse(verify_token(req_bad, "secret123"))
        req_query = make_mocked_request("GET", "/api/status?token=secret123")
        self.assertFalse(verify_token(req_query, "secret123"))
        self.assertFalse(verify_token(req_bearer, ""))

    def test_csrf_same_origin_validation(self):
        with patch.dict(os.environ, {"DASHBOARD_TOKEN": "secret123"}):
            req_valid = make_mocked_request(
                "POST", "/api/exit-only",
                headers={"Host": "localhost:8080", "Origin": "http://localhost:8080"}
            )
            self.assertTrue(self.dashboard._is_valid_origin(req_valid))

            req_evil = make_mocked_request(
                "POST", "/api/exit-only",
                headers={"Host": "localhost:8080", "Origin": "http://attacker.com"}
            )
            self.assertFalse(self.dashboard._is_valid_origin(req_evil))

    def test_trades_summary_and_export_endpoints(self):
        with TemporaryDirectory() as persistence_dir:
            tmp = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            tmp.open_position("BTC/USDT", 100.0, 1.0, entry_fee_quote=1.0)
            record = tmp.close_position("BTC/USDT", 110.0, "test", fee_cost=2.0)
            # Seed the dashboard bot's in-memory history only (no file writes).
            self.dashboard.bot.risk_manager.trade_history = [record]

            req = make_mocked_request("GET", "/api/trades/summary")
            resp = self.loop.run_until_complete(self.dashboard.handle_trades_summary(req))
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.text)
            self.assertEqual(body["summary"]["total_fees"], 3.0)
            self.assertEqual(body["summary"]["total_pnl"], 7.0)
            self.assertEqual(body["by_symbol"]["BTC/USDT"]["net_pnl"], 7.0)
            self.assertEqual(body["by_symbol"]["BTC/USDT"]["fees"], 3.0)

            req_csv = make_mocked_request("GET", "/api/trades/export.csv")
            resp_csv = self.loop.run_until_complete(self.dashboard.handle_trades_export(req_csv))
            self.assertEqual(resp_csv.status, 200)
            self.assertIn("text/csv", resp_csv.headers["Content-Type"])
            self.assertIn("BTC/USDT", resp_csv.text)
            self.assertIn("net_pnl", resp_csv.text)

    def test_liveness_and_readiness_probes(self):
        req_live = make_mocked_request("GET", "/api/health/liveness")
        resp_live = self.loop.run_until_complete(self.dashboard.handle_liveness(req_live))
        self.assertEqual(resp_live.status, 200)
        self.assertIn('"status": "alive"', resp_live.text)

    def test_market_activity_summary_counts_only_recent_relevant_blocks(self):
        now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
        market_events = [
            {"created_at": "2026-11-11T11:00:00Z", "details": {
                "signal": "HOLD", "reason": "No consensus (BUY: 0/3, SELL: 0/3)",
                "votes": {name: {"signal": "HOLD"} for name in ("ema", "rsi", "macd")},
                "indicators": {"entry_gates": {"trend": {"enabled": True, "relevant_to_current_buy": False, "passed": False}}},
            }},
            {"created_at": "2026-11-11T10:00:00Z", "details": {
                "signal": "HOLD", "reason": "BUY BLOCKED by Downtrend filter",
                "votes": {"ema": {"signal": "BUY"}, "rsi": {"signal": "HOLD"}, "macd": {"signal": "BUY"}},
                "indicators": {"entry_gates": {"trend": {"enabled": True, "relevant_to_current_buy": True, "passed": False}}},
            }},
            {"created_at": "2026-11-09T10:00:00Z", "details": {"signal": "BUY"}},
        ]
        audit_events = [{"event_type": "market_data_issue", "created_at": "2026-11-11T09:00:00Z"}]
        summary = WebDashboard._summarize_market_activity(market_events, audit_events, now=now)
        self.assertEqual(summary["checks"], 2)
        self.assertEqual(summary["no_consensus"], 1)
        self.assertEqual(summary["all_votes_hold"], 1)
        self.assertEqual(summary["buy_setups_blocked"], 1)
        self.assertEqual(summary["blocked_by_gate"], {"trend": 1})
        self.assertEqual(summary["analysis_issues"], 1)
        self.assertFalse(summary["capped"])

    def test_market_activity_summary_reports_when_window_is_capped(self):
        now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
        market_events = [{"created_at": "2026-11-11T11:00:00Z", "details": {"signal": "HOLD"}}] * 5
        capped = WebDashboard._summarize_market_activity(market_events, [], now=now, cap=5)
        self.assertTrue(capped["capped"])
        self.assertEqual(capped["checks"], 5)
        uncapped = WebDashboard._summarize_market_activity(market_events, [], now=now, cap=100)
        self.assertFalse(uncapped["capped"])

    def test_asset_serving_and_cache_control(self):
        req_css = make_mocked_request("GET", "/assets/app.css", match_info={"name": "app.css"})
        resp_css = self.loop.run_until_complete(self.dashboard.handle_asset(req_css))
        self.assertEqual(resp_css.status, 200)
        self.assertIn("no-cache", resp_css.headers.get("Cache-Control", ""))

        req_missing = make_mocked_request("GET", "/assets/unknown.txt", match_info={"name": "unknown.txt"})
        with self.assertRaises(HTTPNotFound):
            self.loop.run_until_complete(self.dashboard.handle_asset(req_missing))

    def test_f16_healthcheck_handlers(self):
        """F-16: Liveness and readiness probes respond with clean status without secrets."""
        dash = object.__new__(WebDashboard)
        dash.bot = Mock()
        dash.bot.risk_manager = Mock()
        dash.bot.exchange = Mock()
        dash.bg_task = Mock()
        dash.bg_task.done.return_value = False
        dash._worker_interval_seconds = 60
        dash._worker_cycle_started_at = time.time() - 10
        dash._worker_cycle_completed_at = time.time() - 5
        dash._worker_last_cycle_ok = True
        dash._worker_state = "running"
        dash._worker_last_error = ""

        req = Mock()
        resp = asyncio.run(dash.handle_liveness(req))
        self.assertEqual(resp.status, 200)

        dash.bot._last_reconciliation_ok = True
        dash.bot.risk_manager._is_halted = False
        dash.bot.risk_manager.entry_mode.return_value = "ACTIVE"
        dash.bot.exchange.is_fallback_simulated = False
        resp_ready = asyncio.run(dash.handle_readiness(req))
        self.assertEqual(resp_ready.status, 200)
        ready_body = json.loads(resp_ready.text)
        self.assertEqual(ready_body["worker"]["state"], "running")
        self.assertTrue(ready_body["worker"]["task_alive"])
        self.assertTrue(ready_body["worker"]["last_cycle_ok"])

        dash._worker_cycle_completed_at = time.time() - 301
        resp_stale = asyncio.run(dash.handle_readiness(req))
        self.assertEqual(resp_stale.status, 503)
        self.assertEqual(json.loads(resp_stale.text)["worker"]["state"], "stale")
        dash._worker_cycle_completed_at = time.time() - 5

        dash.bg_task.done.return_value = True
        resp_stopped = asyncio.run(dash.handle_readiness(req))
        self.assertEqual(resp_stopped.status, 503)
        self.assertFalse(json.loads(resp_stopped.text)["worker"]["task_alive"])
        dash.bg_task.done.return_value = False

        dash.bot.risk_manager._is_halted = True
        resp_halted = asyncio.run(dash.handle_readiness(req))
        self.assertEqual(resp_halted.status, 503)


# ==============================================================================
# 8. Templates, Wording Safety & Mobile UX Suite
# ==============================================================================

class TestDashboardAuthMiddleware(unittest.TestCase):
    def test_auth_helpers_reject_unconfigured_and_query_tokens(self):
        req = make_mocked_request("GET", "/api/status?token=secret123")
        self.assertFalse(verify_token(req, ""))
        self.assertFalse(verify_token(req, "secret123"))

    def test_protective_stop_review_remains_available_when_balance_reconciliation_fails(self):
        risk = SimpleNamespace(
            _is_halted=True,
            _halt_reason="Exchange stop filled for XRP/USDT; manual review required before resuming",
        )
        self.assertTrue(WebDashboard._manual_resume_available(
            risk, [], account_available=True, exchange_reconciled=False, unresolved_orders=[]
        ))
        self.assertFalse(WebDashboard._manual_resume_available(
            risk, [object()], account_available=True, exchange_reconciled=False, unresolved_orders=[]
        ))
        self.assertFalse(WebDashboard._manual_resume_available(
            risk, [], account_available=True, exchange_reconciled=False, unresolved_orders=[object()]
        ))
        risk._halt_reason = "Emergency stop failed; operator review required"
        self.assertFalse(WebDashboard._manual_resume_available(
            risk, [], account_available=True, exchange_reconciled=False, unresolved_orders=[]
        ))

    def test_reconciled_order_intent_halt_offers_review_only_when_flat_and_clean(self):
        risk = SimpleNamespace(
            _is_halted=True,
            _halt_reason="Unresolved order intent exists; exchange reconciliation required",
        )
        self.assertTrue(WebDashboard._manual_resume_available(
            risk, [], account_available=True, exchange_reconciled=True, unresolved_orders=[]
        ))
        self.assertFalse(WebDashboard._manual_resume_available(
            risk, [], account_available=True, exchange_reconciled=False, unresolved_orders=[]
        ))
        self.assertFalse(WebDashboard._manual_resume_available(
            risk, [], account_available=True, exchange_reconciled=True, unresolved_orders=[object()]
        ))
        self.assertFalse(WebDashboard._manual_resume_available(
            risk, [object()], account_available=True, exchange_reconciled=True, unresolved_orders=[]
        ))
        self.assertFalse(WebDashboard._manual_resume_available(
            risk, [], account_available=False, exchange_reconciled=True, unresolved_orders=[]
        ))

class TestTemplatesWordingAndMobileUx(unittest.TestCase):
    """Verifies plain-English wording, absence of misleading claims, glossary clarity, and mobile UI responsiveness."""

    def test_ema72_strategy_shows_rule_status_instead_of_voting_unavailable(self):
        strategy_page = (Path("templates") / "strategy.html").read_text(encoding="utf-8")
        self.assertIn("if(isDonchian){", strategy_page)
        self.assertIn("Donchian breakout and EMA strategy rules", strategy_page)
        self.assertIn("EMA72 strategy · WARMUP / DATA CHECK", strategy_page)
        self.assertIn(
            "This strategy uses breakout and EMA rules, not EMA/RSI/MACD voting.",
            strategy_page,
        )

    def test_strategy_decision_view_explains_dynamic_routing_and_fallback(self):
        strategy_page = (Path("templates") / "strategy.html").read_text(encoding="utf-8")
        self.assertIn("Dynamic regime routing", strategy_page)
        self.assertIn("How a decision becomes an order", strategy_page)
        self.assertIn("Recent decisions", strategy_page)
        self.assertIn("Confirmed for 3 closed candles", strategy_page)
        self.assertIn("Stand aside · no route", strategy_page)

    def test_machine_labels_are_formatted_for_dashboard_readers(self):
        strategy_page = (Path("templates") / "strategy.html").read_text(encoding="utf-8")
        logs_page = (Path("templates") / "logs.html").read_text(encoding="utf-8")
        overview_page = (Path("templates") / "overview.html").read_text(encoding="utf-8")
        positions_page = (Path("templates") / "positions.html").read_text(encoding="utf-8")
        self.assertIn("macro_donchian_ema72:'Donchian breakout · EMA 72'", strategy_page)
        self.assertIn("macro_trend_pullback:'Trend pullback · EMA 36'", strategy_page)
        self.assertIn("Macro 200 EMA", overview_page)
        self.assertIn("Pullback 20 EMA", overview_page)
        self.assertIn("Reclaim 9 EMA", overview_page)
        self.assertIn("EMA36 close", positions_page)
        self.assertIn("replaceAll('_',' ')", strategy_page)
        self.assertIn("replaceAll('_',' ')", logs_page)
        self.assertIn("readableReason", overview_page)
        self.assertIn("needs_(\\d+)_closed_hourly_candles_received_(\\d+)", logs_page)
        self.assertIn("Donchian breakout · EMA 72", strategy_page)
        self.assertIn("paper_environment_changed:'Paper account changed'", logs_page)
        self.assertIn("Enter your private dashboard access token", LOGIN_HTML)
        self.assertNotIn("DASHBOARD_TOKEN", LOGIN_HTML)
        self.assertIn("review-closed-trade", overview_page)
        self.assertIn("/api/reconcile-closed-trade", overview_page)
        self.assertIn("reconcile-orders", overview_page)
        self.assertIn("/api/reconcile-orders-only", overview_page)

    def test_wording_safety_no_misleading_profit_claims(self):
        banned_phrases = [
            "guaranteed profit",
            "risk-free",
            "money printer",
            "get rich quick",
            "sure win",
            "100% win",
            "guaranteed return"
        ]
        templates_dir = Path("templates")
        for file in templates_dir.glob("*.html"):
            content = file.read_text(encoding="utf-8").lower()
            for phrase in banned_phrases:
                self.assertNotIn(phrase, content, f"Misleading claim '{phrase}' found in {file.name}")

    def test_practice_mode_and_paper_trading_disclaimers(self):
        overview = (Path("templates") / "overview.html").read_text(encoding="utf-8")
        self.assertIn("Paper-trading", overview)
        self.assertIn("Practice mode", overview)
        self.assertIn("App-monitored exits can fail during outages", overview)

    def test_trade_history_explains_profit_factor_and_incomplete_accounting(self):
        history = (Path("templates") / "history.html").read_text(encoding="utf-8")
        self.assertIn("Profit factor", history)
        self.assertIn("Net gains ÷ net losses", history)
        self.assertIn("Net P&amp;L includes recorded fees", history)
        self.assertIn("'Net P&L','Fees','Accounting'", history)
        self.assertIn("valid?'Reconciled':'Review needed'", history)
        self.assertIn("Pending review", history)
        self.assertIn("incomplete fee or partial-fill accounting", history)
        self.assertIn("profit-factor-note", history)
        self.assertIn("d.profit_factor==null?'N/A'", history)
        self.assertIn("function finishLoading()", history)

    def test_glossary_definitions_completeness(self):
        glossary = (Path("templates") / "glossary.html").read_text(encoding="utf-8")
        expected_terms = [
            "Cash balance",
            "Account value / net worth",
            "Profit and loss (P&amp;L)",
            "Realized and unrealized P&amp;L",
            "Profit factor",
            "Stop-loss",
            "Take-profit",
            "Fees and slippage",
            "Backtest",
            "Paper trading / testnet"
        ]
        for term in expected_terms:
            self.assertIn(term, glossary)

    def test_positions_page_has_manual_close_and_fee_estimate_disclosure(self):
        positions = (Path("templates") / "positions.html").read_text(encoding="utf-8")
        dashboard = (Path("dashboard.py")).read_text(encoding="utf-8")
        self.assertIn("Estimated net P&amp;L", positions)
        self.assertIn("Close position", positions)
        self.assertIn("/api/close-position", positions)
        self.assertIn("estimated_net_pnl", dashboard)
        self.assertIn("estimated_exit_fee_rate", dashboard)
        self.assertIn("allow_loss_close", positions)
        self.assertIn("No order has been sent yet", positions)
        self.assertIn("prices[sym] = float(p)", dashboard)
        self.assertIn("const priceText", positions)

    def test_manual_demo_controls_explain_risk_and_reuse_positions_sell_flow(self):
        overview = (Path("templates") / "overview.html").read_text(encoding="utf-8")
        trade_desk = (Path("templates") / "trade_desk.html").read_text(encoding="utf-8")
        positions = (Path("templates") / "positions.html").read_text(encoding="utf-8")
        dashboard = (Path("dashboard.py")).read_text(encoding="utf-8")
        engine = (Path("bot_engine.py")).read_text(encoding="utf-8")
        self.assertIn('href="/trade-desk">Trade desk', overview)
        self.assertNotIn('id="manual-demo-trade"', overview)
        self.assertIn("Manual order", trade_desk)
        self.assertIn("Fees, slippage, and outages can cause losses", trade_desk)
        self.assertIn("/api/manual-demo-buy", trade_desk)
        self.assertIn("/api/close-position", trade_desk)
        self.assertIn("manual_close_available", trade_desk)
        self.assertIn("allow_loss_close:true", trade_desk)
        self.assertIn("/api/candles", trade_desk)
        self.assertIn('order_status:', (Path("templates") / "logs.html").read_text(encoding="utf-8"))
        self.assertIn("Origin not recorded", (Path("templates") / "trade_desk.html").read_text(encoding="utf-8"))
        self.assertIn("/api/close-position", positions)
        self.assertIn("Manual Demo", positions)
        self.assertIn("handle_manual_demo_buy", dashboard)
        self.assertIn("Manual buys are available only on Binance Demo", engine)
        self.assertIn("operator-managed Demo position", engine)
        self.assertIn("The exchange protective stop and configured target remain active", positions)

    def test_mobile_overflow_protection_in_css(self):
        """Verify CSS contains mobile overflow prevention rules and responsive layouts."""
        css_path = Path(__file__).parent / "templates" / "app.css"
        self.assertTrue(css_path.exists(), "templates/app.css must exist")
        css_text = css_path.read_text(encoding="utf-8")

        self.assertIn("overflow-x: clip", css_text)
        self.assertIn("@media (max-width: 700px)", css_text)
        self.assertIn("@media (max-width: 480px)", css_text)
        self.assertIn(".overview-metrics", css_text)
        self.assertIn(".chart-price-row", css_text)
        self.assertIn("flex-wrap: wrap", css_text)
        self.assertIn(".table-wrap", css_text)
        self.assertIn("-webkit-overflow-scrolling: touch", css_text)
        self.assertIn(".trading-controls-actions", css_text)

    def test_template_html_mobile_meta_and_navigation_integrity(self):
        """Verify all templates have responsive viewport meta tags and balanced navigation."""
        templates_dir = Path(__file__).parent / "templates"
        template_files = ["overview.html", "positions.html", "history.html", "strategy.html", "glossary.html"]

        for filename in template_files:
            file_path = templates_dir / filename
            self.assertTrue(file_path.exists(), f"{filename} must exist")
            html = file_path.read_text(encoding="utf-8")

            self.assertIn('name="viewport"', html, f"{filename} missing viewport meta tag")
            self.assertIn('content="width=device-width', html, f"{filename} missing width=device-width")
            self.assertIn('class="brand"', html, f"{filename} missing brand class")
            self.assertIn('class="nav-links"', html, f"{filename} missing nav-links class")
            self.assertIn('data-theme-toggle', html, f"{filename} missing data-theme-toggle")

            nav_links_start = html.find('class="nav-links"')
            nav_links_close = html.find('</div>', nav_links_start)
            theme_toggle_pos = html.find('data-theme-toggle')
            self.assertTrue(
                theme_toggle_pos > nav_links_close,
                f"{filename}: theme-toggle must be outside nav-links"
            )
            self.assertEqual(html.count('</main>'), 1, f"{filename} must have exactly one </main> tag")


# ==============================================================================
# 9. Backtesting & Realism Suite
# ==============================================================================

class TestBacktestingAndRealism(unittest.TestCase):
    """Verifies backtesting execution realism, lookahead prevention, and exploratory warnings."""

    def test_research_squeeze_simulator_uses_capped_initial_stop_and_close_based_trail(self):
        from research.backtest_strategy import simulate

        frame = pd.DataFrame({
            "timestamp_ms": [1_700_000_000_000 + i * 3_600_000 for i in range(3)],
            "open": [100.0, 100.0, 100.0],
            "high": [101.0, 104.0, 101.0],
            "low": [99.0, 99.0, 98.0],
            "close": [100.0, 104.0, 99.0],
            "atr": [2.0, 2.0, 2.0],
            "swing_low_reference": [94.0, 94.0, 94.0],
            "signal": ["BUY", "HOLD", "HOLD"],
        })
        sim = simulate(
            {"BTCUSDT": frame}, 0.0, 0.0, enforce_risk_halts=False,
            initial_stop_model="swing_low_atr_capped",
            trailing_atr_multiple=2.5,
            trailing_activation_atr_multiple=1.5,
            trailing_uses_close=True,
            gap_exit_atr_multiple=3.5,
        )
        self.assertEqual(len(sim["trades"]), 1)
        self.assertEqual(sim["trades"][0]["reason"], "stop")
        # The signal's swing low is more than 4% away, so the first stop is
        # capped; the completed t=1 close then ratchets it to 104 - 2.5*2=99.
        self.assertAlmostEqual(sim["trades"][0]["exit"], 99.0, places=6)

    def test_research_macro_donchian_stop_and_channel_exit_do_not_apply_legacy_trailing(self):
        from research.backtest_strategy import simulate

        frame = pd.DataFrame({
            "timestamp_ms": [1_700_000_000_000 + i * 3_600_000 for i in range(3)],
            "open": [100.0, 100.0, 100.0],
            "high": [101.0, 110.0, 101.0],
            "low": [99.0, 99.0, 97.0],
            "close": [100.0, 109.0, 100.0],
            "atr": [2.0, 2.0, 2.0],
            "swing_low_reference": [90.0, 90.0, 90.0],
            "signal": ["BUY", "SELL", "HOLD"],
        })
        sim = simulate(
            {"BTCUSDT": frame}, 0.0, 0.0, enforce_risk_halts=False,
            initial_stop_model="swing_low_floor_4pct",
            take_profit_atr_multiple=100.0,
            enable_breakeven_trailing=False,
        )
        self.assertEqual(len(sim["trades"]), 1)
        self.assertEqual(sim["trades"][0]["reason"], "sell_consensus")
        self.assertAlmostEqual(sim["trades"][0]["exit"], 100.0, places=6)

    def test_backtest_exploratory_warning_and_next_bar_logic(self):
        cfg = BotConfig()
        cfg.strategy.name = "combined"  # generic replay only models legacy strategies
        cfg.strategy.trading_pairs = ["BTC/USDT"]
        
        dates = pd.date_range("2026-01-01", periods=100, freq="15min")
        synthetic_ohlcv = [
            [int(d.timestamp() * 1000), 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 1000.0]
            for i, d in enumerate(dates)
        ]
        with patch("exchange.ExchangeConnector.fetch_ohlcv", return_value=synthetic_ohlcv):
            f = io.StringIO()
            with redirect_stdout(f):
                run_backtest(cfg)
            out = f.getvalue()
            self.assertIn("EXPLORATORY HISTORICAL REPLAY ONLY", out)
            self.assertIn("BACKTEST RESULTS", out)
            self.assertIn("Do NOT use these simulated results as evidence of live edge", out)

    def test_generic_backtest_rejects_active_candidate(self):
        cfg = BotConfig()
        cfg.strategy.name = "macro_donchian_ema72"
        with patch("exchange.ExchangeConnector.fetch_ohlcv") as fetch_ohlcv:
            with self.assertRaisesRegex(SystemExit, "does not model macro_donchian_ema72"):
                run_backtest(cfg)
        fetch_ohlcv.assert_not_called()


class TestShadowLedger(unittest.TestCase):
    """Halt-free, order-free forward expectancy ledger."""

    def _store(self, tmpdir):
        return StateStore(Path(tmpdir) / "state.db")

    def test_next_open_entry_and_stop_first(self):
        with TemporaryDirectory() as tmpdir:
            ledger = ShadowLedger(self._store(tmpdir), initial_equity=10000.0)
            # Candle 1: BUY queues an entry for the next open.
            ledger.advance("macro_donchian_ema72", "BTC/USDT",
                           [1, 100.0, 105.0, 99.0, 104.0, 1000.0], "BUY", stop_ref=95.0, atr=1.0)
            self.assertEqual(len(ledger.trades), 0)
            self.assertEqual(len(ledger.positions), 0)
            self.assertTrue(ledger.has_position("macro_donchian_ema72", "BTC/USDT") is False)
            # Candle 2: entry fills at open 101, then the low breaches the stop.
            ledger.advance("macro_donchian_ema72", "BTC/USDT",
                           [2, 101.0, 102.0, 90.0, 95.0, 1000.0], "HOLD", stop_ref=95.0, atr=1.0)
            self.assertEqual(len(ledger.trades), 1)
            self.assertEqual(ledger.trades[0]["reason"], "stop_loss")
            self.assertLess(ledger.trades[0]["pnl"], 0)

    def test_next_open_exit_and_persistence(self):
        with TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            ledger = ShadowLedger(store, initial_equity=10000.0)
            # Entry queued on candle 1.
            ledger.advance("macro_donchian_ema72", "BTC/USDT",
                           [1, 100.0, 105.0, 99.0, 104.0, 1000.0], "BUY", stop_ref=95.0, atr=1.0)
            # Candle 2 fills entry and queues a SELL exit (close below EMA72).
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
