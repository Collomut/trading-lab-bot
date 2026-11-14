"""
Configuration loader for the trading bot.
Reads settings from .env file and provides typed config access.
"""

import math
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool, *, strict: bool = False) -> bool:
    """Parse environment booleans without silently mapping typos to live mode."""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    accepted = {"true": True, "false": False} if strict else {
        "true": True, "false": False, "1": True, "0": False, "yes": True, "no": False,
    }
    if value not in accepted:
        allowed = "true or false" if strict else "true/false, 1/0, or yes/no"
        raise ValueError(f"{name} must be {allowed}; got an invalid boolean value")
    return accepted[value]


@dataclass
class ExchangeConfig:
    """Exchange connection settings."""
    name: str = os.getenv("EXCHANGE", "binance")
    api_key: str = os.getenv("API_KEY", "")
    api_secret: str = os.getenv("API_SECRET", "")
    demo_api_key: str = os.getenv("DEMO_API_KEY", "")
    demo_api_secret: str = os.getenv("DEMO_API_SECRET", "")
    paper_trading: bool = field(default_factory=lambda: _env_bool("PAPER_TRADING", True, strict=True))
    paper_environment: str = os.getenv("PAPER_ENVIRONMENT", "testnet").strip().lower()
    fallback_to_simulation: bool = field(default_factory=lambda: _env_bool("FALLBACK_TO_SIMULATION", False))
    allow_live_trading: bool = field(default_factory=lambda: _env_bool("ALLOW_LIVE_TRADING", False))


@dataclass
class RiskConfig:
    """Risk management parameters."""
    max_risk_per_trade: float = float(os.getenv("MAX_RISK_PER_TRADE", "1.0"))
    max_open_positions: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
    stop_loss_pct: float = float(os.getenv("STOP_LOSS_PCT", "2.0"))
    take_profit_pct: float = float(os.getenv("TAKE_PROFIT_PCT", "3.0"))
    max_daily_loss_pct: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "3.0"))
    position_size_pct: float = float(os.getenv("POSITION_SIZE_PCT", "10.0"))
    max_rolling_drawdown_pct: float = float(os.getenv("MAX_ROLLING_DRAWDOWN_PCT", "5.0"))
    max_consecutive_losses: int = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
    start_paused: bool = field(default_factory=lambda: _env_bool("START_PAUSED", False))


@dataclass
class StrategyConfig:
    """Strategy parameters."""
    name: str = os.getenv("STRATEGY", "combined")
    timeframe: str = os.getenv("TIMEFRAME", "1h")
    lookback_candles: int = int(os.getenv(
        "LOOKBACK_CANDLES",
        "2500" if os.getenv("STRATEGY", "combined").strip().lower() in {"macro_donchian_ema72", "dynamic_regime"} else "150",
    ))
    regime_filter_enabled: bool = field(default_factory=lambda: _env_bool("REGIME_FILTER_ENABLED", True))
    min_trend_adx: float = float(os.getenv("MIN_TREND_ADX", "20"))
    max_atr_pct: float = float(os.getenv("MAX_ATR_PCT", "3.0"))
    trading_pairs: list = field(default_factory=lambda: os.getenv(
        "TRADING_PAIRS", "BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,LINK/USDT"
    ).split(","))


