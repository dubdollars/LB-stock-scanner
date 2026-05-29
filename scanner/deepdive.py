"""
Per-stock deep-dive data fetcher.

Returns everything the deep-dive page needs in one call:
  - 1y daily OHLCV with SMAs (50/150/200)
  - RS Line vs SPY (with new-high markers)
  - 8-quarter EPS / Revenue history with YoY growth
  - Margins, sector context
  - Next earnings date
  - Pre-computed VCP contraction zones (rolling stdev windows)
  - Trade execution math: buy trigger, stop, R-multiples at 1R/2R/3R
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

from .core import clean_history, calc_rsi, safe_get_info, format_mktcap
from .setups import (
    calc_atr,
    detect_vcp_contractions,
    vcp_quality_score,
    find_pivot_point,
    calculate_smart_stop,
    detect_power_play,
    detect_breakout_today,
    detect_extension_from_50ma,
)


@dataclass
class DeepDiveData:
    ticker: str
    company_name: str
    sector: str
    industry: str
    market_cap: str
    current_price: float
    pct_from_52w_high: float

    # Price history (for chart)
    history: pd.DataFrame  # cols: Open, High, Low, Close, Volume, SMA50, SMA150, SMA200

    # RS line
    rs_line: pd.Series  # indexed by date
    rs_new_high_dates: list  # dates where RS line hit a new 50d high

    # VCP zones — list of (start_date, end_date, contraction_pct)
    vcp_zones: list

    # Fundamentals
    eps_history: pd.DataFrame  # quarter, eps, yoy_pct
    revenue_history: pd.DataFrame  # quarter, revenue, yoy_pct
    profit_margin: Optional[float]
    operating_margin: Optional[float]

    # Trade execution
    buy_trigger: float
    natural_stop: float
    risk_per_share: float
    risk_pct: float
    shares_for_risk: int  # at user's account_size + risk_per_trade
    target_1r: float
    target_2r: float
    target_3r: float
    target_5pct: float
    target_10pct: float
    target_20pct: float

    # Calendar
    next_earnings_date: Optional[str]

    # Indicators
    rs_rating: Optional[int]
    rsi_14: Optional[float]

    # Setup detail (populated from setups.py)
    pivot_price: Optional[float] = None
    pivot_date: Optional[str] = None
    stop_reason: Optional[str] = None
    stop_candidates: dict = field(default_factory=dict)
    atr_20: Optional[float] = None
    vcp_quality: Optional[str] = None
    vcp_contractions: list = field(default_factory=list)  # list of {peak, trough, depth_pct}
    is_breakout_today: bool = False
    vol_vs_avg: float = 0.0
    is_power_play: bool = False
    power_play_gain: float = 0.0
    pct_above_50ma: float = 0.0
    is_extended: bool = False

    # ---- Tier 1 fundamentals: Key Ratios + Ownership (from yfinance .info) ----
    roe: Optional[float] = None                 # % (O'Neil/Minervini want >=17%)
    debt_to_equity: Optional[float] = None      # %
    forward_pe: Optional[float] = None
    trailing_pe: Optional[float] = None
    peg_ratio: Optional[float] = None
    price_to_book: Optional[float] = None
    gross_margin: Optional[float] = None        # %
    institutional_pct: Optional[float] = None   # % shares held by institutions
    insider_pct: Optional[float] = None         # % shares held by insiders
    float_shares: Optional[float] = None        # absolute count
    shares_outstanding: Optional[float] = None
    short_pct_of_float: Optional[float] = None  # %
    short_ratio: Optional[float] = None         # days to cover


def _calc_vcp_zones(close: pd.Series) -> list:
    """
    Detect rolling windows where 20d stdev / 60d stdev < 0.85 (contraction)
    AND 5d stdev / 20d stdev < 0.55 (current tightness).

    Returns list of (start_idx, end_idx, contraction_pct).
    Only the most recent ~5 distinct zones (merged if adjacent).
    """
    if len(close) < 60:
        return []
    std_5 = close.rolling(5).std()
    std_20 = close.rolling(20).std()
    std_60 = close.rolling(60).std()

    contracting = (std_20 / std_60) < 0.85
    tight = (std_5 / std_20) < 0.55
    flag = (contracting & tight).fillna(False)

    # Merge contiguous True regions into zones, only on the last 180 bars
    flag = flag.iloc[-180:] if len(flag) > 180 else flag
    zones = []
    in_zone = False
    start = None
    for idx, val in flag.items():
        if val and not in_zone:
            in_zone = True
            start = idx
        elif not val and in_zone:
            in_zone = False
            zones.append((start, idx))
    if in_zone and start is not None:
        zones.append((start, flag.index[-1]))

    out = []
    for start_dt, end_dt in zones[-5:]:
        try:
            window = close.loc[start_dt:end_dt]
            if len(window) >= 3:
                contraction_pct = float(
                    (window.max() - window.min()) / window.max() * 100
                )
                out.append((start_dt, end_dt, round(contraction_pct, 2)))
        except Exception:
            continue
    return out


def _quarterly_yoy(series: pd.Series, label: str) -> pd.DataFrame:
    """Build a DataFrame of (quarter, value, yoy_pct) sorted oldest -> newest, last 8 quarters."""
    if series is None or len(series) == 0:
        return pd.DataFrame(columns=["quarter", label.lower(), "yoy_pct"])
    s = series.dropna().sort_index()  # oldest first after sort
    rows = []
    for i, (dt, val) in enumerate(s.items()):
        yoy = None
        if i >= 4:
            prev = s.iloc[i - 4]
            if prev and prev != 0:
                yoy = (val - prev) / abs(prev) * 100
        rows.append({"quarter": dt, label.lower(): float(val), "yoy_pct": yoy})
    df = pd.DataFrame(rows).tail(8)
    return df


def fetch_deep_dive(
    ticker: str,
    account_size: float = 10_000.0,
    risk_per_trade: float = 0.0125,
) -> Optional[DeepDiveData]:
    """Fetch all data needed for the deep-dive page. Returns None on failure."""
    try:
        t_obj = yf.Ticker(ticker)

        # 1y of daily data (more for SMAs to stabilize)
        start = (datetime.now() - timedelta(days=420)).strftime("%Y-%m-%d")
        hist = t_obj.history(start=start, interval="1d")
        if isinstance(hist.columns, pd.MultiIndex):
            hist.columns = hist.columns.get_level_values(0)
        hist = clean_history(hist)
        if len(hist) < 50:
            return None

        # SMAs
        hist["SMA50"] = hist["Close"].rolling(50).mean()
        hist["SMA150"] = hist["Close"].rolling(150).mean()
        hist["SMA200"] = hist["Close"].rolling(200).mean()

        close = hist["Close"]
        current_price = float(close.iloc[-1])
        high_52w = float(close.tail(252).max()) if len(close) >= 50 else float(close.max())
        pct_from_high = (current_price - high_52w) / high_52w * 100

        # SPY for RS line
        spy = yf.Ticker("SPY").history(start=start, interval="1d")
        if isinstance(spy.columns, pd.MultiIndex):
            spy.columns = spy.columns.get_level_values(0)
        spy = clean_history(spy)
        spy_close = spy["Close"].reindex(hist.index).ffill()
        rs_line = (close / spy_close).dropna()

        # RS line new-high markers (50d rolling max)
        rs_50d_max = rs_line.rolling(50, min_periods=10).max()
        new_high_dates = list(rs_line[rs_line >= rs_50d_max * 0.999].index[-30:])

        # VCP zones
        vcp_zones = _calc_vcp_zones(close)

        # Fundamentals
        info = safe_get_info(t_obj)
        company_name = info.get("longName", info.get("shortName", ticker))
        sector = info.get("sector", "N/A")
        industry = info.get("industry", "N/A")
        market_cap = format_mktcap(info.get("marketCap"))
        profit_margin = info.get("profitMargins")
        operating_margin = info.get("operatingMargins")
        if profit_margin is not None:
            profit_margin = round(float(profit_margin) * 100, 2)
        if operating_margin is not None:
            operating_margin = round(float(operating_margin) * 100, 2)

        # ---- Tier 1 fundamentals: Key Ratios + Ownership (from .info) ----
        def _pct(key):
            v = info.get(key)
            try:
                return round(float(v) * 100, 2) if v is not None else None
            except Exception:
                return None

        def _num(key):
            v = info.get(key)
            try:
                return round(float(v), 2) if v is not None else None
            except Exception:
                return None

        roe = _pct("returnOnEquity")
        gross_margin = _pct("grossMargins")
        institutional_pct = _pct("heldPercentInstitutions")
        insider_pct = _pct("heldPercentInsiders")
        short_pct_of_float = _pct("shortPercentOfFloat")
        # debt/equity from yfinance is already a percentage value (e.g., 175.0)
        debt_to_equity_raw = _num("debtToEquity")
        debt_to_equity = debt_to_equity_raw  # already in %, no conversion
        forward_pe = _num("forwardPE")
        trailing_pe = _num("trailingPE")
        peg_ratio = _num("pegRatio")
        price_to_book = _num("priceToBook")
        short_ratio = _num("shortRatio")
        float_shares_raw = info.get("floatShares")
        shares_outstanding_raw = info.get("sharesOutstanding")
        try:
            float_shares = float(float_shares_raw) if float_shares_raw is not None else None
        except Exception:
            float_shares = None
        try:
            shares_outstanding = float(shares_outstanding_raw) if shares_outstanding_raw is not None else None
        except Exception:
            shares_outstanding = None

        # Quarterly EPS / Revenue
        try:
            fin = t_obj.quarterly_financials
        except Exception:
            fin = pd.DataFrame()

        eps_series = pd.Series(dtype=float)
        rev_series = pd.Series(dtype=float)
        if not fin.empty:
            if "Net Income" in fin.index:
                eps_series = fin.loc["Net Income"]
            if "Total Revenue" in fin.index:
                rev_series = fin.loc["Total Revenue"]

        eps_history = _quarterly_yoy(eps_series, "EPS")
        revenue_history = _quarterly_yoy(rev_series, "Revenue")

        # Next earnings date
        next_earnings = None
        try:
            cal = t_obj.calendar
            if cal is not None and len(cal) > 0:
                # yfinance returns either dict or DataFrame depending on version
                if isinstance(cal, dict):
                    val = cal.get("Earnings Date")
                    if isinstance(val, list) and val:
                        val = val[0]
                    if val:
                        next_earnings = str(val)[:10]
                else:
                    if "Earnings Date" in cal.index:
                        val = cal.loc["Earnings Date"].iloc[0]
                        next_earnings = str(val)[:10]
        except Exception:
            pass

        # ---- v2 setup detail (matches scanner core logic) ----
        sma_50_now = float(hist["SMA50"].iloc[-1]) if not pd.isna(hist["SMA50"].iloc[-1]) else None
        avg_vol_50 = float(hist["Volume"].rolling(50).mean().iloc[-1])

        contractions_v2 = detect_vcp_contractions(hist, lookback=80)
        vcp_q = vcp_quality_score(contractions_v2)

        pivot_info = find_pivot_point(hist, lookback=60)
        if pivot_info is None:
            pivot_price_v2 = float(hist["High"].tail(10).max())
            pivot_date_v2 = str(hist.index[-1])[:10]
        else:
            pivot_price_v2, pdt = pivot_info
            pivot_date_v2 = str(pdt)[:10]

        atr_20_val = calc_atr(hist, period=20)
        stop_info = calculate_smart_stop(hist, pivot_price_v2, atr_20_val, sma_50=sma_50_now)
        breakout_info = detect_breakout_today(hist, pivot_price_v2, avg_vol_50)
        powerplay_info = detect_power_play(hist)
        ext_info = detect_extension_from_50ma(current_price, sma_50_now)

        # Trade execution — use the smart pivot + smart stop
        buy_trigger = pivot_price_v2 * 1.005
        natural_stop = float(stop_info["stop"])
        risk_per_share = buy_trigger - natural_stop
        risk_pct = (risk_per_share / buy_trigger) if buy_trigger > 0 else 0
        dollar_risk = account_size * risk_per_trade
        shares = int(dollar_risk / risk_per_share) if risk_per_share > 0 else 0
        target_1r = buy_trigger + risk_per_share
        target_2r = buy_trigger + 2 * risk_per_share
        target_3r = buy_trigger + 3 * risk_per_share
        target_5pct = buy_trigger * 1.05
        target_10pct = buy_trigger * 1.10
        target_20pct = buy_trigger * 1.20

        # RS rating — recompute on the fly using same formula as core
        rs_rating = None
        try:
            if len(close) >= 252:
                c = close.iloc[-1]
                c_63 = close.iloc[-63]
                c_126 = close.iloc[-126]
                c_189 = close.iloc[-189]
                c_252 = close.iloc[-252]
                if c_63 != 0:
                    raw = (
                        0.4 * ((c - c_63) / c_63)
                        + 0.2 * ((c - c_126) / c_126)
                        + 0.2 * ((c - c_189) / c_189)
                        + 0.2 * ((c - c_252) / c_252)
                    )
                    rs_rating = max(1, min(99, int(50 + raw * 100)))
        except Exception:
            pass

        rsi_14 = calc_rsi(close)

        return DeepDiveData(
            ticker=ticker,
            company_name=company_name,
            sector=sector,
            industry=industry,
            market_cap=market_cap,
            current_price=round(current_price, 2),
            pct_from_52w_high=round(pct_from_high, 2),
            history=hist,
            rs_line=rs_line,
            rs_new_high_dates=new_high_dates,
            vcp_zones=vcp_zones,
            eps_history=eps_history,
            revenue_history=revenue_history,
            profit_margin=profit_margin,
            operating_margin=operating_margin,
            buy_trigger=round(buy_trigger, 2),
            natural_stop=round(natural_stop, 2),
            risk_per_share=round(risk_per_share, 2),
            risk_pct=round(risk_pct * 100, 2),
            shares_for_risk=shares,
            target_1r=round(target_1r, 2),
            target_2r=round(target_2r, 2),
            target_3r=round(target_3r, 2),
            target_5pct=round(target_5pct, 2),
            target_10pct=round(target_10pct, 2),
            target_20pct=round(target_20pct, 2),
            next_earnings_date=next_earnings,
            rs_rating=rs_rating,
            rsi_14=rsi_14,
            pivot_price=round(pivot_price_v2, 2),
            pivot_date=pivot_date_v2,
            stop_reason=stop_info.get("reason", ""),
            stop_candidates=stop_info.get("candidates", {}),
            atr_20=atr_20_val,
            vcp_quality=vcp_q.get("quality"),
            vcp_contractions=contractions_v2,
            is_breakout_today=breakout_info.get("is_breakout", False),
            vol_vs_avg=breakout_info.get("vol_ratio", 0.0),
            is_power_play=powerplay_info.get("is_power_play", False),
            power_play_gain=powerplay_info.get("gain_8wk_pct", 0.0),
            pct_above_50ma=ext_info.get("pct_above_50ma", 0.0),
            is_extended=ext_info.get("is_extended", False),
            # Tier 1 fundamentals
            roe=roe,
            debt_to_equity=debt_to_equity,
            forward_pe=forward_pe,
            trailing_pe=trailing_pe,
            peg_ratio=peg_ratio,
            price_to_book=price_to_book,
            gross_margin=gross_margin,
            institutional_pct=institutional_pct,
            insider_pct=insider_pct,
            float_shares=float_shares,
            shares_outstanding=shares_outstanding,
            short_pct_of_float=short_pct_of_float,
            short_ratio=short_ratio,
        )
    except Exception as e:
        print(f"Deep dive failed for {ticker}: {e}")
        return None
