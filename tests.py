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
from exchange import ExchangeConnector, filter_closed_ohlcv
from bot_engine import TradingBot


class TestTradingBot(unittest.TestCase):

    def setUp(self):
        # Generate 150 synthetic candles
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


    def test_closed_candle_filter_drops_incomplete_and_malformed_rows(self):
        now = 1_800_000_000_000
        candles = [
            [now - 60_000, 100, 102, 99, 101, 10],  # completed 1m candle
            [now, 101, 103, 100, 102, 11],          # current, incomplete candle
            [now - 120_000, 100, 99, 98, 100, 5],   # invalid high/low structure
            [now - 180_000, 100, float("nan"), 99, 101, 4],
        ]
        filtered = filter_closed_ohlcv(candles, 60, now_ms=now + 3000)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0][0], now - 60_000)

    def test_only_confirmed_complete_fills_are_accepted(self):
        self.assertTrue(TradingBot._confirmed_full_fill(
            {"status": "closed", "filled": 1.0}, 1.0
        ))
        self.assertFalse(TradingBot._confirmed_full_fill(
            {"status": "open", "filled": 1.0}, 1.0
        ))
        self.assertFalse(TradingBot._confirmed_full_fill(
            {"status": "closed", "filled": 0.5}, 1.0
        ))
        self.assertFalse(TradingBot._confirmed_full_fill(
            {"status": "closed", "filled": None}, 1.0
        ))

    def test_base_asset_fee_is_removed_from_tracked_fill_quantity(self):
        net = TradingBot._net_base_filled({
            "filled": "1.000", "fees": [{"currency": "BTC", "cost": "0.001"}],
        }, "BTC/USDT")
        self.assertEqual(str(net), "0.999")
        self.assertIsNone(TradingBot._net_base_filled({"filled": "1.0"}, "BTC/USDT"))

    def test_client_order_id_is_deterministic_and_exchange_sized(self):
        first = TradingBot._client_order_id("BTC/USDT", "buy", "candle-123")
        second = TradingBot._client_order_id("BTC/USDT", "buy", "candle-123")
        different = TradingBot._client_order_id("BTC/USDT", "buy", "candle-124")
        self.assertEqual(first, second)
        self.assertNotEqual(first, different)
        self.assertLessEqual(len(first), 36)

    def test_rolling_equity_drawdown_halts_entries(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(max_daily_loss_pct=10.0, max_rolling_drawdown_pct=5.0), persistence_dir=persistence_dir)
            self.assertTrue(rm.monitor_equity(1000.0))
            self.assertFalse(rm.monitor_equity(940.0))
            self.assertIn("rolling", rm._halt_reason.lower())

    def test_consecutive_losses_latch_the_risk_manager(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(max_daily_loss_pct=5.0, max_consecutive_losses=2), persistence_dir=persistence_dir)
            rm.open_position("BTC/USDT", 100.0, 1.0)
            rm.close_position("BTC/USDT", 99.0, "test loss")
            rm.open_position("ETH/USDT", 100.0, 1.0)
            rm.close_position("ETH/USDT", 99.0, "test loss")
            self.assertTrue(rm._is_halted)
            self.assertEqual(rm._consecutive_losses, 2)

    def test_seeded_testnet_inventory_is_baselined_not_mistaken_for_a_position(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = SimpleNamespace(strategy=SimpleNamespace(trading_pairs=["BTC/USDT"]))
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

    def test_closed_candle_return_correlation_is_aligned_by_timestamp(self):
        candles_a, candles_b = [], []
        for i in range(40):
            price_a = 100 + i * 2 + (i % 3)
            price_b = 50 + i * 1 + ((i % 3) / 2)
            candles_a.append([i + 1, price_a, price_a, price_a, price_a, 1])
            candles_b.append([i + 1, price_b, price_b, price_b, price_b, 1])
        self.assertGreater(TradingBot._return_correlation(candles_a, candles_b), 0.9)

    def test_non_finite_risk_configuration_is_rejected(self):
        from config import BotConfig, StrategyConfig
        config = BotConfig(
            exchange=ExchangeConfig(api_key="key", api_secret="secret"),
            risk=RiskConfig(max_risk_per_trade=float("nan")),
            strategy=StrategyConfig(),
            dashboard_token="test-token",
        )
        self.assertTrue(any("MAX_RISK_PER_TRADE must be finite" in e for e in config.validate()))

    def test_position_persistence_failure_halts_new_entries(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            with patch.object(rm.store, "write_positions", side_effect=OSError("disk unavailable")):
                rm.open_position("BTC/USDT", 100.0, 0.1)
            self.assertTrue(rm._is_halted)
            self.assertIn("could not be saved", rm._halt_reason)

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
        import sqlite3
        from state_store import StateStore
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

    def test_exchange_reconciliation_error_is_not_treated_as_empty_balance(self):
        from unittest.mock import Mock
        import ccxt
        connector = object.__new__(ExchangeConnector)
        connector.exchange = Mock()
        connector.exchange.fetch_balance.side_effect = ccxt.AuthenticationError("secret omitted")
        with self.assertRaisesRegex(RuntimeError, "Testnet authentication"):
            connector.fetch_account_snapshot()

    def test_account_authentication_failure_is_actionable_without_echoing_secrets(self):
        import ccxt
        from exchange import AccountAccessError
        from unittest.mock import Mock
        connector = object.__new__(ExchangeConnector)
        connector.exchange = Mock()
        connector.exchange.fetch_balance.side_effect = ccxt.AuthenticationError("private-value-must-not-appear")

        with self.assertRaises(AccountAccessError) as raised:
            connector.fetch_balance_snapshot()

        self.assertIn("Spot Testnet", str(raised.exception))
        self.assertNotIn("private-value-must-not-appear", str(raised.exception))

        from exchange import account_access_message
        self.assertIn("API_SECRET does not match", account_access_message(ccxt.ExchangeError('{"code":-1022,"msg":"signature invalid"}')))
        self.assertIn("API key, IP, or permissions", account_access_message(ccxt.ExchangeError('{"code":-2015,"msg":"key rejected"}')))

    def test_resume_entries_requires_clean_reconciliation(self):
        import threading
        from unittest.mock import Mock
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

    def test_native_stop_is_submitted_as_market_stop_with_persistable_client_id(self):
        from unittest.mock import Mock
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
        from types import SimpleNamespace
        from unittest.mock import Mock
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


    def test_missing_open_position_price_halts_new_trading(self):
        with TemporaryDirectory() as persistence_dir:
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            rm.open_position("BTC/USDT", 100.0, 0.1)
            rm.check_positions(lambda symbol: 0.0)
            can_open, reason = rm.can_open_position("ETH/USDT", 10_000.0)
            self.assertFalse(can_open)
            self.assertTrue(rm._is_halted)
            self.assertIn("market price", reason.lower())

    def test_invalid_persisted_daily_loss_halts_startup(self):
        with TemporaryDirectory() as persistence_dir:
            today = datetime.now(timezone.utc).date().isoformat()
            Path(persistence_dir, "daily_risk_state.json").write_text(
                '{"date": "' + today + '", "daily_loss": NaN, "is_halted": false}',
                encoding="utf-8",
            )
            rm = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            self.assertTrue(rm._is_halted)

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
