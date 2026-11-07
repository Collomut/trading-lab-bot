"""
Trading strategies — the brain of the bot.

Each strategy analyzes market data using technical indicators
and produces BUY/SELL/HOLD signals. All strategies prioritize
capital preservation over aggressive gains.

Upgraded features:
  - 200 EMA Macro Trend Filter (never buy in a downtrend)
  - ATR (Average True Range) volatility calculation for dynamic risk
  - Volume Confirmation Filter (avoids low-liquidity fakeouts)
"""

import logging
from enum import Enum
from dataclasses import dataclass

import pandas as pd
import numpy as np
from ta.trend import ADXIndicator, EMAIndicator, MACD, SMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

logger = logging.getLogger("bot.strategy")


class Signal(Enum):
    """Trading signal."""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class TradeSignal:
    """A trading signal with heuristic metadata.

    Note: `confidence` represents a heuristic rule-matching score (0.0 to 1.0),
    not a calibrated statistical probability of profit (F-21).
    """
    signal: Signal
    symbol: str
    confidence: float  # Heuristic score between 0.0 and 1.0 (not calibrated probability)
    reason: str
    indicators: dict

    def __str__(self):
        emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}[self.signal.value]
        return (
            f"{emoji} {self.signal.value} {self.symbol} "
            f"(score: {self.confidence:.0%}) — {self.reason}"
        )


