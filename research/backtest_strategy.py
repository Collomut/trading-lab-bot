from __future__ import annotations
import json
import math
import sys
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from ta.trend import ADXIndicator, EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from strategies import CombinedStrategy, Signal, ohlcv_to_dataframe

DATA = ROOT / "data"
OUT = ROOT / "results"
OUT.mkdir(exist_ok=True)
LOOKBACK_CANDLES = 100
INITIAL_CASH = 10_000.0
POSITION_SIZE_PCT = 10.0
MAX_RISK_PER_TRADE_PCT = 1.0
MAX_OPEN_POSITIONS = 3
MAX_DAILY_LOSS_PCT = 3.0
MAX_ROLLING_DRAWDOWN_PCT = 5.0
MAX_CONSECUTIVE_LOSSES = 3
MAX_PAIRWISE_CORRELATION = 0.85
FEE_BPS = 10.0                 # base scenario: taker fee per side; actual account tier may differ
SLIPPAGE_BPS = 5.0             # execution haircut per side; OHLCV has no historical order book
STRESS_FEE_BPS = 20.0
STRESS_SLIPPAGE_BPS = 10.0


def compute_signals(symbol: str, crossover_window: int = 2, trend_enabled: bool = True,
                    regime_enabled: bool = True, consensus_required: int = 2,
                    interval: str = "1h", min_trend_adx: float = 18.0,
                    max_atr_pct: float = 3.0, min_atr_pct: float = 0.0,
                    trend_slope_bars: int = 0):
    if interval not in {"15m", "1h"}:
        raise ValueError(f"Unsupported research interval: {interval}")
    path = DATA / f"{symbol}_{interval}.csv"
    raw = pd.read_csv(path)
    raw = raw.drop_duplicates("timestamp_ms").sort_values("timestamp_ms").reset_index(drop=True)
    interval_ms = {"15m": 15 * 60_000, "1h": 60 * 60_000}[interval]
    # Match the production candle-sequence guard: do not generate a signal from
    # a rolling input window containing timestamp gaps or fewer than 100 bars.
    contiguous_step = raw.timestamp_ms.diff().eq(interval_ms)
    sequence_ok = contiguous_step.rolling(LOOKBACK_CANDLES - 1,
                                          min_periods=LOOKBACK_CANDLES - 1).sum().eq(LOOKBACK_CANDLES - 1)
    # Only complete hourly bars are present in Binance's monthly archive.
    df = raw.copy()
    close, high, low, volume = df.close, df.high, df.low, df.volume
    df["ema9"] = EMAIndicator(close, window=9).ema_indicator()
    df["ema21"] = EMAIndicator(close, window=21).ema_indicator()
    df["ema50"] = EMAIndicator(close, window=50).ema_indicator()
    macd_obj = MACD(close, window_fast=12, window_slow=26, window_sign=9)
    df["macd"] = macd_obj.macd()
    df["macd_signal"] = macd_obj.macd_signal()
    df["macd_hist"] = macd_obj.macd_diff()
    df["rsi"] = RSIIndicator(close, window=14).rsi()
    df["atr"] = AverageTrueRange(high, low, close, window=14).average_true_range()
    df["adx"] = ADXIndicator(high, low, close, window=14).adx()
    df["vol_sma20"] = volume.rolling(window=20, min_periods=20).mean()
    # Strategy code's last-two-bars rule includes a cross on the current or immediately prior bar.
    def recent_directional_cross(fast, slow, window, direction):
        if direction == "up":
            cross = (fast.shift(1) <= slow.shift(1)) & (fast > slow)
        else:
            cross = (fast.shift(1) >= slow.shift(1)) & (fast < slow)
        # Find the newest cross of either polarity inside the actionable window.
        up = (fast.shift(1) <= slow.shift(1)) & (fast > slow)
        down = (fast.shift(1) >= slow.shift(1)) & (fast < slow)
        latest_up = pd.Series(np.where(up.to_numpy(), np.arange(len(df)), -10**9), index=df.index).replace(-10**9, np.nan).ffill()
        latest_down = pd.Series(np.where(down.to_numpy(), np.arange(len(df)), -10**9), index=df.index).replace(-10**9, np.nan).ffill()
        last_up = latest_up.fillna(-10**9).to_numpy()
        last_down = latest_down.fillna(-10**9).to_numpy()
        latest_up_age = np.arange(len(df)) - last_up
        latest_down_age = np.arange(len(df)) - last_down
        latest_up_is_newest = last_up > last_down
        # `cross` uses current direction; freshness is window bars inclusive.
        fresh_up = latest_up_is_newest & (latest_up_age < window)
        fresh_down = ~latest_up_is_newest & (latest_down_age < window)
        if direction == "up":
            return (fast > slow) & fresh_up
        return (fast < slow) & fresh_down

    ema_buy = recent_directional_cross(df.ema9, df.ema21, crossover_window, "up")
    ema_sell = recent_directional_cross(df.ema9, df.ema21, crossover_window, "down")
    macd_buy = recent_directional_cross(df.macd, df.macd_signal, crossover_window, "up")
    macd_sell = recent_directional_cross(df.macd, df.macd_signal, crossover_window, "down")
    prev_rsi, rsi = df.rsi.shift(1), df.rsi
    rsi_buy = (prev_rsi < 40.0) & (rsi >= 40.0)
    rsi_sell = (rsi > 70.0) & ~rsi_buy  # RSI recovery BUY check has precedence in source
    buy_n = ema_buy.astype(int) + rsi_buy.astype(int) + macd_buy.astype(int)
    sell_n = ema_sell.astype(int) + rsi_sell.astype(int) + macd_sell.astype(int)

    # Component heuristic confidences from the actual strategy implementation.
    ema_conf = np.minimum(0.8, 0.4 + (abs(df.ema9 - df.ema21) / df.ema21 * 1000))
    rsi_buy_conf = np.minimum(0.75, 0.45 + (rsi - 40.0) / 100.0)
    rsi_sell_conf = np.minimum(0.85, 0.5 + (rsi - 70.0) / 30.0)
    rsi_sell_conf = np.where((rsi > 70) & (rsi < prev_rsi), np.minimum(0.9, rsi_sell_conf + 0.1), rsi_sell_conf)
    macd_conf = np.minimum(0.75, 0.4 + abs(df.macd_hist) * 100)
    buy_conf_sum = ema_buy.astype(float)*ema_conf + rsi_buy.astype(float)*rsi_buy_conf + macd_buy.astype(float)*macd_conf
    sell_conf_sum = ema_sell.astype(float)*ema_conf + rsi_sell.astype(float)*rsi_sell_conf + macd_sell.astype(float)*macd_conf
    buy_conf = np.where(buy_n > 0, buy_conf_sum / np.maximum(buy_n, 1) + 0.1, 0.0).clip(0, 0.95)
    sell_conf = np.where(sell_n > 0, sell_conf_sum / np.maximum(sell_n, 1) + 0.1, 0.0).clip(0, 0.95)

    atr_pct = df.atr / close * 100.0
    regime_ok = (df.adx.notna() & atr_pct.notna() & (atr_pct >= min_atr_pct)
                 & (atr_pct <= max_atr_pct) & (df.adx >= min_trend_adx))
    if not regime_enabled:
        regime_ok = pd.Series(True, index=df.index)
    trend_ok = close >= df.ema50
    if trend_slope_bars > 0:
        trend_ok &= df.ema50 > df.ema50.shift(trend_slope_bars)
    if not trend_enabled:
        trend_ok = pd.Series(True, index=df.index)
    vol_avg = df.vol_sma20.where(df.vol_sma20.notna(), volume)
    volume_ok = (vol_avg <= 0) | (volume >= 0.3 * vol_avg)
    enough = sequence_ok
    regime_ok = regime_ok & enough
    # Use actual component sub-strategy count and the gates in CombinedStrategy.
    candidate_buy = (buy_n >= consensus_required) & sequence_ok
    candidate_sell = (sell_n >= consensus_required) & sequence_ok
    reason = np.full(len(df), "no_consensus", dtype=object)
    signal = np.full(len(df), "HOLD", dtype=object)
    reason[candidate_sell] = "sell_consensus"
    signal[candidate_sell] = "SELL"
    # Actual combined strategy gives SELL consensus precedence if contradictory votes reach threshold.
    buy_candidate = candidate_buy & ~candidate_sell
    regime_block = buy_candidate & ~regime_ok
    trend_block = buy_candidate & regime_ok & ~trend_ok
    volume_block = buy_candidate & regime_ok & trend_ok & ~volume_ok
    confidence_block = buy_candidate & regime_ok & trend_ok & volume_ok & (buy_conf < 0.5)
    buy_allowed = buy_candidate & regime_ok & trend_ok & volume_ok & (buy_conf >= 0.5)
    signal[regime_block] = "HOLD"; reason[regime_block] = "buy_blocked_regime"
    signal[trend_block] = "HOLD"; reason[trend_block] = "buy_blocked_trend"
    signal[volume_block] = "HOLD"; reason[volume_block] = "buy_blocked_volume"
    signal[confidence_block] = "HOLD"; reason[confidence_block] = "buy_blocked_confidence"
    signal[buy_allowed] = "BUY"; reason[buy_allowed] = "buy_allowed"
    # Mark only eligible signals; retain raw candidate and veto masks for funnel report.
    result = pd.DataFrame({
        "timestamp_ms": raw.timestamp_ms, "open": raw.open, "high": raw.high,
        "low": raw.low, "close": raw.close, "volume": raw.volume,
        "atr": df.atr, "adx": df.adx, "atr_pct": atr_pct, "ema50": df.ema50,
        "sequence_ok": sequence_ok,
        "buy_votes": buy_n, "sell_votes": sell_n,
        "ema_buy": ema_buy, "rsi_buy": rsi_buy, "macd_buy": macd_buy,
        "ema_sell": ema_sell, "rsi_sell": rsi_sell, "macd_sell": macd_sell,
        "candidate_buy": candidate_buy, "candidate_sell": candidate_sell,
        "regime_ok": regime_ok, "trend_ok": trend_ok, "volume_ok": volume_ok,
        "buy_confidence": buy_conf, "sell_confidence": sell_conf,
        "regime_block": regime_block, "trend_block": trend_block,
        "volume_block": volume_block, "confidence_block": confidence_block,
        "signal": signal, "reason": reason,
    })
    return result


