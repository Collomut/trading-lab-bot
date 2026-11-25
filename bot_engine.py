Warning: truncated output (original token count: 62460)
Total output lines: 4118

"""
Trading Bot Engine — the main loop that ties everything together.

This is the orchestrator that:
  1. Fetches real-time market data
  2. Runs strategy analysis with a fixed 50 EMA trend & volume filter
  3. Checks risk management rules (with ATR & break-even protection)
  4. Executes trades when conditions are met
  5. Monitors open positions for trailing stop-loss / take-profit
  6. Dispatches notifications (Console + File + optional Telegram)
"""

import logging
import logging.handlers
import html
import time
import json
import os
import math
import threading
import hashlib
import re
from decimal import Decimal, InvalidOperation
import numpy as np
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from config import BotConfig
from exchange import AccountAccessError, ExchangeConnector
from strategies import (
    DowntrendRsiRecoveryTestnetStrategy, MacroTrendPullbackStrategy, get_strategy, ohlcv_to_dataframe,
    Signal, TradeSignal,
)
from risk_manager import RiskManager
from shadow_strategy import CANDIDATE_ID, evaluate_shadow_candidate
from regime_router import evaluate_regime_router
from shadow_ledger import ShadowLedger

logger = logging.getLogger("bot.engine")
ESTIMATED_TAKER_FEE_RATE = 0.001


class TradingBot:
    """
    Main trading bot engine.

    Runs in a continuous loop, analyzing markets and executing trades
    according to the configured strategy and risk management rules.
    """

    def __init__(self, config: BotConfig):
        self.config = config
        self.exchange = ExchangeConnector(config.exchange)
        self.strategy = get_strategy(
            config.strategy.name,
            regime_filter_enabled=config.strategy.regime_filter_enabled,
            min_trend_adx=config.strategy.min_trend_adx,
            max_atr_pct=config.strategy.max_atr_pct,
            ema72_max_atr_pct=config.strategy.ema72_max_atr_pct,…61460 tokens truncated…r("interval_seconds must be a positive integer")
        self.config.display()
        errors = self.config.validate()
        if errors:
            logger.error("Configuration errors:")
            for err in errors:
                logger.error("  ❌ %s", err)
            return

        self.is_running = True
        logger.info("🤖 Bot running...")
        logger.info("Strategy: %s", self.config.strategy.name)
        logger.info("Pairs: %s", ", ".join(self.config.strategy.trading_pairs))
        logger.info("Timeframe: %s", self.config.strategy.timeframe)
        logger.info("Loop interval: %ss", interval_seconds)

        try:
            while self.is_running:
                cycle_start = time.time()
                self.run_cycle()
                sleep_time = max(0, interval_seconds - (time.time() - cycle_start))
                if sleep_time:
                    time.sleep(sleep_time)
        except KeyboardInterrupt:
            logger.info("\n🛑 Bot stopped by user (Ctrl+C)")
        finally:
            self.is_running = False
            self._shutdown()

    def run_once(self):
        """Run a single serialized analysis cycle."""
        self.config.display()
        if self.run_cycle():
            self.print_status()
            self.risk_manager.display_performance()

    def _shutdown(self):
        """Clean shutdown — display final stats."""
        logger.info("\n📊 Final Performance Report:")
        self.risk_manager.display_performance()

        if self.risk_manager.open_positions:
            logger.warning(
                f"⚠️  You have {len(self.risk_manager.open_positions)} "
                f"persisted open positions in data/open_positions.json."
            )
            for symbol, pos in self.risk_manager.open_positions.items():
                logger.warning(
                    f"    {symbol}: {pos.amount:.6f} entered at ${pos.entry_price:,.2f}"
                )

