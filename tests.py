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
import threading
import urllib.error
import unittest
from contextlib import closing, redirect_stdout
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
from dashboard import LOGIN_HTML, WebDashboard, verify_token
from exchange import (
    AccountAccessError,
    ExchangeConnector,
    account_access_message,
    extract_ccxt_response_metadata,
    filter_closed_ohlcv,
    validate_candle_sequence,
)
from main import run_backtest
from risk_manager import Position, RiskManager
from state_store import StateStore
from strategies import (
    CombinedStrategy,
    EMA_Crossover_Strategy,
    MACD_Strategy,
    RSI_Mean_Reversion_Strategy,
    Signal,
    TradeSignal,
    get_strategy,
    ohlcv_to_dataframe,
)
from shadow_strategy import evaluate_shadow_candidate
from regime_router import evaluate_regime_router


class TestShadowLogSafety(unittest.TestCase):
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
            else:
                close = 100.0
                spread = 0.2
            candles.append([
                1_700_000_000_000 + index * 3_600_000,
                close, close + spread, close - spread, close, 100.0,
            ])
        return candles

    def test_router_is_shadow_only_and_requires_persistent_uptrend(self):
        result = evaluate_regime_router(self.candles(), "macro_donchian_ema72")
        self.assertEqual(result["regime"], "uptrend")
        self.assertEqual(result["route"], "observe_existing_strategy_only")
        self.assertFalse(result["entry_authorized"])
        self.assertEqual(result["raw_regimes"], ["uptrend"] * 3)

    def test_dynamic_router_selects_only_the_testnet_trend_candidate(self):
        uptrend = evaluate_regime_router(self.candles(), "dynamic_regime")
        self.assertEqual(uptrend["status"], "dynamic_testnet")
        self.assertEqual(uptrend["selected_strategy"], "macro_donchian_ema72")
        self.assertTrue(uptrend["entry_route_enabled"])

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
        self.assertEqual(short["reason_code"], "needs_2501_closed_hourly_candles_received_100")
        self.assertEqual(short["reason"], "Need 2,501 completed hourly candles; received 100.")
        self.assertNotIn("_", short["reason"])
        gapped = self.candles()
        gapped[100][0] += 3_600_000
        result = evaluate_regime_router(gapped, "macro_donchian_ema72")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["route"], "no_validated_strategy")


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
        self.assertEqual(strategy.timeframe, "1h")
        self.assertEqual(strategy.lookback_candles, 150)
        self.assertEqual(strategy.trading_pairs, ["BTC/USDT", "ETH/USDT", "SOL/USDT"])
        self.assertEqual(strategy.min_trend_adx, 20.0)

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
        cfg.strategy.lookback_candles = 2501
        structural_errors = [e for e in cfg.validate() if "API_KEY" not in e and "API_SECRET" not in e]
        self.assertEqual(structural_errors, [])

        cfg.strategy.lookback_candles = 2500
        self.assertTrue(any("LOOKBACK_CANDLES >= 2501" in e for e in cfg.validate()))
        cfg.strategy.lookback_candles = 2501
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
        cfg.strategy.lookback_candles = 2501
        structural_errors = [e for e in cfg.validate() if "API_KEY" not in e and "API_SECRET" not in e]
        self.assertEqual(structural_errors, [])

        cfg.strategy.lookback_candles = 2500
        self.assertTrue(any("LOOKBACK_CANDLES >= 2501" in e for e in cfg.validate()))
        cfg.strategy.lookback_candles = 2501
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

    def test_rolling_equity_drawdown_halts_entries(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(max_daily_loss_pct=10.0, max_rolling_drawdown_pct=5.0), persistence_dir=persistence_dir)
            self.assertTrue(rm.monitor_equity(1000.0))
            self.assertFalse(rm.monitor_equity(940.0))
            self.assertIn("rolling", rm._halt_reason.lower())

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

    def test_order_transitions_enforcement(self):
        with TemporaryDirectory() as tmpdir:
            store = StateStore(Path(tmpdir) / "state.db")
            store.create_order_intent("client_1", "BTC/USDT", "BUY", 0.1, {"type": "market"})
            store.update_order("client_1", "SUBMITTED")
            store.update_order("client_1", "OPEN")
            store.update_order("client_1", "FILLED")
            
            with self.assertRaises(ValueError):
                store.update_order("client_1", "SUBMITTED")

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
            self.assertEqual(store.read_positions(), {})
            self.assertEqual(store.unresolved_orders(), [])
            self.assertIsNone(store.read_state("baseline_asset_balances"))

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
        start = (now_ms // 60_000) * 60_000 - 180_000
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

class TestBotEngineOrderLifecycle(unittest.TestCase):
    """Verifies complete order execution, fee deduction, stop loss synchronization, recovery, and alerts."""

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

    def test_manual_review_cannot_clear_other_risk_halts(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            rm.halt("Daily equity drawdown limit reached: 5.1%")
            self.assertFalse(rm.acknowledge_manual_halt_review())
            self.assertTrue(rm._is_halted)
            rm.halt("Rolling peak-to-trough drawdown limit reached: 5.2%")
            self.assertFalse(rm.acknowledge_manual_halt_review())
            self.assertTrue(rm._is_halted)

    def test_f02_confirmed_buy_places_stop_even_if_position_persistence_fails(self):
        """F-02: Confirm native stop loss is placed on exchange even if local DB write fails."""
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = BotConfig(dashboard_token="test_token")
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

    def test_f03_missing_fee_data_still_protects_and_records_position(self):
        """F-03: Missing fee/fill array must not abandon acquired coins unprotected."""
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = BotConfig(dashboard_token="test_token")
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
            bot._submit_market_order = Mock(return_value=(buy_order, "cb_buy_client_2"))
            bot.exchange.exchange.fetch_order = Mock(return_value=buy_order)
            bot.send_notification = Mock()
            bot._log_trade = Mock()

            signal = TradeSignal(Signal.BUY, "BTC/USDT", 0.8, "Test fee-less buy", indicators={"atr": 500.0})
            bot._handle_buy_signal(signal)

            self.assertIn("BTC/USDT", bot.risk_manager.open_positions)
            self.assertTrue(bot.exchange.place_native_stop_loss.called)
            self.assertTrue(bot.risk_manager._is_halted)

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
            mock_smtp.return_value = server_instance

            bot = TradingBot(config)
            bot.send_notification("Test email body", subject="Test Subject")

            mock_smtp.assert_called_with("smtp.example.com", 587, timeout=10)
            server_instance.starttls.assert_called_once()
            server_instance.login.assert_called_with("alerts@example.com", "secretpassword")
            server_instance.sendmail.assert_called_once()
            args = server_instance.sendmail.call_args[0]
            self.assertEqual(args[0], "alerts@example.com")
            self.assertEqual(args[1], ["trader@example.com"])
            self.assertIn("Test Subject", args[2])

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

        req = Mock()
        resp = asyncio.run(dash.handle_liveness(req))
        self.assertEqual(resp.status, 200)

        dash.bot._last_reconciliation_ok = True
        dash.bot.risk_manager._is_halted = False
        dash.bot.risk_manager.entry_mode.return_value = "ACTIVE"
        dash.bot.exchange.is_fallback_simulated = False
        resp_ready = asyncio.run(dash.handle_readiness(req))
        self.assertEqual(resp_ready.status, 200)

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

    def test_regime_router_panel_explains_dynamic_routing_and_fallback(self):
        strategy_page = (Path("templates") / "strategy.html").read_text(encoding="utf-8")
        self.assertIn("Dynamic strategy selection · Paper", strategy_page)
        self.assertIn("Other conditions stay flat because the tested alternatives have not passed cost-adjusted review.", strategy_page)
        self.assertIn("three completed hourly candles", strategy_page)
        self.assertIn("No entry strategy · stand aside", strategy_page)

    def test_machine_labels_are_formatted_for_dashboard_readers(self):
        strategy_page = (Path("templates") / "strategy.html").read_text(encoding="utf-8")
        logs_page = (Path("templates") / "logs.html").read_text(encoding="utf-8")
        overview_page = (Path("templates") / "overview.html").read_text(encoding="utf-8")
        self.assertIn("macro_donchian_ema72:'Donchian breakout · EMA 72'", strategy_page)
        self.assertIn("replaceAll('_',' ')", strategy_page)
        self.assertIn("replaceAll('_',' ')", logs_page)
        self.assertIn("readableReason", overview_page)
        self.assertIn("needs_(\\d+)_closed_hourly_candles_received_(\\d+)", logs_page)
        self.assertIn("Donchian breakout · EMA 72", strategy_page)
        self.assertIn("paper_environment_changed:'Paper account changed'", logs_page)
        self.assertIn("Enter your private dashboard access token", LOGIN_HTML)
        self.assertNotIn("DASHBOARD_TOKEN", LOGIN_HTML)

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
        self.assertIn("Gross gains ÷ gross losses", history)
        self.assertIn("Pending reconciliation", history)
        self.assertIn("incomplete fee or partial-fill accounting", history)

    def test_glossary_definitions_completeness(self):
        glossary = (Path("templates") / "glossary.html").read_text(encoding="utf-8")
        expected_terms = [
            "Cash balance",
            "Account value / net worth",
            "Profit and loss (P&amp;L)",
            "Realized and unrealized P&amp;L",
            "Stop-loss",
            "Take-profit",
            "Fees and slippage",
            "Backtest",
            "Paper trading / testnet"
        ]
        for term in expected_terms:
            self.assertIn(term, glossary)

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


if __name__ == "__main__":
    unittest.main()