def compute_pullback_signals(symbol: str, *, rsi_mode: str = "rising45", slope_bars: int = 3,
                             require_regime: bool = True):
    """Research-only trend/pullback candidate; never used by the production bot here."""
    raw = pd.read_csv(DATA / f"{symbol}_1h.csv").drop_duplicates("timestamp_ms").sort_values("timestamp_ms").reset_index(drop=True)
    close, high, low, volume = raw.close, raw.high, raw.low, raw.volume
    ema20 = EMAIndicator(close, window=20).ema_indicator()
    ema50 = EMAIndicator(close, window=50).ema_indicator()
    rsi = RSIIndicator(close, window=14).rsi()
    macd_obj = MACD(close, window_fast=12, window_slow=26, window_sign=9)
    macd, macd_signal = macd_obj.macd(), macd_obj.macd_signal()
    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    adx = ADXIndicator(high, low, close, window=14).adx()
    vol_avg = volume.rolling(20, min_periods=20).mean()
    trend = (close > ema50) & (ema50 > ema50.shift(slope_bars))
    regime = (adx >= 18.0) & ((atr / close * 100.0) <= 3.0)
    if not require_regime:
        regime = pd.Series(True, index=raw.index)
    vol_ok = (vol_avg.isna()) | (vol_avg <= 0) | (volume >= 0.3 * vol_avg)
    reclaim = (close.shift(1) <= ema20.shift(1)) & (close > ema20)
    rsi_rising45 = (rsi >= 45.0) & (rsi > rsi.shift(1))
    rsi_recovery40 = (rsi.shift(1) < 40.0) & (rsi >= 40.0)
    if rsi_mode == "rising45":
        trigger_ok = rsi_rising45
    elif rsi_mode == "recovery40":
        trigger_ok = rsi_recovery40
    elif rsi_mode == "none":
        trigger_ok = pd.Series(True, index=raw.index)
    else:
        raise ValueError(f"Unknown RSI mode: {rsi_mode}")
    buy = trend & regime & vol_ok & reclaim & trigger_ok
    sell = (close < ema20) & (macd < macd_signal)
    signal = np.full(len(raw), "HOLD", dtype=object)
    signal[sell.to_numpy()] = "SELL"
    signal[(buy & ~sell).to_numpy()] = "BUY"
    return pd.DataFrame({"timestamp_ms":raw.timestamp_ms,"open":raw.open,"high":raw.high,
                         "low":raw.low,"close":raw.close,"volume":raw.volume,"atr":atr,
                         "signal":signal,"candidate_buy":buy,"candidate_sell":sell,
                         "trend_ok":trend,"regime_ok":regime,"volume_ok":vol_ok,
                         "rsi":rsi,"ema20":ema20,"ema50":ema50,"adx":adx})


