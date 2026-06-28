"""
Setup detection — pure functions for SEPA-style pattern recognition.

Everything here takes a price DataFrame (OHLCV) and returns metrics or
classifications. No I/O, no global state.

Functions:
    calc_atr(df, period=20)              -> float
    find_swing_highs_lows(df, window=5)  -> (highs, lows) lists of (date, price)
    detect_vcp_contractions(df, lookback=80)
                                          -> list of contraction dicts
    find_pivot_point(df, lookback=60)    -> (pivot_price, pivot_date) or None
    calculate_smart_stop(df, pivot, atr) -> dict {stop, reason}
    detect_power_play(df)                -> dict {is_power_play, gain_8wk, ...}
    detect_breakout_today(df, pivot, avg_vol)
                                          -> bool
    detect_extension_from_50ma(df, sma50)-> dict {pct_above, is_extended}
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

def calc_atr(df: pd.DataFrame, period: int = 20) -> Optional[float]:
    """Average True Range over `period` bars (Wilder's smoothing)."""
    try:
        high = df["High"]
        low = df["Low"]
        close = df["Close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        val = float(atr.iloc[-1])
        if np.isnan(val):
            return None
        return round(val, 4)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Swing high / low detection
# ---------------------------------------------------------------------------

def find_swing_highs_lows(df: pd.DataFrame, window: int = 5) -> tuple[list, list]:
    """Find local maxima (swing highs) and minima (swing lows).

    A swing high at bar i means: high[i] > high[i-window..i-1] AND high[i] > high[i+1..i+window].
    Returns two lists of (date, price) tuples.
    """
    if len(df) < (2 * window + 1):
        return [], []

    highs = df["High"].values
    lows = df["Low"].values
    idx = df.index

    swing_highs = []
    swing_lows = []
    for i in range(window, len(df) - window):
        left_h = highs[i - window:i]
        right_h = highs[i + 1:i + window + 1]
        if highs[i] > left_h.max() and highs[i] > right_h.max():
            swing_highs.append((idx[i], float(highs[i])))

        left_l = lows[i - window:i]
        right_l = lows[i + 1:i + window + 1]
        if lows[i] < left_l.min() and lows[i] < right_l.min():
            swing_lows.append((idx[i], float(lows[i])))

    return swing_highs, swing_lows


# ---------------------------------------------------------------------------
# VCP contraction detection
# ---------------------------------------------------------------------------

def detect_vcp_contractions(df: pd.DataFrame, lookback: int = 80) -> list[dict]:
    """Detect actual price contractions in the last `lookback` bars.

    A contraction = a swing-high followed by a swing-low (a pullback).
    Returns a list of dicts ordered oldest -> newest:
        {peak_date, peak_price, trough_date, trough_price, depth_pct, length_bars}

    A high-quality VCP shows decreasing depth_pct from one contraction to the next
    (e.g., 25% -> 15% -> 8% -> 4%).
    """
    if len(df) < 30:
        return []

    window_df = df.tail(lookback) if len(df) > lookback else df
    swing_highs, swing_lows = find_swing_highs_lows(window_df, window=4)

    if not swing_highs or not swing_lows:
        return []

    # Merge swings into chronological events
    events = [("H", d, p) for d, p in swing_highs] + [("L", d, p) for d, p in swing_lows]
    events.sort(key=lambda x: x[1])

    # Walk events: each H followed by an L (before the next H) forms one contraction
    contractions = []
    current_peak = None
    for kind, dt, price in events:
        if kind == "H":
            current_peak = (dt, price)
        elif kind == "L" and current_peak is not None:
            peak_dt, peak_price = current_peak
            if dt <= peak_dt or peak_price <= 0:
                continue
            depth = (peak_price - price) / peak_price * 100
            length = (dt - peak_dt).days if hasattr(dt - peak_dt, "days") else 0
            # Average volume in a 5-bar window centered on the trough.
            # Used by vcp_quality_score for the volume-drying-up check.
            # Falls back to None if Volume column or lookup fails (graceful).
            trough_volume = None
            if "Volume" in df.columns:
                try:
                    pos = df.index.get_loc(dt)
                    if isinstance(pos, slice):
                        pos = pos.start
                    lo = max(0, pos - 2)
                    hi = min(len(df), pos + 3)
                    vol_slice = df["Volume"].iloc[lo:hi]
                    if len(vol_slice) > 0:
                        trough_volume = float(vol_slice.mean())
                except Exception:
                    trough_volume = None

            contractions.append({
                "peak_date":     peak_dt,
                "peak_price":    round(peak_price, 2),
                "trough_date":   dt,
                "trough_price":  round(price, 2),
                "depth_pct":     round(depth, 2),
                "length_bars":   length,
                "trough_volume": trough_volume,
            })
            current_peak = None

    return contractions


def _filter_to_single_base(contractions: list[dict],
                            peak_tolerance_pct: float = 5.0) -> list[dict]:
    """Restrict contractions to those whose peak is near the highest peak.

    A real Minervini VCP is one tightening structure UNDER a recent resistance
    level — not random pullbacks scattered across the lookback window. Each
    contraction's peak should retest near the same prior high (the level the
    breakout would clear).
    """
    if not contractions:
        return contractions
    base_high = max(c["peak_price"] for c in contractions)
    threshold = base_high * (1.0 - peak_tolerance_pct / 100.0)
    return [c for c in contractions if c["peak_price"] >= threshold]


def vcp_quality_score(contractions: list[dict]) -> dict:
    """Summarize VCP quality from a list of contractions.

    Grading rules (tightened 2026-06-06+ after SKYT-class false positives):
      A: 3+ contractions in a single base, MEANINGFULLY contracting
         (each <= 75% of prior), current depth < 6%, volume drying up
      B: 2+ contractions, current depth < 10%
      C: 1+ contraction, any depth
    """
    if not contractions:
        return {"count": 0, "depths": [], "decreasing": False,
                "volume_drying": False, "current_depth": None, "quality": "None"}

    real = [c for c in contractions if c["depth_pct"] >= 4.0]
    real = _filter_to_single_base(real, peak_tolerance_pct=5.0)
    depths = [c["depth_pct"] for c in real]

    if not real:
        return {"count": 0, "depths": [], "decreasing": False,
                "volume_drying": False, "current_depth": None, "quality": "None"}

    meaningfully_contracting = all(
        depths[i + 1] <= depths[i] * 0.75
        for i in range(len(depths) - 1)
    )
    current_depth = depths[-1]

    trough_vols = [c.get("trough_volume") for c in real]
    if all(v is not None for v in trough_vols) and len(trough_vols) >= 2:
        volume_drying = all(
            trough_vols[i + 1] <= trough_vols[i] * 1.10
            for i in range(len(trough_vols) - 1)
        )
    else:
        volume_drying = True  # graceful: missing data doesn't penalize

    if (len(real) >= 3 and meaningfully_contracting
            and current_depth < 6 and volume_drying):
        quality = "A"
    elif len(real) >= 2 and current_depth < 10:
        quality = "B"
    elif len(real) >= 1:
        quality = "C"
    else:
        quality = "None"

    return {
        "count": len(real),
        "depths": depths,
        "decreasing": meaningfully_contracting,
        "volume_drying": volume_drying,
        "current_depth": round(current_depth, 2),
        "quality": quality,
    }


# ---------------------------------------------------------------------------
# Pivot point detection
# ---------------------------------------------------------------------------

def _consolidation_high_since_swing_low(
    df: pd.DataFrame,
    current_close: float,
    lookback: int = 30,
) -> Optional[tuple]:
    """Find the natural top of a post-pullback consolidation range.

    After a stock makes a recent swing low, the highest high it has printed
    since that low is the natural pivot for a Minervini-style breakout entry —
    the top edge of whatever shelf has formed during the bounce. This is a
    much more honest pivot than the legacy "5-day high" fallback because it
    adapts to however long the consolidation has actually been forming
    (3 days, 2 weeks, whatever).

    Returns (high_price, high_date) if a valid consolidation high is found
    AND it sits inside the relevance band (within 5% above or 15% below
    current price). Returns None if no swing low exists or if the post-low
    high is too extended to be actionable — in which case the caller falls
    back to the 5-day high or some other last-resort logic.

    `window=2` (5-bar fractal) on swing-low detection keeps it sensitive
    enough to catch short consolidations that form 4-6 bars after a pullback.
    """
    if len(df) < 10:
        return None

    sub = df.tail(lookback) if len(df) > lookback else df
    _, swing_lows = find_swing_highs_lows(sub, window=2)
    if not swing_lows:
        return None

    # Most recent swing low by date — that's the bounce point we measure from
    most_recent_low_dt = max(swing_lows, key=lambda x: x[0])[0]

    # Bars strictly after the swing low (the bounce-and-consolidate phase)
    post_low = df[df.index > most_recent_low_dt]
    if post_low.empty:
        return None

    high_val = float(post_low["High"].max())
    high_dt = post_low["High"].idxmax()

    # Apply the same relevance-band filter Step 1 uses: pivot must be within
    # 5% above or 15% below current price. If the post-low high is too far
    # above current (e.g., a still-elevated post-climax wick), bail and let
    # the caller fall through to the 5-day high.
    if not (0.85 * high_val <= current_close <= 1.05 * high_val):
        return None

    return (high_val, high_dt)


def find_pivot_point(df: pd.DataFrame, lookback: int = 60) -> Optional[tuple]:
    """Find the most relevant pivot point to buy.

    Logic (in priority order):
      1. RELEVANT SWING-HIGH PIVOT — the highest 9-bar-fractal swing high in
         the lookback window that's within 5% above OR 15% below current price.
         This is the "real" Minervini pivot when a normal base has formed.
      2. POST-PULLBACK CONSOLIDATION HIGH — for stocks that recently bounced
         from a swing low and have started carving out a new range, use the
         highest high since that swing low. Adapts to the actual consolidation
         length (vs. the legacy 5-day window which arbitrarily clips it).
         Must still pass the relevance band.
      3. 5-DAY HIGH (last resort) — for extended/post-climax stocks where no
         swing low has formed yet and no swing high sits in the band. The EXT
         warning fires alongside this so the user knows the pivot is a soft
         guess, not a real resistance level.

    Returns (pivot_price, pivot_date) or None.
    """
    if len(df) < 20:
        return None

    window_df = df.tail(lookback) if len(df) > lookback else df
    current_close = float(df["Close"].iloc[-1])
    swing_highs, _ = find_swing_highs_lows(window_df, window=4)

    # Step 1: relevant swing-high pivots are within 5% above OR 15% below current.
    # That's the "near-term resistance" zone — meaningful buy/breakout levels.
    if swing_highs:
        relevant = [
            (dt, price) for dt, price in swing_highs
            if 0.85 * price <= current_close <= 1.05 * price
        ]
        if relevant:
            # Use the highest of the relevant pivots (most resistance)
            best_dt, best_price = max(relevant, key=lambda x: x[1])
            return (round(best_price, 2), best_dt)

    # Step 2: post-pullback consolidation high. Catches stocks that bounced
    # off a recent swing low and are forming a new shelf. More accurate than
    # the legacy 5-day high because it spans the actual consolidation period.
    consol = _consolidation_high_since_swing_low(df, current_close)
    if consol is not None:
        return (round(consol[0], 2), consol[1])

    # Step 3: 5-day high (last-resort fallback). All recent swing highs are
    # >15% below current AND no actionable consolidation has formed yet.
    # Stock is in a steep uptrend or still in post-climax correction with
    # no defined range. The EXT warning fires in parallel so the user knows
    # the pivot is a soft estimate, not a real resistance level.
    recent_high = float(df["High"].tail(5).max())
    recent_high_dt = df["High"].tail(5).idxmax()
    return (round(recent_high, 2), recent_high_dt)


# ---------------------------------------------------------------------------
# Smart stop placement
# ---------------------------------------------------------------------------

def calculate_smart_stop(
    df: pd.DataFrame,
    pivot: float,
    atr: Optional[float],
    sma_50: Optional[float] = None,
) -> dict:
    """Return the tightest justifiable Minervini-style stop.

    Candidates considered:
      A. Pivot - 1 ATR  (volatility-respecting)
      B. Most recent swing low in the last 30 bars
      C. Pivot * 0.93   (Minervini's hard 7% ceiling)
      D. SMA 50, if price is currently above it (secondary stop)

    Returns the HIGHEST of A/B (so the stop isn't farther than necessary)
    BUT not violating C (never wider than 7% from pivot).

    Returns {stop, reason, candidates: [...]}.
    """
    candidates = {}

    # A. Pivot - 1 ATR
    if atr is not None:
        candidates["A: pivot - 1 ATR"] = round(pivot - atr, 2)

    # B. Most recent swing low (30-bar lookback)
    try:
        _, swing_lows = find_swing_highs_lows(df.tail(30), window=3)
        if swing_lows:
            most_recent_low = max(swing_lows, key=lambda x: x[0])[1]
            candidates["B: recent swing low"] = round(most_recent_low, 2)
    except Exception:
        pass

    # C. 7% hard ceiling
    candidates["C: 7% hard cap"] = round(pivot * 0.93, 2)

    # D. SMA 50 (if price > SMA 50)
    if sma_50 is not None and df["Close"].iloc[-1] > sma_50:
        candidates["D: SMA 50"] = round(float(sma_50), 2)

    # Pick the tightest stop that's NOT above the 7% cap
    seven_pct_cap = candidates["C: 7% hard cap"]

    # Filter: only candidates >= seven_pct_cap (= no farther than 7% from pivot)
    valid = {k: v for k, v in candidates.items()
             if v >= seven_pct_cap and v < pivot}
    if not valid:
        # Fall back to 7% cap
        return {
            "stop": seven_pct_cap,
            "reason": "C: 7% hard cap (other candidates outside range)",
            "candidates": candidates,
        }

    # Highest valid stop = tightest acceptable
    best_reason = max(valid, key=valid.get)
    best_stop = valid[best_reason]
    return {
        "stop": best_stop,
        "reason": best_reason,
        "candidates": candidates,
    }


# ---------------------------------------------------------------------------
# Power Play detection
# ---------------------------------------------------------------------------

def detect_power_play(df: pd.DataFrame) -> dict:
    """Minervini Power Play (high-tight-flag) setup — TIGHTENED 2026-06-30.

    Original logic (gain ≥100%, ≥3 weeks since peak, ≤25% off peak) caught
    failed climax patterns like HPE 2026-06: stock had a parabolic 220% run,
    peaked, then broke down on heavy volume — all three loose checks passed
    while the actual price action was the OPPOSITE of a high-tight flag.

    Real high-tight flag (Minervini, "Trade Like a Stock Market Wizard"):
      - Quiet, tight consolidation after the big move
      - Volume DRYING UP during consolidation
      - Volatility CONTRACTING (5-day range < prior 20-day range)
      - Stock HOLDING above its short-term moving average
      - NO major distribution days during the pause

    All SEVEN conditions required for is_power_play = True:
      1. Gain ≥ 100% in last 40 bars (~8 weeks) — original criterion
      2. At least 10 trading bars since the peak (3-week pause, in BARS)
      3. Distance from peak to recent low ≤ 15% (tight flag, was 25%)
      4. Last 5-day average daily range ≤ prior 20-day average range
         (volatility contracting — the actual "high-tight" part)
      5. Last 5-day average volume < 80% of 50-day average (volume drying up)
      6. Today's close above 10-EMA (still holding the flag, not collapsing)
      7. No distribution day in last 5 sessions
         (no down day >2% on volume >1.5× 50-day avg)

    Returns the full diagnostic dict — even when is_power_play is False you
    can see WHICH check failed, useful for debugging false positives.
    """
    base_result = {
        "is_power_play": False, "gain_8wk_pct": 0.0,
        "bars_since_peak": 0, "distance_from_peak_pct": 0.0,
        "range_ratio": None, "volume_ratio": None,
        "above_10ema": None, "distribution_days_5": None,
        "fail_reason": "insufficient_data",
    }
    if len(df) < 60:
        return base_result

    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]
    vol   = df["Volume"]

    # ---- Condition 1: gain ≥100% in last 8 weeks vs prior 8 weeks' low ----
    recent_40 = close.tail(40)
    pre_run_idx = max(0, len(close) - 80)
    pre_run_window = close.iloc[pre_run_idx: len(close) - 40]
    if len(pre_run_window) == 0:
        base_result["fail_reason"] = "no_pre_run_window"
        return base_result

    pre_run_low = float(pre_run_window.min())
    peak_in_window = float(recent_40.max())
    if pre_run_low <= 0:
        base_result["fail_reason"] = "invalid_pre_run_low"
        return base_result

    gain_pct = (peak_in_window - pre_run_low) / pre_run_low * 100
    base_result["gain_8wk_pct"] = round(gain_pct, 2)

    # ---- Condition 2: peak ≥10 trading bars ago (in BARS, not calendar days) ----
    peak_idx = recent_40.idxmax()
    try:
        peak_pos = close.index.get_loc(peak_idx)
        bars_since_peak = (len(close) - 1) - peak_pos
    except Exception:
        bars_since_peak = 0
    base_result["bars_since_peak"] = int(bars_since_peak)

    # ---- Condition 3: distance from peak to recent low ≤15% ----
    post_peak = close.iloc[peak_pos:]
    if len(post_peak) < 5:
        base_result["fail_reason"] = "post_peak_too_short"
        return base_result
    distance_from_peak = (peak_in_window - float(post_peak.min())) / peak_in_window * 100
    base_result["distance_from_peak_pct"] = round(distance_from_peak, 2)

    # ---- Condition 4: volatility contracting (5d range vs prior 20d) ----
    daily_range = (high - low).abs()
    if len(daily_range) < 25:
        base_result["fail_reason"] = "insufficient_range_history"
        return base_result
    last5_range = float(daily_range.tail(5).mean())
    prior20_range = float(daily_range.iloc[-25:-5].mean())
    range_ratio = (last5_range / prior20_range) if prior20_range > 0 else 999.0
    base_result["range_ratio"] = round(range_ratio, 2)

    # ---- Condition 5: volume drying up (5d vs 50d avg) ----
    if len(vol) < 50:
        base_result["fail_reason"] = "insufficient_volume_history"
        return base_result
    last5_vol = float(vol.tail(5).mean())
    last50_vol = float(vol.tail(50).mean())
    volume_ratio = (last5_vol / last50_vol) if last50_vol > 0 else 999.0
    base_result["volume_ratio"] = round(volume_ratio, 2)

    # ---- Condition 6: last close above 10-EMA ----
    ema_10 = float(close.ewm(span=10, adjust=False).mean().iloc[-1])
    last_close = float(close.iloc[-1])
    above_10ema = last_close > ema_10
    base_result["above_10ema"] = bool(above_10ema)

    # ---- Condition 7: no distribution day in last 5 sessions ----
    prev_close = close.shift(1)
    pct_change = (close - prev_close) / prev_close * 100
    rolling_avg_vol_50 = vol.rolling(50).mean()
    distribution_mask = (pct_change < -2.0) & (vol > rolling_avg_vol_50 * 1.5)
    distribution_days_5 = int(distribution_mask.tail(5).sum())
    base_result["distribution_days_5"] = distribution_days_5

    # ---- ALL SEVEN must pass ----
    checks = {
        "gain": gain_pct >= 100,
        "bars_since_peak": bars_since_peak >= 10,
        "distance": distance_from_peak <= 15,
        "range_contracting": range_ratio <= 1.0,
        "volume_drying": volume_ratio < 0.8,
        "above_10ema": above_10ema,
        "no_distribution": distribution_days_5 == 0,
    }
    is_power_play = all(checks.values())

    if not is_power_play:
        # First failing check goes into fail_reason for quick debugging
        for name, passed in checks.items():
            if not passed:
                base_result["fail_reason"] = f"failed_{name}"
                break
    else:
        base_result["fail_reason"] = ""

    base_result["is_power_play"] = bool(is_power_play)
    return base_result


# ---------------------------------------------------------------------------
# CHEAT setup detection (Minervini "Volatility Contraction Cheat" entry)
# ---------------------------------------------------------------------------

def detect_cheat_setup(df: pd.DataFrame, vcp_contractions: list,
                        pivot: Optional[float], avg_vol_50: Optional[float]) -> dict:
    """Minervini "cheat" entry — slightly inside the base, before formal pivot.

    Definition (from "Trade Like a Stock Market Wizard"):
      The cheat is an earlier entry on a clean VCP setup. Rather than waiting
      for the formal breakout above the WHOLE base's high (the pivot), you
      enter when price clears the most recent CONTRACTION's high on volume.
      Cuts down the wait time but requires a tight, clean VCP for the setup
      to be valid — otherwise you're just early to a breakout that doesn't fire.

    All conditions required:
      - VCP detected with ≥2 contractions (we need a "most recent" contraction)
      - Today's close > the most recent contraction's high
      - Today's close < pivot (otherwise it's BO TODAY, not CHEAT)
      - Today's volume > avg_vol_50 × 1.0 (confirmation, lighter bar than BO)
      - Stock not already extended (close not >20% above the most recent
        contraction's low — otherwise we're chasing, not cheating)

    Returns {is_cheat, last_contraction_high, distance_to_pivot_pct, fail_reason}.
    """
    out = {
        "is_cheat": False,
        "last_contraction_high": None,
        "distance_to_pivot_pct": None,
        "fail_reason": "",
    }
    if df is None or len(df) < 30:
        out["fail_reason"] = "insufficient_data"
        return out
    if not vcp_contractions or len(vcp_contractions) < 2:
        out["fail_reason"] = "needs_2plus_contractions"
        return out
    if pivot is None or pivot <= 0:
        out["fail_reason"] = "no_pivot"
        return out
    if avg_vol_50 is None or avg_vol_50 <= 0:
        out["fail_reason"] = "no_avg_volume"
        return out

    try:
        # The "most recent contraction" — last item in the VCP list
        last_contraction = vcp_contractions[-1]
        # Contractions track {peak_price, trough_price, ...} so the contraction's
        # high is the peak that started it
        last_contr_high = float(last_contraction.get("peak_price", 0))
        last_contr_low  = float(last_contraction.get("trough_price", 0))
        if last_contr_high <= 0 or last_contr_low <= 0:
            out["fail_reason"] = "invalid_contraction"
            return out
        out["last_contraction_high"] = round(last_contr_high, 2)

        last_close = float(df["Close"].iloc[-1])
        last_vol   = float(df["Volume"].iloc[-1])

        # Above last contraction high (the cheat trigger)?
        above_last_contr = last_close > last_contr_high
        # But still below the full-base pivot (otherwise it's BO TODAY)?
        below_pivot = last_close < pivot
        # Volume confirmation (lighter than a full BO is fine)
        vol_ok = last_vol > avg_vol_50 * 1.0
        # Not extended from the contraction low
        if last_contr_low > 0:
            pct_above_contr_low = (last_close / last_contr_low - 1) * 100
        else:
            pct_above_contr_low = 0.0
        not_extended = pct_above_contr_low < 20.0

        out["distance_to_pivot_pct"] = round((pivot - last_close) / last_close * 100, 2)

        checks = {
            "above_last_contraction": above_last_contr,
            "below_pivot": below_pivot,
            "volume_ok": vol_ok,
            "not_extended": not_extended,
        }
        is_cheat = all(checks.values())
        if not is_cheat:
            for name, passed in checks.items():
                if not passed:
                    out["fail_reason"] = f"failed_{name}"
                    break
        out["is_cheat"] = bool(is_cheat)
    except Exception as e:
        out["fail_reason"] = f"error:{type(e).__name__}"
    return out


# ---------------------------------------------------------------------------
# GAP UP TODAY detection
# ---------------------------------------------------------------------------

def detect_gap_up_today(df: pd.DataFrame, avg_vol_50: Optional[float]) -> dict:
    """Gap-up entry: today's open >2% above yesterday's close, held through
    the day on above-average volume.

    Often catalyst-driven (earnings, news, upgrade). Tradable when the gap
    HOLDS — fillers are head-fakes. Requires:
      - Today's open ≥ prior close × 1.02 (gap ≥ 2%)
      - Today's close ≥ today's open (gap held, didn't fill)
      - Today's volume > avg_vol_50 × 1.5 (real institutional interest)

    Returns {is_gap_up, gap_pct, held_gap, volume_ratio, fail_reason}.
    """
    out = {
        "is_gap_up": False, "gap_pct": 0.0, "held_gap": False,
        "volume_ratio": None, "fail_reason": "",
    }
    if df is None or len(df) < 2:
        out["fail_reason"] = "insufficient_data"
        return out
    if avg_vol_50 is None or avg_vol_50 <= 0:
        out["fail_reason"] = "no_avg_volume"
        return out

    try:
        today_open  = float(df["Open"].iloc[-1])
        today_close = float(df["Close"].iloc[-1])
        today_vol   = float(df["Volume"].iloc[-1])
        prev_close  = float(df["Close"].iloc[-2])
        if prev_close <= 0:
            out["fail_reason"] = "invalid_prev_close"
            return out

        gap_pct = (today_open - prev_close) / prev_close * 100
        out["gap_pct"] = round(gap_pct, 2)
        held_gap = today_close >= today_open
        out["held_gap"] = bool(held_gap)
        vol_ratio = today_vol / avg_vol_50 if avg_vol_50 > 0 else 0.0
        out["volume_ratio"] = round(vol_ratio, 2)

        checks = {
            "gap_at_least_2pct": gap_pct >= 2.0,
            "gap_held": held_gap,
            "volume_above_1_5x": vol_ratio > 1.5,
        }
        is_gap_up = all(checks.values())
        if not is_gap_up:
            for name, passed in checks.items():
                if not passed:
                    out["fail_reason"] = f"failed_{name}"
                    break
        out["is_gap_up"] = bool(is_gap_up)
    except Exception as e:
        out["fail_reason"] = f"error:{type(e).__name__}"
    return out


# ---------------------------------------------------------------------------
# Breakout-today detection
# ---------------------------------------------------------------------------

def detect_breakout_today(
    df: pd.DataFrame, pivot: float, avg_vol_50: Optional[float],
) -> dict:
    """Did today's bar break out above the pivot on volume?

    Returns {is_breakout, vol_ratio, close_above_pivot_pct}.
    """
    try:
        last = df.iloc[-1]
        close = float(last["Close"])
        vol = float(last["Volume"])

        close_above_pivot = close > pivot
        close_above_pct = (close - pivot) / pivot * 100 if pivot > 0 else 0
        vol_ratio = (vol / avg_vol_50) if (avg_vol_50 and avg_vol_50 > 0) else 0

        # Minervini: breakout requires ~40-50%+ above-average volume.
        # We use 1.5x as the threshold (50% above average).
        is_breakout = bool(close_above_pivot and vol_ratio >= 1.5)
        return {
            "is_breakout": is_breakout,
            "vol_ratio": round(vol_ratio, 2),
            "close_above_pivot_pct": round(close_above_pct, 2),
        }
    except Exception:
        return {"is_breakout": False, "vol_ratio": 0.0, "close_above_pivot_pct": 0.0}


# ---------------------------------------------------------------------------
# Extension from 50-day MA
# ---------------------------------------------------------------------------

def detect_extension_from_50ma(close: float, sma_50: Optional[float]) -> dict:
    """Is the stock extended above its 50-day MA?

    Minervini: avoid initiating positions when price is >20% above the 50MA
    (likely to face mean-reversion pullback).

    Returns {pct_above_50ma, is_extended}.
    """
    if not sma_50 or sma_50 <= 0:
        return {"pct_above_50ma": 0.0, "is_extended": False}
    pct = (close - sma_50) / sma_50 * 100
    return {
        "pct_above_50ma": round(pct, 2),
        "is_extended": bool(pct > 20),
    }

# ---------------------------------------------------------------------------
# Extension from 50-day MA
# ---------------------------------------------------------------------------

def detect_extension_from_50ma(close: float, sma_50) -> dict:
    """Is the stock extended above its 50-day MA?

    Minervini: avoid initiating positions when price is >20% above the 50MA
    (likely to face mean-reversion pullback).

    Returns {pct_above_50ma, is_extended}.
    """
    if not sma_50 or sma_50 <= 0:
        return {"pct_above_50ma": 0.0, "is_extended": False}
    pct = (float(close) - float(sma_50)) / float(sma_50) * 100
    return {
        "pct_above_50ma": round(pct, 2),
        "is_extended": bool(pct > 20),
    }
# Fibonacci retracements + extensions
# ---------------------------------------------------------------------------

def fibonacci_levels(df, lookback: int = 120) -> Optional[dict]:
    """Auto-detect the most recent meaningful swing and compute Fibonacci
    retracement + extension levels.

    Swing detection (simple, robust):
      - swing_high = highest close in the last `lookback` sessions (~6 months)
      - swing_low  = lowest close BEFORE the swing_high inside the same window
        (this is the rally\\'s starting point — the low that the price climbed
        from to reach the swing high)

    Output dict:
      swing_high (float), swing_high_date (Timestamp),
      swing_low  (float), swing_low_date  (Timestamp),
      range_pts  (float, swing_high - swing_low),
      retracements: list of {label, pct, price} for 23.6 / 38.2 / 50 / 61.8 / 78.6 %
      extensions  : list of {label, pct, price} for 127.2 / 161.8 / 261.8 %

    Returns None if there\\'s not enough data or the swing is degenerate
    (e.g. high == low, or fewer than 30 bars of history).
    """
    try:
        if df is None or len(df) < 30 or "Close" not in df.columns:
            return None
        h = df.tail(lookback)
        if len(h) < 10:
            return None
        swing_high = float(h["Close"].max())
        swing_high_idx = h["Close"].idxmax()
        # Lowest close BEFORE the swing high — the rally start
        before_high = h.loc[:swing_high_idx]
        if len(before_high) < 2:
            # Edge case: swing high is the first bar in window — fall back to
            # using the lowest close in the full window
            before_high = h
        swing_low = float(before_high["Close"].min())
        swing_low_idx = before_high["Close"].idxmin()
        range_pts = swing_high - swing_low
        if range_pts <= 0:
            return None

        retracements = []
        for pct in (0.236, 0.382, 0.5, 0.618, 0.786):
            price = swing_high - range_pts * pct
            retracements.append({
                "label": f"{pct * 100:.1f}%".rstrip("0").rstrip("."),
                "pct": pct,
                "price": round(price, 2),
            })

        extensions = []
        for pct, label_pct in ((0.272, 127.2), (0.618, 161.8), (1.618, 261.8)):
            price = swing_high + range_pts * pct
            extensions.append({
                "label": f"{label_pct}%",
                "pct": pct,
                "price": round(price, 2),
            })

        return {
            "swing_high": round(swing_high, 2),
            "swing_high_date": swing_high_idx,
            "swing_low": round(swing_low, 2),
            "swing_low_date": swing_low_idx,
            "range_pts": round(range_pts, 2),
            "retracements": retracements,
            "extensions": extensions,
        }
    except Exception:
        return None