def ohlcv_to_dataframe(ohlcv: list) -> pd.DataFrame:
    """Convert ccxt OHLCV data to a pandas DataFrame."""
    df = pd.DataFrame(
        ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


class EMA_Crossover_Strategy:
    """
    Exponential Moving Average Crossover Strategy with Trend Filter.

    BUY when short EMA crosses ABOVE long EMA (bullish crossover).
    SELL when short EMA crosses BELOW long EMA (bearish crossover).

    Risk level: LOW-MEDIUM
    """

    def __init__(self, short_period: int = 9, long_period: int = 21, crossover_lookback: int = 2):
        self.short_period = short_period
        self.long_period = long_period
        self.crossover_lookback = max(1, int(crossover_lookback))

    def analyze(self, df: pd.DataFrame, symbol: str) -> TradeSignal:
        """Analyze data and produce a trading signal."""
        ema_short_indicator = EMAIndicator(close=df["close"], window=self.short_period)
        ema_long_indicator = EMAIndicator(close=df["close"], window=self.long_period)

        df["ema_short"] = ema_short_indicator.ema_indicator()
        df["ema_long"] = ema_long_indicator.ema_indicator()

        if len(df) < self.long_period + 2:
            return TradeSignal(
                signal=Signal.HOLD, symbol=symbol, confidence=0.0,
                reason="Not enough data", indicators={}
            )

        current = df.iloc[-1]
        ema_short_now = current["ema_short"]
        ema_long_now = current["ema_long"]

        if any(pd.isna([ema_short_now, ema_long_now])):
            return TradeSignal(
                signal=Signal.HOLD, symbol=symbol, confidence=0.0,
                reason="Insufficient data for EMA calculation", indicators={}
            )

        indicators = {
            "ema_short": round(ema_short_now, 2),
            "ema_long": round(ema_long_now, 2),
            "price": round(current["close"], 2),
        }

        # Keep a fresh cross actionable for the next two completed candles.
        recent_cross = None
        first_pair = max(1, len(df) - self.crossover_lookback)
        for index in range(first_pair, len(df)):
            previous = df.iloc[index - 1]
            current_pair = df.iloc[index]
            values = [previous["ema_short"], previous["ema_long"],
                      current_pair["ema_short"], current_pair["ema_long"]]
            if any(pd.isna(values)):
                continue
            if values[0] <= values[1] and values[2] > values[3]:
                recent_cross = (Signal.BUY, len(df) - 1 - index)
            elif values[0] >= values[1] and values[2] < values[3]:
                recent_cross = (Signal.SELL, len(df) - 1 - index)

        # Bullish crossover
        if recent_cross and recent_cross[0] == Signal.BUY and ema_short_now > ema_long_now:
            gap_pct = abs(ema_short_now - ema_long_now) / ema_long_now * 100
            confidence = min(0.8, 0.4 + gap_pct * 10)
            return TradeSignal(
                signal=Signal.BUY, symbol=symbol, confidence=confidence,
                reason=f"EMA{self.short_period} crossed above EMA{self.long_period} ({recent_cross[1]} candle(s) ago)",
                indicators=indicators
            )

        # Bearish crossover
        if recent_cross and recent_cross[0] == Signal.SELL and ema_short_now < ema_long_now:
            gap_pct = abs(ema_short_now - ema_long_now) / ema_long_now * 100
            confidence = min(0.8, 0.4 + gap_pct * 10)
            return TradeSignal(
                signal=Signal.SELL, symbol=symbol, confidence=confidence,
                reason=f"EMA{self.short_period} crossed below EMA{self.long_period} ({recent_cross[1]} candle(s) ago)",
                indicators=indicators
            )

        return TradeSignal(
            signal=Signal.HOLD, symbol=symbol, confidence=0.0,
            reason="No crossover detected", indicators=indicators
        )


class RSI_Mean_Reversion_Strategy:
    """
    RSI Mean Reversion Strategy.

    BUY when RSI drops below oversold level.
    SELL when RSI rises above overbought level.

    Risk level: LOW
    """

    def __init__(
        self,
        rsi_period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
        pullback_recovery: float = 40.0,
    ):
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.overbought = overbought
        self.pullback_recovery = pullback_recovery

    def analyze(self, df: pd.DataFrame, symbol: str) -> TradeSignal:
        """Analyze data and produce a trading signal."""
        rsi_indicator = RSIIndicator(close=df["close"], window=self.rsi_period)
        df["rsi"] = rsi_indicator.rsi()

        if df["rsi"].isna().all():
            return TradeSignal(
                signal=Signal.HOLD, symbol=symbol, confidence=0.0,
                reason="Not enough data for RSI", indicators={}
            )

        current_rsi = df["rsi"].iloc[-1]
        prev_rsi = df["rsi"].iloc[-2] if len(df) > 1 else current_rsi

        if pd.isna(current_rsi):
            return TradeSignal(
                signal=Signal.HOLD, symbol=symbol, confidence=0.0,
                reason="RSI not yet available", indicators={}
            )

        indicators = {
            "rsi": round(current_rsi, 2),
            "price": round(df["close"].iloc[-1], 2),
            "oversold_threshold": self.oversold,
            "overbought_threshold": self.overbought,
            "pullback_recovery_threshold": self.pullback_recovery,
        }

        # Require a confirmed recovery through 40 after a pullback; an RSI that
        # is still falling through oversold territory is not a reversal vote.
        if not pd.isna(prev_rsi) and prev_rsi < self.pullback_recovery <= current_rsi:
            confidence = min(0.75, 0.45 + (current_rsi - self.pullback_recovery) / 100)
            return TradeSignal(
                signal=Signal.BUY, symbol=symbol, confidence=confidence,
                reason=f"RSI pullback recovered above {self.pullback_recovery:.0f} ({prev_rsi:.1f} → {current_rsi:.1f})",
                indicators=indicators
            )

        # Overbought — potential sell
        if current_rsi > self.overbought:
            depth = (current_rsi - self.overbought) / (100 - self.overbought)
            confidence = min(0.85, 0.5 + depth)

            if not pd.isna(prev_rsi) and current_rsi < prev_rsi:
                confidence = min(0.9, confidence + 0.1)
                reason = f"RSI={current_rsi:.1f} overbought AND turning down"
            else:
                reason = f"RSI={current_rsi:.1f} overbought (above {self.overbought})"

            return TradeSignal(
                signal=Signal.SELL, symbol=symbol, confidence=confidence,
                reason=reason, indicators=indicators
            )

        return TradeSignal(
            signal=Signal.HOLD, symbol=symbol, confidence=0.0,
            reason=f"RSI={current_rsi:.1f} neutral",
            indicators=indicators
        )


class MACD_Strategy:
    """
    MACD Strategy.

    BUY when MACD line crosses above signal line.
    SELL when MACD line crosses below signal line.

    Risk level: LOW-MEDIUM
    """

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self.fast = fast
        self.slow = slow
        self.signal_period = signal
        self.crossover_lookback = 2

    def analyze(self, df: pd.DataFrame, symbol: str) -> TradeSignal:
        """Analyze data and produce a trading signal."""
        macd_indicator = MACD(
            close=df["close"],
            window_fast=self.fast,
            window_slow=self.slow,
            window_sign=self.signal_period
        )

        df["macd"] = macd_indicator.macd()
        df["macd_signal"] = macd_indicator.macd_signal()
        df["macd_hist"] = macd_indicator.macd_diff()

        if df["macd"].isna().all() or df["macd_signal"].isna().all():
            return TradeSignal(
                signal=Signal.HOLD, symbol=symbol, confidence=0.0,
                reason="Not enough data for MACD", indicators={}
            )

        current = df.iloc[-1]

        if any(pd.isna([current["macd"], current["macd_signal"]])):
            return TradeSignal(
                signal=Signal.HOLD, symbol=symbol, confidence=0.0,
                reason="MACD values not yet available", indicators={}
            )

        indicators = {
            "macd": round(current["macd"], 4),
            "macd_signal": round(current["macd_signal"], 4),
            "macd_histogram": round(current["macd_hist"], 4),
            "price": round(current["close"], 2),
        }

        recent_cross = None
        first_pair = max(1, len(df) - self.crossover_lookback)
        for index in range(first_pair, len(df)):
            previous = df.iloc[index - 1]
            current_pair = df.iloc[index]
            values = [previous["macd"], previous["macd_signal"],
                      current_pair["macd"], current_pair["macd_signal"]]
            if any(pd.isna(values)):
                continue
            if values[0] <= values[1] and values[2] > values[3]:
                recent_cross = (Signal.BUY, len(df) - 1 - index)
            elif values[0] >= values[1] and values[2] < values[3]:
                recent_cross = (Signal.SELL, len(df) - 1 - index)

        # Bullish crossover
        if (recent_cross and recent_cross[0] == Signal.BUY
                and current["macd"] > current["macd_signal"]):
            confidence = min(0.75, 0.4 + abs(current["macd_hist"]) * 100)
            return TradeSignal(
                signal=Signal.BUY, symbol=symbol, confidence=confidence,
                reason=f"MACD crossed above signal line ({recent_cross[1]} candle(s) ago)",
                indicators=indicators
            )

        # Bearish crossover
        if (recent_cross and recent_cross[0] == Signal.SELL
                and current["macd"] < current["macd_signal"]):
            confidence = min(0.75, 0.4 + abs(current["macd_hist"]) * 100)
            return TradeSignal(
                signal=Signal.SELL, symbol=symbol, confidence=confidence,
                reason=f"MACD crossed below signal line ({recent_cross[1]} candle(s) ago)",
                indicators=indicators
            )

        return TradeSignal(
            signal=Signal.HOLD, symbol=symbol, confidence=0.0,
            reason="No MACD crossover", indicators=indicators
        )


class CombinedStrategy:
    """
    Institutional-grade conservative strategy:
      1. Macro Trend Filter (EMA 200 or EMA 50): only buy in uptrend.
      2. Multi-indicator consensus: 2/3 agreement across EMA, RSI, MACD.
      3. Volume confirmation: rejects low-volume false breaks.
      4. Dynamic ATR calculation for volatility-adaptive stops.

    Risk level: LOWEST / CONSERVATIVE
    """

    def __init__(
        self,
        use_trend_filter: bool = True,
        trend_period: int = 50,
        regime_filter_enabled: bool = True,
        min_trend_adx: float = 18.0,
        max_atr_pct: float = 3.0,
    ):
        self.ema = EMA_Crossover_Strategy()
        self.rsi = RSI_Mean_Reversion_Strategy()
        self.macd = MACD_Strategy()
        self.use_trend_filter = use_trend_filter
        self.trend_period = trend_period
        self.regime_filter_enabled = regime_filter_enabled
        self.min_trend_adx = min_trend_adx
        self.max_atr_pct = max_atr_pct

    def analyze(self, df: pd.DataFrame, symbol: str) -> TradeSignal:
        """
        Run multi-indicator consensus with trend & volume safety filters.
        """
        # Calculate ATR for volatility context
        atr_indicator = AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"], window=14
        )
        df["atr"] = atr_indicator.average_true_range()
        current_atr = df["atr"].iloc[-1] if not df["atr"].isna().all() else 0.0

        # A fixed, inspectable gate: pause new BUYs in weak-trend or unusually
        # volatile conditions. It never changes its own thresholds.
        regime_ok = True
        regime_reason = "Regime filter disabled"
        regime_adx = np.nan
        atr_pct = np.nan
        regime_label = "filter_off"
        if self.regime_filter_enabled:
            if len(df) < 30:
                regime_ok = False
                regime_reason = "Not enough candles to classify market regime"
                regime_label = "unknown"
            else:
                try:
                    adx_indicator = ADXIndicator(
                        high=df["high"], low=df["low"], close=df["close"], window=14
                    )
                    regime_adx = float(adx_indicator.adx().iloc[-1])
                    current_close = float(df["close"].iloc[-1])
                    atr_pct = (float(current_atr) / current_close * 100.0) if current_close > 0 else np.nan
                    if not np.isfinite(regime_adx) or not np.isfinite(atr_pct):
                        raise ValueError("Non-finite regime indicator")
                    if atr_pct > self.max_atr_pct:
                        regime_ok = False
                        regime_reason = f"High volatility ({atr_pct:.2f}% ATR > {self.max_atr_pct:.2f}% limit)"
                        regime_label = "high_volatility"
                    elif regime_adx < self.min_trend_adx:
                        regime_ok = False
                        regime_reason = f"Weak trend ({regime_adx:.1f} ADX < {self.min_trend_adx:.1f} minimum)"
                        regime_label = "weak_trend"
                    else:
                        regime_reason = f"Trend conditions pass ({regime_adx:.1f} ADX, {atr_pct:.2f}% ATR)"
                        regime_label = "trend"
                except Exception as exc:
                    regime_ok = False
                    regime_reason = f"Regime unavailable; entry paused ({exc})"
                    regime_label = "unknown"

        # Calculate volume moving average
        vol_sma_indicator = SMAIndicator(close=df["volume"], window=20)
        df["vol_sma20"] = vol_sma_indicator.sma_indicator()
        current_vol = df["volume"].iloc[-1]
        vol_avg = df["vol_sma20"].iloc[-1] if not df["vol_sma20"].isna().all() else current_vol

        # Trend filter (EMA 50 or 200)
        trend_ok_for_buy = True
        trend_reason = ""
        trend_val = None

        effective_trend_period = min(self.trend_period, len(df) - 1)
        if self.use_trend_filter and effective_trend_period >= 20:
            trend_ind = EMAIndicator(close=df["close"], window=effective_trend_period)
            df["ema_trend"] = trend_ind.ema_indicator()
            trend_val = df["ema_trend"].iloc[-1]
            current_close = df["close"].iloc[-1]

            if not pd.isna(trend_val):
                if current_close < trend_val:
                    trend_ok_for_buy = False
                    trend_reason = f"Downtrend filter: Price ${current_close:,.2f} < EMA{effective_trend_period} ${trend_val:,.2f}"

        # Run sub-strategies
        signals = [
            self.ema.analyze(df.copy(), symbol),
            self.rsi.analyze(df.copy(), symbol),
            self.macd.analyze(df.copy(), symbol),
        ]

        buy_signals = [s for s in signals if s.signal == Signal.BUY]
        sell_signals = [s for s in signals if s.signal == Signal.SELL]

        all_indicators = {
            "atr": round(current_atr, 4) if not pd.isna(current_atr) else 0.0,
            "atr_pct": round(float(atr_pct), 3) if np.isfinite(atr_pct) else None,
            "adx": round(float(regime_adx), 2) if np.isfinite(regime_adx) else None,
            "market_regime": regime_label,
            "volume": round(current_vol, 2),
            "vol_avg20": round(vol_avg, 2) if not pd.isna(vol_avg) else 0.0,
        }
        if trend_val and not pd.isna(trend_val):
            all_indicators["ema_trend"] = round(trend_val, 2)

        for s in signals:
            all_indicators.update(s.indicators)
        # Expose the component decisions for status/API diagnostics so a HOLD
        # can be explained without reconstructing hidden votes.
        all_indicators["strategy_votes"] = {
            name: {
                "signal": item.signal.value,
                "reason": item.reason,
                "confidence": round(float(item.confidence), 3),
            }
            for name, item in zip(("ema", "rsi", "macd"), signals)
        }
        all_indicators["consensus_required"] = 2

        # Handle SELL signals
        if len(sell_signals) >= 2:
            avg_confidence = np.mean([s.confidence for s in sell_signals])
            reasons = " + ".join([s.reason for s in sell_signals])
            return TradeSignal(
                signal=Signal.SELL, symbol=symbol,
                confidence=min(0.95, avg_confidence + 0.1),
                reason=f"SELL CONSENSUS ({len(sell_signals)}/3 agree): {reasons}",
                indicators=all_indicators
            )

        # Handle BUY signals (Must pass Trend Filter + Agreement)
        if len(buy_signals) >= 2:
            if not regime_ok:
                return TradeSignal(
                    signal=Signal.HOLD, symbol=symbol, confidence=0.0,
                    reason=f"BUY PAUSED: {regime_reason}",
                    indicators=all_indicators
                )

            # Check Trend Filter
            if not trend_ok_for_buy:
                return TradeSignal(
                    signal=Signal.HOLD, symbol=symbol, confidence=0.0,
                    reason=f"BUY BLOCKED by {trend_reason}",
                    indicators=all_indicators
                )

            # Check Volume Filter (avoid trading on extreme volume drought)
            if not pd.isna(vol_avg) and vol_avg > 0 and current_vol < 0.3 * vol_avg:
                return TradeSignal(
                    signal=Signal.HOLD, symbol=symbol, confidence=0.0,
                    reason=f"BUY BLOCKED by low volume ({current_vol:.1f} vs avg {vol_avg:.1f})",
                    indicators=all_indicators
                )

            avg_confidence = np.mean([s.confidence for s in buy_signals])
            reasons = " + ".join([s.reason for s in buy_signals])
            return TradeSignal(
                signal=Signal.BUY, symbol=symbol,
                confidence=min(0.95, avg_confidence + 0.1),
                reason=f"BUY CONSENSUS ({len(buy_signals)}/3 agree): {reasons}",
                indicators=all_indicators
            )

        return TradeSignal(
            signal=Signal.HOLD, symbol=symbol, confidence=0.0,
            reason=(f"No consensus (BUY: {len(buy_signals)}/3, SELL: {len(sell_signals)}/3); "
                    f"EMA {signals[0].signal.value}, RSI {signals[1].signal.value}, "
                    f"MACD {signals[2].signal.value}"),
            indicators=all_indicators
        )


def get_strategy(
    name: str,
    *,
    regime_filter_enabled: bool = True,
    min_trend_adx: float = 18.0,
    max_atr_pct: float = 3.0,
):
    """Factory function to get a strategy by name."""
    strategies = {
        "ema_crossover": EMA_Crossover_Strategy,
        "rsi_mean_reversion": RSI_Mean_Reversion_Strategy,
        "macd": MACD_Strategy,
        "combined": CombinedStrategy,
    }
    if name not in strategies:
        raise ValueError(
            f"Unknown strategy '{name}'. Available: {list(strategies.keys())}"
        )
    if name == "combined":
        return CombinedStrategy(
            regime_filter_enabled=regime_filter_enabled,
            min_trend_adx=min_trend_adx,
            max_atr_pct=max_atr_pct,
        )
    return strategies[name]()
