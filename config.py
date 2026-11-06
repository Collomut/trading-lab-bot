"""
Configuration loader for the trading bot.
Reads settings from .env file and provides typed config access.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class ExchangeConfig:
    """Exchange connection settings."""
    name: str = os.getenv("EXCHANGE", "binance")
    api_key: str = os.getenv("API_KEY", "")
    api_secret: str = os.getenv("API_SECRET", "")
    paper_trading: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"
    allow_live_trading: bool = os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true"


@dataclass
class RiskConfig:
    """Risk management parameters."""
    max_risk_per_trade: float = float(os.getenv("MAX_RISK_PER_TRADE", "1.0"))
    max_open_positions: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
    stop_loss_pct: float = float(os.getenv("STOP_LOSS_PCT", "2.0"))
    take_profit_pct: float = float(os.getenv("TAKE_PROFIT_PCT", "3.0"))
    max_daily_loss_pct: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "3.0"))
    position_size_pct: float = float(os.getenv("POSITION_SIZE_PCT", "10.0"))


@dataclass
class StrategyConfig:
    """Strategy parameters."""
    name: str = os.getenv("STRATEGY", "combined")
    timeframe: str = os.getenv("TIMEFRAME", "15m")
    lookback_candles: int = int(os.getenv("LOOKBACK_CANDLES", "100"))
    trading_pairs: list = field(default_factory=lambda: os.getenv(
        "TRADING_PAIRS", "BTC/USDT,ETH/USDT"
    ).split(","))


@dataclass
class BotConfig:
    """Top-level bot configuration."""
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_file: str = os.getenv("LOG_FILE", "logs/trading_bot.log")
    enable_notifications: bool = os.getenv("ENABLE_NOTIFICATIONS", "true").lower() == "true"
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    dashboard_token: str = os.getenv("DASHBOARD_TOKEN", "")
    data_dir: str = os.getenv("DATA_DIR", "data")

    def validate(self):
        """Validate configuration values."""
        errors = []

        # Fail closed: live orders require two independent, explicit settings.
        if not self.exchange.paper_trading and not self.exchange.allow_live_trading:
            errors.append("Live trading is disabled. Set ALLOW_LIVE_TRADING=true only after completing the live-readiness checklist.")
        if not self.exchange.paper_trading and self.exchange.allow_live_trading:
            errors.append("Live trading requires an explicit operator confirmation and is not enabled by this application yet.")

        if not self.exchange.api_key or self.exchange.api_key == "your_testnet_api_key_here":
            errors.append("API_KEY is not set. Get one from https://testnet.binance.vision")
        if not self.exchange.api_secret or self.exchange.api_secret == "your_testnet_api_secret_here":
            errors.append("API_SECRET is not set. Get one from https://testnet.binance.vision")
        if self.risk.max_risk_per_trade > 5.0:
            errors.append("MAX_RISK_PER_TRADE > 5% is extremely risky!")
        if self.risk.stop_loss_pct > 10.0:
            errors.append("STOP_LOSS_PCT > 10% defeats the purpose of a stop-loss")
        if self.risk.position_size_pct > 50.0:
            errors.append("POSITION_SIZE_PCT > 50% is dangerously high")
        if not self.strategy.trading_pairs:
            errors.append("No TRADING_PAIRS configured")

        if self.risk.max_risk_per_trade <= 0 or self.risk.max_risk_per_trade > 2:
            errors.append("MAX_RISK_PER_TRADE must be greater than 0 and no more than 2%")
        if self.risk.max_open_positions < 1:
            errors.append("MAX_OPEN_POSITIONS must be at least 1")
        if self.risk.max_daily_loss_pct <= 0 or self.risk.max_daily_loss_pct > 5:
            errors.append("MAX_DAILY_LOSS_PCT must be greater than 0 and no more than 5%")
        if not (0 < self.risk.stop_loss_pct <= 10):
            errors.append("STOP_LOSS_PCT must be greater than 0 and no more than 10%")
        if not (0 < self.risk.position_size_pct <= 50):
            errors.append("POSITION_SIZE_PCT must be greater than 0 and no more than 50%")
        if not self.dashboard_token:
            errors.append("DASHBOARD_TOKEN is required because the Railway dashboard exposes account and trading controls")

        return errors

    def display(self):
        """Print current configuration (hiding secrets)."""
        masked_key = self.exchange.api_key[:6] + "..." if len(self.exchange.api_key) > 6 else "NOT SET"
        mode = "🧪 PAPER TRADING (Testnet)" if self.exchange.paper_trading else "💰 LIVE TRADING (Real Money!)"

        print(f"""
╔══════════════════════════════════════════════════════════╗
║           AUTOMATED CRYPTO TRADING BOT                   ║
╠══════════════════════════════════════════════════════════╣
║  Mode:           {mode:<39}║
║  Exchange:       {self.exchange.name:<39}║
║  API Key:        {masked_key:<39}║
║  Pairs:          {', '.join(self.strategy.trading_pairs):<39}║
╠══════════════════════════════════════════════════════════╣
║  RISK MANAGEMENT                                         ║
║  Risk per trade: {self.risk.max_risk_per_trade}% {'(Conservative ✓)' if self.risk.max_risk_per_trade <= 2 else '(Aggressive ⚠)':<36}║
║  Stop-loss:      {self.risk.stop_loss_pct}%{'':<37}║
║  Take-profit:    {self.risk.take_profit_pct}%{'':<37}║
║  Max positions:  {self.risk.max_open_positions:<39}║
║  Position size:  {self.risk.position_size_pct}% of balance{'':<27}║
║  Max daily loss: {self.risk.max_daily_loss_pct}%{'':<37}║
╠══════════════════════════════════════════════════════════╣
║  STRATEGY                                                ║
║  Strategy:       {self.strategy.name:<39}║
║  Timeframe:      {self.strategy.timeframe:<39}║
║  Lookback:       {self.strategy.lookback_candles} candles{'':<30}║
╚══════════════════════════════════════════════════════════╝
""")
