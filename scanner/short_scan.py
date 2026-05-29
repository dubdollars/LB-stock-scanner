"""
Short / mean-reversion scanner pipeline.

Independent ~25-min scan that downloads its own price data and identifies
overbought stocks with high probability of pulling back over 1-3 weeks.

Distinct from the long scan:
  - Filter logic looks for OVEREXTENDED leaders, not breakouts
  - Returns ShortScanResult (separate dataclass) cached separately from longs
  - Tags candidates as ANTICIPATE (overbought-only) or CONFIRMED (reversal signal present)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Callable, Optional

import pandas as pd
import yfinance as yf

from .core import (
    ScanSettings, calc_rsi, clean_history, collect_tickers, days_until,
    fetch_fundamentals_sector_mktcap, format_mktcap, safe_get_info,
    SECTOR_ABBREV, SOURCE_STATUS, fetch_sp500_set, fetch_nq100_set,
)
from .short_setups import (
    calc_rsi2,
    detect_distribution_day,
    detect_bearish_reversal_candle,
    detect_climax_run,
    detect_bb_upper_rejection,
    calc_atr_stretch,
    calc_weekly_rsi,
    detect_closed_below_prior_low,
)

ProgressCallback = Optional[Callable[[str, int, int, str], None]]


# ---------------------------------------------------------------------------
# Settings + result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ShortScanSettings:
    """Settings specific to the short scan. Intentionally separate from
    long-scan settings so each can evolve independently."""
    min_price: float = 10.00          # higher than long scan (better options liquidity)
    min_avg_volume: int = 500_000     # higher than long scan (must be shortable / liquid)
    min_rsi: float = 75.0             # entry threshold
    min_pct_above_50ma: float = 25.0  # stretch threshold
    require_market_overbought: bool = False  # gate by SPY/QQQ RSI > 70
    ticker_subset: Optional[list[str]] = None


@dataclass
class ShortScanResult:
    """Everything the short-candidates page needs from one short scan."""
    timestamp: str
    settings: dict
    spy_rsi: Optional[float]
    qqq_rsi: Optional[float]
    market_overbought: bool
    universe_size: int
    candidates: list[dict] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)
    skipped_history: int = 0
    skipped_liquidity: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Market overbought check
# ---------------------------------------------------------------------------

def _market_overbought() -> tuple[Optional[float], Optional[float], bool]:
    """Returns (spy_rsi, qqq_rsi, both_overbought).
    Both RSIs > 70 = market is broadly overbought, mean-reversion edge for shorts."""
    try:
        start = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
        spy = yf.Ticker("SPY").history(start=start, interval="1d")
        qqq = yf.Ticker("QQQ").history(start=start, interval="1d")
        if isinstance(spy.columns, pd.MultiIndex):
            spy.columns = spy.columns.get_level_values(0)
        if isinstance(qqq.columns, pd.MultiIndex):
            qqq.columns = qqq.columns.get_level_values(0)
        spy = clean_history(spy)
        qqq = clean_history(qqq)
        spy_rsi = calc_rsi(spy["Close"]) if len(spy) > 14 else None
        qqq_rsi = calc_rsi(qqq["Close"]) if len(qqq) > 14 else None
        ob = (spy_rsi is not None and spy_rsi >= 70) or (qqq_rsi is not None and qqq_rsi >= 70)
        return spy_rsi, qqq_rsi, ob
    except Exception:
        return None, None, False


# ---------------------------------------------------------------------------
# Download history — same as long scan but uses ShortScanSettings thresholds
# ---------------------------------------------------------------------------

def _download_history(
    tickers: list[str],
    settings: ShortScanSettings,
    progress: ProgressCallback = None,
) -> tuple[dict, int, int]:
    start_date = (datetime.now() - timedelta(days=540)).strftime("%Y-%m-%d")
    stock_cache: dict[str, pd.DataFrame] = {}
    skipped_liquidity = 0
    skipped_history = 0
    total = len(tickers)

    for i, ticker in enumerate(tickers):
        if progress and i % 25 == 0:
            progress("history", i, total, f"Downloading {ticker} ({i}/{total})")
        try:
            df = yf.Ticker(ticker).history(start=start_date, interval="1d")
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = clean_history(df)
            if len(df) < 252:
                skipped_history += 1
                continue
            current_price = df["Close"].iloc[-1]
            avg_volume = df["Volume"].rolling(50).mean().iloc[-1]
            if current_price < settings.min_price:
                skipped_liquidity += 1
                continue
            if avg_volume < settings.min_avg_volume:
                skipped_liquidity += 1
                continue
            stock_cache[ticker] = df
            if i % 100 == 0 and i > 0:
                time.sleep(0.5)
        except Exception:
            continue
    return stock_cache, skipped_history, skipped_liquidity


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_short_candidate(c: dict) -> tuple[int, list[str], list[str]]:
    """0-100 composite. Returns (score, reasons, caveats)."""
    score = 0
    reasons = []
    caveats = []

    rsi = c.get("RSI", 0) or 0
    if rsi >= 85:
        score += 25; reasons.append(f"extreme RSI ({rsi:.1f})")
    elif rsi >= 80:
        score += 20; reasons.append(f"very overbought RSI ({rsi:.1f})")
    elif rsi >= 75:
        score += 10; reasons.append(f"overbought RSI ({rsi:.1f})")

    stretch = c.get("Pct_Above_50MA", 0) or 0
    if stretch >= 40:
        score += 25; reasons.append(f"extreme stretch (+{stretch:.1f}% above 50MA)")
    elif stretch >= 30:
        score += 20; reasons.append(f"very stretched (+{stretch:.1f}% above 50MA)")
    elif stretch >= 25:
        score += 10; reasons.append(f"stretched (+{stretch:.1f}% above 50MA)")

    setup = c.get("Setup_Tier", "")
    if setup == "CONFIRMED":
        score += 15; reasons.append("confirmation signal present")

    # Bonus signals
    rsi2 = c.get("RSI2", 0) or 0
    if rsi2 >= 95:
        score += 5; reasons.append(f"Connors RSI(2)={rsi2:.0f} extreme")
    elif rsi2 >= 90:
        score += 3

    if c.get("Climax_Run"):
        gain = c.get("Run_Pct_15d", 0)
        score += 5; reasons.append(f"climax run (+{gain:.0f}% in 15 bars on rising vol)")
    if c.get("BB_Upper_Rej"):
        score += 5; reasons.append("BB upper-band rejection")
    if c.get("Distribution_Day"):
        score += 5; reasons.append("distribution day (heavy-vol drop)")
    if c.get("Bearish_Candle"):
        pat = c.get("Bearish_Candle_Pattern") or "bearish reversal candle"
        score += 5; reasons.append(pat.lower())
    weekly_rsi = c.get("Weekly_RSI", 0) or 0
    if weekly_rsi >= 70:
        score += 5; reasons.append(f"weekly RSI overbought ({weekly_rsi:.0f})")

    atr_stretch = c.get("ATR_Stretch", 0) or 0
    if atr_stretch >= 3.5:
        score += 5; reasons.append(f"{atr_stretch:.1f} ATRs above 21-EMA (extreme)")
    elif atr_stretch >= 2.5:
        score += 3

    if c.get("Market_Overbought"):
        score += 10; reasons.append("market also overbought (SPY/QQQ RSI>=70)")

    # Penalties
    days_to_er = c.get("Days_To_Earnings")
    if days_to_er is not None and 0 <= days_to_er <= 14:
        score -= 15; caveats.append(f"earnings imminent ({days_to_er}d) — binary catalyst risk")

    if c.get("Industry_Rank") and c.get("Industry_Rank") <= 3:
        score -= 10; caveats.append("top-3 industry leader (true leaders pull back less)")

    score = max(0, min(100, score))
    return score, reasons, caveats


# ---------------------------------------------------------------------------
# Filter pipeline
# ---------------------------------------------------------------------------

def run_short_filters(
    stock_cache: dict,
    fund_map: dict,
    sector_map: dict,
    mktcap_map: dict,
    earnings_map: dict,
    industry_ranks: dict,
    sp500_set: set,
    nq100_set: set,
    settings: ShortScanSettings,
    market_overbought: bool,
    progress: ProgressCallback = None,
) -> tuple[list[dict], dict]:
    candidates: list[dict] = []
    d_rsi = d_stretch = d_trend = d_other = 0
    total = len(stock_cache)

    for i, (ticker, data) in enumerate(stock_cache.items()):
        if progress and i % 50 == 0:
            progress("filter", i, total, f"Short-filtering ({i}/{total})")
        try:
            close = float(data["Close"].iloc[-1])
            sma_50 = float(data["Close"].rolling(50).mean().iloc[-1])
            sma_150 = float(data["Close"].rolling(150).mean().iloc[-1])
            sma_200 = float(data["Close"].rolling(200).mean().iloc[-1])

            # ---- Required: stock is in an uptrend (don't short broken stocks) ----
            if not (close > sma_50 > sma_150 > sma_200):
                d_trend += 1
                continue

            # ---- Required: overbought ----
            rsi_val = calc_rsi(data["Close"])
            if rsi_val is None or rsi_val < settings.min_rsi:
                d_rsi += 1
                continue

            # ---- Required: stretched above 50MA ----
            pct_above_50 = (close - sma_50) / sma_50 * 100 if sma_50 > 0 else 0
            if pct_above_50 < settings.min_pct_above_50ma:
                d_stretch += 1
                continue

            # ---- Optional gating: market must also be overbought ----
            if settings.require_market_overbought and not market_overbought:
                d_other += 1
                continue

            # ---- Confirmation signals ----
            dist = detect_distribution_day(data)
            bear = detect_bearish_reversal_candle(data)
            bb_rej = detect_bb_upper_rejection(data)
            climax = detect_climax_run(data, lookback=15)
            atr_stretch = calc_atr_stretch(data)
            rsi2 = calc_rsi2(data["Close"])
            weekly_rsi = calc_weekly_rsi(data)
            broke_prior_low = detect_closed_below_prior_low(data)

            # CONFIRMED tier requires an ACTIONABLE today-bar weakness trigger.
            # Weekly RSI overbought is intentionally NOT in this list — it's a
            # *condition* (multi-timeframe overbought), not a *trigger*. It
            # still appears in the SIGNALS column and adds to the score, but
            # without one of these four real reversal signals, the candidate
            # stays as ANTICIPATE. Otherwise in broadly overbought markets
            # (SPY/QQQ weekly RSI >= 70) basically every candidate would
            # auto-promote to CONFIRMED, leaving ANTICIPATE empty.
            confirmed_signals = (
                dist["is_dist_day"]
                or bear["is_bearish"]
                or bb_rej["is_rejection"]
                or broke_prior_low
            )
            setup_tier = "CONFIRMED" if confirmed_signals else "ANTICIPATE"

            # ---- Trade execution levels (sell-stop entry) ----
            today_low = float(data["Low"].iloc[-1])
            today_high = float(data["High"].iloc[-1])
            recent_high_5 = float(data["High"].iloc[-5:].max())
            short_below = round(today_low * 0.995, 2)  # 0.5% below today's low
            cover_stop = round(max(today_high, recent_high_5) * 1.01, 2)  # 1% above the high
            risk_per_share = cover_stop - short_below
            risk_pct = (risk_per_share / short_below * 100) if short_below > 0 else 0

            # Profit targets — pullback levels
            target_20ma = round(float(data["Close"].rolling(20).mean().iloc[-1]), 2)
            target_50ma = round(sma_50, 2)

            pat = bear.get("pattern") if bear.get("is_bearish") else None
            sector_val, industry_val = sector_map.get(ticker, ("N/A", "N/A"))
            ind_rank = (industry_ranks.get(industry_val) or [None])[0] if industry_ranks else None

            # Index label
            t_clean = ticker.replace("-", ".")
            if t_clean in sp500_set or ticker in sp500_set:
                index_label = "S&P500"
            elif t_clean in nq100_set or ticker in nq100_set:
                index_label = "NQ100"
            else:
                index_label = "RUT"

            candidates.append({
                "Ticker": ticker,
                "Setup_Tier": setup_tier,
                "RSI": round(rsi_val, 1),
                "RSI2": rsi2 if rsi2 is not None else 0.0,
                "Weekly_RSI": weekly_rsi if weekly_rsi is not None else 0.0,
                "Pct_Above_50MA": round(pct_above_50, 2),
                "Pct_Above_200MA": round((close - sma_200) / sma_200 * 100 if sma_200 > 0 else 0, 2),
                "ATR_Stretch": atr_stretch if atr_stretch is not None else 0.0,
                "Run_Pct_15d": climax.get("gain_pct", 0.0),
                "Climax_Run": climax.get("is_climax", False),
                "Distribution_Day": dist.get("is_dist_day", False),
                "Vol_Ratio": dist.get("vol_ratio", 0.0),
                "BB_Upper_Rej": bb_rej.get("is_rejection", False),
                "Bearish_Candle": bear.get("is_bearish", False),
                "Bearish_Candle_Pattern": pat,
                "Broke_Prior_Low": broke_prior_low,
                "Market_Overbought": market_overbought,
                "Price": round(close, 2),
                "Short_Below": short_below,
                "Cover_Stop": cover_stop,
                "Risk_Pct": round(risk_pct, 2),
                "Target_20MA": target_20ma,
                "Target_50MA": target_50ma,
                "Fundamentals": fund_map.get(ticker, "N/A"),
                "Days_To_Earnings": days_until(earnings_map.get(ticker)),
                "Earnings_Date": earnings_map.get(ticker),
                "Index": index_label,
                "Industry_Rank": ind_rank,
                "Market_Cap": mktcap_map.get(ticker, "N/A"),
                "Sector": sector_val,
                "Industry": industry_val,
            })
        except Exception:
            d_other += 1
            continue

    # Score every candidate
    for c in candidates:
        s, reasons, caveats = score_short_candidate(c)
        c["Score"] = s
        c["Score_Reasons"] = reasons
        c["Score_Caveats"] = caveats

    # Build display strings: SIGNALS column = which bonus signals fired
    for c in candidates:
        sigs = []
        if c.get("Climax_Run"):
            sigs.append("CLIMAX RUN")
        if c.get("Distribution_Day"):
            sigs.append("DIST DAY")
        if c.get("Bearish_Candle"):
            sigs.append("BEARISH CANDLE")
        if c.get("BB_Upper_Rej"):
            sigs.append("BB UPPER REJ")
        if c.get("Broke_Prior_Low"):
            sigs.append("BROKE PRIOR LOW")
        if c.get("Weekly_RSI", 0) >= 70:
            sigs.append("WEEKLY OB")
        if c.get("RSI2", 0) >= 95:
            sigs.append("RSI2>=95")
        c["Signals"] = sigs
        c["Signals_Display"] = " | ".join(sigs) if sigs else "-"

    diagnostics = {
        "failed_trend": d_trend,
        "failed_rsi": d_rsi,
        "failed_stretch": d_stretch,
        "other": d_other,
        "passed": len(candidates),
    }
    return candidates, diagnostics


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

def run_short_scan(settings: ShortScanSettings, progress: ProgressCallback = None) -> ShortScanResult:
    """Independent ~25-min scan that finds overbought short candidates."""
    timestamp = datetime.utcnow().isoformat() + "Z"

    # Phase 0: market overbought
    if progress:
        progress("market", 0, 1, "Checking SPY/QQQ overbought…")
    spy_rsi, qqq_rsi, mkt_ob = _market_overbought()

    # Phase 1: tickers
    if settings.ticker_subset is not None:
        if progress:
            progress("tickers", 0, 1, f"Using {len(settings.ticker_subset)} tickers from subset")
        tickers = list(settings.ticker_subset)
        sp500_set = fetch_sp500_set()
        nq100_set = fetch_nq100_set()
    else:
        tickers, sp500_set, nq100_set = collect_tickers(progress)

    # Phase 1: download
    stock_cache, skipped_history, skipped_liquidity = _download_history(
        tickers, settings, progress
    )

    # Phase 1.8: fundamentals + sector + earnings
    fund_map, sector_map, mktcap_map, earnings_map, _peg_map = fetch_fundamentals_sector_mktcap(
        stock_cache, progress
    )

    # Phase 1.9: industry ranks (using long-scan helper for consistency)
    if progress:
        progress("groups", 0, 1, "Ranking industry groups…")
    # Quick local industry rank (skip universe-RS — we don't have it here without
    # running the full long pipeline; rank by RSI mean as a proxy for momentum).
    industry_ranks = {}
    rows = []
    for tkr, df in stock_cache.items():
        si = sector_map.get(tkr)
        if not si or si == ("N/A", "N/A"):
            continue
        _, industry = si
        if not industry or industry == "N/A":
            continue
        r = calc_rsi(df["Close"])
        if r is not None:
            rows.append({"Industry": industry, "RSI": r})
    if rows:
        df_ind = pd.DataFrame(rows)
        grp = df_ind.groupby("Industry").agg(
            mean_rsi=("RSI", "mean"), n=("RSI", "count"),
        ).reset_index()
        grp = grp[grp["n"] >= 3].sort_values("mean_rsi", ascending=False).reset_index(drop=True)
        for rank, row in enumerate(grp.itertuples(index=False), start=1):
            industry_ranks[row.Industry] = (rank, round(float(row.mean_rsi), 1), int(row.n))

    # Phase 2: filter
    candidates, diagnostics = run_short_filters(
        stock_cache, fund_map, sector_map, mktcap_map, earnings_map,
        industry_ranks, sp500_set, nq100_set, settings, mkt_ob, progress,
    )

    if progress:
        progress("done", 1, 1, "Short scan complete")

    diagnostics = dict(diagnostics)
    diagnostics["source_status"] = dict(SOURCE_STATUS)

    return ShortScanResult(
        timestamp=timestamp,
        settings=asdict(settings),
        spy_rsi=spy_rsi,
        qqq_rsi=qqq_rsi,
        market_overbought=mkt_ob,
        universe_size=len(stock_cache),
        candidates=candidates,
        diagnostics=diagnostics,
        skipped_history=skipped_history,
        skipped_liquidity=skipped_liquidity,
    )