def validate_against_actual_strategy(symbol: str, frame: pd.DataFrame, sample_count: int = 100,
                                     crossover_window: int = 2, trend_enabled: bool = True,
                                     regime_enabled: bool = True, interval: str = "1h",
                                     min_trend_adx: float = 18.0):
    """Compare vectorized baseline to the repository's CombinedStrategy on deterministic samples."""
    raw = pd.read_csv(DATA / f"{symbol}_{interval}.csv")
    rng = np.random.default_rng(20261108)
    valid = np.arange(100, len(frame)-1)
    indices = sorted(rng.choice(valid, size=min(sample_count, len(valid)), replace=False).tolist())
    strat = CombinedStrategy(regime_filter_enabled=regime_enabled, min_trend_adx=min_trend_adx, max_atr_pct=3.0)
    strat.use_trend_filter = trend_enabled
    strat.ema.crossover_lookback = crossover_window
    strat.macd.crossover_lookback = crossover_window
    mismatches = []
    for i in indices:
        chunk = ohlcv_to_dataframe(raw.iloc[max(0, i-LOOKBACK_CANDLES+1):i+1].to_numpy().tolist())
        actual = strat.analyze(chunk, symbol)
        expected = frame.iloc[i].signal
        # The bot's execution layer additionally rejects confidence < 0.50.
        expected = "HOLD" if expected == "BUY" and frame.iloc[i].buy_confidence < 0.5 else expected
        if actual.signal.value != expected:
            mismatches.append({"index": int(i), "actual": actual.signal.value, "expected": str(expected), "reason": actual.reason})
    return {"samples": len(indices), "mismatches": mismatches}