@dataclass
class BotConfig:
    """Top-level bot configuration."""
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_file: str = os.getenv("LOG_FILE", "logs/trading_bot.log")
    enable_notifications: bool = field(default_factory=lambda: _env_bool("ENABLE_NOTIFICATIONS", True))
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587")) if os.getenv("SMTP_PORT", "587").strip().isdigit() else 587
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    alert_email: str = os.getenv("ALERT_EMAIL", "")
    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "")
    dashboard_token: str = os.getenv("DASHBOARD_TOKEN", "")
    data_dir: str = os.getenv("DATA_DIR", "data")
    max_spread_bps: float = float(os.getenv("MAX_SPREAD_BPS", "20"))
    max_slippage_bps: float = float(os.getenv("MAX_SLIPPAGE_BPS", "30"))
    max_pairwise_correlation: float = float(os.getenv("MAX_PAIRWISE_CORRELATION", "0.85"))
    max_testnet_price_divergence_pct: float = float(os.getenv("MAX_TESTNET_PRICE_DIVERGENCE_PCT", "0.5"))

    def validate(self):
        """Validate configuration values."""
        errors = []

        for label, value in (
            ("MAX_RISK_PER_TRADE", self.risk.max_risk_per_trade),
            ("STOP_LOSS_PCT", self.risk.stop_loss_pct),
            ("TAKE_PROFIT_PCT", self.risk.take_profit_pct),
            ("MAX_DAILY_LOSS_PCT", self.risk.max_daily_loss_pct),
            ("POSITION_SIZE_PCT", self.risk.position_size_pct),
            ("MAX_ROLLING_DRAWDOWN_PCT", self.risk.max_rolling_drawdown_pct),
        ):
            if not math.isfinite(value):
                errors.append(f"{label} must be finite")
        if not isinstance(self.risk.max_open_positions, int) or isinstance(self.risk.max_open_positions, bool):
            errors.append("MAX_OPEN_POSITIONS must be an integer")
        if not isinstance(self.risk.max_consecutive_losses, int) or isinstance(self.risk.max_consecutive_losses, bool):
            errors.append("MAX_CONSECUTIVE_LOSSES must be an integer")

        if not isinstance(self.exchange.paper_trading, bool):
            errors.append("PAPER_TRADING must be a boolean parsed from true or false")
        if not isinstance(self.exchange.fallback_to_simulation, bool):
            errors.append("FALLBACK_TO_SIMULATION must be a boolean")
        if not isinstance(self.exchange.allow_live_trading, bool):
            errors.append("ALLOW_LIVE_TRADING must be a boolean")
        if self.exchange.paper_trading:
            if self.exchange.paper_environment not in {"testnet", "demo", "simulated"}:
                errors.append("PAPER_ENVIRONMENT must be either 'testnet', 'demo', or 'simulated'")
            if self.exchange.paper_environment == "demo" and self.exchange.name.lower() != "binance":
                errors.append("PAPER_ENVIRONMENT=demo is currently supported only for Binance")

        # Fail closed: live orders require two independent, explicit settings.
        if not self.exchange.paper_trading and not self.exchange.allow_live_trading:
            errors.append("Live trading is disabled. Set ALLOW_LIVE_TRADING=true only after completing the live-readiness checklist.")
        if not self.exchange.paper_trading and self.exchange.allow_live_trading:
            errors.append("Live trading requires an explicit operator confirmation and is not enabled by this application yet.")

        if self.exchange.paper_trading and self.exchange.paper_environment == "demo":
            if not self.exchange.demo_api_key:
                errors.append("DEMO_API_KEY is required for Binance Demo Mode")
            if not self.exchange.demo_api_secret:
                errors.append("DEMO_API_SECRET is required for Binance Demo Mode")
        elif self.exchange.paper_trading and (self.exchange.paper_environment == "simulated" or self.exchange.fallback_to_simulation):
            pass
        else:
            if not self.exchange.api_key or self.exchange.api_key == "your_testnet_api_key_here":
                errors.append("API_KEY is not set. Get a testnet key from https://testnet.binance.vision")
            if not self.exchange.api_secret or self.exchange.api_secret == "your_testnet_api_secret_here":
                errors.append("API_SECRET is not set. Get a testnet secret from https://testnet.binance.vision")
        if self.risk.max_risk_per_trade > 5.0:
            errors.append("MAX_RISK_PER_TRADE > 5% is extremely risky!")
        if self.risk.stop_loss_pct > 10.0:
            errors.append("STOP_LOSS_PCT > 10% defeats the purpose of a stop-loss")
        if self.risk.position_size_pct > 50.0:
            errors.append("POSITION_SIZE_PCT > 50% is dangerously high")
        strategy_names = {"combined", "ema_crossover", "rsi_mean_reversion", "macd", "macro_donchian_ema72", "dynamic_regime"}
        if self.strategy.name not in strategy_names:
            errors.append(f"STRATEGY must be one of: {', '.join(sorted(strategy_names))}")
        supported_timeframes = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"}
        if self.strategy.timeframe not in supported_timeframes:
            errors.append("TIMEFRAME is unsupported")
        if not isinstance(self.strategy.lookback_candles, int) or not 50 <= self.strategy.lookback_candles <= 5000:
            errors.append("LOOKBACK_CANDLES must be an integer between 50 and 5000")
        normalized_pairs = [pair.strip() for pair in self.strategy.trading_pairs]
        if not normalized_pairs or any(not pair for pair in normalized_pairs):
            errors.append("TRADING_PAIRS must contain at least one non-empty pair")
        if len({pair.upper() for pair in normalized_pairs}) != len(normalized_pairs):
            errors.append("TRADING_PAIRS must not contain duplicates")
        if any("/" not in pair or len(pair.split("/")) != 2 for pair in normalized_pairs):
            errors.append("Each TRADING_PAIRS item must use BASE/QUOTE format")
        if any(pair.split("/")[-1].upper() != "USDT" for pair in normalized_pairs if "/" in pair):
            errors.append("All configured pairs must be USDT-quoted; portfolio sizing is USDT-based")
        if not math.isfinite(self.max_spread_bps) or not 0 < self.max_spread_bps <= 100:
            errors.append("MAX_SPREAD_BPS must be finite and between 0 and 100")
        if not math.isfinite(self.max_slippage_bps) or not 0 < self.max_slippage_bps <= 100:
            errors.append("MAX_SLIPPAGE_BPS must be finite and between 0 and 100")
        if not math.isfinite(self.max_pairwise_correlation) or not 0.5 <= self.max_pairwise_correlation < 1:
            errors.append("MAX_PAIRWISE_CORRELATION must be finite and between 0.5 and 1")
        if (not math.isfinite(self.max_testnet_price_divergence_pct)
                or not 0 < self.max_testnet_price_divergence_pct <= 2):
            errors.append("MAX_TESTNET_PRICE_DIVERGENCE_PCT must be finite and greater than 0 and no more than 2")
        if not math.isfinite(self.risk.take_profit_pct) or self.risk.take_profit_pct <= 0:
            errors.append("TAKE_PROFIT_PCT must be a finite value greater than 0")

        if self.risk.max_risk_per_trade <= 0 or self.risk.max_risk_per_trade > 2:
            errors.append("MAX_RISK_PER_TRADE must be greater than 0 and no more than 2%")
        if self.risk.max_open_positions < 1:
            errors.append("MAX_OPEN_POSITIONS must be at least 1")
        if self.risk.max_daily_loss_pct <= 0 or self.risk.max_daily_loss_pct > 5:
            errors.append("MAX_DAILY_LOSS_PCT must be greater than 0 and no more than 5%")
        if self.risk.max_rolling_drawdown_pct <= 0 or self.risk.max_rolling_drawdown_pct > 10:
            errors.append("MAX_ROLLING_DRAWDOWN_PCT must be greater than 0 and no more than 10%")
        if not 1 <= self.risk.max_consecutive_losses <= 20:
            errors.append("MAX_CONSECUTIVE_LOSSES must be between 1 and 20")
        if not (0 < self.risk.stop_loss_pct <= 10):
            errors.append("STOP_LOSS_PCT must be greater than 0 and no more than 10%")
        if not (0 < self.risk.position_size_pct <= 50):
            errors.append("POSITION_SIZE_PCT must be greater than 0 and no more than 50%")
        if not self.dashboard_token:
            errors.append("DASHBOARD_TOKEN is required because the Railway dashboard exposes account and trading controls")
        if not math.isfinite(self.strategy.min_trend_adx) or not (0 <= self.strategy.min_trend_adx <= 100):
            errors.append("MIN_TREND_ADX must be a finite value between 0 and 100")
        if not math.isfinite(self.strategy.max_atr_pct) or self.strategy.max_atr_pct <= 0:
            errors.append("MAX_ATR_PCT must be a finite value greater than 0")
        if self.strategy.name in {"macro_donchian_ema72", "dynamic_regime"}:
            if (not self.exchange.paper_trading or self.exchange.paper_environment not in {"testnet", "demo"}
                    or self.exchange.name.strip().lower() != "binance"):
                errors.append(f"{self.strategy.name} requires Binance Spot Testnet or Binance Demo paper trading")
            if self.strategy.timeframe != "1h":
                errors.append(f"{self.strategy.name} requires TIMEFRAME=1h")
            if self.strategy.lookback_candles < 2500:
                errors.append(f"{self.strategy.name} requires LOOKBACK_CANDLES >= 2500 for its contiguous EMA480 warmup")
            if self.max_testnet_price_divergence_pct > 0.5:
                errors.append(f"{self.strategy.name} caps MAX_TESTNET_PRICE_DIVERGENCE_PCT at 0.5%")
            if self.risk.max_risk_per_trade > 1.0:
                errors.append(f"{self.strategy.name} caps MAX_RISK_PER_TRADE at 1%")
            if self.risk.position_size_pct > 10.0:
                errors.append(f"{self.strategy.name} caps POSITION_SIZE_PCT at 10%")
            if self.risk.max_open_positions > 3:
                errors.append(f"{self.strategy.name} caps MAX_OPEN_POSITIONS at 3")
            if self.risk.max_daily_loss_pct > 3.0:
                errors.append(f"{self.strategy.name} caps MAX_DAILY_LOSS_PCT at 3%")
            if self.risk.max_rolling_drawdown_pct > 5.0:
                errors.append(f"{self.strategy.name} caps MAX_ROLLING_DRAWDOWN_PCT at 5%")
            if self.risk.max_consecutive_losses > 3:
                errors.append(f"{self.strategy.name} keeps MAX_CONSECUTIVE_LOSSES at 3 or lower")
        if self.smtp_host and not (1 <= self.smtp_port <= 65535):
            errors.append("SMTP_PORT must be an integer between 1 and 65535")

        return errors

    def display(self):
        """Print current configuration (hiding secrets)."""
        masked_key = "SET" if self.exchange.api_key else "NOT SET"
        if self.exchange.paper_trading:
            paper_names = {"testnet": "Spot Testnet", "demo": "Binance Demo", "simulated": "Simulated"}
            mode = f"🧪 PAPER TRADING ({paper_names.get(self.exchange.paper_environment, self.exchange.paper_environment)})"
        else:
            mode = "💰 LIVE TRADING (Real Money!)"

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
