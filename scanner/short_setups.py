"""
Short / mean-reversion detection — pure functions.

Companion to setups.py. Where setups.py looks for breakout/leadership signals,
this module looks for overbought-exhaustion signals: stocks that have run too
far too fast and are statistically due for a pullback over 1-3 weeks.

Functions:
    calc_rsi2(closes)                        -> float (Connors 2-period RSI)
    detect_distribution_day(df)              -> dict {is_dist_day, vol_ratio, drop_pct}
    detect_bearish_reversal_candle(df)       -> dict {pattern, is_bearish}
    detect_climax_run(df, lookback=15)       -> dict {gain_pct, vol_accelerating, is_climax}
    detect_bb_upper_rejection(df, period=20) -> dict {touched_upper, closed_inside, is_rejection}
    calc_atr_stretch(df, ema_period=21, atr_period=20) -> float (price stretch in ATRs above EMA)
    calc_weekly_rsi(df, period=14)           -> float (weekly RSI from daily data)
    detect_closed_below_prior_low(df)        -> bool (after recent new highs, broke yesterday's low)
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Connors RSI(2) — extreme short-term overbought/oversold
# ---------------------------------------------------------------------------

def calc_rsi2(closes: pd.Series) -> Optional[float]:
    """Larry Connors' famous 2-period RSI. >95 = extreme overbought,
    <5 = extreme oversold. Used for short-term mean reversion."""
    try:
        delta = closes.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        # Wilder's smoothing with period=2
        avg_gain = gain.ewm(alpha=1 / 2, min_periods=2, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / 2, min_periods=2, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        val = float(rsi.iloc[-1])
        if np.isnan(val):
            return None
        return round(val, 1)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Distribution day — high-volume down day after recent uptrend
# ---------------------------------------------------------------------------

def detect_distribution_day(df: pd.DataFrame) -> dict:
    """Today's bar is a distribution day if:
      - Closed lower than previous close
      - On volume > 1.5x 50-day average
      - After recent uptrend (price near recent highs)
    Returns {is_dist_day, vol_ratio, drop_pct}.
    """
    try:
        if len(df) < 51:
            return {"is_dist_day": False, "vol_ratio": 0.0, "drop_pct": 0.0}
        last = df.iloc[-1]
        prev = df.iloc[-2]
        avg_vol_50 = float(df["Volume"].rolling(50).mean().iloc[-1])
        if avg_vol_50 <= 0:
            return {"is_dist_day": False, "vol_ratio": 0.0, "drop_pct": 0.0}

        close = float(last["Close"])
        prev_close = float(prev["Close"])
        vol = float(last["Volume"])

        drop_pct = (close - prev_close) / prev_close * 100 if prev_close > 0 else 0
        vol_ratio = vol / avg_vol_50
        # Was price near recent highs before today?
        recent_high = float(df["High"].tail(20).max())
        near_high = close >= recent_high * 0.93  # within 7% of 20-day high

        is_dist = (drop_pct < -0.3) and (vol_ratio >= 1.5) and near_high
        return {
            "is_dist_day": bool(is_dist),
            "vol_ratio": round(vol_ratio, 2),
            "drop_pct": round(drop_pct, 2),
        }
    except Exception:
        return {"is_dist_day": False, "vol_ratio": 0.0, "drop_pct": 0.0}


# ---------------------------------------------------------------------------
# Bearish reversal candle — shooting star, bearish engulfing, evening star
# ---------------------------------------------------------------------------

def detect_bearish_reversal_candle(df: pd.DataFrame) -> dict:
    """Identify a bearish reversal candle on the most recent bar.

    Patterns checked:
      - Shooting Star: small body at bottom, long upper wick (>= 2x body),
                       small/no lower wick, after an uptrend
      - Bearish Engulfing: today's red body engulfs yesterday's green body
      - Hanging Man: similar to shooting star but with long lower wick at top
    Returns {pattern, is_bearish}.
    """
    try:
        if len(df) < 5:
            return {"pattern": None, "is_bearish": False}

        last = df.iloc[-1]
        prev = df.iloc[-2]

        o, h, l, c = float(last["Open"]), float(last["High"]), float(last["Low"]), float(last["Close"])
        po, ph, pl, pc = float(prev["Open"]), float(prev["High"]), float(prev["Low"]), float(prev["Close"])

        body = abs(c - o)
        upper_wick = h - max(c, o)
        lower_wick = min(c, o) - l
        total_range = h - l
        if total_range <= 0:
            return {"pattern": None, "is_bearish": False}

        # Was price recently making new highs (uptrend context)?
        recent_high_5d = float(df["High"].iloc[-6:-1].max())
        in_uptrend = h >= recent_high_5d  # today's high is at/above last 5 days' highs

        # Shooting Star
        if (body / total_range < 0.35
                and upper_wick >= 2.0 * body
                and lower_wick < 0.5 * body
                and in_uptrend):
            return {"pattern": "Shooting Star", "is_bearish": True}

        # Bearish Engulfing
        prev_was_green = pc > po
        today_is_red = c < o
        engulfs = (c < po) and (o > pc)
        if prev_was_green and today_is_red and engulfs and in_uptrend:
            return {"pattern": "Bearish Engulfing", "is_bearish": True}

        # Hanging Man (similar shape to hammer but at the top of an uptrend)
        if (body / total_range < 0.35
                and lower_wick >= 2.0 * body
                and upper_wick < 0.5 * body
                and in_uptrend
                and c < o):  # bearish hanging man closes red
            return {"pattern": "Hanging Man", "is_bearish": True}

        return {"pattern": None, "is_bearish": False}
    except Exception:
        return {"pattern": None, "is_bearish": False}


# ---------------------------------------------------------------------------
# Climax run — Minervini's "25%+ in 15 bars on accelerating volume"
# ---------------------------------------------------------------------------

def detect_climax_run(df: pd.DataFrame, lookback: int = 15) -> dict:
    """A climax top is a parabolic move that often marks the local top.

    Criteria:
      - 25%+ price gain over the last `lookback` bars
      - Volume in the last 5 bars is higher than the prior 5 bars (accelerating)
    Returns {gain_pct, vol_accelerating, is_climax}.
    """
    try:
        if len(df) < lookback + 10:
            return {"gain_pct": 0.0, "vol_accelerating": False, "is_climax": False}

        close = df["Close"]
        gain_pct = (close.iloc[-1] - close.iloc[-lookback]) / close.iloc[-lookback] * 100

        vol_recent_5 = float(df["Volume"].iloc[-5:].mean())
        vol_prior_5 = float(df["Volume"].iloc[-10:-5].mean())
        vol_accelerating = vol_recent_5 > vol_prior_5 * 1.15

        is_climax = (gain_pct >= 25) and vol_accelerating
        return {
            "gain_pct": round(float(gain_pct), 2),
            "vol_accelerating": bool(vol_accelerating),
            "is_climax": bool(is_climax),
        }
    except Exception:
        return {"gain_pct": 0.0, "vol_accelerating": False, "is_climax": False}


# ---------------------------------------------------------------------------
# Bollinger Band upper-rejection
# ---------------------------------------------------------------------------

def detect_bb_upper_rejection(df: pd.DataFrame, period: int = 20, num_std: float = 2.0) -> dict:
    """Touched upper Bollinger Band today (or in last 2 bars) but closed back
    inside. Classic mean-reversion entry signal.
    Returns {touched_upper, closed_inside, is_rejection}.
    """
    try:
        if len(df) < period + 2:
            return {"touched_upper": False, "closed_inside": False, "is_rejection": False}

        close = df["Close"]
        sma = close.rolling(period).mean()
        std = close.rolling(period).std()
        upper = sma + num_std * std

        last_high = float(df["High"].iloc[-1])
        last_close = float(close.iloc[-1])
        last_upper = float(upper.iloc[-1])

        # Touched the upper band intraday but closed below it
        touched = last_high >= last_upper
        closed_inside = last_close < last_upper

        is_rejection = touched and closed_inside
        return {
            "touched_upper": bool(touched),
            "closed_inside": bool(closed_inside),
            "is_rejection": bool(is_rejection),
        }
    except Exception:
        return {"touched_upper": False, "closed_inside": False, "is_rejection": False}


# ---------------------------------------------------------------------------
# ATR stretch — price stretch above EMA21 in volatility units
# ---------------------------------------------------------------------------

def calc_atr_stretch(df: pd.DataFrame, ema_period: int = 21, atr_period: int = 20) -> Optional[float]:
    """How many ATRs is the current price above its 21-day EMA?

    Volatility-normalized version of "% above 50MA". 2.5+ ATRs above is
    considered extended; 3.5+ ATRs is extreme.
    """
    try:
        if len(df) < max(ema_period, atr_period) + 1:
            return None

        ema = df["Close"].ewm(span=ema_period, adjust=False).mean()

        # Wilder's ATR
        high = df["High"]
        low = df["Low"]
        close = df["Close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / atr_period, min_periods=atr_period, adjust=False).mean()

        last_close = float(close.iloc[-1])
        last_ema = float(ema.iloc[-1])
        last_atr = float(atr.iloc[-1])
        if last_atr <= 0:
            return None

        stretch = (last_close - last_ema) / last_atr
        return round(stretch, 2)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Weekly RSI — multi-timeframe overbought confirmation
# ---------------------------------------------------------------------------

def calc_weekly_rsi(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """RSI computed on weekly (W-FRI) resampled closes. Slower / smoother
    than daily RSI — confirms higher-timeframe overbought when daily fires."""
    try:
        if len(df) < period * 7:  # need ~14 weeks of daily data
            return None
        weekly = df["Close"].resample("W-FRI").last().dropna()
        if len(weekly) < period + 1:
            return None
        delta = weekly.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        val = float(rsi.iloc[-1])
        if np.isnan(val):
            return None
        return round(val, 1)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Closed below previous low after recent new highs
# ---------------------------------------------------------------------------

def detect_closed_below_prior_low(df: pd.DataFrame) -> bool:
    """First sign of weakness: stock made new highs in the last 5 bars,
    then today's close broke below yesterday's low."""
    try:
        if len(df) < 8:
            return False
        last_close = float(df["Close"].iloc[-1])
        prev_low = float(df["Low"].iloc[-2])
        # Was there a recent new high in the last 5 bars (excluding today)?
        recent_5d_high = float(df["High"].iloc[-6:-1].max())
        prior_high_20 = float(df["High"].iloc[-26:-6].max())
        made_new_high = recent_5d_high > prior_high_20
        return bool(made_new_high and last_close < prev_low)
    except Exception:
        return False