def nav(cash, positions, frames, i):
    return cash + sum(p["qty"] * frames[s].iloc[i].close for s, p in positions.items())


def correlation_ok(candidate: str, positions: dict, frames: dict, i: int,
                   lookback_bars: int = 100) -> bool:
    if not positions:
        return True
    start = max(0, i-lookback_bars)
    a = frames[candidate].close.iloc[start:i+1].pct_change().dropna()
    for existing in positions:
        b = frames[existing].close.iloc[start:i+1].pct_change().dropna()
        aligned = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna()
        if len(aligned) < 31:
            return False
        corr = aligned.a.corr(aligned.b)
        if not math.isfinite(corr) or corr >= MAX_PAIRWISE_CORRELATION:
            return False
    return True


def simulate(frames: dict, fee_bps: float, slip_bps: float, enforce_risk_halts: bool = True,
             start_timestamp_ms: int | None = None, end_timestamp_ms: int | None = None,
             blueprint_stop_model: bool = False, correlation_lookback_bars: int = 100):
    # Use common timestamps across all selected Binance symbols and intervals.
    common = set(frames[next(iter(frames))].timestamp_ms)
    for f in frames.values():
        common &= set(f.timestamp_ms)
    timestamps = sorted(common)
    cash = INITIAL_CASH
    positions = {}
    closed = []
    equity = []
    peak = INITIAL_CASH
    day = None
    day_start = INITIAL_CASH
    halted = False
    halt_reason = ""
    halt_time = None
    consecutive_losses = 0
    fee = fee_bps / 10000.0
    slip = slip_bps / 10000.0
    events = {s: f.set_index("timestamp_ms") for s, f in frames.items()}
    symbol_order = sorted(frames)

    def close_position(sym, raw_price, ts, reason):
        nonlocal cash, consecutive_losses, halted, halt_reason, halt_time
        p = positions.pop(sym)
        fill = raw_price * (1 - slip)
        gross = (fill - p["entry"]) * p["qty"]
        exit_fee = fill * p["qty"] * fee
        pnl = gross - p["entry_fee"] - exit_fee
        cash += fill * p["qty"] - exit_fee
        closed.append({"symbol": sym, "entry_time": p["entry_time"], "exit_time": ts,
                       "entry": p["entry"], "exit": fill, "qty": p["qty"], "pnl": pnl,
                       "pnl_pct_on_notional": pnl / (p["entry"]*p["qty"])*100,
                       "reason": reason, "fee": p["entry_fee"]+exit_fee})
        consecutive_losses = consecutive_losses + 1 if pnl < 0 else 0
        if enforce_risk_halts and consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            halted = True
            halt_reason = "three consecutive losing trades"
            halt_time = ts

    last_simulated_ts = None
    for i, ts in enumerate(timestamps):
        if start_timestamp_ms is not None and ts < start_timestamp_ms:
            continue
        if end_timestamp_ms is not None and ts >= end_timestamp_ms:
            break
        last_simulated_ts = ts
        date_key = datetime.fromtimestamp(ts/1000, tz=timezone.utc).date()
        current_nav_open = cash + sum(p["qty"] * events[s].loc[ts].open for s,p in positions.items())
        if date_key != day:
            day = date_key
            day_start = current_nav_open
        # Process stop/target gaps at the open before strategy orders.
        for sym in list(positions):
            bar = events[sym].loc[ts]
            p = positions[sym]
            if bar.open <= p["stop"]:
                close_position(sym, float(bar.open), ts, "stop_gap")
            elif bar.open >= p["target"]:
                close_position(sym, float(bar.open), ts, "target_gap")
        prev_ts = timestamps[i-1] if i > 0 else None
        # Strategy exits are known only after the previous candle closes; fill at this bar's open.
        if prev_ts is not None:
            for sym in symbol_order:
                if sym in positions and events[sym].loc[prev_ts].signal == "SELL":
                    close_position(sym, float(events[sym].loc[ts].open), ts, "sell_consensus")
        # New BUY signals from the previous completed candle enter at this bar's open.
        if prev_ts is not None and not halted and (start_timestamp_ms is None or prev_ts >= start_timestamp_ms):
            for sym in symbol_order:
                prev_signal = events[sym].loc[prev_ts]
                if prev_signal.signal != "BUY" or sym in positions:
                    continue
                current_nav = cash + sum(p["qty"]*events[s].loc[ts].open for s,p in positions.items())
                if (len(positions) >= MAX_OPEN_POSITIONS or cash <= 0
                        or not correlation_ok(sym, positions, frames, i-1, correlation_lookback_bars)):
                    continue
                fill = float(events[sym].loc[ts].open) * (1 + slip)
                atr = float(prev_signal.atr) if math.isfinite(float(prev_signal.atr)) else 0.0
                if blueprint_stop_model and "swing_low_reference" in prev_signal.index:
                    stop = max(float(prev_signal.swing_low_reference), fill * 0.985)
                    if not math.isfinite(stop) or stop <= 0 or stop >= fill:
                        continue
                    stop_dist = fill - stop
                    target = fill + 2.0 * stop_dist
                else:
                    stop_dist = max(fill*0.01, min(fill*0.03, 1.5*atr)) if atr > 0 else fill*0.02
                    stop = fill-stop_dist
                    target = fill*1.03
                risk_qty = (cash*MAX_RISK_PER_TRADE_PCT/100)/stop_dist
                max_qty = (cash*POSITION_SIZE_PCT/100)/fill
                qty = min(risk_qty, max_qty)
                notional = qty*fill
                entry_fee = notional*fee
                if qty <= 0 or notional+entry_fee > cash:
                    continue
                cash -= notional+entry_fee
                positions[sym] = {"entry": fill, "qty": qty, "stop": stop, "target": target,
                                  "highest": fill, "break_even": False, "entry_fee": entry_fee, "entry_time": ts}
        # Check intrabar risk exits after open-time strategy orders. If both target and
        # stop were touched inside one hourly candle, assume the stop happened first.
        for sym in list(positions):
            bar = events[sym].loc[ts]
            p = positions[sym]
            if bar.low <= p["stop"]:
                close_position(sym, p["stop"], ts, "stop")
            elif bar.high >= p["target"]:
                close_position(sym, p["target"], ts, "take_profit")
            else:
                # Approximate the live +1.5% break-even and 1.5% trailing logic.
                # Newly tightened stops apply from the next bar because OHLCV cannot
                # establish the order of the high and low within this bar.
                p["highest"] = max(p["highest"], float(bar.high))
                if p["break_even"]:
                    p["stop"] = max(p["stop"], p["highest"] * 0.985)
                elif bar.high >= p["entry"] * 1.015:
                    p["break_even"] = True
                    p["stop"] = max(p["stop"], p["entry"])
        # Hourly approximation of equity monitoring circuit-breakers.
        close_nav = nav(cash, positions, frames, i)
        peak = max(peak, close_nav)
        daily_dd = (day_start-close_nav)/day_start*100 if day_start else 0.0
        rolling_dd = (peak-close_nav)/peak*100 if peak else 0.0
        if enforce_risk_halts and (daily_dd >= MAX_DAILY_LOSS_PCT or rolling_dd >= MAX_ROLLING_DRAWDOWN_PCT):
            halted = True
            halt_reason = "daily loss limit" if daily_dd >= MAX_DAILY_LOSS_PCT else "rolling drawdown limit"
            halt_time = ts
        equity.append((ts, close_nav, len(positions)))

    # Mark open positions at the final close; close with conservative costs for the final realized-stat row.
    if last_simulated_ts is not None:
        last_ts = last_simulated_ts
        for sym in list(positions):
            close_position(sym, float(events[sym].loc[last_ts].close), last_ts, "period_end")
        final_nav = cash
    else:
        final_nav = cash
    return {"trades": closed, "equity": equity, "final_nav": final_nav, "halted": halted,
            "halt_reason": halt_reason, "halt_time": halt_time}


