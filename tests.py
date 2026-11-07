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
from config import BotConfig, ExchangeConfig, RiskConfig
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
        from exchange import AccountAccessError, account_access_message, extract_ccxt_response_metadata
        from unittest.mock import Mock

        # 1. CCXT response metadata extraction test
        mock_ex = Mock()
        mock_ex.last_json_response = {"code": -2014, "msg": "API-key format invalid."}
        mock_ex.last_http_response = '{"code": -2014, "msg": "API-key format invalid."}'
        mock_ex.last_http_status = 400
        body, status = extract_ccxt_response_metadata(mock_ex)
        self.assertEqual(body, {"code": -2014, "msg": "API-key format invalid."})
        self.assertEqual(status, 400)

        # 2. Live snapshot test asserting secrets do not leak
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

        # 3. Binance -2014 (API-key format invalid)
        msg_2014 = account_access_message(ccxt.ExchangeError('{"code":-2014,"msg":"API-key format invalid."}'))
        self.assertIn("-2014", msg_2014)
        self.assertIn("API key format is invalid", msg_2014)

        # 4. Binance -2015 (API key, IP, or permissions)
        msg_2015_dict = account_access_message(ccxt.ExchangeError("Authentication error"), response_body={"code": -2015, "msg": "key rejected"})
        self.assertIn("-2015", msg_2015_dict)
        self.assertIn("API key, IP, or permissions", msg_2015_dict)

        msg_2015_text = account_access_message(ccxt.ExchangeError("Invalid API-key, IP, or permissions for action."))
        self.assertIn("-2015", msg_2015_text)

        # 5. Binance -1022 (Signature invalid)
        msg_1022 = account_access_message(ccxt.ExchangeError('{"code":-1022,"msg":"signature for this request is not valid"}'))
        self.assertIn("-1022", msg_1022)
        self.assertIn("API_SECRET does not match", msg_1022)

        # 6. Binance -1021 (Timestamp outside window)
        msg_1021 = account_access_message(ccxt.ExchangeError('{"code":-1021,"msg":"Timestamp for this request is outside of the recvWindow."}'))
        self.assertIn("-1021", msg_1021)
        self.assertIn("server clock synchronization", msg_1021)

        # 7. HTTP 401 & HTTP 403
        msg_401 = account_access_message(ccxt.AuthenticationError("HTTP 401 Unauthorized"), http_status=401)
        self.assertIn("HTTP 401", msg_401)
        self.assertIn("Spot Testnet", msg_401)

        msg_403 = account_access_message(ccxt.PermissionDenied("HTTP 403 Forbidden"), http_status=403)
        self.assertIn("HTTP 403", msg_403)
        self.assertIn("denied account access", msg_403)

        # 8. Malformed / Non-JSON response (e.g. HTML 502 error)
        msg_html = account_access_message(ccxt.ExchangeError("502 Bad Gateway"), response_body="<html><title>502 Bad Gateway</title></html>", http_status=502)
        self.assertIn("HTTP 502", msg_html)
        self.assertNotIn("<html>", msg_html)
        self.assertNotIn("<title>", msg_html)

        # 9. Unrelated network failure
        msg_net = account_access_message(ccxt.NetworkError("Failed to connect to host"))
        self.assertIn("could not be reached", msg_net)

        # 10. Redaction: signed URL, signatures, and query parameters must never leak
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
        import ccxt
        from unittest.mock import Mock
        from exchange import ExchangeConfig, ExchangeConnector

        # Case 1: Simulated wallet returns SIMULATED_WALLET
        config_sim = ExchangeConfig(paper_trading=True, paper_environment="simulated")
        conn_sim = ExchangeConnector(config_sim)
        diag_sim = conn_sim.diagnose_account_access()
        self.assertEqual(diag_sim["overall_status"], "SIMULATED_WALLET")
        self.assertTrue(diag_sim["balance_ok"])
        self.assertTrue(diag_sim["orders_ok"])

        # Case 2: Balance auth failure
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

        # Case 3: Balance ok, but open-order permission missing
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

        # Case 4: Both balance and orders succeed
        conn.exchange.fetch_open_orders.side_effect = None
        conn.exchange.fetch_open_orders.return_value = []
        diag_ok = conn.diagnose_account_access()
        self.assertEqual(diag_ok["overall_status"], "AUTHENTICATED_AND_RECONCILED")
        self.assertTrue(diag_ok["balance_ok"])
        self.assertTrue(diag_ok["orders_ok"])

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

            bot.risk_manager.set_exit_only()
            bot.risk_manager.halt("Emergency stop could not verify/cancel exchange orders; operator action required")
            success, _ = bot.resume_entries()
            self.assertFalse(success, "Emergency-stop halts must require explicit operator review")
            success, message = bot.resume_entries(acknowledge_manual_halt=True)
            self.assertTrue(success)
            self.assertEqual(bot.risk_manager.entry_mode(), "ACTIVE")
            self.assertFalse(bot.risk_manager._is_halted)

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
        import ccxt
        from unittest.mock import Mock
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

    def test_telegram_notification_dispatch(self):
        from unittest.mock import patch, MagicMock
        from bot_engine import TradingBot
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

    def test_email_notification_dispatch(self):
        from unittest.mock import patch, MagicMock
        from bot_engine import TradingBot
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

    def test_discord_notification_dispatch(self):
        import json
        from unittest.mock import patch
        from bot_engine import TradingBot
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

    def test_notification_disabled(self):
        from unittest.mock import patch
        from bot_engine import TradingBot
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

    def test_f02_confirmed_buy_places_stop_even_if_position_persistence_fails(self):
        """F-02: Confirm native stop loss is placed on exchange even if local DB write fails."""
        from unittest.mock import Mock, patch
        from bot_engine import TradingBot
        from strategies import TradeSignal, Signal

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

            # Simulate buy fill on Binance
            buy_order = {
                "id": "buy_f02_123",
                "status": "closed",
                "filled": "0.01",
                "average": "50000.0",
                "fees": [{"currency": "USDT", "cost": "0.5"}],
            }
            bot._submit_market_order = Mock(return_value=(buy_order, "cb_buy_client_1"))
            # Fault injection: simulate database position save failure
            with patch.object(bot.risk_manager, "_save_positions", side_effect=RuntimeError("disk full")):
                # When _save_positions fails, open_position sets halted
                # Now run buy signal
                signal = TradeSignal(Signal.BUY, "BTC/USDT", 0.8, "Test buy", indicators={"atr": 500.0})
                bot._handle_buy_signal(signal)

            # Assert risk manager halted due to DB error
            self.assertTrue(bot.risk_manager._is_halted)
            # CRITICAL F-02 ASSERTION: Exchange stop loss WAS STILL PLACED on Binance!
            self.assertTrue(bot.exchange.place_native_stop_loss.called)
            call_args = bot.exchange.place_native_stop_loss.call_args[0]
            self.assertEqual(call_args[0], "BTC/USDT")
            self.assertEqual(call_args[1], 0.01)  # Amount protected

    def test_f03_missing_fee_data_still_protects_and_records_position(self):
        """F-03: Missing fee/fill array must not abandon acquired coins unprotected."""
        from unittest.mock import Mock
        from bot_engine import TradingBot
        from strategies import TradeSignal, Signal

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

            # Fill response with NO fee array, NO fee dict, and NO info.fills
            buy_order = {
                "id": "buy_f03_456",
                "status": "closed",
                "filled": "0.01",
                "average": "50000.0",
                # Omitted fees!
            }
            bot._submit_market_order = Mock(return_value=(buy_order, "cb_buy_client_2"))
            bot.exchange.exchange.fetch_order = Mock(return_value=buy_order)
            bot.send_notification = Mock()
            bot._log_trade = Mock()

            signal = TradeSignal(Signal.BUY, "BTC/USDT", 0.8, "Test fee-less buy", indicators={"atr": 500.0})
            bot._handle_buy_signal(signal)

            # Assert position was recorded and NOT abandoned
            self.assertIn("BTC/USDT", bot.risk_manager.open_positions)
            # Assert exchange stop loss was placed
            self.assertTrue(bot.exchange.place_native_stop_loss.called)
            # Assert halted for operator verification
            self.assertTrue(bot.risk_manager._is_halted)

    def test_f04_exit_failure_restores_protective_stop(self):
        """F-04: If market sell fails after stop cancellation, protective stop must be restored."""
        from unittest.mock import Mock
        from bot_engine import TradingBot
        from strategies import TradeSignal, Signal

        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = BotConfig(dashboard_token="test_token")
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.get_current_price.return_value = 52000.0

            # Position with open stop
            pos = bot.risk_manager.open_position("BTC/USDT", 50000.0, 0.01, order_id="buy_1")
            pos.stop_order_id = "initial_stop_id"
            bot.risk_manager._save_positions()

            bot.exchange.cancel_order.return_value = True
            # Simulate market sell exception (e.g. network disconnect during sell)
            bot._submit_market_order = Mock(side_effect=RuntimeError("Network failure on sell"))

            signal = TradeSignal(Signal.SELL, "BTC/USDT", 0.9, "Take profit signal", indicators={})
            bot._handle_sell_signal(signal)

            # Assert market sell failed and trading halted
            self.assertTrue(bot.risk_manager._is_halted)
            # CRITICAL F-04 ASSERTION: Protective stop was RESTORED on exchange!
            self.assertTrue(bot.exchange.place_native_stop_loss.called)
            call_args = bot.exchange.place_native_stop_loss.call_args[0]
            self.assertEqual(call_args[0], "BTC/USDT")
            self.assertEqual(call_args[1], 0.01)

    def test_f05_sync_exchange_stop_on_breakeven_or_trailing(self):
        """F-05: Tighter break-even or trailing stops synchronize to exchange."""
        from unittest.mock import Mock
        from bot_engine import TradingBot

        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = BotConfig(dashboard_token="test_token")
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot.exchange = Mock()
            bot.exchange.cancel_order.return_value = True
            bot.exchange.place_native_stop_loss.return_value = {"id": "new_tighter_stop_id", "status": "open"}

            # Position entered at 50,000, initial SL at 49,000
            pos = bot.risk_manager.open_position("BTC/USDT", 50000.0, 0.01, order_id="buy_1")
            pos.stop_order_id = "old_stop_id"
            pos.stop_loss_price = 50000.0  # Tightened to entry (break-even)
            bot.risk_manager._save_positions()

            synced = bot._sync_exchange_stop("BTC/USDT")
            self.assertTrue(synced)
            # Old stop cancelled
            bot.exchange.cancel_order.assert_called_with("old_stop_id", "BTC/USDT")
            # New stop placed at 50,000
            bot.exchange.place_native_stop_loss.assert_called_with("BTC/USDT", 0.01, 50000.0, pos.stop_client_order_id)
            self.assertEqual(pos.stop_order_id, "new_tighter_stop_id")

    def test_f13_emergency_stop_cancels_only_bot_orders(self):
        """F-13: Emergency stop cancels bot-owned orders and leaves user orders untouched."""
        from unittest.mock import Mock
        import threading
        from bot_engine import TradingBot

        with TemporaryDirectory() as persistence_dir:
            bot = object.__new__(TradingBot)
            bot.config = BotConfig(dashboard_token="test_token")
            bot.risk_manager = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            bot._cycle_lock = threading.Lock()
            bot.exchange = Mock()

            # Mock exchange open orders: 1 bot order, 1 manual user order
            bot_order = {"id": "111", "symbol": "BTC/USDT", "clientOrderId": "cb_bot_stop_123"}
            manual_order = {"id": "222", "symbol": "BTC/USDT", "clientOrderId": "web_manual_limit_order"}
            bot.exchange.fetch_account_snapshot.return_value = ({"total": {}}, [bot_order, manual_order])
            bot.exchange.cancel_order.return_value = True

            success, msg = bot.emergency_stop()
            self.assertTrue(success)
            # Only bot order cancelled
            bot.exchange.cancel_order.assert_called_once_with("111", "BTC/USDT")
            # Manual order untouched message
            self.assertIn("1 external/manual order(s) were left untouched", msg)

    def test_f06_exchange_locking_is_present(self):
        """F-06: ExchangeConnector must serialize CCXT calls through an internal lock."""
        from exchange import ExchangeConnector
        cfg = ExchangeConfig(name="binance", paper_trading=True, paper_environment="simulated")
        conn = ExchangeConnector(cfg)
        self.assertTrue(hasattr(conn, "_exchange_lock"))
        # Lock is reentrant
        with conn._exchange_lock:
            with conn._exchange_lock:
                pass

    def test_f07_ticker_failure_returns_none_not_zero(self):
        """F-07: Ticker failure or invalid price must return None, never numeric $0.00."""
        from unittest.mock import Mock
        from exchange import ExchangeConnector
        import ccxt

        cfg = ExchangeConfig(name="binance", paper_trading=True, paper_environment="simulated")
        conn = ExchangeConnector(cfg)
        conn.exchange = Mock()

        # 1. CCXT error on fetch_ticker
        conn.exchange.fetch_ticker.side_effect = ccxt.NetworkError("Exchange timeout")
        self.assertIsNone(conn.get_ticker("BTC/USDT"))
        self.assertIsNone(conn.get_current_price("BTC/USDT"))

        # 2. Malformed last price (None, 0.0, negative, NaN)
        conn.exchange.fetch_ticker.side_effect = None
        conn.exchange.fetch_ticker.return_value = {"last": None}
        self.assertIsNone(conn.get_current_price("BTC/USDT"))

        conn.exchange.fetch_ticker.return_value = {"last": 0.0}
        self.assertIsNone(conn.get_current_price("BTC/USDT"))

        conn.exchange.fetch_ticker.return_value = {"last": -50.0}
        self.assertIsNone(conn.get_current_price("BTC/USDT"))

        conn.exchange.fetch_ticker.return_value = {"last": float("nan")}
        self.assertIsNone(conn.get_current_price("BTC/USDT"))

        # 3. Valid price
        conn.exchange.fetch_ticker.return_value = {"last": 52100.50}
        self.assertEqual(conn.get_current_price("BTC/USDT"), 52100.50)

    def test_f16_healthcheck_handlers(self):
        """F-16: Liveness and readiness probes respond with clean status without secrets."""
        import asyncio
        from unittest.mock import Mock
        from dashboard import WebDashboard

        dash = object.__new__(WebDashboard)
        dash.bot = Mock()
        dash.bot.risk_manager = Mock()
        dash.bot.exchange = Mock()

        # Liveness test
        req = Mock()
        resp = asyncio.run(dash.handle_liveness(req))
        self.assertEqual(resp.status, 200)

        # Readiness test: when ready
        dash.bot._last_reconciliation_ok = True
        dash.bot.risk_manager._is_halted = False
        dash.bot.risk_manager.entry_mode.return_value = "ACTIVE"
        dash.bot.exchange.is_fallback_simulated = False
        resp_ready = asyncio.run(dash.handle_readiness(req))
        self.assertEqual(resp_ready.status, 200)

        # Readiness test: when halted -> 503
        dash.bot.risk_manager._is_halted = True
        resp_halted = asyncio.run(dash.handle_readiness(req))
        self.assertEqual(resp_halted.status, 503)

    def test_f17_sqlite_schema_version_and_online_backup(self):
        """F-17 & F-09: SQLite schema versioning, online backup, and Testnet reset recovery."""
        from state_store import StateStore

        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            store = StateStore(db_path)

            # Assert schema version
            self.assertEqual(store.get_schema_version(), 1)

            # Write sample state and position
            store.write_state("baseline_asset_balances", {"BTC": "0.1"})
            store.write_positions({"BTC/USDT": {"amount": 0.05, "entry_price": 50000.0}})
            store.create_order_intent("client_123", "BTC/USDT", "BUY", 0.05, {"type": "market"})

            # Create online backup
            backup_file = store.create_backup()
            self.assertTrue(backup_file.exists())
            # Verify backup has data
            backup_store = StateStore(backup_file)
            self.assertIn("BTC/USDT", backup_store.read_positions())

            # Perform Testnet reset recovery
            reset_data = store.snapshot_and_reset_for_testnet_wipe("Bi-weekly Spot Testnet reset")
            self.assertIn("backup_file", reset_data)
            self.assertEqual(reset_data["operator_reason"], "Bi-weekly Spot Testnet reset")

            # Check positions were safely reset and orders canceled
            self.assertEqual(store.read_positions(), {})
            self.assertEqual(store.unresolved_orders(), [])
            self.assertIsNone(store.read_state("baseline_asset_balances"))

    def test_f11_canonical_fee_netting_in_close_position(self):
        """F-11: Close position must net fees against gross PnL."""
        with TemporaryDirectory() as persistence_dir:
            risk = RiskManager(RiskConfig(), persistence_dir=persistence_dir)
            # Open position: 0.1 BTC @ $50,000 = $5,000 cost basis
            risk.open_position("BTC/USDT", 50000.0, 0.1, order_id="buy_1")

            # Exit at $50,500 (+ $50 gross gain), with $10 in trading fees
            record = risk.close_position("BTC/USDT", 50500.0, "Take profit", fee_cost=10.0)
            self.assertIsNotNone(record)
            # Net PnL must be $50 - $10 = $40
            self.assertAlmostEqual(record.pnl, 40.0)
            self.assertEqual(record.fee, 10.0)
            # Return % is 40 / 5000 = 0.8%
            self.assertAlmostEqual(record.pnl_pct, 0.8)

    def test_f12_start_paused_policy(self):
        """F-12: START_PAUSED=true policy defaults to EXIT_ONLY mode."""
        with TemporaryDirectory() as persistence_dir:
            config = RiskConfig(start_paused=True)
            risk = RiskManager(config, persistence_dir=persistence_dir)

            # Assert default entry mode is EXIT_ONLY
            self.assertEqual(risk.entry_mode(), "EXIT_ONLY")
            can_open, reason = risk.can_open_position("BTC/USDT", 10000.0)
            self.assertFalse(can_open)
            self.assertIn("Entry-only pause is active", reason)

    def test_f21_trade_signal_heuristic_score_formatting(self):
        """F-21: TradeSignal string must present confidence as a heuristic score."""
        from strategies import TradeSignal, Signal
        sig = TradeSignal(Signal.BUY, "BTC/USDT", 0.75, "RSI bounce", {})
        sig_str = str(sig)
        self.assertIn("score: 75%", sig_str)


    def test_mobile_overflow_protection_in_css(self):
        """Verify CSS contains mobile overflow prevention rules and responsive layouts."""
        css_path = Path(__file__).parent / "templates" / "app.css"
        self.assertTrue(css_path.exists(), "templates/app.css must exist")
        css_text = css_path.read_text(encoding="utf-8")

        # Root/body overflow clipping to prevent horizontal page wobble
        self.assertIn("overflow-x: clip", css_text)

        # Responsive media queries present
        self.assertIn("@media (max-width: 700px)", css_text)
        self.assertIn("@media (max-width: 480px)", css_text)

        # Metrics grid must adapt to fewer columns on small screens
        self.assertIn(".overview-metrics", css_text)

        # Chart price row must allow wrapping
        self.assertIn(".chart-price-row", css_text)
        self.assertIn("flex-wrap: wrap", css_text)

        # Table wrap must support smooth touch scrolling without breaking page width
        self.assertIn(".table-wrap", css_text)
        self.assertIn("-webkit-overflow-scrolling: touch", css_text)

        # Controls actions should be responsive
        self.assertIn(".trading-controls-actions", css_text)

    def test_template_html_mobile_meta_and_navigation_integrity(self):
        """Verify all templates have responsive viewport meta tags and balanced navigation."""
        templates_dir = Path(__file__).parent / "templates"
        template_files = ["overview.html", "positions.html", "history.html", "strategy.html", "glossary.html"]

        for filename in template_files:
            file_path = templates_dir / filename
            self.assertTrue(file_path.exists(), f"{filename} must exist")
            html = file_path.read_text(encoding="utf-8")

            # Must have mobile viewport meta tag
            self.assertIn('name="viewport"', html, f"{filename} missing viewport meta tag")
            self.assertIn('content="width=device-width', html, f"{filename} missing width=device-width")

            # Nav integrity: brand, nav-links, and theme-toggle must all be present
            self.assertIn('class="brand"', html, f"{filename} missing brand class")
            self.assertIn('class="nav-links"', html, f"{filename} missing nav-links class")
            self.assertIn('data-theme-toggle', html, f"{filename} missing data-theme-toggle")

            # Ensure theme toggle button is NOT nested inside nav-links
            nav_links_start = html.find('class="nav-links"')
            nav_links_close = html.find('</div>', nav_links_start)
            theme_toggle_pos = html.find('data-theme-toggle')
            self.assertTrue(
                theme_toggle_pos > nav_links_close,
                f"{filename}: theme-toggle must be outside nav-links"
            )

            # Ensure no duplicate </main> tags
            self.assertEqual(html.count('</main>'), 1, f"{filename} must have exactly one </main> tag")


if __name__ == "__main__":
    unittest.main()
