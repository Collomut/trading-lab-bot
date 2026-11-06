"""
Self-contained unit and integration sanity tests for the trading bot.
Run with: python tests.py
"""

import unittest
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from strategies import (
    Signal,
    EMA_Crossover_Strategy,
    RSI_Mean_Reversion_Strategy,
    MACD_Strategy,
    CombinedStrategy,
    ohlcv_to_dataframe
)
from risk_manager import RiskManager, Position
from config import ExchangeConfig, RiskConfig
from exchange import ExchangeConnector


class TestTradingBot(unittest.TestCase):

    def setUp(self):
        # Generate 150 synthetic candles
        dates = pd.date_range("2026-01-01", periods=150, freq="15min")
        prices = [100.0]
        for _ in range(149):
            change = np.random.normal(0, 0.5)
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

    def test_rsi_strategy_runs(self):
        strat = RSI_Mean_Reversion_Strategy(14)
        sig = strat.analyze(self.df.copy(), "BTC/USDT")
        self.assertIn(sig.signal, [Signal.BUY, Signal.SELL, Signal.HOLD])

    def test_macd_strategy_runs(self):
        strat = MACD_Strategy(12, 26, 9)
        sig = strat.analyze(self.df.copy(), "BTC/USDT")
        self.assertIn(sig.signal, [Signal.BUY, Signal.SELL, Signal.HOLD])

    def test_combined_strategy_consensus(self):
        strat = CombinedStrategy(use_trend_filter=False)
        sig = strat.analyze(self.df.copy(), "BTC/USDT")
        self.assertIn(sig.signal, [Signal.BUY, Signal.SELL, Signal.HOLD])

    def test_risk_manager_position_sizing(self):
        config = RiskConfig(
            max_risk_per_trade=1.0,
            max_open_positions=3,
            stop_loss_pct=2.0,
            take_profit_pct=3.0,
            max_daily_loss_pct=3.0,
            position_size_pct=10.0,
        )
        rm = RiskManager(config, persistence_dir="data/test_data")
        portfolio = 10000.0
        entry_price = 100.0
        sl_price = 98.0  # 2% stop-loss

        # Position size capped at 10% ($1,000) -> 10 units
        size = rm.calculate_position_size(portfolio, entry_price, sl_price)
        self.assertAlmostEqual(size, 10.0)

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

        self.assertEqual(binance_factory.call_args.args[0]["apiKey"], "demo-key")
        self.assertEqual(binance_factory.call_args.args[0]["secret"], "demo-secret")
        exchange = binance_factory.return_value
        exchange.enable_demo_trading.assert_called_once_with(True)
        exchange.set_sandbox_mode.assert_not_called()

    def test_corrupt_position_state_halts_new_entries(self):
        with TemporaryDirectory() as persistence_dir:
            state_path = Path(persistence_dir) / "open_positions.json"
            state_path.write_text("{broken", encoding="utf-8")

            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            can_open, reason = rm.can_open_position("BTC/USDT", 10_000.0)

            self.assertFalse(can_open)
            self.assertTrue(rm._is_halted)
            self.assertIn("unreadable", reason.lower())

    def test_risk_manager_break_even_and_trailing_stop(self):
        config = RiskConfig(stop_loss_pct=2.0, take_profit_pct=3.0)
        rm = RiskManager(config, persistence_dir="data/test_data")

        # Open at $100 -> SL is $98, TP is $103
        pos = rm.open_position("BTC/USDT", 100.0, 1.0)
        self.assertEqual(pos.stop_loss_price, 98.0)

        # Price climbs to $101.8 (+1.8% -> triggers break-even!)
        rm.check_positions(lambda s: 101.8)
        self.assertTrue(rm.open_positions["BTC/USDT"].break_even_activated)
        self.assertGreaterEqual(rm.open_positions["BTC/USDT"].stop_loss_price, 100.0)


if __name__ == "__main__":
    unittest.main()