def summarize(name, sim, start_ts=None, end_ts=None):
    trades = [t for t in sim["trades"] if (start_ts is None or t["exit_time"] >= start_ts) and (end_ts is None or t["exit_time"] < end_ts)]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    total_pnl = sum(t["pnl"] for t in trades)
    # Equity return/drawdown for a whole-range run. Period-specific trade stats avoid pretending a reset portfolio ran independently.
    eq = [v for ts,v,_ in sim["equity"] if (start_ts is None or ts >= start_ts) and (end_ts is None or ts < end_ts)]
    path = [INITIAL_CASH] + eq
    running_peak = np.maximum.accumulate(path)
    drawdowns = (running_peak - np.asarray(path)) / np.maximum(running_peak, 1e-12) * 100
    return {"variant":name,"closed_trades":len(trades),"wins":len(wins),"losses":len(losses),
            "win_rate_pct":round(100*len(wins)/len(trades),2) if trades else 0,
            "net_pnl_usdt":round(total_pnl,2),"avg_trade_pnl_usdt":round(total_pnl/len(trades),2) if trades else 0,
            "profit_factor":round(gross_win/gross_loss,3) if gross_loss else (None if not gross_win else "inf"),
            "mean_trade_return_pct":round(float(np.mean([t["pnl_pct_on_notional"] for t in trades])),3) if trades else 0,
            "portfolio_return_pct":round((sim["final_nav"]/INITIAL_CASH-1)*100,3),
            "max_drawdown_pct":round(float(np.max(drawdowns)),3) if len(drawdowns) else 0,
            "ending_equity_usdt":round(sim["final_nav"],2),
            "period_end_equity_points":len(eq),"halts_by_end":sim["halted"],
            "halt_reason":sim["halt_reason"],"halt_timestamp_ms":sim["halt_time"]}


