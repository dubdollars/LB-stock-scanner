"""
Per-ticker SEPA diagnostic.

Given any ticker symbol, downloads its data and runs all the same tests the
main scanner uses, returning a structured pass/fail breakdown. Used by the
"Check Ticker" feature to answer "Why didn't NVDA show up in the scan?"
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

from .core import ScanSettings, clean_history, safe_get_info
from .setups import (
    calc_atr,
    detect_extension_from_50ma,
    detect_vcp_contractions,
    find_pivot_point,
    vcp_quality_score,
)


def diagnose_ticker(ticker: str, settings: ScanSettings) -> Optional[dict]:
    """Run every SEPA test on `ticker`. Returns dict with passes_all + tests list."""
    ticker = ticker.upper().strip()
    if not ticker:
        return None

    try:
        start = (datetime.now() - timedelta(days=540)).strftime("%Y-%m-%d")
        df = yf.Ticker(ticker).history(start=start, interval="1d")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = clean_history(df)
        if df is None or len(df) < 252:
            return {
                "passes_all": False,
                "tests": [{
                    "name": "Data availability",
                    "passed": False,
                    "detail": f"Only {len(df) if df is not None else 0} bars of history available; need at least 252.",
                }],
            }
    except Exception as e:
        return None

    tests = []
    all_pass = True

    # ---- Liquidity ----
    close = float(df["Close"].iloc[-1])
    avg_vol = float(df["Volume"].rolling(50).mean().iloc[-1])

    p = close >= settings.min_price
    tests.append({"name": f"Price ≥ ${settings.min_price:.2f}", "passed": p,
                  "detail": f"Current price: ${close:.2f}"})
    if not p: all_pass = False

    p = avg_vol >= settings.min_avg_volume
    tests.append({"name": f"50-day avg volume ≥ {settings.min_avg_volume:,}", "passed": p,
                  "detail": f"Avg volume: {avg_vol:,.0f}"})
    if not p: all_pass = False

    # ---- Trend Template ----
    sma_50 = df["Close"].rolling(50).mean().iloc[-1]
    sma_150 = df["Close"].rolling(150).mean().iloc[-1]
    sma_200 = df["Close"].rolling(200).mean().iloc[-1]
    low_52 = df["Close"].tail(252).min()
    high_52 = df["Close"].tail(252).max()

    p = close > sma_50
    tests.append({"name": "Price > 50-day MA", "passed": p,
                  "detail": f"Price ${close:.2f} vs 50MA ${sma_50:.2f}"})
    if not p: all_pass = False

    p = close > sma_150
    tests.append({"name": "Price > 150-day MA", "passed": p,
                  "detail": f"Price ${close:.2f} vs 150MA ${sma_150:.2f}"})
    if not p: all_pass = False

    p = close > sma_200
    tests.append({"name": "Price > 200-day MA", "passed": p,
                  "detail": f"Price ${close:.2f} vs 200MA ${sma_200:.2f}"})
    if not p: all_pass = False

    p = sma_50 > sma_150
    tests.append({"name": "50MA > 150MA", "passed": p,
                  "detail": f"50MA ${sma_50:.2f} vs 150MA ${sma_150:.2f}"})
    if not p: all_pass = False

    p = sma_150 > sma_200
    tests.append({"name": "150MA > 200MA", "passed": p,
                  "detail": f"150MA ${sma_150:.2f} vs 200MA ${sma_200:.2f}"})
    if not p: all_pass = False

    p = close > 1.30 * low_52
    tests.append({"name": "Price > 30% above 52-week low", "passed": p,
                  "detail": f"+{(close/low_52 - 1)*100:.1f}% above ${low_52:.2f}"})
    if not p: all_pass = False

    p = close > 0.75 * high_52
    tests.append({"name": "Price within 25% of 52-week high", "passed": p,
                  "detail": f"{(close/high_52 - 1)*100:.1f}% from ${high_52:.2f}"})
    if not p: all_pass = False

    sma_200_20_ago = df["Close"].rolling(200).mean().shift(20).iloc[-1]
    p = sma_200 > sma_200_20_ago
    tests.append({"name": "200MA trending up (vs 20 bars ago)", "passed": p,
                  "detail": f"Now ${sma_200:.2f} vs 20d ago ${sma_200_20_ago:.2f}"})
    if not p: all_pass = False

    # ---- Fundamentals (only enforced in Elite) ----
    try:
        t_obj = yf.Ticker(ticker)
        fin = t_obj.quarterly_financials
        eps_flag = rev_flag = False
        if "Net Income" in fin.index:
            ni = fin.loc["Net Income"]
            if len(ni) > 4 and ni.iloc[4] != 0:
                if ((ni.iloc[0] - ni.iloc[4]) / abs(ni.iloc[4])) > 0.25:
                    eps_flag = True
        if "Total Revenue" in fin.index:
            rev = fin.loc["Total Revenue"]
            if len(rev) > 4 and rev.iloc[4] != 0:
                if ((rev.iloc[0] - rev.iloc[4]) / abs(rev.iloc[4])) > 0.20:
                    rev_flag = True
    except Exception:
        eps_flag = rev_flag = False

    fund_ok = eps_flag or rev_flag
    if settings.elite_mode:
        tests.append({
            "name": "Fundamentals (Elite mode requires EPS+ or Sale+)",
            "passed": fund_ok,
            "detail": f"EPS+25% YoY: {'YES' if eps_flag else 'no'}  ·  Rev+20% YoY: {'YES' if rev_flag else 'no'}",
        })
        if not fund_ok: all_pass = False
    else:
        tests.append({
            "name": "Fundamentals (informational in Standard mode)",
            "passed": True,
            "detail": f"EPS+: {'YES' if eps_flag else 'no'}  ·  Sale+: {'YES' if rev_flag else 'no'}",
        })

    # ---- Setup quality (informational only — never disqualifies) ----
    pivot_info = find_pivot_point(df)
    if pivot_info:
        pivot_price = pivot_info[0]
        ext = detect_extension_from_50ma(close, float(sma_50))
        tests.append({
            "name": "Pivot point identified",
            "passed": True,
            "detail": f"Pivot ${pivot_price:.2f} | extension from 50MA: {ext['pct_above_50ma']:+.1f}%"
                      f"{' (EXT warning)' if ext['is_extended'] else ''}",
        })

        contractions = detect_vcp_contractions(df, lookback=80)
        vcp_q = vcp_quality_score(contractions)
        tests.append({
            "name": "VCP characterization",
            "passed": True,
            "detail": f"Quality: {vcp_q['quality']} · contractions: {vcp_q['count']}"
                      f" · current depth: {vcp_q['current_depth'] or '—'}%",
        })

    # ---- RS rating note ----
    # We can't compute the universe-relative RS in isolation, so note that.
    tests.append({
        "name": "RS rating (≥85 in Elite, ≥70 in Standard)",
        "passed": True,
        "detail": "Can't be computed for a single ticker — RS is universe-relative. "
                  "Look up this ticker in the latest scan results to see its actual RS rating.",
    })

    return {
        "passes_all": all_pass,
        "tests": tests,
    }