def main():
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    variants = [
        ("current_2of3_2bar", 2, True, True, 2),
        ("2of3_4bar_window", 4, True, True, 2),
        ("2of3_6bar_window", 6, True, True, 2),
        ("2of3_no_ema_trend_gate_diagnostic", 2, False, True, 2),
        ("2of3_no_adx_regime_gate_diagnostic", 2, True, False, 2),
        ("1of3_vote_diagnostic", 2, True, True, 1),
    ]
    all_rows=[]; all_sims={}
    for name, window, trend, regime, votes in variants:
        frames={s:compute_signals(s,window,trend,regime,votes) for s in symbols}
        # Report raw candidate and sequential veto funnel from same signal data.
        funnel={"variant":name}
        for s,f in frames.items():
            funnel[s]={"bars":len(f),"candidate_buy_consensus":int(f.candidate_buy.sum()),
                       "buy_veto_regime":int(f.regime_block.sum()),"buy_veto_trend":int(f.trend_block.sum()),
                       "buy_veto_volume":int(f.volume_block.sum()),"buy_veto_confidence":int(f.confidence_block.sum()),
                       "eligible_buys":int((f.signal=="BUY").sum()),"sell_consensus":int((f.signal=="SELL").sum()),
                       "final_hold":int((f.signal=="HOLD").sum())}
        all_rows.append({"kind":"funnel","data":funnel})
        sim=simulate(frames,FEE_BPS,SLIPPAGE_BPS,enforce_risk_halts=True)
        all_sims[name]=sim
        all_rows.append({"kind":"performance_base_cost","data":summarize(name,sim)})
        stress=simulate(frames,STRESS_FEE_BPS,STRESS_SLIPPAGE_BPS,enforce_risk_halts=True)
        all_rows.append({"kind":"performance_stress_cost","data":summarize(name,stress)})
        # Whole-range chronological segments with one continuous account, trade exits attributed by exit date.
        cut_2024=int(pd.Timestamp("2024-01-01",tz="UTC").timestamp()*1000)
        cut_2026=int(pd.Timestamp("2026-01-01",tz="UTC").timestamp()*1000)
        all_rows.append({"kind":"period_2021_2023","data":summarize(name,sim,None,cut_2024)})
        all_rows.append({"kind":"period_2024_2025","data":summarize(name,sim,cut_2024,cut_2026)})
        all_rows.append({"kind":"period_2026_ytd_oct","data":summarize(name,sim,cut_2026,None)})
        uninterrupted=simulate(frames,FEE_BPS,SLIPPAGE_BPS,enforce_risk_halts=False)
        all_rows.append({"kind":"strategy_without_permanent_risk_halt_diagnostic","data":summarize(name,uninterrupted)})
        warmup_end=int(pd.Timestamp("2021-02-01",tz="UTC").timestamp()*1000)
        for period_name, period_start, period_end in (
            ("2021-02_through_2023", warmup_end, cut_2024),
            ("2024_through_2025", cut_2024, cut_2026),
            ("2026_ytd_through_oct", cut_2026, None),
        ):
            period_sim=simulate(frames,FEE_BPS,SLIPPAGE_BPS,enforce_risk_halts=False,
                                start_timestamp_ms=period_start,end_timestamp_ms=period_end)
            all_rows.append({"kind":"no_halt_"+period_name,"data":summarize(name,period_sim)})
        print(f"completed {name}; closed trades={len(sim['trades'])}; final equity={sim['final_nav']:.2f}; halted={sim['halted']}", flush=True)
    # Predeclared alternative setup: trend state + EMA20 pullback reclaim, with three RSI variants.
    pullback_variants = [
        ("pullback_rising_rsi45", {"rsi_mode":"rising45","slope_bars":3,"require_regime":True}),
        ("pullback_rsi_recovery40", {"rsi_mode":"recovery40","slope_bars":3,"require_regime":True}),
        ("pullback_no_rsi_diagnostic", {"rsi_mode":"none","slope_bars":3,"require_regime":True}),
    ]
    warmup_end=int(pd.Timestamp("2021-02-01",tz="UTC").timestamp()*1000)
    cut_2024=int(pd.Timestamp("2024-01-01",tz="UTC").timestamp()*1000)
    cut_2026=int(pd.Timestamp("2026-01-01",tz="UTC").timestamp()*1000)
    for name, kwargs in pullback_variants:
        frames={s:compute_pullback_signals(s,**kwargs) for s in symbols}
        funnel={"variant":name}
        for s,f in frames.items():
            funnel[s]={"bars":len(f),"eligible_buy_signals":int((f.signal=="BUY").sum()),
                       "sell_signals":int((f.signal=="SELL").sum())}
        all_rows.append({"kind":"funnel","data":funnel})
        for period_name, period_start, period_end in (
            ("2021-02_through_2023",warmup_end,cut_2024),
            ("2024_through_2025",cut_2024,cut_2026),
            ("2026_ytd_through_oct",cut_2026,None),
        ):
            sim=simulate(frames,FEE_BPS,SLIPPAGE_BPS,enforce_risk_halts=False,
                         start_timestamp_ms=period_start,end_timestamp_ms=period_end)
            all_rows.append({"kind":"pullback_"+period_name,"data":summarize(name,sim)})
            if period_name=="2026_ytd_through_oct":
                all_sims[name]=sim
        print(f"completed {name}",flush=True)
    # Validate vectorized 2-of-3 variants against the repository's actual strategy class.
    validations={}
    for name, window, trend, regime, votes in variants:
        if votes != 2:
            continue
        for s in symbols:
            frame=compute_signals(s,window,trend,regime,votes)
            validations[f"{name}:{s}"]=validate_against_actual_strategy(
                s,frame,sample_count=100,crossover_window=window,
                trend_enabled=trend,regime_enabled=regime)
    # Save trades/equity compactly for inspectability.
    (OUT/"summary.json").write_text(json.dumps({"method":{"source":"Binance official 1h Spot monthly klines","period":"2021-01 through 2026-10 completed bars","execution":"signal at close, fill at next open; fee/slippage haircuts on each side","baseline_cost_bps_each_side":{"fee":FEE_BPS,"slippage":SLIPPAGE_BPS},"stress_cost_bps_each_side":{"fee":STRESS_FEE_BPS,"slippage":STRESS_SLIPPAGE_BPS},"ohlcv_stop_assumption":"if stop and target hit within same hourly bar, assume stop first","limitations":["historical OHLCV cannot reconstruct actual spread, order-book depth, exchange filters, exact fill fees, or 60-second trailing-stop path","rolling risk halts and three-loss halt are approximated at hourly close","strategy-only no-halt scenarios are diagnostic and are not deployed risk behavior","this is a research backtest, not evidence of future profitability"]},"validation":validations,"results":all_rows,"trades":{k:v['trades'] for k,v in all_sims.items()}},indent=2,default=str),encoding="utf-8")
    print(json.dumps({"validation":validations,"summary":all_rows},indent=2))

if __name__ == "__main__":
    main()
